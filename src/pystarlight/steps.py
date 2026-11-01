"""Multi-step fit of a light curve: occultation disappearance (D) and reappearance (R) times.

NEW CODE. The curve is fitted in flux (linear, so an occulted star at zero flux or below is no
problem) with a piecewise-constant model:

1. change points by circular binary segmentation: the split, or the box (a short event inside
   a segment), that lowers the sum of squares most, kept while that drop exceeds `penalty` *
   ln(n) per change point in units of the noise variance (BIC-like);
2. backward elimination: drop a change point again when the two levels it separates are not
   significantly different any more (after the other splits);
3. each transition time to a fraction of a frame: the row the step falls in is a mix of both
   levels by the fraction of its exposure before the step (rows are assumed to be exposures of
   one frame interval centred on their time stamps);
4. 1-sigma time error: the larger of half the chi^2 + 1 interval and the scatter of the time
   fitted to simulated curves (the fitted step plus noise like the levels' scatter), which
   also counts a step landing a frame off. Levels are held fixed in both.

    from pystarlight import steps
    fit = steps.fit_steps(t, flux)              # t in s, flux e.g. normalised signal
    for tr in fit.transitions:
        print(tr.kind, tr.time, tr.sigma)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

MAD = 1.4826


@dataclass
class Level:
    start: int              # first row (index into the input arrays)
    stop: int               # last row + 1
    value: float            # mean flux
    error: float            # error of the mean
    scatter: float          # robust rms of the rows

    @property
    def n(self) -> int:
        return self.stop - self.start

    @property
    def limit(self) -> bool:
        """Within 3 sigma of zero flux: the star is gone, only a faint limit is known."""
        return not self.value > 3 * self.error

    def mag(self, m0: float = 0.0) -> tuple[float, float, float]:
        """(magnitude, error of the mean, per-frame scatter) on m = m0 - 2.5 log10(flux); for a
        limit level the 3 sigma limit ('fainter than') and NaN errors."""
        if self.limit:
            return float(to_mag(3 * self.error, m0)), float('nan'), float('nan')
        k = 2.5 / math.log(10) / self.value
        return float(to_mag(self.value, m0)), k * self.error, k * self.scatter


@dataclass
class Transition:
    index: int              # first row on the new level
    time: float             # s, on the axis of the input times
    sigma: float            # 1 sigma in s (chi^2 + 1 half width or Monte Carlo, the larger)
    lo: float               # chi^2 + 1 interval
    hi: float
    before: float           # level flux before and after
    after: float
    significance: float     # level change / its error
    sigma_mc: float = float('nan')      # Monte Carlo part of sigma

    @property
    def kind(self) -> str:
        """'D' (disappearance, flux drops) or 'R' (reappearance)."""
        return 'D' if self.after < self.before else 'R'

    @property
    def drop(self) -> float:
        """Flux change as a fraction of the brighter level."""
        top = max(self.before, self.after)
        return abs(self.before - self.after) / top if top > 0 else float('nan')


@dataclass
class StepFit:
    t: np.ndarray           # times of the fitted rows (NaN rows left out)
    y: np.ndarray           # their flux
    dt: float               # frame interval in s
    noise: float            # point-to-point noise of the flux
    levels: list[Level] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)

    def model(self, t: np.ndarray | None = None) -> np.ndarray:
        """Flux of the step model at the times t (default: the fitted rows), steps sharp."""
        t = self.t if t is None else np.asarray(t, dtype=float)
        y = np.full(t.shape, self.levels[0].value if self.levels else np.nan)
        for tr, lev in zip(self.transitions, self.levels[1:]):
            y[t >= tr.time] = lev.value
        return y

    def describe(self, t0: float = 0.0, fmt=None) -> str:
        """'D at 12.345 +- 0.021 s (drop 98 %, 25 sigma); R at ...' or 'no transition'.
        t0 is added to the times; fmt(t) formats them (e.g. as UTC)."""
        if not self.transitions:
            return 'no transition'
        fmt = fmt or (lambda s: f'{s:.3f} s')
        return '; '.join(f'{tr.kind} at {fmt(tr.time + t0)} ± {tr.sigma:.3f} s '
                         f'(change {100 * tr.drop:.0f} %, {tr.significance:.0f} sigma)'
                         for tr in self.transitions)


def _noise(y: np.ndarray) -> float:
    """Point-to-point noise: robust rms of the differences / sqrt 2 (steps barely affect it)."""
    d = np.diff(y)
    s = MAD * np.median(np.abs(d - np.median(d))) / math.sqrt(2) if d.size else 0.0
    return float(s) if s > 0 else float(np.std(y)) or 1.0


def _best_split(y: np.ndarray, min_len: int) -> tuple[int, float]:
    """(k, drop in the sum of squares) of the best single split y[:k] | y[k:]."""
    n = y.size
    if n < 2 * min_len:
        return -1, 0.0
    c = np.cumsum(y)
    k = np.arange(min_len, n - min_len + 1)
    s1, s2 = c[k - 1], c[-1] - c[k - 1]
    gain = s1 ** 2 / k + s2 ** 2 / (n - k) - c[-1] ** 2 / n
    i = int(np.argmax(gain))
    return int(k[i]), float(gain[i])


def _gain(y: np.ndarray, k: int) -> float:
    a, b = y[:k], y[k:]
    return float(a.size * b.size / y.size * (a.mean() - b.mean()) ** 2)


def _best_box(y: np.ndarray, min_len: int) -> tuple[int, int, float]:
    """(i, j, drop in the sum of squares) of the best box y[i:j] against the rest: a short event
    in the middle, which no single split finds (circular binary segmentation)."""
    n = y.size
    c = np.concatenate([[0.0], np.cumsum(y)])
    best = (-1, -1, 0.0)
    for L in range(min_len, n - 2 * min_len + 1):
        i = np.arange(min_len, n - min_len - L + 1)     # a box, not a split: rest on both sides
        if not i.size:
            break
        sb = c[i + L] - c[i]
        g = sb ** 2 / L + (c[-1] - sb) ** 2 / (n - L) - c[-1] ** 2 / n
        k = int(np.argmax(g))
        if g[k] > best[2]:
            best = (int(i[k]), int(i[k] + L), float(g[k]))
    return best


def _change_points(y: np.ndarray, sigma: float, threshold: float, min_len: int, max_steps: int) -> list[int]:
    cps: list[int] = []
    todo = [(0, y.size)]
    while todo and len(cps) < max_steps:
        best = None                     # (score, segment, new change points)
        for a, b in todo:
            k, g = _best_split(y[a:b], min_len)
            if k > 0 and (best is None or g / sigma ** 2 - threshold > best[0]):
                best = (g / sigma ** 2 - threshold, (a, b), [a + k])
            i, j, g = _best_box(y[a:b], min_len)
            if i > 0 and (best is None or g / sigma ** 2 - 2 * threshold > best[0]):
                best = (g / sigma ** 2 - 2 * threshold, (a, b), [a + i, a + j])
        if best is None or best[0] <= 0:
            break
        (a, b), new = best[1], best[2]
        todo.remove((a, b))
        cps += new
        edges = [a, *new, b]
        todo += list(zip(edges[:-1], edges[1:]))
    cps.sort()
    while cps:                          # backward elimination with all splits in place
        bounds = [0, *cps, y.size]
        gains = [_gain(y[bounds[i]:bounds[i + 2]], cps[i] - bounds[i]) for i in range(len(cps))]
        i = int(np.argmin(gains))
        if gains[i] / sigma ** 2 > threshold:
            break
        cps.pop(i)
    for _ in range(3):                  # move each point to its best place between its neighbours
        bounds = [0, *cps, y.size]
        for i in range(len(cps)):
            a, b = bounds[i], bounds[i + 2]
            k, _ = _best_split(y[a:b], min_len)
            if k > 0:
                cps[i] = bounds[i + 1] = a + k
    return cps


def _level(y: np.ndarray, a: int, b: int) -> Level:
    s = y[a:b]
    scatter = MAD * float(np.median(np.abs(s - np.median(s)))) if s.size > 2 else float('nan')
    return Level(a, b, float(s.mean()), float('nan'), scatter)


def _timing(t: np.ndarray, y: np.ndarray, dt: float, a: Level, b: Level, k: int,
            sa: float, sb: float, grid: int = 20, n_mc: int = 300,
            seed: int = 0) -> tuple[float, float, float, float]:
    """Best step time, its chi^2 + 1 interval and its Monte Carlo 1 sigma, with partial exposures,
    between the middles of the two levels (at most 30 rows either side). The Monte Carlo puts
    the fitted step plus noise of the levels' scatter through the same fit n_mc times: it also
    counts the chance of the step landing a frame off, which the local chi^2 profile misses."""
    i0 = max(a.start, k - min(30, max(2, (k - a.start) // 2)))
    i1 = min(b.stop, k + min(30, max(2, (b.stop - k) // 2)))
    ts, ys = t[i0:i1], y[i0:i1]
    tau = np.linspace(ts[0] - dt / 2, ts[-1] + dt / 2, (i1 - i0) * grid + 1)
    f = np.clip((tau[:, None] - (ts[None, :] - dt / 2)) / dt, 0.0, 1.0)     # exposure before tau
    model = b.value + (a.value - b.value) * f                               # (grid, rows)
    w = 1.0 / (f * sa ** 2 + (1 - f) * sb ** 2)

    def chi2(obs):                      # obs (m, rows) -> (m, grid)
        return (obs ** 2) @ w.T - 2 * obs @ (model * w).T + np.sum(model ** 2 * w, axis=1)
    c = chi2(ys[None, :])[0]
    j = int(np.argmin(c))
    ok = np.nonzero(c <= c[j] + 1.0)[0]
    step = float(tau[1] - tau[0])
    lo, hi = min(float(tau[ok[0]]), tau[j] - step / 2), max(float(tau[ok[-1]]), tau[j] + step / 2)
    rng = np.random.default_rng(seed)
    sims = model[j] + rng.standard_normal((n_mc, ys.size)) / np.sqrt(w[j])
    mc = float(np.std(tau[np.argmin(chi2(sims), axis=1)]))
    return float(tau[j]), lo, hi, mc


def fit_steps(t, y, penalty: float = 6.0, min_len: int = 2, max_steps: int = 10,
              min_significance: float = 5.0) -> StepFit:
    """Piecewise-constant fit of y (flux) against t (s); NaN rows are left out.
    penalty: a change point must lower chi^2 by more than penalty * ln(n) (6: about 6.4 sigma
    for 600 rows; red noise in a real curve needs more than the BIC's 2).
    min_significance: a kept step's level change in units of its error (with the levels'
    own scatter, which also counts slow wander the noise estimate misses)."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(t) & np.isfinite(y)
    rows = np.nonzero(ok)[0]
    tt, yy = t[ok], y[ok]
    dt = float(np.median(np.diff(tt))) if tt.size > 1 else 1.0
    sigma = _noise(yy)
    fit = StepFit(t=tt, y=yy, dt=dt, noise=sigma)
    if tt.size < 2 * min_len:
        return fit
    threshold = penalty * math.log(max(tt.size, 3))
    cps = _change_points(yy, sigma, threshold, min_len, max_steps)
    while True:
        bounds = [0, *cps, yy.size]
        levels = [_level(yy, bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
        for lev in levels:              # scatter for the errors: at least the point-to-point noise
            s = lev.scatter if np.isfinite(lev.scatter) else sigma
            lev.scatter = max(s, sigma)
            lev.error = lev.scatter / math.sqrt(lev.n)
        z = [abs(levels[i].value - levels[i + 1].value) / math.hypot(levels[i].error, levels[i + 1].error)
             for i in range(len(cps))]
        mid = [i for i in range(1, len(levels) - 1) if levels[i].n <= 2 * min_len
               and min(levels[i - 1].value, levels[i + 1].value) < levels[i].value
               < max(levels[i - 1].value, levels[i + 1].value)]
        if mid:                         # a frame or two part way through a step: not a level of its own
            i = mid[0]                  # join it to the nearer neighbour; _timing models the partial frames
            near = abs(levels[i].value - levels[i - 1].value) < abs(levels[i].value - levels[i + 1].value)
            cps.pop(i - 1 if near else i)
            continue
        if not z or min(z) >= min_significance:
            break
        cps.pop(int(np.argmin(z)))
    for i, k in enumerate(cps):
        a, b = levels[i], levels[i + 1]
        tau, lo, hi, mc = _timing(tt, yy, dt, a, b, k, a.scatter, b.scatter)
        fit.transitions.append(Transition(int(rows[k]), tau, max((hi - lo) / 2, mc), lo, hi,
                                          a.value, b.value, z[i], mc))
    for lev in levels:                  # back to the caller's row numbers
        lev.start = int(rows[lev.start])
        lev.stop = int(rows[lev.stop - 1]) + 1
    fit.levels = levels
    return fit


def to_mag(flux, m0: float = 0.0):
    """m0 - 2.5 log10(flux); NaN for flux <= 0."""
    f = np.asarray(flux, dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(f > 0, m0 - 2.5 * np.log10(f), np.nan)


def event_report(fit: StepFit, date: str = '', m0: float = 0.0, band: str = '') -> dict:
    """The step fit as plain data (JSON): levels with flux and magnitude on m0 - 2.5 log10(flux)
    (band, e.g. 'G'), transitions with UTC (fit times are seconds since midnight of `date`,
    YYYY-MM-DD), errors, and the D/R pairs nested from the inside out (D2 R1 inner, D1 R2
    outer) with their durations."""
    from datetime import datetime, timedelta

    def utc(s):
        if not date:
            return None
        try:
            d = datetime.strptime(date, '%Y-%m-%d') + timedelta(seconds=float(s))
        except ValueError:
            return None
        return d.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-2] + 'Z'          # 0.1 ms

    def num(v, nd=6):
        return round(float(v), nd) if v is not None and np.isfinite(v) else None
    levels = []
    edges = [fit.t[0] - fit.dt / 2, *[tr.time for tr in fit.transitions], fit.t[-1] + fit.dt / 2]
    for k, lv in enumerate(fit.levels):
        m, err, scatter = lv.mag(m0)
        levels.append({
            'start_utc': utc(edges[k]), 'end_utc': utc(edges[k + 1]),
            'duration_s': num(edges[k + 1] - edges[k], 4),
            'first_row': lv.start, 'last_row': lv.stop - 1, 'frames': lv.n,
            'flux': num(lv.value), 'flux_error': num(lv.error), 'flux_scatter': num(lv.scatter),
            'mag': None if lv.limit else num(m, 4), 'mag_error': num(err, 4),
            'mag_scatter_per_frame': num(scatter, 4),
            'fainter_than_mag_3sigma': num(m, 3) if lv.limit else None})
    transitions = []
    for i, tr in enumerate(fit.transitions):
        a, b = fit.levels[i].mag(m0)[0], fit.levels[i + 1].mag(m0)[0]
        transitions.append({
            'kind': tr.kind, 'utc': utc(tr.time), 'seconds_of_day': num(tr.time, 4),
            'sigma_s': num(tr.sigma, 4), 'sigma_mc_s': num(tr.sigma_mc, 4),
            'chi2_plus_1_interval_s': [num(tr.lo - tr.time, 4), num(tr.hi - tr.time, 4)],
            'first_row_after': tr.index, 'flux_before': num(tr.before), 'flux_after': num(tr.after),
            'change_fraction': num(tr.drop, 4), 'delta_mag': num(b - a, 4),
            'significance_sigma': num(tr.significance, 1)})
    pairs, open_d = [], []
    for k, tr in enumerate(fit.transitions):
        if tr.kind == 'D':
            open_d.append(k)
        elif open_d:
            d = open_d.pop()
            dd, rr = fit.transitions[d], tr
            pairs.append({'D': d, 'R': k, 'D_utc': utc(dd.time), 'R_utc': utc(rr.time),
                          'duration_s': num(rr.time - dd.time, 4),
                          'duration_sigma_s': num(math.hypot(dd.sigma, rr.sigma), 4)})
    return {'band': band, 'm0': num(m0, 4), 'frame_interval_s': num(fit.dt, 6),
            'noise_flux': num(fit.noise), 'rows_fitted': int(fit.t.size),
            'levels': levels, 'transitions': transitions, 'events': sorted(pairs, key=lambda p: p['D'])}
