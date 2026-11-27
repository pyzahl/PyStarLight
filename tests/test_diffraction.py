"""diffraction.py: bar model against the exact knife edge, incoherent band sum, star profile, the
sinc engine against the bar model, and fits recovering known single and double star events."""

import numpy as np
import pytest

from pystarlight import diffraction as dif

Z = 2.33          # AU (Bob Anderson's (9203) Myrtus example)


def test_knife_edge_values():
    f = dif.fresnel_scale_km(500, Z)
    x = np.linspace(-60 * f, 60 * f, 24001)              # strip from 0 to far away: a knife edge
    i = dif.bar_intensity(x, [(0.0, 1e6)], 500, Z)
    assert dif.bar_intensity([0.0], [(0.0, 1e6)], 500, Z)[0] == pytest.approx(0.25, abs=1e-6)
    assert i[x < 0].max() == pytest.approx(1.3704, abs=2e-3)          # first bright fringe
    assert i[x > 20 * f].max() < 1e-3                                   # deep shadow
    assert abs(i[x < -40 * f] - 1).max() < 0.03                         # far outside: fringes die out


def test_long_strip_equals_two_knife_edges():
    f = dif.fresnel_scale_km(600, Z)
    x = np.linspace(-10 * f, 10 * f, 2001)
    a = dif.bar_intensity(x, [(0.0, 400 * f)], 600, Z)
    b = dif.bar_intensity(x, [(0.0, 1e7)], 600, Z)
    assert np.abs(a - b).max() < 5e-3


def test_band_is_an_incoherent_sum():
    band = dif.Band(np.array([450.0, 750.0]), np.array([0.5, 0.5]))
    t = np.linspace(0, 4, 400)
    comp = [dif.Component([(1.5, 2.5)])]
    both = dif.transmission(t, comp, Z, 5.0, band)
    mono = [dif.transmission(t, comp, Z, 5.0, dif.Band.monochromatic(w)) for w in (450, 750)]
    assert np.allclose(both, 0.5 * (mono[0] + mono[1]), atol=1e-12)     # point star: exact, no grid


def test_band_from_table(tmp_path):
    p = tmp_path / 'qe.txt'
    p.write_text('# nm, QE\n400, 0.5\n600, 1.0\n800, 0.5\n1000, 0.0\n')
    flat = dif.Band.from_table(p)
    assert flat.weights.sum() == pytest.approx(1.0)
    assert flat.effective_nm == pytest.approx(635, abs=10)      # QE-weighted mean of the curve
    cool = dif.Band.from_table(p, teff=3500)
    hot = dif.Band.from_table(p, teff=12000)
    assert cool.effective_nm > flat.effective_nm > hot.effective_nm


def test_strip_profile_and_big_star_half_light():
    k = dif.strip_profile(2.0, 0.6, 0.01)
    assert k.sum() == pytest.approx(1.0)
    assert np.allclose(k, k[::-1])
    t = np.array([0.0])                   # observer at the geometric edge of a long strip
    tr = dif.transmission(t, [dif.Component([(0.0, 100.0)], diameter_km=3.0, limb=0.0)], Z, 1.0)
    assert tr[0] == pytest.approx(0.5, abs=0.02)


def test_sinc_engine_matches_bar_model():
    """A 4 km strip over the whole 2-D plane (its ends 12 km = 40 Fresnel scales away) seen along
    the middle row equals the 1-D bar model. The sinc method needs fine sampling for a sharp
    silhouette: max error 3-5 % at dx = f/10 near the edges, 0.5 % at f/30 (used here); and it
    reaches only ~lambda Z / (2 dx) from an edge (band limit), so compare within 1.5 km."""
    lam = 500.0
    f = dif.fresnel_scale_km(lam, Z)
    n, width = 2400, 24.0                    # dx = 0.01 km ~ f / 31
    dx = width / n
    x = (np.arange(n) - n // 2) * dx
    sel = (x >= -2.0) & (x < 2.0)
    body = np.zeros((n, n))
    body[:, sel] = 1.0
    row = dif.sinc_intensity(body, width, dif.Band.monochromatic(lam), Z, row=n // 2)
    bar = dif.bar_intensity(x, [(x[sel][0] - dx / 2, x[sel][-1] + dx / 2)], lam, Z)
    near = np.abs(np.abs(x) - 2.0) < 1.5
    assert np.abs(row[near] - bar[near]).max() < 0.01
    assert f == pytest.approx(0.295, abs=0.002)        # sqrt(lambda Z / 2) at 500 nm, 2.33 AU


def _synthetic(comps, t, base=1.0, bottom=0.05, noise=0.02, exposure=0.1, seed=1):
    tr = dif.transmission(t, comps, Z, 5.0, dif.Band.monochromatic(600), exposure)
    y = bottom + (base - bottom) * tr
    return y + np.random.default_rng(seed).normal(0, noise, len(t))


def test_fit_single_event():
    t = np.arange(0, 20, 0.1)
    truth = (10.033, 12.371)
    y = _synthetic([dif.Component([truth], diameter_km=0.2)], t)
    fit = dif.fit_event(t, y, Z, 5.0, exposure=0.1, band=dif.Band.monochromatic(600), diameter_km=0.2)
    for key, true in zip(('D', 'R'), truth):
        val, err = fit.times[key]
        assert 0 < err < 0.03
        assert abs(val - true) < 4 * err + 1e-3
    assert fit.chi2 / fit.dof < 1.5


def test_fit_double_star():
    t = np.arange(0, 24, 0.1)
    comps = [dif.Component([(10.02, 13.11)], 0.7), dif.Component([(10.63, 13.74)], 0.3)]
    y = _synthetic(comps, t, noise=0.015, seed=3)
    fit = dif.fit_event(t, y, Z, 5.0, exposure=0.1, band=dif.Band.monochromatic(600), double=True)
    f, ferr = fit.flux_ratio
    got = {k: v[0] for k, v in fit.times.items()}
    if f < 0.5:                                           # components may come out swapped
        f = 1 - f
        got = {'D1': got['D2'], 'R1': got['R2'], 'D2': got['D1'], 'R2': got['R1']}
    assert f == pytest.approx(0.7, abs=max(4 * ferr, 0.03))
    for k, true in (('D1', 10.02), ('R1', 13.11), ('D2', 10.63), ('R2', 13.74)):
        assert abs(got[k] - true) < 0.03


def test_grid_path_matches_exact_evaluation():
    """With smoothing the curve is computed on a grid; a negligible exposure must reproduce the
    exact point evaluation within 20 Fresnel scales of the edges (beyond, the window tapers to the
    geometric value: monochromatic point-star fringes there are ~1 %, smoothed away in practice)."""
    f = dif.fresnel_scale_km(600, Z)
    v = 5.0
    t = np.linspace(1.0 - 20 * f / v, 3.0 + 20 * f / v, 3001)
    comp = [dif.Component([(1.0, 3.0)])]
    exact = dif.transmission(t, comp, Z, v, dif.Band.monochromatic(600), exposure=0.0)
    grid = dif.transmission(t, comp, Z, v, dif.Band.monochromatic(600), exposure=1e-5)
    assert np.abs(grid - exact).max() < 0.01


def test_from_steps_and_compare_single():
    from pystarlight import steps
    t = np.arange(0, 20, 0.1)
    y = _synthetic([dif.Component([(10.033, 12.371)], diameter_km=0.2)], t)
    sf = steps.fit_steps(t, y)
    fit = dif.from_steps(sf, Z, 5.0, band=dif.Band.monochromatic(600), diameter_km=0.2)
    rows = dif.compare(sf, fit)
    assert [r['kind'] for r in rows] == ['D', 'R']
    assert abs(rows[0]['diffraction'] - 10.033) < 0.03 and abs(rows[1]['diffraction'] - 12.371) < 0.03
    assert all(abs(r['difference']) < 0.1 for r in rows)
    assert fit.curve(np.array([5.0, 11.0])) == pytest.approx([fit.baseline[0], fit.bottom[0]], abs=0.05)


def test_from_steps_double_auto_pairs_components():
    from pystarlight import steps
    t = np.arange(0, 24, 0.1)
    # the primary (70 %) goes first but comes back LAST: pairing by the size of the steps
    comps = [dif.Component([(10.02, 13.74)], 0.7), dif.Component([(10.63, 13.11)], 0.3)]
    y = _synthetic(comps, t, noise=0.01, seed=5)
    sf = steps.fit_steps(t, y)
    assert [tr.kind for tr in sf.transitions] == ['D', 'D', 'R', 'R']
    fit = dif.from_steps(sf, Z, 5.0, band=dif.Band.monochromatic(600))
    assert fit.flux_ratio is not None
    got = {k: v[0] for k, v in fit.times.items()}
    pairs = sorted([(got['D1'], got['R1']), (got['D2'], got['R2'])])
    assert pairs[0][0] == pytest.approx(10.02, abs=0.03) and pairs[0][1] == pytest.approx(13.74, abs=0.03)
    assert pairs[1][0] == pytest.approx(10.63, abs=0.03) and pairs[1][1] == pytest.approx(13.11, abs=0.03)
    rows = dif.compare(sf, fit)
    assert len(rows) == 4 and all(abs(r['difference']) < 0.1 for r in rows)


def test_components_and_pairing_ambiguity():
    t = np.arange(0, 24, 0.075)
    single = dif.fit_event(t, _synthetic([dif.Component([(10.03, 12.37)])], t), Z, 5.0, exposure=0.075,
                           band=dif.Band.monochromatic(600))
    c = single.components()
    assert len(c) == 1 and c[0]['chord_km'] == pytest.approx(5.0 * c[0]['duration_s'])
    # nearly equal components: which R belongs to which D cannot be told
    eq = [dif.Component([(10.02, 16.41)], 0.485), dif.Component([(13.64, 20.17)], 0.515)]
    y = _synthetic(eq, t, noise=0.02, exposure=0.075, seed=7)
    from pystarlight import steps
    fit = dif.from_steps(steps.fit_steps(t, y), Z, 5.0, band=dif.Band.monochromatic(600))
    comps = fit.components()
    assert comps[0]['ambiguous'] and comps[1]['ambiguous']
    durations = sorted(round(x['duration_s'], 1) for x in comps)
    alternatives = sorted(round(x['alternative']['duration_s'], 1) for x in comps)
    assert durations == [6.4, 6.5] and alternatives == [2.8, 10.2]
    # clearly unequal components: not ambiguous
    un = [dif.Component([(10.02, 16.41)], 0.8), dif.Component([(13.64, 20.17)], 0.2)]
    y = _synthetic(un, t, noise=0.02, exposure=0.075, seed=8)
    fit = dif.from_steps(steps.fit_steps(t, y), Z, 5.0, band=dif.Band.monochromatic(600))
    assert not fit.components()[0]['ambiguous']


def test_monte_carlo_sees_correlated_noise():
    t = np.arange(0, 20, 0.1)
    tr = dif.transmission(t, [dif.Component([(10.033, 12.371)])], Z, 5.0, dif.Band.monochromatic(600), 0.1)
    rng = np.random.default_rng(11)
    e = np.zeros(t.size)
    for i in range(1, t.size):                      # AR(1) noise, phi 0.6
        e[i] = 0.6 * e[i - 1] + rng.normal(0, 0.03 * np.sqrt(1 - 0.36))
    y = 0.05 + 0.95 * tr + e
    fit = dif.fit_event(t, y, Z, 5.0, exposure=0.1, band=dif.Band.monochromatic(600))
    before = dict(fit.times)
    dif.monte_carlo(fit, trials=40, seed=1)
    assert fit.mc['trials'] >= 35 and fit.mc['phi'] == pytest.approx(0.6, abs=0.2)
    for k in ('D', 'R'):
        assert fit.times[k][0] == before[k][0]                      # only the errors change
        assert fit.times[k][1] >= fit.sigma_fit[k]
        assert np.isfinite(fit.mc['sigma'][k]) and fit.mc['sigma'][k] > 0
    for k in ('D', 'R'):                            # same order as the covariance errors
        assert 0.3 < fit.mc['sigma'][k] / fit.sigma_fit[k] < 3
    rows = dif.compare(type('S', (), {'transitions': []})(), fit)        # no step fit: no rows, no error
    assert rows == []
