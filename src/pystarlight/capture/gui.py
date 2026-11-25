"""Qt GUI of the capture app: live image view, control panel, status.

The main thread only renders the newest frame (debayer -> crop + scale to the view -> levels
+ gamma LUT) and talks to the camera through the Controller thread; see app.py for the threads.

Keys: e/E exposure -/+, g/G gain -/+, d/D gamma -/+, a auto levels, t software trigger,
r record on/off, s stack, c continue live view, q quit.
"""
import importlib.util
import math
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

from . import bodies, favorites, localconfig, photo, starnames, station_header
from .cameras import VEYE_MIN_ROI
from .decode import format_bits, format_for_bits
from .imageview import ImageView, RecordOverlay  # noqa: F401  (also imported from gui)
from .qt import QShortcut, QtCore, QtGui, QtWidgets, Signal, Slot
from .recorder import RECORD_FORMATS, file_tag
from .station_dialog import StationDialog, default_site  # noqa: F401
from .timing import (
    GPS_RATES,
    LAG_EXTRA_NS,
    PulseStamper,
    max_exposure_us,
    onset_delay_us,
    parse_rate,
    parse_utc,
    pulses_per_frame,
    rate_str,
    utc_now_ns,
    utc_str,
)
from .version import CAMERA_VERSION
from .widgets import (  # noqa: F401
    GAMMA_MAX,
    GAMMA_MIN,
    LEVELS_SMOOTH,
    NIGHT_DIM,
    NIGHT_QSS,
    NIGHT_RED,
    CollapsibleBox,
    LevelsBox,
    grid_box,
    night_color,
    zscale,
)

EXP_STEPS = 1000            # exposure slider resolution (log scale)
PHOTO_MAX_US = 60e6         # longest photo: frames of the camera's longest exposure, summed
# exposure slider ranges (us): short up to 2 s; typing a longer exposure switches it to 0.5 .. 60 s
# (or the camera's limit), typing one below 1 s back
EXP_SHORT_MAX_US = 2e6
EXP_LONG_MIN_US = 0.5e6
EXP_BACK_DOWN_US = 1e6
REANCHOR_LEAD_NS = 20 * 10**9   # set start time: anchor on GPS UTC this long before
GAIN_STEPS = 10             # gain slider: 0.1 dB per step
STACK_FULL = 65535          # the shown stack: mean scaled to 0..this (keeps the fraction)
SOLVE_RADIUS_DEG = 3.0      # plate solve: search this far around the expected position


def player_command(path):
    """Command line that opens `path` in the player (pystarlight.capture.player): this
    interpreter when the full package is importable (uv run), else the repo's .venv."""
    module = ["-m", "pystarlight.capture.player", str(path)]
    if importlib.util.find_spec("pystarlight") is not None:
        return [sys.executable, *module]
    venv = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "python"
    return [str(venv), *module] if venv.exists() else None




class MainWindow(QtWidgets.QMainWindow):
    favorites_ready = Signal(object)         # PySyzygos favorites (list) or an error message (str)

    def __init__(self, ctx, args):
        super().__init__()
        self.args = args
        self.c = ctx                        # camera, controller, grabber, recorder, gps ...
        self.set_title(ctx)
        self.lut, self.lut_key = None, None # display lookup table and its (size, scale)
        self.img = None                     # newest decoded frame (full resolution)
        self.frame_id = None
        self.redraw = False
        self.levels_pending = False          # set the levels from the next frame (new depth)
        self.disp_fps, self.disp_t0, self.disp_n = 0.0, time.monotonic(), 0
        self.gps_busy = False               # timepulse rate being set (worker thread)
        self.gps_msg = ""
        self.last_exposure = None
        self.clock_seen = False             # GPS clock anchored (poll: then set the rate)
        # record pressed in GPS mode: anchor on GPS UTC again first, then arm the recorder
        # ({"args": arm() arguments, "begin_ns": when to start anchoring, "anchoring": bool})
        self.pending_rec = None
        self.wait_fix = False           # Record pressed without a GPS fix: arms once there is one
        self.last_saved = ""
        self.stack_seen = -1                # stacker.version shown
        self.stack_mean = None              # stack shown (float, raw values)
        self.stack_img = None               # ... as uint16 for the view (x STACK_FULL / max)
        self.solve_thread = None            # plate solve running (worker thread)
        self.solve_cancel = None
        self.solve_log = ""                 # its newest message
        self.solution = None                # result dict of the last solve
        self.solve_count = 0                # solutions so far (a new one redoes the markers)
        self.solved_path = None             # the stack it belongs to
        self.auto_solved = None             # stack already sent by "Plate-solve when done"
        # markers (brightest stars + target, catalogue names) from the solution, in sensor
        # pixels of the stack's first frame: [{"x", "y", "r", "label", "kind"}]
        self.marks = []
        self.marks_key = None               # (solve count, target, n, verify) they were made for
        self.marks_roi = (0, 0)             # the stack's region offset
        self.marks_thread = None            # naming them (worker thread: network)
        self.marks_log = ""
        self.marks_shift = (0.0, 0.0)       # field drift since the stack (live frames)
        self.marks_frame = None             # frame id the drift was last measured on
        self.target_radec = None            # (ra, dec) of the target once looked up
        self.target_found = None            # (text typed, catalogue name) of that look-up
        self.body_at = 0.0                  # the Moon / a planet as the target: its position's time
        self.roi_seen = 0                   # controller.roi_changes already followed
        self.roi_pending = None             # selected/typed region not applied yet
        # last requested values (keys step from these, not from a lagging camera read-back)
        self.target = {"exposure": ctx.controller.exposure_us, "gain": ctx.controller.gain_db}

        self.settings = QtCore.QSettings("PyStarLight", "camera")  # layout, target, observer, telescope
        self.restore_camera_settings()
        self.view = ImageView()
        self.rec_overlay = RecordOverlay(self.view, self.on_record_button, self.on_photo)
        self.view.changed.connect(self.on_view_changed)
        self.view.selected.connect(self.on_roi_selected)
        self.view.hline_moved.connect(self.on_hline_moved)
        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.addWidget(self.make_camera_box())
        self.levels = LevelsBox(self.shown_image, float(np.clip(self.args.gamma, GAMMA_MIN, GAMMA_MAX)),
                                full=lambda: (0, self.c.decode.max_value))
        self.levels.changed.connect(self.on_levels)
        self.cross_check = QtWidgets.QCheckBox("Crosshair")
        self.cross_check.setToolTip("a crosshair through the image centre, its middle open")
        self.cross_check.setChecked(self.settings.value("crosshair", False, type=bool))
        self.view.crosshair = self.cross_check.isChecked()
        self.cross_check.toggled.connect(self.on_crosshair)
        self.levels.layout().addWidget(self.cross_check, 3, 0, 1, 3)
        col.addWidget(self.levels)
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
        self.make_view_menu()
        self.make_telescope_docks()
        self.make_dslr_menu()
        if self.settings.value("night", False, type=bool):     # once the docks exist
            self.night_action.setChecked(True)
        view_w = int(ctx.width * self.args.scale)
        self.resize(view_w + panel_w + 20, max(int(ctx.height * self.args.scale) + 40, 600))
        self.splitter.setSizes([view_w, panel_w])
        sizes = self.settings.value("splitter")
        if sizes:
            self.splitter.setSizes([int(v) for v in sizes])

        for key, fn in (("e", lambda: self.step_exposure(0.8, user=True)),
                        ("Shift+E", lambda: self.step_exposure(1.25, user=True)),
                        ("g", lambda: self.gain_spin.setValue(self.target["gain"] - 0.5)),
                        ("Shift+G", lambda: self.gain_spin.setValue(self.target["gain"] + 0.5)),
                        ("d", lambda: self.levels.gamma_spin.setValue(self.levels.gamma - 0.1)),
                        ("Shift+D", lambda: self.levels.gamma_spin.setValue(self.levels.gamma + 0.1)),
                        ("a", self.levels.auto_levels),
                        ("r", self.on_record_button),
                        ("t", self.software_trigger),
                        ("s", self.on_stack_button),
                        ("c", self.continue_live),
                        ("m", self.marks_check.toggle),
                        ("Esc", self.continue_live),
                        ("q", self.close)):
            QShortcut(QtGui.QKeySequence(key), self, fn)

        self.frame_timer = QtCore.QTimer(self, timeout=self.on_frame_timer, interval=5)
        self.frame_timer.start()
        self.status_timer = QtCore.QTimer(self, timeout=self.on_status_timer, interval=250)
        self.status_timer.start()
        self.levels.full_levels()           # live frames as they are until a / dragged
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
        for r, (name, slider, spin) in enumerate((("Exposure", self.exp_slider, self.exp_spin),
                                                  ("Gain", self.gain_slider, self.gain_spin))):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            g.addWidget(slider, r, 1)
            g.addWidget(spin, r, 2)
        self.cam_info = QtWidgets.QLabel()
        g.addWidget(self.cam_info, 2, 0, 1, 3)
        # measured frame timing (from the frame-start times) and the exposure actually set
        self.frame_labels = {}
        for r, (key, name) in enumerate((("frame", "Frame"), ("rate", "Rate"),
                                         ("interval", "Interval"), ("exp", "Exposure")), 3):
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
        if c.is_v4l2:                       # a video device: its pixel formats instead
            self.bits_choices = list(c.available)
            self.bits_combo.clear()
            self.bits_combo.addItems(self.bits_choices)
            self.bits_combo.setToolTip("the device's pixel format (luminance kept): YUYV / UYVY / "
                                       "GREY as sent; MJPG is compressed (worse for photometry "
                                       "and a time-code OCR)")
        self.show_bits()
        self.bits_combo.currentIndexChanged.connect(self.on_bits)
        g.addWidget(QtWidgets.QLabel("Depth"), 7, 0)
        g.addWidget(self.bits_combo, 7, 1, 1, 2)

        self.choose_exp_range(self.target["exposure"])
        self.set_exposure_widgets(self.target["exposure"])
        self.set_gain_widgets(self.target["gain"])
        if c.is_v4l2:                       # a video device: its own controls, where it has them
            self.gain_spin.setSuffix("")    # device units, not dB
            cam = c.camera
            for fixed, widgets, what in ((cam.exposure_fixed, (self.exp_slider, self.exp_spin), "exposure"),
                                         (cam.gain_fixed, (self.gain_slider, self.gain_spin), "gain")):
                for w in widgets:
                    w.setEnabled(not fixed)
                    if fixed:
                        w.setToolTip(f"this video device has no {what} control: the camera sets it")
        self.exp_slider.valueChanged.connect(lambda p: self.set_exposure(self.slider_to_exp(p), user=True))
        self.exp_spin.valueChanged.connect(lambda ms: self.set_exposure(ms * 1000, typed=True, user=True))
        self.gain_slider.valueChanged.connect(
            lambda p: self.set_gain(c.gain_min + p / GAIN_STEPS))
        self.gain_spin.valueChanged.connect(self.set_gain)
        return box

    def camera_key(self, what):
        """QSettings key of a per-camera value, e.g. 'camera/ZWO ASI432MM/gain_db'."""
        return f"camera/{self.c.describe()}/{what}"

    def restore_camera_settings(self):
        """Exposure and gain as last used with this camera model, unless --exposure / --gain
        say otherwise (the camera itself may hold another program's values)."""
        saved = {}
        for what, arg, key in (("exposure", "exposure", "exposure_us"), ("gain", "gain", "gain_db")):
            v = self.settings.value(self.camera_key(key), None)
            if v is None or getattr(self.args, arg, None) is not None:
                continue
            try:
                saved[what] = float(v)
            except (TypeError, ValueError):
                continue
        if "exposure" in saved:
            us = float(np.clip(saved["exposure"], self.c.exp_min, self.c.exp_max))
            self.target["exposure"] = us
            self.c.controller.request("exposure", us)
        if "gain" in saved:
            db = float(np.clip(saved["gain"], self.c.gain_min, self.c.gain_max))
            self.target["gain"] = db
            self.c.controller.request("gain", db)

    def remember_camera_settings(self):
        self.settings.setValue(self.camera_key("exposure_us"), self.target["exposure"])
        self.settings.setValue(self.camera_key("gain_db"), self.target["gain"])

    def exp_ranges(self):
        """(short, long) slider ranges in us; long is None when the camera stops at 2 s."""
        lo, hi = self.c.exp_min, self.c.exp_max
        short = (lo, min(hi, EXP_SHORT_MAX_US))
        return short, ((max(lo, EXP_LONG_MIN_US), hi) if hi > EXP_SHORT_MAX_US else None)

    def choose_exp_range(self, us):
        """Slider range for a typed exposure: long above 2 s, short below 1 s, else as it is
        (the slider itself never switches it, so a drag stays in its range)."""
        short, long = self.exp_ranges()
        current = getattr(self, "exp_range", None)
        if long is not None and us > EXP_SHORT_MAX_US:
            new = long
        elif current is None or long is None or us < EXP_BACK_DOWN_US:
            new = short
        else:
            new = current
        if new == current:
            return
        self.exp_range = new
        tip = f"exposure {new[0] / 1000:g} .. {new[1] / 1000:g} ms (log scale)"
        if long is not None:
            tip += (f"; type more than 2 s in the box for up to {long[1] / 1e6:g} s" if new == short
                    else "; type less than 1 s in the box for the short range")
        self.exp_slider.setToolTip(tip)

    def exp_to_slider(self, us):
        lo, hi = self.exp_range
        us = min(max(us, lo), hi)
        return int(round(EXP_STEPS * np.log(us / lo) / np.log(hi / lo)))

    def slider_to_exp(self, pos):
        lo, hi = self.exp_range
        return float(lo * (hi / lo) ** (pos / EXP_STEPS))

    @staticmethod
    def _quiet(widget, value):
        widget.blockSignals(True)
        widget.setValue(value)
        widget.blockSignals(False)

    def set_exposure_widgets(self, us):
        self._quiet(self.exp_slider, self.exp_to_slider(float(np.clip(us, self.c.exp_min, self.c.exp_max))))
        self._quiet(self.exp_spin, us / 1000)

    def set_gain_widgets(self, db):
        self._quiet(self.gain_slider, int(round((db - self.c.gain_min) * GAIN_STEPS)))
        self._quiet(self.gain_spin, db)

    def set_exposure(self, us, typed=False, user=False):
        """typed: from the box or the e / E keys, which may switch the slider's range. user: set
        by hand, which overrides "Exposure: longest for the rate" (unticked)."""
        if user and getattr(self, "expmax_check", None) is not None and self.expmax_check.isChecked() \
                and self.mode() == "gps":
            self.expmax_check.blockSignals(True)
            self.expmax_check.setChecked(False)
            self.expmax_check.blockSignals(False)
            self.settings.setValue("exposure_for_rate", False)
        us = float(np.clip(us, self.c.exp_min, self.c.exp_max))
        if typed:
            self.choose_exp_range(us)
        self.target["exposure"] = us
        self.set_exposure_widgets(us)
        self.c.controller.request("exposure", us)
        self.remember_camera_settings()

    def step_exposure(self, factor, user=False):
        self.set_exposure(self.target["exposure"] * factor, typed=True, user=user)

    def fit_exposure_to_rate(self):
        """GPS trigger with "Exposure: longest for the rate" ticked: the longest exposure with
        which the camera still takes every pulse (timing.max_exposure_us, by the sensor's
        profile and the region's readout). Nothing when the rate is out of reach."""
        c = self.c
        if self.mode() != "gps" or not self.expmax_check.isChecked() or not c.can_trigger:
            return
        delay = 0 if self.ytrig_check.isChecked() else c.trigger_delay_ns()   # y-trigger: follows
        readout = getattr(c.camera, "_readout_period_ns", lambda: None)() if c.is_veye else 0
        us = max_exposure_us(float(self.gps_rate()), delay, c.timing if c.is_veye else None, readout)
        if us is None:
            return
        us = float(np.clip(us, c.exp_min, c.exp_max))
        if abs(us - self.target["exposure"]) > 1:
            self.set_exposure(us, typed=True)
            print(f"exposure {us / 1000:.3f} ms: the longest for {rate_str(self.gps_rate())}")

    def set_gain(self, db):
        db = float(np.clip(db, self.c.gain_min, self.c.gain_max))
        self.target["gain"] = db
        self.set_gain_widgets(db)
        self.c.controller.request("gain", db)
        self.remember_camera_settings()

    def show_bits(self):
        bits = self.c.pixel_format if self.c.is_v4l2 else format_bits(self.c.pixel_format)
        if bits in self.bits_choices:
            self.bits_combo.blockSignals(True)
            self.bits_combo.setCurrentIndex(self.bits_choices.index(bits))
            self.bits_combo.blockSignals(False)

    def on_bits(self, index):
        c = self.c
        fmt = (self.bits_choices[index] if c.is_v4l2 else
               format_for_bits(self.bits_choices[index], c.available, c.pixel_format))
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

    def set_title(self, c):
        self.setWindowTitle(f"PyStarLight camera {CAMERA_VERSION} - {c.camera.get_vendor_name()} "
                            f"{c.camera.get_model_name()} - {c.width}x{c.height} {c.pixel_format}")

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
        self.set_title(c)
        self.show_roi(c.region)
        self.show_bits()
        self.roi_pending = None
        self.update_roi_marks()
        self.update_rows_label()
        self.ytrig_spin.setMaximum(c.height - 1)
        self.img = None                         # old size/format; wait for a new frame
        self.levels_pending = True              # new value range: levels from the next frame
        self.last_exposure = None               # new frame period -> new pulse-to-frame lag
        self.fit_exposure_to_rate()             # a new region reads out faster or slower

    # ---- view ----
    def on_view_changed(self):
        self.redraw = True

    def shown_image(self):
        """The image the levels belong to: the stack (raw values) while shown, else the frame."""
        if self.stack_show.isChecked() and self.stack_mean is not None:
            return self.stack_mean
        return self.img

    def on_levels(self):
        self.lut = None                         # rebuilt for the shown image
        self.redraw = True

    def display_lut(self, n, scale):
        if self.lut is None or self.lut_key != (n, scale):
            self.lut, self.lut_key = self.levels.lut(n, scale), (n, scale)
        return self.lut

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
        self.expmax_check = QtWidgets.QCheckBox("Exposure: longest for the rate")
        self.expmax_check.setToolTip("GPS trigger: set the exposure to the longest with which the "
                                     "camera still takes every pulse, whenever the rate (or the "
                                     "region) changes. Changing the exposure by hand unticks it.")
        self.expmax_check.setChecked(self.settings.value("exposure_for_rate", True, type=bool))
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
        if not self.c.can_trigger:          # a video device (/dev/videoN), a ZWO ASI camera
            what = "an ASI camera" if self.c.is_asi else "a video device"
            self.gps_check.setChecked(False)
            self.gps_check.setText(f"GPS trigger  (not for {what})")
            self.gps_check.setToolTip(f"{what} is not triggered here: frames carry the "
                                      + ("Pi clock (NTP) of their arrival over USB" if self.c.is_asi
                                         else "driver's time (Pi clock, NTP)")
                                      + ". For the true time record an embedded time code (VTI) "
                                      "or GPS LED flashes in the image")
            self.delay_spin.setEnabled(False)
        g.addWidget(self.gps_check, 0, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("GPS frame rate"), 1, 0)
        g.addWidget(self.rate_combo, 1, 1)
        g.addWidget(self.expmax_check, 2, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("Trigger delay"), 3, 0)
        g.addWidget(self.delay_spin, 3, 1)
        g.addWidget(self.ytrig_check, 4, 0)
        g.addWidget(self.ytrig_spin, 4, 1)
        g.addWidget(self.trig_button, 5, 0, 1, 2)
        g.addWidget(self.trig_info, 6, 0, 1, 2)
        self.expmax_check.setVisible(self.c.can_trigger)
        self.expmax_check.toggled.connect(self.on_expmax)
        self.gps_check.toggled.connect(lambda _: self.on_trigger_mode())
        self.rate_combo.activated.connect(lambda _: self.on_rate_text())
        self.rate_combo.lineEdit().editingFinished.connect(self.on_rate_text)
        self.delay_spin.valueChanged.connect(self.on_delay)
        self.ytrig_check.toggled.connect(lambda _: self.follow_y_trigger())
        self.ytrig_spin.valueChanged.connect(lambda _: self.follow_y_trigger())
        return box

    def mode(self):
        return "gps" if self.gps_check.isChecked() else self.base_mode

    def on_expmax(self, on):
        self.settings.setValue("exposure_for_rate", on)
        self.fit_exposure_to_rate()

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
        self.fit_exposure_to_rate()
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
                                     if (c.is_veye or c.is_v4l2) and mode == "free" else None)
        self.trig_button.setVisible(mode == "sw")
        if mode == "gps":
            if not initial:
                self.fit_exposure_to_rate()
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
                ("clock", "Time base"), ("sysclock", "Pi clock"),
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
        # station and equipment: [observer] of the private config, edited in a dialog
        self.station_button = QtWidgets.QPushButton("Station && equipment …")
        self.station_button.setToolTip("observer(s), station, telescope, aperture, focal length, "
                                       "mount, method, camera delay, fallback site: written into "
                                       "every recording; required before recording")
        self.station_button.clicked.connect(lambda: self.edit_station())
        self.station_info = QtWidgets.QLabel(wordWrap=True)
        self.asteroid_edit = QtWidgets.QLineEdit(self.settings.value("asteroid", "", type=str))
        self.asteroid_edit.setPlaceholderText("occulting asteroid, e.g. 158772 (2003 SB60)")
        self.asteroid_edit.setToolTip("goes into the file name (before the star) and the "
                                      "header's OBJNAME")
        self.asteroid_edit.editingFinished.connect(
            lambda: self.settings.setValue("asteroid", self.asteroid_edit.text().strip()))
        self.fav_button = QtWidgets.QPushButton("Favorites")
        self.fav_button.setToolTip("events of the next 12 h from the PySyzygos server "
                                   "([pysyzygos_server] in the config, or --pysyzygos URL): "
                                   "fills asteroid, target star and the recording window "
                                   "(event -{:.0f} s to +{:.0f} s; [camera] in the config)".format(
                                       *favorites.window()))
        self.fav_button.clicked.connect(self.fetch_favorites)
        self.favorites_ready.connect(self.on_favorites)
        self.event_info = QtWidgets.QLabel(wordWrap=True)
        self.event_info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
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
        g.addWidget(self.station_button, 2, 0, 1, 4)
        g.addWidget(self.station_info, 3, 0, 1, 4)
        g.addWidget(QtWidgets.QLabel("Asteroid"), 4, 0)
        g.addWidget(self.asteroid_edit, 4, 1, 1, 2)
        g.addWidget(self.fav_button, 4, 3)
        g.addWidget(self.event_info, 5, 0, 1, 4)
        g.addWidget(QtWidgets.QLabel("Start"), 6, 0)
        g.addWidget(self.start_now, 6, 1)
        g.addWidget(self.start_at, 7, 1)
        g.addWidget(self.start_time, 7, 2)
        g.addWidget(next_min, 7, 3)
        g.addWidget(self.dur_check, 8, 0, 1, 2)
        g.addWidget(self.dur_spin, 8, 2, 1, 2)
        g.addWidget(self.frames_check, 9, 0, 1, 2)
        g.addWidget(self.frames_spin, 9, 2, 1, 2)
        g.addWidget(self.rec_button, 10, 0, 1, 4)
        g.addWidget(self.rec_info, 11, 0, 1, 4)
        self.review_button = QtWidgets.QPushButton("Play last clip")
        self.review_button.setToolTip("open the last recording in the player (GPS / time "
                                      "stamps, frame status, stack & plate solve)")
        self.review_button.clicked.connect(self.review_last)
        self.review_button.setEnabled(False)
        g.addWidget(self.review_button, 12, 0, 1, 4)
        self.show_station()
        self.rec_widgets = [self.dir_edit, browse, self.format_combo, self.station_button,
                            self.asteroid_edit, self.fav_button,
                            self.start_now, self.start_at, self.start_time, next_min,
                            self.dur_check, self.dur_spin, self.frames_check, self.frames_spin]
        return box

    # ---- PySyzygos favorites: tonight's events ----
    def fetch_favorites(self):
        """Favorites button: ask the PySyzygos server (worker thread) for the next 12 h."""
        url, token = favorites.server_settings(getattr(self.args, "pysyzygos", None))
        past = favorites.past_hours()
        now = self.now_utc()
        self.fav_button.setEnabled(False)
        self.event_info.setText(f"asking {url or 'PySyzygos'} for the next 12 h"
                                + (f" (and the last {past:g} h)" if past else "") + " ...")

        def work():
            try:
                self.favorites_ready.emit(favorites.fetch_favorites(url, token, past=past, now=now))
            except Exception as e:          # no server, refused token, bad answer
                self.favorites_ready.emit(f"favorites: {e}")
        threading.Thread(target=work, daemon=True).start()

    def on_favorites(self, result):
        self.fav_button.setEnabled(self.c.recorder.state == "idle")
        if isinstance(result, str):
            self.event_info.setText(result)
            return
        if not result:
            self.event_info.setText("no favorites in the time window")
            return
        fav = result[0]
        if len(result) > 1:
            items = [favorites.describe(f) for f in result]
            choice, ok = QtWidgets.QInputDialog.getItem(self, "PySyzygos favorites",
                                                        f"{len(result)} events:",
                                                        items, 0, False)
            if not ok:
                self.event_info.setText("")
                return
            fav = result[items.index(choice)]
        self.apply_favorite(fav)

    def apply_favorite(self, fav):
        """Asteroid, target star (and its Gaia DR3 id to verify), start before the event and stop
        after it by the config's [camera] record_before_s / record_after_s (30 s each)."""
        plan = favorites.recording_plan(fav)
        self.asteroid_edit.setText(plan["asteroid"])
        self.settings.setValue("asteroid", plan["asteroid"])
        self.target_edit.setText(plan["target"])
        self.verify_edit.setText(plan["verify"])
        self.on_target()
        start = plan["start"].replace(microsecond=0)            # the start field has whole seconds
        end = plan["event"] + timedelta(seconds=plan["trail_s"])
        now = self.now_utc()
        if end <= now:                          # over (past_hours, tests): the same length, now
            end = now + timedelta(seconds=plan["duration_s"])
        if start <= now:
            self.start_now.setChecked(True)
            dur = (end - now).total_seconds()
        else:
            self.start_time.setTime(QtCore.QTime(start.hour, start.minute, start.second))
            self.start_at.setChecked(True)
            dur = (end - start).total_seconds()
        self.dur_check.setChecked(True)
        self.dur_spin.setValue(max(1.0, round(dur, 1)))
        star = fav.get("star") or {}
        alt = (f", star alt {star['alt']:.0f} az {star['az']:.0f}"
               if star.get("alt") is not None and star.get("az") is not None else "")
        past = " (over: test recording from now)" if plan["event"] + timedelta(
            seconds=plan["trail_s"]) <= now else ""
        self.event_info.setText(f"event {plan['event'].strftime('%H:%M:%S.%f')[:12]} UTC{past}: "
                                f"{favorites.describe(fav)}{alt}")
        print(f"Favorite {fav.get('key')}: recording {self.start_time.time().toString('HH:mm:ss')}"
              f" UTC for {self.dur_spin.value():.1f} s")

    def review_last(self):
        path = self.c.recorder.last_path
        cmd = player_command(path) if path else None
        if cmd is None:
            self.last_saved = ("the player needs the full package: run "
                               "'uv run pystarlight player FILE'")
            return
        print("Player: " + " ".join(cmd))
        subprocess.Popen(cmd, start_new_session=True)       # own process: capture goes on

    def browse_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder",
                                                       self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    # ---- station and equipment ([observer] in the private config) ----
    def station(self):
        """[observer] as it goes into the header (command line over name / telescope)."""
        return station_header.effective(localconfig.section("observer"), self.args.observer,
                                        self.args.telescope)

    def station_missing(self):
        return station_header.missing(self.station(), need_site=self.c.gps is None)

    def show_station(self):
        miss = self.station_missing()
        line = station_header.summary(self.station()) or "no station data yet"
        self.station_info.setText(line + (f"\nmissing: {', '.join(miss)}" if miss else ""))
        self.station_info.setStyleSheet("color: #c0392b" if miss else "")

    def edit_station(self, why=""):
        """The station dialog; Save writes [observer] into the config. True when saved."""
        observer = station_header.migrate(localconfig.section("observer"))   # cm keys of earlier versions
        if station_header.unconfigured(observer):   # the first time: the PySyzygos site as defaults
            site = default_site()
            filled = {k: v for k, v in station_header.site_defaults(site).items() if not observer.get(k)}
            if filled:
                observer.update(filled)
                why = (why + "\n" if why else "") + (
                    f"First time: filled from the PySyzygos site '{site.get('id')}' "
                    f"({', '.join(station_header.LABELS[k] for k in filled)}): check, then Save.")
        for key, old in (("name", "observer"), ("telescope", "telescope")):   # typed in earlier versions
            if not observer.get(key):
                observer[key] = self.settings.value(old, "", type=str)
        path = localconfig.config_path() or localconfig.user_config_path()
        dlg = StationDialog(self, observer, path, need_site=self.c.gps is None, why=why,
                            pixel_um=self.c.pixel_um)
        if not (dlg.exec() if hasattr(dlg, "exec") else dlg.exec_()):
            return False
        try:
            localconfig.update_section("observer", dlg.values(), path,
                                       drop=[cm for _, cm in station_header.LEGACY_CM])
        except Exception as e:                  # e.g. not writable
            QtWidgets.QMessageBox.warning(self, "Station & equipment", f"could not save {path}: {e}")
            return False
        try:                                    # one focal length: the stack's (FOCALLEN, solve scale) too
            focal = float(dlg.values().get("focal_length_mm") or 0)
        except (TypeError, ValueError):
            focal = 0.0
        if focal > 0 and abs(focal - self.focal_spin.value()) > 0.05:
            print(f"focal length {self.focal_spin.value():g} -> {focal:g} mm (Station & equipment)")
            self.focal_spin.setValue(focal)     # on_focal: c.focal_mm, the setting, the scale label
        self.show_station()
        return True

    def now_ns(self):
        """UTC now (ns): GPS UTC while the GPS clock is anchored, else the Pi clock."""
        return utc_now_ns(self.c.gps.clock if self.c.gps is not None else None)

    def now_utc(self):
        return datetime.fromtimestamp(self.now_ns() / 1e9, timezone.utc)

    def set_next_minute(self):
        t = self.now_utc() + timedelta(seconds=10)
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.start_time.setTime(QtCore.QTime(t.hour, t.minute, 0))

    def on_record_button(self):
        rec = self.c.recorder
        if self.pending_rec is not None:        # cancel while anchoring
            self.pending_rec = None
            return
        if self.wait_fix:                       # cancel while waiting for the GPS fix
            self.wait_fix = False
            return
        if rec.state != "idle":
            rec.stop()
            return
        miss = self.station_missing()
        if miss:                                # no recording without the station data
            self.rec_info.setText("not armed: station data incomplete (" + ", ".join(miss) + ")")
            if not self.edit_station(why="Fill in the station data before recording. Missing: "
                                     + ", ".join(miss)) or self.station_missing():
                return
        gps = self.c.gps
        if gps is not None and not gps.has_fix():   # the recording's position is this GPS fix
            self.wait_fix = True
            print("Record: waiting for a GPS fix before arming")
            self.on_status_timer()
            return
        rec.out_dir = os.path.expanduser(self.dir_edit.text().strip() or self.args.record_dir)
        asteroid = self.asteroid_edit.text().strip()
        # observer / telescope come from the config ([observer], station_header); the command line overrides
        self.update_body(force=True)
        star = self.target_edit.text().strip()
        looked_up = bool(star) and self.target_found is not None and self.target_found[0] == star
        ra, dec = self.target_radec if looked_up and self.target_radec else (None, None)
        rec.text.update(observer=self.args.observer, telescope=self.args.telescope,
                        objname=asteroid or self.target_name(), target_star=self.target_name(),
                        target_ra=ra, target_dec=dec)   # TARGET-*: the analysis places its aperture
        rec.tag = self.file_tag()
        self.settings.setValue("asteroid", asteroid)
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
            begin = self.now_ns() if start_ns is None else start_ns - REANCHOR_LEAD_NS
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
            if self.now_ns() >= p["begin_ns"]:
                p["anchoring"] = True
                self.reanchor()
        elif self.timing_ready():
            self.pending_rec = None
            start_ns = p["args"][0]
            if start_ns is not None and start_ns < self.now_ns():
                print("GPS anchoring took past the start time: recording starts now")
            self.c.recorder.arm(*p["args"])

    # ---- timers ----
    def on_frame_timer(self):
        c = self.c
        if not c.stream_lock.acquire(blocking=False):
            return                              # the stream is being replaced (new ROI)
        new_frame = False
        try:
            item = c.grabber.take()
            if item is not None:
                buf, self.frame_id, frame_exp_ns = item
                try:
                    if c.recorder.state == "recording":
                        if c.recorder.latest is not None:     # already unpacked by the recorder
                            self.img, c.recorder.latest = c.recorder.latest, None
                    else:
                        self.img = c.decode(buf)    # copies the data out of the buffer
                finally:
                    c.stream.push_buffer(buf)
                self.redraw = new_frame = True
                if self.photo is not None:
                    self.check_photo(frame_exp_ns)
        finally:
            c.stream_lock.release()
        if new_frame and self.img is not None and not self.stack_show.isChecked():
            if self.levels_pending:             # new depth: levels for the new value range
                self.levels_pending = False
                if self.levels.auto:
                    self.levels.auto_levels()
                else:
                    self.levels.full_levels()
            else:
                self.levels.new_image()         # per-frame auto levels
        img, n, scale = self.img, c.decode.max_value + 1, 1.0
        if self.stack_show.isChecked():
            st = c.stacker
            if st.version != self.stack_seen:
                self.stack_seen = st.version
                self.stack_mean = st.preview()
                self.stack_img = (None if self.stack_mean is None else
                                  np.clip(self.stack_mean * (STACK_FULL / c.decode.max_value) + 0.5,
                                          0, STACK_FULL).astype(np.uint16))
                self.redraw = True
                if self.levels.auto:
                    self.levels.auto_levels()
            if self.stack_img is not None:
                img, n, scale = self.stack_img, STACK_FULL + 1, STACK_FULL / c.decode.max_value
        if new_frame and self.marks and self.marks_check.isChecked() and not self.stack_show.isChecked():
            self.track_marks(self.img)
        if img is None or not self.redraw:
            return
        self.redraw = False
        self.place_marks()
        # Order: debayer (needs full res) -> crop + scale to the view -> levels + gamma LUT
        frame = cv2.cvtColor(img, c.decode.bayer_code) if c.decode.bayer_code is not None else img
        self.view.render(frame, self.display_lut(n, scale))
        self.disp_n += 1

    def on_status_timer(self):
        c = self.c
        now = time.monotonic()
        self.update_body()
        self.focal_from_solution()
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
            regs = getattr(c.camera, "regs", None)          # VEYE: what the sensor really runs
            if regs is not None:
                try:
                    from .veye_mvcam import REG_TRIGGER_MODE
                    print(f"STALL: VEYE trigger mode register = {regs.read(REG_TRIGGER_MODE)} (0 = free run)")
                except Exception as e:
                    print(f"STALL: trigger register not read ({e})")
            # free run again from scratch (trigger cleared, stream restarted); still nothing at the
            # next stall: the VEYE firmware hang that only a camera restart clears (seen on an
            # IMX265M back from GPS mode): reboot the camera and set it up again
            done = c.stream.get_statistics()[0] if c.stream is not None else 0
            stuck = getattr(self, "stall_frames", None) == done and c.is_veye
            self.stall_frames = done
            if stuck:
                print("STALL again: rebooting the camera (firmware hang)")
                c.controller.request("recover", True)
            else:
                c.controller.request("trigger", "free")

        dt = now - self.disp_t0
        if dt >= 1.0:
            self.disp_fps, self.disp_t0, self.disp_n = self.disp_n / dt, now, 0

        if c.controller.reconfigs != self.roi_seen:
            self.roi_seen = c.controller.reconfigs
            self.after_reconfigure()
        self.check_camera_settings(now)
        if c.controller.restarts != getattr(self, "restarts_seen", 0):
            # a camera reboot (VEYE hang) starts from its defaults and was set to the values read
            # back last, which may already be those: the exposure and gain asked for, again
            self.restarts_seen = c.controller.restarts
            print(f"camera restarted: exposure {self.target['exposure'] / 1000:g} ms, "
                  f"gain {self.target['gain']:g} dB again")
            c.controller.request("exposure", self.target["exposure"])
            c.controller.request("gain", self.target["gain"])

        # exposure changed -> new frame period -> new pulse-to-frame lag
        stamper = c.grabber.stamper
        if stamper is not None and c.controller.exposure_us != self.last_exposure:
            self.last_exposure = c.controller.exposure_us
            stamper.lag_ns = self.predicted_lag_ns()
        self.follow_pending_record()
        if c.sysclock is not None:
            self.gps_labels["sysclock"].setText(
                c.sysclock.check(c.gps.clock if c.gps is not None else None,
                                 busy=rec.state == "recording"))
        if self.wait_fix and c.gps is not None and c.gps.has_fix():
            self.wait_fix = False
            print("GPS fix: arming the recording")
            self.on_record_button()
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
        self.show_solving()

        # ---- trigger ----
        busy = rec.state != "idle" or self.pending_rec is not None or self.wait_fix
        self.gps_check.setEnabled(not busy and c.can_trigger)
        self.rate_combo.setEnabled(not busy and mode == "gps" and not self.gps_busy)
        self.follow_y_trigger(busy)
        ytrig = self.view.hline is not None
        self.delay_spin.setEnabled(not busy and not ytrig and c.can_trigger)
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
            overlay = ("■  STOP", "#ff5252", True)
            secs = (rec.last_ns - rec.first_ns) / 1e9 if rec.first_ns else 0.0
            text = (f"<b>Recording</b> {rec.frames} frames, {secs:.1f} s"
                    + (f" <b style='color:#d33'>DROPPED {rec.dropped}</b>" if rec.dropped else "")
                    + f"<br>{os.path.basename(rec.path or '')}")
            if rec.stop_ns:
                text += f"<br>stops at {utc_str(rec.stop_ns, 0)} UTC"
        elif self.wait_fix:
            self.rec_button.setText("Cancel  (r)")
            self.rec_button.setStyleSheet("background: #ef6c00; color: white; font-weight: bold;")
            overlay = ("Cancel (fix)", "#ffa726", True)
            gps = c.gps
            text = ("<b>Waiting for a GPS fix</b> before arming: the recording's position is the "
                    f"GPS fix at its start ({gps.sats_used if gps else 0} satellites used)")
        elif self.pending_rec is not None:
            self.rec_button.setText("Cancel  (r)")
            self.rec_button.setStyleSheet("background: #ef6c00; color: white; font-weight: bold;")
            overlay = ("Cancel (armed)", "#ffa726", True)
            p = self.pending_rec
            if p["anchoring"]:
                text = (f"<b>Anchoring to GPS UTC</b> (1 Hz), then {rate_str(self.selected_rate)}"
                        f"; recording starts when the stamps are ready")
            else:
                text = (f"<b>Armed</b>: anchoring to GPS UTC at {utc_str(p['begin_ns'], 0)} UTC "
                        f"(in {(p['begin_ns'] - self.now_ns()) / 1e9:.0f} s)")
        elif rec.state == "armed":
            self.rec_button.setText("Cancel  (r)")
            self.rec_button.setStyleSheet("background: #ef6c00; color: white; font-weight: bold;")
            overlay = ("Cancel (armed)", "#ffa726", True)
            wait = (rec.start_ns - self.now_ns()) / 1e9
            text = (f"<b>Armed</b>: first frame at {utc_str(rec.start_ns, 0)} UTC "
                    f"(in {wait:.0f} s)")
        else:
            self.rec_button.setText("●  Record  (r)")
            self.rec_button.setStyleSheet("font-weight: bold;")
            overlay = ("●  REC", "#ff5252", False)
            if rec.last_path:
                self.last_saved = (f"Saved {rec.last_frames} frames: "
                                   f"{os.path.basename(rec.last_path)}"
                                   + (f" ({rec.last_dropped} dropped!)" if rec.last_dropped else ""))
            text = self.last_saved
        self.rec_info.setText(text)
        gps = c.gps
        source = ("GPS clock" if gps is not None and gps.clock.anchored else "Pi clock")
        ms = self.photo_spin.value()
        if self.photo is not None:
            left = self.photo_left()
            self.photo_button.setText("Cancel photo" + left)
            self.rec_overlay.show_photo("Cancel photo" + left, "#ffa726", True,
                                        "waiting for the photo frame")
        else:
            exp = f"{ms / 1000:g} s" if ms >= 1000 else f"{ms:g} ms"
            self.rec_overlay.show_photo(f"PHOTO {exp}", "#64b5f6", False,
                                        "one frame at the photo exposure, saved as FITS (exposure: "
                                        "Stack & plate solve, Photo)")
        self.rec_overlay.show_state(*overlay, utc_str(self.now_ns(), 0), source)

        # ---- status bar ----
        g = c.grabber
        self.status.setText(
            f"exp {c.controller.exposure_us / 1000:.3f} ms  gain {c.controller.gain_db:.1f} dB  "
            f"cam {g.cam_fps:.1f} fps  view {self.disp_fps:.1f} fps  lost {g.dropped}  "
            f"bad {sum(g.bad.values())}  restarts {c.controller.restarts}  "
            + (f"SLIPS {g.slip_events} ({g.slipped} fr)  "      # slipguard (temporary)
               if g.slipguard_factory is not None else "")
            + f"frame {self.frame_id}{trig}" + self.cursor_text())

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
        # the command line wins; else the last focal length set (remembered across runs)
        self.c.focal_mm = self.c.focal_mm or self.settings.value("focal_mm", 0.0, type=float)
        self.focal_spin.setValue(self.c.focal_mm)
        self.focal_spin.setSpecialValueText("unknown (blind solve)")
        self.focal_spin.valueChanged.connect(self.on_focal)
        self.scale_label = QtWidgets.QLabel()
        self.solve_check = QtWidgets.QCheckBox("Plate-solve when done", checked=True)
        self.stars_spin = QtWidgets.QSpinBox(minimum=0, maximum=30, prefix="name the ",
                                             suffix=" brightest stars")
        self.stars_spin.setValue(5)
        self.simbad_check = QtWidgets.QCheckBox(
            "SIMBAD names", checked=self.settings.value("simbad", True, type=bool))
        self.simbad_check.setToolTip("name the stars of the solution in SIMBAD (CDS) too; the "
                                     "markers' UCAC4 / TYC / DR3 names do not need it")
        self.simbad_check.toggled.connect(lambda on: self.settings.setValue("simbad", on))
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
        self.target_edit = QtWidgets.QLineEdit(self.settings.value("target", "", type=str))
        self.target_edit.setPlaceholderText("Gaia DR3 id, UCAC4 zzz-nnnnnn, RA Dec, Polaris, Moon...")
        self.target_edit.setToolTip("occultation target, marked on the view after a plate solve "
                                    "(looked up online at GAVO / ESA); the Moon or a planet: its "
                                    "position for this site now (offline), also the Mount panel's "
                                    "From target")
        self.target_edit.editingFinished.connect(self.on_target)
        self.verify_edit = QtWidgets.QLineEdit(self.settings.value("verify", "", type=str))
        self.verify_edit.setPlaceholderText("another ID of the target, to check (DR3 / UCAC4 / TYC)")
        self.verify_edit.setToolTip("e.g. the prediction's UCAC4 or TYC number: marked too, and "
                                    "the panel says whether it is the same star as Target")
        self.verify_edit.editingFinished.connect(self.on_target)
        self.marks_spin = QtWidgets.QSpinBox(minimum=0, maximum=10, prefix="mark ",
                                             suffix=" brightest")
        self.marks_spin.setValue(self.settings.value("mark_stars", 3, type=int))
        self.marks_spin.valueChanged.connect(self.on_target)
        self.marks_check = QtWidgets.QCheckBox("Markers  (m)", checked=True)
        self.marks_check.setToolTip("ticks and catalogue names (UCAC4, else TYC, else Gaia DR3) "
                                    "for the target and the brightest stars; they follow the "
                                    "field's drift on the live frames")
        self.marks_check.toggled.connect(lambda _: self.on_view_changed())
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
        g.addWidget(QtWidgets.QLabel("Target"), 4, 0)
        g.addWidget(self.target_edit, 4, 1, 1, 2)
        g.addWidget(QtWidgets.QLabel("Verify"), 5, 0)
        g.addWidget(self.verify_edit, 5, 1, 1, 2)
        g.addWidget(self.marks_spin, 6, 1)
        g.addWidget(self.marks_check, 6, 2)
        buttons = QtWidgets.QHBoxLayout()
        for w in (self.stack_button, self.solve_button, self.stack_show):
            buttons.addWidget(w)
        g.addLayout(buttons, 7, 0, 1, 3)
        self.live_button = QtWidgets.QPushButton("▶  Continue live view  (c)")
        self.live_button.setMinimumHeight(32)
        self.live_button.setStyleSheet("font-weight: bold;")
        self.live_button.setToolTip("back to the live frames; a running stack goes on, the "
                                    "stack and its solution stay ('Show stack' brings them back)")
        self.live_button.clicked.connect(self.continue_live)
        self.live_button.setVisible(False)
        g.addWidget(self.live_button, 8, 0, 1, 3)
        self.photo = None                   # a photo being taken: its exposure, the old one
        self.photo_msg = ""
        self.photo_spin = QtWidgets.QDoubleSpinBox(decimals=3, suffix=" ms", keyboardTracking=False)
        # longer than the camera's longest exposure (VEYE: 10 s): frames of at most that, summed
        self.photo_spin.setRange(self.c.exp_min / 1000, max(self.c.exp_max, PHOTO_MAX_US) / 1000)
        self.photo_spin.setValue(self.settings.value("photo_exposure_ms", 1000.0, type=float))
        self.photo_spin.setToolTip("exposure of a single photo (the live exposure comes back after it)")
        self.photo_spin.valueChanged.connect(lambda ms: self.settings.setValue("photo_exposure_ms", ms))
        self.photo_button = QtWidgets.QPushButton("Photo")
        self.photo_button.setToolTip("one frame at the photo exposure, saved as FITS next to the "
                                     "recordings, with the plate solution's WCS, centre, target "
                                     "and named stars when the field is solved")
        self.photo_button.clicked.connect(self.on_photo)
        g.addWidget(QtWidgets.QLabel("Photo"), 9, 0)
        g.addWidget(self.photo_spin, 9, 1)
        g.addWidget(self.photo_button, 9, 2)
        g.addWidget(self.stack_info, 10, 0, 1, 3)
        self.on_focal(self.c.focal_mm)
        return box

    # ---- single photo ----
    def on_photo(self):
        """Photo button: the photo exposure, the first frame fully exposed with it is kept, the
        live exposure back; pressed again while waiting: cancelled. Longer than the camera's
        longest exposure: n equal frames of at most that, summed (EXPTIME the total)."""
        c = self.c
        if self.photo is not None:
            self.end_photo("photo cancelled")
            return
        if c.recorder.state != "idle" or self.pending_rec is not None:
            self.photo_msg = "no photo while recording (it would change the recording's exposure)"
            return
        total = self.photo_spin.value() * 1000
        n = max(1, math.ceil(total / c.exp_max - 1e-9))
        us = total / n
        if self.mode() == "gps":
            longest = max_exposure_us(float(self.gps_rate()), c.trigger_delay_ns(),
                                      c.timing if c.is_veye else None)
            if longest is not None and us > longest:
                self.photo_msg = (f"{us / 1000:.0f} ms is longer than GPS mode at "
                                  f"{rate_str(self.gps_rate())} allows ({longest / 1000:.0f} ms): "
                                  "free run for a long photo")
                return
        self.photo = {"us": us, "n": n, "sum": None, "count": 0, "first_ns": None,
                      "old": self.target["exposure"], "applied": None, "since": time.monotonic()}
        self.photo_msg = (f"photo: {total / 1000:g} ms ..." if n == 1 else
                          f"photo: {n} x {us / 1e6:g} s = {total / 1e6:g} s, summed ...")
        self.photo_button.setText("Cancel photo")
        self.set_exposure(us)

    def check_photo(self, exp_ns):
        """A new frame while a photo is wanted: the first one whose exposure started after the
        camera took the photo exposure is the photo."""
        p, c = self.photo, self.c
        if p["applied"] is None:
            if not c.controller.busy and abs(c.controller.exposure_us - p["us"]) <= max(5, 0.02 * p["us"]):
                p["applied"] = self.now_ns()
                # the countdown: at worst the frame under way, then the photo frame(s)
                p["end"] = time.monotonic() + (1 + p["n"]) * p["us"] / 1e6
            return
        if exp_ns is None or exp_ns < p["applied"] + 50_000_000 or self.img is None:
            p["end"] = time.monotonic() + p["n"] * p["us"] / 1e6   # not this one: the photo starts now
            return
        img = self.img.astype(np.float64)
        p["sum"] = img if p["sum"] is None else p["sum"] + img
        p["count"] += 1
        if p["first_ns"] is None:
            p["first_ns"] = exp_ns
        if p["count"] < p["n"]:
            p["end"] = time.monotonic() + (p["n"] - p["count"]) * p["us"] / 1e6
            return
        img, n, first = p["sum"], p["n"], p["first_ns"]
        exposure_s = n * c.controller.exposure_us / 1e6
        self.end_photo("")
        threading.Thread(target=self.save_photo, args=(img, first, exposure_s, n), daemon=True).start()

    def photo_left(self):
        """The photo button's countdown for a long photo (> 1 s): ' 23 s' until the photo frame is
        read, '' for short ones."""
        p = self.photo
        if p is None or p["us"] * p.get("n", 1) <= 1e6:
            return ""
        if p.get("end") is None:
            return " (setting)"
        left = p["end"] - time.monotonic()
        return f" {math.ceil(left):.0f} s" if left > 0 else " (reading)"

    def end_photo(self, msg):
        p, self.photo = self.photo, None
        if p is not None:
            self.set_exposure(p["old"])
        self.photo_button.setText("Photo")
        if msg:
            self.photo_msg = msg

    def save_photo(self, img, exp_ns, exposure_s, n=1):
        """FITS with the camera's header values and, once solved, the WCS moved onto this frame;
        n > 1: the sum of n frames (no alignment: the mount tracks)."""
        c = self.c
        try:
            info = dict(c.stacker.header_info())
            info.update(width=img.shape[1], height=img.shape[0])
            rx, ry = c.region[:2]
            wcs, stars = [], []
            if self.solution is not None and self.solution.get("wcs"):
                dx, dy = self.marks_shift
                shift = (self.marks_roi[0] - rx + dx, self.marks_roi[1] - ry + dy)
                wcs = photo.wcs_cards(self.solution["wcs"], shift)
                for m in self.marks:
                    if m.get("ra") is not None:
                        stars.append((m.get("name") or "?", m["ra"], m["dec"],
                                      m["x"] - rx + dx, m["y"] - ry + dy))
            name = self.target_name()
            target = ((name, *self.target_radec) if self.target_radec else (name, None, None)) \
                if name else None
            cards = photo.header(info, exp_ns, exposure_s, wcs, stars, target, CAMERA_VERSION)
            if n > 1:
                cards.insert(4, ("NCOMBINE", int(n), "frames summed"))
            path = photo.save(c.stacker.out_dir, c.stacker.name, img, cards, exp_ns)
            self.photo_msg = (f"photo saved: {os.path.basename(path)}"
                              + (" (with WCS)" if wcs else " (no plate solution: no WCS)"))
            print(self.photo_msg)
        except Exception as e:
            self.photo_msg = f"photo not saved: {e}"
            print(self.photo_msg)

    def on_focal(self, mm):
        c = self.c
        c.focal_mm = mm
        self.settings.setValue("focal_mm", float(mm))
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
            if self.levels.auto:                # back to the frame's levels
                self.levels.auto_levels()
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
        center = self.solve_hint()
        t0 = time.monotonic()

        def log(msg):
            self.solve_log = f"[{time.monotonic() - t0:.0f} s] {msg}"
            print(f"solve: {self.solve_log}")

        def work():
            try:
                sol = solve_fits(path, n, use_simbad=simbad, log=log, cancel=cancel,
                                 center=center, radius_deg=SOLVE_RADIUS_DEG)
                c = sol["center"]
                self.settings.setValue("last_center", [c["ra"], c["dec"]])
                print(f"solve: done in {time.monotonic() - t0:.1f} s")
                self.view.compass = self.compass_of(sol)     # N-E arrows on the image
                self.solution = sol
                self.solve_count += 1
                self.solved_path = path
                self.solve_log = ""
            except Exception as e:
                self.solve_log = f"plate solve failed: {e}"
                print(self.solve_log)
            self.solve_thread = None

        self.solve_cancel = cancel
        self.solve_log = "plate solving..."
        self.solve_started = time.monotonic()
        self.solve_thread = threading.Thread(target=work, daemon=True)
        self.solve_thread.start()

    def file_tag(self):
        """The middle of the file names: <camera>_<asteroid>_<star>_<UTC>Z (recordings, and the
        DSLR's photos: dslr_gui)."""
        return "_".join(t for t in (file_tag(self.asteroid_edit.text().strip()), file_tag(self.target_name()))
                        if t)

    def target_name(self):
        """The target for file names and headers: its catalogue name once looked up (UCAC4,
        else TYC, else Gaia DR3), else as typed."""
        text = self.target_edit.text().strip()
        if self.target_found and self.target_found[0] == text:
            return self.target_found[1]
        try:
            return bodies.body_name(text) or text   # 'jupiter' -> 'Jupiter'
        except ValueError:
            return text

    def site_now(self):
        """(lat, lon, alt_m) from the GPS fix, else the config's [observer] site; None."""
        meta = station_header.site_meta(self.c.gps, localconfig.section("observer"))
        if meta.get("latitude") is None or meta.get("longitude") is None:
            return None
        return meta["latitude"], meta["longitude"], meta.get("altitude") or 0.0

    def update_body(self, force=False):
        """The Moon or a planet as the target: its position for the site now (every 10 s, the
        Moon moves 5" in that time), as target_radec. False if the target is none of them."""
        text = self.target_edit.text().strip()
        if not bodies.is_body(text):
            return False
        if not force and time.monotonic() - self.body_at < 10.0:
            return True
        self.body_at = time.monotonic()
        try:
            name, ra, dec = bodies.position(text, self.site_now())
        except Exception as e:
            self.target_radec, self.target_found = None, None
            self.marks_log = f"target: {e}"
            return True
        self.target_radec, self.target_found = (ra, dec), (text, name)
        return True

    def check_camera_settings(self, now):
        """The camera lost the gain / exposure asked for (seen after a reboot: 0 dB, 5 ms, its
        defaults, when the writes came before it was ready): send them again, every 3 s, up to 5
        times. Only gross misses count for the exposure (half of it), as a GPS rate may clamp it."""
        c, t = self.c, self.target
        ctl = c.controller
        if ctl.busy or self.photo is not None:
            self.settings_since = None
            return
        lost = abs(ctl.gain_db - t["gain"]) > 0.3 or ctl.exposure_us < 0.5 * t["exposure"]
        if not lost:
            self.settings_since, self.settings_tries = None, 0
            return
        if getattr(self, "settings_since", None) is None:
            self.settings_since = now
            return
        tries = getattr(self, "settings_tries", 0)
        if now - self.settings_since < 3.0 or tries >= 5:
            return
        self.settings_since, self.settings_tries = now, tries + 1
        print(f"camera runs gain {ctl.gain_db:g} dB, exposure {ctl.exposure_us / 1000:g} ms, not "
              f"{t['gain']:g} dB, {t['exposure'] / 1000:g} ms: set again ({tries + 1}/5)")
        ctl.request("exposure", t["exposure"])
        ctl.request("gain", t["gain"])

    def update_star_name(self):
        """A star's proper name as the target (Polaris, Atlas, alf UMi): its position now from the
        offline table at once (Mount panel: From target), then, in the background, the
        catalogue star there (UCAC4, else TYC, else Gaia DR3) for the file names and headers.
        False if the target is no name in the table."""
        text = self.target_edit.text().strip()
        star = starnames.lookup(text)
        if star is None:
            return False
        if self.target_found and self.target_found[0] == text:
            return True                         # looked up already
        self.target_radec, self.target_found = starnames.position(star), (text, star["name"])

        def work():
            try:
                from pystarlight.wcs.catnames import resolve_target
                t = resolve_target(text, log=lambda m: None)
            except Exception as e:
                print(f"target {text}: no catalogue name ({e})")
                return
            if self.target_edit.text().strip() == text:
                self.target_radec, self.target_found = (t["ra"], t["dec"]), (text, t["name"])
                print(f"target {text} = {t['name']} (" + ", ".join(f"{k} {v}" for k, v in t["ids"].items()
                                                         if v != t["name"]) + ")")
        threading.Thread(target=work, daemon=True).start()
        return True

    def focal_from_solution(self):
        """A new plate solution: the Focal length box from its scale (206.265 x pixel um /
        arcsec per px) when that differs by more than 0.5 %, so the next solves have the right
        scale hint (another telescope, a reducer, a focuser that moved the focal plane)."""
        sol = self.solution
        if sol is None or sol is getattr(self, "focal_sol", None):
            return
        self.focal_sol = sol
        pix, scale = self.c.pixel_um, sol.get("scale_arcsec")
        if not pix or not scale:
            return
        focal = round(206.265 * float(pix) / float(scale), 1)
        old = self.focal_spin.value()
        if not old or abs(focal / old - 1) > 0.005:
            print(f"focal length {old:g} -> {focal:g} mm (plate solution, {scale:.4f}\"/px)")
            self.focal_spin.setValue(focal)     # on_focal: c.focal_mm, the setting, the scale label
            self.solve_log = f"focal length set to {focal:g} mm from the plate solution (was {old:g})"

    def solve_hint(self):
        """Where the field probably is: where the mount points (Mount panel connected), else the
        last solution's centre (this run or the last one), else the target's position (from the
        markers). None: search the whole sky."""
        dock = getattr(self, "mount_dock", None)
        if dock is not None and dock.mount is not None and dock.client is not None and dock.client.connected:
            try:
                # parked: the AM5 keeps reporting where it was, wherever the scope points
                pos = None if dock.mount.parked() else dock.mount.position()
                if pos is not None:
                    return pos
            except Exception:
                pass
        if self.solution is not None:
            c = self.solution["center"]
            return c["ra"], c["dec"]
        last = self.settings.value("last_center")
        if last:
            try:
                return float(last[0]), float(last[1])
            except (TypeError, ValueError, IndexError):
                pass
        return self.target_radec

    def show_solution_marks(self):
        sol = self.solution
        if not sol or not self.stack_show.isChecked():
            return
        color = QtGui.QColor("#00e676")
        self.view.circles = [(s["x"], s["y"], max(4.0, 2 * (s.get("fwhm") or 2)),
                              f"{i} {s.get('name') or ''}".strip(), color)
                             for i, s in enumerate(sol.get("stars", []), 1)]
        self.view.update()

    # ---- markers: target and brightest stars with catalogue names ----
    def on_target(self):
        self.settings.setValue("target", self.target_edit.text().strip())
        if not self.update_body(force=True):
            self.update_star_name()
        self.settings.setValue("verify", self.verify_edit.text().strip())
        self.settings.setValue("mark_stars", self.marks_spin.value())
        self.marks_key = None                   # made again (update_stack_box starts it)

    def start_marks(self, sol, key):
        """Name the brightest stars and find the target (worker thread: VO look-ups)."""
        text, n, check = key[1], key[2], key[3]

        def log(msg):
            self.marks_log = msg
            print(f"markers: {msg}")

        def work():
            marks, notes = [], []
            try:
                from astropy.io import fits

                from .marks import solution_marks
                try:
                    hdr = fits.getheader(sol["stack"])
                    roi = (int(hdr.get("ROIX", 0)), int(hdr.get("ROIY", 0)))
                except Exception:
                    roi = (0, 0)
                target = text
                if bodies.is_body(text):            # the Moon / a planet when the stack was taken
                    when = None
                    if sol.get("time"):
                        when = datetime.fromisoformat(sol["time"]).replace(tzinfo=timezone.utc)
                    name, ra, dec = bodies.position(text, self.site_now(), when)
                    target = {"name": name, "ra": ra, "dec": dec}
                marks, notes, found = solution_marks(sol, n, target, check, log=log, offset=roi)
                if "target" in found:
                    t = found["target"]
                    self.target_radec = (t["ra"], t["dec"])
                    self.target_found = (text, t["name"])
                for m in marks:                     # stack pixels -> sensor pixels
                    m["x"] += roi[0]
                    m["y"] += roi[1]
                self.marks_roi = roi
            except Exception as e:
                notes.append(f"markers: {e}")
            self.marks, self.marks_shift, self.marks_frame = marks, (0.0, 0.0), None
            self.marks_log = "\n".join(notes)
            print(f"markers: {self.marks_log}" if notes else f"markers: {len(marks)}")
            self.redraw = True
            self.marks_thread = None

        self.marks_key = key
        self.marks_log = "naming the stars..."
        self.marks_thread = threading.Thread(target=work, daemon=True)
        self.marks_thread.start()

    def track_marks(self, img):
        """Field drift of a live frame against the stack: the bright marked stars' centroids
        near where they were (the target is too faint to steer by)."""
        if self.frame_id == self.marks_frame or img is None or img.ndim != 2:
            return
        self.marks_frame = self.frame_id
        rx, ry = self.c.region[:2]
        H, W = img.shape
        half = 40                               # search box: drift between measurements
        found = []
        for m in self.marks:
            if m["kind"] != "star":
                continue
            px = m["x"] - rx + self.marks_shift[0]
            py = m["y"] - ry + self.marks_shift[1]
            x0, y0 = int(px) - half, int(py) - half
            if x0 < 0 or y0 < 0 or x0 + 2 * half >= W or y0 + 2 * half >= H:
                continue
            box = cv2.GaussianBlur(img[y0:y0 + 2 * half + 1, x0:x0 + 2 * half + 1].astype(np.float32),
                                   (5, 5), 1.5)
            sky = float(np.median(box))
            noise = 1.4826 * float(np.median(np.abs(box - sky))) + 1e-3
            j, i = np.unravel_index(int(np.argmax(box)), box.shape)
            if box[j, i] - sky < 10 * noise:
                continue
            r = max(4, int(m["r"]))
            ys, xs = slice(max(0, j - r), j + r + 1), slice(max(0, i - r), i + r + 1)
            w = np.clip(box[ys, xs] - sky - 3 * noise, 0, None)
            if w.sum() <= 0:
                continue
            yy, xx = np.mgrid[ys, xs]
            found.append((x0 + (xx * w).sum() / w.sum() - (m["x"] - rx),
                          y0 + (yy * w).sum() / w.sum() - (m["y"] - ry)))
        if found:
            self.marks_shift = tuple(float(v) for v in np.median(np.array(found), axis=0))

    def place_marks(self):
        """The markers in the shown image's pixels (the stack: as solved; live: drift followed)."""
        if not self.marks or not self.marks_check.isChecked():
            self.view.markers = []
            return
        if self.stack_show.isChecked():
            ox, oy, (dx, dy) = *self.marks_roi, (0.0, 0.0)
        else:
            ox, oy = self.c.region[:2]
            dx, dy = self.marks_shift
        colors = {"star": QtGui.QColor("#40c4ff"), "target": QtGui.QColor("#ff4081"),
                  "verify": QtGui.QColor("#ffd740")}
        self.view.markers = [(m["x"] - ox + dx, m["y"] - oy + dy, m["r"], m["label"],
                              colors[m["kind"]]) for m in self.marks]

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
        p = self.photo
        if p is not None and time.monotonic() - p["since"] > 4 * p["us"] / 1e6 + 10:
            self.end_photo("photo: no frame came at that exposure")
        if self.photo_msg:
            text += ("\n" if text else "") + self.photo_msg
        if self.solve_log:
            text += "\n" + self.solve_log
        sol = self.solution
        if sol is not None and self.solved_path == st.path:
            from pystarlight.wcs.solve_stack import describe
            text += ("\n" if text else "") + describe(sol)
            if not self.view.circles:
                self.show_solution_marks()
        if sol is not None and self.marks_thread is None:
            key = (self.solve_count, self.target_edit.text().strip(), self.marks_spin.value(),
                   self.verify_edit.text().strip())
            if key != self.marks_key:
                self.start_marks(sol, key)
        if self.marks_log:
            text += ("\n" if text else "") + self.marks_log
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

    # ---- telescope docks (telescope_gui): hidden until wanted, arrangement remembered ----
    def field_moving(self):
        """The Mount panel: a slew starts (goto, Center). The star lock waits."""
        if getattr(self, "lock_dock", None) is not None and self.lock_dock.lock.state != "idle":
            self.lock_dock.thread.ask("moving")

    def field_moved(self, shift):
        """The Mount panel: the slew has ended, the field moved by about `shift` (dx, dy) px
        (None: not known). The markers move by it (their tracking refines it), the star lock
        looks for its stars there and holds the new position."""
        if shift is not None:
            self.marks_shift = (self.marks_shift[0] + shift[0], self.marks_shift[1] + shift[1])
            self.marks_frame = None
            print(f"field moved by about {shift[0]:+.0f}, {shift[1]:+.0f} px (slew): markers follow")
        if getattr(self, "lock_dock", None) is not None and self.lock_dock.lock.state != "idle":
            self.lock_dock.thread.ask("moved", shift)

    def on_crosshair(self, on):
        self.settings.setValue("crosshair", on)
        self.view.crosshair = on
        self.view.update()

    @staticmethod
    def compass_of(sol):
        """[('N', ux, uy), ('E', ux, uy)]: north and east on the image as unit vectors (screen y
        down), from the solution's WCS at the field centre; None if it cannot be read."""
        try:
            from astropy.io import fits
            from astropy.wcs import WCS
            w = WCS(fits.getheader(sol["wcs"]))
            ra, dec = sol["center"]["ra"], sol["center"]["dec"]
            (x0,), (y0,) = w.wcs_world2pix([ra], [dec], 0)
            out = []
            for label, (r, d) in (("N", (ra, dec + 1 / 60)),
                                  ("E", (ra + 1 / 60 / np.cos(np.radians(dec)), dec))):
                (x,), (y,) = w.wcs_world2pix([r], [d], 0)
                n = float(np.hypot(x - x0, y - y0)) or 1.0
                out.append((label, float(x - x0) / n, float(y - y0) / n))
            return out
        except Exception:
            return None

    def show_solving(self):
        """The status bar's busy indicator while a plate solve runs (with its time so far)."""
        label = getattr(self, "solve_status", None)
        if label is None:
            return
        busy = self.solve_thread is not None
        text = f"plate solving {time.monotonic() - getattr(self, 'solve_started', time.monotonic()):.0f} s" \
            if busy else ""
        if text != label.text():
            label.setText(text)
            label.setStyleSheet("background: #ffb300; color: black; padding: 0 6px; border-radius: 3px;"
                                if busy else "")

    def make_view_menu(self):
        """View: Night mode (red, F9; remembered) and Full screen (F11, hides the title bar)."""
        menu = self.menuBar().addMenu("&View")
        self.night_action = menu.addAction("&Night mode (red)")
        self.night_action.setCheckable(True)
        self.night_action.setShortcut(QtGui.QKeySequence("F9"))
        self.night_action.toggled.connect(self.set_night)
        full = menu.addAction("&Full screen")
        full.setCheckable(True)
        full.setShortcut(QtGui.QKeySequence("F11"))
        full.toggled.connect(lambda on: self.showFullScreen() if on else self.showNormal())
        self.full_action = full

    def set_night(self, on):
        """Night mode on / off: the app's style sheet (the GUI) and the buttons on the image; the
        image and its annotations keep their colours."""
        self.settings.setValue("night", on)
        QtWidgets.QApplication.instance().setStyleSheet(NIGHT_QSS if on else "")
        self.rec_overlay.set_night(on)
        dock = getattr(self, "mount_dock", None)
        if dock is not None:
            dock.set_night(on)
        for name in ("focuser_window", "polar_window", "dslr_window"):
            window = getattr(self, name, None)
            if window is not None:
                window.set_night(on)
        self.redraw = True
        self.view.update()

    def make_telescope_docks(self):
        from .focuser_gui import FocuserWindow
        from .telescope_gui import MountDock
        self.solve_status = QtWidgets.QLabel()         # "plate solving 37 s": busy indicator
        self.solve_status.setToolTip("a plate solve is running (Stack & plate solve: Cancel solve)")
        self.statusBar().addPermanentWidget(self.solve_status)
        self.mount_status = QtWidgets.QLabel()
        self.mount_status.setToolTip("the mount (INDI); click to show the Mount panel")
        self.mount_status.mousePressEvent = lambda e: self.show_dock(self.mount_dock)
        self.statusBar().addPermanentWidget(self.mount_status)
        self.mount_dock = MountDock(self)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.mount_dock)
        self.mount_dock.hide()
        menu = self.menuBar().addMenu("&INDI Telescope Controls")
        act = self.mount_dock.toggleViewAction()
        act.setText("Mount &panel")
        act.setShortcut(QtGui.QKeySequence("Shift+M"))
        menu.addAction(act)
        self.focuser_window = FocuserWindow(self)     # a window of its own, on the same connection
        act = menu.addAction("&Focuser window")
        act.setShortcut(QtGui.QKeySequence("Shift+F"))
        self.focuser_window.bind_action(act)
        from .polar_gui import PolarWindow
        self.polar_window = PolarWindow(self)         # a window of its own, on the same connection
        act = menu.addAction("&Polar alignment window")
        act.setShortcut(QtGui.QKeySequence("Shift+P"))
        self.polar_window.bind_action(act)
        menu.addAction("INDI &settings…", self.mount_dock.show_settings)
        self.lock_dock = None
        if getattr(self.c, "locker", None) is not None:   # star lock (lock_gui.py, guider.py)
            from .lock_gui import LockDock
            self.lock_dock = LockDock(self)
            self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.lock_dock)
            self.lock_dock.hide()
            act = self.lock_dock.toggleViewAction()
            act.setText("Star &lock panel")
            act.setShortcut(QtGui.QKeySequence("Shift+L"))
            menu.addAction(act)
        state = self.settings.value("dock_state")
        if state is not None:
            self.restoreState(state)

    def make_dslr_menu(self):
        """DSLR: a second, photo-only camera (dslr_gui.py), beside the video camera."""
        from .dslr_gui import DslrWindow
        self.dslr_window = DslrWindow(self)
        menu = self.menuBar().addMenu("&DSLR")
        act = menu.addAction("DSLR &window")
        act.setShortcut(QtGui.QKeySequence("Shift+C"))
        self.dslr_window.bind_action(act)

    def show_dock(self, dock):
        dock.show()
        dock.raise_()

    def closeEvent(self, event):
        if self.solve_cancel is not None:
            self.solve_cancel.set()
        self.settings.setValue("dock_state", self.saveState())
        self.focuser_window.shutdown()
        self.focuser_window.close()
        self.polar_window.shutdown()
        self.polar_window.close()
        self.dslr_window.shutdown()
        self.dslr_window.close()
        self.mount_dock.shutdown()
        if self.lock_dock is not None:
            self.lock_dock.shutdown()
        self.settings.setValue("splitter", self.splitter.sizes())
        self.frame_timer.stop()
        self.status_timer.stop()
        event.accept()
