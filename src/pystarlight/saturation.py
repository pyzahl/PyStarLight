"""Saturated stars: found per aperture and frame, and kept out of the evaluation.

NEW CODE. A star whose brightest pixel reaches the ADC's full scale is clipped: its measured
flux is too low, and by an amount that follows the seeing (a sharper image clips more), so it
neither normalises the target (the ensemble wobbles with the seeing, clouds dim it less) nor
gives a magnitude zero point. Both photometry modes lose flux; optimal extraction more than
the circle (its weights are largest in the clipped core). Such stars are not repaired (no
masking of the core): they are left out of the references, check-star comparisons and the
zero point, with a warning. Tracking still uses them (a clipped star is still symmetric).

    level, how = full_scale(metadata, dtype)        # e.g. (4095, '12-bit (IMAGE-BIT-DEPTH)')
    rep = report(lc, level)                          # {name: fraction of frames saturated}
"""

from __future__ import annotations

from typing import Optional

import numpy as np

FRACTION = 0.9          # a pixel at >= this x full scale counts as saturated (non-linear before)
MAX_FRACTION = 0.01     # a star saturated in more frames than this is left out of the evaluation


def full_scale(metadata: Optional[dict], max_value=None) -> tuple[Optional[float], str]:
    """(full-scale pixel value, how it is known) from the recording itself: its bit depth (RAVF
    IMAGE-BIT-DEPTH, SER PixelDepthPerPlane) or the reader's max_value (ADV MaxPixelValue);
    (None, '') when it does not say (ASTRID RAVF: 10 bit left-aligned in 16, no depth)."""
    if max_value:
        return float(max_value), 'MaxPixelValue'
    m = {str(k).upper(): v for k, v in (metadata or {}).items()}
    for key in ('IMAGE-BIT-DEPTH', 'PIXELDEPTHPERPLANE', 'IMAGE-BITPIX', 'BITPIX', 'BITPIX-DEPTH'):
        try:
            bits = int(float(m[key]))
        except (KeyError, TypeError, ValueError):
            continue
        if 1 < bits <= 32:
            return float(2 ** bits - 1), f'{bits}-bit ({key})'
    return None, ''


def ceiling(peaks: np.ndarray, min_repeats: int = 5) -> Optional[float]:
    """A clip level seen in the data: the highest brightest-pixel value, when it occurs exactly
    in at least min_repeats frames (a clipped star hits the same number again and again)."""
    p = np.asarray(peaks, dtype=float)
    p = p[np.isfinite(p)]
    if not p.size:
        return None
    top = p.max()
    return float(top) if top > 0 and np.count_nonzero(p == top) >= min_repeats else None


def level_of(lc, metadata=None, dtype=None, max_value=None) -> tuple[Optional[float], str]:
    """The saturation level for a run: the recording's full scale (full_scale), else a ceiling
    the apertures' brightest pixels keep hitting (right-aligned data without a depth), else the
    data type's (255 for 8 bit, 65535 for 16: left-aligned data, e.g. ASTRID), else (None, '')."""
    level, how = full_scale(metadata if metadata is not None else getattr(lc, 'metadata', {}), max_value)
    if level is not None:
        return level, how
    peaks = np.concatenate([np.asarray(lc[n]['maxpx'], dtype=float) for n in lc.names
                            if 'maxpx' in lc.columns[n]] or [np.zeros(0)])
    top = ceiling(peaks)
    if top is not None:
        return top, 'ceiling in the data'
    if dtype is not None and np.dtype(dtype).kind == 'u':
        return float(np.iinfo(np.dtype(dtype)).max), f'{np.dtype(dtype).itemsize * 8}-bit data'
    return None, ''


def report(lc, level: Optional[float], names=None, fraction: float = FRACTION) -> dict:
    """{aperture: fraction of its frames with the brightest pixel >= fraction x level}; {} when
    the level is not known."""
    if not level:
        return {}
    out = {}
    for n in names or lc.names:
        if n not in lc.columns or 'maxpx' not in lc.columns[n]:
            continue
        p = np.asarray(lc[n]['maxpx'], dtype=float)
        ok = np.isfinite(p)
        out[n] = float(np.count_nonzero(p[ok] >= fraction * level) / max(1, np.count_nonzero(ok)))
    return out


def split(names, rep: dict, max_fraction: float = MAX_FRACTION) -> tuple[list, list]:
    """(names kept, names left out: saturated in more than max_fraction of their frames)."""
    bad = [n for n in names if rep.get(n, 0.0) > max_fraction]
    return [n for n in names if n not in bad], bad


def describe(rep: dict, names=None) -> str:
    """'ref2 72 %, check1 3 %' for the apertures with saturated frames."""
    return ', '.join(f'{n} {100 * f:.0f} %' for n, f in rep.items()
                     if f > 0 and (names is None or n in names))
