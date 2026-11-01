"""Step fit of a light curve: event times and their errors on synthetic occultations."""

import numpy as np
import pytest

from pystarlight import steps

DT = 1 / 6.0                                    # 6 fps
T = 40000.0 + np.arange(600) * DT              # seconds since midnight


def event(d, r, depth=1.0, noise=0.05, seed=0):
    """Flux 1 with a drop of `depth` from d to r; rows are exposures of DT centred on T."""
    rng = np.random.default_rng(seed)
    frac = np.clip((np.minimum(T + DT / 2, r) - np.maximum(T - DT / 2, d)) / DT, 0, 1)
    return 1.0 - depth * frac + rng.normal(0, noise, T.size)


def test_no_event_in_noise():
    for seed in range(20):
        fit = steps.fit_steps(T, event(0, 0, depth=0.0, seed=seed))
        assert fit.transitions == [] and len(fit.levels) == 1
    assert fit.describe() == 'no transition'
    assert fit.levels[0].value == pytest.approx(1.0, abs=0.01)


def test_disappearance_and_reappearance_to_a_fraction_of_a_frame():
    d, r = T[150] + 0.37 * DT, T[260] + 0.81 * DT
    fit = steps.fit_steps(T, event(d, r, noise=0.03))
    D, R = fit.transitions
    assert (D.kind, R.kind) == ('D', 'R')
    assert abs(D.time - d) < 3 * D.sigma and abs(R.time - r) < 3 * R.sigma
    assert D.sigma < 0.2 * DT and D.lo < D.time < D.hi
    assert len(fit.levels) == 3 and fit.levels[1].value == pytest.approx(0.0, abs=0.02)
    assert D.drop == pytest.approx(1.0, abs=0.03) and D.significance > 20
    assert fit.model()[200] == fit.levels[1].value
    assert fit.describe().startswith('D at 40025.')


def test_short_and_partial_events():
    d, r = T[300] + 0.2 * DT, T[302] + 0.6 * DT        # 2.4 frames: a box, no single split finds it
    fit = steps.fit_steps(T, event(d, r, noise=0.05, seed=3))
    assert [tr.kind for tr in fit.transitions] == ['D', 'R']
    fit = steps.fit_steps(T, event(T[100], T[400], depth=0.3, noise=0.05, seed=4))
    assert [tr.kind for tr in fit.transitions] == ['D', 'R']
    assert fit.transitions[0].drop == pytest.approx(0.3, abs=0.03)


def test_sigma_is_calibrated():
    pulls = []
    for seed in range(30):
        rng = np.random.default_rng(50 + seed)
        d, r = T[200] + rng.uniform(0, 1), T[350] + rng.uniform(0, 1)
        fit = steps.fit_steps(T, event(d, r, depth=0.3, noise=0.064, seed=seed))
        assert len(fit.transitions) == 2
        pulls += [(fit.transitions[0].time - d) / fit.transitions[0].sigma,
                  (fit.transitions[1].time - r) / fit.transitions[1].sigma]
    assert 0.6 < np.sqrt(np.mean(np.square(pulls))) < 1.5


def test_nan_rows_and_to_mag():
    y = event(T[100], T[200], seed=5)
    y[150:160] = np.nan                                 # rows without a measurement
    fit = steps.fit_steps(T, y)
    assert len(fit.transitions) == 2 and fit.t.size == T.size - 10
    assert fit.levels[-1].stop == T.size
    assert np.allclose(steps.to_mag([1.0, 0.1, 0.0, -1.0], 10.0), [10.0, 12.5, np.nan, np.nan],
                       equal_nan=True)


def test_event_report_nests_pairs_and_gives_mags():
    d1, d2, r1, r2 = T[100] + 0.3 * DT, T[150], T[200] + 0.5 * DT, T[260]
    rng = np.random.default_rng(9)
    y = 1.0 + rng.normal(0, 0.02, T.size)
    for a, b in ((d1, r2), (d2, r1)):                   # two stars, 40 % each
        y -= 0.4 * np.clip((np.minimum(T + DT / 2, b) - np.maximum(T - DT / 2, a)) / DT, 0, 1)
    fit = steps.fit_steps(T, y)
    rep = steps.event_report(fit, '2026-10-11', m0=10.0, band='G')
    assert [tr['kind'] for tr in rep['transitions']] == ['D', 'D', 'R', 'R']
    assert [(p['D'], p['R']) for p in rep['events']] == [(0, 3), (1, 2)]       # outer, inner
    assert rep['events'][0]['duration_s'] == pytest.approx(r2 - d1, abs=0.1)
    assert rep['transitions'][0]['utc'].startswith('2026-10-11T11:06:')        # 40000 s + 16.7 s
    lv = rep['levels']
    assert lv[0]['mag'] == pytest.approx(10.0, abs=0.01) and lv[2]['mag'] == pytest.approx(11.75, abs=0.1)
    assert 0 < lv[0]['mag_error'] < lv[0]['mag_scatter_per_frame']
    gone = steps.event_report(steps.fit_steps(T, event(T[100], T[200], noise=0.02)), '', 10.0)
    assert gone['levels'][1]['mag'] is None and gone['levels'][1]['fainter_than_mag_3sigma'] > 14
    assert gone['transitions'][0]['utc'] is None


def test_object_name():
    from pystarlight.movie import object_name
    assert object_name({'OBJNAME': '(4337) Arecibo'}) == '(4337) Arecibo'
    assert object_name({'OBJECT': 'Juno'}) == 'Juno'
    assert object_name({'OBJNAME': ''}, 'Arecibo_20261011_042339_1.ravf') == 'Arecibo'
    assert object_name({'OBJNAME': ''}, 'None_20261011_042339_1.ravf') == ''
