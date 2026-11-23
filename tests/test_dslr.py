"""DSLR as a photo camera (capture/dslr.py, dslr_gui.py) against a fake gphoto2: shutter speed /
ISO choices, bulb timed here, RAW + JPEG files downloaded, the FITS (Bayer mosaic uint16 with
BAYERPAT, or RGB planes from a JPEG), the camera thread, and the DSLR window (offscreen Qt)."""

import csv
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from pystarlight.capture import dslr as ds
from pystarlight.capture import photo


class GPhoto2Error(Exception):
    def __init__(self, code):
        super().__init__(f"[{code}]")
        self.code = code


class Widget:
    def __init__(self, value, choices=()):
        self.value, self.choices_ = value, list(choices)

    def get_value(self):
        return self.value

    def set_value(self, v):
        self.value = v

    def count_choices(self):
        return len(self.choices_)

    def get_choice(self, i):
        return self.choices_[i]


class Config:
    def __init__(self, widgets):
        self.widgets = widgets

    def get_child_by_name(self, name):
        if name not in self.widgets:
            raise GPhoto2Error(-2)
        return self.widgets[name]


def jpeg(h=60, w=80):
    img = np.zeros((h, w, 3), np.uint8)
    img[..., 2] = 200                                   # red (BGR)
    cv2.circle(img, (40, 30), 3, (255, 255, 255), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


class FakeCamera:
    """A Canon-like camera: shutter speeds to 30 s and Bulb, remote release for bulb."""

    def __init__(self, files=("IMG_0001.JPG",)):
        self.widgets = {
            "iso": Widget("400", ["Auto", "100", "200", "400", "800", "1600", "3200",
                                  "800 Multi Frame Noise Reduction"]),
            "shutterspeed": Widget("1/60", ["bulb", "30", "15", "2.5", "1", "0.5", "1/30", "1/60", "1/4000"]),
            "aperture": Widget("implicit auto", ["implicit auto"]),
            "imageformat": Widget("RAW", ["RAW", "RAW + Large Fine JPEG", "Large Fine JPEG"]),
            "capturetarget": Widget("Internal RAM", ["Internal RAM", "Memory card"]),
            "batterylevel": Widget("75%"),
            "cameramodel": Widget("Canon EOS 6D"),
            "eosremoterelease": Widget("None", ["None", "Press Full", "Release Full"]),
        }
        self.files, self.events, self.log = list(files), [], []
        self.data = {}

    def init(self):
        pass

    def exit(self):
        self.log.append("exit")

    def get_config(self):
        return Config(self.widgets)

    def set_config(self, cfg):
        self.log.append(("set", {k: w.value for k, w in self.widgets.items()}))
        if self.widgets["eosremoterelease"].value == "Release Full":
            self.events += [(1, SimpleNamespace(folder="/", name=f)) for f in self.files]

    def capture(self, kind):
        self.log.append(("capture", self.widgets["shutterspeed"].value))
        self.events += [(1, SimpleNamespace(folder="/", name=f)) for f in self.files[1:]]
        return SimpleNamespace(folder="/", name=self.files[0])

    def wait_for_event(self, ms):
        return self.events.pop(0) if self.events else (0, None)

    def file_get(self, folder, name, kind):
        data = self.data.get(name) or (jpeg() if name.lower().endswith(".jpg") else b"RAWDATA")
        return SimpleNamespace(get_data_and_size=lambda: data)

    def capture_preview(self):
        return SimpleNamespace(get_data_and_size=lambda: jpeg(30, 40))


def fake_gp(camera):
    return SimpleNamespace(Camera=lambda: camera, GPhoto2Error=GPhoto2Error, GP_CAPTURE_IMAGE=0,
                           GP_FILE_TYPE_NORMAL=1, GP_EVENT_TIMEOUT=0, GP_EVENT_FILE_ADDED=1,
                           GP_EVENT_CAPTURE_COMPLETE=3)


def test_choices_as_numbers():
    assert ds.shutter_seconds("1/4000") == pytest.approx(1 / 4000)
    assert ds.shutter_seconds('30"') == 30 and ds.shutter_seconds("2.5s") == 2.5
    assert ds.shutter_seconds("0,3") == pytest.approx(0.3)
    assert ds.shutter_seconds("Bulb") is None and ds.iso_value("Auto") is None
    assert ds.iso_value("ISO 1600") == 1600
    assert ds.nearest(["1/30", "1/60", "1"], 0.03, ds.shutter_seconds) == "1/30"
    assert ds.raw_choice(["Large Fine JPEG", "RAW + Large Fine JPEG", "RAW"]) == "RAW"
    assert ds.raw_choice(["NEF (Raw)", "NEF+Fine"]) == "NEF (Raw)"


def test_exposure_nearest_or_bulb():
    cam = FakeCamera()
    d = ds.Dslr(g=fake_gp(cam), clock=lambda: 1_700_000_000 * 10**9)
    assert d.model == "Canon EOS 6D" and d.timed_max_s() == 30 and d.has_bulb()
    assert d.set_exposure(0.04) == ("1/30", False)
    assert d.set_exposure(120.0) == ("bulb", True)
    assert d.set_iso(1000) == "800"
    assert ds.Dslr("Sony Alpha-A7r II (Control)", g=fake_gp(cam)).model == "Sony Alpha-A7r II"
    s = d.settings()
    assert s["battery"] == "75%" and s["choices"]["format"][0] == "RAW"


def test_bulb_is_timed_here(tmp_path):
    cam = FakeCamera(files=("IMG_0002.JPG",))
    d = ds.Dslr(g=fake_gp(cam), clock=lambda: 1_700_000_000 * 10**9)
    t0 = time.monotonic()
    shot = d.capture(0.3)                              # timed: no bulb below 30 s
    assert not shot.bulb and shot.exposure_s == pytest.approx(0.5)
    d.timed_max_s = lambda: 0.1                        # a camera whose longest timed speed is short
    shot = d.capture(0.3)
    assert shot.bulb and shot.exposure_s == 0.3 and time.monotonic() - t0 >= 0.3
    releases = [e[1]["eosremoterelease"] for e in cam.log if e[0] == "set" and "eosremoterelease" in e[1]]
    assert "Press Full" in releases and releases[-1] == "Release Full"
    assert shot.files[0][0] == "IMG_0002.JPG"


def test_jpeg_photo_to_rgb_fits(tmp_path):
    from astropy.io import fits
    cam = FakeCamera(files=("IMG_0003.JPG",))
    d = ds.Dslr(g=fake_gp(cam), clock=lambda: 1_700_000_000 * 10**9)
    shot = d.capture(1.0)
    cards = photo.header({"instrument": "Canon EOS 6D", "latitude": 40.9, "longitude": -72.9},
                         shot.start_ns, shot.exposure_s, target=("HD 1", 10.0, 20.0))
    paths, img = ds.save(shot, str(tmp_path), "Canon-EOS-6D", cards)
    assert [os.path.splitext(p)[1] for p in paths] == [".jpg", ".fits"]
    data, hdr = fits.getdata(paths[1], header=True)
    assert data.shape == (3, 60, 80) and data[0].mean() > 150 > data[2].mean()     # red
    assert hdr["DATE-OBS"].startswith("2023-11-14T22:13:20") and hdr["OBJECT"] == "HD 1"
    assert hdr["ISOSPEED"] == "400" and "BAYERPAT" not in hdr and hdr["ROWORDER"] == "TOP-DOWN"
    assert ds.preview_rgb(img).shape == (60, 80, 3)


def test_raw_plus_jpeg_to_bayer_fits(tmp_path, monkeypatch):
    from astropy.io import fits
    cam = FakeCamera(files=("IMG_0004.CR2", "IMG_0004.JPG"))
    mosaic = (np.arange(40 * 60, dtype=np.uint16).reshape(40, 60) * 7) % 16000
    monkeypatch.setattr(ds, "decode", lambda name, data: ds.Image(mosaic, "RGGB", 2048.0, 15000.0,
                                                                  (2.0, 1.0, 1.5), 14, "raw"))
    d = ds.Dslr(g=fake_gp(cam), clock=lambda: 1_700_000_000 * 10**9)
    shot = d.capture(2.0)
    assert [n for n, _ in shot.files] == ["IMG_0004.CR2", "IMG_0004.JPG"]
    assert shot.raw_file()[0] == "IMG_0004.CR2"
    paths, img = ds.save(shot, str(tmp_path), "Canon-EOS-6D", photo.header({}, shot.start_ns, 2.0))
    assert [os.path.splitext(p)[1] for p in paths] == [".cr2", ".jpg", ".fits"]
    data, hdr = fits.getdata(paths[2], header=True)
    assert data.dtype.kind in "iu" and np.array_equal(data, mosaic)
    assert hdr["BAYERPAT"] == "RGGB" and hdr["BLKLEVEL"] == 2048 and hdr["BITDEPTH"] == 14
    assert hdr["CAMFILE"] == "IMG_0004.CR2" and hdr["SHUTTER"] == "2.5"
    rgb = ds.preview_rgb(img, width=30)
    assert rgb.shape == (20, 30, 3) and rgb.dtype == np.uint8


def test_thread_connects_takes_photos_and_live_view(tmp_path):
    cam = FakeCamera()
    t = ds.DslrThread(factory=lambda *a, clock: ds.Dslr(g=fake_gp(cam), clock=clock))
    t.ask("connect", "Canon EOS 6D", "usb:001,005")
    t.ask("set", "iso", "1600")
    saved = []
    t.ask("photo", 0.5, lambda shot: saved.append(shot) or "ok")
    end = time.monotonic() + 5
    while (t.shots < 1 or t.state != "idle") and time.monotonic() < end:
        time.sleep(0.02)
    assert t.last == "ok" and saved[0].settings["iso"] == "1600" and t.settings["iso"] == "1600"
    t.live_view = True
    while t.live_count < 2 and time.monotonic() < end:
        time.sleep(0.02)
    assert t.live.shape == (30, 40, 3)
    t.live_view = False
    t.close()
    assert "exit" in cam.log


def test_dslr_window(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from pystarlight.capture import dslr_gui as dg
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    monkeypatch.setattr(dg.ds, "find_dslrs", lambda: [("Canon EOS 6D", "usb:001,005")])
    monkeypatch.setattr(dg.ds, "available", lambda: "")
    main = QtWidgets.QWidget()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(gps=None)
    main.dir_edit = QtWidgets.QLineEdit(str(tmp_path / "rec"))
    main.target_name = lambda: "TYC 3574-1718-1"
    main.target_radec = (310.1, 45.2)
    main.file_tag = lambda: "476_TYC-3574-1718-1"          # as the recordings: <asteroid>_<star>
    cam = FakeCamera(files=("IMG_0005.JPG",))
    thread = ds.DslrThread(factory=lambda *a, clock: ds.Dslr(g=fake_gp(cam), clock=clock))
    w = dg.DslrWindow(main, thread=thread)
    w.timer.stop()
    w.find()
    assert w.camera_combo.currentData() == ("Canon EOS 6D", "usb:001,005")
    thread.state = "closed"
    w.on_connect()

    def until(cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            app.processEvents()
            w.poll()
            time.sleep(0.02)
        return cond()
    assert until(lambda: thread.dslr is not None and thread.state == "idle")
    assert w.combos["iso"].currentText() == "400" and "battery 75%" in w.state_label.text()
    w.combos["iso"].setCurrentIndex(w.combos["iso"].findText("3200"))
    w.on_setting("iso")
    assert until(lambda: thread.settings.get("iso") == "3200")
    w.exp_spin.setValue(1.0)
    w.solve_check.setChecked(False)                    # no real plate solve here (test_photo_solve)
    w.on_photo()
    assert until(lambda: thread.shots == 1 and thread.state == "idle" and "IMG_0005" not in w.error
                 and w.info_label.text())
    files = sorted(os.listdir(tmp_path / "rec"))
    assert len(files) == 2 and files[1].endswith("_photo.jpg")
    assert re.fullmatch(r"Canon_EOS_6D_476_TYC-3574-1718-1_\d{8}T\d{6}Z_photo\.fits", files[0]), files
    assert "RGB (JPEG)" in w.info_label.text() and "ISO 3200" in w.info_label.text()
    from astropy.io import fits
    hdr = fits.getheader(tmp_path / "rec" / files[0])
    assert hdr["OBJECT"] == "TYC 3574-1718-1" and hdr["INSTRUME"] == "Canon EOS 6D"
    w.set_night(True)
    w.shutdown()


def test_program_tab_edits_saves_and_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from pystarlight.capture import dslr_gui as dg
    from pystarlight.capture import schedule as sc
    from pystarlight.capture import schedule_gui as sg
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    monkeypatch.setattr(dg.ds, "available", lambda: "")
    monkeypatch.setattr(dg.ds, "find_dslrs", lambda: [("Canon EOS 6D", "usb:001,005")])
    main = QtWidgets.QWidget()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(gps=None)
    main.dir_edit = QtWidgets.QLineEdit(str(tmp_path / "rec"))
    main.file_tag = lambda: "TYC-3574-1718-1"
    cam = FakeCamera(files=("IMG_0006.JPG",))
    thread = ds.DslrThread(factory=lambda *a, clock: ds.Dslr(g=fake_gp(cam), clock=clock))
    w = dg.DslrWindow(main, thread=thread)
    w.timer.stop()
    panel = w.program
    assert len(panel.prog.phases) == 7 and panel.tree.topLevelItemCount() == 7     # the default eclipse

    data = Path(__file__).parent / "data" / "sofimagic_nikon_d7000.xml"
    panel.open(str(data))
    panel.update_timeline()
    assert panel.tree.topLevelItemCount() == 9 and panel.slider.maximum() == 8
    assert panel.phase_count.text() == "1 / 9" and panel.c0_label.text().startswith("C0 12:33:57")
    assert [n.text() for n, _ in panel.time_cols] == ["C1", "C2", "C3", "C4"]
    panel.slider.setValue(2)
    assert panel.phase_name.text() == "Contact2" and panel.grid.columnCount() == 1
    panel.slider.setValue(0)
    assert [panel.grid.item(r, 0).text() for r in range(5)] == ["S", "", "100", "4", "1/1600"]
    panel.grid.item(2, 0).setText("200")                   # ISO of the first exposure
    assert panel.prog.phases[0].exposures[0].iso == 200
    panel.grid.item(4, 1).setText("1/7")                   # not a shutter speed -> 1/7 is read (any n/d)
    panel.grid.item(0, 2).setText("X")                     # a drive mode that is not
    assert panel.grid.item(0, 2).background().color().name() == "#ffcdd2" and "exposure 3" in panel.error
    panel.grid.item(0, 2).setText("S")
    assert len(panel.prog.phases[0].exposures) == 3
    panel.base_combo.setCurrentIndex(1)                    # local: the same instants, shown locally
    assert panel.collect().times["C2"] == sc.load(str(data), day=sc.today()).times["C2"]
    panel.base_combo.setCurrentIndex(0)
    panel.path = str(tmp_path / "eclipse.xml")
    panel.on_save()
    back = sc.load(panel.path, day=sc.today())
    assert back.phases[0].exposures[0].iso == 200 and len(back.phases) == 9
    panel.path = str(tmp_path / "eclipse.toml")
    panel.on_save()
    assert sc.load(panel.path).phases[0].exposures[0].iso == 200

    # Start: as programmed / the program / a reference in 15 s; a fast-forward dry run, its list
    panel.update_timeline()
    starts = [panel.start_combo.itemData(i) for i in range(panel.start_combo.count())]
    assert starts[:2] == [None, ""] and "C2" in starts and "C0" in starts
    panel.start_combo.setCurrentIndex(panel.start_combo.findData("C2"))
    panel.on_dry_run(fast=True)
    w.poll()
    assert panel.dry_path.endswith("_SoFiMagic-eclipse.md") and os.path.exists(panel.dry_path)
    assert "dry run, done" in panel.status_label.text() and "<a href=" in panel.status_label.text()
    assert "Partial1" not in {r["phase"] for r in panel.runner.records}

    # a short sequence, run on the (fake) camera
    seq = sc.sequence(3, 0.3, [sc.parse_exposure("1/60 ISO400")], start_in_s=0.2, files="files")
    panel.set_program(seq)
    w.find()
    thread.state = "closed"
    w.on_connect()
    end = time.monotonic() + 5
    while thread.dslr is None or thread.state != "idle":
        app.processEvents()
        time.sleep(0.02)
        assert time.monotonic() < end
    panel.on_run()
    while panel.runner is None or panel.running() or thread.state == "program":
        app.processEvents()
        w.poll()
        time.sleep(0.02)
        assert time.monotonic() < end + 10
    r = panel.runner
    assert r.state == "done" and r.done == 3, (r.errors, panel.status_label.text())
    photos = sorted(p for p in os.listdir(tmp_path / "rec") if p.endswith(".jpg"))
    assert len(photos) == 3 and all(p.startswith("Canon_EOS_6D_TYC-3574-1718-1_") for p in photos)
    w.poll()
    assert "done" in panel.status_label.text() and "dry run" not in panel.status_label.text()
    assert re.fullmatch(r"Canon_EOS_6D_TYC-3574-1718-1_\d{8}T\d{6}Z_sequence_photolog\.csv",
                        os.path.basename(r.log_path))
    assert sg.TEST_LEAD_S == 15.0
    w.shutdown()


def test_camera_lists_sorted_in_one_notation():
    pytest.importorskip("pystarlight.capture.qt", exc_type=ImportError)
    from pystarlight.capture.schedule_gui import camera_list
    sony = ["30", "25", "4", "32/10", "25/10", "2", "16/10", "13/10", "1", "8/10", "5/10", "1/3",
            "1/1600", "1/32000", "Bulb"]                 # as the A7R II lists them (gphoto2)
    assert camera_list("shutter", sony) == ["1/32000", "1/1600", "1/3", "1/2", "0.8", "1", "1.3", "1.6",
                                            "2", "2.5", "3.2", "4", "25", "30"]
    isos = ["Auto ISO", "640", "100", "12800 Multi Frame Noise Reduction+"]
    assert camera_list("iso", isos) == ["100", "640"]
    assert camera_list("aperture", ["f/0", "f/22", "f/1.4", "implicit auto"]) == ["1.4", "22"]


class NoLensCamera(FakeCamera):
    """As the A7R II with a telescope: lists f-numbers, ignores setting one (stays f/0)."""

    def __init__(self, files=("IMG_0007.JPG",)):
        super().__init__(files)
        self.widgets["aperture"] = Widget("f/0", ["f/0", "f/4", "f/5.6", "f/8"])

    def set_config(self, cfg):
        self.widgets["aperture"].value = "f/0"
        super().set_config(cfg)


def test_an_ignored_aperture_is_a_warning_and_the_log_tells_the_truth(tmp_path):
    from pystarlight.capture import schedule as sc
    cam = NoLensCamera()
    d = ds.Dslr(g=fake_gp(cam), clock=time.time_ns)
    pc = ds.ProgramCamera(d, str(tmp_path), "Sony_Alpha-A7r_II")
    prog = sc.sequence(2, 0.2, [sc.parse_exposure("1/60 ISO800 f/5.6")], start_in_s=0.05).at(time.time_ns())
    r = sc.Runner(prog, pc, time.time_ns, log_dir=str(tmp_path), name="Sony_Alpha-A7r_II")
    assert r.run() == "done" and r.done == 2 and not r.errors          # the photos are taken
    assert any("f/5.6 not taken" in w and "f/0" in w for w in d.warnings) and len(d.warnings) == 1
    rows = list(csv.reader(open(r.log_path)))
    head = rows[0]
    assert rows[1][head.index("fnumber")] == "5.6"                       # as programmed
    assert rows[1][head.index("camera_settings")] == "1/60 ISO800 f/0"    # as the camera had it
