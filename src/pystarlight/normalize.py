"""Light-curve normalisation with reference stars.

NEW CODE: PyMovie itself only extracts raw signals and leaves normalisation to PyOTE.
This follows the usual PyOTE practice: divide the target by a smoothed reference,
then scale so the reference's average level is kept.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
from scipy.signal import savgol_filter


def combine_references(refs: Sequence[np.ndarray], weights: Optional[Sequence[float]] = None) -> np.ndarray:
    """Weighted sum of several reference-star signals (equal weights by default)."""
    refs = np.asarray(refs, dtype=float)
    w = np.ones(len(refs)) if weights is None else np.asarray(weights, dtype=float)
    return np.tensordot(w, refs, axes=1)


def smooth(y: np.ndarray, window: int, method: str = 'savgol', polyorder: int = 2) -> np.ndarray:
    """Smooth a reference curve. method: 'savgol' | 'mean' (running mean) | 'median'."""
    y = np.asarray(y, dtype=float)
    if window <= 1:
        return y
    window = min(window | 1, len(y) - (1 - len(y) % 2))   # odd and <= len(y)
    if method == 'savgol':
        return savgol_filter(y, window, min(polyorder, window - 1), mode='interp')
    pad = window // 2
    yp = np.pad(y, pad, mode='edge')
    if method == 'mean':
        return np.convolve(yp, np.ones(window) / window, mode='valid')
    if method == 'median':
        from numpy.lib.stride_tricks import sliding_window_view
        return np.median(sliding_window_view(yp, window), axis=1)
    raise ValueError(method)


def normalize(target: np.ndarray, reference: np.ndarray, window: int = 101,
              method: str = 'savgol', min_ref_fraction: float = 0.05) -> np.ndarray:
    """target * mean(ref_s) / ref_s, ref_s = smoothed reference.

    Removes transparency changes and clouds common to both stars while keeping the
    target in its own units. Points where the smoothed reference falls below
    min_ref_fraction of its median are set to NaN (cloud, star lost)."""
    target = np.asarray(target, dtype=float)
    ref_s = smooth(reference, window, method)
    bad = ref_s < min_ref_fraction * np.nanmedian(ref_s)
    ref_s = np.where(bad, np.nan, ref_s)
    return target * np.nanmean(ref_s) / ref_s


def relative_flux(target: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Plain ratio target / reference (differential photometry)."""
    return np.asarray(target, dtype=float) / np.asarray(reference, dtype=float)


def delta_magnitude(ratio: np.ndarray, reference_mag: Optional[float] = None) -> np.ndarray:
    """-2.5 log10(ratio); add reference_mag to get the target magnitude."""
    with np.errstate(divide='ignore', invalid='ignore'):
        dm = -2.5 * np.log10(np.asarray(ratio, dtype=float))
    return dm if reference_mag is None else dm + reference_mag


def baseline_normalize(y: np.ndarray, baseline: slice | np.ndarray) -> np.ndarray:
    """Scale so the median over the baseline (a slice or boolean mask) is 1.0."""
    y = np.asarray(y, dtype=float)
    return y / np.nanmedian(y[baseline])


def snr(y: np.ndarray, baseline: slice | np.ndarray) -> float:
    """Baseline mean / baseline std: the usual occultation-quality figure."""
    b = np.asarray(y, dtype=float)[baseline]
    return float(np.nanmean(b) / np.nanstd(b))
