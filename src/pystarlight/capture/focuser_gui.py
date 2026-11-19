"""The camera's Focuser window: a window of its own (menu INDI Telescope Controls, Shift+F; closed
when done, place and size remembered) for the focuser on the Mount panel's INDI connection
(telescope_gui.MountDock.focuser, capture/focuser.py) and the focus helper (capture/focus.py).

NEW CODE. Focuser: position (and temperature), steps in / out (the step picked, or ten of them;
remembered per focuser), go to, abort, a focus mark (per focuser). Focus helper: a star (the
brightest unsaturated one near the centre, or one clicked on the image) measured on every frame
it gets: its HFD against the focuser position, each position's frames once the focuser has
stood still for SETTLE_S, the V-curve fitted and its best position offered (Go to best).
Closing the window stops the measuring.
"""
import time

import numpy as np

from . import focus as fm
from . import focuser as fc
from .qt import QtCore, QtGui, QtWidgets

FOCUS_STEPS = (1, 10, 100, 1000, 10000)
SETTLE_S = 1.0             # after a move: frames this much later count for the new position
MIN_FRAMES = 3             # frames a position needs to be on the curve


class CurvePlot(QtWidgets.QWidget):
    """HFD (px) over the focuser position: each position's median with its spread, the fitted
    curve and its best position, the focuser's position now."""

    COLORS = {False: {"bg": "#111", "text": "#888", "grid": "#444", "now": "#00b8d4", "fit": "#ffb300",
                      "point": "#cfcfcf"},
              True: {"bg": "#0a0000", "text": "#8a0000", "grid": "#3a0000", "now": "#6a0000",
                     "fit": "#d00000", "point": "#ff3030"}}     # night mode: dim reds only

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(180)
        self.points, self.fit, self.now, self.live = [], None, None, None
        self.colors = self.COLORS[False]

    def set_night(self, on):
        self.colors = self.COLORS[bool(on)]
        self.update()

    def set_data(self, points, fit, now, live):
        self.points, self.fit, self.now, self.live = points, fit, now, live
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        c = {k: QtGui.QColor(v) for k, v in self.colors.items()}
        p.fillRect(self.rect(), c["bg"])
        w, h = self.width(), self.height()
        left, right, top, bottom = 40, 8, 8, 20
        grey = c["text"]
        p.setPen(grey)
        if self.live is not None:
            p.drawText(QtCore.QRectF(left, top, w - left - right, 16), QtCore.Qt.AlignRight,
                       f"HFD now {self.live:.2f} px")
        if not self.points:
            p.drawText(self.rect(), QtCore.Qt.AlignCenter,
                       "no curve yet: measure a star, then step the focuser")
            p.end()
            return
        pos = np.array([q[0] for q in self.points], dtype=float)
        xs = list(pos) + ([self.now] if self.now is not None else [])
        x_lo, x_hi = min(xs), max(xs)
        pad = max((x_hi - x_lo) * 0.08, 1.0)
        x_lo, x_hi = x_lo - pad, x_hi + pad
        y_hi = max(q[1] + q[2] for q in self.points) * 1.15 or 1.0

        def pt(x, y):
            return QtCore.QPointF(left + (x - x_lo) / (x_hi - x_lo) * (w - left - right),
                                  top + (1 - y / y_hi) * (h - top - bottom))

        p.setPen(QtGui.QPen(c["grid"], 1, QtCore.Qt.DotLine))
        step = 10 ** np.floor(np.log10(y_hi / 2)) if y_hi > 0 else 1
        for i in range(1, int(y_hi / step) + 1):
            a = pt(x_lo, i * step)
            p.drawLine(a, pt(x_hi, i * step))
            p.setPen(grey)
            p.drawText(QtCore.QRectF(0, a.y() - 8, left - 4, 16), QtCore.Qt.AlignRight, f"{i * step:g}")
            p.setPen(QtGui.QPen(c["grid"], 1, QtCore.Qt.DotLine))
        p.setPen(grey)
        p.drawText(QtCore.QRectF(left, h - bottom + 2, w - left - right, 16), QtCore.Qt.AlignLeft,
                   f"{x_lo + pad:.0f}")
        p.drawText(QtCore.QRectF(left, h - bottom + 2, w - left - right, 16), QtCore.Qt.AlignRight,
                   f"{x_hi - pad:.0f}")
        if self.now is not None:                              # where the focuser is
            p.setPen(QtGui.QPen(c["now"], 1))
            p.drawLine(pt(self.now, 0), pt(self.now, y_hi))
        if self.fit is not None:
            best, low, curve = self.fit
            xx = np.linspace(pos.min(), pos.max(), 80)
            yy = curve(xx)
            p.setPen(QtGui.QPen(c["fit"], 1.5))
            p.drawPolyline(QtGui.QPolygonF([pt(a, b) for a, b in zip(xx, yy) if b <= y_hi]))
            p.setPen(QtGui.QPen(c["fit"], 1, QtCore.Qt.DashLine))
            p.drawLine(pt(best, 0), pt(best, y_hi))
        p.setPen(QtGui.QPen(c["point"], 1))
        p.setBrush(c["point"])
        for x, y, spread, _ in self.points:
            if spread > 0:
                p.drawLine(pt(x, max(y - spread, 0)), pt(x, y + spread))
            p.drawEllipse(pt(x, y), 3.0, 3.0)
        p.end()


class FocuserWindow(QtWidgets.QWidget):
    def __init__(self, main):
        super().__init__(main, QtCore.Qt.Window | QtCore.Qt.Tool)
        self.setWindowTitle("Focuser")
        self.setObjectName("focuser_window")
        self.main = main
        self.meter = getattr(main.c, "focus", None)
        self.action = None
        self.error = ""
        self.device_shown = None
        self.curve = {}                  # position -> [HFD of each settled frame]
        self.read = 0                    # the meter's samples taken so far
        self.last_pos = None
        self.settled_at = 0.0

        g = QtWidgets.QGridLayout(self)
        g.setContentsMargins(8, 8, 8, 8)
        g.setSpacing(4)
        r = 0
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.setToolTip("the Mount panel's INDI connection (focuser driver: menu INDI "
                                       "Telescope Controls, INDI settings)")
        self.connect_button.clicked.connect(lambda: self.main.mount_dock.on_connect())
        self.state_label = QtWidgets.QLabel("not connected")
        self.state_label.setWordWrap(True)
        g.addWidget(self.connect_button, r, 0)
        g.addWidget(self.state_label, r, 1, 1, 4)
        r += 1
        self.pos_label = QtWidgets.QLabel("")
        self.pos_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        self.pos_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.info_label = QtWidgets.QLabel("")
        g.addWidget(self.pos_label, r, 0, 1, 2)
        g.addWidget(self.info_label, r, 2, 1, 3)
        r += 1

        self.step_combo = QtWidgets.QComboBox()
        self.step_combo.setEditable(True)
        self.step_combo.setValidator(QtGui.QIntValidator(1, 1000000, self))
        for s in FOCUS_STEPS:
            self.step_combo.addItem(str(s))
        self.step_combo.setToolTip("steps per click (type any number); the double arrows move ten "
                                   "times as far. A timer focuser: milliseconds")
        self.step_combo.setCurrentText("100")
        self.step_combo.currentTextChanged.connect(self.on_step_changed)
        g.addWidget(QtWidgets.QLabel("Step"), r, 0)
        g.addWidget(self.step_combo, r, 1)
        self.goto_spin = QtWidgets.QSpinBox()
        self.goto_spin.setRange(0, 10000000)
        self.goto_spin.setToolTip("an absolute position (steps); Enter or Go to goes there")
        self.goto_typed = False          # a number typed in: kept until used (not overwritten)
        self.goto_spin.valueChanged.connect(self.on_goto_typed)
        self.goto_spin.lineEdit().returnPressed.connect(self.on_goto_button)
        self.goto_button = QtWidgets.QPushButton("Go to")
        self.goto_button.clicked.connect(self.on_goto_button)
        g.addWidget(self.goto_spin, r, 2, 1, 2)
        g.addWidget(self.goto_button, r, 4)
        r += 1
        self.move_buttons = []
        for col, (text, sign, factor, tip) in enumerate((
                ("◀◀", -1, 10, "in by ten steps"), ("◀ in", -1, 1, "in by one step"),
                ("out ▶", 1, 1, "out by one step"), ("▶▶", 1, 10, "out by ten steps"))):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, s=sign * factor: self.on_move(s))
            g.addWidget(b, r, col)
            self.move_buttons.append(b)
        self.abort_button = QtWidgets.QPushButton("Abort")
        self.abort_button.setStyleSheet("font-weight: bold;")
        self.abort_button.clicked.connect(lambda: self.run(fc.Focuser.abort))
        g.addWidget(self.abort_button, r, 4)
        r += 1
        self.mark_button = QtWidgets.QPushButton("Mark focus")
        self.mark_button.setToolTip("remember this position as the focus (per focuser)")
        self.mark_button.clicked.connect(self.on_mark)
        self.back_button = QtWidgets.QPushButton("Back to mark")
        self.back_button.clicked.connect(lambda: self.on_goto(self.mark()))
        self.mark_label = QtWidgets.QLabel("")
        g.addWidget(self.mark_button, r, 0, 1, 2)
        g.addWidget(self.back_button, r, 2, 1, 2)
        g.addWidget(self.mark_label, r, 4)
        r += 1

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        g.addWidget(line, r, 0, 1, 5)
        r += 1
        self.center_button = QtWidgets.QPushButton("Centre star")
        self.center_button.setToolTip("measure the brightest unsaturated star near the image centre")
        self.center_button.clicked.connect(lambda: self.meter and self.meter.ask("center"))
        self.pick_button = QtWidgets.QPushButton("Pick star")
        self.pick_button.setToolTip("then click a star on the image")
        self.pick_button.clicked.connect(self.on_pick)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_measuring)
        g.addWidget(self.center_button, r, 0, 1, 2)
        g.addWidget(self.pick_button, r, 2, 1, 2)
        g.addWidget(self.stop_button, r, 4)
        r += 1
        self.star_label = QtWidgets.QLabel("")
        self.star_label.setWordWrap(True)
        g.addWidget(self.star_label, r, 0, 1, 5)
        r += 1
        self.plot = CurvePlot()
        g.addWidget(self.plot, r, 0, 1, 5)
        g.setRowStretch(r, 1)
        r += 1
        self.clear_button = QtWidgets.QPushButton("Clear curve")
        self.clear_button.clicked.connect(self.clear_curve)
        self.best_button = QtWidgets.QPushButton("Go to best")
        self.best_button.setToolTip("the fitted curve's lowest point (needs positions on both sides)")
        self.best_button.clicked.connect(self.on_best)
        self.best_label = QtWidgets.QLabel("")
        g.addWidget(self.clear_button, r, 0, 1, 2)
        g.addWidget(self.best_button, r, 2, 1, 2)
        g.addWidget(self.best_label, r, 4)
        r += 1
        self.msg_label = QtWidgets.QLabel("")
        self.msg_label.setWordWrap(True)
        self.msg_label.setStyleSheet("color: gray;")
        g.addWidget(self.msg_label, r, 0, 1, 5)
        self.fit = None

        geom = main.settings.value("focuser/geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        else:
            self.resize(430, 560)
        self.timer = QtCore.QTimer(self, timeout=self.poll, interval=500)
        self.timer.start()
        self.poll()

    def set_night(self, on):
        """Night mode (the camera's View menu): the plot in dim reds too."""
        self.plot.set_night(on)

    # ---- the menu's check mark follows the window ----
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
        self.main.settings.setValue("focuser/geometry", self.saveGeometry())
        self.stop_measuring()                       # closed: done focusing
        self._sync_action(False)
        super().closeEvent(e)

    # ---- focuser ----
    def focuser(self):
        dock = self.main.mount_dock
        f = dock.focuser
        return f if f is not None and dock.client is not None and dock.client.connected else None

    def key(self, name):
        return f"focuser/{self.device_shown}/{name}"

    def step(self):
        try:
            return max(1, int(self.step_combo.currentText()))
        except ValueError:
            return 1

    def on_step_changed(self, text):
        if self.device_shown and text.isdigit():
            self.main.settings.setValue(self.key("step"), text)

    def run(self, what, *args):
        f = self.focuser()
        if f is None:
            return None
        try:
            out = what(f, *args)
            self.error = ""
            return out
        except RuntimeError as e:
            self.error = str(e)
            return None

    def on_move(self, factor):
        self.run(fc.Focuser.move_by, factor * self.step())

    def on_goto(self, position):
        self.goto_typed = False                     # a move: the box follows the focuser again
        if position is not None:
            self.run(fc.Focuser.move_to, position)

    def on_goto_typed(self, value):
        if not getattr(self, "showing_position", False):
            self.goto_typed = True

    def on_goto_button(self):
        self.on_goto(self.goto_spin.value())

    def show_position(self, pos):
        """The focuser's position into the Go to box (not over a number typed in and not used)."""
        if self.goto_typed or self.goto_spin.value() == int(pos):
            return
        self.showing_position = True
        self.goto_spin.setValue(int(pos))
        self.showing_position = False

    def mark(self):
        v = self.main.settings.value(self.key("mark"), None) if self.device_shown else None
        try:
            return int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            return None

    def on_mark(self):
        f = self.focuser()
        pos = f.position() if f is not None else None
        if pos is not None:
            self.main.settings.setValue(self.key("mark"), int(pos))

    # ---- focus star and curve ----
    def on_pick(self):
        if self.meter is None:
            return
        view = self.main.view
        view.pick_callback = lambda x, y: self.meter.ask("at", x, y)
        view.setCursor(QtCore.Qt.CrossCursor)
        self.error = "click a star on the image"

    def stop_measuring(self):
        if self.meter is not None and self.meter.state != "idle":
            self.meter.ask("stop")
        view = getattr(self.main, "view", None)
        if view is not None:
            view.pick_callback = None
            if view.focus_boxes:
                view.focus_boxes = []
                view.update()

    def clear_curve(self):
        self.curve, self.fit = {}, None

    def on_best(self):
        if self.fit is not None:
            self.on_goto(round(self.fit[0]))

    def curve_points(self):
        """[(position, median HFD, spread, frames)] of the positions with enough frames."""
        out = []
        for pos in sorted(self.curve):
            v = np.array(self.curve[pos])
            if len(v) >= MIN_FRAMES:
                q1, q3 = np.percentile(v, [25, 75])
                out.append((pos, float(np.median(v)), float(q3 - q1) / 2, len(v)))
        return out

    def take_samples(self, st):
        """The meter's new frames onto the curve, at the focuser's position: only frames taken
        SETTLE_S after it last moved."""
        m = self.meter
        if m is None:
            return None
        if len(m.samples) < self.read:                 # a new star: the meter started afresh
            self.read = 0
            self.clear_curve()
        new, self.read = m.samples[self.read:], len(m.samples)
        if st is None:
            return None
        pos = st["position"] if st["kind"] == "absolute" else (
            st["moved"] if st["kind"] == "relative" else None)
        now = time.monotonic()
        if st["moving"] or pos != self.last_pos:
            self.settled_at = now + SETTLE_S
            self.last_pos = pos
        if pos is not None and not st["moving"]:
            hfds = [s[1] for s in new if s[0] >= self.settled_at]
            if hfds:
                self.curve.setdefault(pos, []).extend(hfds)
        return pos

    def show_star(self):
        m, view = self.meter, self.main.view
        if m is None:
            self.star_label.setText("focus helper: needs the camera (no frames here)")
            return
        box = m.box()
        boxes = []
        stack = getattr(self.main, "stack_show", None)
        if box is not None and not (stack is not None and stack.isChecked()):
            color = "#ffd740" if m.state == "measuring" else "#ff5252"
            boxes = [(*box, QtGui.QColor(color))]
        if boxes != view.focus_boxes:
            view.focus_boxes = boxes
            view.update()
        text = m.note
        if m.state == "measuring" and m.samples:
            recent = np.array([s[1:] for s in m.samples[-10:]])
            hfd, fwhm, peak = np.nanmedian(recent, axis=0)
            full = getattr(getattr(self.main.c, "decode", None), "max_value", None)
            sat = "  SATURATED: pick a fainter star" if full and peak >= 0.9 * full else ""
            text = (f"star at ({m.star[0]:.0f}, {m.star[1]:.0f}): HFD {hfd:.2f} px, FWHM {fwhm:.2f} px, "
                    f"peak {peak:.0f}{sat}")
        elif m.state == "idle" and not text:
            text = "focus star: Centre star, or Pick star and click one"
        self.star_label.setText(text)

    def poll(self):
        dock = self.main.mount_dock
        f = self.focuser()
        self.connect_button.setText("Disconnect" if dock.client is not None else "Connect")
        self.connect_button.setEnabled(not dock.busy)
        st = f.status() if f is not None else None
        if st is not None and st["device"] != self.device_shown:        # a focuser connected
            self.device_shown = st["device"]
            step = self.main.settings.value(self.key("step"), None)
            if step:
                self.step_combo.setCurrentText(str(step))
            if st["position"] is not None:
                self.show_position(st["position"])
        absolute = st is not None and st["kind"] == "absolute"
        if absolute and st["position"] is not None and not st["moving"]:
            self.show_position(st["position"])                     # follows, unless a number is typed in
        for b in self.move_buttons + [self.abort_button]:
            b.setEnabled(st is not None)
        for b in (self.goto_button, self.goto_spin, self.mark_button):
            b.setEnabled(absolute)
        mark = self.mark() if absolute else None
        self.back_button.setEnabled(mark is not None)
        self.mark_label.setText(f"{mark}" if mark is not None else "")
        if st is None:
            self.state_label.setText(dock.busy or ("connected, no focuser" if dock.client is not None
                                                   else "not connected"))
            self.pos_label.setText("")
            self.info_label.setText("")
            msg = dock.focus_error if dock.client is not None else ""
        else:
            what = "moving" if st["moving"] else "stopped (alert)" if st["alert"] else "idle"
            self.state_label.setText(f"{st['device']}: {what}")
            if absolute and st["position"] is not None:
                self.pos_label.setText(f"{int(st['position'])}")
                info = f"of {int(st['max'])}" if st["max"] else ""
            elif st["kind"] == "relative":
                self.pos_label.setText(f"{st['moved']:+d}")
                info = "steps since connect"
            else:
                self.pos_label.setText("")
                info = "timer focuser: step = ms"
            if st["temperature"] is not None:
                info += f"   {st['temperature']:.1f} °C"
            self.info_label.setText(info.strip())
            msg = st["message"]
            hint = fc.lost_hint(msg) if st["alert"] or "error" in msg.lower() else ""
            if hint:
                msg = f"{msg}\n{hint}"
        top = int(st["max"]) if st and st["max"] else 10000000
        if self.goto_spin.maximum() != top:
            self.showing_position = True
            self.goto_spin.setMaximum(top)
            self.showing_position = False

        has_meter = self.meter is not None
        for b in (self.center_button, self.pick_button, self.stop_button):
            b.setEnabled(has_meter)
        pos = self.take_samples(st)
        if self.isVisible():
            self.show_star()
            points = self.curve_points()
            self.fit = fm.fit_curve([(q[0], q[1]) for q in points])
            self.best_button.setEnabled(self.fit is not None and absolute)
            self.best_label.setText(f"{self.fit[0]:.0f}" if self.fit else "")
            if self.fit is not None:
                self.best_button.setToolTip(f"best {self.fit[0]:.0f}: HFD {self.fit[1]:.2f} px (fit)")
            live = None
            if has_meter and self.meter.state == "measuring" and self.meter.samples:
                live = float(np.median([s[1] for s in self.meter.samples[-5:]]))
            self.plot.set_data(points, self.fit, pos, live)
        self.msg_label.setText(self.error or msg)

    def shutdown(self):
        self.timer.stop()
        self.stop_measuring()
