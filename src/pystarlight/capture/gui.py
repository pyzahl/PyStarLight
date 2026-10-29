"""Qt GUI of the capture app: live image view, control panel, status.

The main thread only renders the newest frame (debayer -> crop + scale to the view -> gamma
LUT) and talks to the camera through the Controller thread; see app.py for the threads.

Keys: e/E exposure -/+, g/G gain -/+, d/D gamma -/+, t software trigger, r record on/off,
q quit.
"""
import importlib.util
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

from .cameras import VEYE_MIN_ROI
from .decode import format_bits, format_for_bits
from .qt import QShortcut, QtCore, QtGui, QtWidgets, Signal, Slot, event_pos
from .recorder import RECORD_FORMATS
from .stacker import display_stretch
from .timing import (
    GPS_RATES,
    LAG_EXTRA_NS,
    PulseStamper,
    onset_delay_us,
    parse_rate,
    parse_utc,
    pulses_per_frame,
    rate_str,
    utc_str,
)

EXP_STEPS = 1000            # exposure slider resolution (log scale)
REANCHOR_LEAD_NS = 20 * 10**9   # set start time: anchor on GPS UTC this long before
GAIN_STEPS = 10             # gain slider: 0.1 dB per step
GAMMA_MIN, GAMMA_MAX = 0.1, 4.0


def review_command(path):
    """Command line that opens `path` in the review player (pystarlight.capture.review): this
    interpreter when the full package is importable (uv run), else the repo's .venv."""
    module = ["-m", "pystarlight.capture.review", str(path)]
    if importlib.util.find_spec("pystarlight") is not None:
        return [sys.executable, *module]
    venv = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python"
    return [str(venv), *module] if venv.exists() else None


class ImageView(QtWidgets.QWidget):
    """Shows the newest frame: fit to the window; mouse wheel zooms around the cursor, left
    drag pans, double-click fits again. In select mode a left drag draws a rectangle
    instead, emitted in image pixels. Past 1:1 pixels are shown as blocks (nearest)."""

    changed = Signal()                       # view moved: render again
    selected = Signal(int, int, int, int)    # x0, y0, x1, y1 (image pixels)
    hline_moved = Signal(int)                # the dashed line was dragged to this row
    MAX_ZOOM = 64.0

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)  # clicking the image ends typing in a field
        self.zoom = 1.0             # 1: the whole image fits
        self.center = None          # image point at the view centre (None: image centre)
        self.img_size = None        # (w, h) of the frame shown
        self.qimg = None
        # mapping of the drawn image: screen = (ox, oy) + (image - (x0, y0)) * (sx, sy)
        self.map = None
        self.select_mode = False
        self.drag = None            # ("pan", start pos, start centre) / ("select", image point)
                                    # / ("hline", row): dragging the dashed line
        self.band = None            # rectangle being dragged (image pixels)
        self.marks = []             # [(x0, y0, x1, y1, QColor)] to draw (image pixels)
        self.hline = None           # (row, QColor): a dashed line across the image
        self.hspan = 0              # rows above/below it: dotted lines (0: none)
        self.circles = []           # [(x, y, radius, text, QColor)]: stars (image pixels)
        self.banner = ""            # shown at the bottom (e.g. while the stack is displayed)
        self.cursor_img = None      # image point under the mouse

    def reset_view(self):
        self.zoom, self.center = 1.0, None
        self.changed.emit()

    def _fit(self):
        w, h = self.img_size
        return min(max(1, self.width()) / w, max(1, self.height()) / h)

    def render(self, frame, lut):
        """frame: full-resolution (h, w) or (h, w, 3) raw values; lut: raw -> 8 bit."""
        H, W = frame.shape[:2]
        if self.img_size != (W, H):             # new region: start from the whole image
            self.img_size, self.zoom, self.center = (W, H), 1.0, None
        vw, vh = max(1, self.width()), max(1, self.height())
        scale = self._fit() * self.zoom
        sw, sh = vw / scale, vh / scale         # visible part, image pixels
        cx, cy = self.center or (W / 2, H / 2)
        cx = min(max(cx, sw / 2), W - sw / 2) if sw < W else W / 2
        cy = min(max(cy, sh / 2), H - sh / 2) if sh < H else H / 2
        self.center = (cx, cy) if self.zoom > 1.0 else None
        X0, Y0 = cx - sw / 2, cy - sh / 2       # view's top-left, image pixels (may be < 0)
        x0, y0 = max(0, int(np.floor(X0))), max(0, int(np.floor(Y0)))
        x1, y1 = min(W, int(np.ceil(X0 + sw)) + 1), min(H, int(np.ceil(Y0 + sh)) + 1)
        ow = max(1, round((x1 - x0) * scale))
        oh = max(1, round((y1 - y0) * scale))
        crop = frame[y0:y1, x0:x1]
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_NEAREST
        disp = np.ascontiguousarray(np.take(lut, cv2.resize(crop, (ow, oh), interpolation=interp),
                                            mode="clip"))
        fmt = QtGui.QImage.Format_Grayscale8 if disp.ndim == 2 else QtGui.QImage.Format_BGR888
        self.qimg = QtGui.QImage(disp.data, ow, oh, disp.strides[0], fmt).copy()
        sx, sy = ow / (x1 - x0), oh / (y1 - y0)
        self.map = ((x0 - X0) * scale, (y0 - Y0) * scale, x0, y0, sx, sy)
        self.update()

    def to_image(self, pos):
        ox, oy, x0, y0, sx, sy = self.map
        return x0 + (pos.x() - ox) / sx, y0 + (pos.y() - oy) / sy

    def to_screen(self, x, y):
        ox, oy, x0, y0, sx, sy = self.map
        return ox + (x - x0) * sx, oy + (y - y0) * sy

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtCore.Qt.black)
        if self.qimg is None:
            p.setPen(QtCore.Qt.gray)
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "waiting for frames...")
            p.end()
            return
        p.drawImage(QtCore.QPointF(self.map[0], self.map[1]), self.qimg)
        rects = list(self.marks)
        if self.band is not None:
            rects.append((*self.band, QtGui.QColor("#00e5ff")))
        for x0, y0, x1, y1, color in rects:
            pen = QtGui.QPen(color, 2)
            pen.setCosmetic(True)
            p.setPen(pen)
            a, b = self.to_screen(x0, y0), self.to_screen(x1, y1)
            p.drawRect(QtCore.QRectF(QtCore.QPointF(*a), QtCore.QPointF(*b)))
        if self.hline is not None:
            row, color = self.hline
            if self.drag is not None and self.drag[0] == "hline":
                row = self.drag[1]              # (the status timer resets self.hline)
            pen = QtGui.QPen(color, 1, QtCore.Qt.DashLine)
            pen.setCosmetic(True)
            p.setPen(pen)
            a, b = self.to_screen(0, row + 0.5), self.to_screen(self.img_size[0], row + 0.5)
            p.drawLine(QtCore.QPointF(*a), QtCore.QPointF(*b))
            if self.hspan:
                pen.setStyle(QtCore.Qt.DotLine)
                p.setPen(pen)
                for r in (row - self.hspan, row + self.hspan):
                    if 0 <= r < self.img_size[1]:
                        a, b = self.to_screen(0, r + 0.5), self.to_screen(self.img_size[0], r + 0.5)
                        p.drawLine(QtCore.QPointF(*a), QtCore.QPointF(*b))
        for x, y, r, text, color in self.circles:
            pen = QtGui.QPen(color, 2)
            pen.setCosmetic(True)
            p.setPen(pen)
            cx, cy = self.to_screen(x + 0.5, y + 0.5)
            rs = max(6.0, r * self.map[4])
            p.drawEllipse(QtCore.QPointF(cx, cy), rs, rs)
            if text:
                p.drawText(QtCore.QPointF(cx + rs + 3, cy - 3), text)
        if self.zoom > 1.0:
            p.setPen(QtCore.Qt.yellow)
            p.drawText(8, 18, f"zoom {self.zoom:.1f}×  (double-click: fit)")
        if self.banner:
            r = p.fontMetrics().boundingRect(self.banner).adjusted(-8, -4, 8, 4)
            r.moveBottomLeft(QtCore.QPoint(8, self.height() - 8))
            p.fillRect(r, QtGui.QColor(21, 101, 192, 200))
            p.setPen(QtCore.Qt.white)
            p.drawText(r, QtCore.Qt.AlignCenter, self.banner)
        p.end()

    def wheelEvent(self, e):
        if self.map is None:
            return
        new = min(max(self.zoom * 1.25 ** (e.angleDelta().y() / 120), 1.0), self.MAX_ZOOM)
        ix, iy = self.to_image(event_pos(e))
        s = self._fit() * new                   # keep the image point under the cursor
        self.center = (ix - (event_pos(e).x() - self.width() / 2) / s,
                       iy - (event_pos(e).y() - self.height() / 2) / s)
        self.zoom = new
        self.changed.emit()

    def _near_hline(self, pos):
        """True if the screen point is within a few pixels of the dashed line."""
        if self.hline is None:
            return False
        return abs(pos.y() - self.to_screen(0, self.hline[0] + 0.5)[1]) <= 5

    def mousePressEvent(self, e):
        if self.map is None or e.button() != QtCore.Qt.LeftButton:
            return
        if self._near_hline(event_pos(e)):
            self.drag = ("hline", self.hline[0])
        elif self.select_mode:
            self.drag = ("select", self.to_image(event_pos(e)))
        else:
            W, H = self.img_size
            self.drag = ("pan", event_pos(e), self.center or (W / 2, H / 2))

    def mouseMoveEvent(self, e):
        if self.map is None:
            return
        self.cursor_img = self.to_image(event_pos(e))
        if self.drag is None:
            near = self._near_hline(event_pos(e))
            self.setCursor(QtCore.Qt.SizeVerCursor if near else QtCore.Qt.ArrowCursor)
            return
        if self.drag[0] == "hline":
            self.drag = ("hline", int(min(max(self.cursor_img[1], 0), self.img_size[1] - 1)))
            self.update()
        elif self.drag[0] == "pan" and self.zoom > 1.0:
            s = self._fit() * self.zoom
            d = event_pos(e) - self.drag[1]
            self.center = (self.drag[2][0] - d.x() / s, self.drag[2][1] - d.y() / s)
            self.changed.emit()
        elif self.drag[0] == "select":
            (ax, ay), (bx, by) = self.drag[1], self.cursor_img
            self.band = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag is not None and self.drag[0] == "select" and self.band is not None:
            W, H = self.img_size
            x0, y0, x1, y1 = self.band
            self.selected.emit(int(max(0, x0)), int(max(0, y0)), int(min(W, x1)), int(min(H, y1)))
        if self.drag is not None and self.drag[0] == "hline":
            self.hline_moved.emit(self.drag[1])
        self.drag, self.band = None, None
        self.update()

    def leaveEvent(self, e):
        self.cursor_img = None

    def mouseDoubleClickEvent(self, e):
        self.reset_view()


class CollapsibleBox(QtWidgets.QWidget):
    """A titled panel section whose body folds away; set_summary() fills the title line
    while it is folded (e.g. 'GPS - 3D, 9 sats')."""

    toggled = Signal(bool)                   # True: open

    def __init__(self, title, opened=True):
        super().__init__()
        self.title = title
        self.summary = ""
        self.button = QtWidgets.QToolButton(checkable=True, checked=opened)
        self.button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.button.setStyleSheet("QToolButton { border: none; font-weight: bold; text-align: left; }")
        self.button.toggled.connect(self._toggled)
        self.body = QtWidgets.QFrame()
        self.body.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.grid = QtWidgets.QGridLayout(self.body)
        self.grid.setColumnStretch(1, 1)
        col = QtWidgets.QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        col.addWidget(self.button)
        col.addWidget(self.body)
        self._toggled(opened)

    def _toggled(self, opened):
        self.body.setVisible(opened)
        self.button.setArrowType(QtCore.Qt.DownArrow if opened else QtCore.Qt.RightArrow)
        self._title()
        self.toggled.emit(opened)

    def set_summary(self, text):
        if text != self.summary:
            self.summary = text
            self._title()

    def _title(self):
        folded = not self.button.isChecked() and self.summary
        text = f"{self.title}  —  {self.summary}" if folded else self.title
        self.button.setText(text.replace("&", "&&"))        # '&' would mark a shortcut


def grid_box(title):
    box = QtWidgets.QGroupBox(title)
    grid = QtWidgets.QGridLayout(box)
    grid.setColumnStretch(1, 1)
    return box, grid


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctx, args):
        super().__init__()
        self.args = args
        self.c = ctx                        # camera, controller, grabber, recorder, gps ...
        self.setWindowTitle(f"{ctx.camera.get_vendor_name()} {ctx.camera.get_model_name()}"
                            f" - {ctx.width}x{ctx.height} {ctx.pixel_format}")
        self.gamma = float(np.clip(self.args.gamma, GAMMA_MIN, GAMMA_MAX))
        self.lut = self.build_lut(self.gamma)
        self.img = None                     # newest decoded frame (full resolution)
        self.frame_id = None
        self.redraw = False
        self.disp_fps, self.disp_t0, self.disp_n = 0.0, time.monotonic(), 0
        self.gps_busy = False               # timepulse rate being set (worker thread)
        self.gps_msg = ""
        self.last_exposure = None
        self.clock_seen = False             # GPS clock anchored (poll: then set the rate)
        # record pressed in GPS mode: anchor on GPS UTC again first, then arm the recorder
        # ({"args": arm() arguments, "begin_ns": when to start anchoring, "anchoring": bool})
        self.pending_rec = None
        self.last_saved = ""
        self.stack_seen = -1                # stacker.version shown
        self.stack_img = None               # stretched stack for the view
        self.solve_thread = None            # plate solve running (worker thread)
        self.solve_cancel = None
        self.solve_log = ""                 # its newest message
        self.solution = None                # result dict of the last solve
        self.solved_path = None             # the stack it belongs to
        self.auto_solved = None             # stack already sent by "Plate-solve when done"
        self.roi_seen = 0                   # controller.roi_changes already followed
        self.roi_pending = None             # selected/typed region not applied yet
        # last requested values (keys step from these, not from a lagging camera read-back)
        self.target = {"exposure": ctx.controller.exposure_us, "gain": ctx.controller.gain_db}

        self.settings = QtCore.QSettings("PyStarLight", "camera")     # panel layout only
        self.view = ImageView()
        self.view.changed.connect(self.on_view_changed)
        self.view.selected.connect(self.on_roi_selected)
        self.view.hline_moved.connect(self.on_hline_moved)
        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.addWidget(self.make_camera_box())
        col.addWidget(self.make_roi_box())
        col.addWidget(self.make_trigger_box())
        col.addWidget(self.make_gps_box())
        col.addWidget(self.make_record_box())
        col.addWidget(self.make_stack_box())
        col.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        # the labels are still empty here, so the size hint is too small; the splitter
        # handle lets the panel be widened (its width is remembered)
        panel_w = max(panel.sizeHint().width() + 30, 480)
        scroll.setMinimumWidth(min(panel_w, 360))

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        view_w = int(ctx.width * self.args.scale)
        self.resize(view_w + panel_w + 20, max(int(ctx.height * self.args.scale) + 40, 600))
        self.splitter.setSizes([view_w, panel_w])
        sizes = self.settings.value("splitter")
        if sizes:
            self.splitter.setSizes([int(v) for v in sizes])

        for key, fn in (("e", lambda: self.step_exposure(0.8)),
                        ("Shift+E", lambda: self.step_exposure(1.25)),
                        ("g", lambda: self.gain_spin.setValue(self.target["gain"] - 0.5)),
                        ("Shift+G", lambda: self.gain_spin.setValue(self.target["gain"] + 0.5)),
                        ("d", lambda: self.gamma_spin.setValue(self.gamma - 0.1)),
                        ("Shift+D", lambda: self.gamma_spin.setValue(self.gamma + 0.1)),
                        ("r", self.on_record_button),
                        ("t", self.software_trigger),
                        ("s", self.on_stack_button),
                        ("c", self.continue_live),
                        ("Esc", self.continue_live),
                        ("q", self.close)):
            QShortcut(QtGui.QKeySequence(key), self, fn)

        self.frame_timer = QtCore.QTimer(self, timeout=self.on_frame_timer, interval=5)
        self.frame_timer.start()
        self.status_timer = QtCore.QTimer(self, timeout=self.on_status_timer, interval=250)
        self.status_timer.start()
        self.on_trigger_mode(initial=True)
        self.on_status_timer()

    # ---- panel: camera ----
    def make_camera_box(self):
        c = self.c
        box, g = grid_box("Camera")
        self.exp_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, maximum=EXP_STEPS)
        self.exp_spin = QtWidgets.QDoubleSpinBox(decimals=3, suffix=" ms", keyboardTracking=False)
        self.exp_spin.setRange(c.exp_min / 1000, c.exp_max / 1000)
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gain_slider.setRange(0, int(round((c.gain_max - c.gain_min) * GAIN_STEPS)))
        self.gain_spin = QtWidgets.QDoubleSpinBox(decimals=1, suffix=" dB", singleStep=0.5,
                                                  keyboardTracking=False)
        self.gain_spin.setRange(c.gain_min, c.gain_max)
        self.gamma_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gamma_slider.setRange(int(GAMMA_MIN * 100), int(GAMMA_MAX * 100))
        self.gamma_spin = QtWidgets.QDoubleSpinBox(decimals=2, singleStep=0.1,
                                                   keyboardTracking=False)
        self.gamma_spin.setRange(GAMMA_MIN, GAMMA_MAX)
        for r, (name, slider, spin) in enumerate((("Exposure", self.exp_slider, self.exp_spin),
                                                  ("Gain", self.gain_slider, self.gain_spin),
                                                  ("Gamma", self.gamma_slider, self.gamma_spin))):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            g.addWidget(slider, r, 1)
            g.addWidget(spin, r, 2)
        self.cam_info = QtWidgets.QLabel()
        g.addWidget(self.cam_info, 3, 0, 1, 3)
        # measured frame timing (from the frame-start times) and the exposure actually set
        self.frame_labels = {}
        for r, (key, name) in enumerate((("frame", "Frame"), ("rate", "Rate"),
                                         ("interval", "Interval"), ("exp", "Exposure")), 4):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            lab = QtWidgets.QLabel("-")
            lab.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            g.addWidget(lab, r, 1, 1, 2)
            self.frame_labels[key] = lab
        self.bits_combo = QtWidgets.QComboBox()
        self.bits_choices = [b for b in (12, 10, 8)
                             if format_for_bits(b, self.c.available, self.c.pixel_format)]
        self.bits_combo.addItems([f"{b} bit" for b in self.bits_choices])
        self.bits_combo.setToolTip("lower depth reads out faster (IMX178 full frame: "
                                   "12 bit 33.2, 10 bit 39.2, 8 bit 40.1 fps)")
        self.show_bits()
        self.bits_combo.currentIndexChanged.connect(self.on_bits)
        g.addWidget(QtWidgets.QLabel("Depth"), 8, 0)
        g.addWidget(self.bits_combo, 8, 1, 1, 2)

        self.set_exposure_widgets(self.target["exposure"])
        self.set_gain_widgets(self.target["gain"])
        self.set_gamma_widgets(self.gamma)
        self.exp_slider.valueChanged.connect(lambda p: self.set_exposure(self.slider_to_exp(p)))
        self.exp_spin.valueChanged.connect(lambda ms: self.set_exposure(ms * 1000))
        self.gain_slider.valueChanged.connect(
            lambda p: self.set_gain(c.gain_min + p / GAIN_STEPS))
        self.gain_spin.valueChanged.connect(self.set_gain)
        self.gamma_slider.valueChanged.connect(lambda p: self.set_gamma(p / 100))
        self.gamma_spin.valueChanged.connect(self.set_gamma)
        return box

    def exp_to_slider(self, us):
        c = self.c
        return int(round(EXP_STEPS * np.log(us / c.exp_min) / np.log(c.exp_max / c.exp_min)))

    def slider_to_exp(self, pos):
        c = self.c
        return float(c.exp_min * (c.exp_max / c.exp_min) ** (pos / EXP_STEPS))

    @staticmethod
    def _quiet(widget, value):
        widget.blockSignals(True)
        widget.setValue(value)
        widget.blockSignals(False)

    def set_exposure_widgets(self, us):
        self._quiet(self.exp_slider, self.exp_to_slider(np.clip(us, self.c.exp_min, self.c.exp_max)))
        self._quiet(self.exp_spin, us / 1000)

    def set_gain_widgets(self, db):
        self._quiet(self.gain_slider, int(round((db - self.c.gain_min) * GAIN_STEPS)))
        self._quiet(self.gain_spin, db)

    def set_gamma_widgets(self, gamma):
        self._quiet(self.gamma_slider, int(round(gamma * 100)))
        self._quiet(self.gamma_spin, gamma)

    def set_exposure(self, us):
        us = float(np.clip(us, self.c.exp_min, self.c.exp_max))
        self.target["exposure"] = us
        self.set_exposure_widgets(us)
        self.c.controller.request("exposure", us)

    def step_exposure(self, factor):
        self.set_exposure(self.target["exposure"] * factor)

    def set_gain(self, db):
        db = float(np.clip(db, self.c.gain_min, self.c.gain_max))
        self.target["gain"] = db
        self.set_gain_widgets(db)
        self.c.controller.request("gain", db)

    def build_lut(self, g):
        x = np.arange(self.c.decode.max_value + 1, dtype=np.float32) / self.c.decode.max_value
        return np.round(255.0 * x ** (1.0 / g)).astype(np.uint8)

    def set_gamma(self, gamma):
        self.gamma = float(np.clip(gamma, GAMMA_MIN, GAMMA_MAX))
        self.set_gamma_widgets(self.gamma)
        self.lut = self.build_lut(self.gamma)
        self.redraw = True

    def show_bits(self):
        bits = format_bits(self.c.pixel_format)
        if bits in self.bits_choices:
            self.bits_combo.blockSignals(True)
            self.bits_combo.setCurrentIndex(self.bits_choices.index(bits))
            self.bits_combo.blockSignals(False)

    def on_bits(self, index):
        c = self.c
        fmt = format_for_bits(self.bits_choices[index], c.available, c.pixel_format)
        if fmt and fmt != c.pixel_format:
            self.roi_info.setText(f"switching to {fmt}...")
            c.controller.request("reconfigure", {"pixel_format": fmt})

    # ---- panel: sensor region ----
    def make_roi_box(self):
        c = self.c
        sw, sh = c.sensor
        box, g = grid_box("Sensor region (ROI)")
        g.setColumnStretch(3, 1)                # x/w and y/h boxes equally wide
        self.roi_spins = []
        # VEYE: x and width in steps of 8, y and height in steps of 4
        mw, mh = self.min_roi = VEYE_MIN_ROI if c.is_veye else (64, 64)
        for i, (name, top, step, lo) in enumerate((("x", sw - mw, 8, 0), ("y", sh - mh, 4, 0),
                                                   ("w", sw, 8, mw), ("h", sh, 4, mh))):
            spin = QtWidgets.QSpinBox(minimum=lo, maximum=top, singleStep=step,
                                      keyboardTracking=False)
            spin.valueChanged.connect(lambda _: self.on_roi_typed())
            g.addWidget(QtWidgets.QLabel(name), i // 2, 2 * (i % 2))
            g.addWidget(spin, i // 2, 2 * (i % 2) + 1)
            self.roi_spins.append(spin)
        self.roi_select = QtWidgets.QPushButton("Select", checkable=True)
        self.roi_select.setToolTip("drag a rectangle on the image")
        self.roi_select.toggled.connect(self.on_roi_select_mode)
        self.roi_apply = QtWidgets.QPushButton("Apply")
        self.roi_apply.clicked.connect(self.apply_roi)
        self.roi_full = QtWidgets.QPushButton("Full sensor")
        self.roi_full.clicked.connect(lambda: self.apply_roi((0, 0, sw, sh)))
        buttons = QtWidgets.QHBoxLayout()
        for w in (self.roi_select, self.roi_apply, self.roi_full):
            buttons.addWidget(w)
        g.addLayout(buttons, 2, 0, 1, 4)
        self.roi_info = QtWidgets.QLabel(wordWrap=True)
        g.addWidget(self.roi_info, 3, 0, 1, 4)
        self.show_roi(c.region)
        return box

    def show_roi(self, region):
        for spin, v in zip(self.roi_spins, region):
            self._quiet(spin, int(v))

    def snap_roi(self, x, y, w, h, sensor):
        """Round to the VEYE steps (x, w: 8; y, h: 4), at least the minimum size, keep it on
        the sensor (a too small selection grows around its centre)."""
        sw, sh = sensor
        mw, mh = self.min_roi
        cx, cy = x + w / 2, y + h / 2
        w = max(mw, min(sw, int(round(w / 8)) * 8))
        h = max(mh, min(sh, int(round(h / 4)) * 4))
        x, y = cx - w / 2, cy - h / 2
        x = max(0, min(sw - w, int(x) // 8 * 8))
        y = max(0, min(sh - h, int(y) // 4 * 4))
        return x, y, w, h

    def on_roi_typed(self):
        self.roi_pending = self.snap_roi(*(s.value() for s in self.roi_spins), self.c.sensor)
        self.update_roi_marks()

    def on_roi_select_mode(self, on):
        self.view.select_mode = on
        self.view.setCursor(QtCore.Qt.CrossCursor if on else QtCore.Qt.ArrowCursor)

    def on_roi_selected(self, x0, y0, x1, y1):
        rx, ry = self.c.region[:2]              # image pixels -> sensor pixels
        if x1 - x0 < 8 or y1 - y0 < 8:
            return                              # a click, not a rectangle
        self.roi_pending = self.snap_roi(rx + x0, ry + y0, x1 - x0, y1 - y0, self.c.sensor)
        self.show_roi(self.roi_pending)
        self.roi_select.setChecked(False)
        self.update_roi_marks()

    def update_roi_marks(self):
        """Pending region (yellow) on the image, in image pixels of the current region."""
        marks = []
        if self.roi_pending is not None and tuple(self.roi_pending) != tuple(self.c.region):
            rx, ry = self.c.region[:2]
            x, y, w, h = self.roi_pending
            marks.append((x - rx, y - ry, x - rx + w, y - ry + h, QtGui.QColor("#ffd600")))
        self.view.marks = marks
        self.view.update()

    def apply_roi(self, region=None):
        region = self.snap_roi(*(region or (s.value() for s in self.roi_spins)), self.c.sensor)
        if tuple(region) == tuple(self.c.region):
            return
        self.roi_pending = region
        self.roi_info.setText(f"applying {region[2]}x{region[3]} at ({region[0]}, {region[1]})...")
        self.c.controller.request("reconfigure", {"region": region})

    def after_reconfigure(self):
        """New region and/or pixel format (or the old ones back, if they failed)."""
        c = self.c
        self.setWindowTitle(f"{c.camera.get_vendor_name()} {c.camera.get_model_name()}"
                            f" - {c.width}x{c.height} {c.pixel_format}")
        self.show_roi(c.region)
        self.show_bits()
        self.roi_pending = None
        self.update_roi_marks()
        self.lut = self.build_lut(self.gamma)   # new value range
        self.update_rows_label()
        self.ytrig_spin.setMaximum(c.height - 1)
        self.img = None                         # old size/format; wait for a new frame
        self.last_exposure = None               # new frame period -> new pulse-to-frame lag

    # ---- view ----
    def on_view_changed(self):
        self.redraw = True

    # ---- panel: trigger ----
    def make_trigger_box(self):
        box, g = grid_box("Trigger")
        base = {"hw": "external trigger", "sw": "software trigger"}.get(self.args.trigger, "free run")
        self.base_mode = self.args.trigger if self.args.trigger in ("hw", "sw") else "free"
        self.gps_check = QtWidgets.QCheckBox(f"GPS trigger  (off: {base})")
        self.gps_check.setChecked(self.args.trigger == "gps")
        # a list of rates, or type one: fps ("1.5") or the pulse period ("250 ms", "2 s")
        self.rate_combo = QtWidgets.QComboBox(editable=True)
        self.rate_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.rate_combo.addItems([f"{r} fps" for r in GPS_RATES])
        self.rate_combo.setToolTip("GPS trigger rate: pick one, or type fps (e.g. 1.5) or the "
                                   "pulse period (e.g. 250 ms, 2 s) and press Enter. Rates that "
                                   "are not whole fps become a period in whole µs.")
        text = self.args.gps_rate
        self.selected_rate = parse_rate(text)
        self.rate_combo.setEditText(text if any(ch.isalpha() for ch in text) else f"{text} fps")
        self.rate_error = ""
        self.trig_button = QtWidgets.QPushButton("Software trigger (t)")
        self.trig_button.clicked.connect(self.software_trigger)
        self.trig_info = QtWidgets.QLabel(wordWrap=True)
        self.delay_spin = QtWidgets.QDoubleSpinBox(decimals=3, maximum=10_000.0, suffix=" ms",
                                                   keyboardTracking=False)
        self.delay_spin.setValue((self.c.trigger_delay_us or 0) / 1000)
        self.delay_spin.setToolTip("trigger edge to exposure start, in camera units")
        self.ytrig_check = QtWidgets.QCheckBox("y-trigger row")
        self.ytrig_check.setToolTip("GPS trigger: set the delay so the timepulse (the GPS LED "
                                    "switching on) falls on this image row (orange line)")
        self.ytrig_check.setChecked(self.args.y_trigger is not None)
        self.ytrig_spin = QtWidgets.QSpinBox(maximum=self.c.height - 1, keyboardTracking=False)
        self.ytrig_spin.setValue(self.c.height // 2 if self.args.y_trigger is None else self.args.y_trigger)
        self.ytrig_check.setVisible(self.c.is_veye)
        self.ytrig_spin.setVisible(self.c.is_veye)
        g.addWidget(self.gps_check, 0, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("GPS frame rate"), 1, 0)
        g.addWidget(self.rate_combo, 1, 1)
        g.addWidget(QtWidgets.QLabel("Trigger delay"), 2, 0)
        g.addWidget(self.delay_spin, 2, 1)
        g.addWidget(self.ytrig_check, 3, 0)
        g.addWidget(self.ytrig_spin, 3, 1)
        g.addWidget(self.trig_button, 4, 0, 1, 2)
        g.addWidget(self.trig_info, 5, 0, 1, 2)
        self.gps_check.toggled.connect(lambda _: self.on_trigger_mode())
        self.rate_combo.activated.connect(lambda _: self.on_rate_text())
        self.rate_combo.lineEdit().editingFinished.connect(self.on_rate_text)
        self.delay_spin.valueChanged.connect(self.on_delay)
        self.ytrig_check.toggled.connect(lambda _: self.follow_y_trigger())
        self.ytrig_spin.valueChanged.connect(lambda _: self.follow_y_trigger())
        return box

    def mode(self):
        return "gps" if self.gps_check.isChecked() else self.base_mode

    def gps_rate(self):
        """The selected GPS trigger rate (Fraction, fps)."""
        return self.selected_rate

    def on_rate_text(self):
        """A rate picked or typed (Enter, or the box lost focus). Focus goes back to the image
        so the one-letter hotkeys work again instead of being typed into the box."""
        text = self.rate_combo.currentText()
        self.view.setFocus()
        try:
            rate = parse_rate(text)
        except ValueError as e:
            self.rate_error = str(e)
            self.rate_combo.setEditText(rate_str(self.selected_rate).split(" (")[0])
            return
        self.rate_error = ""
        if not any(ch.isalpha() for ch in text):
            self.rate_combo.setEditText(f"{text.strip()} fps")
        changed = rate != self.selected_rate
        self.selected_rate = rate
        if changed and self.mode() == "gps" and self.c.gps is not None:
            self.reanchor()             # every rate change: 1 Hz, anchor, then the new rate
        else:
            self.on_rate()

    def reanchor(self):
        """Anchor the GPS clock again: the timepulse goes to 1 Hz until it is anchored, then
        (status timer) to the selected rate."""
        self.c.gps.clock.reset()
        self.clock_seen = False
        self.on_rate()

    def timing_ready(self):
        """GPS trigger: anchored and stamping exactly at the selected rate."""
        c = self.c
        st = c.grabber.stamper
        return (c.gps is None or
                (c.gps.clock.anchored and not c.gps.clock.suspect and not self.gps_busy
                 and c.gps.tp_rate == self.selected_rate and st is not None
                 and st.rate == self.selected_rate and st.ready))

    def predicted_lag_ns(self):
        """Frame start after the trigger pulse: by the VEYE sensor's timing (IMX178, measured:
        frame period + ~3 ms); elsewhere just a guess (exposure + 3 ms)."""
        c = self.c
        delay = c.trigger_delay_ns()
        if c.is_veye:
            return c.timing.frame_lag_ns(c.camera.frame_period_ns(),
                                         c.controller.exposure_us * 1000, delay)
        return c.controller.exposure_us * 1000 + LAG_EXTRA_NS + delay

    def on_trigger_mode(self, initial=False):
        c = self.c
        mode = self.mode()
        if not initial:
            c.controller.request("trigger", mode)
        c.grabber.frame_period_ns = (c.camera.frame_period_ns
                                     if c.is_veye and mode == "free" else None)
        self.trig_button.setVisible(mode == "sw")
        if mode == "gps":
            if c.gps is not None and c.gps.clock.stale():
                c.gps.clock.reset()         # anchor (again) on GPS UTC
            c.grabber.stamper = None        # a new one for the current rate
            self.on_rate()
        else:
            c.grabber.stamper = None

    def pulse_rate(self):
        """The rate the pulses really come at: as the GPS confirmed it (its default 1 Hz
        until then), or without a GPS the selected one."""
        gps = self.c.gps
        if gps is None:
            return Fraction(self.gps_rate())
        return Fraction(gps.tp_rate) if gps.tp_rate else Fraction(1)

    def wanted_rate(self):
        """The selected rate, or 1 Hz until the GPS clock is anchored."""
        gps = self.c.gps
        if gps is not None and not gps.clock.anchored:
            return Fraction(1)
        return Fraction(self.gps_rate())

    def on_rate(self):
        """GPS trigger: stamp the pulses as they come; ask the GPS for the wanted rate. The
        stamper stays across the change (it takes either rate until the GPS confirms)."""
        c = self.c
        clock = c.gps.clock if c.gps is not None else None
        if self.mode() == "gps" and c.grabber.stamper is None:
            c.grabber.stamper = PulseStamper(self.pulse_rate(), self.predicted_lag_ns(), clock)
            c.grabber.missed = 0
        self.last_exposure = c.controller.exposure_us
        rate = self.wanted_rate()
        # tp_rate None: the GPS is untouched (or left at another rate by a crash): set it
        if (c.gps is None or self.gps_busy or self.mode() != "gps"
                or (rate == self.pulse_rate() and c.gps.tp_rate is not None)):
            return
        stamper = c.grabber.stamper
        stamper.change_rate(rate)

        def work():
            ok = False
            try:
                ok = c.gps.set_timepulse(rate)
                self.gps_msg = "" if ok else "GPS rejected the timepulse setting (NAK)"
            except Exception as e:
                self.gps_msg = f"GPS: {e}"
            if ok:
                stamper.rate_confirmed()
            else:
                stamper.cancel_change()
            self.gps_busy = False
            if ok:      # catch up if the selection changed meanwhile
                QtCore.QMetaObject.invokeMethod(self, "on_rate_later", QtCore.Qt.QueuedConnection)

        self.gps_busy = True
        self.gps_msg = f"setting GPS timepulse to {rate_str(rate)}..."
        threading.Thread(target=work, daemon=True).start()

    @Slot()
    def on_rate_later(self):
        self.on_rate()

    def on_delay(self, ms):
        self.c.trigger_delay_us = ms * 1000             # camera units (µs)
        if self.mode() != "free":
            self.c.controller.request("delay", self.c.trigger_delay_us)
        stamper = self.c.grabber.stamper
        if self.mode() == "gps" and stamper is not None:
            stamper.lag_ns = self.predicted_lag_ns()    # new pulse-to-frame lag

    def y_trigger_delay_us(self):
        """The delay that puts the timepulse on the y-trigger row (None: off or not GPS)."""
        c = self.c
        if not (c.is_veye and self.mode() == "gps" and self.ytrig_check.isChecked()):
            return None
        line = c.timing.line(format_bits(c.pixel_format))
        # for the selected rate, also while anchoring at 1 Hz: the delay stays short and
        # the camera is not restarted twice
        return onset_delay_us(self.ytrig_spin.value(), float(self.gps_rate()),
                              c.camera.frame_period_ns(), c.controller.exposure_us * 1000, line,
                              c.timing)

    def follow_y_trigger(self, busy=False):
        """Keeps the delay on the y-trigger row as exposure, region, depth and rate change
        (not while recording: each change restarts the camera)."""
        us = self.y_trigger_delay_us()
        self.view.hline = None if us is None else (self.ytrig_spin.value(), QtGui.QColor("#ff9100"))
        # dotted: rows whose exposure ends / starts on the pulse (the edge is spread between;
        # global shutter: no spread, all rows expose together)
        line = self.c.timing.line(format_bits(self.c.pixel_format))
        self.view.hspan = round(self.c.controller.exposure_us * 1000 / 2 / line) if line else 0
        self.view.update()
        if us is not None and not busy and abs(us - (self.c.trigger_delay_us or 0)) > 1:
            self.delay_spin.setValue(round(us) / 1000)  # -> on_delay

    def on_hline_moved(self, row):
        """The y-trigger line was dragged in the image."""
        if self.ytrig_spin.isEnabled():
            self.ytrig_spin.setValue(row)       # -> follow_y_trigger -> new delay
        self.follow_y_trigger()                 # (locked: the line goes back)

    def software_trigger(self):
        if self.mode() == "sw":
            self.c.camera.software_trigger()   # a single register write, fine on the GUI thread

    # ---- panel: GPS ----
    def make_gps_box(self):
        opened = self.settings.value("gps_open", True, type=bool)
        box = self.gps_box = CollapsibleBox("GPS", opened)
        box.toggled.connect(lambda o: self.settings.setValue("gps_open", o))
        g = box.grid
        self.gps_labels = {}
        rows = [("utc", "UTC (GPS)"), ("fix", "Fix"), ("sats", "Satellites"), ("dop", "DOP"),
                ("pos", "Position"), ("alt", "Altitude"), ("pulse", "Timepulse"),
                ("clock", "Time base"),
                ("stamp", "Exposure start"), ("pulse_t", "Trigger pulse"),
                ("lag", "Pulse → frame"), ("rows", "Rolling shutter")]
        if self.c.compass is not None:
            rows.append(("heading", "Heading"))
        for r, (key, name) in enumerate(rows):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            lab = QtWidgets.QLabel("-", wordWrap=True)
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            g.addWidget(lab, r, 1)
            self.gps_labels[key] = lab
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.gps_labels["utc"].setFont(mono)
        self.gps_labels["stamp"].setFont(mono)
        self.gps_labels["pulse_t"].setFont(mono)
        self.update_rows_label()
        return box

    def update_rows_label(self):
        c = self.c
        if not c.is_veye:
            return
        bits = format_bits(c.pixel_format)
        line = c.timing.line(bits)
        cal = c.timing.calibrated(bits)
        lab = self.gps_labels["rows"]
        if c.timing.global_shutter:
            lab.setText("global shutter: all rows expose together"
                        + ("" if cal else " - timing not measured"))
            lab.setStyleSheet("" if cal else "color: #e65100;")
            lab.setToolTip(f"{c.timing.name}: recorded time stamps are the exposure start"
                           + ("" if cal else "; the trigger-to-exposure offset is assumed, "
                              "not measured (LED test, CAPTURE.md)"))
            return
        lab.setText(f"row y +y×{line / 1000:.2f} µs ({c.height * line / 1e6:.1f} ms to the last row)"
                    + ("" if cal else " - estimate"))
        lab.setStyleSheet("" if cal else "color: #e65100;")
        lab.setToolTip("recorded time stamps are the exposure start of row 0"
                       + ("" if cal else f"; {c.timing.name}: timing not measured at {bits} "
                          "bit, stamps may be off by a few ms"))

    # ---- panel: recording ----
    def make_record_box(self):
        box, g = grid_box("Recording")
        self.dir_edit = QtWidgets.QLineEdit(self.args.record_dir)
        browse = QtWidgets.QToolButton(text="…")
        browse.clicked.connect(self.browse_dir)
        self.observer_edit = QtWidgets.QLineEdit(self.args.observer)
        self.telescope_edit = QtWidgets.QLineEdit(self.args.telescope)
        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems([f.upper() for f in RECORD_FORMATS])
        self.format_combo.setCurrentIndex(RECORD_FORMATS.index(self.args.record_format))
        self.format_combo.setToolTip("RAVF (as ASTRID writes): every frame carries its own "
                                     "time stamp, so a recording cut short by a crash stays "
                                     "usable. SER: timestamps in a trailer at the end (kept "
                                     "in a .times sidecar until then)")

        self.start_now = QtWidgets.QRadioButton("Now")
        self.start_at = QtWidgets.QRadioButton("At UTC")
        self.start_time = QtWidgets.QTimeEdit(displayFormat="HH:mm:ss")
        next_min = QtWidgets.QToolButton(text="next min")
        next_min.setToolTip("next full minute at least 10 s away")
        next_min.clicked.connect(self.set_next_minute)
        if self.args.record_start:
            t = parse_utc(self.args.record_start)
            self.start_time.setTime(QtCore.QTime(t.hour, t.minute, t.second))
            self.start_at.setChecked(True)
        else:
            self.set_next_minute()
            self.start_now.setChecked(True)
        self.start_time.timeChanged.connect(lambda _: self.start_at.setChecked(True))

        self.dur_check = QtWidgets.QCheckBox("Stop after")
        self.dur_spin = QtWidgets.QDoubleSpinBox(decimals=1, suffix=" s", maximum=1e6)
        self.dur_spin.setValue(60)
        self.frames_check = QtWidgets.QCheckBox("Stop after")
        self.frames_spin = QtWidgets.QSpinBox(suffix=" frames", maximum=10**9)
        self.frames_spin.setValue(100)
        if self.args.record_duration:
            self.dur_check.setChecked(True)
            self.dur_spin.setValue(self.args.record_duration)
        if self.args.record_frames:
            self.frames_check.setChecked(True)
            self.frames_spin.setValue(self.args.record_frames)

        self.rec_button = QtWidgets.QPushButton()
        self.rec_button.setMinimumHeight(40)
        self.rec_button.clicked.connect(self.on_record_button)
        self.rec_info = QtWidgets.QLabel(wordWrap=True)
        self.rec_info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        g.addWidget(QtWidgets.QLabel("Folder"), 0, 0)
        g.addWidget(self.dir_edit, 0, 1, 1, 2)
        g.addWidget(browse, 0, 3)
        g.addWidget(QtWidgets.QLabel("Format"), 1, 0)
        g.addWidget(self.format_combo, 1, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Observer"), 2, 0)
        g.addWidget(self.observer_edit, 2, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Telescope"), 3, 0)
        g.addWidget(self.telescope_edit, 3, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Start"), 4, 0)
        g.addWidget(self.start_now, 4, 1)
        g.addWidget(self.start_at, 5, 1)
        g.addWidget(self.start_time, 5, 2)
        g.addWidget(next_min, 5, 3)
        g.addWidget(self.dur_check, 6, 0, 1, 2)
        g.addWidget(self.dur_spin, 6, 2, 1, 2)
        g.addWidget(self.frames_check, 7, 0, 1, 2)
        g.addWidget(self.frames_spin, 7, 2, 1, 2)
        g.addWidget(self.rec_button, 8, 0, 1, 4)
        g.addWidget(self.rec_info, 9, 0, 1, 4)
        self.review_button = QtWidgets.QPushButton("Review last clip")
        self.review_button.setToolTip("open the last recording in the review player (time "
                                      "stamps, frame status, stack & plate solve)")
        self.review_button.clicked.connect(self.review_last)
        self.review_button.setEnabled(False)
        g.addWidget(self.review_button, 10, 0, 1, 4)
        self.rec_widgets = [self.dir_edit, browse, self.format_combo, self.observer_edit,
                            self.telescope_edit,
                            self.start_now, self.start_at, self.start_time, next_min,
                            self.dur_check, self.dur_spin, self.frames_check, self.frames_spin]
        return box

    def review_last(self):
        path = self.c.recorder.last_path
        cmd = review_command(path) if path else None
        if cmd is None:
            self.last_saved = ("the review player needs the full package: run "
                               "'uv run pystarlight-review FILE'")
            return
        print("Review: " + " ".join(cmd))
        subprocess.Popen(cmd, start_new_session=True)       # own process: capture goes on

    def browse_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder",
                                                       self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def set_next_minute(self):
        t = datetime.now(timezone.utc) + timedelta(seconds=10)
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.start_time.setTime(QtCore.QTime(t.hour, t.minute, 0))

    def on_record_button(self):
        rec = self.c.recorder
        if self.pending_rec is not None:        # cancel while anchoring
            self.pending_rec = None
            return
        if rec.state != "idle":
            rec.stop()
            return
        rec.out_dir = os.path.expanduser(self.dir_edit.text().strip() or self.args.record_dir)
        rec.text.update(observer=self.observer_edit.text(), telescope=self.telescope_edit.text())
        rec.fmt = RECORD_FORMATS[self.format_combo.currentIndex()]
        start_ns = None
        if self.start_at.isChecked():
            start = parse_utc(self.start_time.time().toString("HH:mm:ss"))
            start_ns = int(start.timestamp()) * 10**9
            print(f"Recording armed for {start.isoformat()}")
        args = (start_ns, self.dur_spin.value() if self.dur_check.isChecked() else None,
                self.frames_spin.value() if self.frames_check.isChecked() else None)
        if self.mode() == "gps" and self.c.gps is not None:
            # every recording: anchor on GPS UTC again (~5 s), 20 s before a set start time
            begin = time.time_ns() if start_ns is None else start_ns - REANCHOR_LEAD_NS
            self.pending_rec = {"args": args, "begin_ns": begin, "anchoring": False}
        else:
            rec.arm(*args)
        self.on_status_timer()

    def follow_pending_record(self):
        """Status timer: start anchoring when due; arm the recorder once the stamps are ready."""
        p = self.pending_rec
        if p is None:
            return
        if self.mode() != "gps":
            self.pending_rec = None
        elif not p["anchoring"]:
            if time.time_ns() >= p["begin_ns"]:
                p["anchoring"] = True
                self.reanchor()
        elif self.timing_ready():
            self.pending_rec = None
            start_ns = p["args"][0]
            if start_ns is not None and start_ns < time.time_ns():
                print("GPS anchoring took past the start time: recording starts now")
            self.c.recorder.arm(*p["args"])

    # ---- timers ----
    def on_frame_timer(self):
        c = self.c
        if not c.stream_lock.acquire(blocking=False):
            return                              # the stream is being replaced (new ROI)
        try:
            item = c.grabber.take()
            if item is not None:
                buf, self.frame_id, _ = item
                try:
                    if c.recorder.state == "recording":
                        if c.recorder.latest is not None:     # already unpacked by the recorder
                            self.img, c.recorder.latest = c.recorder.latest, None
                    else:
                        self.img = c.decode(buf)    # copies the data out of the buffer
                finally:
                    c.stream.push_buffer(buf)
                self.redraw = True
        finally:
            c.stream_lock.release()
        img = self.img
        if self.stack_show.isChecked():
            st = c.stacker
            if st.version != self.stack_seen:
                self.stack_seen = st.version
                mean = st.preview()
                self.stack_img = (None if mean is None
                                  else display_stretch(mean, c.decode.max_value))
                self.redraw = True
            img = self.stack_img if self.stack_img is not None else img
        if img is None or not self.redraw:
            return
        self.redraw = False
        # Order: debayer (needs full res) -> crop + scale to the view -> gamma LUT
        frame = cv2.cvtColor(img, c.decode.bayer_code) if c.decode.bayer_code is not None else img
        self.view.render(frame, self.lut)
        self.disp_n += 1

    def on_status_timer(self):
        c = self.c
        now = time.monotonic()
        mode = self.mode()
        rec = c.recorder

        # ---- stall watchdog: no complete frame for a while -> report and restart ----
        # (not when triggered: then no frames just means no triggers)
        stall_s = max(3.0, 3.0 * c.controller.exposure_us / 1e6 + 1.0)
        if mode == "free" and not c.controller.busy and now - c.grabber.last_ok > stall_s:
            done, failed, underrun = c.stream.get_statistics()
            print(f"STALL: no complete frame for {now - c.grabber.last_ok:.1f} s | "
                  f"stream completed {done}, failed {failed}, underruns {underrun} | "
                  f"bad statuses {c.grabber.bad or 'none'} | lost IDs {c.grabber.dropped}")
            c.grabber.last_ok = now
            c.controller.request("restart", True)

        dt = now - self.disp_t0
        if dt >= 1.0:
            self.disp_fps, self.disp_t0, self.disp_n = self.disp_n / dt, now, 0

        if c.controller.reconfigs != self.roi_seen:
            self.roi_seen = c.controller.reconfigs
            self.after_reconfigure()

        # exposure changed -> new frame period -> new pulse-to-frame lag
        stamper = c.grabber.stamper
        if stamper is not None and c.controller.exposure_us != self.last_exposure:
            self.last_exposure = c.controller.exposure_us
            stamper.lag_ns = self.predicted_lag_ns()
        self.follow_pending_record()
        # GPS clock just anchored (at 1 Hz): go to the selected rate (not while recording)
        anchored = c.gps is not None and c.gps.clock.anchored
        if anchored != self.clock_seen and not (anchored and rec.state != "idle"):
            self.clock_seen = anchored
            if anchored and mode == "gps":
                self.on_rate()

        # ---- camera ----
        peak = int(self.img.max()) if self.img is not None else 0
        self.cam_info.setText(f"{c.width}x{c.height} {c.pixel_format}   "
                              f"peak {peak}/{c.decode.max_value}")
        self.update_frame_info(mode)
        self.update_stack_box()

        # ---- trigger ----
        busy = rec.state != "idle" or self.pending_rec is not None
        self.gps_check.setEnabled(not busy)
        self.rate_combo.setEnabled(not busy and mode == "gps" and not self.gps_busy)
        self.follow_y_trigger(busy)
        ytrig = self.view.hline is not None
        self.delay_spin.setEnabled(not busy and not ytrig)
        self.ytrig_check.setEnabled(not busy and mode == "gps")
        self.ytrig_spin.setEnabled(not busy and mode == "gps")
        info = []
        trig = ""
        if mode != "free" and c.is_veye:
            count, lost = c.camera.get_trigger_count()
            trig = f"  trig {count}" + (f" lost {lost}" if lost else "")
        if mode == "gps":
            rate = float(self.pulse_rate())
            exp_ms = c.controller.exposure_us / 1000
            if exp_ms * rate > 1000:
                info.append(f"<b style='color:#d33'>Exposure {exp_ms:.0f} ms is longer than "
                            f"the pulse period {1000 / rate:.0f} ms.</b>")
            elif c.is_veye:
                delay_ns = c.trigger_delay_ns()
                n = pulses_per_frame(rate, c.camera.frame_period_ns(), delay_ns, c.timing)
                if stamper is not None:
                    stamper.stride = n
                if n > 1 and delay_ns < 1e9 / rate / 2:
                    fmax = 1e9 / c.timing.busy_ns(c.camera.frame_period_ns(), 0)
                    info.append(f"<b style='color:#d33'>The camera follows only ~{fmax:.0f} fps "
                                f"here; use a smaller ROI or a lower rate.</b>")
                elif n > 1:
                    info.append(f"The delay runs past the next pulse: the camera takes every "
                                f"{n}. pulse ({rate / n:g} fps).")
                line = c.timing.line(format_bits(c.pixel_format))
                if ytrig and not line:
                    info.append("y-trigger: timepulse in the middle of the exposure (global "
                                "shutter: every row)")
                elif ytrig:
                    info.append(f"y-trigger: timepulse mid-exposure on row {self.ytrig_spin.value()}; "
                                f"the edge spans ~{exp_ms * 1e6 / line:.0f} rows between the dotted "
                                f"lines (first lit row: upper line; shorter exposure: sharper)")
            if c.gps is None:
                info.append("No GPS: the trigger pulses must come at this rate by themselves.")
            elif not c.gps.has_fix():
                info.append("<b style='color:#d33'>No GPS fix: no timepulses.</b>")
            if c.gps is not None and not c.gps.clock.anchored:
                info.append("<b style='color:#e65100'>Anchoring to GPS UTC at 1 Hz (stamps: Pi "
                            "clock until then)</b>")
            elif c.gps is not None and c.gps.clock.suspect:
                info.append(f"<b style='color:#d33'>{c.gps.clock.message}</b>")
            if c.grabber.missed:
                info.append(f"missed pulses: {c.grabber.missed}")
            if stamper is not None and stamper.off_grid:
                info.append(f"frames off the pulse grid: {stamper.off_grid}")
            if stamper is not None and stamper.grid_moved:
                info.append(f"<b style='color:#d33'>TIM-TP pulses off the grid: "
                            f"{stamper.grid_moved}</b>")
            if Fraction(self.gps_rate()).denominator != 1:
                info.append(f"{rate_str(self.gps_rate())}: pulse phase from TIM-TP")
        if self.gps_msg:
            info.append(self.gps_msg)
        if self.rate_error:
            info.append(f"<b style='color:#d33'>{self.rate_error}</b>")
        self.trig_info.setText("<br>".join(info))
        self.trig_info.setVisible(bool(info))

        # ---- GPS ----
        L = self.gps_labels
        if c.gps is not None:
            for key, text in c.gps.summary().items():
                if key in L:
                    L[key].setText(text)
            fix_ok = c.gps.has_fix()
            L["fix"].setStyleSheet("" if fix_ok else "color: #d33; font-weight: bold;")
            utc = c.gps.utc.strftime("%H:%M:%S") if c.gps.utc else "-"
            self.gps_box.set_summary(f"{L['fix'].text().split(',')[0]}, {c.gps.sats_used} sats, "
                                     f"{utc} UTC" + ("" if fix_ok else "  NO FIX"))
        else:
            L["utc"].setText("no GPS")
            self.gps_box.set_summary("no GPS")
        if c.compass is not None:
            h = c.compass.heading()
            L["heading"].setText(f"{h:.0f}° magnetic (uncalibrated)" if h is not None else "-")
        if c.grabber.exp_ns is not None:
            L["stamp"].setText(utc_str(c.grabber.exp_ns) + (
                "  Pi clock" if c.grabber.pulse_ns is None else
                "  GPS UTC" if stamper is not None and stamper.clock is not None else
                "  pulse, Pi clock"))
        L["pulse_t"].setText(utc_str(c.grabber.pulse_ns) if c.grabber.pulse_ns is not None else "-")
        L["lag"].setText(f"{stamper.last_lag_ns / 1e6:.1f} ms"
                         if stamper is not None and stamper.last_lag_ns is not None else "-")

        # ---- region ----
        stacking = c.stacker.state == "stacking"
        for w in self.roi_spins + [self.roi_select, self.roi_apply, self.roi_full, self.bits_combo]:
            w.setEnabled(not busy and not stacking and not c.controller.busy)
        x, y, w, h = c.region
        sw, sh = c.sensor
        if not c.controller.busy:
            self.roi_info.setText(f"now {w}x{h} at ({x}, {y})"
                                  + ("" if (w, h) != (sw, sh) else " - full sensor")
                                  + ("<br>locked while recording" if busy else ""))

        # ---- recording ----
        for w in self.rec_widgets:
            w.setEnabled(not busy)
        self.review_button.setEnabled(bool(rec.last_path) and rec.state == "idle")
        if rec.state == "recording":
            self.rec_button.setText("■  Stop recording  (r)")
            self.rec_button.setStyleSheet("background: #c62828; color: white; font-weight: bold;")
            secs = (rec.last_ns - rec.first_ns) / 1e9 if rec.first_ns else 0.0
            text = (f"<b>Recording</b> {rec.frames} frames, {secs:.1f} s"
                    + (f" <b style='color:#d33'>DROPPED {rec.dropped}</b>" if rec.dropped else "")
                    + f"<br>{os.path.basename(rec.path or '')}")
            if rec.stop_ns:
                text += f"<br>stops at {utc_str(rec.stop_ns, 0)} UTC"
        elif self.pending_rec is not None:
            self.rec_button.setText("Cancel  (r)")
            self.rec_button.setStyleSheet("background: #ef6c00; color: white; font-weight: bold;")
            p = self.pending_rec
            if p["anchoring"]:
                text = (f"<b>Anchoring to GPS UTC</b> (1 Hz), then {rate_str(self.selected_rate)}"
                        f"; recording starts when the stamps are ready")
            else:
                text = (f"<b>Armed</b>: anchoring to GPS UTC at {utc_str(p['begin_ns'], 0)} UTC "
                        f"(in {(p['begin_ns'] - time.time_ns()) / 1e9:.0f} s)")
        elif rec.state == "armed":
            self.rec_button.setText("Cancel  (r)")
            self.rec_button.setStyleSheet("background: #ef6c00; color: white; font-weight: bold;")
            wait = (rec.start_ns - time.time_ns()) / 1e9
            text = (f"<b>Armed</b>: first frame at {utc_str(rec.start_ns, 0)} UTC "
                    f"(in {wait:.0f} s)")
        else:
            self.rec_button.setText("●  Record  (r)")
            self.rec_button.setStyleSheet("font-weight: bold;")
            if rec.last_path:
                self.last_saved = (f"Saved {rec.last_frames} frames: "
                                   f"{os.path.basename(rec.last_path)}"
                                   + (f" ({rec.last_dropped} dropped!)" if rec.last_dropped else ""))
            text = self.last_saved
        self.rec_info.setText(text)

        # ---- status bar ----
        g = c.grabber
        self.status.setText(
            f"exp {c.controller.exposure_us / 1000:.3f} ms  gain {c.controller.gain_db:.1f} dB  "
            f"cam {g.cam_fps:.1f} fps  view {self.disp_fps:.1f} fps  lost {g.dropped}  "
            f"bad {sum(g.bad.values())}  restarts {c.controller.restarts}  "
            f"frame {self.frame_id}{trig}" + self.cursor_text())

    def cursor_text(self):
        """Sensor position and raw value under the mouse."""
        p, img = self.view.cursor_img, self.img
        if p is None or img is None:
            return ""
        ix, iy = int(p[0]), int(p[1])
        if not (0 <= ix < img.shape[1] and 0 <= iy < img.shape[0]):
            return ""
        v = img[iy, ix]
        rx, ry = self.c.region[:2]
        return f"   ({rx + ix}, {ry + iy}) = {v if np.ndim(v) == 0 else tuple(v)}"

    def update_frame_info(self, mode):
        """Frame number, measured rate and spacing of the last ~200 frames, actual exposure."""
        c, g, F = self.c, self.c.grabber, self.frame_labels
        F["frame"].setText(f"#{self.frame_id if self.frame_id is not None else '-'}"
                           f"   lost {g.dropped + g.missed}")
        iv = np.array(g.intervals, dtype=np.float64) / 1e6     # ms
        if len(iv) >= 2:
            F["rate"].setText(f"{1000 / iv.mean():.3f} fps   ({len(iv) + 1} frames)")
            F["interval"].setText(f"{iv.mean():.3f} ms  ±{iv.std():.3f}  "
                                  f"{iv.min():.2f}..{iv.max():.2f}")
        else:
            F["rate"].setText(f"{g.cam_fps:.2f} fps")
            F["interval"].setText("-")
        exp = c.controller.exposure_us / 1000
        text = f"{exp:.3f} ms"
        if abs(c.controller.exposure_us - self.target["exposure"]) > 1:
            text += f"  (asked {self.target['exposure'] / 1000:.3f})"
        if c.is_veye and mode == "free":
            text += f"  period {c.camera.frame_period_ns() / 1e6:.3f}"
        F["exp"].setText(text)
        F["exp"].setToolTip("exposure read back from the camera"
                            + ("; period: frame period the camera is set to (free run)"
                               if c.is_veye else ""))

    # ---- panel: stack & plate solve ----
    def make_stack_box(self):
        opened = self.settings.value("stack_open", True, type=bool)
        box = self.stack_box = CollapsibleBox("Stack & plate solve", opened)
        box.toggled.connect(lambda o: self.settings.setValue("stack_open", o))
        g = box.grid
        self.stack_frames = QtWidgets.QSpinBox(minimum=1, maximum=10_000, suffix=" frames")
        self.stack_frames.setValue(self.args.stack_frames)
        self.stack_align = QtWidgets.QCheckBox("Align on the stars", checked=True)
        self.stack_align.setToolTip("follow the sky's drift (no tracking: ~15\"/s); off for "
                                    "short stacks or a tracking mount")
        self.focal_spin = QtWidgets.QDoubleSpinBox(decimals=1, maximum=10_000.0, suffix=" mm",
                                                   keyboardTracking=False)
        self.focal_spin.setValue(self.c.focal_mm)
        self.focal_spin.setSpecialValueText("unknown (blind solve)")
        self.focal_spin.valueChanged.connect(self.on_focal)
        self.scale_label = QtWidgets.QLabel()
        self.solve_check = QtWidgets.QCheckBox("Plate-solve when done", checked=True)
        self.stars_spin = QtWidgets.QSpinBox(minimum=0, maximum=30, prefix="name the ",
                                             suffix=" brightest stars")
        self.stars_spin.setValue(5)
        self.simbad_check = QtWidgets.QCheckBox("SIMBAD names", checked=True)
        self.stack_button = QtWidgets.QPushButton()
        self.stack_button.setMinimumHeight(32)
        self.stack_button.clicked.connect(self.on_stack_button)
        self.solve_button = QtWidgets.QPushButton("Solve")
        self.solve_button.setToolTip("plate-solve the last stack (again)")
        self.solve_button.clicked.connect(self.on_solve_button)
        self.stack_show = QtWidgets.QCheckBox("Show stack")
        self.stack_show.setToolTip("the view shows the stack (with the identified stars) "
                                   "instead of the live frames")
        self.stack_show.toggled.connect(self.on_stack_show)
        self.stack_info = QtWidgets.QLabel(wordWrap=True)
        self.stack_info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.stack_info.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        g.addWidget(QtWidgets.QLabel("Stack"), 0, 0)
        g.addWidget(self.stack_frames, 0, 1)
        g.addWidget(self.stack_align, 0, 2)
        g.addWidget(QtWidgets.QLabel("Focal length"), 1, 0)
        g.addWidget(self.focal_spin, 1, 1)
        g.addWidget(self.scale_label, 1, 2)
        g.addWidget(self.solve_check, 2, 0, 1, 2)
        g.addWidget(self.simbad_check, 2, 2)
        g.addWidget(self.stars_spin, 3, 0, 1, 3)
        buttons = QtWidgets.QHBoxLayout()
        for w in (self.stack_button, self.solve_button, self.stack_show):
            buttons.addWidget(w)
        g.addLayout(buttons, 4, 0, 1, 3)
        self.live_button = QtWidgets.QPushButton("▶  Continue live view  (c)")
        self.live_button.setMinimumHeight(32)
        self.live_button.setStyleSheet("font-weight: bold;")
        self.live_button.setToolTip("back to the live frames; a running stack goes on, the "
                                    "stack and its solution stay ('Show stack' brings them back)")
        self.live_button.clicked.connect(self.continue_live)
        self.live_button.setVisible(False)
        g.addWidget(self.live_button, 5, 0, 1, 3)
        g.addWidget(self.stack_info, 6, 0, 1, 3)
        self.on_focal(self.c.focal_mm)
        return box

    def on_focal(self, mm):
        c = self.c
        c.focal_mm = mm
        if mm and c.pixel_um:
            self.scale_label.setText(f"{206.265 * c.pixel_um / mm:.2f}\"/px ({c.pixel_um} µm)")
        else:
            self.scale_label.setText("pixel size unknown" if not c.pixel_um else "")

    def on_stack_button(self):
        st = self.c.stacker
        if st.state == "stacking":
            st.cancel()
            return
        st.out_dir = os.path.expanduser(self.dir_edit.text().strip() or self.args.record_dir)
        self.solution = None
        self.view.circles = []
        st.begin(self.stack_frames.value(), self.stack_align.isChecked())
        self.stack_show.setChecked(True)

    def on_stack_show(self, on):
        self.stack_seen = -1
        self.live_button.setVisible(on)
        if not on:
            self.view.circles = []
            self.view.banner = ""
        else:
            self.show_solution_marks()
        self.redraw = True

    def continue_live(self):
        """Back to the live frames (the stack keeps running if it is, and stays shown-able)."""
        self.stack_show.setChecked(False)

    def on_solve_button(self):
        if self.solve_thread is not None:
            self.solve_cancel.set()
            return
        if self.c.stacker.path:
            self.start_solve(self.c.stacker.path)

    def start_solve(self, path):
        try:
            from pystarlight.wcs.solve_stack import solve_fits
        except ImportError as e:
            self.solve_log = (f"plate solving needs the full package ({e}): run the app with "
                              "'uv run pystarlight-camera'")
            return
        cancel = threading.Event()
        n, simbad = self.stars_spin.value(), self.simbad_check.isChecked()

        def log(msg):
            self.solve_log = msg
            print(f"solve: {msg}")

        def work():
            try:
                self.solution = solve_fits(path, n, use_simbad=simbad, log=log, cancel=cancel)
                self.solved_path = path
                self.solve_log = ""
            except Exception as e:
                self.solve_log = f"plate solve failed: {e}"
                print(self.solve_log)
            self.solve_thread = None

        self.solve_cancel = cancel
        self.solve_log = "plate solving..."
        self.solve_thread = threading.Thread(target=work, daemon=True)
        self.solve_thread.start()

    def show_solution_marks(self):
        sol = self.solution
        if not sol or not self.stack_show.isChecked():
            return
        color = QtGui.QColor("#00e676")
        self.view.circles = [(s["x"], s["y"], max(4.0, 2 * (s.get("fwhm") or 2)),
                              f"{i} {s.get('name') or ''}".strip(), color)
                             for i, s in enumerate(sol.get("stars", []), 1)]
        self.view.update()

    def update_stack_box(self):
        st = self.c.stacker
        if st.state == "stacking":
            self.stack_button.setText("Cancel stack  (s)")
            self.stack_button.setStyleSheet("background: #1565c0; color: white; font-weight: bold;")
            text = f"stacking {st.count}/{st.target}"
            if st.align and st.count > 1:
                text += f", drift {st.shift[0]:+.1f}, {st.shift[1]:+.1f} px"
            if st.unaligned:
                text += f" ({st.unaligned} not aligned)"
        else:
            self.stack_button.setText("Stack  (s)")
            self.stack_button.setStyleSheet("font-weight: bold;")
            text = st.error or (f"saved {os.path.basename(st.path)}" if st.path else "")
        if st.state == "done" and self.solve_check.isChecked() and self.solve_thread is None \
                and st.path not in (self.solved_path, self.auto_solved):
            self.auto_solved = st.path                  # once per stack
            self.start_solve(st.path)
        self.solve_button.setText("Cancel solve" if self.solve_thread else "Solve")
        self.solve_button.setEnabled(bool(st.path) or self.solve_thread is not None)
        for w in (self.stack_frames, self.stack_align, self.focal_spin):
            w.setEnabled(st.state != "stacking")
        if self.solve_log:
            text += "\n" + self.solve_log
        sol = self.solution
        if sol is not None and self.solved_path == st.path:
            from pystarlight.wcs.solve_stack import describe
            text += ("\n" if text else "") + describe(sol)
            if not self.view.circles:
                self.show_solution_marks()
        if text != self.stack_info.text():
            self.stack_info.setText(text)
        banner = ""
        if self.stack_show.isChecked():
            banner = (f"STACK {st.count}/{st.target}" if st.state == "stacking" else
                      f"STACK {st.count} frames" + (" - solved" if sol is not None
                                                     and self.solved_path == st.path else ""))
            banner += "   ·   c: continue live view"
        if banner != self.view.banner:
            self.view.banner = banner
            self.view.update()
        centre = sol["center"] if sol else None
        self.stack_box.set_summary(f"RA {centre['ra_hms']} Dec {centre['dec_dms']}" if centre else
                                   f"stacking {st.count}/{st.target}" if st.state == "stacking"
                                   else "")

    def closeEvent(self, event):
        if self.solve_cancel is not None:
            self.solve_cancel.set()
        self.settings.setValue("splitter", self.splitter.sizes())
        self.frame_timer.stop()
        self.status_timer.stop()
        event.accept()
