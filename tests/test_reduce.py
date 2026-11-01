"""PRAIA-style reduction against a (made-up) catalogue: synthetic fields with known distortion."""

import math

import numpy as np
import pytest

from pystarlight.wcs import reduce as R

RA0, DEC0, SCALE = 310.3, 45.1, 2.07            # deg, deg, arcsec/px
W, H = 400, 300


class FakeCatalog:
    """Stands in for localcat.LocalGaia: cone() and lookup() on a fixed star list."""

    def __init__(self, stars):
        self.stars = stars

    def cone(self, ra, dec, radius_arcsec, epoch=None):
        out = []
        for s in self.stars:
            sep = math.hypot((s['ra'] - ra) * math.cos(math.radians(dec)), s['dec'] - dec) * 3600
            if sep <= radius_arcsec:
                out.append({**s, 'sep_arcsec': sep})
        return sorted(out, key=lambda s: s['sep_arcsec'])

    def lookup(self, ra, dec, radius_arcsec=10.0, epoch=None):
        c = self.cone(ra, dec, radius_arcsec)
        if not c:
            return None
        return {'name': f"Gaia DR3 {c[0]['source_id']}", 'ids': {}, 'gmag': c[0]['g'],
                'sep_arcsec': c[0]['sep_arcsec'], 'ra': c[0]['ra'], 'dec': c[0]['dec']}


def sky_of(x, y, distortion=0.0, mirrored=False):
    """The 'true' optics: scale, a 30 deg rotation, optional mirror and a quadratic distortion."""
    u, v = x - (W - 1) / 2, y - (H - 1) / 2
    r2 = (u * u + v * v) / (W / 2) ** 2
    u, v = u * (1 + distortion * r2), v * (1 + distortion * r2)
    if mirrored:
        u = -u
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    xi, eta = SCALE * (c * u - s * v), SCALE * (s * u + c * v)
    return R.untangent(xi, eta, RA0, DEC0)


def field(n=60, seed=1, **kw):
    rng = np.random.default_rng(seed)
    x = rng.uniform(15, W - 16, n)
    y = rng.uniform(15, H - 16, n)
    ra, dec = sky_of(x, y, **kw)
    return x, y, ra, dec


def catalog_of(ra, dec):
    return [{'ra': float(a), 'dec': float(d), 'g': 9.0 + 0.05 * i, 'source_id': 1000 + i}
            for i, (a, d) in enumerate(zip(ra, dec))]


def test_tangent_roundtrip():
    ra, dec = np.array([310.0, 310.5, 309.7]), np.array([45.0, 45.3, 44.8])
    xi, eta = R.tangent(ra, dec, RA0, DEC0)
    back = R.untangent(xi, eta, RA0, DEC0)
    assert np.allclose(back[0], ra, atol=1e-10) and np.allclose(back[1], dec, atol=1e-10)


def test_third_order_takes_out_distortion_four_constants_do_not():
    x, y, ra, dec = field(distortion=0.002)
    cat = catalog_of(ra, dec)
    meas = list(zip(x, y))
    pairs = [(i, i) for i in range(len(x))]
    r3 = R.fit_reduction(meas, cat, pairs, RA0, DEC0, (H, W), degree=3, mirrored=False)
    r0 = R.fit_reduction(meas, cat, pairs, RA0, DEC0, (H, W), degree=0, mirrored=False, clip=99)
    assert r3.degree == 3 and r3.sigma_ra < 0.01 and r3.sigma_dec < 0.01
    assert r0.sigma_ra > 0.1
    assert r3.scale() == pytest.approx(SCALE, rel=1e-3)
    ra1, dec1 = r3.world(123.4, 210.7)
    ra_t, dec_t = sky_of(123.4, 210.7, distortion=0.002)
    assert abs(ra1 - ra_t) * 3600 * math.cos(math.radians(dec_t)) < 0.02
    assert abs(dec1 - dec_t) * 3600 < 0.02


def test_four_constants_find_the_mirror_image():
    x, y, ra, dec = field(mirrored=True)
    cat = catalog_of(ra, dec)
    pairs = [(i, i) for i in range(len(x))]
    r = R.fit_reduction(list(zip(x, y)), cat, pairs, RA0, DEC0, (H, W), degree=0)
    assert r.mirrored and r.sigma_ra < 0.01
    assert r.model == '4 constants (mirror image)'


def test_outlier_dropped_and_errors_reflect_scatter():
    rng = np.random.default_rng(3)
    x, y, ra, dec = field(n=80)
    xm, ym = x + rng.normal(0, 0.1, x.size), y + rng.normal(0, 0.1, y.size)     # 0.1 px centroids
    xm[5] += 3.0                                                                # a bad pair
    cat = catalog_of(ra, dec)
    r = R.fit_reduction(list(zip(xm, ym)), cat, [(i, i) for i in range(len(x))], RA0, DEC0, (H, W),
                        degree=1, mirrored=False)
    assert not r.stars[5].used and r.n_used >= 75
    assert r.sigma_ra == pytest.approx(0.1 * SCALE, rel=0.3)
    era, edec = r.error(W / 2, H / 2)
    assert r.sigma_ra < era < 1.1 * r.sigma_ra                  # near the centre: about the scatter
    assert r.error(0, 0)[0] > era                               # corner: model less certain


def test_degree_lowered_with_few_stars():
    x, y, ra, dec = field(n=12)
    r = R.fit_reduction(list(zip(x, y)), catalog_of(ra, dec), [(i, i) for i in range(12)], RA0, DEC0,
                        (H, W), degree=3, mirrored=False)
    assert r.degree == 1                    # 3rd order needs 30 stars, 2nd 18, 1st 9


def _render(x, y, flux=4000.0, fwhm=3.0, seed=0):
    rng = np.random.default_rng(seed)
    img = rng.normal(500.0, 5.0, (H, W))
    yy, xx = np.mgrid[0:H, 0:W]
    s = fwhm / 2.3548
    for a, b, f in zip(x, y, flux if np.ndim(flux) else [flux] * len(x)):
        img += f / (2 * math.pi * s * s) * np.exp(-((xx - a) ** 2 + (yy - b) ** 2) / (2 * s * s))
    return img


def _wcs(dx=0.0, dy=0.0):
    """Linear TAN WCS of the undistorted optics, off by (dx, dy) px (an approximate solution)."""
    from astropy.wcs import WCS
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = [RA0, DEC0]
    w.wcs.crpix = [(W - 1) / 2 + 1 + dx, (H - 1) / 2 + 1 + dy]
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    # xi = SCALE (c u - s v); RA increases to the east = -xi direction in CD convention (xi is east)
    w.wcs.cd = np.array([[c, -s], [s, c]]) * SCALE / 3600
    return w


def test_reduce_field_and_aperture_check():
    x, y, ra, dec = field(n=40, seed=5)
    img = _render(x, y, flux=np.linspace(20000, 4000, x.size))
    cat = catalog_of(ra, dec)
    cat[0]['ra'] += 4.0 / 3600 / math.cos(math.radians(DEC0))      # star 0: catalogue 4" off (not it)
    red = R.reduce_field(img, _wcs(2.5, -1.5), FakeCatalog(cat), epoch=2026.8, degree=1, log=lambda s: None)
    assert red.n_used >= 30 and red.sigma_ra < 0.2
    assert all(not s.used for s in red.stars if s.source_id == 1000)  # the displaced one is out
    checks = R.check_positions(img, red, FakeCatalog(cat), {'target': (x[3] + 2, y[3] - 1),
                                                             'odd': (x[0], y[0])})
    t = checks['target']
    assert t.star == 'Gaia DR3 1003' and t.offset < 0.2 and t.significance < 3
    assert checks['odd'].offset == pytest.approx(4.0, abs=0.3) and checks['odd'].significance > 3


def test_decimal_year():
    assert R.decimal_year('2026-01-01') == 2026.0
    assert R.decimal_year('2026-07-02', '12:00:00.0') == pytest.approx(2026.5, abs=0.002)
    assert R.decimal_year(None) is None and R.decimal_year('garbage') is None


def test_magnitudes_on_the_gaia_scale():
    x, y, ra, dec = field(n=40, seed=7)
    cat = catalog_of(ra, dec)                               # G 9.00, 9.05, ...
    g = np.array([c['g'] for c in cat])
    flux = 10 ** (-0.4 * (g - 20.0))                        # zero point 20 (flux 25000 .. 4000)
    var = int(np.argmin(np.hypot(x - W / 2, y - H / 2)))   # the most central star: a variable / blend,
    flux[var] *= 2.0                                        # 0.75 mag too bright
    img = _render(x, y, flux=flux, seed=2)
    red = R.reduce_field(img, _wcs(), FakeCatalog(cat), epoch=2026.8, degree=1, log=lambda s: None)
    checks = R.check_positions(img, red, FakeCatalog(cat), {'target': (x[var + 1], y[var + 1])})
    p = R.measure_mags(img, red, checks)
    assert p.zero_point == pytest.approx(20.0 - 2.5 * math.log10(0.99), abs=0.03)    # ~all light in 2 FWHM
    assert p.sigma < 0.03 and p.n_used >= len([s for s in red.stars if s.used]) - 4   # variable, blends out
    star = next(s for s in red.stars if s.source_id == 1000 + var)
    assert star.mag == pytest.approx(g[var] - 0.75, abs=0.05)
    t = checks['target']
    assert t.mag == pytest.approx(g[var + 1], abs=0.03) and 0 < t.mag_err < 0.02
    assert t.mag_sigma == p.sigma and abs(t.mag_significance) < 3
    assert 'sigma of' in t.describe().split('mag ')[-1]


def test_shifted_wcs_follows_the_drift():
    w = _wcs()
    ws = R.shifted_wcs(w, 4.6, -2.0)            # stars sit at frame pixel + (4.6, -2.0) in the stack
    a = w.all_pix2world(104.6, 48.0, 0)
    b = ws.all_pix2world(100.0, 50.0, 0)
    assert np.allclose(a, b, atol=1e-10)


def test_remeasure_follows_a_fading_target_on_a_drifted_frame():
    x, y, ra, dec = field(n=40, seed=7)
    cat = catalog_of(ra, dec)
    g = np.array([c['g'] for c in cat])
    flux = 10 ** (-0.4 * (g - 20.0))
    img = _render(x, y, flux=flux, seed=2)
    red = R.reduce_field(img, _wcs(), FakeCatalog(cat), epoch=2026.8, degree=1, log=lambda s: None)
    d = np.hypot(x[:, None] - x, y[:, None] - y) + np.eye(len(x)) * 1e9
    inner = (x > 30) & (x < W - 30) & (y > 30) & (y < H - 30)
    t = int(np.argmax(np.where(inner, d.min(axis=1), 0)))          # an isolated star as the target
    checks = R.check_positions(img, red, FakeCatalog(cat), {'target': (x[t], y[t])})
    R.measure_mags(img, red, checks)
    faded = flux.copy()
    faded[t] *= 0.1                                         # occulted to 10 %: 2.5 mag fainter
    later = _render(x + 3.0, y - 2.0, flux=faded * 0.8, seed=3)     # drifted, and 20 % cloud
    fm = R.remeasure(later, red, checks, shift=(3.0, -2.0), frame=99)
    assert fm.frame == 99 and fm.scale.zero_point == pytest.approx(red.photometry.zero_point - 0.24, abs=0.03)
    m = fm.mags['target']
    assert m.mag == pytest.approx(g[t] + 2.5, abs=0.1) and m.mag_significance > 3
    gone = _render(x + 3.0, y - 2.0, flux=np.where(np.arange(len(x)) == t, 0.0, flux), seed=4)
    m = R.remeasure(gone, red, checks, shift=(3.0, -2.0)).mags['target']      # only noise left
    assert m.mag is None and m.mag_text() == 'mag - (no flux)' or m.mag > g[t] + 3
