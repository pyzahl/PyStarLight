"""Dark / flat calibration, applied only to the aperture regions while measuring.

Formulas follow PyMovie 4.2.8 (main.py: buildDarkFrame, buildFlatFrame, calculateGainFrame,
buildDarkDefectFrame, buildGainDefectFrame, applyDarkFlatCorrect):

    master dark/flat = mean of N frames
    gain   = (mean(flat) - mean(dark)) / (flat - dark), clipped to 0.33..3 (0.25 where flat <= dark)
    output = (frame - dark) * gain + median(dark),  clipped at 0

The median(dark) offset keeps the background level where it was, so thresholds set on
uncorrected frames still work. Correcting only the aperture boxes gives exactly the same
numbers as correcting whole frames, at a fraction of the cost.

Taking calibration frames (record a few seconds of each, same gain/exposure as the run):
  dark: telescope covered.   flat: evenly lit (twilight sky or a light panel), not saturated.
  flat-dark (optional): covered, at the flat's exposure, if it differs from the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.signal import medfilt2d

from .movie import open_movie


def master_frame(source, start: int = 0, count: Optional[int] = None, method: str = 'mean') -> np.ndarray:
    """Stack frames of a calibration video (path or open reader). method: 'mean' (PyMovie)
    or 'median' (ignores cosmic-ray hits and satellites)."""
    reader = open_movie(source) if isinstance(source, str) else source
    try:
        stop = len(reader) - 1 if count is None else min(len(reader) - 1, start + count - 1)
        frames = [f.image.astype(np.float64) for f in reader.frames(start, stop)]
    finally:
        if isinstance(source, str):
            reader.close()
    if method == 'median':
        return np.median(frames, axis=0)
    return np.mean(frames, axis=0)


@dataclass
class Calibration:
    dark: Optional[np.ndarray] = None    # master dark, float
    gain: Optional[np.ndarray] = None    # per-pixel gain from the flat (1.0 = no change)
    dark_median: float = 0.0
    defects: Optional[np.ndarray] = None  # 1 where a pixel is hot (dark) or has bad gain (flat)

    # ---------------------------------------------------------------- build

    @classmethod
    def from_frames(cls, dark: Optional[np.ndarray] = None, flat: Optional[np.ndarray] = None,
                    flat_dark: Optional[np.ndarray] = None,
                    dark_threshold: Optional[float] = None, gain_threshold: float = 0.5) -> Calibration:
        """dark only: offset and hot pixels are corrected, no flat field.
        flat_dark: dark for the flat exposure, if different from `dark`.
        dark_threshold: counts above the local dark level that make a pixel 'hot'. Default:
            6 x the dark's robust pixel noise (PyMovie used a fixed 50, meant for 8-bit video;
            16-bit cameras need far more).
        gain_threshold: gain deviation from the local median that makes a pixel defective."""
        if dark is None and flat is None:
            raise ValueError('need a dark and/or a flat')
        shape = (dark if dark is not None else flat).shape
        d = np.zeros(shape) if dark is None else np.asarray(dark, dtype=np.float64)
        dark_median = float(np.median(d))
        defects = np.zeros(shape, dtype=np.uint16)
        if dark is not None:
            local = d - medfilt2d(d, 5) + dark_median
            if dark_threshold is None:
                sigma = 1.4826 * float(np.median(np.abs(local - dark_median)))
                dark_threshold = max(6.0 * sigma, 1.0)
            defects |= (local > dark_median + dark_threshold).astype(np.uint16)

        gain = np.ones(shape)
        if flat is not None:
            fd = d if flat_dark is None else np.asarray(flat_dark, dtype=np.float64)
            f = np.asarray(flat, dtype=np.float64)
            normal_delta = f.mean() - fd.mean()
            if normal_delta <= 0:
                raise ValueError('flat mean is not above dark mean')
            delta = f - fd
            with np.errstate(divide='ignore'):
                gain = np.where(delta <= 0, 0.25, np.clip(normal_delta / delta, 0.33, 3.0))
            local = gain - medfilt2d(gain, 5) + np.median(gain)
            med = np.median(local)
            upper = min(med + gain_threshold, 3.0)
            lower = max(0.33, med - gain_threshold)
            defects |= ((local > upper) | (local < lower)).astype(np.uint16)
        # the 5x5 median filter pads with zeros: ignore its 2-pixel edge (as PyMovie does)
        defects[:2, :] = defects[-2:, :] = 0
        defects[:, :2] = defects[:, -2:] = 0
        return cls(None if dark is None else d, gain, dark_median, defects)

    @classmethod
    def from_videos(cls, dark: Optional[str] = None, flat: Optional[str] = None,
                    flat_dark: Optional[str] = None, count: Optional[int] = None,
                    method: str = 'mean', **kw) -> Calibration:
        m = lambda p: None if p is None else master_frame(p, count=count, method=method)
        return cls.from_frames(m(dark), m(flat), m(flat_dark), **kw)

    def save(self, path: str):
        np.savez_compressed(path, dark=self.dark if self.dark is not None else np.zeros(0),
                            gain=self.gain, dark_median=self.dark_median, defects=self.defects)

    @classmethod
    def load(cls, path: str) -> Calibration:
        z = np.load(path)
        dark = z['dark'] if z['dark'].size else None
        return cls(dark, z['gain'], float(z['dark_median']), z['defects'])

    # ---------------------------------------------------------------- apply

    def apply(self, image: np.ndarray) -> np.ndarray:
        """Whole frame (for display or export)."""
        return self.apply_roi(image, 0, 0)

    def apply_roi(self, pixels: np.ndarray, x0: int, y0: int) -> np.ndarray:
        """Correct a cut-out whose top-left corner is (x0, y0) in the full frame."""
        h, w = pixels.shape
        sl = (slice(y0, y0 + h), slice(x0, x0 + w))
        dark = 0.0 if self.dark is None else self.dark[sl]
        gain = 1.0 if self.gain is None else self.gain[sl]
        return np.clip((pixels - dark) * gain + self.dark_median, 0.0, None)

    def defects_roi(self, x0: int, y0: int, size: int) -> Optional[np.ndarray]:
        return None if self.defects is None else self.defects[y0:y0 + size, x0:x0 + size]


def _cache_dir():
    import os
    from pathlib import Path
    base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'pystarlight' / 'masters'


def cached_master(path: str, count: Optional[int] = None, method: str = 'mean', log=print) -> np.ndarray:
    """master_frame() of a clip, cached by (path, size, mtime, count, method) in
    ~/.cache/pystarlight/masters. Nothing is written next to the clip."""
    import hashlib
    from pathlib import Path
    p = Path(path).expanduser().resolve()
    st = p.stat()
    key = hashlib.sha1(f'{p}|{st.st_size}|{st.st_mtime_ns}|{count}|{method}'.encode()).hexdigest()[:16]
    f = _cache_dir() / f'{p.stem}-{key}.npy'
    if f.exists():
        log(f'using stacked master {f.name}')
        return np.load(f)
    log(f'stacking {p.name} ({method}{", %d frames" % count if count else ", all frames"})')
    m = master_frame(str(p), count=count, method=method)
    f.parent.mkdir(parents=True, exist_ok=True)
    np.save(f, m.astype(np.float32))
    return m


def from_config(cfg: dict, log=print) -> Optional['Calibration']:
    """Calibration from the [calibration] section: dark / flat / flat_dark clips (stacked and
    cached), else a prebuilt `file` (.npz). None if nothing is configured or
    use_calibration = false."""
    from .config import resolve_path
    c = cfg.get('calibration', {})
    if not c.get('use_calibration', True):            # quick off switch, entries kept
        log('calibration disabled (use_calibration = false)')
        return None
    count = int(c.get('frames') or 0) or None
    method = c.get('method', 'mean')
    # relative paths are relative to the config file's folder
    clips = {k: resolve_path(cfg, c.get(k) or '') for k in ('dark', 'flat', 'flat_dark')}
    for k, v in clips.items():
        if v is not None and not v.exists():
            raise FileNotFoundError(f'[calibration] {k} = "{c.get(k)}" not found (looked for {v})')
    if clips['dark'] is not None and clips['flat'] is not None and clips['dark'] == clips['flat']:
        raise ValueError('[calibration] dark and flat are the same clip; a flat must be an evenly lit field')
    if clips['dark'] is not None or clips['flat'] is not None:
        m = {k: cached_master(str(v), count, method, log) if v is not None else None
             for k, v in clips.items()}
        return Calibration.from_frames(m['dark'], m['flat'], m['flat_dark'])
    f = resolve_path(cfg, c.get('file') or '')
    if f is not None:
        return Calibration.load(str(f))
    legacy = resolve_path(cfg, cfg.get('paths', {}).get('calibration') or '')     # older configs
    return Calibration.load(str(legacy)) if legacy is not None else None
