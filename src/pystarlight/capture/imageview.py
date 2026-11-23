"""The camera window's live image (from gui.py): ImageView (zoom, pan, selections, the marks of
the stack, lock, focus and polar alignment), RecordOverlay (the Record / Photo buttons on it)."""
import cv2
import numpy as np

from .qt import QtCore, QtGui, QtWidgets, Signal, event_pos
from .widgets import NIGHT_DIM, night_color


class RecordOverlay(QtWidgets.QFrame):
    """The Record / Stop and Photo buttons and the time (HH:MM:SS UTC), floating in a corner of
    the image so they never scroll away (the side panel's buttons stay; same actions). Outlined
    and see-through, so they do not glare; filled a little only while active."""

    MARGIN = 10

    def __init__(self, parent, on_click, on_photo=None):
        super().__init__(parent)
        self.setObjectName("rec_overlay")
        self.night = False
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 4, 4)
        lay.setSpacing(10)
        self.clock = QtWidgets.QLabel("--:--:--")
        font = QtGui.QFont("monospace")
        font.setStyleHint(QtGui.QFont.Monospace)
        font.setPointSize(15)
        font.setBold(True)
        self.clock.setFont(font)
        self.clock.setToolTip("UTC: the GPS clock once anchored, else the Pi clock")
        self.button = QtWidgets.QPushButton()
        self.button.setMinimumSize(120, 40)
        self.button.setFocusPolicy(QtCore.Qt.NoFocus)        # the keys stay with the window
        self.button.clicked.connect(on_click)
        lay.addWidget(self.clock)
        self.photo = None
        if on_photo is not None:                    # a single photo (Stack & plate solve: its exposure)
            self.photo = QtWidgets.QPushButton()
            self.photo.setMinimumSize(110, 40)
            self.photo.setFocusPolicy(QtCore.Qt.NoFocus)
            self.photo.clicked.connect(on_photo)
            lay.addWidget(self.photo)
        lay.addWidget(self.button)
        self.keys = {}
        self.set_night(False)

    def set_night(self, on):
        self.night = on
        self.keys = {}                              # restyle everything on the next update
        clock = NIGHT_DIM if on else "rgba(255, 255, 255, 200)"
        self.setStyleSheet("QFrame#rec_overlay { background: rgba(0, 0, 0, 60); border-radius: 8px; }"
                           f"QLabel {{ color: {clock}; background: transparent; }}")

    def _style(self, widget, text, color, active, tip=None):
        """Outlined: the colour as text and border on a faint fill (stronger while active)."""
        key = (text, color, active, self.night)
        if self.keys.get(widget) == key:
            return
        self.keys[widget] = key
        c = night_color(color, self.night)
        fill = f"rgba({c.red()}, {c.green()}, {c.blue()}, {70 if active else 15})"
        widget.setText(text)
        if tip:
            widget.setToolTip(tip)
        widget.setStyleSheet(f"QPushButton {{ font-size: 15px; font-weight: bold; border-radius: 6px; "
                             f"padding: 4px 12px; color: {c.name()}; background: {fill}; "
                             f"border: 2px solid {c.name()}; }}"
                             f"QPushButton:hover {{ background: rgba({c.red()}, {c.green()}, "
                             f"{c.blue()}, 90); }}")

    def show_photo(self, text, color, active, tip):
        if self.photo is not None:
            self._style(self.photo, text, color, active, tip)

    def show_state(self, text, color, active, clock, source):
        """Button text / colour and the clock; only what changed is redrawn."""
        self._style(self.button, text, color, active)
        if clock != self.clock.text():
            self.clock.setText(clock)
            self.clock.setToolTip(f"UTC from the {source}")
        self.adjustSize()
        self.place()

    def place(self):
        """Top right of the image view (in full screen the black band above the image)."""
        p = self.parentWidget()
        self.move(p.width() - self.width() - self.MARGIN, self.MARGIN)


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
        self.lock_boxes = []        # the same, of the star lock (its stars and the target)
        self.focus_boxes = []       # the same, of the Focuser window's star
        self.pick_callback = None   # set: the next click on the image goes to it as (x, y)
        self.crosshair = False      # full-size crosshair through the image centre, open middle
        self.compass = None         # [(label, ux, uy)]: north / east as unit vectors on the image
        self.hline = None           # (row, QColor): a dashed line across the image
        self.hspan = 0              # rows above/below it: dotted lines (0: none)
        self.circles = []           # [(x, y, radius, text, QColor)]: stars (image pixels)
        self.markers = []           # [(x, y, radius, text, QColor)]: ticks around, star left free
        self.banner = ""            # shown at the bottom (e.g. while the stack is displayed)
        self.cursor_img = None      # image point under the mouse
        self.polar = None           # the Polar alignment window's marks (polar_gui.paint_overlay)

    def reset_view(self):
        self.zoom, self.center = 1.0, None
        self.changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for child in self.findChildren(RecordOverlay):
            child.place()

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
        rects = list(self.marks) + list(self.lock_boxes) + list(self.focus_boxes)
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
        self.paint_markers(p)
        if self.polar:
            from .polar_gui import paint_overlay
            paint_overlay(self, p, self.polar)
        if self.crosshair and self.img_size:
            self.paint_crosshair(p)
        if self.compass:
            self.paint_compass(p)
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

    CROSS_GAP_PX = 18               # the open middle of the crosshair (screen px)

    def paint_crosshair(self, p):
        """A crosshair through the image centre, across the whole image, its middle left open
        (the star there stays visible)."""
        w, h = self.img_size
        cx, cy = self.to_screen(w / 2, h / 2)
        x0, y0 = self.to_screen(0, 0)
        x1, y1 = self.to_screen(w, h)
        pen = QtGui.QPen(QtGui.QColor(255, 82, 82, 190), 1)
        pen.setCosmetic(True)
        p.setPen(pen)
        g = self.CROSS_GAP_PX
        for a, b in (((x0, cy), (cx - g, cy)), ((cx + g, cy), (x1, cy)),
                     ((cx, y0), (cx, cy - g)), ((cx, cy + g), (cx, y1))):
            p.drawLine(QtCore.QPointF(*a), QtCore.QPointF(*b))

    def paint_compass(self, p, length=34):
        """N and E arrows in the lower-right corner (top right: the Record button, top left: the
        zoom note, bottom left: the banner), from the plate solution (as in the analysis)."""
        ox, oy = self.width() - 58.0, self.height() - 58.0
        pen = QtGui.QPen(QtGui.QColor("#ffa726"), 2)
        pen.setCosmetic(True)
        p.setPen(pen)
        for label, ux, uy in self.compass:
            tip = QtCore.QPointF(ox + ux * length, oy + uy * length)
            p.drawLine(QtCore.QPointF(ox, oy), tip)
            for side in (+1, -1):                       # arrow head
                a = np.arctan2(uy, ux) + np.pi + side * 0.45
                p.drawLine(tip, QtCore.QPointF(tip.x() + 8 * np.cos(a), tip.y() + 8 * np.sin(a)))
            p.drawText(QtCore.QPointF(ox + ux * (length + 12) - 4, oy + uy * (length + 12) + 5), label)

    def paint_markers(self, p):
        """Four ticks around each star, starting outside its image (the star stays visible),
        and the label up and to the right of the upper-right tick, on a dark box."""
        for x, y, r, text, color in self.markers:
            cx, cy = self.to_screen(x + 0.5, y + 0.5)
            if not (-50 < cx < self.width() + 50 and -50 < cy < self.height() + 50):
                continue
            r0 = max(12.0, r * self.map[4] + 4.0)       # gap: the star's radius on screen
            r1 = r0 + 12.0
            pen = QtGui.QPen(color, 2)
            pen.setCosmetic(True)
            p.setPen(pen)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p.drawLine(QtCore.QPointF(cx + dx * r0, cy + dy * r0),
                           QtCore.QPointF(cx + dx * r1, cy + dy * r1))
            if text:
                br = p.fontMetrics().boundingRect(text).adjusted(-3, -1, 3, 1)
                br.moveBottomLeft(QtCore.QPoint(int(cx + 0.7 * r1), int(cy - 0.7 * r1)))
                p.fillRect(br, QtGui.QColor(0, 0, 0, 150))
                p.drawText(br, QtCore.Qt.AlignCenter, text)

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
        if self.pick_callback is not None:            # one click: a star picked
            callback, self.pick_callback = self.pick_callback, None
            self.unsetCursor()
            callback(*self.to_image(event_pos(e)))
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
            self.setCursor(QtCore.Qt.CrossCursor if self.pick_callback is not None else
                           QtCore.Qt.SizeVerCursor if near else QtCore.Qt.ArrowCursor)
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
