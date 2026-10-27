"""Live stacking (alignment, FITS) and the stack plate solve (astrometry.net call faked)."""
import time

import numpy as np
import pytest

from pystarlight.capture import stacker
from pystarlight.wcs import solve_stack

H, W = 300, 400
STARS = [(60.3, 50.2, 3000), (300.7, 80.4, 2000), (200.1, 220.6, 5000), (90.5, 250.3, 1500),
         (350.2, 200.8, 2500), (150.6, 140.1, 1000), (250.4, 30.9, 1800)]


def field(dx=0.0, dy=0.0, noise=2.0, seed=0):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[:H, :W]
    img = 100 + rng.normal(0, noise, (H, W))
    for sx, sy, a in STARS:
        img += a * np.exp(-((x - sx - dx) ** 2 + (y - sy - dy) ** 2) / (2 * 1.3 ** 2))
    img[H - 60:, :120] = 30                     # static dark "tree"
    return img.astype(np.float32)


@pytest.mark.parametrize("dx, dy", [(4.6, -3.2), (-9.0, 6.4)])
def test_alignment_measures_drift(dx, dy):
    ref, cur = stacker.star_map(field()), stacker.star_map(field(dx, dy, seed=1))
    import cv2
    win = cv2.createHanningWindow(ref.shape[::-1], cv2.CV_32F)
    mx, my, resp = stacker.measure_shift(ref, cur, win)
    assert abs(mx - dx) < 0.6 and abs(my - dy) < 0.6 and resp > 0.1
    back, weight = stacker.shift_image(field(dx, dy, noise=0), mx, my)
    sx, sy, _ = STARS[2]
    py, px = np.unravel_index(np.argmax(back * (weight > 0.99)), back.shape)
    assert abs(px - sx) <= 1 and abs(py - sy) <= 1


def test_write_fits_round_trip(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    img = field()
    path = str(tmp_path / "s.fits")
    stacker.write_fits(path, img, [("DATE-OBS", "2026-10-26T23:55:26.713", "UTC"),
                                   ("EXPTIME", 12.8), ("NCOMBINE", 16), ("ALIGNED", True),
                                   ("INSTRUME", "VEYE O'Cam")])
    with fits.open(path) as h:
        np.testing.assert_array_equal(h[0].data, img)
        hd = h[0].header
        assert hd["DATE-OBS"] == "2026-10-26T23:55:26.713" and hd["EXPTIME"] == 12.8
        assert hd["NCOMBINE"] == 16 and hd["ALIGNED"] is True and hd["INSTRUME"] == "VEYE O'Cam"


def test_live_stacker_thread(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    frames = [field(0.5 * i, -0.3 * i, seed=i) for i in range(6)]
    st = stacker.LiveStacker(lambda raw: raw, str(tmp_path), "cam",
                             lambda: {"pixel_um": 2.4, "focal_mm": 24.0, "latitude": 40.9,
                                      "longitude": -72.9, "instrument": "test"})
    st.begin(5, align=True)
    t0 = 1_792_000_000 * 10**9
    for i, f in enumerate(frames):
        deadline = time.monotonic() + 5
        while not st.wants() and st.state == "stacking" and time.monotonic() < deadline:
            time.sleep(0.005)
        if st.state != "stacking":
            break
        st.put(f, t0 + i * 10**9, {"exposure_ns": 800_000_000})
    deadline = time.monotonic() + 10
    while st.state == "stacking" and time.monotonic() < deadline:
        time.sleep(0.01)
    st.close()
    assert st.state == "done" and st.count == 5
    assert abs(st.shift[0] - 2.0) < 0.6 and abs(st.shift[1] + 1.2) < 0.6
    with fits.open(st.path) as h:
        hd = h[0].header
        assert hd["NCOMBINE"] == 5 and hd["FOCALLEN"] == 24.0 and hd["XPIXSZ"] == 2.4
        assert hd["DATE-OBS"] == "2026-10-14T17:46:40.000" and abs(hd["EXPTIME"] - 4.0) < 1e-6
        assert hd["SITELAT"] == 40.9
        sx, sy, _ = STARS[2]                    # stack aligned to the first frame
        cut = h[0].data[round(sy) - 4:round(sy) + 5, round(sx) - 4:round(sx) + 5] - 100.0
        yy, xx = np.mgrid[-4:5, -4:5]
        cx, cy = (cut * xx).sum() / cut.sum() + round(sx), (cut * yy).sum() / cut.sum() + round(sy)
        assert abs(cx - sx) < 0.5 and abs(cy - sy) < 0.5


def test_solve_stack_with_faked_astrometry(tmp_path, monkeypatch):
    fits = pytest.importorskip("astropy.io.fits")
    from astropy.wcs import WCS
    monkeypatch.setenv("PYSTARLIGHT_CONFIG", "none")
    # truth: TAN projection, 20.63"/px, north up, east left, centre RA 310 Dec +45
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [(W + 1) / 2, (H + 1) / 2]
    w.wcs.crval = [310.0, 45.0]
    s = 20.63 / 3600
    w.wcs.cd = [[-s, 0], [0, -s]]            # row 0 at the top: dec decreases downwards
    wcs_file = tmp_path / "truth.wcs.fit"
    fits.PrimaryHDU(header=w.to_header()).writeto(wcs_file)
    sent = {}

    def fake_solve(image, **kw):
        sent.update(kw, shape=image.shape)
        return str(wcs_file), None

    monkeypatch.setattr(solve_stack, "solve_astrometry_net", fake_solve)
    path = tmp_path / "cam_stack16.fits"
    stacker.write_fits(str(path), field(), [("DATE-OBS", "2026-10-27T00:00:00.000"),
                                            ("XPIXSZ", 2.4), ("FOCALLEN", 24.0),
                                            ("SITELAT", 40.87), ("SITELONG", -72.86)])
    out = solve_stack.solve_fits(str(path), n_stars=3, use_simbad=False, log=lambda m: None)
    assert sent["scale_arcsec"] == pytest.approx(20.63, abs=0.01)
    assert sent["downsample"] is None                       # narrow frame
    assert out["center"]["ra"] == pytest.approx(310.0, abs=0.01)
    assert out["center"]["dec"] == pytest.approx(45.0, abs=0.01)
    assert out["center"]["ra_hms"].startswith("20h40m00")
    assert out["scale_arcsec"] == pytest.approx(20.63, rel=0.01)
    assert out["field_deg"][0] == pytest.approx(W * 20.63 / 3600, rel=0.01)
    assert not out["mirrored"] and abs(out["north_deg"]) < 0.5
    assert 0 < out["altaz"]["alt"] < 90 and out["site"]["source"] == "GPS"
    xy = sorted((round(st["x"]), round(st["y"])) for st in out["stars"])
    assert xy == sorted((round(x), round(y)) for x, y, _ in sorted(STARS, key=lambda t: -t[2])[:3])
    assert (tmp_path / "cam_stack16.solve.json").exists()
    assert "Centre  RA 20h40m00" in solve_stack.describe(out)
