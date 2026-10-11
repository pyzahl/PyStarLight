"""Compare pystarlight with the original PyMovie functions on synthetic star fields.

Needs the original package importable (run with PyMovie's venv); skipped otherwise.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from pystarlight import photometry as ph
from pystarlight import preprocess as pp

pm = pytest.importorskip('pymovie.main')


def star_field(rng, n=51, dtype='uint16', bg=400, noise=12, stars=((25, 25, 3000, 1.6),)):
    y, x = np.indices((n, n))
    img = rng.normal(bg, noise, (n, n))
    for r, c, amp, s in stars:
        img += amp * np.exp(-((y - r) ** 2 + (x - c) ** 2) / (2 * s * s))
    hi = 255 if dtype == 'uint8' else 65535
    return np.clip(img, 0, hi).astype(dtype)


CASES = [
    dict(),
    dict(dtype='uint8', bg=30, noise=4, stars=((24, 27, 150, 1.4),)),
    dict(stars=((25, 25, 3000, 1.6), (8, 40, 800, 1.2))),
    dict(stars=()),
    dict(stars=((22, 30, 300, 2.0),)),
]


@pytest.mark.parametrize('seed', range(5))
@pytest.mark.parametrize('case', CASES)
def test_robust_mean_std(seed, case):
    img = star_field(np.random.default_rng(seed), **case)
    mean, std, sorted_data, *_rest, bk = pm.newRobustMeanStd(img)
    st = ph.robust_mean_std(img)
    assert st.mean == pytest.approx(mean, rel=1e-12)
    assert st.std == pytest.approx(std, rel=1e-12)
    assert st.max == sorted_data[-1]
    np.testing.assert_array_equal(np.sort(st.bkgnd_values), np.sort(bk))


@pytest.mark.parametrize('seed', range(5))
def test_robust_mean_std_lunar(seed):
    img = star_field(np.random.default_rng(seed))
    mean, std, *_ = pm.newRobustMeanStd(img, lunar=True)
    st = ph.robust_mean_std(img, lunar=True)
    assert (st.mean, st.std) == pytest.approx((mean, std))


@pytest.mark.parametrize('seed', range(5))
@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('dist', [None, 8])
@pytest.mark.parametrize('offset', [10, 40, 150])
def test_blob_mask(seed, case, dist, offset):
    img = star_field(np.random.default_rng(seed), **case)
    cut = int(round(ph.robust_mean_std(img).mean)) + offset
    area, mask, _t, centroid, cvx, nblobs, extent = pm.get_mask(
        img, ksize=(5, 5), cut=cut, apply_centroid_distance_constraint=dist is not None,
        max_centroid_distance=dist)
    b = ph.blob_mask(img, cut=cut, max_centroid_distance=dist)
    assert b.area == area
    assert b.n_blobs == nblobs
    np.testing.assert_array_equal(b.mask, mask)
    if area:
        assert b.centroid == pytest.approx(centroid)
        assert b.convex_area == cvx
        assert b.extent == extent


@pytest.mark.parametrize('seed', range(5))
def test_brightest_pixel(seed):
    img = star_field(np.random.default_rng(seed), stars=((20, 31, 900, 1.5),)).astype('int32')
    mask, _ = ph.circular_mask(51, 6.8)
    expected = pm.brightest_pixel((mask * img).copy(), 5)
    np.testing.assert_allclose(ph.brightest_pixel(mask * img, 5), expected)


def test_circular_mask():
    for radius in (2.0, 2.4, 3.2, 4.0, 4.5, 5.3, 6.8):
        mask, n = pm.PyMovie.buildPsfStarMask(51, radius)[:2]
        m2, n2 = ph.circular_mask(51, radius)
        np.testing.assert_array_equal(m2, mask)
        assert n2 == n


def test_scrub_and_hot_pixels():
    rng = np.random.default_rng(3)
    img = star_field(rng, n=64)
    pts = [(5, 5), (10, 20), (30, 31)]

    class Fake:
        outlawPoints = pts
    expected = pm.PyMovie.scrubImage(Fake(), img)
    np.testing.assert_array_equal(pp.scrub_outlaw_pixels(img, pts), expected)


# --------------------------------------------------------------------------- full measurement

def fake_pymovie(image, fields=False):
    f = MagicMock()
    f.image = image
    f.one_time_suppress_stats = False
    f.darkDefectFrame = f.gainDefectFrame = None
    f.lunarCheckBox.isChecked.return_value = False
    f.processAsFieldsCheckBox.isChecked.return_value = fields
    f.topFieldFirstRadioButton.isChecked.return_value = True
    f.currentFrameSpinBox.text.return_value = '7'
    f.gaussian_blur = (5, 5)
    f.allowed_centroid_delta = 8
    f.roi_size, f.roi_center = 51, 25
    f.recordPsf = False
    f.target_psf_gathering_in_progress = False
    f.useOptimalExtraction = False
    f.extractionCode = 'AP'
    f.use_yellow_mask = False
    f.yellow_mask = None
    f.avi_in_use, f.aav_file_in_use = True, False
    f.upperTimestamp = f.lowerTimestamp = f.avi_timestamp = ''
    f.smoothingCount = 100
    f.applyHuntBiasCorrection = False
    f.tmeSearchGridSize = 3
    f.finderFrameBeingDisplayed = False
    f.nonRecenteringAperture = pm.PyMovie.nonRecenteringAperture
    f.calcTMEIntensity = lambda *a, **k: pm.PyMovie.calcTMEIntensity(f, *a, **k)
    f.bestNxNappsum = lambda *a, **k: pm.PyMovie.bestNxNappsum(f, *a, **k)
    return f


def big_image(rng, stars):
    return star_field(rng, n=200, stars=stars)


MEAS_CASES = [
    # name, color, thresh offset (None = 99999), star (row, col, amp, sigma) relative to aperture
    ('ap01', 'red', None, (25, 25, 3000, 1.6)),
    ('ap01', 'red', None, (22, 29, 3000, 1.6)),     # off-centre: default mask re-centres
    ('ap01', 'red', 'auto', (24, 27, 3000, 1.6)),   # blob mask
    ('ap01', 'green', 'auto', (24, 27, 600, 2.0)),
    ('ap01', 'yellow', 'auto', (20, 30, 3000, 1.6)),
    ('ap01', 'yellow', None, (21, 29, 3000, 1.6)),
    ('ap01', 'white', None, (25, 25, 3000, 1.6)),
    ('empty-no-rc', 'red', None, (22, 29, 3000, 1.6)),
    ('ap01', 'red', None, None),                    # no star at all
    ('TME01', 'red', None, (24, 26, 3000, 1.6)),
]


@pytest.mark.parametrize('seed', range(4))
@pytest.mark.parametrize('case', MEAS_CASES)
@pytest.mark.parametrize('fields', [False, True])
@pytest.mark.parametrize('y0', [60, 61])
def test_measure_aperture(seed, case, fields, y0):
    name, color, thresh, star = case
    x0 = 70
    stars = () if star is None else ((y0 + star[0], x0 + star[1], star[2], star[3]),)
    img = big_image(np.random.default_rng(seed), stars)
    thumb = img[y0:y0 + 51, x0:x0 + 51]
    t = 99999 if thresh is None else ph.initial_threshold(thumb, 2.0)
    radius = 3.2
    mask, count = ph.circular_mask(51, radius)

    ap = SimpleNamespace(name=name, color=color, thresh=t, defaultMask=mask, defaultMaskPixelCount=count,
                         default_mask_radius=radius, primary_yellow_aperture=True, xsize=51,
                         smoothed_background=0, getBbox=lambda: (x0, y0, 51, 51))
    f = fake_pymovie(img, fields)
    (xc_roi, yc_roi, xc_w, yc_w, signal, appsum, mean, max_area, _fn, cvx, maxpx, std,
     _ts, ap_mean, _sc, hit) = pm.PyMovie.getApertureStats(f, ap, show_stats=False)

    method = 'tme' if name.startswith('TME') else 'aperture'
    m = ph.measure_aperture(img, x0, y0, 51, color=color, thresh=t, name=name, default_mask=mask,
                            default_mask_radius=radius, method=method, fields=fields)
    assert m.signal == pytest.approx(signal)
    assert m.appsum == pytest.approx(appsum)
    assert m.bkavg == pytest.approx(mean)
    assert m.bkstd == pytest.approx(std)
    assert m.mskpx == max_area
    assert m.maxpx == maxpx
    assert m.cvxhull == cvx
    assert m.background_sample == pytest.approx(ap_mean)
    assert (m.xc_roi, m.yc_roi, m.xc, m.yc) == pytest.approx((xc_roi, yc_roi, xc_w, yc_w))
    if color == 'yellow':
        assert m.track_xy is not None
        assert m.track_xy == (f.firstYellowApertureX, f.firstYellowApertureY)
    if fields:
        for mine, theirs in zip(m.fields, (f.field1_data, f.field2_data)):
            assert mine.signal == pytest.approx(theirs[4])
            assert mine.appsum == pytest.approx(theirs[5])
            assert mine.bkavg == pytest.approx(theirs[6])
            assert mine.mskpx == theirs[7]
