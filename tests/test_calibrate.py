"""Dark/flat calibration."""

from unittest.mock import MagicMock

import numpy as np
import pytest

from pystarlight import Calibration
from pystarlight.photometry import measure_aperture


def frames(rng, shape=(120, 160)):
    dark = rng.normal(300, 4, shape)
    dark[rng.integers(0, shape[0], 8), rng.integers(0, shape[1], 8)] += 400   # hot pixels
    vignette = 1 - 0.3 * np.hypot(*np.indices(shape) - np.array(shape)[:, None, None] / 2) / max(shape)
    flat = dark + 8000 * vignette * rng.normal(1, 0.01, shape)
    y, x = np.indices(shape)
    sky = 500 + 6000 * np.exp(-((y - 60) ** 2 + (x - 80) ** 2) / (2 * 1.8 ** 2))
    light = (dark + sky * vignette + rng.normal(0, 5, shape)).clip(0, 65535).astype(np.uint16)
    return dark, flat, light


def test_roi_equals_full_frame():
    dark, flat, light = frames(np.random.default_rng(0))
    cal = Calibration.from_frames(dark, flat)
    full = cal.apply(light)
    a = measure_aperture(light, 55, 35, 51, thresh=99999, calibration=cal)
    b = measure_aperture(full, 55, 35, 51, thresh=99999, defect_frame=cal.defects)
    assert (a.signal, a.bkavg, a.bkstd, a.mskpx, a.hit_defect) == (b.signal, b.bkavg, b.bkstd, b.mskpx,
                                                                   b.hit_defect)


def test_flat_removes_vignetting():
    dark, flat, light = frames(np.random.default_rng(1))
    cal = Calibration.from_frames(dark, flat)
    out = cal.apply(light)
    corner, centre = out[5:25, 5:25], out[50:70, 20:40]
    raw_ratio = np.median(light[5:25, 5:25]) / np.median(light[50:70, 20:40])
    assert abs(np.median(corner) / np.median(centre) - 1) < abs(raw_ratio - 1) / 3


def test_hot_pixels_found():
    rng = np.random.default_rng(2)
    dark, _, _ = frames(rng)
    cal = Calibration.from_frames(dark=dark)
    hot = np.argwhere(dark - np.median(dark) > 200)
    assert all(cal.defects[r, c] for r, c in hot if 2 <= r < dark.shape[0] - 2 and 2 <= c < dark.shape[1] - 2)
    h, w = dark.shape
    interior = (hot[:, 0] >= 2) & (hot[:, 0] < h - 2) & (hot[:, 1] >= 2) & (hot[:, 1] < w - 2)
    assert cal.defects.sum() == interior.sum()


def test_save_load(tmp_path):
    dark, flat, _ = frames(np.random.default_rng(3))
    cal = Calibration.from_frames(dark, flat)
    cal.save(tmp_path / 'cal.npz')
    again = Calibration.load(tmp_path / 'cal.npz')
    np.testing.assert_array_equal(again.gain, cal.gain)
    assert again.dark_median == cal.dark_median


pm = None
try:
    import pymovie.main as pm  # noqa: F811
except Exception:  # pragma: no cover
    pass


@pytest.mark.skipif(pm is None, reason='needs PyMovie importable')
def test_matches_pymovie():
    dark, flat, light = frames(np.random.default_rng(4), shape=(40, 50))
    f = MagicMock()
    f.darkFrame, f.flatFrame = dark, flat
    f.darkMean, f.flatMean = dark.mean(), flat.mean()
    for sb in ('dfTopRedactSpinBox', 'dfBottomRedactSpinBox', 'dfLeftRedactSpinBox', 'dfRightRedactSpinBox'):
        getattr(f, sb).value.return_value = 0
    f.fillFrameBorder = lambda *a, **k: None
    pm.PyMovie.calculateGainFrame(f)

    cal = Calibration.from_frames(dark, flat)
    np.testing.assert_allclose(cal.gain, f.gainFrame)

    f.darkMedian = float(np.median(dark))
    _, _, expected = pm.PyMovie.applyDarkFlatCorrect(f, light)
    np.testing.assert_allclose(cal.apply(light), expected)

    f.dfDarkThreshSpinBox.value.return_value = 50
    f.meanFrameROI = lambda fr, **k: fr.mean()
    f.medianFrameROI = lambda fr, **k: np.median(fr)
    f.stdFrameROI = lambda fr, **k: fr.std()
    f.clearDefectFrameEdges = lambda *a, **k: pm.PyMovie.clearDefectFrameEdges(f, *a, **k)
    pm.PyMovie.buildDarkDefectFrame(f)
    dark_only = Calibration.from_frames(dark=dark, dark_threshold=50)
    np.testing.assert_array_equal(dark_only.defects, f.darkDefectFrame)


def test_from_config_stacks_and_caches_clips(tmp_path, monkeypatch):
    from pystarlight.calibrate import from_config
    from test_pipeline import write_ser
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    rng = np.random.default_rng(5)
    hot = np.zeros((240, 320))
    hot[100, 100] = 3000
    write_ser(tmp_path / 'dark.ser', [rng.normal(300, 6, (240, 320)) + hot for _ in range(8)])
    write_ser(tmp_path / 'flat.ser', [rng.normal(9000, 30, (240, 320)) + hot for _ in range(8)])
    cfg = {'calibration': {'dark': str(tmp_path / 'dark.ser'), 'flat': str(tmp_path / 'flat.ser'),
                           'flat_dark': '', 'frames': 0, 'method': 'mean', 'file': ''}}
    logs = []
    cal = from_config(cfg, log=logs.append)
    assert cal.dark[100, 100] == pytest.approx(3300, abs=10)        # averaged clip
    assert cal.defects[100, 100] == 1
    assert sum('stacking' in m for m in logs) == 2
    logs.clear()
    from_config(cfg, log=logs.append)                               # second time: from the cache
    assert sum('using stacked master' in m for m in logs) == 2
    assert from_config({'calibration': {'dark': '', 'flat': '', 'file': ''}}) is None
    with pytest.raises(FileNotFoundError):
        from_config({'calibration': {'dark': str(tmp_path / 'missing.ser')}})


def test_same_clip_for_dark_and_flat_is_refused(tmp_path):
    from pystarlight.calibrate import from_config
    from test_pipeline import write_ser
    write_ser(tmp_path / 'd.ser', [np.full((240, 320), 300.0)] * 2)
    cfg = {'_path': str(tmp_path / 'pystarlight.toml'),
           'calibration': {'dark': 'd.ser', 'flat': 'd.ser', 'flat_dark': '', 'file': ''}}
    with pytest.raises(ValueError, match='same clip'):
        from_config(cfg)


def test_use_calibration_false_disables_without_touching_entries(tmp_path):
    from pystarlight.calibrate import from_config
    cfg = {'_path': str(tmp_path / 'pystarlight.toml'),
           'calibration': {'use_calibration': False, 'dark': 'missing/dark.ravf', 'flat': '', 'file': ''}}
    logs = []
    assert from_config(cfg, log=logs.append) is None          # not even checked for existence
    assert 'disabled' in logs[0]
