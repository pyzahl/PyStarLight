"""The camera's DSLR window (menu DSLR, Shift+C; a window of its own, place and size remembered):
a DSLR or mirrorless camera as a second, photo-only camera beside the video camera, which keeps
streaming, recording and tracking. capture/dslr.py does the camera.

NEW CODE. Connect (the cameras gphoto2 sees), the camera's ISO / aperture / image format / where
its files go, the exposure (the nearest shutter speed, or bulb timed here beyond the longest),
Photo: the camera's files and a FITS (Bayer mosaic, or RGB from a JPEG) into the recording
folder, with the site, target and time the camera window knows. The window shows the live view
(colour, the camera's JPEG) or the last photo (debayered with the camera's white balance); in
night mode in dim red. Config [dslr].
"""
import os
import threading
import time

import numpy as np

from . import dslr as ds
from . import localconfig, photo, station_header
from .qt import QtCore, QtGui, QtWidgets

DEFAULTS = {"focal_mm": 0.0, "pixel_um": 0.0, "out_dir": "", "exposure_s": 10.0, "solve_timeout_s": 120.0}
NO_CAMERA = "(no camera found)"


def re_safe(text):
    """A model name for a file: letters, digits, '.', '+', '-', '_' ('Sony Alpha-A7r II' ->
    'Sony_Alpha-A7r_II', as the video camera's file names)."""
    import re
    return re.sub(r"[^A-Za-z0-9.+_-]+", "_", text or "").strip("_.")[:60]


MARK_COLORS = {"star": "#40c4ff", "target": "#ff4081", "verify": "#ffd740"}   # as the camera window


def _compass(sol):
    from .photo_solve import compass
    return compass(sol)


class PhotoView(QtWidgets.QLabel):
    """An RGB image fitted to the label (aspect kept); night mode: its brightness in dim red."""

    def __init__(self):
        super().__init__("no photo yet")
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumSize(320, 220)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setStyleSheet("background: black; color: gray;")
        self.rgb, self.night = None, False
        self.marks, self.compass = [], None       # the plate solve's: (x, y as fractions, label, kind)

    def show_rgb(self, rgb, keep_marks=False):
        self.rgb = None if rgb is None else np.ascontiguousarray(rgb)
        if not keep_marks:
            self.marks, self.compass = [], None
        self._draw()

    def set_marks(self, marks, compass):
        self.marks, self.compass = list(marks), compass
        self._draw()

    def set_night(self, on):
        self.night = on
        self._draw()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._draw()

    def _draw(self):
        if self.rgb is None:
            self.setPixmap(QtGui.QPixmap())
            return
        rgb = self.rgb
        if self.night:                          # dark adaptation: brightness in red only, dimmed
            v = (rgb.astype(np.uint16).sum(axis=2) // 3 * 6 // 10).astype(np.uint8)
            rgb = np.zeros_like(rgb)
            rgb[..., 0] = v
        h, w = rgb.shape[:2]
        qimg = QtGui.QImage(rgb.data, w, h, rgb.strides[0], QtGui.QImage.Format_RGB888).copy()
        pix = QtGui.QPixmap.fromImage(qimg).scaled(self.size(), QtCore.Qt.KeepAspectRatio,
                                                   QtCore.Qt.SmoothTransformation)
        if self.marks or self.compass:
            self._paint_marks(pix)
        self.setPixmap(pix)

    def _paint_marks(self, pix):
        """The markers (stars, T, V) and N / E over the photo, as on the camera window's image;
        night mode: dim red."""
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        W, H = pix.width(), pix.height()
        for fx, fy, label, kind in self.marks:
            color = QtGui.QColor("#a00000" if self.night else MARK_COLORS.get(kind, "#ffffff"))
            p.setPen(QtGui.QPen(color, 1.5))
            x, y = fx * W, fy * H
            p.drawEllipse(QtCore.QPointF(x, y), 7.0, 7.0)
            p.drawText(QtCore.QPointF(x + 10, y - 4), label)
        if self.compass:
            ox, oy = W - 40.0, H - 40.0
            p.setPen(QtGui.QPen(QtGui.QColor("#a00000" if self.night else "#ffa726"), 1.5))
            for label, ux, uy in self.compass:
                p.drawLine(QtCore.QPointF(ox, oy), QtCore.QPointF(ox + 24 * ux, oy + 24 * uy))
                p.drawText(QtCore.QPointF(ox + 32 * ux - 4, oy + 32 * uy + 4), label)
        p.end()


class DslrWindow(QtWidgets.QWidget):
    def __init__(self, main, thread=None):
        super().__init__(main, QtCore.Qt.Window | QtCore.Qt.Tool)
        self.setWindowTitle("DSLR")
        self.setObjectName("dslr_window")
        self.main = main
        self.cfg = {**DEFAULTS, **localconfig.section("dslr")}
        self.thread = thread             # dslr.DslrThread, made on first Connect
        self.action = None
        self.error = ""
        self.shown_live = self.shown_shots = -1
        self.settings_shown = None
        self.solving = None               # the plate solve's thread
        self.solve_log = ""
        self.solution = None              # of the last photo: photo_solve's dict

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs)
        cam_tab = QtWidgets.QWidget()
        self.tabs.addTab(cam_tab, "Camera")
        g = QtWidgets.QGridLayout(cam_tab)
        g.setContentsMargins(8, 8, 8, 8)
        g.setSpacing(4)
        r = 0
        self.camera_combo = QtWidgets.QComboBox()
        self.camera_combo.setToolTip("the cameras gphoto2 sees on USB (PC / PTP mode)")
        self.find_button = QtWidgets.QPushButton("Find")
        self.find_button.setToolTip("look for cameras again")
        self.find_button.clicked.connect(self.find)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.clicked.connect(self.on_connect)
        g.addWidget(self.camera_combo, r, 0, 1, 2)
        g.addWidget(self.find_button, r, 2)
        g.addWidget(self.connect_button, r, 3)
        r += 1
        self.state_label = QtWidgets.QLabel("")
        self.state_label.setWordWrap(True)
        g.addWidget(self.state_label, r, 0, 1, 4)
        r += 1

        form = QtWidgets.QFormLayout()
        self.combos = {}
        for meaning, label, tip in (
                ("iso", "ISO", "the camera's ISO choices"),
                ("aperture", "Aperture", "a lens with contacts (a telescope has none)"),
                ("format", "Image format",
                 "RAW gives the Bayer mosaic in the FITS; RAW + JPEG: both files kept"),
                ("target", "Files go to", "the camera's RAM (downloaded only) or its card (kept there too)")):
            c = QtWidgets.QComboBox()
            c.setToolTip(tip)
            c.activated.connect(lambda i, m=meaning: self.on_setting(m))
            self.combos[meaning] = c
            form.addRow(label, c)
        self.exp_spin = QtWidgets.QDoubleSpinBox(decimals=4, minimum=0.0001, maximum=ds.MAX_BULB_S,
                                                 suffix=" s", keyboardTracking=False)
        self.exp_spin.setValue(main.settings.value("dslr/exposure_s", float(self.cfg["exposure_s"]),
                                                   type=float))
        self.exp_spin.setToolTip("the nearest shutter speed of the camera; longer than its longest "
                                 "(usually 30 s): Bulb, timed here (the camera in M or B mode)")
        self.shutter_label = QtWidgets.QLabel("")
        self.shutter_label.setStyleSheet("color: gray;")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.exp_spin)
        row.addWidget(self.shutter_label)
        form.addRow("Exposure", row)
        g.addLayout(form, r, 0, 1, 4)
        r += 1

        self.photo_button = QtWidgets.QPushButton("Photo")
        self.photo_button.setMinimumHeight(40)
        self.photo_button.setStyleSheet("font-weight: bold;")
        self.photo_button.setToolTip("one photo: the camera's files and a FITS into the recording folder")
        self.photo_button.clicked.connect(self.on_photo)
        self.live_check = QtWidgets.QCheckBox("Live view")
        self.live_check.setToolTip("the camera's live view here (framing, focus on a bright star); "
                                   "pauses for a photo")
        self.live_check.toggled.connect(self.on_live)
        self.solve_check = QtWidgets.QCheckBox("Plate solve")
        self.solve_check.setChecked(main.settings.value("dslr/solve", True, type=bool))
        self.solve_check.setToolTip("solve each photo as the video camera's stack (the camera window's star "
                                    "names, target and verify star): WCS and markers into its FITS, an "
                                    "annotated PNG beside it")
        self.solve_check.toggled.connect(lambda on: main.settings.setValue("dslr/solve", on))
        g.addWidget(self.photo_button, r, 0, 1, 2)
        g.addWidget(self.solve_check, r, 2)
        g.addWidget(self.live_check, r, 3)
        r += 1
        self.view = PhotoView()
        g.addWidget(self.view, r, 0, 1, 4)
        g.setRowStretch(r, 1)
        r += 1
        self.info_label = QtWidgets.QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        g.addWidget(self.info_label, r, 0, 1, 4)
        r += 1
        self.msg_label = QtWidgets.QLabel("")
        self.msg_label.setWordWrap(True)
        g.addWidget(self.msg_label, r, 0, 1, 4)

        from .schedule_gui import ProgramPanel  # photo programs (schedule.py)
        self.program = ProgramPanel(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(self.program)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.tabs.addTab(scroll, "Program")

        geo = main.settings.value("dslr/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(460, 640)
        self.timer = QtCore.QTimer(self, timeout=self.poll, interval=250)
        self.timer.start()
        self.cameras = []
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
        if not self.cameras and self.thread is None:
            self.find()
        super().showEvent(e)

    def closeEvent(self, e):
        self.main.settings.setValue("dslr/geometry", self.saveGeometry())
        self.live_check.setChecked(False)           # the camera stays connected
        self._sync_action(False)
        super().closeEvent(e)

    def set_night(self, on):
        self.view.set_night(on)

    # ---- connecting ----
    def find(self):
        why = ds.available()
        self.cameras = [] if why else ds.find_dslrs()
        self.camera_combo.clear()
        for model, port in self.cameras:
            self.camera_combo.addItem(f"{model}  ({port})", (model, port))
        if not self.cameras:
            self.camera_combo.addItem(NO_CAMERA, None)
        self.error = why or ("" if self.cameras else
                             "no camera: USB cable, the camera on and in PC / PTP mode (not mass storage)")

    def clock(self):
        gps = getattr(self.main.c, "gps", None)
        from .timing import utc_now_ns
        return utc_now_ns(gps.clock if gps is not None else None)

    def on_connect(self):
        t = self.thread
        if t is not None and t.state != "closed":
            self.live_check.setChecked(False)
            t.ask("close")
            return
        cam = self.camera_combo.currentData()
        if cam is None:
            self.find()
            return
        if t is None:
            self.thread = t = ds.DslrThread(clock=self.clock)
        t.state = "busy"
        t.ask("connect", *cam)

    # ---- settings ----
    def on_setting(self, meaning):
        value = self.combos[meaning].currentText()
        if self.thread is not None and self.thread.dslr is not None:
            self.thread.ask("set", meaning, value)

    def fill_settings(self, s):
        """The camera's settings into the lists (only when they changed: a list being opened
        is not refilled under the mouse)."""
        key = repr(s)
        if key == self.settings_shown:
            return
        self.settings_shown = key
        choices = s.get("choices", {})
        for meaning, combo in self.combos.items():
            combo.blockSignals(True)
            combo.clear()
            items = list(choices.get(meaning) or [])
            value = s.get(meaning)
            if value is not None and str(value) not in items:
                items.insert(0, str(value))
            combo.addItems([str(i) for i in items])
            combo.setCurrentIndex(max(0, combo.findText(str(value))) if value is not None else -1)
            combo.setEnabled(bool(items))
            combo.blockSignals(False)

    # ---- photos ----
    def header_info(self, model):
        observer = localconfig.section("observer")
        info = {"instrument": model,
                "pixel_um": float(self.cfg.get("pixel_um") or 0.0) or ds.pixel_um_of(model) or 0.0,
                "focal_mm": float(self.cfg.get("focal_mm") or 0.0)}
        info.update(station_header.site_meta(getattr(self.main.c, "gps", None), observer))
        return info

    def target(self):
        name = getattr(self.main, "target_name", lambda: "")()
        radec = getattr(self.main, "target_radec", None)
        if not name and not radec:
            return None
        return (name or "target", *(radec or (None, None)))

    def out_dir(self):
        d = self.cfg.get("out_dir") or ""
        if not d:
            edit = getattr(self.main, "dir_edit", None)
            d = edit.text().strip() if edit is not None else ""
            d = d or getattr(getattr(self.main, "args", None), "record_dir", "") or "."
        return os.path.expanduser(d)

    def file_name(self):
        """<DSLR model>_<asteroid>_<star>: as the video camera's recordings (spaces as '_', the
        camera window's target); the photo's UTC and '_photo' follow (dslr.save_files)."""
        t = self.thread
        model = t.dslr.model if t is not None and t.dslr is not None else "DSLR"
        tag = getattr(self.main, "file_tag", lambda: "")()
        return "_".join(x for x in (re_safe(model), tag) if x)

    def photo_header(self):
        """A function shot -> FITS cards of the camera window's knowledge (site, target, time)."""
        t = self.thread
        info = self.header_info(t.dslr.model if t is not None and t.dslr is not None else "DSLR")
        target, software = self.target(), localconfig.software_version()
        return lambda shot: photo.header(info, shot.start_ns, shot.exposure_s, target=target,
                                         software=software)

    def on_photo(self):
        t = self.thread
        if t is None or t.dslr is None:
            self.error = "connect the DSLR first"
            return
        seconds = self.exp_spin.value()
        self.main.settings.setValue("dslr/exposure_s", seconds)
        header = self.photo_header()
        out_dir, name = self.out_dir(), self.file_name()

        def save(shot):                          # in the DSLR thread, off the GUI
            paths, img = ds.save(shot, out_dir, name, header(shot))
            sat = float(np.mean(img.data >= 0.98 * img.white)) if img.kind == "raw" else 0.0
            return {"paths": paths, "rgb": ds.preview_rgb(img), "kind": img.kind, "pattern": img.pattern,
                    "shape": img.data.shape, "saturated": sat, "shot": shot, "image": img}
        self.error = ""
        t.ask("photo", seconds, save)

    def on_live(self, on):
        if self.thread is not None:
            self.thread.live_view = on

    # ---- status ----
    def poll(self):
        t = self.thread
        connected = t is not None and t.dslr is not None
        state = "closed" if t is None else t.state
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.connect_button.setEnabled(state != "busy" or connected)
        self.camera_combo.setEnabled(not connected)
        self.find_button.setEnabled(not connected)
        busy = state in ("busy", "exposing", "program") or (t is not None and t.pending() > 0)
        self.photo_button.setEnabled(connected and not busy)
        self.live_check.setEnabled(connected)
        if connected:
            s = t.settings
            self.fill_settings(s)
            batt = f", battery {s['battery']}" if s.get("battery") else ""
            text = f"{t.dslr.model}{batt}"
            if state == "exposing" and t.exposing_until is not None:
                left = max(0.0, t.exposing_until - time.monotonic())
                text += f" · exposing, {left:.0f} s left" if left >= 1 else " · exposing, downloading"
                self.photo_button.setText("exposing…")
            elif state == "program":
                text += " · running a program (Program tab)"
                self.photo_button.setText("Photo")
            else:
                text += " · busy" if busy else ""
                self.photo_button.setText("Photo")
            self.shutter_label.setText(f"camera: {s.get('shutter')}" if s.get("shutter") else "")
        else:
            text = {"busy": "connecting…"}.get(state, "not connected")
            self.shutter_label.setText("")
        self.state_label.setText(text)
        if t is not None:
            if t.live_view and t.live is not None and t.live_count != self.shown_live:
                self.shown_live = t.live_count
                self.view.show_rgb(t.live)
            elif t.last is not None and t.shots != self.shown_shots and isinstance(t.last, dict):
                self.shown_shots = t.shots
                self.show_photo(t.last)
            if t.error:
                self.error = t.error
                t.error = ""
        pending = getattr(self, "pending_marks", None)
        if pending is not None:                  # the solve's markers onto the photo (GUI thread)
            self.pending_marks = None
            if not self.live_check.isChecked():
                self.view.set_marks(*pending)
        self.msg_label.setText("\n".join(t for t in (self.error, self.solve_log) if t))
        self.program.poll()

    def show_photo(self, res):
        if not self.live_check.isChecked():
            self.view.show_rgb(res["rgb"])
        shot = res["shot"]
        kind = (f"Bayer {res['pattern']}" if res["kind"] == "raw" and res["pattern"] else
                "raw, not Bayer" if res["kind"] == "raw" else "RGB (JPEG)")
        h, w = res["shape"][-2:]
        files = ", ".join(os.path.basename(p) for p in res["paths"])
        sat = f", {res['saturated'] * 100:.2f} % saturated" if res["saturated"] > 1e-4 else ""
        self.info_label.setText(f"{w} x {h} {kind}, {shot.exposure_s:g} s"
                                + (" (bulb)" if shot.bulb else "")
                                + f", ISO {shot.settings.get('iso')}{sat}\n{files}")
        print(f"DSLR photo: {', '.join(res['paths'])}")
        self.solution = None
        self.view.set_marks([], None)
        if self.solve_check.isChecked():
            self.start_solve(res)
        res.pop("image", None)                   # (85 MB: the solve has it now)

    # ---- plate solve: as the video camera's stack (photo_solve, marks) ----
    def start_solve(self, res):
        if self.solving is not None:
            self.solve_log = "a plate solve runs still: this photo later (photo_solve on its FITS)"
            return
        main = self.main
        fits_path = next(p for p in res["paths"] if p.endswith(".fits"))
        img, shape = res["image"], res["shape"]
        n_stars = getattr(getattr(main, "stars_spin", None), "value", lambda: 5)()
        n_marks = getattr(getattr(main, "marks_spin", None), "value", lambda: 5)()
        simbad = getattr(getattr(main, "simbad_check", None), "isChecked", lambda: True)()
        target = getattr(getattr(main, "target_edit", None), "text", lambda: "")().strip()
        verify = getattr(getattr(main, "verify_edit", None), "text", lambda: "")().strip()
        center = getattr(main, "solve_hint", lambda: None)()
        t0 = time.monotonic()

        def log(msg):
            self.solve_log = f"plate solve [{time.monotonic() - t0:.0f} s] {msg}"

        def work():
            try:
                from .photo_solve import solve_photo
                sol = solve_photo(fits_path, n_stars, n_marks, target, verify, simbad, center=center, log=log,
                                  img=img, timeout=float(self.cfg.get("solve_timeout_s") or 120.0))
                c = sol["center"]
                self.solution = sol
                self.solve_log = (f"solved: {c['ra_hms']} {c['dec_dms']}, {sol['field_deg'][0]:.2f} x "
                                  f"{sol['field_deg'][1]:.2f} deg, {sol['scale_arcsec']:.2f}\"/px, "
                                  f"{len(sol['marks'])} markers; WCS in the FITS, "
                                  f"{os.path.basename(sol['annotated'])}")
                if sol["notes"]:
                    self.solve_log += "\n" + "\n".join(sol["notes"])
                h, w = shape[-2:]
                self.pending_marks = ([(m["x"] / w, m["y"] / h, m["label"], m["kind"]) for m in sol["marks"]],
                                      _compass(sol))
            except Exception as e:
                self.solve_log = f"plate solve failed: {e}"
            self.solving = None
        self.solve_log = "plate solving…"
        self.solving = threading.Thread(target=work, daemon=True)
        self.solving.start()

    def shutdown(self):
        self.timer.stop()
        if self.program.running():
            self.program.runner.stop()
        if self.thread is not None:
            self.thread.close()
