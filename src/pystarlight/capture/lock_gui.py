"""The camera window's Star lock dock (guider.py): lock on 2-3 stars, guide the mount through
the Mount dock's INDI connection, and the target's flux live.

NEW CODE. The Lock check box switches the lock on and off by hand, independent of recording:
on, it chooses the stars on the next frame (never the target: its marker, from the star field /
plate solve, if shown); no lock star found, or the camera region changed: it unticks itself.
Guide the mount: calibrates first (pulses north and west, ~20 s), then corrects; only while the
lock is certain, never during a slew or a Center / Solve & sync job. One CSV row per window goes
next to the recordings (<name>_<UTC>Z_lock.csv), from the moment the stars are locked.
The plot: target / lock stars (or the lock stars' transparency without a target) per frame,
divided by its median, grey points and a 1 s running median; a quick look, not the analysis.
"""
import os
from datetime import datetime, timezone

import numpy as np

from . import guider as gd
from .qt import QtCore, QtGui, QtWidgets

STATE_COLORS = {gd.LOCKED: "#c8e6c9", gd.CALIBRATING: "#bbdefb", gd.HOLD: "#ffe0b2",
                gd.LOST: "#ffcdd2", gd.IDLE: ""}
BOX_COLORS = {gd.LOCKED: "#00e676", gd.CALIBRATING: "#40c4ff", gd.HOLD: "#ffab40", gd.LOST: "#ff5252"}
WINDOWS_S = (60, 120, 300, 900)


class FluxPlot(QtWidgets.QWidget):
    """Points (t, value) of the last `window` seconds: value / median, a 1 s running median."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(140)
        self.window = 120
        self.data = np.empty((0, 2))
        self.label = ""

    def set_data(self, data, label):
        self.data, self.label = data, label
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#111"))
        w, h = self.width(), self.height()
        left, right, top, bottom = 38, 6, 6, 18
        d = self.data
        p.setPen(QtGui.QColor("#888"))
        if len(d) < 2:
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "no flux yet (Lock)")
            p.end()
            return
        t_end = d[-1, 0]
        d = d[d[:, 0] >= t_end - self.window]
        v = d[:, 1] / (np.median(d[:, 1]) or 1.0)
        lo, hi = np.percentile(v, [0.5, 99.5])
        lo, hi = min(lo, 0.0) if lo < 0.5 else min(lo, 0.9), max(hi, 1.1)
        sx = (w - left - right) / self.window
        sy = (h - top - bottom) / (hi - lo)

        def pt(t, y):
            return QtCore.QPointF(left + (t - (t_end - self.window)) * sx, top + (hi - y) * sy)

        for level in (0.0, 0.5, 1.0):                   # guide lines and their labels
            if lo <= level <= hi:
                p.setPen(QtGui.QPen(QtGui.QColor("#444"), 1, QtCore.Qt.DotLine))
                a = pt(t_end - self.window, level)
                p.drawLine(a, pt(t_end, level))
                p.setPen(QtGui.QColor("#aaa"))
                p.drawText(QtCore.QPointF(4, a.y() + 4), f"{level:.1f}")
        p.setPen(QtGui.QPen(QtGui.QColor("#9e9e9e"), 2))
        p.drawPoints(QtGui.QPolygonF([pt(t, y) for t, y in zip(d[:, 0], v)]))
        if len(v) >= 5:                                  # 1 s running median
            fps = max(1.0, (len(d) - 1) / max(d[-1, 0] - d[0, 0], 1e-3))
            k = max(3, int(fps) | 1)
            if len(v) > k:
                from numpy.lib.stride_tricks import sliding_window_view
                med = np.median(sliding_window_view(v, k), axis=1)
                pen = QtGui.QPen(QtGui.QColor("#ff9100"), 2)
                p.setPen(pen)
                ts = d[k // 2:k // 2 + len(med), 0]
                p.drawPolyline(QtGui.QPolygonF([pt(t, y) for t, y in zip(ts, med)]))
        p.setPen(QtGui.QColor("#aaa"))
        end = datetime.fromtimestamp(t_end, timezone.utc).strftime("%H:%M:%S UTC")
        p.drawText(QtCore.QRectF(left, h - bottom, w - left - right, bottom),
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, end)
        p.drawText(QtCore.QRectF(left, h - bottom, w - left - right, bottom),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, f"-{self.window} s   {self.label}")
        p.end()


class LockDock(QtWidgets.QDockWidget):
    def __init__(self, main):
        super().__init__("Star lock", main)
        self.setObjectName("dock_lock")
        self.main = main
        self.thread = main.c.locker
        self.lock = self.thread.lock
        self.lock.pulse = self.send_pulse
        self.region = None              # the camera region the lock was made on
        self.csv_path = None
        self.logging = False            # a lock was asked for: its log opens once locked

        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        self.lock_check = QtWidgets.QCheckBox("Lock")
        self.lock_check.setToolTip("on: choose 2-3 lock stars on the next frame (never the target) and "
                                   "hold them; off: release. Independent of recording")
        self.lock_check.toggled.connect(self.set_lock)
        self.cal_button = QtWidgets.QPushButton("Calibrate")
        self.cal_button.setToolTip("guide pulses north and west, the response measured (~20 s); "
                                   "needed again after a meridian flip")
        self.cal_button.clicked.connect(lambda: self.thread.ask("calibrate"))
        self.guide_check = QtWidgets.QCheckBox("Guide the mount")
        self.guide_check.setToolTip("corrections through the Mount panel's INDI connection, only while the "
                                    "lock is certain; calibrates first if needed")
        self.guide_check.toggled.connect(self.on_guide)
        self.state_label = QtWidgets.QLabel("idle")
        self.state_label.setMargin(4)
        self.info_label = QtWidgets.QLabel()
        self.note_label = QtWidgets.QLabel()
        self.note_label.setWordWrap(True)
        self.window_combo = QtWidgets.QComboBox()
        for s in WINDOWS_S:
            self.window_combo.addItem(f"{s // 60} min" if s >= 60 else f"{s} s", s)
        self.window_combo.setCurrentIndex(WINDOWS_S.index(120))
        self.window_combo.currentIndexChanged.connect(
            lambda i: setattr(self.plot, "window", self.window_combo.itemData(i)))
        self.plot = FluxPlot()
        g.addWidget(self.lock_check, 0, 0)
        g.addWidget(self.cal_button, 0, 1)
        g.addWidget(self.guide_check, 1, 0, 1, 2)
        g.addWidget(self.state_label, 3, 0, 1, 2)
        g.addWidget(self.info_label, 4, 0, 1, 2)
        g.addWidget(self.note_label, 5, 0, 1, 2)
        g.addWidget(self.plot, 6, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("plot"), 7, 0)
        g.addWidget(self.window_combo, 7, 1)
        g.setRowStretch(6, 1)
        self.setWidget(w)
        self.timer = QtCore.QTimer(self, timeout=self.poll, interval=250)
        self.timer.start()

    # ---- mount ----
    def mount(self):
        """The Mount panel's mount when it can take a pulse now (connected, not slewing, no job)."""
        dock = getattr(self.main, "mount_dock", None)
        m = getattr(dock, "mount", None)
        if m is None or dock.client is None or not dock.client.connected or dock.job is not None:
            return None
        return m

    def send_pulse(self, direction, ms):
        """The lock's pulse callback (lock thread). Nothing when the mount cannot take one now."""
        m = self.mount()
        if m is None:
            return
        try:
            if not m.slewing():
                m.pulse(direction, ms)
        except Exception as e:
            self.lock.event(f"guide pulse failed: {e}")

    def can_guide(self):
        m = self.mount()
        try:
            return m is not None and m.can_guide()
        except Exception:
            return False

    # ---- actions ----
    def target_xy(self):
        """The target marker in the live frame's pixels (as the camera window places it), or None."""
        main = self.main
        if not main.marks or not main.marks_check.isChecked():
            return None
        rx, ry = main.c.region[:2]
        for m in main.marks:
            if m["kind"] == "target":
                return m["x"] - rx + main.marks_shift[0], m["y"] - ry + main.marks_shift[1]
        return None

    def set_lock(self, on):
        """The Lock check box: on locks (on the next frame), off releases."""
        if on:
            if self.lock.state == gd.IDLE:
                self.lock.full_scale = self.main.c.decode.max_value
                self.region = tuple(self.main.c.region)
                self.logging = True
                self.thread.ask("lock", self.target_xy())
        else:
            self.release()

    def release(self, note="released"):
        self.show_lock(False)
        self.logging = False
        self.guide_check.setChecked(False)
        self.thread.ask("release", note)            # the lock thread also closes the log

    def show_lock(self, on):
        """Tick or untick Lock without acting on it."""
        self.lock_check.blockSignals(True)
        self.lock_check.setChecked(on)
        self.lock_check.blockSignals(False)

    def on_guide(self, on):
        """Guiding on: calibrates first when needed (the lock corrects only once calibrated)."""
        if on and not self.can_guide():
            self.guide_check.setChecked(False)
            self.lock.note = "guiding needs the Mount panel connected to a mount that takes guide pulses"
            return
        self.lock.guide = on
        if on and self.lock.cal is None and self.lock.state in (gd.LOCKED, gd.HOLD):
            self.thread.ask("calibrate")

    def open_csv(self):
        st = self.main.c.stacker
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        try:
            os.makedirs(st.out_dir, exist_ok=True)
            self.csv_path = os.path.join(st.out_dir, f"{st.name}_{stamp}Z_lock.csv")
            f = open(self.csv_path, "w")
            f.write("# PyStarLight star lock: one row per window; error = field shift from the lock (px); "
                    "pulses in ms (+ north / west, - south / east)\n"
                    "unix_time,state,error_x,error_y,valid_frames,frames,pulse_ns_ms,pulse_we_ms\n")
            self.lock.csv = f
        except OSError as e:
            self.lock.note = f"no lock log: {e}"

    # ---- status ----
    def poll(self):
        lock, main = self.lock, self.main
        if lock.state != gd.IDLE and self.region is not None and tuple(main.c.region) != self.region:
            self.release("released: the camera region changed")
        if self.lock_check.isChecked() and lock.state == gd.IDLE and self.thread.request is None:
            self.show_lock(False)                       # no lock star found: unticked
            self.logging = False
        if self.logging and lock.state != gd.IDLE and lock.csv is None:
            self.open_csv()                             # the log starts once locked
        if self.guide_check.isChecked() and (not lock.guide or not self.can_guide()):
            self.guide_check.setChecked(False)          # stopped by the lock, or the mount gone
        locked = lock.state != gd.IDLE
        self.cal_button.setEnabled(locked and lock.state != gd.CALIBRATING and self.can_guide())
        text = lock.state + (f": {lock.message}" if lock.message else "")
        if lock.guide and lock.state == gd.LOCKED:
            text += "  · guiding"
        self.state_label.setText(text)
        color = STATE_COLORS.get(lock.state, "")
        self.state_label.setStyleSheet(f"background: {color}; border-radius: 4px;" if color else "")
        info = []
        if locked:
            info.append(f"stars {lock.stars_ok}/{len(lock.ref)}, frames {lock.frames_valid}/{lock.frames}")
            rms = lock.rms()
            if rms is not None:
                info.append(f"error rms {rms:.2f} px (1 min)")
            info.append(f"pulses {lock.pulses}" + ("" if lock.cal is not None else ", not calibrated"))
        if self.thread.error:
            info.append(self.thread.error)
        self.info_label.setText("\n".join(info))
        self.note_label.setText(lock.note)
        view = main.view
        boxes = []
        if locked and not main.stack_show.isChecked():
            for x0, y0, x1, y1, kind in lock.boxes():
                color = "#ff4081" if kind == "target" else BOX_COLORS.get(lock.state, "#00e676")
                boxes.append((x0, y0, x1, y1, QtGui.QColor(color)))
        if boxes != view.lock_boxes:
            view.lock_boxes = boxes
            view.update()
        if self.isVisible() and lock.history:
            label = "target / lock stars" if lock.target is not None else "lock stars (transparency)"
            self.plot.set_data(np.array(lock.history), label)

    def summary(self):
        """One line for the status bar ('' when idle)."""
        if self.lock.state == gd.IDLE:
            return ""
        return f"lock: {self.lock.state}" + (" · guiding" if self.lock.guide else "")

    def shutdown(self):
        self.timer.stop()
        self.thread.ask("release")
