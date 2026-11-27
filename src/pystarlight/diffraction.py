"""Diffraction-aware occultation light curves: Fresnel diffraction, finite (limb-darkened) star,
double star, camera bandpass, exposure time; least-squares fit of the event times.

NEW CODE (2026-11-26), with the sinc engine ported from Bob Anderson's (IOTA) notebooks, used with his
permission (he pointed us to the code for this purpose):

    https://github.com/bob-anderson-ok/Sinc-based-fresnel-diffraction  (Python notebooks)
    https://github.com/bob-anderson-ok/IOTAdiffraction                 (Go; double-star PSF)

which implement the sinc method of M. Cubillos and E. Jimenez, "Diffraction integral computation using
sinc approximation", Applied Numerical Mathematics 178 (2022), doi:10.1016/j.apnum.2022.02.011.

Two engines:

* the BAR model (1-D, exact in Fresnel integrals): every occultation is an opaque strip between its D
  and R edges, crossed perpendicularly at `v_perp` km/s; several strips add in the field (Babinet:
  E = 1 - sum of the strips' aperture fields), so short chords, a satellite or a second body are
  handled. A long chord gives the classic knife edge (0.25 at the geometric edge, first fringe 1.37).
  This engine is fast enough for least-squares fitting.
* the SINC engine (2-D): the field behind any source-plane silhouette, E = W S W with W the symmetric
  Toeplitz matrix of Fresnel-integral weights (only one row is computed); for irregular shapes and
  cross-checks (tests: a long strip in 2-D equals the bar model).

Wavelengths are summed INCOHERENTLY (intensities, weighted by the band): light of different
wavelengths does not interfere. (IOTAdiffraction sums the complex fields over the band, then squares;
the difference is small, at most ~1 % of the unocculted flux for 400-900 nm at 2.3 AU.)

The star: a disk with linear limb darkening I(mu) = 1 - u (1 - mu); its strip brightness (the disk
integrated along the edge) is exact. A double star: two components, each with its own edge times (the
separation projected on the edge normal differs between D and R), added in intensity with the flux
fraction of the primary `f`.

Times are mid-exposure (as plot.seconds in the viewer). Lengths in km, distances in AU.

    from pystarlight import diffraction as dif
    band = dif.Band.monochromatic(600)                   # or Band.from_table('qe.txt', teff=5800)
    fit = dif.fit_event(t, flux, distance_au=2.33, v_perp=5.1, exposure=0.1, band=band, double=False)
    print(fit.report())
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares
from scipy.signal import fftconvolve
from scipy.special import fresnel

AU_KM = 1.495978707e8
MAS_RAD = math.pi / 180 / 3600 / 1000


def fresnel_scale_km(wavelength_nm: float, distance_au: float) -> float:
    """sqrt(lambda Z / 2): the width of the first Fresnel zone (km)."""
    return math.sqrt(wavelength_nm * 1e-12 * distance_au * AU_KM / 2)


def star_diameter_km(diameter_mas: float, distance_au: float) -> float:
    """The star's angular diameter projected to the distance of the occulting body (km)."""
    return diameter_mas * MAS_RAD * distance_au * AU_KM


# ---------------------------------------------------------------------------------------------
# camera bandpass
# ---------------------------------------------------------------------------------------------

@dataclass
class Band:
    """Wavelengths (nm) and normalised weights of the detected photons."""
    wavelengths_nm: np.ndarray
    weights: np.ndarray

    @classmethod
    def monochromatic(cls, wavelength_nm: float = 600.0) -> Band:
        """A single effective wavelength (600 nm suits unfiltered mono CMOS sensors)."""
        return cls(np.array([float(wavelength_nm)]), np.array([1.0]))

    @classmethod
    def from_table(cls, table, teff: float | None = None, step_nm: float = 20.0) -> Band:
        """A sensor QE curve: a file or array of (wavelength_nm, QE) rows (any QE scale; '#'
        comments, commas or blanks), resampled every `step_nm`. With `teff` (K) the weights are QE
        times the star's photon spectrum (black body); without, QE alone (a flat star spectrum)."""
        if isinstance(table, (str, bytes)) or hasattr(table, '__fspath__'):
            rows = []
            with open(table) as fh:
                for line in fh:
                    line = line.split('#')[0].replace(',', ' ').replace('[', ' ').replace(']', ' ').strip()
                    if line:
                        try:
                            rows.append([float(v) for v in line.split()[:2]])
                        except ValueError:
                            continue
            table = rows
        a = np.asarray(table, dtype=float)
        if a.ndim != 2 or a.shape[1] < 2 or len(a) < 2:
            raise ValueError('a QE table needs at least two (wavelength_nm, QE) rows')
        a = a[np.argsort(a[:, 0])]
        lam = np.arange(a[0, 0], a[-1, 0] + 0.5 * step_nm, step_nm)
        w = np.clip(np.interp(lam, a[:, 0], a[:, 1]), 0, None)
        if teff:
            h, c, k = 6.62607015e-34, 2.99792458e8, 1.380649e-23
            lm = lam * 1e-9
            w = w * lm ** -4 / np.expm1(h * c / (lm * k * teff))      # photons: B_lambda * lambda
        keep = w > 1e-4 * w.max()
        return cls(lam[keep], w[keep] / w[keep].sum())

    @property
    def effective_nm(self) -> float:
        return float(np.sum(self.wavelengths_nm * self.weights))


# ---------------------------------------------------------------------------------------------
# bar model (1-D)
# ---------------------------------------------------------------------------------------------

def _aperture_field(x, a, b, wavelength_km, z_km):
    """Field through an open strip a < x' < b seen at x (unit incident wave, far-field phase
    dropped): (1 - i)/2 [F(v_b) - F(v_a)], F = C + iS, v = (edge - x) sqrt(2 / (lambda Z))."""
    s = math.sqrt(2.0 / (wavelength_km * z_km))
    sb, cb = fresnel((b - x) * s)
    sa, ca = fresnel((a - x) * s)
    return 0.5 * (1 - 1j) * ((cb - ca) + 1j * (sb - sa))


def bar_intensity(x_km, strips_km, wavelength_nm: float, distance_au: float) -> np.ndarray:
    """Monochromatic intensity (unocculted = 1) of a point star behind opaque strips
    [(a, b), ...] (km along the edge normal), observer at x_km."""
    x = np.asarray(x_km, dtype=float)
    lam, z = wavelength_nm * 1e-12, distance_au * AU_KM
    e = np.ones(x.shape, dtype=complex)
    for a, b in strips_km:
        e -= _aperture_field(x, min(a, b), max(a, b), lam, z)
    return e.real ** 2 + e.imag ** 2


def strip_profile(diameter_km: float, limb: float, dx: float) -> np.ndarray:
    """Normalised 1-D brightness of a limb-darkened disk integrated along a straight edge
    (linear law 1 - u (1 - mu)), sampled every dx km; [1.0] for a point star."""
    r = diameter_km / 2
    if r <= dx / 2:
        return np.array([1.0])
    n = int(math.ceil(r / dx))
    s = np.arange(-n, n + 1) * dx
    q = np.clip(r * r - s * s, 0, None)
    p = (1 - limb) * 2 * np.sqrt(q) + limb * (math.pi / 2) * q / r
    return p / p.sum()


@dataclass
class Component:
    """One star of the (double) star: its edge times (s) and size."""
    events: list                # [(t_D, t_R), ...] one per occultation (strip); t_R may be None
    flux: float = 1.0           # share of the total starlight
    diameter_km: float = 0.0
    limb: float = 0.6


def transmission(t, components: list[Component], distance_au: float, v_perp: float,
                 band: Band | None = None, exposure: float = 0.0, oversample: int = 10) -> np.ndarray:
    """Share of the starlight received (1 = all) at mid-exposure times t (s) for a (double) star
    behind opaque strips crossed at v_perp km/s: Fresnel diffraction summed over the band, the
    star's strip brightness, a box exposure of `exposure` s."""
    band = band or Band.monochromatic()
    t = np.asarray(t, dtype=float)
    z = distance_au
    fz = min(fresnel_scale_km(wl, z) for wl in band.wavelengths_nm)
    total = np.zeros(t.shape)
    for comp in components:
        strips = []
        for td, tr in comp.events:
            tr = t.max() + 1e6 / max(v_perp, 1e-9) if tr is None else tr     # no R: a knife edge
            strips.append((td * v_perp, tr * v_perp))
        xs = t * v_perp
        if comp.diameter_km <= 0 and exposure <= 0:
            # point star, instantaneous samples: exact at the sample points, no grid
            out = np.zeros(xs.shape)
            for lam, w in zip(band.wavelengths_nm, band.weights):
                out += w * bar_intensity(xs, strips, lam, z)
            total += comp.flux * out
            continue
        ext = 3 * comp.diameter_km + v_perp * exposure
        # the full calculation only in windows of +-`margin` around each edge: deep inside a strip
        # the transmission is 0, outside all strips 1 (monochromatic fringes beyond the margin:
        # < 1.5 %, averaged away by band, star and exposure). The grid samples the fringes out to
        # the margin: their local period at a distance d from an edge is ~ f^2 / d, so
        # dx <= f / (2 * 30) (oversample = 10 -> f/60)
        mz = 30
        margin = mz * fz + ext
        dx = min(fz / (oversample * mz / 5), comp.diameter_km / 20 if comp.diameter_km else np.inf,
                 v_perp * exposure / 10 if exposure else np.inf)
        k = strip_profile(comp.diameter_km, comp.limb, dx)
        if exposure > 0:
            nb = max(1, int(round(v_perp * exposure / dx)))
            k = np.convolve(k, np.ones(nb) / nb)
        edges = sorted(e for st in strips for e in st if xs.min() - margin <= e <= xs.max() + margin)
        windows = []
        for e in edges:
            lo, hi = e - margin, e + margin
            if windows and lo <= windows[-1][1]:
                windows[-1][1] = hi
            else:
                windows.append([lo, hi])
        out = np.ones(xs.shape)
        for a, b in strips:                         # geometric shadow outside the windows
            out[(xs > a) & (xs < b)] = 0.0
        for lo, hi in windows:
            inside = (xs >= lo) & (xs <= hi)
            if not inside.any():
                continue
            n = int((hi - lo) / dx) + 1
            step = dx
            if n > 400_000:                         # very wide windows: keep the work bounded
                step = (hi - lo) / 400_000
                n = 400_001
            grid = lo + step * np.arange(n)
            inten = np.zeros(n)
            for lam, w in zip(band.wavelengths_nm, band.weights):
                inten += w * bar_intensity(grid, strips, lam, z)
            if len(k) > 1:
                pad = len(k)
                inten = fftconvolve(np.pad(inten, pad, mode='edge'), k, mode='same')[pad:-pad]
            val = np.interp(xs[inside], grid, inten)
            # taper to the geometric value over the outer third of the margin, so the model stays
            # continuous when an edge time (and with it the window) moves: a step there would
            # spoil the numerical derivatives of the fit
            d_edge = np.minimum(xs[inside] - lo, hi - xs[inside])
            w = np.clip(d_edge / (margin / 3), 0, 1)
            w = w * w * (3 - 2 * w)
            out[inside] = w * val + (1 - w) * out[inside]
        total += comp.flux * out
    return total / sum(c.flux for c in components)


# ---------------------------------------------------------------------------------------------
# fit
# ---------------------------------------------------------------------------------------------

@dataclass
class DiffractionFit:
    times: dict                     # name -> (time, sigma) e.g. 'D' / 'R' or 'D1', 'R1', 'D2', 'R2'
    baseline: tuple                 # (value, sigma): flux with the whole star visible
    bottom: tuple                   # (value, sigma): flux with the whole star hidden
    flux_ratio: tuple | None        # (f, sigma): primary's share of the starlight (double star)
    diameter_km: tuple | None       # (value, sigma) when fitted
    v_perp: tuple                   # (value, sigma or nan)
    chi2: float
    dof: int
    noise: float
    model: np.ndarray = field(repr=False)
    params: dict = field(default_factory=dict, repr=False)
    curve: object = field(default=None, repr=False)     # curve(t): the fitted model at any times
    settings: dict = field(default_factory=dict)        # distance, speeds, band, exposure, star
    data: tuple = field(default=None, repr=False)       # (t, y) fitted
    refit: object = field(default=None, repr=False)     # refit(y): the same fit of other data
    sigma_fit: dict = field(default_factory=dict)       # name -> sigma from the covariance alone
    mc: dict | None = None                              # monte_carlo(): trials, phi, sigma per name

    def components(self) -> list[dict]:
        """Per component of a double star (one entry for a single star): its D, R, duration and
        chord (duration x v_perp, km). For a double star whose light share is within
        max(3 sigma, 0.05) of 0.5 the D-R pairing cannot be told from this light curve (swapping
        the R times gives the same curve): 'ambiguous' and the other pairing's durations / chords
        under 'alternative'."""
        vp = self.v_perp[0]
        if self.flux_ratio is None:
            d, r = self.times['D'][0], self.times['R'][0]
            return [{'name': 'star', 'share': 1.0, 'D': 'D', 'R': 'R', 'duration_s': r - d,
                     'chord_km': (r - d) * vp, 'ambiguous': False}]
        f, fs = self.flux_ratio
        amb = abs(f - 0.5) <= max(3 * fs if np.isfinite(fs) else 0.0, 0.05)
        out = []
        for k, share in ((1, f), (2, 1 - f)):
            d, r = self.times[f'D{k}'][0], self.times[f'R{k}'][0]
            o = self.times[f'R{3 - k}'][0]
            out.append({'name': f'component {k}', 'share': share, 'D': f'D{k}', 'R': f'R{k}',
                        'duration_s': r - d, 'chord_km': (r - d) * vp, 'ambiguous': amb,
                        'alternative': {'R': f'R{3 - k}', 'duration_s': o - d, 'chord_km': (o - d) * vp}})
        return out

    def report(self) -> str:
        lines = [f"{k}: {v[0]:.4f} +- {v[1]:.4f} s" for k, v in self.times.items()]
        lines.append(f"baseline {self.baseline[0]:.4g} +- {self.baseline[1]:.2g}, bottom "
                     f"{self.bottom[0]:.4g} +- {self.bottom[1]:.2g}")
        if self.flux_ratio:
            lines.append(f"primary's share of the light {self.flux_ratio[0]:.3f} +- {self.flux_ratio[1]:.3f}")
        if self.diameter_km:
            lines.append(f"star diameter {self.diameter_km[0]:.3f} +- {self.diameter_km[1]:.3f} km")
        lines.append(f"v_perp {self.v_perp[0]:.3f} km/s; chi2/dof {self.chi2 / max(self.dof, 1):.2f} "
                     f"(noise {self.noise:.3g})")
        return '\n'.join(lines)


def _noise(y):
    d = np.diff(y)
    return float(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2)) or float(np.std(y)) or 1.0


def _initial(t, y, double):
    """Starting values from the multi-step fit: (baseline, bottom, [(tD, tR)] per component)."""
    from . import steps
    fit = steps.fit_steps(t, y)
    lv = [lev.value for lev in fit.levels]
    base, bottom = max(lv[0], lv[-1]), min(lv)
    ds = [tr.time for tr in fit.transitions if tr.kind == 'D']
    rs = [tr.time for tr in fit.transitions if tr.kind == 'R']
    if not ds or not rs:
        raise ValueError('no D / R transition found to start the diffraction fit')
    if double:
        d1, d2 = ds[0], ds[1] if len(ds) > 1 else ds[0] + 0.5 * (rs[-1] - ds[0]) / 10
        r1, r2 = (rs[0], rs[-1]) if len(rs) > 1 else (rs[0] - 0.05 * (rs[0] - ds[0]), rs[0])
        return base, bottom, [(d1, r1), (d2, r2)], 0.5
    return base, bottom, [(ds[0], rs[-1])], 1.0


def fit_event(t, flux, distance_au: float, v_perp: float, exposure: float = 0.0, band: Band | None = None,
              double: bool = False, diameter_km: float = 0.0, limb: float = 0.6, fit_diameter: bool = False,
              fit_speed: bool = False, sigma: float | None = None, initial=None) -> DiffractionFit:
    """Least-squares fit of one occultation (one strip) with diffraction, finite star, band and
    exposure. Free: baseline, bottom, D and R time (per component for a double star), the
    primary's share of the light (double), optionally the star diameter (km, both components) and
    the perpendicular shadow speed. `initial` = (baseline, bottom, [(tD, tR), ...], f) overrides the
    start from steps.fit_steps. Errors: from the Jacobian, scaled by the noise (`sigma` or the
    point-to-point scatter)."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(flux, dtype=float)
    sig = float(sigma) if sigma else _noise(y)
    base0, bot0, ev0, f0 = initial if initial is not None else _initial(t, y, double)
    names = ['baseline', 'bottom']
    p0 = [base0, bot0]
    for i, (td, tr) in enumerate(ev0):
        sfx = str(i + 1) if double else ''
        names += ['D' + sfx, 'R' + sfx]
        p0 += [td, tr]
    if double:
        names.append('f')
        p0.append(f0)
    if fit_diameter:
        names.append('diameter')
        f_eff = fresnel_scale_km((band or Band.monochromatic()).effective_nm, distance_au)
        p0.append(max(diameter_km, f_eff))
    if fit_speed:
        names.append('v_perp')
        p0.append(v_perp)
    idx = {n: i for i, n in enumerate(names)}

    def model(p, tt=t):
        dia = abs(p[idx['diameter']]) if fit_diameter else diameter_km
        vp = abs(p[idx['v_perp']]) if fit_speed else v_perp
        if double:
            f = min(max(p[idx['f']], 0.0), 1.0)
            comps = [Component([(p[idx['D1']], p[idx['R1']])], f, dia, limb),
                     Component([(p[idx['D2']], p[idx['R2']])], 1 - f, dia, limb)]
        else:
            comps = [Component([(p[idx['D']], p[idx['R']])], 1.0, dia, limb)]
        tr = transmission(tt, comps, distance_au, vp, band, exposure)
        return p[0] * tr + p[1] * (1 - tr)

    dt = float(np.median(np.diff(t))) if len(t) > 1 else 1.0
    scale = [abs(base0 - bot0) or 1.0] * 2 + [dt] * (len(ev0) * 2) + ([0.1] if double else []) \
        + ([0.1] if fit_diameter else []) + ([0.1 * v_perp] if fit_speed else [])
    # finite-difference steps in absolute terms (times are seconds of the day: a relative step
    # would be far too coarse there): 1/100 frame for times, small fractions for the rest
    absstep = [1e-4 * scale[0], 1e-4 * scale[1]] + [dt / 100] * (len(ev0) * 2) + ([1e-4] if double else []) \
        + ([1e-3] if fit_diameter else []) + ([1e-4 * v_perp] if fit_speed else [])
    rel = [h / max(1.0, abs(x)) for h, x in zip(absstep, p0)]
    res = least_squares(lambda p: (model(p) - y) / sig, p0, x_scale=scale, method='trf', diff_step=rel)
    p = res.x
    m = model(p)
    dof = max(len(y) - len(p), 1)
    chi2 = float(np.sum(((m - y) / sig) ** 2))
    try:
        cov = np.linalg.inv(res.jac.T @ res.jac) * max(chi2 / dof, 1.0)
        err = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        err = np.full(len(p), np.nan)
    times = {n: (float(p[i]), float(err[i])) for n, i in idx.items() if n[0] in 'DR'}
    return DiffractionFit(
        times=times, baseline=(float(p[0]), float(err[0])), bottom=(float(p[1]), float(err[1])),
        flux_ratio=(float(p[idx['f']]), float(err[idx['f']])) if double else None,
        diameter_km=(abs(float(p[idx['diameter']])), float(err[idx['diameter']])) if fit_diameter else None,
        v_perp=((abs(float(p[idx['v_perp']])), float(err[idx['v_perp']])) if fit_speed
                else (v_perp, float('nan'))),
        chi2=chi2, dof=dof, noise=sig, model=m, params=dict(zip(names, map(float, p))),
        curve=lambda tt, _p=p.copy(): model(_p, np.asarray(tt, dtype=float)),
        data=(t, y), sigma_fit={n: v[1] for n, v in times.items()},
        refit=lambda yy, _p=p.copy(): fit_event(
            t, yy, distance_au, v_perp, exposure, band, double, diameter_km, limb, fit_diameter, fit_speed,
            sigma, initial=_initial_from(_p, idx, double)),
        settings={'distance_au': distance_au, 'v_perp_km_s': v_perp, 'exposure_s': exposure,
                  'band_effective_nm': (band or Band.monochromatic()).effective_nm,
                  'band_points': len((band or Band.monochromatic()).wavelengths_nm),
                  'star_diameter_km': diameter_km, 'limb_darkening': limb, 'double': double,
                  'fresnel_scale_km': fresnel_scale_km((band or Band.monochromatic()).effective_nm,
                                                       distance_au)})


def _initial_from(p, idx, double):
    """fit_event's `initial` from fitted parameters (a refit starts at the best fit)."""
    if double:
        ev = [(p[idx['D1']], p[idx['R1']]), (p[idx['D2']], p[idx['R2']])]
        return p[0], p[1], ev, p[idx['f']]
    return p[0], p[1], [(p[idx['D']], p[idx['R']])], 1.0


def monte_carlo(dfit: DiffractionFit, trials: int = 100, seed: int = 0, progress=None) -> DiffractionFit:
    """Monte-Carlo errors: the fitted model plus simulated noise like the residuals (their rms and
    their frame-to-frame correlation, AR(1) coefficient phi), refitted `trials` times. Per time the MC
    sigma is the 16-84 % half width of the refitted values (robust, catches a step landing a frame
    off); the quoted sigma becomes max(covariance sigma, MC sigma), as in steps.fit_steps. Fills
    dfit.mc and updates dfit.times (sigma_fit keeps the covariance part). progress(k, trials)."""
    t, y = dfit.data
    res = y - dfit.model
    sd = float(np.std(res))
    r0, r1 = res[:-1] - res.mean(), res[1:] - res.mean()
    phi = float(np.clip(np.sum(r0 * r1) / max(np.sum(r0 * r0), 1e-300), -0.9, 0.9))
    rng = np.random.default_rng(seed)
    got = {n: [] for n in dfit.times}
    shares = []
    failed = 0
    for k in range(trials):
        e = np.empty(len(t))
        e[0] = rng.normal(0, sd)
        innov = rng.normal(0, sd * math.sqrt(1 - phi * phi), len(t))
        for i in range(1, len(t)):
            e[i] = phi * e[i - 1] + innov[i]
        try:
            f = dfit.refit(dfit.model + e)
        except (ValueError, np.linalg.LinAlgError):
            failed += 1
            continue
        for n in got:
            got[n].append(f.times[n][0])
        if f.flux_ratio is not None:
            shares.append(f.flux_ratio[0])
        if progress:
            progress(k + 1, trials)
    sig = {}
    for n, v in got.items():
        v = np.asarray(v)
        sig[n] = float((np.percentile(v, 84) - np.percentile(v, 16)) / 2) if v.size >= 5 else float('nan')
        dfit.times[n] = (dfit.times[n][0], float(np.nanmax([dfit.sigma_fit[n], sig[n]])))
    dfit.mc = {'trials': trials - failed, 'failed': failed, 'phi': phi, 'noise_rms': sd, 'sigma': sig}
    if shares:
        ss = np.asarray(shares)
        dfit.mc['share_sigma'] = float((np.percentile(ss, 84) - np.percentile(ss, 16)) / 2)
        dfit.flux_ratio = (dfit.flux_ratio[0], float(max(dfit.flux_ratio[1], dfit.mc['share_sigma'])))
    return dfit


def from_steps(stepfit, distance_au: float, v_perp: float, exposure: float | None = None,
               band: Band | None = None, double='auto', diameter_km: float = 0.0, limb: float = 0.6,
               fit_diameter: bool = False, pad_frames: int = 25) -> DiffractionFit:
    """The diffraction fit of the main event of a steps.StepFit (first D to last R), on its data
    (stepfit.t, .y) within pad_frames of the event, started from its levels and times.
    exposure: default the frame interval (as the step fit assumes). double: True / False / 'auto'
    (two D and two R steps). For a double star the steps are paired into components by the light
    each one takes away / brings back."""
    tr = stepfit.transitions
    ds = [x for x in tr if x.kind == 'D']
    rs = [x for x in tr if x.kind == 'R']
    if not ds or not rs or rs[-1].time <= ds[0].time:
        raise ValueError('the step fit has no D followed by an R')
    dt = float(stepfit.dt)
    exposure = dt if exposure is None else exposure
    t, y = np.asarray(stepfit.t, dtype=float), np.asarray(stepfit.y, dtype=float)
    sel = (t >= ds[0].time - pad_frames * dt) & (t <= rs[-1].time + pad_frames * dt)
    t, y = t[sel], y[sel]
    base = max(ds[0].before, rs[-1].after)
    bottom = min(min(x.after for x in ds), min(x.before for x in rs))
    if double == 'auto':
        double = len(ds) >= 2 and len(rs) >= 2
    if double:
        d1, d2, r1, r2 = ds[0], ds[1], rs[-2], rs[-1]
        span = (base - bottom) or 1.0
        f_d1 = (d1.before - d1.after) / span           # light lost at the first D
        f_r1 = (r1.after - r1.before) / span           # light back at the first R
        f_r2 = (r2.after - r2.before) / span
        # the component that went first comes back at the R step of the same size
        if abs(f_r1 - f_d1) <= abs(f_r2 - f_d1):
            events = [(d1.time, r1.time), (d2.time, r2.time)]
        else:
            events = [(d1.time, r2.time), (d2.time, r1.time)]
        initial = (base, bottom, events, min(max(f_d1, 0.05), 0.95))
    else:
        initial = (base, bottom, [(ds[0].time, rs[-1].time)], 1.0)
    return fit_event(t, y, distance_au, v_perp, exposure=exposure, band=band, double=bool(double),
                     diameter_km=diameter_km, limb=limb, fit_diameter=fit_diameter, initial=initial)


def compare(stepfit, dfit: DiffractionFit) -> list[dict]:
    """The event times of both fits side by side: [{'kind', 'step', 'step_sigma', 'diffraction',
    'diffraction_sigma', 'difference'}] in time order (seconds as fitted; difference =
    diffraction - step)."""
    out = []
    for kind in ('D', 'R'):
        steps_k = sorted((x for x in stepfit.transitions if x.kind == kind), key=lambda x: x.time)
        dif_k = sorted(((n, v) for n, v in dfit.times.items() if n.startswith(kind)), key=lambda nv: nv[1][0])
        if len(dif_k) == 1 and steps_k:                 # single star: first D / last R of the step fit
            steps_k = [steps_k[0] if kind == 'D' else steps_k[-1]]
        elif len(steps_k) > len(dif_k):
            steps_k = steps_k[:len(dif_k)] if kind == 'D' else steps_k[-len(dif_k):]
        for st, (name, (tv, ts)) in zip(steps_k, dif_k):
            out.append({'kind': name, 'step': st.time, 'step_sigma': st.sigma, 'diffraction': tv,
                        'diffraction_sigma': ts, 'difference': tv - st.time,
                        'diffraction_sigma_fit': dfit.sigma_fit.get(name, ts),
                        'diffraction_sigma_mc': (dfit.mc or {}).get('sigma', {}).get(name, float('nan'))})
    return sorted(out, key=lambda r: r['step'])


# ---------------------------------------------------------------------------------------------
# sinc engine (2-D), after Bob Anderson's Sinc-diffraction-module (Cubillos & Jimenez 2022)
# ---------------------------------------------------------------------------------------------

def sinc_weights_row(n: int, width_km: float, wavelength_nm: float, distance_au: float) -> np.ndarray:
    """Top row of the symmetric Toeplitz Fresnel-weight matrix W (n x n) for a square plane of
    width_km (sample spacing width_km / n)."""
    z = distance_au * AU_KM
    lam = wavelength_nm * 1e-12
    dx = width_km / n
    k = 2 * math.pi / lam
    w = 1 / (2 * dx)
    t1 = -math.pi * math.sqrt(2 * z / k) * w
    t2 = math.sqrt(k / (2 * z))
    t4 = (dx / math.pi) * math.sqrt(k / (2 * z)) * math.sqrt(math.pi / 2)
    t5 = k / (2 * z)
    t6 = math.sqrt(2 / math.pi)
    slide = np.arange(n) * dx
    s1, c1 = fresnel((t1 - t2 * slide) * t6)
    s2, c2 = fresnel((-t1 - t2 * slide) * t6)
    return t4 * np.exp(1j * slide ** 2 * t5) * ((c2 - c1) - 1j * (s2 - s1))


def _toeplitz(row):
    n = len(row)
    i = np.arange(n)
    return row[np.abs(i[:, None] - i[None, :])]


def sinc_field(source: np.ndarray, width_km: float, wavelength_nm: float, distance_au: float) -> np.ndarray:
    """Field (unit incident wave) behind an n x n source plane of width_km; source = 1 where the
    plane is OPEN (an aperture). For an occulting body pass its silhouette (1 = body) and use
    I = |1 - E|^2 (Babinet), as sinc_intensity does."""
    w = _toeplitz(sinc_weights_row(source.shape[0], width_km, wavelength_nm, distance_au))
    return w @ source.astype(complex) @ w


def sinc_intensity(body: np.ndarray, width_km: float, band: Band | None = None, distance_au: float = 1.0,
                   row: int | None = None) -> np.ndarray:
    """Intensity behind an occulting silhouette (body = 1 inside), summed over the band
    (incoherently); the full plane, or one row (cheap: O(n^2) per wavelength)."""
    band = band or Band.monochromatic()
    n = body.shape[0]
    out = None
    for lam, wt in zip(band.wavelengths_nm, band.weights):
        r = sinc_weights_row(n, width_km, lam, distance_au)
        if row is None:
            e = 1 - sinc_field(body, width_km, lam, distance_au)
        else:
            v1 = r[np.abs(row - np.arange(n))]                # row `row` of W
            v2 = v1 @ body.astype(complex)
            e = 1 - v2 @ _toeplitz(r)
        i = wt * (e.real ** 2 + e.imag ** 2)
        out = i if out is None else out + i
    return out
