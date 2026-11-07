"""Saturated stars: full scale of a recording, per-aperture fractions, left out of the evaluation."""

from types import SimpleNamespace

import numpy as np
import pytest

from pystarlight import saturation as sat


class _LC:
    """Just what saturation reads of a LightCurves: names, columns['maxpx'], metadata."""

    def __init__(self, peaks, meta):
        self.columns = {n: {'maxpx': np.asarray(p, dtype=float)} for n, p in peaks.items()}
        self.names, self.metadata, self.roles = list(peaks), meta, {}

    def __getitem__(self, n):
        return self.columns[n]


def test_full_scale_of_a_recording():
    assert sat.full_scale({'IMAGE-BIT-DEPTH': 12}) == (4095.0, '12-bit (IMAGE-BIT-DEPTH)')
    assert sat.full_scale({'PixelDepthPerPlane': 16})[0] == 65535.0                  # SER
    assert sat.full_scale({}, max_value=65535) == (65535.0, 'MaxPixelValue')          # ADV (DVTI)
    assert sat.full_scale({'INSTRUMENT': 'Astrid'}) == (None, '')                     # ASTRID: no depth


def test_level_ceiling_then_data_type():
    clipped = _LC({'a': [4095] * 6 + [3000, 2000], 'b': [500, 600]}, {})
    assert sat.level_of(clipped) == (4095.0, 'ceiling in the data')    # right-aligned, no depth
    left = _LC({'a': [65472, 40000, 30000]}, {'INSTRUMENT': 'Astrid'})
    assert sat.level_of(left, dtype=np.uint16) == (65535.0, '16-bit data')            # ASTRID, 10 bit << 6
    assert sat.level_of(_LC({'a': [100, 200]}, {})) == (None, '')
    assert sat.level_of(_LC({'a': [100]}, {'IMAGE-BIT-DEPTH': 12}))[0] == 4095.0


def test_report_and_split():
    lc = _LC({'target': [1000] * 100, 'ref1': [2000] * 100, 'ref2': [4000] * 70 + [3000] * 30,
              'check1': [3700] + [1000] * 99}, {})
    rep = sat.report(lc, 4095.0)
    assert rep == {'target': 0.0, 'ref1': 0.0, 'ref2': 0.7, 'check1': 0.01}     # >= 0.9 x 4095 = 3685.5
    assert sat.split(['ref1', 'ref2'], rep) == (['ref1'], ['ref2'])
    assert sat.split(['check1'], rep) == (['check1'], [])                        # one frame: kept
    assert sat.describe(rep) == 'ref2 70 %, check1 1 %'
    assert sat.report(lc, None) == {}


def test_viewer_leaves_saturated_stars_out(monkeypatch, tmp_path):
    pytest.importorskip('matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    from pystarlight.viewer import Viewer
    v = Viewer.__new__(Viewer)                       # just the bookkeeping, no window
    v.say_job = lambda m: setattr(v, '_msg', m)
    v._saturation = {'frames_saturated': {'target': 0.0, 'ref1': 0.0, 'ref2': 0.7, 'check1': 0.5}}
    refs, checks = v.unsaturated('target', ['ref1', 'ref2'], ['check1'])
    assert refs == ['ref1'] and checks == []
    assert v._saturation['excluded_references'] == ['ref2'] and v._saturation['excluded_checks'] == ['check1']
    assert 'ref2' in v._msg and 'check1' in v._msg
    v._saturation = {'frames_saturated': {'ref1': 0.9, 'ref2': 0.7, 'target': 0.3}}
    refs, _ = v.unsaturated('target', ['ref1', 'ref2'], [])
    assert refs == ['ref1', 'ref2'] and 'ALL references' in v._msg and 'target' in v._msg


def test_live_saturation_mark():
    pytest.importorskip('matplotlib')
    from pystarlight.tracking import Aperture
    from pystarlight.viewer import Viewer
    v = Viewer.__new__(Viewer)
    img = np.full((60, 60), 300, np.uint16)
    img[30, 30] = 4095
    v.image = img
    v.movie = SimpleNamespace(metadata={'IMAGE-BIT-DEPTH': 12})
    assert v.is_saturated(Aperture.centered_at('ref1', 30, 30, size=21))
    assert not v.is_saturated(Aperture.centered_at('ref2', 10, 10, size=11))
    v.movie = SimpleNamespace(metadata={})                         # unknown depth: 16-bit full scale
    assert not v.is_saturated(Aperture.centered_at('ref1', 30, 30, size=21))


def test_viewer_time_base_mid_exposure(tmp_path):
    """RAVF stamps the exposure start: figures and the step fit get +exposure / 2."""
    pytest.importorskip('matplotlib')
    from pathlib import Path

    from pystarlight import plot
    from pystarlight.viewer import Viewer
    v = Viewer.__new__(Viewer)
    v.path = Path(tmp_path / 'x.ravf')
    lc = SimpleNamespace(time=['04:00:00.000', '04:00:00.500', '04:00:01.000'],
                         metadata={'INSTRUMENT': 'Astrid', 'INSTRUMENT-SHUTTER': 500_000_000})
    assert v.set_time_base(lc) == pytest.approx(0.25)
    assert plot.seconds(lc)[0] == pytest.approx(4 * 3600 + 0.25)
    assert v._time_base['timestamp_reference'] == 'start'
    v.path = Path(tmp_path / 'x.adv')                    # ADV: the start is read (Adv2), shifted too
    lc.metadata = {'EXPOSURE-S': 1.0}
    assert v.set_time_base(lc) == pytest.approx(0.5)
    v.path = Path(tmp_path / 'x.ser')                    # SER: unknown, taken as mid-exposure
    assert v.set_time_base(lc) == 0.0
