"""Global tracking with peak fits, and sub-pixel aperture photometry.

NEW CODE (not from PyMovie). Aperture roles:

    target     the star being occulted (one)
    reference  comparison stars (any number); used for normalisation
    check      measured and tracked like a reference, but not used for normalisation
    sky        star-free regions; their robust mean is the global background

Each frame:
 1. every star aperture (target and references) gets a 2-D Gaussian peak fit near
    where the previous frame's transform predicts it;
 2. one rigid transform (shift, optionally rotation) is fitted to all good peaks,
    weighted by their positional precision, with outlier rejection;
 3. every aperture, including sky and the target, is placed by that transform. A
    star that fades (the target during the event) or is hit by a cosmic ray does not
    move its aperture: the other stars carry it;
 4. photometry: sum inside a circle of the given radius with exact sub-pixel
    weights, minus the local (or sky) background per pixel.

    lc = analyze_global(movie, apertures)        # apertures with .role set
    flux = lc.normalized()                       # target / smoothed sum of references
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
from scipy.ndimage import median_filter
from scipy.optimize import least_squares
from scipy.signal import savgol_filter

from .movie import MovieReader
from .photometry import robust_mean_std
from .pipeline import LightCurves
from .tracking import Aperture

STAR_ROLES = ('target', 'reference', 'check')


# --------------------------------------------------------------------------- peak fit

@dataclass
class PeakFit:
    x: float          # image coordinates (column)
    y: float          # (row)
    sigma: float      # Gaussian sigma, px (FWHM = 2.3548 sigma)
    amp: float        # peak above background
    bkg: float
    snr: float        # peak / residual rms
    ok: bool

    @property
    def fwhm(self) -> float:
        return 2.3548 * self.sigma

    @property
    def weight(self) -> float:
        """1 / position variance up to a constant. All stars share the PSF, so snr^2
        (capped, so one bright star cannot dominate)."""
        return min(self.snr, 100.0) ** 2 if self.ok else 0.0


def _gauss(p, xx, yy):
    x0, y0, s, a, b = p
    return b + a * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * s * s))


def fit_peak(image: np.ndarray, x: float, y: float, search: float = 15, half: int = 7,
             min_snr: float = 5.0, calibration=None,
             sigma_range: Optional[tuple[float, float]] = None) -> PeakFit:
    """Find the brightest star within `search` px of (x, y) and fit a circular Gaussian
    plus constant background on a (2*half+1)^2 cut-out around it.

    sigma_range: accept only fits whose width is in this range (px). With the known PSF
    width this rejects noise spikes and cosmic rays, which fit as very narrow peaks."""
    h, w = image.shape
    xs0, xs1 = max(0, int(x - search)), min(w, int(x + search) + 1)
    ys0, ys1 = max(0, int(y - search)), min(h, int(y + search) + 1)
    win = image[ys0:ys1, xs0:xs1].astype(np.float64)
    if calibration is not None:
        win = calibration.apply_roi(win, xs0, ys0)
    if win.size == 0:
        return PeakFit(x, y, 0, 0, 0, 0, False)
    sm = median_filter(win, 3)                         # hot pixels and cosmic rays out
    py, px = np.unravel_index(np.argmax(sm), sm.shape)
    px += xs0
    py += ys0

    cx0, cx1 = max(0, px - half), min(w, px + half + 1)
    cy0, cy1 = max(0, py - half), min(h, py + half + 1)
    cut = image[cy0:cy1, cx0:cx1].astype(np.float64)
    if calibration is not None:
        cut = calibration.apply_roi(cut, cx0, cy0)
    yy, xx = np.mgrid[cy0:cy1, cx0:cx1]
    b0 = float(np.median(cut))
    a0 = float(cut.max() - b0)
    if a0 <= 0:
        return PeakFit(float(px), float(py), 0, 0, b0, 0, False)
    p0 = [px, py, 1.5, a0, b0]
    lo = [cx0 - 0.5, cy0 - 0.5, 0.3, 0, -np.inf]
    hi = [cx1 - 0.5, cy1 - 0.5, float(half), np.inf, np.inf]
    try:
        r = least_squares(lambda p: (_gauss(p, xx, yy) - cut).ravel(), p0, bounds=(lo, hi),
                          x_scale=[1, 1, 1, a0, max(abs(b0), 1)])
    except ValueError:
        return PeakFit(float(px), float(py), 0, 0, b0, 0, False)
    x0, y0, s, a, b = r.x
    noise = float(np.std(r.fun)) or 1e-9
    snr = a / noise
    smin, smax = sigma_range or (0.3, half * 0.9)
    ok = bool(r.success and snr >= min_snr and smin < s < min(smax, half * 0.9))
    return PeakFit(float(x0), float(y0), float(s), float(a), float(b), float(snr), ok)


def _gauss_xy(p, xx, yy):
    x0, y0, sx, sy, a, b = p
    return b + a * np.exp(-(((xx - x0) / sx) ** 2 + ((yy - y0) / sy) ** 2) / 2)


def savgol_2d(img: np.ndarray, window: int = 5, order: int = 2) -> np.ndarray:
    """Separable 2-D Savitzky-Golay smoothing: rows, then columns. The window is cut to
    the image size (odd) and the order to below the window."""
    w = min(window, *img.shape)
    w -= 1 - w % 2
    if w < 3:
        return img
    o = min(order, w - 1)
    out = savgol_filter(img, w, o, axis=1)
    return savgol_filter(out, w, o, axis=0)


def fit_peak_savgol(image: np.ndarray, x: float, y: float, search: float = 15, half: int = 7,
                    min_snr: float = 5.0, calibration=None,
                    sigma_range: Optional[tuple[float, float]] = None,
                    window: int = 5, order: int = 2) -> PeakFit:
    """Alternative to fit_peak: the cut-out is smoothed with a 2-D Savitzky-Golay filter,
    then fitted with an axis-aligned Gaussian (sigma_x, sigma_y) plus constant. The centre
    is the fitted model's. Same star search as fit_peak; PeakFit.sigma is sqrt(sx * sy).

    The SNR uses the residuals against the unsmoothed cut-out, so it stays comparable with
    fit_peak (residuals of the smoothed data would understate the noise)."""
    h, w = image.shape
    xs0, xs1 = max(0, int(x - search)), min(w, int(x + search) + 1)
    ys0, ys1 = max(0, int(y - search)), min(h, int(y + search) + 1)
    win = image[ys0:ys1, xs0:xs1].astype(np.float64)
    if calibration is not None:
        win = calibration.apply_roi(win, xs0, ys0)
    if win.size == 0:
        return PeakFit(x, y, 0, 0, 0, 0, False)
    sm = median_filter(win, 3)                         # hot pixels and cosmic rays out
    py, px = np.unravel_index(np.argmax(sm), sm.shape)
    px += xs0
    py += ys0

    cx0, cx1 = max(0, px - half), min(w, px + half + 1)
    cy0, cy1 = max(0, py - half), min(h, py + half + 1)
    cut = image[cy0:cy1, cx0:cx1].astype(np.float64)
    if calibration is not None:
        cut = calibration.apply_roi(cut, cx0, cy0)
    smooth = savgol_2d(cut, window, order)
    yy, xx = np.mgrid[cy0:cy1, cx0:cx1]
    b0 = float(np.median(smooth))
    a0 = float(smooth.max() - b0)
    if a0 <= 0:
        return PeakFit(float(px), float(py), 0, 0, b0, 0, False)
    p0 = [px, py, 1.5, 1.5, a0, b0]
    lo = [cx0 - 0.5, cy0 - 0.5, 0.3, 0.3, 0, -np.inf]
    hi = [cx1 - 0.5, cy1 - 0.5, float(half), float(half), np.inf, np.inf]
    try:
        r = least_squares(lambda p: (_gauss_xy(p, xx, yy) - smooth).ravel(), p0, bounds=(lo, hi),
                          x_scale=[1, 1, 1, 1, a0, max(abs(b0), 1)])
    except ValueError:
        return PeakFit(float(px), float(py), 0, 0, b0, 0, False)
    x0, y0, sx, sy, a, b = r.x
    s = math.sqrt(sx * sy)
    noise = float(np.std(_gauss_xy(r.x, xx, yy) - cut)) or 1e-9
    snr = a / noise
    smin, smax = sigma_range or (0.3, half * 0.9)
    ok = bool(r.success and snr >= min_snr and smin < s < min(smax, half * 0.9))
    return PeakFit(float(x0), float(y0), float(s), float(a), float(b), float(snr), ok)


def peak_fitter(savgol: bool = False, window: int = 5, order: int = 2) -> Callable[..., PeakFit]:
    """fit_peak, or fit_peak_savgol with the given filter, as one call signature."""
    if not savgol:
        return fit_peak
    return lambda *args, **kw: fit_peak_savgol(*args, window=window, order=order, **kw)


# --------------------------------------------------------------------------- transform

@dataclass
class Rigid:
    """p = R(theta) @ q + (dx, dy) about the image origin."""
    dx: float = 0.0
    dy: float = 0.0
    theta: float = 0.0   # radians

    def apply(self, x, y):
        c, s = math.cos(self.theta), math.sin(self.theta)
        return c * x - s * y + self.dx, s * x + c * y + self.dy


def fit_rigid(ref: np.ndarray, cur: np.ndarray, w: np.ndarray, rotation: bool = True) -> Rigid:
    """Weighted least-squares rigid transform taking ref points (n,2) to cur points."""
    w = w / w.sum()
    rc = (w[:, None] * ref).sum(0)
    cc = (w[:, None] * cur).sum(0)
    theta = 0.0
    if rotation and len(ref) >= 2:
        a, b = ref - rc, cur - cc
        num = (w * (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])).sum()
        den = (w * (a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])).sum()
        theta = math.atan2(num, den)
    c, s = math.cos(theta), math.sin(theta)
    return Rigid(cc[0] - (c * rc[0] - s * rc[1]), cc[1] - (s * rc[0] + c * rc[1]), theta)


def robust_rigid(ref, cur, w, rotation=True, clip=4.0, min_tol=1.0, prior: Optional[Rigid] = None):
    """fit_rigid with outlier rejection. Starts from the prior transform shifted by the
    median star displacement (a single wrong star cannot pull this), drops stars far
    from it, then refines with weighted fits. Returns (transform, used, rms)."""
    use = w > 0
    if not use.any():
        return None, use, float('nan')
    prior = prior or Rigid()
    px, py = prior.apply(ref[:, 0], ref[:, 1])
    d = cur - np.c_[px, py]
    mdx, mdy = np.median(d[use, 0]), np.median(d[use, 1])
    res = np.hypot(d[:, 0] - mdx, d[:, 1] - mdy)
    if use.sum() >= 3:
        keep = use & (res <= max(clip * 1.4826 * np.median(res[use]), 2 * min_tol))
        if keep.any():
            use = keep
    for _ in range(4):
        t = fit_rigid(ref[use], cur[use], w[use], rotation and use.sum() >= 2)
        px, py = t.apply(ref[:, 0], ref[:, 1])
        res = np.hypot(px - cur[:, 0], py - cur[:, 1])
        mad = 1.4826 * np.median(res[use]) if use.sum() >= 3 else 0.0
        keep = use & (res <= max(clip * mad, min_tol))
        if keep.sum() == 0 or (keep == use).all():
            break
        use = keep
    t = fit_rigid(ref[use], cur[use], w[use], rotation and use.sum() >= 2)
    px, py = t.apply(ref[:, 0], ref[:, 1])
    rms = float(np.sqrt(np.mean(np.hypot(px - cur[:, 0], py - cur[:, 1])[use] ** 2)))
    return t, use, rms


# --------------------------------------------------------------------------- photometry

def aperture_weights(cx: float, cy: float, radius: float, x0: int, y0: int, shape, ss: int = 5):
    """Fraction of each pixel of a cut-out (top-left x0, y0) inside a circle, by
    ss x ss sub-sampling. Pixel (r, c) covers [c-0.5, c+0.5] x [r-0.5, r+0.5]."""
    h, w = shape
    sub = (np.arange(ss) + 0.5) / ss - 0.5
    yy = (np.arange(h) + y0)[:, None, None, None] + sub[None, None, :, None]
    xx = (np.arange(w) + x0)[None, :, None, None] + sub[None, None, None, :]
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius * radius
    return inside.mean(axis=(2, 3))


def circle_sum(image, cx, cy, radius, calibration=None):
    """(sum of pixel values inside the circle, effective pixel count)."""
    s, area, _, _ = circle_stats(image, cx, cy, radius, calibration)
    return s, area


def circle_stats(image, cx, cy, radius, calibration=None):
    """(sum inside the circle, effective pixel count, brightest pixel touched by the circle,
    number of defect pixels touched (from the calibration's defect map))."""
    hh, ww = image.shape
    r = int(math.ceil(radius)) + 1
    x0, x1 = max(0, int(round(cx)) - r), min(ww, int(round(cx)) + r + 1)
    y0, y1 = max(0, int(round(cy)) - r), min(hh, int(round(cy)) + r + 1)
    cut = image[y0:y1, x0:x1].astype(np.float64)
    if calibration is not None:
        cut = calibration.apply_roi(cut, x0, y0)
    wts = aperture_weights(cx, cy, radius, x0, y0, cut.shape)
    inside = wts > 0
    maxpx = float(cut[inside].max()) if inside.any() else float('nan')
    defects = 0
    if calibration is not None and calibration.defects is not None:
        defects = int(calibration.defects[y0:y1, x0:x1][inside].sum())
    return float((wts * cut).sum()), float(wts.sum()), maxpx, defects


def box_background(image, cx, cy, size, calibration=None, lunar=False):
    """PyMovie's robust (star-masked) background of a size x size box centred at (cx, cy)."""
    hh, ww = image.shape
    x0 = int(np.clip(round(cx) - size // 2, 0, ww - size))
    y0 = int(np.clip(round(cy) - size // 2, 0, hh - size))
    box = image[y0:y0 + size, x0:x0 + size]
    if calibration is not None:
        box = calibration.apply_roi(box, x0, y0)
    st = robust_mean_std(box, lunar=lunar)
    return st.mean, st.std


# --------------------------------------------------------------------------- tracker

class GlobalTracker:
    """dim_fraction: a star whose peak SNR falls below this fraction of its own recent
    level, *relative to how the other stars changed*, is left out of the tracking fit.
    Clouds dim all stars alike and do not trigger it; an occultation dims only the target."""

    def __init__(self, apertures: list[Aperture], search: float = 15, rotation: bool = True,
                 min_snr: float = 5.0, calibration=None, dim_fraction: float = 0.4,
                 fitter: Optional[Callable[..., PeakFit]] = None):
        self.apertures = apertures
        self.fitter = fitter or fit_peak
        self.search = search
        self.rotation = rotation
        self.min_snr = min_snr
        self.calibration = calibration
        self.dim_fraction = dim_fraction
        roles = {a.role for a in apertures}
        if not roles & set(STAR_ROLES):
            raise ValueError("give apertures roles: 'target', 'reference' (and optionally 'sky')")
        self.stars = [a for a in apertures if a.role in STAR_ROLES]
        self.ref = {}
        self.transform = Rigid()
        self.sigma_range: Optional[tuple[float, float]] = None
        self.snr_hist = {a.name: deque(maxlen=50) for a in self.stars}

    def initialize(self, image):
        """Reference positions: peak fits on the first frame (box centre if a fit fails).
        The median star width sets the accepted width range for later fits."""
        sig = []
        for a in self.apertures:
            cx, cy = a.x0 + a.size / 2 - 0.5, a.y0 + a.size / 2 - 0.5
            if a.role in STAR_ROLES:
                f = self.fitter(image, cx, cy, a.size / 2, min_snr=self.min_snr, calibration=self.calibration)
                if f.ok:
                    cx, cy = f.x, f.y
                    sig.append(f.sigma)
            self.ref[a.name] = (cx, cy)
        if sig:
            s = float(np.median(sig))
            self.sigma_range = (0.5 * s, 2.0 * s)
        self.transform = Rigid()

    def step(self, image, exclude=()):
        """exclude: star names not to use for tracking in this frame (e.g. the target
        during a known event). Returns ({name: (x, y)} centres, {name: PeakFit}, info)."""
        prev = self.transform
        fits = {}
        for search in (self.search, 2 * self.search):
            for a in self.stars:
                gx, gy = prev.apply(*self.ref[a.name])
                fits[a.name] = self.fitter(image, gx, gy, search, min_snr=self.min_snr,
                                        calibration=self.calibration, sigma_range=self.sigma_range)
            if sum(f.ok for f in fits.values()) >= max(1, len(self.stars) // 2):
                break                                    # most stars found; else widen search once
        names = [a.name for a in self.stars]
        # differential dimming: own SNR vs own recent median, compared with the other stars
        ratio = {n: fits[n].snr / np.median(self.snr_hist[n])
                 for n in names if fits[n].ok and len(self.snr_hist[n]) >= 5}
        typical = np.median(list(ratio.values())) if len(ratio) >= 2 else None
        for n in names:
            f = fits[n]
            f.dimmed = bool(typical and n in ratio and ratio[n] < self.dim_fraction * typical)
            f.excluded = n in exclude
            if f.ok and not f.dimmed and not f.excluded:
                self.snr_hist[n].append(f.snr)
        ref = np.array([self.ref[n] for n in names])
        cur = np.array([(fits[n].x, fits[n].y) for n in names])
        w = np.array([0.0 if (fits[n].dimmed or fits[n].excluded) else fits[n].weight for n in names])
        t, used, rms = robust_rigid(ref, cur, w, self.rotation, prior=prev)
        lost = t is None
        if not lost:
            self.transform = t
        centres = {a.name: self.transform.apply(*self.ref[a.name]) for a in self.apertures}
        info = {'dx': self.transform.dx, 'dy': self.transform.dy,
                'rot_deg': math.degrees(self.transform.theta),
                'n_stars': int(used.sum()) if not lost else 0,
                'track_rms': rms, 'lost': float(lost)}
        for n, u in zip(names, used):
            fits[n].used = bool(u)
        return centres, fits, info


# --------------------------------------------------------------------------- analysis

STAR_COLUMNS = ('frame', 'signal', 'sum', 'area', 'bkavg', 'bkstd', 'noise', 'xc', 'yc',
                'x_fit', 'y_fit', 'fwhm', 'peak_snr', 'used', 'dimmed', 'maxpx', 'hit_defect')
SKY_COLUMNS = ('frame', 'bkavg', 'bkstd', 'xc', 'yc')


def auto_radius(fwhms, factor: float = 1.5) -> float:
    """Photometry radius from the measured star widths: factor x median FWHM (at least
    1.5 px; 4 px without any fit). The FWHM is set by the seeing, so this adapts to it.
    1.5 keeps nearly all the light; about 0.85 maximises the SNR of a faint star when the
    sky noise dominates."""
    fw = [f for f in fwhms if np.isfinite(f)]
    return max(1.5, factor * float(np.median(fw))) if fw else 4.0


def analyze_global(reader: MovieReader, apertures: list[Aperture],
                   start: int = 0, stop: Optional[int] = None, *,
                   radius: Optional[float] = None, radius_fwhm: float = 1.5,
                   background: str = 'local',
                   search: float = 15, rotation: bool = True, min_snr: float = 5.0,
                   calibration=None, preprocess: Optional[Callable] = None,
                   lunar: bool = False, event: Optional[tuple[int, int]] = None,
                   target_drift: bool = True, dim_fraction: float = 0.4,
                   savgol_fit: bool = False, savgol_window: int = 5, savgol_order: int = 2,
                   progress: Optional[Callable[[int, dict], None]] = None,
                   cancel: Optional[threading.Event] = None) -> LightCurves:
    """Track all star apertures together and measure with sub-pixel circular apertures.

    radius: photometry radius in px; default radius_fwhm x median FWHM on the first frame.
    background: 'local' (robust mean of each aperture's box, as PyMovie), or 'sky'
        (robust mean over the sky apertures; needs at least one).
    search: px around the predicted position to look for each star (doubled once
        automatically when most stars are not found, e.g. after a jolt).
    event: (first, last) frame of a known occultation; the target is not used for
        tracking there. Without it the target is still dropped automatically whenever
        its fit fails or it dims relative to the other stars (dim_fraction).
    target_drift: the target aperture follows the global solution plus the target's own
        small offset from it (refraction, field distortion). That offset is measured on
        frames where the target is well seen, smoothed, and linearly interpolated across
        the event. This needs a second pass over the frames (about doubles the time).
    savgol_fit: alternative peak fit (fit_peak_savgol): Savitzky-Golay smoothing
        (savgol_window, savgol_order) and a 2-D Gaussian fit, used for tracking; each
        star's circle is centred on its own fitted centre wherever that fit is good (ok,
        not faded or excluded, within `radius` of the global solution). Elsewhere, e.g.
        the target during the event, the global solution places it as before (with
        target_drift: plus the target's interpolated own offset).
    Columns per star: signal (sum - area*bkavg), sum, area, bkavg, bkstd,
        noise (bkstd*sqrt(area), background-only), xc/yc (aperture centre used),
        x_fit/y_fit/fwhm/peak_snr (this star's own fit), used (1 if it steered tracking),
        dimmed (1 if left out for fading relative to the others).
    Per row (lc.frame_info): dx, dy, rot_deg, n_stars, track_rms, lost.
    """
    if background not in ('local', 'sky'):
        raise ValueError("background must be 'local' or 'sky'")
    sky = [a for a in apertures if a.role == 'sky']
    if background == 'sky' and not sky:
        raise ValueError("background='sky' needs at least one aperture with role 'sky'")
    if stop is None:
        stop = len(reader) - 1
    step = 1 if stop >= start else -1

    first = reader.read(start).image
    if preprocess:
        first = preprocess(first)
    fitter = peak_fitter(savgol_fit, savgol_window, savgol_order)
    tr = GlobalTracker(apertures, search, rotation, min_snr, calibration, dim_fraction, fitter)
    target = next((a for a in apertures if a.role == 'target'), None)
    tr.initialize(first)
    if radius is None:
        fw = [f.fwhm for f in (fitter(first, *tr.ref[a.name], 3, calibration=calibration)
                               for a in tr.stars) if f.ok]
        radius = auto_radius(fw, radius_fwhm)

    rows = []
    glob = {a.name: [] for a in apertures if a.role != 'sky'}   # global-solution centres
    for fr in reader.frames(start, stop, step):
        if cancel is not None and cancel.is_set():
            break
        img = preprocess(fr.image) if preprocess else fr.image
        in_event = event is not None and target is not None and event[0] <= fr.index <= event[1]
        centres, fits, info = tr.step(img, exclude=(target.name,) if in_event else ())

        sky_vals = {}
        for a in sky:
            cx, cy = centres[a.name]
            sky_vals[a.name] = (*box_background(img, cx, cy, a.size, calibration, lunar), cx, cy)
        sky_level = np.median([v[0] for v in sky_vals.values()]) if sky else None
        sky_std = np.median([v[1] for v in sky_vals.values()]) if sky else None

        vals = {}
        for a in apertures:
            cx, cy = centres[a.name]
            if a.role == 'sky':
                vals[a.name] = (fr.index, *sky_vals[a.name])
                continue
            glob[a.name].append((cx, cy))
            f = fits.get(a.name)
            if (savgol_fit and f is not None and f.ok and not getattr(f, 'dimmed', False)
                    and not getattr(f, 'excluded', False) and math.hypot(f.x - cx, f.y - cy) <= radius):
                cx, cy = f.x, f.y                        # centre from the fitted model
            s, area, maxpx, hits = circle_stats(img, cx, cy, radius, calibration)
            if background == 'sky':
                bk, bs = sky_level, sky_std
            else:
                bk, bs = box_background(img, cx, cy, a.size, calibration, lunar)
            fx, fy, fw, snr, used = ((f.x, f.y, f.fwhm, f.snr, float(getattr(f, 'used', False)))
                                     if f is not None and f.ok else (np.nan,) * 4 + (0.0,))
            dimmed = float(getattr(f, 'dimmed', False) or getattr(f, 'excluded', False)) if f else 0.0
            vals[a.name] = (fr.index, s - area * bk, s, area, bk, bs, bs * math.sqrt(area),
                            cx, cy, fx, fy, fw, snr, used, dimmed, maxpx, hits)
        rows.append((fr.index, fr.date, fr.time, info, vals))
        if progress:
            progress(fr.index, vals)

    order = np.argsort([r[0] for r in rows], kind='stable')
    rows = [rows[i] for i in order]
    glob = {n: np.array(v, dtype=float).reshape(-1, 2)[order] for n, v in glob.items()}
    cols = {}
    for a in apertures:
        names = SKY_COLUMNS if a.role == 'sky' else STAR_COLUMNS
        data = np.array([r[4][a.name] for r in rows], dtype=float).reshape(-1, len(names))
        cols[a.name] = {c: data[:, i] for i, c in enumerate(names)}
    if target_drift and target is not None and rows:
        tc = cols[target.name]
        if savgol_fit:                       # offsets from the global solution, not the fit centres
            gx, gy = glob[target.name].T
            fitted = (tc['xc'] != gx) | (tc['yc'] != gy)
            _apply_target_drift(reader, tc, target, radius, background, cols, sky, calibration,
                                preprocess, lunar, cancel, global_xy=(gx, gy), keep=fitted)
        else:
            _apply_target_drift(reader, tc, target, radius, background, cols,
                                sky, calibration, preprocess, lunar, cancel)
    info_keys = list(rows[0][3]) if rows else []
    frame_info = {k: np.array([r[3][k] for r in rows], dtype=float) for k in info_keys}
    frame_info['radius'] = np.full(len(rows), radius)
    return LightCurves(cols, [r[1] for r in rows], [r[2] for r in rows], [None] * len(rows),
                       roles={a.name: a.role for a in apertures}, frame_info=frame_info,
                       source=str(getattr(reader, 'path', '')), metadata=dict(reader.metadata),
                       mode='global', apertures=list(apertures))


def _running_median(y, window):
    h = window // 2
    return np.array([np.median(y[max(0, i - h):i + h + 1]) for i in range(len(y))])


def _apply_target_drift(reader, tc, target, radius, background, cols, sky, calibration,
                        preprocess, lunar, cancel, window: int = 25, min_shift: float = 0.02,
                        global_xy=None, keep=None):
    """Move the target aperture by its own (smoothed, gap-interpolated) offset from the
    global solution and re-measure it. Updates the target's columns in place.

    global_xy: the global-solution centres when xc/yc are not (savgol_fit centres them on
    the fit); keep: rows left as measured (already on the fitted centre)."""
    gx, gy = global_xy if global_xy is not None else (tc['xc'].copy(), tc['yc'].copy())
    # well seen = fitted and not faded; it need not have steered the tracking (a drifting
    # target is rejected from the global fit as an outlier, which is fine)
    good = (tc['dimmed'] == 0) & np.isfinite(tc['x_fit'])
    if good.sum() < 5:
        return
    f = tc['frame']
    rx = _running_median((tc['x_fit'] - gx)[good], window)
    ry = _running_median((tc['y_fit'] - gy)[good], window)
    ox = np.interp(f, f[good], rx)          # linear across gaps, constant beyond the ends
    oy = np.interp(f, f[good], ry)
    if max(np.abs(ox).max(), np.abs(oy).max()) < min_shift:
        return
    row_of = {int(fr): i for i, fr in enumerate(f)}
    for fr in reader.frames(int(f.min()), int(f.max())):
        if cancel is not None and cancel.is_set():
            break
        i = row_of.get(fr.index)
        if i is None or (keep is not None and keep[i]):
            continue
        img = preprocess(fr.image) if preprocess else fr.image
        cx, cy = gx[i] + ox[i], gy[i] + oy[i]
        s, area, maxpx, hits = circle_stats(img, cx, cy, radius, calibration)
        tc['maxpx'][i], tc['hit_defect'][i] = maxpx, hits
        if background == 'sky':
            bk = float(np.median([cols[a.name]['bkavg'][i] for a in sky]))
            bs = float(np.median([cols[a.name]['bkstd'][i] for a in sky]))
        else:
            bk, bs = box_background(img, cx, cy, target.size, calibration, lunar)
        tc['signal'][i], tc['sum'][i], tc['area'][i] = s - area * bk, s, area
        tc['bkavg'][i], tc['bkstd'][i], tc['noise'][i] = bk, bs, bs * math.sqrt(area)
        tc['xc'][i], tc['yc'][i] = cx, cy
