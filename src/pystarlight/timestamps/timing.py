"""Timestamp parsing and timing checks.

NEW CODE, not from PyMovie. PyMovie writes the timestamps to the CSV and leaves checking
them to PyOTE. These helpers find the usual problems early: OCR misreads, dropped or
duplicated frames, midnight rollover, and the wrong field order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

_TIME_RE = re.compile(r'^\[?\s*(\d{1,2}):(\d{2}):(\d{2}(?:\.\d*)?)\s*\]?$')


def parse_time(s: Optional[str]) -> float:
    """'hh:mm:ss.ffff' or '[hh:mm:ss.ffff]' -> seconds since midnight; NaN if not a time.
    PyMovie's 'unreadable' marker [00:00:00.0000] also gives NaN."""
    if not s:
        return float('nan')
    m = _TIME_RE.match(s.strip())
    if not m:
        return float('nan')
    t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    return float('nan') if t == 0.0 else t


def parse_times(times: Sequence[Optional[str]]) -> np.ndarray:
    return np.array([parse_time(t) for t in times], dtype=float)


def unwrap_midnight(seconds: np.ndarray) -> np.ndarray:
    """Add 86400 s after each backwards jump of more than 12 h (UTC midnight)."""
    t = np.asarray(seconds, dtype=float).copy()
    valid = np.flatnonzero(np.isfinite(t))
    if len(valid) < 2:
        return t
    jumps = np.diff(t[valid]) < -43200
    offsets = np.concatenate([[0], np.cumsum(jumps)]) * 86400.0
    t[valid] += offsets
    return t


@dataclass
class TimingReport:
    step: float                          # typical time between rows (median), s
    n_rows: int
    unreadable: list[int] = field(default_factory=list)   # rows without a valid time
    dropped: list[tuple[int, int]] = field(default_factory=list)  # (row, frames missing before it)
    duplicates: list[int] = field(default_factory=list)   # rows with the same time as the previous one
    backwards: list[int] = field(default_factory=list)    # rows earlier than the previous one
    irregular: list[int] = field(default_factory=list)    # steps off by more than tol (not a whole multiple)

    @property
    def ok(self) -> bool:
        return not (self.unreadable or self.dropped or self.duplicates or self.backwards or self.irregular)

    def summary(self) -> str:
        parts = [f'{self.n_rows} rows, step {self.step * 1000:.3f} ms']
        for name in ('unreadable', 'dropped', 'duplicates', 'backwards', 'irregular'):
            v = getattr(self, name)
            if v:
                parts.append(f'{name}: {len(v)} (first at row {v[0][0] if name == "dropped" else v[0]})')
        return '; '.join(parts) if len(parts) > 1 else parts[0] + '; no problems found'


def check_timing(times: Sequence | np.ndarray, expected_step: Optional[float] = None,
                 tol: float = 0.25) -> TimingReport:
    """Check a sequence of times (strings or seconds), one per row, in row order.

    expected_step: frame (or field) interval in s; default is the median step.
    tol: allowed deviation as a fraction of the step (VTI times have 0.1 ms resolution,
         camera-clock times can jitter a little)."""
    t = parse_times(times) if len(times) and isinstance(times[0], (str, type(None))) else \
        np.asarray(times, dtype=float)
    t = unwrap_midnight(t)
    n = len(t)
    rep = TimingReport(step=float('nan'), n_rows=n)
    rep.unreadable = [int(i) for i in np.flatnonzero(~np.isfinite(t))]
    valid = np.flatnonzero(np.isfinite(t))
    if len(valid) < 2:
        return rep

    d = np.diff(t[valid])
    rows = valid[1:]
    gaps = np.diff(valid)                 # rows between valid neighbours
    per_row = d / gaps
    if expected_step:
        step = float(expected_step)
    elif np.any(per_row > 0):
        step = float(np.median(per_row[per_row > 0]))
    else:
        step = 0.0
    rep.step = step
    if step <= 0:
        return rep

    for r, dt, g in zip(rows, d, gaps):
        k = dt / step
        if dt < 0:
            rep.backwards.append(int(r))
        elif abs(dt) < tol * step:
            rep.duplicates.append(int(r))
        elif abs(k - round(k)) > tol:
            rep.irregular.append(int(r))
        elif round(k) > g:
            rep.dropped.append((int(r), int(round(k) - g)))
    return rep


def field_times(upper: Sequence[float], lower: Sequence[float], top_field_first: bool) -> np.ndarray:
    """Interleave per-field times into row order (first field, second field, ...)."""
    first, second = (upper, lower) if top_field_first else (lower, upper)
    out = np.empty(2 * len(first))
    out[0::2], out[1::2] = first, second
    return out
