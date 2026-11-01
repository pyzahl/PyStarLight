"""Astrometric reduction against Gaia DR3, in the manner of PRAIA (Assafin et al. 2011).

The astrometry.net WCS only serves to match: stars measured in the (stacked) image are paired
with the stars of PySyzygos' local Gaia DR3 catalogue (positions moved to the observation date
with their proper motions), then (x, y) are related to the tangent-plane coordinates (xi, eta)
of the catalogue by a polynomial: 4 constants (scale, rotation, offset; with or without mirror
image), or complete 1st to 3rd order as the number of stars allows. Outliers are dropped one by
one, the worst first, while it is more than `clip` sigma (and `clip_arcsec`, if given) off.

What it gives on top of the plate solution: the residuals (O-C) of the reference stars, so a
measured precision; RA/Dec of any measured position with an error; and for apertures the offset
from the catalogue star there (a wrong or blended target shows up). Command line:

    python -m pystarlight.wcs.reduce MOVIE      (needs <movie>.wcs.fit and the local catalogue)
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

MODELS = {0: '4 constants', 1: '1st order', 2: '2nd order', 3: '3rd order'}


def tangent(ra, dec, ra0: float, dec0: float):
    """Gnomonic projection: (RA, Dec) deg -> standard coordinates (xi, eta) in arcsec."""
    a, d = np.radians(ra) - math.radians(ra0), np.radians(dec)
    d0 = math.radians(dec0)
    den = np.sin(d) * math.sin(d0) + np.cos(d) * math.cos(d0) * np.cos(a)
    xi = np.cos(d) * np.sin(a) / den
    eta = (np.sin(d) * math.cos(d0) - np.cos(d) * math.sin(d0) * np.cos(a)) / den
    return np.degrees(xi) * 3600.0, np.degrees(eta) * 3600.0


def untangent(xi, eta, ra0: float, dec0: float):
    """(xi, eta) arcsec -> (RA, Dec) deg."""
    x, y = np.radians(np.asarray(xi) / 3600.0), np.radians(np.asarray(eta) / 3600.0)
    d0 = math.radians(dec0)
    den = math.cos(d0) - y * math.sin(d0)
    ra = math.radians(ra0) + np.arctan2(x, den)
    dec = np.arctan2(math.sin(d0) + y * math.cos(d0), np.hypot(x, den))
    return np.degrees(ra) % 360.0, np.degrees(dec)


def _terms(u, v, degree: int) -> np.ndarray:
    """Design matrix of a complete polynomial in (u, v): 1, u, v, u^2, uv, v^2, ..."""
    u, v = np.atleast_1d(u), np.atleast_1d(v)
    return np.column_stack([u ** (n - j) * v ** j for n in range(degree + 1) for j in range(n + 1)])


def n_terms(degree: int) -> int:
    """Parameters per axis (the 4-constant model shares its 4 between both axes)."""
    return 2 if degree == 0 else (degree + 1) * (degree + 2) // 2


@dataclass
class RefStar:
    x: float
    y: float
    ra: float                 # catalogue, at the observation date
    dec: float
    g: Optional[float]
    source_id: int
    used: bool = True
    o_c_ra: float = 0.0       # observed - catalogue, arcsec (RA times cos Dec)
    o_c_dec: float = 0.0
    fwhm: float = float('nan')
    mag: Optional[float] = None       # measured, on the Gaia G scale (measure_mags)
    mag_err: Optional[float] = None


@dataclass
class MagScale:
    """Instrumental magnitudes tied to Gaia G: mag = -2.5 log10(flux) + zero_point."""
    zero_point: float
    sigma: float              # rms of (mag - G) of the stars used: the field's photometric scatter
    n_used: int
    radius: float             # aperture radius, px
    fwhm: float               # median FWHM of the stars, px

    @property
    def zp_err(self) -> float:
        return self.sigma / math.sqrt(max(self.n_used, 1))


@dataclass
class Reduction:
    """A plate solution from the catalogue. Pixel (x, y) is 0-based, (0, 0) the first pixel's
    centre, as everywhere in PyStarLight."""
    degree: int
    mirrored: bool
    ra0: float                 # tangent point
    dec0: float
    cx: float                  # pixel normalisation: u = (x - cx) / norm
    cy: float
    norm: float
    coef_xi: list
    coef_eta: list
    cov_xi: list               # (A^T A)^-1, for errors at any position
    cov_eta: list
    sigma_ra: float            # rms of the used stars' O-C, arcsec (with the fit's degrees of freedom)
    sigma_dec: float
    epoch: float
    stars: list = field(default_factory=list)      # RefStar
    frame: Optional[int] = None                    # the frame reduced (None: the solved stack)
    photometry: Optional[MagScale] = None

    @property
    def n_used(self) -> int:
        return sum(s.used for s in self.stars)

    @property
    def model(self) -> str:
        return MODELS[self.degree] + (' (mirror image)' if self.degree == 0 and self.mirrored else '')

    def _design(self, x, y):
        u = (np.atleast_1d(np.asarray(x, float)) - self.cx) / self.norm
        v = (np.atleast_1d(np.asarray(y, float)) - self.cy) / self.norm
        return _design(u, v, self.degree, self.mirrored)

    def standard(self, x, y):
        """(x, y) -> (xi, eta) arcsec."""
        ax, ay = self._design(x, y)
        return ax @ np.asarray(self.coef_xi), ay @ np.asarray(self.coef_eta)

    def world(self, x, y):
        """(x, y) -> (RA, Dec) deg; scalars for scalar input."""
        ra, dec = untangent(*self.standard(x, y), self.ra0, self.dec0)
        return (float(ra[0]), float(dec[0])) if np.ndim(x) == 0 else (ra, dec)

    def error(self, x, y) -> tuple[float, float]:
        """Error (arcsec) of the RA (times cos Dec) and Dec of a star measured at (x, y) as well
        as the reference stars are: their scatter plus the model's uncertainty there."""
        ax, ay = self._design(x, y)
        hx = float(ax[0] @ np.asarray(self.cov_xi) @ ax[0])
        hy = float(ay[0] @ np.asarray(self.cov_eta) @ ay[0])
        return self.sigma_ra * math.sqrt(1 + hx), self.sigma_dec * math.sqrt(1 + hy)

    def scale(self) -> float:
        """Plate scale at the tangent point, arcsec/px."""
        (x0, x1), (e0, e1) = self.standard([self.cx, self.cx + 1.0], [self.cy, self.cy])
        (y0, y1), (f0, f1) = self.standard([self.cx, self.cx], [self.cy, self.cy + 1.0])
        return float(math.sqrt(abs((x1 - x0) * (f1 - f0) - (e1 - e0) * (y1 - y0))))

    def describe(self) -> str:
        what = 'stack' if self.frame is None else f'frame {self.frame}'
        return (f'Gaia DR3 reduction of the {what}: {self.model}, {self.n_used} of {len(self.stars)} stars, '
                f'O-C rms {self.sigma_ra:.2f}" RA, {self.sigma_dec:.2f}" Dec, {self.scale():.4f}"/px')

    def to_dict(self) -> dict:
        d = asdict(self)
        d['model'] = self.model
        d['scale_arcsec'] = self.scale()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'Reduction':
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        d['stars'] = [RefStar(**s) for s in d.get('stars', [])]
        if d.get('photometry'):
            d['photometry'] = MagScale(**d['photometry'])
        return cls(**d)


def _design(u, v, degree: int, mirrored: bool):
    """Design matrices for xi and eta. Degree 0 is the 4-constant model with shared parameters
    (a, b, c, d): xi = a u + b v + c, eta = s (-b u + a v) + d, s = -1 for a mirror image."""
    if degree == 0:
        one, zero = np.ones_like(u), np.zeros_like(u)
        s = -1.0 if mirrored else 1.0
        return (np.column_stack([u, v, one, zero]), np.column_stack([s * v, -s * u, zero, one]))
    a = _terms(u, v, degree)
    return a, a


def _fit(u, v, xi, eta, degree, mirrored):
    """Least squares; returns coefficients, (A^T A)^-1 and residuals for both axes."""
    ax, ay = _design(u, v, degree, mirrored)
    if degree == 0:                                    # one parameter set for both axes
        a = np.vstack([ax, ay])
        p, *_ = np.linalg.lstsq(a, np.concatenate([xi, eta]), rcond=None)
        cov = np.linalg.pinv(a.T @ a)
        return p, p, cov, cov, xi - ax @ p, eta - ay @ p
    px, *_ = np.linalg.lstsq(ax, xi, rcond=None)
    py, *_ = np.linalg.lstsq(ay, eta, rcond=None)
    cov = np.linalg.pinv(ax.T @ ax)
    return px, py, cov, cov, xi - ax @ px, eta - ay @ py


def _sigma(res, n_par):
    n = len(res)
    return math.sqrt(float(np.sum(res ** 2)) / max(n - n_par, 1))


def match_stars(measured, cat, wcs, radius_px: float = 3.0, shift_px: float = 10.0):
    """Pairs [(measured index, catalogue index)]. The WCS predicts the catalogue stars' pixels;
    first the median shift of pairs within `shift_px` (e.g. drift between the solved frames and
    this image) is removed, then each measured star takes the nearest catalogue star within
    `radius_px` among those within 2 mag of the brightest there (a faint neighbour must not win
    over the star that was measured). One catalogue star per measured star and vice versa."""
    if not measured or not cat:
        return []
    mx = np.array([m[0] for m in measured])
    my = np.array([m[1] for m in measured])
    px, py = (np.asarray(v, float) for v in wcs.all_world2pix([c['ra'] for c in cat],
                                                              [c['dec'] for c in cat], 0))
    g = np.array([c['g'] if c['g'] is not None else 99.0 for c in cat])

    def pairs(dx, dy, r):
        out = []
        for i in range(len(mx)):
            d = np.hypot(px + dx - mx[i], py + dy - my[i])
            near = np.flatnonzero(d <= r)
            if near.size == 0:
                continue
            near = near[g[near] <= g[near].min() + 2.0]
            out.append((i, int(near[np.argmin(d[near])]), float(d[near].min())))
        best = {}
        for i, j, d in out:                            # a catalogue star goes to its nearest
            if j not in best or d < best[j][1]:
                best[j] = (i, d)
        return sorted((i, j) for j, (i, _) in best.items())

    first = pairs(0.0, 0.0, shift_px)
    if not first:
        return []
    dx = float(np.median([mx[i] - px[j] for i, j in first]))
    dy = float(np.median([my[i] - py[j] for i, j in first]))
    return pairs(dx, dy, radius_px)


def fit_reduction(measured, cat, pairs, ra0: float, dec0: float, shape, degree: int = 3,
                  mirrored: Optional[bool] = None, clip: float = 3.0,
                  clip_arcsec: Optional[float] = None, epoch: float = 2016.0) -> Reduction:
    """The polynomial from matched stars, outliers dropped one at a time (the largest O-C first)
    while above `clip` x sigma or `clip_arcsec`. The degree is lowered while there are fewer
    than 3 stars per parameter; with mirrored None both parities of the 4-constant model are
    tried and the better kept (higher degrees fit either parity)."""
    h, w = shape
    cx, cy, norm = (w - 1) / 2.0, (h - 1) / 2.0, max(w, h) / 2.0
    stars = [RefStar(measured[i][0], measured[i][1], cat[j]['ra'], cat[j]['dec'], cat[j]['g'],
                     int(cat[j]['source_id']), fwhm=measured[i][2] if len(measured[i]) > 2 else float('nan'))
             for i, j in pairs]
    if len(stars) < 3:
        raise ValueError(f'only {len(stars)} catalogue stars matched (3 needed)')
    x = np.array([s.x for s in stars])
    y = np.array([s.y for s in stars])
    u, v = (x - cx) / norm, (y - cy) / norm
    xi, eta = tangent([s.ra for s in stars], [s.dec for s in stars], ra0, dec0)
    used = np.ones(len(stars), bool)
    while degree > 0 and len(stars) < 3 * n_terms(degree):
        degree -= 1
    parities = [mirrored] if mirrored is not None else [False, True]
    if degree != 0:
        parities = [bool(mirrored)]
    while True:
        best = None
        for par in parities:
            r = _fit(u[used], v[used], xi[used], eta[used], degree, par)
            err = float(np.sum(r[4] ** 2 + r[5] ** 2))
            if best is None or err < best[0]:
                best = (err, par, r)
        _, par, (px, py, cvx, cvy, _, _) = best
        ax, ay = _design(u, v, degree, par)
        rx, ry = xi - ax @ px, eta - ay @ py
        sx, sy = _sigma(rx[used], n_terms(degree)), _sigma(ry[used], n_terms(degree))
        d = np.where(used, np.hypot(rx / max(sx, 1e-9), ry / max(sy, 1e-9)) / math.sqrt(2), -1.0)
        worst = int(np.argmax(d))
        too_far = d[worst] > clip or (clip_arcsec is not None
                                      and math.hypot(rx[worst], ry[worst]) > clip_arcsec)
        if not too_far or used.sum() <= n_terms(degree) + 2:
            break
        used[worst] = False
        while degree > 0 and used.sum() < 3 * n_terms(degree):
            degree -= 1
            parities = [False, True] if mirrored is None and degree == 0 else [bool(mirrored)]
    for s, ok, ox, oy in zip(stars, used, rx, ry):
        s.used, s.o_c_ra, s.o_c_dec = bool(ok), float(ox), float(oy)
    return Reduction(degree, bool(par), ra0, dec0, cx, cy, norm, px.tolist(), py.tolist(),
                     cvx.tolist(), cvy.tolist(), sx, sy, epoch, stars)


def reduce_field(image: np.ndarray, wcs, catalog, epoch: Optional[float] = None, degree: int = 3,
                 n_stars: int = 150, gmax: Optional[float] = None, clip: float = 3.0,
                 clip_arcsec: Optional[float] = None, match_px: float = 3.0, frame: Optional[int] = None,
                 log: Callable[[str], None] = print) -> Reduction:
    """Reduce a plate-solved image against the local catalogue (localcat.LocalGaia): the stack
    the WCS belongs to, or a frame (`frame`, its number) with the WCS moved by the drift
    (shifted_wcs). epoch: decimal year of the observation (proper motions)."""
    from .identify import brightest_stars
    from .solve import orientation
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape
    o = orientation(wcs, img.shape)
    stars = [s for s in brightest_stars(img, n_stars, min_sep=8) if not s.saturated]
    measured = [(s.x, s.y, s.fwhm) for s in stars]
    radius = 0.55 * math.hypot(w, h) * o.scale_arcsec
    cat = catalog.cone(o.center_ra, o.center_dec, radius, epoch)
    if gmax is not None:
        cat = [c for c in cat if c['g'] is not None and c['g'] <= gmax]
    # the brightest catalogue stars are enough to pair with the brightest measured ones
    cat = sorted(cat, key=lambda c: c['g'] if c['g'] is not None else 99.0)[:max(20 * len(measured), 500)]
    pairs = match_stars(measured, cat, wcs, match_px)
    log(f'{len(measured)} stars measured, {len(cat)} catalogue stars, {len(pairs)} matched')
    red = fit_reduction(measured, cat, pairs, o.center_ra, o.center_dec, img.shape, degree,
                        o.mirrored, clip, clip_arcsec,
                        epoch if epoch is not None else _now_year())
    red.frame = frame
    log(red.describe())
    return red


@dataclass
class ApertureCheck:
    name: str
    x: float
    y: float
    ra: float
    dec: float
    err_ra: float             # arcsec
    err_dec: float
    star: str = ''            # catalogue star there
    g: Optional[float] = None
    o_c_ra: Optional[float] = None    # measured - catalogue, arcsec (RA times cos Dec)
    o_c_dec: Optional[float] = None
    mag: Optional[float] = None       # measured, Gaia G scale (measure_mags)
    mag_err: Optional[float] = None   # incl. the zero point's error
    mag_sigma: Optional[float] = None  # the field's scatter of mag - G (colour, variables, blends)

    @property
    def offset(self) -> Optional[float]:
        return None if self.o_c_ra is None else math.hypot(self.o_c_ra, self.o_c_dec)

    @property
    def significance(self) -> Optional[float]:
        """Offset in units of its error (above about 3: not the catalogue star, or a blend)."""
        if self.o_c_ra is None:
            return None
        return math.hypot(self.o_c_ra / max(self.err_ra, 1e-6),
                          self.o_c_dec / max(self.err_dec, 1e-6)) / math.sqrt(2)

    @property
    def mag_significance(self) -> Optional[float]:
        """(mag - G) in units of the expected spread: this measurement's error and the field's
        scatter of mag - G (stars of other colours scatter that much in any case)."""
        if self.mag is None or self.g is None:
            return None
        return (self.mag - self.g) / max(math.hypot(self.mag_err, self.mag_sigma or 0.0), 1e-6)

    def mag_text(self) -> str:
        """'mag 10.13+-0.05 (G 9.89, +0.24 = 1.2 sigma of 0.20)'."""
        if self.mag is None:
            return 'mag - (no flux)' if self.mag_sigma is not None else ''
        s = f'mag {self.mag:.2f}+-{self.mag_err:.2f}'
        if self.g is not None:
            s += (f' (G {self.g:.2f}, {self.mag - self.g:+.2f} = {self.mag_significance:.1f} sigma'
                  f' of {self.mag_sigma or 0.0:.2f})')
        return s

    def describe(self) -> str:
        from .identify import radec_text
        s = f'{self.name}: {radec_text(self.ra, self.dec)} +-{self.err_ra:.2f}" {self.err_dec:.2f}"'
        if self.o_c_ra is not None:
            s += (f'  {self.star} G {self.g:.2f}: O-C {self.o_c_ra:+.2f}" {self.o_c_dec:+.2f}"'
                  f' ({self.significance:.1f} sigma)')
        else:
            s += '  no catalogue star'
        if self.mag is not None:
            s += '  ' + self.mag_text()
        return s


def check_positions(image: np.ndarray, red: Reduction, catalog, positions: dict,
                    search: float = 8.0, radius_arcsec: Optional[float] = None) -> dict:
    """{name: (x, y)} -> {name: ApertureCheck}: each position refined by a peak fit on the
    reduced image, its RA/Dec from the reduction, and the catalogue star there (localcat lookup)
    with the offset measured - catalogue."""
    from ..globaltrack import fit_peak
    img = np.asarray(image, dtype=np.float64)
    radius = radius_arcsec if radius_arcsec is not None else 3.0 * red.scale()
    out = {}
    for name, (x, y) in positions.items():
        f = fit_peak(img, x, y, search, half=10)
        fx, fy = (f.x, f.y) if f.ok else (float(x), float(y))
        ra, dec = red.world(fx, fy)
        era, edec = red.error(fx, fy)
        c = ApertureCheck(name, fx, fy, ra, dec, era, edec)
        hit = catalog.lookup(ra, dec, radius, red.epoch) if f.ok else None
        if hit is not None:
            c.star, c.g = hit['name'], hit['gmag']
            c.o_c_ra = ((ra - hit['ra'] + 180.0) % 360.0 - 180.0) * 3600.0 * math.cos(math.radians(dec))
            c.o_c_dec = (dec - hit['dec']) * 3600.0
        out[name] = c
    return out


def aperture_mag(image: np.ndarray, x: float, y: float, radius: float, inner: float, outer: float):
    """(instrumental mag, error) in a circle of `radius` px, background the median of the annulus
    inner..outer px (neighbours in it barely move a median). The error is the background noise's
    only (the camera gain is unknown); None if there is no positive flux."""
    from ..globaltrack import circle_stats
    h, w = image.shape
    r = int(math.ceil(outer)) + 1
    x0, x1 = max(0, int(round(x)) - r), min(w, int(round(x)) + r + 1)
    y0, y1 = max(0, int(round(y)) - r), min(h, int(round(y)) + r + 1)
    cut = image[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - x, yy - y)
    ring = cut[(d >= inner) & (d <= outer)]
    if ring.size < 10:
        return None, None
    bg = float(np.median(ring))
    noise = 1.4826 * float(np.median(np.abs(ring - bg)))
    total, area, _, _ = circle_stats(image, x, y, radius)
    flux = total - bg * area
    if flux <= 0:
        return None, None
    return -2.5 * math.log10(flux), 1.0857 * noise * math.sqrt(area) / flux


def _zero_point(stars, inst, clip: float, max_err: float):
    """(zero point, scatter, stars used) from the used stars' G - instrumental (errors below
    max_err), clip-sigma outliers (variables, blends) left out; None with fewer than 3."""
    zp_of = [s.g - inst[k][0] for k, s in enumerate(stars)
             if s.used and s.g is not None and inst[k][0] is not None and inst[k][1] < max_err]
    if len(zp_of) < 3:
        return None
    keep = np.array(zp_of)
    for _ in range(5):
        med = float(np.median(keep))
        sig = 1.4826 * float(np.median(np.abs(keep - med))) or float(np.std(keep)) or 1e-3
        new = keep[np.abs(keep - med) <= clip * sig]
        if len(new) == len(keep) or len(new) < 3:
            break
        keep = new
    return float(np.median(keep)), (float(np.std(keep, ddof=1)) if len(keep) > 1 else 0.0), len(keep)


def _annulus(scale: 'MagScale'):
    return scale.radius + scale.fwhm, scale.radius + scale.fwhm + max(3.0 * scale.fwhm, 6.0)


def measure_mags(image: np.ndarray, red: Reduction, checks: Optional[dict] = None,
                 radius_fwhm: float = 2.0, clip: float = 3.0, max_err: float = 0.1) -> Optional[MagScale]:
    """Measure every reference star and aperture in the image the reduction belongs to: a circle
    of radius_fwhm x the median FWHM (all stars the same, so the light outside it is the same
    fraction and goes into the zero point), the zero point from the median of G - instrumental of
    the used stars with errors below `max_err`, `clip`-sigma outliers (variables, blends) left out.
    Sets RefStar.mag/mag_err and ApertureCheck.mag/mag_err and red.photometry; returns it."""
    img = np.asarray(image, dtype=np.float64)
    fw = [s.fwhm for s in red.stars if s.used and np.isfinite(s.fwhm)]
    fwhm = float(np.median(fw)) if fw else 3.0
    scale = MagScale(0.0, 0.0, 0, max(radius_fwhm * fwhm, 2.0), fwhm)
    inner, outer = _annulus(scale)
    inst = [aperture_mag(img, s.x, s.y, scale.radius, inner, outer) for s in red.stars]
    zp = _zero_point(red.stars, inst, clip, max_err)
    if zp is None:
        return None
    scale.zero_point, scale.sigma, scale.n_used = zp
    for s, (m, e) in zip(red.stars, inst):
        s.mag, s.mag_err = (m + scale.zero_point, e) if m is not None else (None, None)
    for c in (checks or {}).values():
        m, e = aperture_mag(img, c.x, c.y, scale.radius, inner, outer)
        if m is not None:
            c.mag, c.mag_err, c.mag_sigma = m + scale.zero_point, math.hypot(e, scale.zp_err), scale.sigma
    red.photometry = scale
    return scale


@dataclass
class FrameMags:
    """Magnitudes of one frame, measured where the reduced frame's stars and apertures are now."""
    frame: int
    scale: MagScale
    mags: dict                # aperture name -> ApertureCheck-like values: mag, mag_err, g


def remeasure(image: np.ndarray, red: Reduction, checks: dict, shift=(0.0, 0.0), positions=None,
              frame: int = 0, clip: float = 3.0, max_err: float = 0.1) -> Optional[FrameMags]:
    """Magnitudes on another frame with the reduction's stars and aperture (same circle and
    annulus as measure_mags): reference stars at their reduced position + `shift` (frame minus
    reduced frame, px), apertures at `positions` ({name: (x, y)}, e.g. the tracked centres) or
    likewise shifted. No peak fit: a star fading in an occultation is measured where it is, and
    its magnitude follows it down. A new zero point per frame takes out clouds and extinction.
    Returns None without measure_mags first or with fewer than 3 usable stars."""
    if red.photometry is None:
        return None
    img = np.asarray(image, dtype=np.float64)
    sx, sy = shift
    sc = red.photometry
    inner, outer = _annulus(sc)
    inst = [aperture_mag(img, s.x + sx, s.y + sy, sc.radius, inner, outer) for s in red.stars]
    zp = _zero_point(red.stars, inst, clip, max_err)
    if zp is None:
        return None
    scale = MagScale(zp[0], zp[1], zp[2], sc.radius, sc.fwhm)
    out = {}
    for name, c in checks.items():
        x, y = positions[name] if positions and name in positions else (c.x + sx, c.y + sy)
        m, e = aperture_mag(img, x, y, sc.radius, inner, outer)
        out[name] = ApertureCheck(name, x, y, c.ra, c.dec, c.err_ra, c.err_dec, c.star, c.g,
                                  mag=None if m is None else m + scale.zero_point,
                                  mag_err=None if m is None else math.hypot(e, scale.zp_err),
                                  mag_sigma=scale.sigma)
    return FrameMags(frame, scale, out)


def shifted_wcs(wcs, dx: float, dy: float):
    """The WCS of a frame whose stars sit (dx, dy) px from where they are in the solved frames
    (pixel_solved = pixel_frame + (dx, dy))."""
    w = wcs.deepcopy()
    w.wcs.crpix = w.wcs.crpix - np.array([dx, dy])
    return w


def _now_year() -> float:
    now = datetime.now(timezone.utc)
    return now.year + (now.timetuple().tm_yday - 0.5) / 365.25


def decimal_year(date: Optional[str], time: Optional[str] = None) -> Optional[float]:
    """'YYYY-MM-DD' (+ 'HH:MM:SS...') -> decimal year; None if no date."""
    if not date:
        return None
    try:
        d = datetime.strptime(date[:10], '%Y-%m-%d')
    except ValueError:
        return None
    frac = 0.0
    if time:
        parts = time.split(':')
        try:
            frac = (int(parts[0]) + int(parts[1]) / 60 + float(parts[2]) / 3600) / 24
        except (IndexError, ValueError):
            frac = 0.0
    year_days = 366 if (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)) else 365
    return d.year + (d.timetuple().tm_yday - 1 + frac) / year_days


def reduction_path(movie_path) -> Path:
    from .solve import solution_path
    sol = solution_path(movie_path)
    return sol.with_name(sol.name.replace('.wcs.fit', '.astrometry.json')) if sol.name != 'wcs.fit' \
        else sol.with_name('astrometry.json')


def reduce_movie(movie_path: str, cfg: Optional[dict] = None, apertures: Optional[dict] = None,
                 log: Callable[[str], None] = print, save: bool = True, **kw):
    """Reduce the plate-solved stack of a movie (the frames <movie>.wcs.fit belongs to) against
    the local Gaia catalogue; optionally check apertures ({name: (x, y)}). Saved as
    <movie>.astrometry.json. Returns (Reduction, {name: ApertureCheck})."""
    from ..config import load
    from ..movie import open_movie
    from .localcat import local_catalog
    from .solve import load_wcs, solution_path, stack_frames

    cfg = cfg if cfg is not None else load()
    sol = solution_path(movie_path)
    if not sol.exists():
        raise FileNotFoundError(f'{sol}: no plate solution (solve the movie first)')
    catalog = local_catalog(cfg)
    if catalog is None:
        raise RuntimeError('no local Gaia catalogue (PySyzygos; [catalog] gaia_dir in the config)')
    meta = json.loads(sol.with_suffix('.json').read_text()) if sol.with_suffix('.json').exists() else {}
    start = int(meta.get('stack_start', 0))
    count = int(meta.get('stack_frames', cfg['astrometry']['stack_frames']))
    with open_movie(str(movie_path)) as mv:
        img = stack_frames(mv, start, count)
        f0 = mv.read(start)
    epoch = decimal_year(f0.date, f0.time)
    red = reduce_field(img, load_wcs(str(sol)), catalog, epoch, log=log, **kw)
    checks = check_positions(img, red, catalog, apertures) if apertures else {}
    if measure_mags(img, red, checks) is not None:
        p = red.photometry
        log(f'magnitudes: zero point {p.zero_point:.2f}, scatter {p.sigma:.3f} mag ({p.n_used} stars)')
    for c in checks.values():
        log(c.describe())
    if save:
        save_reduction(movie_path, red, checks)
    return red, checks


def save_reduction(movie_path, red: Reduction, checks: dict) -> Path:
    """<movie>.astrometry.json: the reduction (model, coefficients, reference stars with O-C) and
    the aperture checks."""
    path = reduction_path(movie_path)
    out = {'reduction': red.to_dict(),
           'apertures': {k: {**asdict(c), 'offset': c.offset, 'significance': c.significance}
                         for k, c in checks.items()}}
    path.write_text(json.dumps(out, indent=1))
    return path


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog='python -m pystarlight.wcs.reduce', description=__doc__.split('\n\n')[0])
    p.add_argument('movie')
    p.add_argument('--degree', type=int, default=3, choices=[0, 1, 2, 3])
    p.add_argument('--stars', type=int, default=150, help='brightest measured stars to use')
    p.add_argument('--no-save', action='store_true')
    a = p.parse_args(argv)
    aps = {}
    from .solve import solution_path
    apf = solution_path(a.movie)
    apf = apf.with_name(apf.name.replace('.wcs.fit', '.apertures.json'))
    if apf.exists():
        aps = {d['name']: (d['x0'] + d['size'] // 2, d['y0'] + d['size'] // 2)
               for d in json.loads(apf.read_text()) if d.get('role') in ('target', 'reference', 'check')}
    reduce_movie(a.movie, apertures=aps, degree=a.degree, n_stars=a.stars, save=not a.no_save)


if __name__ == '__main__':
    main()
