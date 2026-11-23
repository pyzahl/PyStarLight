"""Widgets and the night theme of the camera window and the player (from gui.py): a collapsible
panel section, a titled grid, zscale levels and the Levels box, night mode's style sheet."""
import numpy as np

from .qt import QtCore, QtGui, QtWidgets, Signal

GAMMA_MIN, GAMMA_MAX = 0.1, 4.0
# per-frame auto levels: move this fraction towards each frame's zscale. On a quiet sensor
# (IMX265M dark: sigma 2.8 ADU, a ~20 ADU window) zscale wobbles by 10-15 % frame to frame and
# the image flickers; a jump larger than the window (new scene) is taken at once
LEVELS_SMOOTH = 0.2


NIGHT_RED = "#c40000"           # night mode: the one colour (dark adaptation)
NIGHT_DIM = "#7a0000"


# Night mode for dark-adapted eyes: the whole app dark, text and lines in dim red (Qt style sheet,
# switched at run time). The window manager draws the title bar: Full screen (F11) hides it.
NIGHT_QSS = f"""
* {{ color: {NIGHT_RED}; }}
QWidget {{ background: #0a0000; selection-background-color: #3a0000; selection-color: #ff2020; }}
QMainWindow, QDialog, QDockWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: #0a0000; }}
QGroupBox {{ border: 1px solid #3a0000; border-radius: 4px; margin-top: 14px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 6px; padding: 0 3px; color: {NIGHT_RED}; }}
QDockWidget::title {{ background: #1a0000; padding: 3px; }}
QPushButton, QToolButton {{ background: #140000; border: 1px solid #5a0000; border-radius: 4px;
                           padding: 3px 8px; }}
QPushButton:hover, QToolButton:hover {{ background: #260000; }}
QPushButton:pressed, QPushButton:checked {{ background: #3a0000; }}
QPushButton:disabled, QLabel:disabled, QCheckBox:disabled {{ color: #4a0000; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: #120000; border: 1px solid #4a0000; border-radius: 3px; }}
QComboBox QAbstractItemView {{ background: #120000; border: 1px solid #4a0000; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 13px; height: 13px; border: 1px solid #7a0000;
                                                background: #120000; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: #9a0000; }}
QSlider::groove:horizontal {{ height: 4px; background: #3a0000; }}
QSlider::handle:horizontal {{ width: 12px; margin: -6px 0; background: #8a0000; border-radius: 6px; }}
QScrollBar {{ background: #0a0000; }}
QScrollBar::handle {{ background: #3a0000; border-radius: 4px; }}
QMenuBar, QMenu, QStatusBar, QToolTip {{ background: #0a0000; border: 1px solid #3a0000; }}
QMenu::item:selected, QMenuBar::item:selected {{ background: #3a0000; }}
QSplitter::handle {{ background: #1a0000; }}
QAbstractScrollArea::corner, QSizeGrip, QMainWindow::separator {{ background: #0a0000; border: none; }}
"""


def night_color(color, night):
    """A colour as shown: in night mode a red of the same brightness (dimmed), else itself."""
    c = QtGui.QColor(color)
    if not night:
        return c
    v = max(c.red(), c.green(), c.blue())
    return QtGui.QColor(int(50 + 0.6 * v), 0, 0, c.alpha())



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


def zscale(img):
    """IRAF zscale display limits (as pystarlight-view): faint stars without burning out the
    sky. Large frames are subsampled first (zscale samples ~1000 pixels anyway); the odd step
    keeps all colours of a Bayer mosaic."""
    from astropy.visualization import ZScaleInterval
    img = np.asarray(img)
    step = 2 * (int(np.sqrt(img.size / 250_000)) // 2) + 1
    return ZScaleInterval().get_limits(np.asarray(img[::step, ::step], np.float32))


class LevelsBox(QtWidgets.QGroupBox):
    """Display levels as in pystarlight-view: auto (zscale) or manual black / white point, auto
    optionally per frame; display gamma on top. image() gives the shown image (raw values);
    changed is emitted on every new setting and the owner redraws. full() (optional) gives
    the raw value range for the "Full" button."""
    changed = Signal()

    def __init__(self, image, gamma=1.0, full=None):
        super().__init__("Display")
        self.image, self.full = image, full
        self.auto, self.per_frame = True, False
        self.lo, self.hi = 0.0, 1.0
        g = QtWidgets.QGridLayout(self)
        g.setColumnStretch(1, 1)
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
        self.gamma_spin = QtWidgets.QDoubleSpinBox(decimals=2, minimum=GAMMA_MIN, maximum=GAMMA_MAX,
                                                   singleStep=0.1, value=gamma, prefix="gamma ",
                                                   keyboardTracking=False)
        self.gamma_spin.setToolTip("display gamma (>1 brightens faint detail; keys d / D)")
        self.gamma_spin.valueChanged.connect(lambda _: self.changed.emit())
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(auto_b)
        if full is not None:
            full_b = QtWidgets.QPushButton("Full")
            full_b.setToolTip("black 0, white the largest raw value (manual levels)")
            full_b.clicked.connect(self.full_levels)
            buttons.addWidget(full_b)
        buttons.addWidget(self.per_frame_check)
        g.addLayout(buttons, 2, 0, 1, 2)
        g.addWidget(self.gamma_spin, 2, 2)

    @property
    def gamma(self):
        return self.gamma_spin.value()

    @staticmethod
    def useful_range(img, lo, hi):
        """Levels slider range: the useful part of the histogram, not up to saturated stars."""
        p_lo, p_hi = (float(v) for v in np.percentile(img[::4, ::4], (0.1, 99.9)))
        span = max(hi - lo, 1.0)
        return min(p_lo, lo), max(p_hi, hi + 4 * span)

    def set_levels(self, lo, hi, manual=True, reset_range=False, slider_range=None):
        """Black / white point. manual=False: set by auto levels. reset_range: slider range
        from the shown image (else it only grows); slider_range: exactly this range."""
        self.lo, self.hi = float(lo), float(max(hi, lo + 1.0))
        if manual:
            self.auto = False
        smin, smax = slider_range or (self.lo_slider.minimum(), self.lo_slider.maximum())
        img = self.image()
        if reset_range and slider_range is None and img is not None:
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
        self.changed.emit()

    def auto_levels(self):
        """zscale levels of the shown image; back to automatic mode."""
        img = self.image()
        self.auto = True
        if img is not None:
            self.set_levels(*zscale(img), manual=False, reset_range=True)

    def full_levels(self):
        lo, hi = self.full()
        self.set_levels(lo, hi, manual=True, slider_range=(lo, hi))

    def new_image(self):
        """The shown image changed: True if per-frame auto levels were set (changed emitted)."""
        img = self.image()
        if self.auto and self.per_frame and img is not None:
            lo, hi = zscale(img)
            span = self.hi - self.lo
            if abs(lo - self.lo) < span and abs(hi - self.hi) < span:
                lo = self.lo + LEVELS_SMOOTH * (lo - self.lo)
                hi = self.hi + LEVELS_SMOOTH * (hi - self.hi)
            self.set_levels(lo, hi, manual=False)
            return True
        return False

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

    def lut(self, n, scale=1.0):
        """Raw value (0..n-1) -> 8 bit through the levels and gamma; scale: the image holds
        raw values times this."""
        x = (np.arange(n, dtype=np.float32) / scale - self.lo) / max(self.hi - self.lo, 1e-6)
        return np.round(255.0 * np.clip(x, 0.0, 1.0) ** (1.0 / self.gamma)).astype(np.uint8)
