"""Plate solve of DSLR photos (capture/photo_solve.py, capture/marks.py): the solve and the
catalogue look-ups stubbed with a known TAN solution; the photo's FITS keeps its uint16 mosaic,
gets the WCS (astropy reads it back), the named stars, the target's pixel; the annotated PNG;
a second solve replaces the first; the DSLR window solves a photo and shows the markers."""

import json
import os
import time
from types import SimpleNamespace

import numpy as np
import pytest

astropy = pytest.importorskip("astropy")
from astropy.io import fits  # noqa: E402
from astropy.wcs import WCS  # noqa: E402

from pystarlight.capture import dslr as ds  # noqa: E402
from pystarlight.capture import marks as mk  # noqa: E402
from pystarlight.capture import photo  # noqa: E402
from pystarlight.capture import photo_solve as ps  # noqa: E402

H, W = 400, 600
RA0, DEC0, SCALE = 310.36, 45.28, 10.0 / 3600          # deg/px


def tan_wcs():
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA0, DEC0]
    w.wcs.crpix = [(W + 1) / 2, (H + 1) / 2]
    w.wcs.cd = [[-SCALE, 0], [0, SCALE]]
    return w


def sky_photo(tmp_path):
    """A photo FITS as dslr.save writes it: an RGGB mosaic with three stars."""
    rng = np.random.default_rng(1)
    m = (512 + rng.normal(0, 8, (H, W))).astype(np.float32)
    yy, xx = np.mgrid[:H, :W]
    stars = [(150.0, 100.0, 9000), (420.0, 260.0, 6000), (300.0, 200.0, 3000)]
    for x, y, a in stars:
        m += a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.5 ** 2))
    mosaic = np.clip(m, 0, 16383).astype(np.uint16)
    img = ds.Image(mosaic, "RGGB", 512.0, 16383.0, (2.0, 1.0, 1.5), 14, "raw")
    shot = ds.Shot([("DSC0001.ARW", b"x")], 1_795_000_000 * 10**9, 2.0, {"iso": "800", "shutter": "2"})
    cards = photo.header({"instrument": "Sony Alpha-A7r II", "latitude": 40.9, "longitude": -72.9},
                         shot.start_ns, 2.0, target=("TYC 3574-1718-1", RA0, DEC0))
    path = str(tmp_path / "Sony_Alpha-A7r_II_20261123T030000Z_photo.fits")
    from pystarlight.capture.stacker import write_fits
    write_fits(path, mosaic, ds.fits_cards(img, shot, cards))
    return path, mosaic, stars


@pytest.fixture
def stubs(monkeypatch, tmp_path):
    """solve_stack -> the TAN solution (its .wcs.fit written); catalogue names offline."""
    import pystarlight.wcs.catnames as cn
    import pystarlight.wcs.solve_stack as sst
    w = tan_wcs()

    def solve_stack(image, header, n_stars=5, workdir=".", dest=None, **kw):
        assert image.shape == (H, W) and image.dtype == np.float32
        assert kw["timeout"] > 0                               # a blind solve gives up
        fits.PrimaryHDU(header=w.to_header()).writeto(dest, overwrite=True)
        ra, dec = w.all_pix2world([[150.0, 100.0], [420.0, 260.0]], 0).T
        return {"wcs": dest, "width": W, "height": H, "scale_arcsec": 10.0, "north_deg": 0.0,
                "mirrored": False, "field_deg": [W * SCALE, H * SCALE], "time": header.get("DATE-OBS"),
                "center": {"ra": RA0, "dec": DEC0, "ra_hms": "20h41m26.4s", "dec_dms": "+45d16m48s"},
                "stars": [{"x": 150.0, "y": 100.0, "ra": ra[0], "dec": dec[0], "fwhm": 3.5, "name": ""},
                          {"x": 420.0, "y": 260.0, "ra": ra[1], "dec": dec[1], "fwhm": 3.5, "name": ""}]}
    monkeypatch.setattr(sst, "solve_stack", solve_stack)
    monkeypatch.setattr(cn, "catalog_name", lambda ra, dec, r, epoch, log=None:
                        {"name": f"TYC {ra:.2f}", "mag": 8.1})
    monkeypatch.setattr(cn, "resolve_target", lambda q, epoch, log=None:
                        {"name": "TYC 3574-1718-1", "ra": RA0, "dec": DEC0, "mag": 10.2, "band": "V",
                         "ids": {"TYC": "TYC 3574-1718-1"}})
    monkeypatch.setenv("PYSTARLIGHT_CONFIG", "none")


def test_solve_writes_wcs_stars_and_target_into_the_photo(tmp_path, stubs):
    path, mosaic, stars = sky_photo(tmp_path)
    sol = ps.solve_photo(path, n_stars=5, n_marks=5, target="TYC 3574-1718-1", log=lambda m: None)
    data, hdr = fits.getdata(path, header=True)
    assert data.dtype.kind in "iu" and np.array_equal(data, mosaic)          # the mosaic, untouched
    assert hdr["BAYERPAT"] == "RGGB" and hdr["PLTSOLVD"] and hdr["PIXSCALE"] == 10.0
    w = WCS(hdr)
    ra, dec = w.all_pix2world([[(W - 1) / 2, (H - 1) / 2]], 0)[0]
    assert ra == pytest.approx(RA0, abs=1e-6) and dec == pytest.approx(DEC0, abs=1e-6)
    assert hdr["STAR1"].startswith("TYC ") and hdr["OBJCTRA"] and hdr["OBJECT"] == "TYC 3574-1718-1"
    assert hdr["TRGX"] == pytest.approx((W - 1) / 2, abs=0.01)               # the target at the centre
    kinds = [m["kind"] for m in sol["marks"]]
    assert kinds == ["star", "star", "target"]
    assert any("detected in the stack" in n for n in sol["notes"])           # a star there (3rd one)
    assert os.path.exists(sol["annotated"]) and os.path.exists(path.replace(".fits", ".solve.json"))
    assert json.load(open(path.replace(".fits", ".solve.json")))["marks"][2]["kind"] == "target"
    import cv2
    png = cv2.imread(sol["annotated"])
    assert png.shape[1] == W and png.shape[2] == 3

    # solved again: one set of cards, not two
    ps.solve_photo(path, target="TYC 3574-1718-1", log=lambda m: None)
    hdr = fits.getheader(path)
    assert list(hdr.keys()).count("CRVAL1") == 1 and list(hdr.keys()).count("STAR1") == 1
    assert np.array_equal(fits.getdata(path), mosaic)


def test_detect_text_finds_or_misses_a_star():
    img = np.zeros((200, 200), np.float32) + np.random.default_rng(0).normal(0, 1, (200, 200))
    yy, xx = np.mgrid[:200, :200]
    img += 50 * np.exp(-((xx - 100) ** 2 + (yy - 98) ** 2) / 4.0)
    assert mk.detect_text(img, 2.0, 100, 100).startswith("detected in the stack")
    assert mk.detect_text(img, 2.0, 60, 60).startswith("NO star detected")


def test_dslr_window_solves_a_photo_and_shows_the_markers(monkeypatch, tmp_path, stubs):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from pystarlight.capture import dslr_gui as dg
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])  # noqa: F841
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    path, mosaic, _ = sky_photo(tmp_path)
    img, _, _ = ps.read_photo(path)
    main = QtWidgets.QWidget()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(gps=None)
    main.target_edit = QtWidgets.QLineEdit("TYC 3574-1718-1")
    main.solve_hint = lambda: (RA0, DEC0)
    w = dg.DslrWindow(main, thread=SimpleNamespace(state="closed", dslr=None, settings={}, pending=lambda: 0,
                                                   live_view=False, live=None, last=None, shots=0, error="",
                                                   close=lambda: None))
    w.timer.stop()
    res = {"paths": [path.replace(".fits", ".arw"), path], "rgb": ds.preview_rgb(img), "kind": "raw",
           "pattern": "RGGB", "shape": mosaic.shape, "saturated": 0.0, "image": img,
           "shot": ds.Shot([], 0, 2.0, {"iso": "800"})}
    w.show_photo(res)
    end = time.monotonic() + 10
    while w.solving is not None and time.monotonic() < end:
        time.sleep(0.02)
    w.poll()
    assert w.solution is not None, w.solve_log
    assert "solved:" in w.msg_label.text() and "WCS in the FITS" in w.msg_label.text()
    assert [m[3] for m in w.view.marks] == ["star", "star", "target"] and w.view.compass
    assert "image" not in res                                           # not kept in the window
    assert WCS(fits.getheader(path)).has_celestial
    w.shutdown()


def test_dslr_pixel_pitch_for_the_scale_hint():
    assert ds.pixel_um_of("Sony Alpha-A7r II") == 4.51 and ds.pixel_um_of("Nikon DSC D7000") == 4.78
    assert ds.pixel_um_of("Canon EOS 6D") is None


@pytest.mark.parametrize("kappa", [3.0, 0.0])
def test_stack_aligns_by_the_solutions_and_clips_a_hot_pixel(tmp_path, stubs, kappa):
    """Five frames drifting over the sky (each its own TAN solution), a hot pixel fixed on the
    sensor: the stack has the stars in place, the hot pixel clipped."""
    from pystarlight.capture import photo_stack as pst
    from pystarlight.capture.stacker import write_fits
    yy, xx = np.mgrid[:H, :W]
    sky_stars = [(150.0, 100.0, 9000), (420.0, 260.0, 6000), (300.0, 200.0, 3000)]   # in frame 0
    paths = []
    for i in range(5):
        dx, dy = 7.0 * i, -5.0 * i                       # the drift: the sky moves on the sensor
        m = 512 + np.random.default_rng(i).normal(0, 8, (H, W)).astype(np.float32)
        for x, y, a in sky_stars:
            m += a * np.exp(-((xx - x - dx) ** 2 + (yy - y - dy) ** 2) / (2 * 1.5 ** 2))
        m[50, 500] = 16000                               # a hot pixel, the same on every frame
        w = tan_wcs()
        w.wcs.crpix = [w.wcs.crpix[0] + dx, w.wcs.crpix[1] + dy]
        cards = [(k, w.to_header()[k], "") for k in w.to_header()]
        cards += [("BAYERPAT", "RGGB"), ("BLKLEVEL", 512.0), ("SATURATE", 16383.0), ("BITDEPTH", 14),
                  ("PLTSOLVD", True), ("EXPTIME", 10.0),
                  ("DATE-OBS", f"2017-09-08T02:53:{10 + 10 * i:02d}.000")]
        p = str(tmp_path / f"DSC0221{i}_photo.fits")
        write_fits(p, np.clip(m, 0, 16383).astype(np.uint16), cards)
        paths.append(p)
    logged = []
    out, sol = pst.stack(paths, str(tmp_path), kappa=kappa, log=logged.append, target="TYC 3574-1718-1")
    if kappa:                                            # the noise itself is (nearly) never clipped
        line = next(m for m in logged if "clipping left out" in m)
        assert float(line.split("left out")[1].split("%")[0]) < 0.5, line
    data, hdr = fits.getdata(out, header=True)
    assert data.shape == (3, H, W) and hdr["NCOMBINE"] == 5 and hdr["EXPTIME"] == 50.0
    assert hdr["DATE-OBS"].startswith("2017-09-08T02:53:10") and hdr["PLTSOLVD"]
    lum = data.sum(axis=0)
    for x, y, _ in sky_stars:                            # the stars where frame 0 has them, sharp
        j, i = np.unravel_index(np.argmax(lum[int(y) - 6:int(y) + 7, int(x) - 6:int(x) + 7]), (13, 13))
        assert (abs(i - 6), abs(j - 6)) <= (1, 1)
    hot = [(50 - 5 * k * -1, 500 - 7 * k) for k in range(5)]   # where the hot pixel lands (frame k)
    bg = float(np.median(lum))
    noise = 1.4826 * float(np.median(np.abs(lum - bg)))
    peaks = [lum[y - 1:y + 2, x - 1:x + 2].max() - bg for y, x in hot]
    if kappa:                                            # clipped: sky there, not a ghost star
        assert max(peaks) < 6 * noise, (peaks, noise)
    else:                                                # a plain mean keeps them (the test can tell)
        assert min(peaks) > 20 * noise
    assert os.path.exists(sol["annotated"])
