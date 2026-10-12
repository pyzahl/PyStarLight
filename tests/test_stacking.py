"""Registered field stack, hot-pixel cleaning, annotated outputs."""

import numpy as np
import pytest
from astropy.wcs import WCS

from pystarlight import open_movie
from pystarlight.globaltrack import analyze_global, fit_peak
from pystarlight.stacking import (
    attach_wcs,
    reference_positions,
    remove_hot_pixels,
    save_field_stack,
    stack_field,
)
from test_globaltrack import apertures, render, write_ser


@pytest.fixture(scope='module')
def run(tmp_path_factory):
    rng = np.random.default_rng(3)
    p = tmp_path_factory.mktemp('s') / 'rot.ser'
    write_ser(p, [render(i, rng, True) for i in range(40)])      # shake + field rotation
    aps = apertures()
    with open_movie(str(p)) as mv:
        lc = analyze_global(mv, aps)
        st = stack_field(mv, lc)
        naive = np.mean([mv.read(i).image.astype(float) for i in range(40)], axis=0)
        single = mv.read(0).image.astype(float)
    return p, aps, lc, st, naive, single


def test_registered_stack_is_sharp(run):
    _, _, lc, st, naive, single = run
    assert st.n == 40 and st.method == 'tracking'
    for n, (x, y) in reference_positions(lc).items():
        if n == 'sky' or n == 'target':
            continue
        fs, ft, fn = fit_peak(single, x, y, 5), fit_peak(st.image, x, y, 5), fit_peak(naive, x, y, 8)
        assert ft.ok and ft.fwhm < 1.15 * fs.fwhm                  # as sharp as one frame
        assert np.hypot(ft.x - x, ft.y - y) < 0.3
        assert not fn.ok or fn.fwhm > 1.3 * fs.fwhm                # a plain mean is smeared


def test_hot_pixels_removed_stars_kept():
    rng = np.random.default_rng(0)
    y, x = np.indices((80, 80))
    img = rng.normal(300, 5, (80, 80)) + 3000 * np.exp(-((y - 40) ** 2 + (x - 40) ** 2) / (2 * 1.4 ** 2))
    img[10, 12] = 5000
    img[60, 70] = 2000
    out = remove_hot_pixels(img.astype(np.float32))
    assert out[10, 12] < 400 and out[60, 70] < 400
    np.testing.assert_array_equal(out[34:47, 34:47], img[34:47, 34:47].astype(np.float32))


def test_outputs_with_wcs_and_layers(run, tmp_path):
    _, aps, lc, st, _, _ = run
    from astropy.io import fits
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = (310.0, 45.0)
    w.wcs.crpix = (160.5, 120.5)
    w.wcs.cd = [[-2.07 / 3600, 0], [0, -2.07 / 3600]]
    solved = np.roll(np.nan_to_num(st.image, nan=0), (3, -2), (0, 1))   # 'first frames': dx -2, dy +3
    attach_wcs(st, w, solved)
    assert st.shift_to_solution == pytest.approx((-2, 3), abs=0.2)
    paths = save_field_stack(st, str(tmp_path / 'f'), aps, lc, title='test', formats=('svg', 'png'))
    assert {p.rsplit('.', 1)[1] for p in paths} == {'fits', 'svg', 'png'}
    hdr = fits.getheader(tmp_path / 'f.fits')
    assert hdr['NCOMBINE'] == 40 and hdr['CRPIX1'] == pytest.approx(162.5, abs=0.2)
    svg = (tmp_path / 'f.svg').read_text()
    for gid in ('field-image', 'apertures', 'aperture-labels', 'compass'):
        assert f'id="{gid}' in svg
    assert 'target' in svg and 'ref1' in svg


def test_phase_correlation_without_tracking(run):
    p, _, _, _, _, single = run
    with open_movie(str(p)) as mv:
        st = stack_field(mv, None, frames=range(0, 40, 4))
    assert st.method == 'phase correlation' and st.n == 10
