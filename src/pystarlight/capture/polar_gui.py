"""The camera's Polar alignment window (menu INDI Telescope Controls, Shift+P; a window of its
own, place and size remembered): measures where the mount's RA axis points from plate solves,
and guides the Alt / Az knobs with an arrow on the live image (capture/polar.py has the method).

NEW CODE. Measure: (optionally a goto near Polaris / sigma Oct first) stack + plate-solve, sync
the mount, turn the RA axis by the step (a goto at the mount's own Dec: only the RA axis moves;
direction auto / west / east within the allowed range: the path above the minimum altitude, not
across the meridian; auto turns back the other way on the next Measure, so repeated measurements
go +30, -30, ... and do not run into the mount's limits), solve again (and once more for three
points). Adjust: a star near the centre (or one clicked) is followed on the live frames; its
position gives the knob turns made so far; the window shows what is left (Alt raise / lower, Az
east / west) and the image an arrow from the star along the Az leg, then the Alt leg, to where
it has to go. Refresh: a new solve at the same position re-bases the model (checks the turns).
Uses the Mount panel's INDI connection and the camera window's stack and plate solve. Config
[polar_align].
"""
import math
import threading
import time
from datetime import datetime, timezone

from . import localconfig
from . import mount as mt
from . import polar as pa
from .qt import QtCore, QtGui, QtWidgets

DEFAULTS = {"start": "here", "points": 2, "step_deg": 30.0, "direction": "auto",
            "min_alt_deg": 20.0, "cross_meridian": False, "refraction": True, "good_arcmin": 2.0}
SETTLE_S = 2.0              # after a slew or a sync: the mount at rest before the stack
STEP_TIMEOUT_S = 600        # a slew, stack or solve that takes longer: given up
WEAK_PX_PER_ARCMIN = 2.0    # a knob that moves the star less than this: a warning
COLORS = {"star": "#69f0ae", "az": "#ffd54f", "alt": "#4dd0e1", "tip": "#69f0ae",
          "pole": "#ff80ab", "axis": "#ffffff", "lost": "#ff5252"}


def jd_now():
    return mt.julian_day(datetime.now(timezone.utc))


def jd_of_stack(sol):
    """UTC jd of the middle of the stack (DATE-OBS / DATE-END, first and last frame's start),
    now if the header has neither."""
    times = []
    try:
        from astropy.io import fits
        h = fits.getheader(sol["stack"])
        for key in ("DATE-OBS", "DATE-END"):
            if h.get(key):
                t = datetime.fromisoformat(str(h[key]).replace("Z", ""))
                times.append(mt.julian_day(t.replace(tzinfo=timezone.utc)))
    except Exception:
        pass
    return sum(times) / len(times) if times else jd_now()


def wcs_functions(sol):
    """(pix2sky, sky2pix) of the solution's WCS (astropy), pixels 0-based as the frames."""
    from astropy.io import fits
    from astropy.wcs import WCS
    w = WCS(fits.getheader(sol["wcs"]))

    def pix2sky(x, y):
        (ra, dec), = w.all_pix2world([[x, y]], 0)
        return float(ra), float(dec)

    def sky2pix(ra, dec):
        (x, y), = w.all_world2pix([[ra, dec]], 0)
        return float(x), float(y)
    return pix2sky, sky2pix


def amount(deg):
    m = abs(deg) * 60.0
    return f"{m / 60:.2f}°" if m >= 100 else f"{m:.1f}′"


class PolarWindow(QtWidgets.QWidget):
    def __init__(self, main):
        super().__init__(main, QtCore.Qt.Window | QtCore.Qt.Tool)
        self.setWindowTitle("Polar alignment")
        self.setObjectName("polar_window")
        self.main = main
        self.follower = getattr(main.c, "polar", None)
        self.cfg = {**DEFAULTS, **localconfig.section("polar_align")}
        self.action = None
        self.job = None              # the measuring / refresh steps, polled
        self.last_sign = None        # the last Measure's RA turn: the next one turns back (auto)
        self.points = []             # pa.Point of this measurement
        self.alignment = None        # pa.Alignment once measured
        self.info = {}               # what went into the axis
        self.error = ""
        self.need_pick = False       # the follower has a new star: it becomes the model's

        g = QtWidgets.QGridLayout(self)
        g.setContentsMargins(8, 8, 8, 8)
        g.setSpacing(4)
        r = 0
        self.state_label = QtWidgets.QLabel("")
        self.state_label.setWordWrap(True)
        g.addWidget(self.state_label, r, 0, 1, 3)
        r += 1

        form = QtWidgets.QFormLayout()
        self.start_combo = QtWidgets.QComboBox()
        self.start_combo.addItem("this field", "here")
        self.start_combo.addItem("near Polaris (σ Oct south)", "polaris")
        self.start_combo.setCurrentIndex(max(0, self.start_combo.findData(self.cfg["start"])))
        self.start_combo.setToolTip("this field: any field with stars, the mount where it is; near Polaris: "
                                    "a goto there first (the pole and the RA axis then show on the image)")
        self.points_combo = QtWidgets.QComboBox()
        self.points_combo.addItem("2 (one RA turn)", 2)
        self.points_combo.addItem("3 (two RA turns, checked)", 3)
        self.points_combo.setCurrentIndex(max(0, self.points_combo.findData(int(self.cfg["points"]))))
        self.points_combo.setToolTip("2: the axis from two fields and the angle the mount reports it turned; "
                                     "3: the plane through three fields (no angle needed), the two-point "
                                     "answer shown as a check")
        self.step_spin = QtWidgets.QDoubleSpinBox(minimum=5.0, maximum=90.0, decimals=0, suffix=" °")
        self.step_spin.setValue(float(self.cfg["step_deg"]))
        self.step_spin.setToolTip("how far the RA axis turns per step (larger: more accurate; 20-40° is "
                                  "usual)")
        self.dir_combo = QtWidgets.QComboBox()
        for text, data in (("auto (stays highest)", "auto"), ("west", "west"), ("east", "east")):
            self.dir_combo.addItem(text, data)
        self.dir_combo.setCurrentIndex(max(0, self.dir_combo.findData(self.cfg["direction"])))
        self.alt_spin = QtWidgets.QDoubleSpinBox(minimum=0.0, maximum=80.0, decimals=0, suffix=" °")
        self.alt_spin.setValue(float(self.cfg["min_alt_deg"]))
        self.alt_spin.setToolTip("the fields must stay above this altitude on the whole way (your horizon)")
        self.meridian_check = QtWidgets.QCheckBox("may cross the meridian")
        self.meridian_check.setChecked(bool(self.cfg["cross_meridian"]))
        self.meridian_check.setToolTip("off: the turns stay on the mount's side of the meridian (a German "
                                       "mount would flip there, and a flip moves the Dec axis)")
        form.addRow("Start", self.start_combo)
        form.addRow("Points", self.points_combo)
        form.addRow("RA step", self.step_spin)
        form.addRow("Direction", self.dir_combo)
        form.addRow("Min. altitude", self.alt_spin)
        form.addRow("", self.meridian_check)
        g.addLayout(form, r, 0, 1, 3)
        r += 1

        self.start_button = QtWidgets.QPushButton("Measure")
        self.start_button.setStyleSheet("font-weight: bold;")
        self.start_button.setToolTip("plate-solve, sync, turn the RA axis, solve again: the axis' error")
        self.start_button.clicked.connect(self.on_start)
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.setToolTip("a new plate solve here (knobs at rest): checks and re-bases the "
                                       "turns made")
        self.refresh_button.clicked.connect(self.on_refresh)
        self.pick_button = QtWidgets.QPushButton("Pick star")
        self.pick_button.setToolTip("follow the star clicked next on the image (default: the brightest "
                                    "unsaturated one near the centre)")
        self.pick_button.clicked.connect(self.on_pick)
        g.addWidget(self.start_button, r, 0)
        g.addWidget(self.refresh_button, r, 1)
        g.addWidget(self.pick_button, r, 2)
        r += 1

        big = "font-size: 16pt; font-weight: bold;"
        self.alt_label = QtWidgets.QLabel("")
        self.az_label = QtWidgets.QLabel("")
        for w in (self.alt_label, self.az_label):
            w.setStyleSheet(big)
            w.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.total_label = QtWidgets.QLabel("")
        self.total_label.setWordWrap(True)
        g.addWidget(self.alt_label, r, 0, 1, 3)
        r += 1
        g.addWidget(self.az_label, r, 0, 1, 3)
        r += 1
        g.addWidget(self.total_label, r, 0, 1, 3)
        r += 1
        self.detail_label = QtWidgets.QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: gray;")
        g.addWidget(self.detail_label, r, 0, 1, 3)
        r += 1
        self.msg_label = QtWidgets.QLabel("")
        self.msg_label.setWordWrap(True)
        g.addWidget(self.msg_label, r, 0, 1, 3)
        g.setRowStretch(r + 1, 1)

        geo = main.settings.value("polar/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(400, 520)
        self.timer = QtCore.QTimer(self, timeout=self.poll, interval=250)
        self.timer.start()
        self.poll()

    # ---- the menu's check mark follows the window (as the Focuser window) ----
    def bind_action(self, action):
        self.action = action
        action.setCheckable(True)
        action.toggled.connect(lambda on: self.show() if on else self.close())

    def _sync_action(self, on):
        if self.action is not None and self.action.isChecked() != on:
            self.action.blockSignals(True)
            self.action.setChecked(on)
            self.action.blockSignals(False)

    def showEvent(self, e):
        self._sync_action(True)
        super().showEvent(e)

    def closeEvent(self, e):
        self.main.settings.setValue("polar/geometry", self.saveGeometry())
        self.stop()                                   # closed: done aligning
        self._sync_action(False)
        super().closeEvent(e)

    def set_night(self, on):
        pass                                          # the app's style sheet does it all

    # ---- the mount, the site ----
    def mount(self):
        dock = self.main.mount_dock
        if dock.mount is None or dock.client is None or not dock.client.connected:
            return None
        return dock.mount

    def site(self):
        s = self.main.mount_dock.site()
        return pa.Site(s[0], s[1]) if s else None

    def mount_jnow(self):
        pos = self.mount().position()
        return mt.j2000_to_jnow(*pos) if pos else None

    # ---- measuring ----
    def settings_now(self):
        return {"start": self.start_combo.currentData(), "points": self.points_combo.currentData(),
                "step_deg": self.step_spin.value(), "direction": self.dir_combo.currentData(),
                "min_alt_deg": self.alt_spin.value(), "cross_meridian": self.meridian_check.isChecked()}

    def on_start(self):
        if self.job is not None:
            self.stop()
            self.error = "stopped"
            return
        m, site = self.mount(), self.site()
        why = ("connect the mount first (Mount panel, Shift+M)" if m is None else
               "no site: a GPS fix or the config's [observer] position" if site is None else
               "the Mount panel is busy (Solve & sync / Center)" if self.main.mount_dock.job else
               "switch the star lock off first (it moves the mount)" if self.lock_on() else "")
        if why:
            self.error = why
            return
        s = self.settings_now()
        self.cfg.update(s)
        if localconfig.config_path() is not None:     # only into a config in use, never a new one
            try:
                localconfig.update_section("polar_align", s)
            except Exception as e:
                print(f"[polar_align] not saved to the config: {e}")
        self.stop()
        self.error = ""
        self.points, self.info = [], {}
        m.set_tracking(True)
        self.job = {"kind": "measure", "n": s["points"], "sign": None}
        if s["start"] == "polaris":
            m.goto(*(pa.POLARIS if site.north else pa.SIGMA_OCT), track=True)
            self.set_phase("slew")
        else:
            self.set_phase("settle")

    def lock_on(self):
        dock = getattr(self.main, "lock_dock", None)
        return dock is not None and dock.lock.state != "idle"

    def on_refresh(self):
        if self.alignment is None or self.job is not None:
            return
        self.error = ""
        self.job = {"kind": "refresh"}
        self.set_phase("settle")

    def on_pick(self):
        if self.follower is None or self.alignment is None:
            return
        self.main.view.pick_callback = lambda x, y: self.follow("at", x, y)
        self.main.view.setCursor(QtCore.Qt.CrossCursor)
        self.error = "click the star to follow on the image"

    def follow(self, *request):
        self.follower.ask(*request)
        self.need_pick = True
        self.error = ""

    def stop(self):
        if self.job is not None:                      # a measure / refresh running: its stack and solve too
            main = self.main
            if main.solve_thread is not None and main.solve_cancel is not None:
                main.solve_cancel.set()               # ends solve-field (poor sky: no endless solve)
            if self.job.get("phase") == "stack" and main.c.stacker.state == "stacking":
                main.c.stacker.cancel()
        self.job = None
        self.alignment = None
        if self.follower is not None and self.follower.state != "idle":
            self.follower.ask("stop")
        view = getattr(self.main, "view", None)
        if getattr(view, "polar", None) is not None:
            view.polar = None
            view.update()
        for w in (self.alt_label, self.az_label, self.total_label):
            w.setText("")

    def set_phase(self, phase):
        self.job["phase"], self.job["since"] = phase, time.monotonic()
        if phase == "stack":
            self.job["count"] = self.main.solve_count
            if self.main.c.stacker.state == "stacking":   # a stack of the field before: anew
                self.main.c.stacker.cancel()
            self.main.on_stack_button()

    def fail(self, why):
        self.error, self.job = why, None

    def step_job(self):
        """One step of measuring / refreshing (called by poll)."""
        job, main = self.job, self.main
        if job is None:
            return
        m = self.mount()
        if m is None:
            self.fail("the mount is gone (INDI connection)")
            return
        st = main.c.stacker
        age = time.monotonic() - job["since"]
        limit = STEP_TIMEOUT_S
        if job["phase"] == "stack":                  # long exposures: 16 x 40 s is more than that
            frames = getattr(main, "stack_frames", None)
            if frames is not None:
                limit += 1.5 * frames.value() * main.c.controller.exposure_us / 1e6
        if age > limit and job["phase"] in ("slew", "stack", "solve"):
            self.fail(f"gave up waiting for the {job['phase']}")
            return
        if job["phase"] == "slew":
            if age > SETTLE_S and not m.slewing():
                self.set_phase("settle")
        elif job["phase"] == "settle":
            if age > SETTLE_S:
                self.set_phase("stack")
        elif job["phase"] == "stack":
            if st.state == "done" and st.path:
                if main.solve_thread is None and main.solved_path != st.path:
                    main.start_solve(st.path)
                self.set_phase("solve")
            elif st.error:
                self.fail(f"stack failed: {st.error}")
            elif st.state == "idle" and age > 1.0:
                self.fail("the stack was cancelled")
        elif job["phase"] == "solve":
            if main.solve_thread is None:
                if main.solve_count == job["count"] or main.solution is None:
                    self.fail(main.solve_log or "plate solve failed")
                    return
                if job["kind"] == "measure":
                    self.add_point(main.solution, m)
                else:
                    self.rebase(main.solution)
        elif job["phase"] == "sync-wait":
            if age > SETTLE_S:
                self.points[0].mount_ra, self.points[0].mount_dec = self.mount_jnow()  # synced reading
                self.move(m)

    def add_point(self, sol, m):
        c = sol["center"]
        mra, mdec = self.mount_jnow()
        self.points.append(pa.Point(c["ra"], c["dec"], jd_of_stack(sol), mra, mdec))
        self.job["solution"] = sol
        n = len(self.points)
        print(f"polar: point {n}: RA {mt.hms(c['ra'])} Dec {mt.dms(c['dec'])}")
        if n == 1:                                   # sync: the mount's gotos are right from here
            threading.Thread(target=m.sync, args=(c["ra"], c["dec"]), daemon=True).start()
            self.set_phase("sync-wait")
        elif n < self.job["n"]:
            self.move(m)
        else:
            self.compute(sol)

    def move(self, m):
        """Turn the RA axis by one step (in the direction planned at the first move)."""
        site = self.site()
        ra, dec = self.mount_jnow()
        if abs(dec - self.points[0].mount_dec) > 0.05:
            self.fail(f"the mount's Dec changed by {dec - self.points[0].mount_dec:+.2f}° between the "
                      "points: Measure again (no other moves meanwhile)")
            return
        if self.job["sign"] is None:
            moves = self.job["n"] - 1
            try:
                sign, _, low = pa.plan_moves(site, ra, dec, jd_now(), self.cfg["step_deg"], moves,
                                             self.cfg["direction"], self.cfg["min_alt_deg"],
                                             self.cfg["cross_meridian"],
                                             prefer=-self.last_sign if self.last_sign else None)
            except ValueError as e:
                self.fail(str(e))
                return
            self.job["sign"] = self.last_sign = sign
            print(f"polar: turning {'west' if sign > 0 else 'east'} by {self.cfg['step_deg']:g}° "
                  f"x {moves} (lowest {low:.0f}°)")
        target = ((ra - self.job["sign"] * self.cfg["step_deg"]) % 360.0, dec)
        m.goto(*mt.jnow_to_j2000(*target), track=True)
        self.set_phase("slew")

    def compute(self, sol):
        site = self.site()
        try:
            axis, info = pa.measure_axis(site, self.points, refraction=bool(self.cfg["refraction"]))
            pix2sky, sky2pix = wcs_functions(sol)
        except Exception as e:
            self.fail(f"no axis: {e}")
            return
        self.info = info
        self.alignment = pa.Alignment(site, axis, self.points[-1].jd, pix2sky, sky2pix)
        self.job = None
        err = pa.axis_error(site, axis)
        print("polar: axis error " + "; ".join(pa.describe(err)) + f"; total {amount(err['total'])}")
        self.start_adjusting()

    def rebase(self, sol):
        al = self.alignment
        try:
            pix2sky, sky2pix = wcs_functions(sol)
            centre = ((sol["width"] - 1) / 2.0, (sol["height"] - 1) / 2.0)
            turned = al.rebase(pix2sky, sky2pix, jd_of_stack(sol), centre, pix2sky(*centre))
        except Exception as e:
            self.fail(f"refresh: {e}")
            return
        self.job = None
        self.info["refreshed"] = turned
        print(f"polar: refreshed, knobs turned alt {turned[0] * 60:+.1f}′ az {turned[1] * 60:+.1f}′")
        self.start_adjusting()

    def start_adjusting(self):
        self.main.continue_live()                    # the live frames, not the stack
        if self.follower is not None:
            self.follow("center")

    # ---- adjusting: the followed star, the arrow ----
    def update_model(self):
        """The follower's star into the model; the arrow onto the image. Returns the remaining
        turns and the error now, or None."""
        al, f = self.alignment, self.follower
        if al is None:
            return None
        view = self.main.view
        jd = jd_now()
        star = f.star if f is not None and f.state == "measuring" else None
        lost = f is not None and f.state == "lost"
        try:
            if star is not None:
                if self.need_pick or al.star is None:
                    al.pick(*star, jd)
                    self.need_pick = False
                else:
                    al.knobs_from_star(*star, jd)
        except Exception as e:
            self.error = f"model: {e}"
        overlay = {"lost": lost, "star": star, "r": pa.STAR_BOX}
        path = al.arrow(jd) if al.star is not None else None
        (d_alt, d_az), now = al.remaining()
        if path is not None:
            overlay["path"] = path
            overlay["labels"] = (f"Az {amount(d_az)} {'E' if d_az > 0 else 'W'}",
                                 f"Alt {amount(d_alt)} {'up' if d_alt > 0 else 'down'}")
        try:
            pole, axis = al.pole_and_axis_pixels(jd)
            w, h = view.img_size or (0, 0)
            inside = (lambda p: -0.25 * w < p[0] < 1.25 * w and -0.25 * h < p[1] < 1.25 * h)
            if inside(pole) or inside(axis):
                overlay["pole"], overlay["axis"] = pole, axis
        except Exception:
            pass
        if getattr(view, "polar", None) != overlay:
            view.polar = overlay
            view.update()
        return (d_alt, d_az), now, (al.sensitivity(jd) if al.star is not None else None)

    # ---- status ----
    def poll(self):
        m, site = self.mount(), self.site()
        try:
            self.step_job()
        except Exception as e:
            self.fail(f"{e}")
        busy = self.job is not None
        self.start_button.setText("Stop" if busy else ("Measure again" if self.alignment else "Measure"))
        self.start_button.setEnabled(m is not None or busy)
        self.refresh_button.setEnabled(self.alignment is not None and not busy and m is not None)
        self.pick_button.setEnabled(self.alignment is not None and self.follower is not None)
        for w in (self.start_combo, self.points_combo, self.step_spin, self.dir_combo, self.alt_spin,
                  self.meridian_check):
            w.setEnabled(not busy)
        if m is None:
            state = "Mount not connected (Mount panel, Shift+M)."
        elif site is None:
            state = "No site: a GPS fix or the config's [observer] position."
        else:
            state = (f"{m.device}, site {site.lat:+.3f}° {site.lon:+.3f}°, "
                     f"{'north' if site.north else 'south'} pole")
        if busy:
            j = self.job
            what = {"slew": "slewing", "settle": "settling", "stack": "stacking", "solve": "plate solving",
                    "sync-wait": "syncing"}.get(j["phase"], j["phase"])
            state += (f"\nPoint {len(self.points) + 1} of {j['n']}: {what}…" if j["kind"] == "measure"
                      else f"\nRefresh: {what}…")
        self.state_label.setText(state)
        res = self.update_model()
        if res is not None:
            self.show_result(*res)
        elif not busy:
            self.alt_label.setText("")
            self.az_label.setText("")
            self.total_label.setText("")
            self.detail_label.setText("Measure: plate solve, sync, RA turn, plate solve. Then turn the "
                                      "Alt / Az knobs: the star follows the arrow to its tip.")
        f = self.follower
        note = f.note if (f is not None and self.alignment is not None) else ""
        self.msg_label.setText("\n".join(t for t in (self.error, note) if t))

    def show_result(self, remaining, now, sens):
        d_alt, d_az = remaining
        self.alt_label.setText(f"Alt: {'raise' if d_alt > 0 else 'lower'} {amount(d_alt)}")
        self.az_label.setText(f"Az: move {'east' if d_az > 0 else 'west'} {amount(d_az)}")
        good = now["total"] * 60 <= float(self.cfg["good_arcmin"])
        self.total_label.setText(
            f"Error left {amount(now['total'])}" + ("  ✓ aligned" if good else "")
            + f"   (axis alt {now['axis_alt']:.2f}°, az {(now['axis_az'] + 180) % 360 - 180:+.2f}°)")
        self.total_label.setStyleSheet("color: #2e7d32; font-weight: bold;" if good else "")
        info, lines = self.info, []
        if info:
            steps = info.get("ha_steps_deg", [])
            lines.append(f"{info['points']} points, RA turned "
                         + ", ".join(f"{abs(s):.1f}° {'west' if s > 0 else 'east'}" for s in steps)
                         + f"; fields {info['radius_deg']:.1f}° from the axis")
            if "two_point_diff_arcmin" in info:
                lines.append(f"two-point check: {info['two_point_diff_arcmin']:.1f}′ apart")
            if "refreshed" in info:
                a, b = info["refreshed"]
                lines.append(f"refreshed: the knobs had turned alt {a * 60:+.1f}′, az {b * 60:+.1f}′")
        if sens is not None:
            lines.append(f"star moves {sens[0]:.0f} px per ′ of Alt, {sens[1]:.0f} px per ′ of Az")
            if min(sens) < WEAK_PX_PER_ARCMIN:
                weak = "Alt" if sens[0] < sens[1] else "Az"
                lines.append(f"⚠ the {weak} knob barely moves this field: use Refresh to check, or "
                             "measure in a field higher up, nearer the meridian or the pole")
        if self.alignment is not None and self.alignment.star is None:
            lines.append("waiting for a star to follow (Pick star)")
        else:
            lines.append("Turn the knobs (mount tracking): the star walks the yellow Az leg, then the "
                         "blue Alt leg, to the green ring. Refresh (knobs at rest) checks.")
        self.detail_label.setText("\n".join(lines))

    def shutdown(self):
        self.timer.stop()
        self.stop()


def paint_overlay(view, p, o):
    """The Polar alignment window's marks on the image (ImageView.paintEvent): the followed star,
    the arrow (Az leg, Alt leg, the ring to reach), and the pole and RA axis when near."""
    def pen(color, width=2, style=QtCore.Qt.SolidLine):
        q = QtGui.QPen(QtGui.QColor(color), width, style)
        q.setCosmetic(True)
        return q

    def pt(xy):
        return QtCore.QPointF(*view.to_screen(xy[0] + 0.5, xy[1] + 0.5))

    def label(at, text, color):
        br = p.fontMetrics().boundingRect(text).adjusted(-3, -1, 3, 1)
        br.moveBottomLeft(QtCore.QPoint(int(at.x() + 8), int(at.y() - 6)))
        p.fillRect(br, QtGui.QColor(0, 0, 0, 160))
        p.setPen(pen(color))
        p.drawText(br, QtCore.Qt.AlignCenter, text)

    def arrow(a, b, color, text):
        p.setPen(pen(color, 2))
        p.drawLine(a, b)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if math.hypot(dx, dy) > 12:
            ang = math.atan2(dy, dx)
            for side in (+1, -1):
                h = ang + math.pi + side * 0.45
                p.drawLine(b, QtCore.QPointF(b.x() + 11 * math.cos(h), b.y() + 11 * math.sin(h)))
        label(QtCore.QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2), text, color)

    if o.get("pole") is not None:
        a, b = pt(o["axis"]), pt(o["pole"])
        p.setPen(pen(COLORS["axis"], 1, QtCore.Qt.DashLine))
        p.drawLine(a, b)
        for at, text, color in ((a, "RA axis", COLORS["axis"]), (b, "pole", COLORS["pole"])):
            p.setPen(pen(color))
            p.drawLine(QtCore.QPointF(at.x() - 8, at.y()), QtCore.QPointF(at.x() + 8, at.y()))
            p.drawLine(QtCore.QPointF(at.x(), at.y() - 8), QtCore.QPointF(at.x(), at.y() + 8))
            label(at, text, color)
    path = o.get("path")
    if path:
        now, mid, tip = (pt(q) for q in path)
        az_text, alt_text = o["labels"]
        arrow(now, mid, COLORS["az"], az_text)
        arrow(mid, tip, COLORS["alt"], alt_text)
        p.setPen(pen(COLORS["tip"], 2))
        p.drawEllipse(tip, 10.0, 10.0)
        p.drawEllipse(tip, 3.0, 3.0)
    star = o.get("star")
    if star is not None or o.get("lost"):
        at = pt(star) if star is not None else (pt(path[0]) if path else None)
        if at is not None:
            color = COLORS["lost"] if o.get("lost") else COLORS["star"]
            p.setPen(pen(color, 2))
            rs = max(8.0, o["r"] * view.map[4])
            p.drawRect(QtCore.QRectF(at.x() - rs, at.y() - rs, 2 * rs, 2 * rs))
