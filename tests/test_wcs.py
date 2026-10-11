"""WCS: two-star solution and WCS-file target placement against a known TAN projection."""

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

from pystarlight.wcs import load_wcs, prepare_for_astrometry, sky_coord, solve_two_stars, target_pixel


def make_wcs(scale_arcsec=2.0, rot_deg=23.0, crval=(273.0, 22.0), crpix=(360, 240), mirrored=False):
    """Pixel y is the row index (down). Non-mirrored sky: east left with north up."""
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = crval
    w.wcs.crpix = crpix
    s = scale_arcsec / 3600
    r = np.radians(rot_deg)
    rot = np.array([[np.cos(r), -np.sin(r)], [np.sin(r), np.cos(r)]])
    w.wcs.cd = np.diag([-s, s if mirrored else -s]) @ rot
    return w


@pytest.mark.parametrize('mirrored', [False, True])
@pytest.mark.parametrize('rot', [0.0, 23.0, 160.0, -75.0])
def test_two_star_solution(rot, mirrored):
    w = make_wcs(rot_deg=rot, mirrored=mirrored)
    px = [(120.0, 80.0), (600.0, 400.0), (350.0, 260.0)]
    sky = [SkyCoord.from_pixel(x, y, w) for x, y in px]
    sol = solve_two_stars((sky[0], *px[0]), (sky[1], *px[1]), sky[2], mirrored=mirrored)
    assert sol.plate_scale == pytest.approx(2.0, rel=1e-3)
    # the flat two-star model is good to a fraction of a pixel over a small field
    assert sol.x == pytest.approx(px[2][0], abs=0.5)
    assert sol.y == pytest.approx(px[2][1], abs=0.5)


def test_two_star_wrong_parity_is_far_off():
    """Documents the PyMovie limitation: a mirrored image solved as non-mirrored."""
    w = make_wcs(mirrored=True)
    px = [(120.0, 80.0), (600.0, 400.0), (350.0, 260.0)]
    sky = [SkyCoord.from_pixel(x, y, w) for x, y in px]
    sol = solve_two_stars((sky[0], *px[0]), (sky[1], *px[1]), sky[2])
    assert np.hypot(sol.x - 350, sol.y - 260) > 20


def test_target_pixel_from_wcs_file(tmp_path):
    from astropy.io import fits
    w = make_wcs()
    path = tmp_path / 'wcs.fit'
    fits.PrimaryHDU(header=w.to_header()).writeto(path)
    target = SkyCoord.from_pixel(410.3, 199.7, w)
    x, y = target_pixel(load_wcs(str(path)), target)
    assert (x, y) == pytest.approx((410.3, 199.7), abs=1e-6)
    # solution computed on an image resized for 0.9 W/H pixels maps back to original x
    x2, _ = target_pixel(load_wcs(str(path)), target, pixel_aspect=0.9)
    assert x2 == pytest.approx(410.3 / 0.9)


def test_prepare_for_astrometry():
    rng = np.random.default_rng(0)
    img = rng.normal(300, 5, (480, 720)).astype(np.uint16)
    img[:20] = 4000
    out = prepare_for_astrometry(img, redact_top=20, pixel_aspect=0.9)
    assert out.shape == (480, 648)
    assert out[:20].max() < 400
    assert img[:20].min() == 4000          # input untouched


def test_sky_coord_formats():
    a = sky_coord('18h12m00s +22d00m00s')
    b = sky_coord('273.0 22.0')
    assert a.separation(b).arcsec < 1e-6


@pytest.mark.parametrize('mirrored', [False, True])
@pytest.mark.parametrize('rot', [0.0, 30.0, -88.7, 170.0])
def test_orientation(rot, mirrored):
    from pystarlight.wcs import orientation
    w = make_wcs(scale_arcsec=2.07, rot_deg=rot, mirrored=mirrored, crpix=(361, 241))
    o = orientation(w, (481, 721))
    assert o.scale_arcsec == pytest.approx(2.07, rel=1e-3)
    assert o.mirrored is mirrored
    if not mirrored:                    # north angle of a plain rotation equals the rotation
        assert (o.north_deg - rot + 180) % 360 - 180 == pytest.approx(0, abs=0.05)
    assert (o.center_ra, o.center_dec) == pytest.approx((273.0, 22.0), abs=1e-6)


def test_plate_scale():
    from pystarlight.wcs import plate_scale
    assert plate_scale(360, 5.02, 1456) == pytest.approx(1.975, abs=0.002)   # ASTRID IMX296 @ 360 mm


def test_pyoccult_sites(tmp_path, monkeypatch):
    from pystarlight import config
    f = tmp_path / 'sites.py'
    f.write_text("import os\nos.system('echo never runs')\n"
                 "sites = {'home': {'lat': 1.0, 'lon': 2.0, 'ele': 3.0, 'focal_mm': 360.0,"
                 " 'sensor_mm': (5.6, 3.2)}}\ndefault_site = 'home'\n")
    c = tmp_path / 'c.toml'
    c.write_text(f'pyoccult_sites = "{f}"\n[sites.home]\nsensor_mm = [5.02, 3.75]\n')
    monkeypatch.setenv('PYSTARLIGHT_CONFIG', str(c))
    s = config.site(config.load())
    assert s['id'] == 'home' and s['lat'] == 1.0 and s['sensor_mm'] == [5.02, 3.75]
