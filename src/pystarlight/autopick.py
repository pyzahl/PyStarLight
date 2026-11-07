"""Automatic aperture placement: reference stars picked from the field.

    candidates(image)                 every star found, with fitted centre, width, SNR and peak
    pick_references(stars, shape)    a few good references, spread over the field

A good reference is bright but not saturated, star-shaped (its FWHM near the field's median:
no double star, galaxy or hot-pixel clump), alone in its box (the local background) and far
enough from the edge that the drift over the recording keeps it inside. Among those the picker
takes the brightest, then again and again the star farthest from the ones already chosen, so
the references cover the field (better rotation fit, clouds and vignetting averaged).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.ndimage import maximum_filter, median_filter


@dataclass
class Candidate:
    x: float          # fitted centre (image coordinates)
    y: float
    flux: float       # 2 pi amp sigma^2
    fwhm: float
    snr: float        # fitted peak height / sky noise
    peak: float       # brightest raw pixel within 2 px of the centre
    note: str = ''    # why it was not picked: saturated, faint, not star-shaped, at the edge,
                      # neighbour in its box, the target, next to the target, spare ('' = picked)


def candidates(image: np.ndarray, n: int = 80, min_sep: int = 5, border: int = 4,
               nsigma: float = 5.0, stamp: Optional[tuple[int, int]] = None) -> list[Candidate]:
    """Up to n stars of the image, brightest (by fitted flux) first. Faint ones count too: they
    tell whether a brighter star has a neighbour in its box. stamp: (top, bottom) rows of a
    burnt-in timestamp, never searched (default: detected, wcs.solve.stamp_rows)."""
    from .globaltrack import fit_peak
    from .wcs.solve import stamp_rows
    img = np.asarray(image, dtype=np.float64)
    top, bottom = stamp if stamp is not None else stamp_rows(img)
    sm = median_filter(img, 3)
    bg = np.median(sm)
    noise = 1.4826 * np.median(np.abs(sm - bg)) or 1.0
    sky = 1.4826 * np.median(np.abs(img - np.median(img))) or 1.0   # per-pixel noise (SNR)
    peaks = (sm == maximum_filter(sm, 2 * min_sep + 1)) & (sm > bg + nsigma * noise)
    peaks[:top + border, :] = peaks[img.shape[0] - bottom - border:, :] = False
    peaks[:, :border] = peaks[:, -border:] = False
    ys, xs = np.nonzero(peaks)
    order = np.argsort(sm[ys, xs])[::-1][:max(4 * n, 200)]
    h, w = img.shape
    out: list[Candidate] = []
    for j in order:
        f = fit_peak(img, xs[j], ys[j], 3, half=7, min_snr=3.0)    # bright stars fit no Gaussian well
        if not f.ok:
            continue
        if any(math.hypot(c.x - f.x, c.y - f.y) < max(min_sep, 1.5 * c.fwhm) for c in out):
            continue                     # a second peak of a star already found (halo)
        ix, iy = int(round(f.x)), int(round(f.y))
        cut = img[max(0, iy - 2):min(h, iy + 3), max(0, ix - 2):min(w, ix + 3)]
        out.append(Candidate(f.x, f.y, 2 * math.pi * f.amp * f.sigma ** 2, f.fwhm, f.amp / sky,
                             float(cut.max()) if cut.size else float('nan')))
        if len(out) >= n:
            break
    out.sort(key=lambda c: c.flux, reverse=True)
    return out


def pick_references(stars: list[Candidate], shape: tuple, n: int = 5, box: int = 51,
                    full_scale: Optional[float] = None, max_peak: float = 0.8, min_snr: float = 20.0,
                    drift: tuple[float, float] = (0.0, 0.0), avoid: tuple = (),
                    stamp: tuple[int, int] = (0, 0), pool: int = 3) -> list[Candidate]:
    """Up to n references from `stars` (candidates(), brightest first).

    full_scale, max_peak: a star whose brightest pixel reaches max_peak x full scale is left out
        (saturated or close to it: no longer linear). Without a full scale this test is skipped.
    min_snr: fitted peak at least this x the sky noise.
    drift: (dx, dy) the field moves from the image to the end of the recording; the box has to
        stay inside the frame (and off the timestamp rows) at both ends.
    avoid: (x, y) positions (the target): no reference whose box reaches within a box of them.
    pool: farthest-point choice among the pool x n brightest good stars (brighter = less noise;
        spreading wins only among stars that are good enough).
    Every star left out gets its reason in .note."""
    h, w = shape[:2]
    half = box / 2
    top, bottom = stamp
    fw = [c.fwhm for c in stars if c.snr >= min_snr] or [c.fwhm for c in stars]
    med = float(np.median(fw)) if fw else 0.0

    def inside(x, y):
        return half <= x <= w - 1 - half and top + half <= y <= h - 1 - bottom - half

    good = []
    for c in stars:
        c.note = ''
        if full_scale and c.peak >= max_peak * full_scale:
            c.note = 'saturated'
        elif c.snr < min_snr:
            c.note = 'faint'
        elif med and not 0.6 * med <= c.fwhm <= 1.6 * med:
            c.note = 'not star-shaped'
        elif not (inside(c.x, c.y) and inside(c.x - drift[0], c.y - drift[1])):
            c.note = 'at the edge'                   # now or at the end of the drift
        elif any(o is not c and abs(o.x - c.x) < half + 1.5 * med and abs(o.y - c.y) < half + 1.5 * med
                 for o in stars):
            c.note = 'neighbour in its box'
        elif any(math.hypot(ax - c.x, ay - c.y) < max(3.0, med) for ax, ay in avoid):
            c.note = 'the target'
        elif any(abs(ax - c.x) < box and abs(ay - c.y) < box for ax, ay in avoid):
            c.note = 'next to the target'
        else:
            good.append(c)
    chosen = []
    pool_stars = good[:max(n, pool * n)]
    while pool_stars and len(chosen) < n:
        if not chosen:
            best = pool_stars[0]
        else:
            others = [*((c.x, c.y) for c in chosen), *avoid]
            best = max(pool_stars, key=lambda c: min(math.hypot(c.x - x, c.y - y) for x, y in others))
        chosen.append(best)
        pool_stars.remove(best)
    for c in good:
        if c not in chosen:
            c.note = 'spare'
    return chosen
