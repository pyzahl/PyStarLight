"""Review player: step through a recording (RAVF, SER, ADV/AAV, AVI, FITS folder), check its
time stamps and per-frame status, stack a frame range and plate-solve it.

    uv run pystarlight-review FILE        (or: python -m pystarlight.capture.review FILE)

The camera app's "Review last clip" button starts it on the file just recorded.

  - Image: the frame, levels auto (zscale, optionally per frame) or manual black / white
    point as in pystarlight-view, display gamma, zoom/pan as in the camera app.
  - Timing: UTC of every frame, the step to the previous one against the typical step, a plot
    of the steps over the whole file (click to jump; red: problems), dropped / duplicated /
    irregular frames (timestamps.timing.check_timing); RAVF also sequence-number gaps,
    exposure, GPS satellites, fix and almanac status per frame.
  - Stack & plate solve a frame range ([ and ] set it): frames aligned on the stars and
    averaged (capture.stacker), solved (wcs.solve_stack: local astrometry.net or nova),
    brightest stars named; optionally saved as the movie's plate solution for
    pystarlight-view (<movie>.wcs.fit, .wcs.json, .stars.json).

Keys: Space play/pause, Left/Right frame, PgUp/PgDn 10 frames, Home/End, n/b next/previous
problem, [ / ] stack range from/to here, a auto levels, s stack & solve, c back to the frames, q quit.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .cameras import pixel_um
from .gui import CollapsibleBox, ImageView, grid_box
from .qt import QShortcut, QtCore, QtGui, QtWidgets, Signal, event_pos, exec_app, fix_cv2_qt_env
from .ravf_writer import FRAME_HEAD, ravf_to_unix
from .stacker import LiveStacker

TICKS_UNIX_EPOCH = 621355968000000000      # .NET ticks at 1970-01-01T00:00:00Z (SER)
FIX_NAMES = {0: "no fix", 1: "time kept", 2: "time fix", 3: "time + position"}
DISPLAY_STEPS = 4095                       # black..white in this many steps before the gamma LUT


# ---------------------------------------------------------------------------
# Time stamps and per-frame status of a whole file
# ---------------------------------------------------------------------------
def frame_time_s(date, time_str):
    """Frame date/time strings -> Unix seconds (seconds of the day if there is no date)."""
    from ..timestamps.timing import parse_time
    sec = parse_time(time_str)
    if not np.isfinite(sec) or not date:
        return sec
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp() + sec


def ravf_headers(path, reader):
    """Per-frame RAVF header fields (all frames, without the image data)."""
    out = []
    idx = reader._r.index
    with open(path, "rb") as f:
        for i in range(idx.count()):
            f.seek(idx.item(i)[0])
            (_m, _hl, _dl, _type, start, exposure, sats, almanac, almanac_off, fix,
             seq) = FRAME_HEAD.unpack(f.read(FRAME_HEAD.size))
            out.append({"t": ravf_to_unix(start) / 1e9, "exposure_ms": exposure / 1e6,
                        "satellites": sats, "fix": fix, "almanac": almanac,
                        "almanac_offset": almanac_off, "sequence": seq})
    return out


def load_timing(path, progress=lambda done, total: None):
    """(times [s], per-frame info dicts) of every frame. RAVF and SER from their index /
    trailer (fast); other formats by reading each frame."""
    from ..movie import RavfReader, SerReader, open_movie
    with open_movie(path) as mv:
        n = len(mv)
        if isinstance(mv, RavfReader):
            info = ravf_headers(path, mv)
            return np.array([d["t"] for d in info]), info
        if isinstance(mv, SerReader) and len(mv.ticks) >= n:
            t = (np.asarray(mv.ticks[:n], dtype=np.int64) - TICKS_UNIX_EPOCH) / 1e7
            return t, [{} for _ in range(n)]
        times, info = np.full(n, np.nan), []
        for i in range(n):
            fr = mv.read(i)
            times[i] = frame_time_s(fr.date, fr.time)
            info.append(dict(fr.status or {}))
            if i % 50 == 0:
                progress(i, n)
        return times, info


def problems(times, info):
    """{frame: text} of the timing problems: check_timing on the stamps, RAVF sequence gaps."""
    from ..timestamps.timing import check_timing
    out = {}
    rep = check_timing(times) if np.isfinite(times).sum() >= 2 else None
    if rep is not None:
        for r, k in rep.dropped:
            out[r] = f"{k} frame(s) missing before (time step)"
        for r in rep.duplicates:
            out[r] = "same time as the previous frame"
        for r in rep.backwards:
            out[r] = "earlier than the previous frame"
        for r in rep.irregular:
            out[r] = "irregular step"
        for r in rep.unreadable:
            out[r] = "no time stamp"
    seq = [d.get("sequence") for d in info]
    if info and all(s is not None for s in seq):
        gaps = np.diff(np.asarray(seq, dtype=np.int64))
        for r in np.flatnonzero(gaps != 1):
            text = (f"sequence +{gaps[r]}: {gaps[r] - 1} frame(s) dropped" if gaps[r] > 1
                    else f"sequence step {gaps[r]}")
            out[int(r) + 1] = out.get(int(r) + 1, "") + ("; " if int(r) + 1 in out else "") + text
    return dict(sorted(out.items())), rep


def zscale(img):
    """IRAF zscale display limits (as pystarlight-view): faint stars without burning out the sky."""
    from astropy.visualization import ZScaleInterval
    return ZScaleInterval().get_limits(np.asarray(img, np.float32))


def utc_text(t):
    if not np.isfinite(t):
        return "-"
    if t < 86400 * 2:                                       # seconds of the day only
        return time.strftime("%H:%M:%S", time.gmtime(t)) + f".{t % 1:.6f}"[2:]
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# ---------------------------------------------------------------------------
# Step plot
# ---------------------------------------------------------------------------
class StepPlot(QtWidgets.QWidget):
    """Time step to the previous frame (ms) over the whole file; the current frame is a
    vertical line, problem frames are red ticks. Click / drag jumps there."""

    clicked = Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(90)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.dt = None              # ms per frame (nan for the first / unknown)
        self.flags = []
        self.current = 0
        self.step = None

    def set_data(self, times, flags, step_s):
        self.dt = np.concatenate([[np.nan], np.diff(times) * 1000.0]) if len(times) else None
        self.flags = list(flags)
        self.step = step_s * 1000 if step_s and np.isfinite(step_s) else None
        self.update()

    def set_current(self, i):
        self.current = i
        self.update()

    def _x(self, i, n):
        return 4 + (self.width() - 8) * i / max(1, n - 1)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        dt = self.dt
        if dt is None or not np.isfinite(dt).any():
            p.setPen(QtCore.Qt.gray)
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "time steps: loading..." if dt is None
                       else "no time stamps")
            p.end()
            return
        n, w, h = len(dt), self.width(), self.height()
        ok = dt[np.isfinite(dt)]
        lo, hi = np.percentile(ok, (0.5, 99.5)) if len(ok) > 10 else (ok.min(), ok.max())
        if self.step:
            lo, hi = min(lo, 0.5 * self.step), max(hi, 1.5 * self.step)
        span = max(hi - lo, 1e-6)

        def y(v):
            return h - 14 - (h - 22) * (min(max(v, lo), hi) - lo) / span

        if self.step:
            p.setPen(QtGui.QPen(QtGui.QColor("#555555"), 1, QtCore.Qt.DashLine))
            p.drawLine(QtCore.QPointF(4, y(self.step)), QtCore.QPointF(w - 4, y(self.step)))
        # min/max per pixel column
        cols = max(1, w - 8)
        edges = np.linspace(0, n, cols + 1).astype(int)
        p.setPen(QtGui.QColor("#4fc3f7"))
        for c in range(cols):
            seg = dt[edges[c]:max(edges[c + 1], edges[c] + 1)]
            seg = seg[np.isfinite(seg)]
            if len(seg):
                p.drawLine(QtCore.QPointF(4 + c, y(seg.min())), QtCore.QPointF(4 + c, y(seg.max())))
        p.setPen(QtGui.QColor("#ff5252"))
        for i in self.flags:
            x = self._x(i, n)
            p.drawLine(QtCore.QPointF(x, h - 10), QtCore.QPointF(x, h - 2))
        p.setPen(QtGui.QColor("#ffd600"))
        x = self._x(self.current, n)
        p.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, h))
        p.setPen(QtCore.Qt.lightGray)
        p.drawText(6, 12, f"step {hi:.1f} ms")
        p.drawText(6, h - 14, f"{lo:.1f} ms")
        p.end()

    def mousePressEvent(self, e):
        if self.dt is not None and len(self.dt):
            n = len(self.dt)
            i = round((event_pos(e).x() - 4) / max(1, self.width() - 8) * (n - 1))
            self.clicked.emit(int(min(max(i, 0), n - 1)))

    mouseMoveEvent = mousePressEvent


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
def label_grid(grid, rows, start=0):
    labels = {}
    mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    for r, (key, name) in enumerate(rows, start):
        grid.addWidget(QtWidgets.QLabel(name), r, 0)
        lab = QtWidgets.QLabel("-", wordWrap=True)
        lab.setFont(mono)
        lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        grid.addWidget(lab, r, 1)
        labels[key] = lab
    return labels


class ReviewWindow(QtWidgets.QMainWindow):
    timing_ready = Signal()

    def __init__(self, path=None, focal_mm=None, pixel_um_=None):
        super().__init__()
        self.settings = QtCore.QSettings("PyStarLight", "review")
        self.mv = None
        self.path = None
        self.index = 0
        self.img = None
        self.times = np.zeros(0)
        self.info = []
        self.flags = {}
        self.report = None
        self.timing_note = ""
        self.focal_arg, self.pixel_arg = focal_mm, pixel_um_
        # stack & solve
        self.stacker = None
        self.stack_thread = None
        self.cancel = None
        self.job_log = ""
        self.solution = None
        self.stack_view = None              # stretched stack while it is shown
        self.stack_seen = -1
        self.timing_ready.connect(self.on_timing_ready)

        self.view = ImageView()
        self.view.changed.connect(self.redraw)
        self.plot = StepPlot()
        self.plot.clicked.connect(self.goto)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.valueChanged.connect(self.goto)
        self.frame_spin = QtWidgets.QSpinBox(keyboardTracking=False)
        self.frame_spin.valueChanged.connect(self.goto)
        self.play_button = QtWidgets.QPushButton("▶ Play")
        self.play_button.clicked.connect(self.toggle_play)
        self.fps_spin = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0.5, maximum=100.0,
                                                 suffix=" fps", value=10.0)
        self.fps_spin.valueChanged.connect(lambda v: self.play_timer.setInterval(int(1000 / v)))
        prev_b = QtWidgets.QPushButton("◀ problem (b)")
        prev_b.clicked.connect(lambda: self.next_problem(-1))
        next_b = QtWidgets.QPushButton("problem (n) ▶")
        next_b.clicked.connect(lambda: self.next_problem(1))
        bar = QtWidgets.QHBoxLayout()
        for w in (self.play_button, self.fps_spin, self.slider, self.frame_spin, prev_b, next_b):
            bar.addWidget(w)
        bar.setStretch(2, 1)
        left = QtWidgets.QWidget()
        lcol = QtWidgets.QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 0, 0)
        lcol.addWidget(self.view, 1)
        lcol.addLayout(bar)
        lcol.addWidget(self.plot)

        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.addWidget(self.make_frame_box())
        col.addWidget(self.make_display_box())
        col.addWidget(self.make_stack_box())
        col.addWidget(self.make_file_box())
        col.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(360)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 1)
        self.setCentralWidget(self.splitter)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        open_b = QtWidgets.QToolButton(text="Open…")
        open_b.clicked.connect(self.open_dialog)
        self.statusBar().addPermanentWidget(open_b)
        self.resize(1500, 950)
        self.splitter.setSizes([1100, 420])

        self.play_timer = QtCore.QTimer(self, interval=100)
        self.play_timer.timeout.connect(lambda: self.step(1, wrap=False))
        self.ui_timer = QtCore.QTimer(self, interval=250)
        self.ui_timer.timeout.connect(self.on_ui_timer)
        self.ui_timer.start()
        for key, fn in (("Space", self.toggle_play), ("Right", lambda: self.step(1)),
                        ("Left", lambda: self.step(-1)), ("PgDown", lambda: self.step(10)),
                        ("PgUp", lambda: self.step(-10)), ("Home", lambda: self.goto(0)),
                        ("End", lambda: self.goto(len(self.mv) - 1 if self.mv else 0)),
                        ("n", lambda: self.next_problem(1)), ("b", lambda: self.next_problem(-1)),
                        ("[", lambda: self.from_spin.setValue(self.index)),
                        ("]", lambda: self.to_spin.setValue(self.index)),
                        ("s", self.on_stack_button), ("a", self.auto_levels), ("c", self.show_frames),
                        ("Esc", self.show_frames), ("q", self.close)):
            QShortcut(QtGui.QKeySequence(key), self, fn)
        if path:
            self.open(path)

    # ---- panels ----
    def make_frame_box(self):
        box, g = grid_box("Frame")
        self.F = label_grid(g, [("frame", "Frame"), ("utc", "UTC"), ("step", "Step"),
                                ("problem", "Problem"), ("status", "Status")])
        self.F["problem"].setStyleSheet("color: #d32f2f;")
        return box

    def make_display_box(self):
        """Levels as in pystarlight-view: auto (zscale) or manual black / white point, auto
        optionally per frame; display gamma on top."""
        box, g = grid_box("Display")
        self.levels_auto, self.per_frame = True, False
        self.lo, self.hi = 0.0, 1.0
        self.lo_slider, self.hi_slider = (QtWidgets.QSlider(QtCore.Qt.Horizontal) for _ in range(2))
        self.lo_spin, self.hi_spin = (QtWidgets.QDoubleSpinBox(decimals=0, minimum=-1e7, maximum=1e7,
                                                               keyboardTracking=False)
                                      for _ in range(2))
        self.lo_slider.valueChanged.connect(lambda v: self.on_level(v, None))
        self.hi_slider.valueChanged.connect(lambda v: self.on_level(None, v))
        self.lo_spin.valueChanged.connect(lambda v: self.on_level(v, None))
        self.hi_spin.valueChanged.connect(lambda v: self.on_level(None, v))
        for row, (name, slider, spin) in enumerate((("Black", self.lo_slider, self.lo_spin),
                                                    ("White", self.hi_slider, self.hi_spin))):
            g.addWidget(QtWidgets.QLabel(name), row, 0)
            g.addWidget(slider, row, 1)
            g.addWidget(spin, row, 2)
        auto_b = QtWidgets.QPushButton("Auto  (a)")
        auto_b.setToolTip("zscale levels of the shown image; back to automatic levels")
        auto_b.clicked.connect(self.auto_levels)
        self.per_frame_check = QtWidgets.QCheckBox("per frame")
        self.per_frame_check.setToolTip("automatic levels: recompute them on every frame")
        self.per_frame_check.toggled.connect(self.on_per_frame)
        self.gamma_spin = QtWidgets.QDoubleSpinBox(decimals=2, minimum=0.1, maximum=4.0,
                                                   singleStep=0.1, value=1.0, prefix="gamma ")
        self.gamma_spin.valueChanged.connect(self.redraw)
        g.addWidget(auto_b, 2, 0)
        g.addWidget(self.per_frame_check, 2, 1)
        g.addWidget(self.gamma_spin, 2, 2)
        return box

    def make_stack_box(self):
        box = CollapsibleBox("Stack & plate solve")
        g = box.grid
        self.from_spin = QtWidgets.QSpinBox(prefix="from ", keyboardTracking=False)
        self.to_spin = QtWidgets.QSpinBox(prefix="to ", keyboardTracking=False)
        from_b = QtWidgets.QPushButton("[ here")
        from_b.clicked.connect(lambda: self.from_spin.setValue(self.index))
        to_b = QtWidgets.QPushButton("] here")
        to_b.clicked.connect(lambda: self.to_spin.setValue(self.index))
        self.align_check = QtWidgets.QCheckBox("Align on the stars", checked=True)
        self.focal_spin = QtWidgets.QDoubleSpinBox(decimals=1, maximum=10_000.0, suffix=" mm")
        self.focal_spin.setSpecialValueText("unknown")
        self.pixel_spin = QtWidgets.QDoubleSpinBox(decimals=2, maximum=50.0, suffix=" µm")
        self.pixel_spin.setSpecialValueText("unknown")
        self.focal_spin.valueChanged.connect(self.show_scale)
        self.pixel_spin.valueChanged.connect(self.show_scale)
        self.scale_label = QtWidgets.QLabel()
        self.stars_spin = QtWidgets.QSpinBox(minimum=0, maximum=30, value=5, prefix="name the ",
                                             suffix=" brightest stars")
        self.simbad_check = QtWidgets.QCheckBox("SIMBAD names", checked=True)
        self.save_check = QtWidgets.QCheckBox("Save as the movie's plate solution")
        self.save_check.setToolTip("<movie>.wcs.fit / .wcs.json / .stars.json, as pystarlight-view "
                                   "writes them (it then uses this solution)")
        self.stack_button = QtWidgets.QPushButton("Stack && solve  (s)")
        self.stack_button.setMinimumHeight(32)
        self.stack_button.clicked.connect(self.on_stack_button)
        self.show_check = QtWidgets.QCheckBox("Show stack")
        self.show_check.toggled.connect(self.on_show_stack)
        self.back_button = QtWidgets.QPushButton("▶ Back to the frames  (c)")
        self.back_button.clicked.connect(self.show_frames)
        self.back_button.setVisible(False)
        self.stack_info = QtWidgets.QLabel(wordWrap=True)
        self.stack_info.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.stack_info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        g.addWidget(self.from_spin, 0, 0)
        g.addWidget(from_b, 0, 1)
        g.addWidget(self.to_spin, 1, 0)
        g.addWidget(to_b, 1, 1)
        g.addWidget(self.align_check, 2, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("Focal length"), 3, 0)
        g.addWidget(self.focal_spin, 3, 1)
        g.addWidget(QtWidgets.QLabel("Pixel"), 4, 0)
        g.addWidget(self.pixel_spin, 4, 1)
        g.addWidget(self.scale_label, 5, 0, 1, 2)
        g.addWidget(self.stars_spin, 6, 0, 1, 2)
        g.addWidget(self.simbad_check, 7, 0)
        g.addWidget(self.save_check, 8, 0, 1, 2)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.stack_button)
        buttons.addWidget(self.show_check)
        g.addLayout(buttons, 9, 0, 1, 2)
        g.addWidget(self.back_button, 10, 0, 1, 2)
        g.addWidget(self.stack_info, 11, 0, 1, 2)
        return box

    def make_file_box(self):
        box = CollapsibleBox("File", True)
        g = box.grid
        self.D = label_grid(g, [("file", "File"), ("image", "Image"), ("frames", "Frames"),
                                ("timing", "Timing"), ("problems", "Problems")])
        self.meta_text = QtWidgets.QPlainTextEdit(readOnly=True)
        self.meta_text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.meta_text.setMinimumHeight(180)
        g.addWidget(QtWidgets.QLabel("Header"), 5, 0, 1, 2)
        g.addWidget(self.meta_text, 6, 0, 1, 2)
        return box

    # ---- opening ----
    def open_dialog(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open recording", os.path.dirname(self.path or os.path.expanduser("~/data")),
            "Recordings (*.ravf *.ser *.adv *.aav *.avi *.mov);;All files (*)")
        if p:
            self.open(p)

    def open(self, path):
        from ..movie import open_movie
        self.stop_play()
        if self.mv is not None:
            self.mv.close()
        self.path = str(path)
        self.mv = open_movie(self.path)
        n = len(self.mv)
        self.setWindowTitle(f"Review - {os.path.basename(self.path)}")
        self.times, self.info, self.flags, self.report = np.zeros(0), [], {}, None
        self.plot.dt = None
        self.plot.update()
        for w in (self.slider, self.frame_spin, self.from_spin, self.to_spin):
            w.blockSignals(True)
            w.setRange(0, max(0, n - 1))
            w.blockSignals(False)
        self.from_spin.setValue(0)
        self.to_spin.setValue(min(n - 1, 15))
        self.solution = None
        self.show_check.setChecked(False)
        self.fill_file_info()
        self.preset_optics()
        self.timing_note = "reading time stamps..."
        threading.Thread(target=self._load_timing, args=(self.path,), daemon=True).start()
        self.index = -1
        self.goto(0)
        if self.levels_auto:
            self.auto_levels()
        else:
            self.set_levels(self.lo, self.hi, manual=True, reset_range=True)

    def _load_timing(self, path):
        def progress(done, total):
            self.timing_note = f"reading time stamps... {done}/{total}"
        try:
            times, info = load_timing(path, progress)
            if path == self.path:
                self.times, self.info = times, info
                self.timing_note = ""
        except Exception as e:
            self.timing_note = f"time stamps not read: {e}"
        self.timing_ready.emit()

    def on_timing_ready(self):
        if len(self.times):
            self.flags, self.report = problems(self.times, self.info)
            step = self.report.step if self.report else float("nan")
            self.plot.set_data(self.times, self.flags, step)
            if np.isfinite(step) and step > 0:
                self.fps_spin.setValue(min(100.0, max(0.5, round(1 / step, 1))))
        self.fill_file_info()
        self.show_frame_info()

    def fill_file_info(self):
        mv, D = self.mv, self.D
        meta = dict(getattr(mv, "metadata", {}) or {})
        h, w = mv.shape
        D["file"].setText(self.path)
        size = os.path.getsize(self.path) / 1e6 if os.path.isfile(self.path) else 0
        D["image"].setText(f"{w} x {h}, {self.depth()} bit, {size:.0f} MB"
                           + ("  (unfinished: recovered)" if getattr(mv, "recovered", False) else ""))
        t = self.times[np.isfinite(self.times)] if len(self.times) else self.times
        dur = f", {t[-1] - t[0]:.2f} s" if len(t) > 1 else ""
        D["frames"].setText(f"{len(mv)}{dur}")
        rep = self.report
        if rep is not None and np.isfinite(rep.step):
            D["timing"].setText(f"step {rep.step * 1000:.3f} ms ({1 / rep.step:.3f} fps)"
                                if rep.step > 0 else "no steps")
        else:
            D["timing"].setText(self.timing_note or "-")
        drops = sum(1 for v in self.flags.values() if "dropped" in v or "missing" in v)
        D["problems"].setText("none" if rep is not None and not self.flags else
                              f"{len(self.flags)} frames ({drops} with dropped frames)"
                              if self.flags else "-")
        D["problems"].setStyleSheet("color: #d32f2f;" if self.flags else "")
        self.meta_text.setPlainText("\n".join(f"{k}: {v}" for k, v in meta.items()))

    def depth(self):
        meta = getattr(self.mv, "metadata", {}) or {}
        for k in ("IMAGE-BIT-DEPTH", "PixelDepthPerPlane", "BITPIX"):
            if meta.get(k):
                return abs(int(meta[k]))
        return 16

    def preset_optics(self):
        """Focal length and pixel size: command line, else the camera model in the file, else
        the config's default site (focal_mm, sensor_mm / width)."""
        meta = getattr(self.mv, "metadata", {}) or {}
        pix = self.pixel_arg or pixel_um(" ".join(str(meta.get(k, "")) for k in
                                                  ("INSTRUMENT", "INSTRUMENT-SENSOR", "Instrument")))
        focal = self.focal_arg
        try:
            from ..config import load, site
            st = site(load()) or {}
        except Exception:
            st = {}
        if not focal and st.get("focal_mm"):
            focal = float(st["focal_mm"])
        if not pix and st.get("sensor_mm"):
            h, w = self.mv.shape
            pix = float(st["sensor_mm"][0]) * 1000.0 / w * int(st.get("binning", 1))
        self.focal_spin.setValue(focal or 0.0)
        self.pixel_spin.setValue(pix or 0.0)
        self.show_scale()

    def show_scale(self):
        f, p = self.focal_spin.value(), self.pixel_spin.value()
        self.scale_label.setText(f"plate scale {206.265 * p / f:.2f}\"/px" if f and p
                                 else "no plate-scale hint (blind solve)")

    # ---- navigation ----
    def goto(self, i):
        if self.mv is None:
            return
        i = int(min(max(i, 0), len(self.mv) - 1))
        if i == self.index:
            return
        self.index = i
        for w in (self.slider, self.frame_spin):
            w.blockSignals(True)
            w.setValue(i)
            w.blockSignals(False)
        self.plot.set_current(i)
        self.img = self.mv.read(i).image
        self.show_frame_info()
        if not self.show_check.isChecked():
            if self.per_frame and self.levels_auto:
                self.set_levels(*zscale(self.img), manual=False)
            else:
                self.redraw()

    def step(self, k, wrap=True):
        if self.mv is None:
            return
        if not wrap and self.index + k >= len(self.mv):
            self.stop_play()
            return
        self.goto(self.index + k)

    def toggle_play(self):
        if self.play_timer.isActive():
            self.stop_play()
        else:
            self.show_check.setChecked(False)
            self.play_timer.start(int(1000 / self.fps_spin.value()))
            self.play_button.setText("⏸ Pause")

    def stop_play(self):
        self.play_timer.stop()
        self.play_button.setText("▶ Play")

    def next_problem(self, direction):
        rows = list(self.flags)
        if direction > 0:
            nxt = next((r for r in rows if r > self.index), None)
        else:
            nxt = next((r for r in reversed(rows) if r < self.index), None)
        if nxt is not None:
            self.goto(nxt)

    def show_frame_info(self):
        i, F = self.index, self.F
        n = len(self.mv) if self.mv else 0
        F["frame"].setText(f"{i} / {n - 1}")
        t = self.times[i] if i < len(self.times) else np.nan
        if not len(self.times):
            fr = self.mv.read(i) if self.mv else None
            F["utc"].setText(f"{fr.date or ''} {fr.time or '-'}" if fr else "-")
        else:
            F["utc"].setText(utc_text(t))
        if 0 < i < len(self.times) and np.isfinite(t) and np.isfinite(self.times[i - 1]):
            dt = (t - self.times[i - 1]) * 1000
            step = self.report.step * 1000 if self.report and np.isfinite(self.report.step) else None
            F["step"].setText(f"{dt:.3f} ms" + (f"  ({dt - step:+.3f} vs {step:.3f})" if step else ""))
        else:
            F["step"].setText("-")
        F["problem"].setText(self.flags.get(i, ""))
        d = self.info[i] if i < len(self.info) else {}
        if "sequence" in d:
            F["status"].setText(f"seq {d['sequence']}, exposure {d['exposure_ms']:.3f} ms, "
                                f"{d['satellites']} sats, {FIX_NAMES.get(d['fix'], d['fix'])}, "
                                f"leap s {'certain' if d['almanac'] == 0 else 'uncertain'}")
        elif d:
            F["status"].setText(", ".join(f"{k}: {v}" for k, v in list(d.items())[:12]))
        else:
            F["status"].setText("-")

    # ---- display ----
    def shown_image(self):
        return self.stack_view if self.show_check.isChecked() and self.stack_view is not None \
            else self.img

    @staticmethod
    def useful_range(img, lo, hi):
        """Levels slider range: the useful part of the histogram, not up to saturated stars."""
        p_lo, p_hi = (float(v) for v in np.percentile(img[::4, ::4], (0.1, 99.9)))
        span = max(hi - lo, 1.0)
        return min(p_lo, lo), max(p_hi, hi + 4 * span)

    def set_levels(self, lo, hi, manual=True, reset_range=False):
        """Black / white point. manual=False: set by auto levels. reset_range: slider range
        from the shown image (else it only grows)."""
        self.lo, self.hi = float(lo), float(max(hi, lo + 1.0))
        if manual:
            self.levels_auto = False
        smin, smax = self.lo_slider.minimum(), self.lo_slider.maximum()
        img = self.shown_image()
        if reset_range and img is not None:
            smin, smax = self.useful_range(img, self.lo, self.hi)
        smin, smax = int(np.floor(min(smin, self.lo))), int(np.ceil(max(smax, self.hi)))
        for slider in (self.lo_slider, self.hi_slider):
            slider.blockSignals(True)
            slider.setRange(smin, smax)
            slider.blockSignals(False)
        for w, v in ((self.lo_slider, round(self.lo)), (self.hi_slider, round(self.hi)),
                     (self.lo_spin, self.lo), (self.hi_spin, self.hi)):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self.redraw()

    def auto_levels(self):
        """zscale levels of the shown image; back to automatic mode."""
        img = self.shown_image()
        self.levels_auto = True
        if img is not None:
            self.set_levels(*zscale(img), manual=False, reset_range=True)

    def on_level(self, lo, hi):
        """User moved a black / white slider or spin box: manual levels."""
        lo = self.lo if lo is None else float(lo)
        hi = self.hi if hi is None else float(hi)
        if hi <= lo:                        # keep black below white
            if hi == self.hi:
                hi = lo + 1.0
            else:
                lo = hi - 1.0
        self.set_levels(lo, hi, manual=True)

    def on_per_frame(self, on):
        self.per_frame = on
        if on:
            self.auto_levels()

    def redraw(self, *_):
        img = self.shown_image()
        if img is None:
            return
        lo, hi = self.lo, max(self.hi, self.lo + 1e-6)
        disp = np.clip((np.asarray(img, np.float32) - lo) * (DISPLAY_STEPS / (hi - lo)),
                       0, DISPLAY_STEPS).astype(np.uint16)
        g = self.gamma_spin.value()
        lut = np.round(255.0 * (np.arange(DISPLAY_STEPS + 1, dtype=np.float32) / DISPLAY_STEPS)
                       ** (1 / g))
        self.view.render(disp, lut.astype(np.uint8))

    # ---- stack & solve ----
    def on_stack_button(self):
        if self.stack_thread is not None:
            self.cancel.set()
            if self.stacker is not None:
                self.stacker.cancel()
            return
        a, b = sorted((self.from_spin.value(), self.to_spin.value()))
        self.stop_play()
        self.solution = None
        self.view.circles = []
        self.cancel = threading.Event()
        self.job_log = f"stacking frames {a}-{b}..."
        self.stack_thread = threading.Thread(target=self._stack_and_solve, args=(a, b), daemon=True)
        self.stack_thread.start()
        self.show_check.setChecked(True)

    def stack_meta(self):
        meta = getattr(self.mv, "metadata", {}) or {}
        out = {"instrument": str(meta.get("INSTRUMENT") or meta.get("Instrument") or ""),
               "depth": self.depth(), "pixel_um": self.pixel_spin.value() or None,
               "focal_mm": self.focal_spin.value() or None}
        try:
            lat, lon = float(meta.get("LATITUDE", 0)), float(meta.get("LONGITUDE", 0))
            if (lat, lon) != (0.0, 0.0):
                out.update(latitude=lat, longitude=lon, altitude=float(meta.get("ALTITUDE", 0)))
        except (TypeError, ValueError):
            pass
        return out

    def _stack_and_solve(self, a, b):
        from ..movie import open_movie
        from ..wcs.solve_stack import solve_fits
        stem = Path(self.path).stem
        try:
            st = self.stacker = LiveStacker(lambda img: img, os.path.dirname(self.path) or ".",
                                            f"{stem}_f{a}-{b}", self.stack_meta)
            st.begin(b - a + 1, self.align_check.isChecked())
            with open_movie(self.path) as mv:
                for i in range(a, b + 1):
                    while not st.wants() and st.state == "stacking":
                        time.sleep(0.005)
                    if st.state != "stacking" or self.cancel.is_set():
                        break
                    fr = mv.read(i)
                    t = self.times[i] if i < len(self.times) else np.nan
                    d = self.info[i] if i < len(self.info) else {}
                    st.put(fr.image, int(t * 1e9) if np.isfinite(t) and t > 86400 * 2 else None,
                           {"exposure_ns": d.get("exposure_ms", 0) * 1e6})
            while st.state == "stacking" and not self.cancel.is_set():
                time.sleep(0.02)
            st.close()
            if st.state != "done":
                self.job_log = st.error or "stack cancelled"
                return
            self.job_log = f"stacked {st.count} frames: {os.path.basename(st.path)}; plate solving..."

            def log(msg):
                self.job_log = msg

            sol = solve_fits(st.path, self.stars_spin.value(), use_simbad=self.simbad_check.isChecked(),
                             log=log, cancel=self.cancel)
            sol["frames"] = [a, b]
            self.solution = sol
            self.job_log = ""
            if self.save_check.isChecked():
                self.job_log = self.save_solution(sol, a, st.count)
        except Exception as e:
            self.job_log = f"failed: {e}"
        finally:
            self.stack_thread = None

    def save_solution(self, sol, start, count):
        """The stack's solution as the movie's (as pystarlight-view caches it). The stack is
        aligned to its first frame, so the WCS belongs to frame `start`."""
        from ..wcs.identify import FieldStar, save_stars
        from ..wcs.solve import solution_path
        dest = solution_path(self.path)
        shutil.copyfile(sol["wcs"], dest)
        with open(dest.with_suffix(".json"), "w") as f:
            json.dump({"job_id": sol.get("job_id"), "stack_start": start, "stack_frames": count,
                       "orientation": {"scale_arcsec": sol["scale_arcsec"],
                                       "north_deg": sol["north_deg"], "mirrored": sol["mirrored"],
                                       "center_ra": sol["center"]["ra"],
                                       "center_dec": sol["center"]["dec"]}}, f, indent=1)
        stars_path = dest.with_name(dest.name.replace(".wcs.fit", ".stars.json"))
        keys = FieldStar.__dataclass_fields__
        save_stars(str(stars_path), [FieldStar(**{k: v for k, v in s.items() if k in keys})
                                     for s in sol.get("stars", [])])
        return f"saved as the movie's plate solution: {dest.name}"

    def on_show_stack(self, on):
        self.back_button.setVisible(on)
        self.stack_seen = -1
        if on:
            self.stop_play()
            self.show_marks()
        else:
            self.view.circles = []
            self.view.banner = ""
        if self.levels_auto:
            self.auto_levels()
        else:
            self.redraw()

    def show_frames(self):
        self.show_check.setChecked(False)

    def show_marks(self):
        sol = self.solution
        if sol and self.show_check.isChecked():
            color = QtGui.QColor("#00e676")
            self.view.circles = [(s["x"], s["y"], max(4.0, 2 * (s.get("fwhm") or 2)),
                                  f"{i} {s.get('name') or ''}".strip(), color)
                                 for i, s in enumerate(sol.get("stars", []), 1)]
            self.view.update()

    def on_ui_timer(self):
        st = self.stacker
        running = self.stack_thread is not None
        self.stack_button.setText("Cancel  (s)" if running else "Stack && solve  (s)")
        if self.show_check.isChecked() and st is not None and st.version != self.stack_seen:
            self.stack_seen = st.version
            mean = st.preview()
            self.stack_view = mean
            if self.levels_auto:
                self.set_levels(*zscale(mean), manual=False, reset_range=st.version <= 1)
            else:
                self.redraw()
        text = self.job_log
        if running and st is not None and st.state == "stacking":
            text = f"stacking {st.count}/{st.target}" + (
                f", drift {st.shift[0]:+.1f}, {st.shift[1]:+.1f} px" if st.align and st.count > 1 else "")
        sol = self.solution
        if sol is not None:
            from ..wcs.solve_stack import describe
            text = (text + "\n" if text else "") + f"frames {sol['frames'][0]}-{sol['frames'][1]}\n" \
                + describe(sol)
            if self.show_check.isChecked() and not self.view.circles:
                self.show_marks()
        if text != self.stack_info.text():
            self.stack_info.setText(text)
        banner = ""
        if self.show_check.isChecked():
            banner = (f"STACK {st.count}/{st.target}" if running and st and st.state == "stacking"
                      else f"STACK frames {self.from_spin.value()}-{self.to_spin.value()}"
                      + (" - solved" if sol else ""))
            banner += "   ·   c: back to the frames"
        if banner != self.view.banner:
            self.view.banner = banner
            self.view.update()
        if self.timing_note:
            self.D["timing"].setText(self.timing_note)
        self.status.setText(os.path.basename(self.path or "") + self.cursor_text())

    def cursor_text(self):
        p, img = self.view.cursor_img, self.img
        if p is None or img is None:
            return ""
        ix, iy = int(p[0]), int(p[1])
        if not (0 <= ix < img.shape[1] and 0 <= iy < img.shape[0]):
            return ""
        return f"   ({ix}, {iy}) = {img[iy, ix]}"

    def closeEvent(self, event):
        self.stop_play()
        if self.cancel is not None:
            self.cancel.set()
        if self.mv is not None:
            self.mv.close()
        event.accept()


def main(argv=None):
    ap = argparse.ArgumentParser(description="PyStarLight review player: frames, time stamps, "
                                             "stack & plate solve")
    ap.add_argument("file", nargs="?", help="recording (.ravf .ser .adv .aav .avi) or FITS folder")
    ap.add_argument("--focal-mm", type=float, default=None, help="focal length for the scale hint")
    ap.add_argument("--pixel-um", type=float, default=None, help="pixel pitch for the scale hint")
    args = ap.parse_args(argv)
    fix_cv2_qt_env()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    win = ReviewWindow(args.file, args.focal_mm, args.pixel_um)
    win.show()
    return exec_app(app)


if __name__ == "__main__":
    main()
