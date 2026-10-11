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
                    dark_threshold: float = 50.0, gain_threshold: float = 0.5) -> Calibration:
        """dark only: offset and hot pixels are corrected, no flat field.
        flat_dark: dark for the flat exposure, if different from `dark`.
        dark_threshold: counts above the local dark level that make a pixel 'hot'.
        gain_threshold: gain deviation from the local median that makes a pixel defective."""
        if dark is None and flat is None:
            raise ValueError('need a dark and/or a flat')
        shape = (dark if dark is not None else flat).shape
        d = np.zeros(shape) if dark is None else np.asarray(dark, dtype=np.float64)
        dark_median = float(np.median(d))
        defects = np.zeros(shape, dtype=np.uint16)
        if dark is not None:
            local = d - medfilt2d(d, 5) + dark_median
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
