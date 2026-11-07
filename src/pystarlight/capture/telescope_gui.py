"""The camera window's telescope docks (Qt dock widgets: docked beside the side panel, or
pulled out as windows of their own; hidden when not needed, the arrangement remembered).

NEW CODE. MountDock: the INDI connection (drivers picked from the installed ones, our own
indiserver or one already running, config [indi]), the mount's position (J2000, alt / az at the
GPS or config site), goto / goto without tracking / sync / tracking / abort / park, and the
plate-solve loop: Solve & sync (new stack -> solve -> sync) and Center (goto, solve & sync,
goto again until within CENTER_TOLERANCE_DEG, at most CENTER_ROUNDS times). Network and
slews run in threads; the dock polls its state twice a second.
"""
import threading
import time

from . import indi, localconfig, station_header
from . import mount as mt
from .qt import QtCore, QtWidgets

CENTER_TOLERANCE_DEG = 1 / 60
CENTER_ROUNDS = 4
NO_DRIVER = "(none)"
JOG_BUTTON_PX = 36


def driver_combo(entries, current):
    """A combo of (label, executable) entries plus '(none)', `current` selected (kept even when
    it is not installed here: a remote server may have it)."""
    combo = QtWidgets.QComboBox()
    combo.addItem(NO_DRIVER, "")
    for label, exe in sorted(entries):
        combo.addItem(f"{label}  ({exe})", exe)
    if current and combo.findData(current) < 0:
        combo.addItem(f"{current}  (not installed here)", current)
    combo.setCurrentIndex(max(0, combo.findData(current or "")))
    narrow(combo)
    return combo


def narrow(combo, chars=10):
    """A combo box that does not widen the panel to its longest entry (the list still shows it)."""
    combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    combo.view().setMinimumWidth(260)


def motion(st):
    return ("slewing" if st["slewing"] else "parked" if st["parked"] else
            "tracking" if st["tracking"] else "not tracking")


class MountDock(QtWidgets.QDockWidget):
    def __init__(self, main):
        super().__init__("Mount", main)
        self.setObjectName("dock_mount")         # saveState / restoreState
        self.main = main
        self.cfg = {"mount_driver": "", "focuser_driver": "", "start_server": True,
                    "host": "localhost", "port": indi.PORT, "mount_connection": "usb", "mount_port": "",
                    **localconfig.section("indi")}
        self.server = self.client = self.mount = None
        self.busy = ""                           # what a worker thread is doing
        self.error = ""
        self.job = None                          # Solve & sync / Center state
        self.drivers = indi.drivers_installed()

        self.make_settings_dialog()

        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        g.setContentsMargins(6, 6, 6, 6)
        g.setSpacing(4)
        r = 0
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.setToolTip("drivers, USB / Wi-Fi, server: menu Mount, INDI settings")
        self.connect_button.clicked.connect(self.on_connect)
        self.state_label = QtWidgets.QLabel("not connected")
        self.state_label.setWordWrap(True)
        g.addWidget(self.connect_button, r, 0)
        g.addWidget(self.state_label, r, 1, 1, 2)
        r += 1

        self.pos_label = QtWidgets.QLabel("")
        self.pos_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.pos_label.setWordWrap(True)
        g.addWidget(self.pos_label, r, 0, 1, 3)
        r += 1

        self.target_edit = QtWidgets.QLineEdit()
        self.target_edit.setPlaceholderText("target RA Dec J2000")
        self.target_edit.setToolTip("J2000: 20:41:25.9 +45:16:49, 20h41m25.9s +45d16m49s or degrees")
        g.addWidget(self.target_edit, r, 0, 1, 3)
        r += 1
        self.from_target = QtWidgets.QPushButton("From target")
        self.from_target.setToolTip("the camera window's target (Recording panel, once looked up)")
        self.from_target.clicked.connect(self.on_from_target)
        self.from_solve = QtWidgets.QPushButton("From solve")
        self.from_solve.setToolTip("the centre of the last plate solve")
        self.from_solve.clicked.connect(self.on_from_solve)
        g.addWidget(self.from_target, r, 0)
        g.addWidget(self.from_solve, r, 1)
        r += 1

        self.goto_button = QtWidgets.QPushButton("Goto")
        self.goto_button.clicked.connect(lambda: self.on_goto(track=True))
        self.goto_still = QtWidgets.QPushButton("Goto, still")
        self.goto_still.setToolTip("slew there and stop tracking (pre-pointing: the field drifts through)")
        self.goto_still.clicked.connect(lambda: self.on_goto(track=False))
        self.center_button = QtWidgets.QPushButton("Center")
        self.center_button.setToolTip(
            f"goto the target, then stack, plate-solve and sync, and go again until within "
            f"{CENTER_TOLERANCE_DEG * 60:g}′ (at most {CENTER_ROUNDS} rounds)")
        self.center_button.clicked.connect(self.on_center)
        g.addWidget(self.goto_button, r, 0)
        g.addWidget(self.goto_still, r, 1)
        g.addWidget(self.center_button, r, 2)
        r += 1
        self.sync_button = QtWidgets.QPushButton("Sync")
        self.sync_button.setToolTip("tell the mount it points at the target position")
        self.sync_button.clicked.connect(self.on_sync)
        self.solve_sync = QtWidgets.QPushButton("Solve && sync")
        self.solve_sync.setToolTip("a new stack, plate-solved, and the mount synced to its centre")
        self.solve_sync.clicked.connect(self.on_solve_sync)
        self.abort_button = QtWidgets.QPushButton("Abort")
        self.abort_button.setStyleSheet("font-weight: bold;")
        self.abort_button.setToolTip("stop any goto, slew, homing or jog")
        self.abort_button.clicked.connect(self.on_abort)
        g.addWidget(self.sync_button, r, 0)
        g.addWidget(self.solve_sync, r, 1)
        g.addWidget(self.abort_button, r, 2)
        r += 1

        # the jog cross; its background shows tracking (green) / not tracking (red)
        self.jog_frame = QtWidgets.QFrame()
        self.jog_frame.setObjectName("jog")
        jg = QtWidgets.QGridLayout(self.jog_frame)
        jg.setContentsMargins(8, 8, 8, 8)
        jg.setSpacing(4)
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.setToolTip("jog speed (the driver's rates, x sidereal)")
        self.rate_combo.activated.connect(self.on_rate)
        narrow(self.rate_combo, 5)
        self.rate_combo.view().setMinimumWidth(90)
        jg.addWidget(self.rate_combo, 1, 1, QtCore.Qt.AlignCenter)
        self.jog_buttons = {}
        for d, (row, col) in {"N": (0, 1), "W": (1, 0), "E": (1, 2), "S": (2, 1)}.items():
            b = QtWidgets.QPushButton(d)
            b.setFixedSize(JOG_BUTTON_PX, JOG_BUTTON_PX)
            b.setStyleSheet("font-weight: bold;")
            b.setToolTip(f"hold to move {dict(N='north', S='south', W='west', E='east')[d]}")
            b.setAutoRepeat(False)
            b.pressed.connect(lambda d=d: self.on_jog(d, True))
            b.released.connect(lambda d=d: self.on_jog(d, False))
            jg.addWidget(b, row, col, QtCore.Qt.AlignCenter)
            self.jog_buttons[d] = b
        self.jogging = set()
        self.jog_frame.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Maximum)
        g.addWidget(self.jog_frame, r, 0, 1, 3, QtCore.Qt.AlignHCenter)
        r += 1
        self.track_check = QtWidgets.QCheckBox("Tracking")
        self.track_check.clicked.connect(lambda on: self.mount and self.mount.set_tracking(on))
        self.park_button = QtWidgets.QPushButton("Park")
        self.park_button.clicked.connect(self.on_park)
        self.home_button = QtWidgets.QPushButton("Home")
        self.home_button.setToolTip("slew to the mount's home position (when its driver offers one)")
        self.home_button.clicked.connect(self.on_home)
        g.addWidget(self.track_check, r, 0)
        g.addWidget(self.park_button, r, 1)
        g.addWidget(self.home_button, r, 2)
        r += 1
        self.msg_label = QtWidgets.QLabel("")
        self.msg_label.setWordWrap(True)
        self.msg_label.setStyleSheet("color: gray;")
        g.addWidget(self.msg_label, r, 0, 1, 3)
        g.setRowStretch(r + 1, 1)
        self.set_tint(None)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setWidget(scroll)
        self.timer = QtCore.QTimer(self, timeout=self.poll, interval=500)
        self.timer.start()
        self.poll()

    # ---- INDI settings (a window of their own, menu Mount) ----
    def make_settings_dialog(self):
        d = self.settings_dialog = QtWidgets.QDialog(self.main)
        d.setWindowTitle("Mount: INDI settings")
        form = QtWidgets.QFormLayout(d)
        self.mount_combo = driver_combo(self.drivers.get("Telescopes", []), self.cfg.get("mount_driver"))
        self.focus_combo = driver_combo(self.drivers.get("Focusers", []), self.cfg.get("focuser_driver"))
        self.link_combo = QtWidgets.QComboBox()
        for text, data in (("USB (serial)", "usb"), ("Wi-Fi (TCP)", "wifi"), ("driver default", "")):
            self.link_combo.addItem(text, data)
        link = self.cfg.get("mount_connection", "usb")
        self.link_combo.setCurrentIndex(max(0, self.link_combo.findData(link if link is not None else "usb")))
        self.link_combo.setToolTip("how the driver reaches the mount (the AM5 driver's default is Wi-Fi)")
        self.port_edit = QtWidgets.QLineEdit(self.cfg.get("mount_port") or "")
        self.port_edit.setPlaceholderText("auto")
        self.port_edit.setToolTip("USB: serial port, e.g. /dev/ttyACM0; empty: the system port named ZWO, "
                                  "else the only one. Wi-Fi: the mount's host[:port], e.g. 192.168.1.50; "
                                  "empty: the driver's (the AM5's own hotspot 192.168.4.1:4030)")
        self.server_check = QtWidgets.QCheckBox("Start the INDI server here")
        self.server_check.setChecked(bool(self.cfg.get("start_server", True)))
        self.server_check.setToolTip("on: run indiserver with these drivers (stopped again on Disconnect "
                                     "and exit); off: connect to a server that already runs")
        host, port = self.cfg.get("host") or "localhost", self.cfg.get("port") or indi.PORT
        self.host_edit = QtWidgets.QLineEdit(f"{host}:{port}")
        self.host_edit.setToolTip("INDI server host:port")
        for c in (self.mount_combo, self.focus_combo):      # room here: the full driver names
            c.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        form.addRow("Mount driver", self.mount_combo)
        form.addRow("Connection", self.link_combo)
        form.addRow("Port / address", self.port_edit)
        form.addRow("Focuser driver", self.focus_combo)
        form.addRow("", self.server_check)
        form.addRow("INDI server", self.host_edit)
        self.settings_note = QtWidgets.QLabel("Saved in the config's [indi] on Connect.")
        self.settings_note.setStyleSheet("color: gray;")
        form.addRow(self.settings_note)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(d.hide)
        form.addRow(close)

    def show_settings(self):
        self.settings_dialog.show()
        self.settings_dialog.raise_()

    # ---- connection ----
    def on_connect(self):
        if self.client is not None:
            self.disconnect_indi()
            return
        mount_drv, focus_drv = self.mount_combo.currentData(), self.focus_combo.currentData()
        host, _, port = self.host_edit.text().strip().rpartition(":")
        try:
            port = int(port)
        except ValueError:
            host, port = self.host_edit.text().strip(), indi.PORT
        host = host or "localhost"
        own = self.server_check.isChecked()
        link, link_port = self.link_combo.currentData(), self.port_edit.text().strip()
        if localconfig.config_path() is not None:      # only into a config in use, never a new one
            try:
                localconfig.update_section("indi", {"mount_driver": mount_drv, "focuser_driver": focus_drv,
                                                    "start_server": own, "host": host, "port": port,
                                                    "mount_connection": link, "mount_port": link_port})
            except Exception as e:
                print(f"[indi] not saved to the config: {e}")

        def work():
            try:
                if own:
                    if not (mount_drv or focus_drv):
                        raise RuntimeError("pick a mount (and / or focuser) driver first")
                    self.server = indi.Server([mount_drv, focus_drv], port=port).start()
                client = indi.Client(host, port).connect()
                self.client = client
                m = mt.Mount(client, timeout=15.0)
                used = m.set_link(link, link_port)
                if used:
                    print(f"mount: {m.device} on {used}")
                m.connect()
                site = self.site()
                if site:
                    m.set_site(*site)
                m.set_time()
                self.mount = m
                self.error = ""
            except Exception as e:
                self.error = str(e)
                self.disconnect_indi()
            self.busy = ""

        self.busy = "connecting..."
        threading.Thread(target=work, daemon=True).start()

    def disconnect_indi(self):
        self.job = None
        if self.mount is not None and self.client is not None and self.client.connected:
            try:
                self.mount.stop_jog()
            except OSError:
                pass
        self.jogging = set()
        if self.client is not None:
            self.client.close()
        if self.server is not None:
            self.server.stop()
        self.client = self.server = self.mount = None

    def site(self):
        """(lat, lon, alt_m) from the GPS fix, else the config's [observer] site; None."""
        meta = station_header.site_meta(self.main.c.gps, localconfig.section("observer"))
        if meta.get("latitude") is None or meta.get("longitude") is None:
            return None
        return meta["latitude"], meta["longitude"], meta.get("altitude") or 0.0

    # ---- target ----
    def target(self):
        try:
            return mt.parse_radec(self.target_edit.text())
        except ValueError as e:
            self.error = str(e)
            return None

    def set_target(self, ra, dec):
        self.target_edit.setText(f"{mt.hms(ra).replace('h', ':').replace('m', ':').rstrip('s')} "
                                 f"{mt.dms(dec).replace('°', ':').replace('′', ':').rstrip('″')}")

    def on_from_target(self):
        if self.main.target_radec:
            self.set_target(*self.main.target_radec)
        else:
            self.error = "no target looked up yet (Recording panel: Target, then Find)"

    def on_from_solve(self):
        sol = self.main.solution
        if sol:
            self.set_target(sol["center"]["ra"], sol["center"]["dec"])
        else:
            self.error = "no plate solution yet (Stack & plate solve, key s)"

    # ---- motion ----
    def on_goto(self, track=True):
        t = self.target()
        if self.mount is not None and t:
            self.mount.goto(*t, track=track)
            self.error = ""

    def on_sync(self):
        t = self.target()
        if self.mount is not None and t:
            threading.Thread(target=self.mount.sync, args=t, daemon=True).start()

    def on_abort(self):
        self.job = None
        if self.mount is not None:
            self.jogging.clear()
            self.mount.stop_jog()
            self.mount.abort()

    def confirm(self, title, text):
        """A guard against a stray click: Yes / No, No the default."""
        box = QtWidgets.QMessageBox.question(self, title, text,
                                             QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                             QtWidgets.QMessageBox.No)
        return box == QtWidgets.QMessageBox.Yes

    TINTS = {True: "#c8e6c9", False: "#ffcdd2"}      # tracking: light green, not: light red

    def set_tint(self, tracking):
        """The jog area's background: green tracking, red not tracking, plain not connected."""
        self.tracking_shown = tracking
        color = self.TINTS.get(tracking)
        self.jog_frame.setStyleSheet(
            f"QFrame#jog {{ background: {color}; border-radius: 6px; }}" if color else
            "QFrame#jog { border: 1px solid palette(mid); border-radius: 6px; }")
        self.jog_frame.setToolTip({True: "tracking", False: "not tracking"}.get(tracking, "not connected"))

    def on_home(self):
        self.job = None
        if self.mount is not None and self.confirm(
                "Home", f"Slew {self.mount.device} to its home position?\n(any recording keeps running)"):
            try:
                self.mount.home()
                self.error = ""
            except RuntimeError as e:
                self.error = str(e)

    def on_jog(self, direction, on):
        if self.mount is None:
            return
        (self.jogging.add if on else self.jogging.discard)(direction)
        self.mount.jog(direction, on)

    def on_rate(self, index):
        element = self.rate_combo.itemData(index)
        if self.mount is not None and element:
            self.mount.set_rate(element)
            self.main.settings.setValue(f"mount/{self.mount.device}/jog_rate", element)

    def fill_rates(self):
        """The driver's slew rates into the speed list, once per connection (the last one used
        selected again)."""
        rates, current = self.mount.rates()
        if [self.rate_combo.itemData(i) for i in range(self.rate_combo.count())] == [k for k, _ in rates]:
            return
        self.rate_combo.clear()
        for element, label in rates:
            self.rate_combo.addItem(label, element)
        saved = self.main.settings.value(f"mount/{self.mount.device}/jog_rate", None)
        if saved and self.rate_combo.findData(saved) >= 0 and saved != current:
            self.mount.set_rate(saved)
            current = saved
        self.rate_combo.setCurrentIndex(max(0, self.rate_combo.findData(current)))

    def on_park(self):
        if self.mount is None:
            return
        if self.mount.parked():
            self.mount.park(False)                 # unpark: no harm in it
        elif self.confirm("Park", f"Park {self.mount.device}? It slews to its park position and stops "
                                  "tracking; gotos are refused until Unpark."):
            self.job = None
            self.mount.park(True)

    # ---- plate-solve loop ----
    def on_solve_sync(self):
        if self.mount is not None:
            self.job = {"kind": "sync", "phase": "stack", "count": self.main.solve_count}
            self.main.on_stack_button()

    def on_center(self):
        t = self.target()
        if self.mount is None or not t:
            return
        self.job = {"kind": "center", "phase": "slew", "target": t, "round": 1}
        self.mount.goto(*t, track=True)

    def step_job(self):
        """Advance Solve & sync / Center by one step (called by poll)."""
        job, main, m = self.job, self.main, self.mount
        if job is None or m is None:
            return
        st = main.c.stacker
        if job["phase"] == "slew":
            if not m.slewing():
                job.update(phase="stack", count=main.solve_count, started=time.monotonic())
                main.on_stack_button()
        elif job["phase"] == "stack":
            if st.state == "done" and st.path and main.solve_thread is None and main.solved_path != st.path:
                main.start_solve(st.path)
                job["phase"] = "solve"
            elif st.error:
                self.error, self.job = f"stack failed: {st.error}", None
        elif job["phase"] == "solve":
            if main.solve_thread is None:
                if main.solve_count == job["count"] or main.solution is None:
                    self.error, self.job = main.solve_log or "plate solve failed", None
                    return
                c = main.solution["center"]
                threading.Thread(target=m.sync, args=(c["ra"], c["dec"]), daemon=True).start()
                if job["kind"] == "sync":
                    self.job = None
                    self.error = f"synced to {mt.hms(c['ra'])} {mt.dms(c['dec'])}"
                    return
                off = mt.separation(c["ra"], c["dec"], *job["target"])
                if off <= CENTER_TOLERANCE_DEG:
                    self.job = None
                    self.error = f"centred: {off * 60:.1f}′ off after {job['round']} round(s)"
                elif job["round"] >= CENTER_ROUNDS:
                    self.job = None
                    self.error = f"still {off * 60:.1f}′ off after {job['round']} rounds"
                else:
                    job["round"] += 1
                    job["phase"] = "sync-wait"
                    job["off"] = off
                    job["until"] = time.monotonic() + 2.0
        elif job["phase"] == "sync-wait" and time.monotonic() > job["until"]:
            job["phase"] = "slew"
            m.goto(*job["target"], track=True)

    # ---- status ----
    def poll(self):
        connected = self.mount is not None and self.client is not None and self.client.connected
        if self.client is not None and not self.client.connected and not self.busy:
            self.error = self.error or f"INDI connection lost ({self.client.error or 'server gone'})"
            self.disconnect_indi()
            connected = False
        self.connect_button.setText("Disconnect" if self.client is not None else "Connect")
        self.connect_button.setEnabled(not self.busy)
        for w in (self.mount_combo, self.focus_combo, self.server_check, self.host_edit,
                  self.link_combo, self.port_edit):
            w.setEnabled(self.client is None and not self.busy)
        for w in (self.goto_button, self.goto_still, self.center_button, self.sync_button,
                  self.solve_sync, self.track_check, self.park_button, self.abort_button,
                  self.home_button, self.rate_combo, *self.jog_buttons.values()):
            w.setEnabled(connected)
        try:
            self.step_job()
        except Exception as e:
            self.error, self.job = f"{e}", None
        if connected:
            self.fill_rates()
            for d in list(self.jogging):          # a release lost (focus, window change): stop
                if not self.jog_buttons[d].isDown():
                    self.on_jog(d, False)
            st = self.mount.status()
            what = motion(st)
            if self.job:
                what += f" · {self.job['kind']}: {self.job['phase']}" + (
                    f" (round {self.job['round']})" if "round" in self.job else "")
            self.state_label.setText(f"{st['device']}: {what}")
            text = ""
            if st["ra"] is not None:
                text = f"RA {mt.hms(st['ra'])}  Dec {mt.dms(st['dec'])}  (J2000)"
                site = self.site()
                if site:
                    alt, az = mt.altaz(st["ra"], st["dec"], site[0], site[1])
                    text += f"\nalt {alt:.1f}°  az {az:.1f}°"
            self.pos_label.setText(text)
            if self.tracking_shown is not bool(st["tracking"]):
                self.set_tint(bool(st["tracking"]))
            self.track_check.blockSignals(True)
            self.track_check.setChecked(st["tracking"])
            self.track_check.blockSignals(False)
            self.park_button.setText("Unpark" if st["parked"] else "Park")
            self.home_button.setEnabled(st["can_home"])
            if not st["can_home"]:
                self.home_button.setToolTip("this mount driver offers no home position")
            msg = st["message"]
        else:
            self.state_label.setText(self.busy or "not connected")
            if self.tracking_shown is not None:
                self.set_tint(None)
            self.pos_label.setText("")
            msg = ""
        self.msg_label.setText("\n".join(t for t in (self.error, msg) if t))
        label = getattr(self.main, "mount_status", None)
        if label is not None:
            label.setText(self.summary() if connected else "")

    def summary(self):
        """One line for the camera window's status bar ('' when not connected)."""
        if self.mount is None:
            return ""
        st = self.mount.status()
        return f"{st['device']}: {motion(st)}"

    def shutdown(self):
        self.timer.stop()
        self.disconnect_indi()
