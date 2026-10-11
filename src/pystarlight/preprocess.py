"""Optional per-frame corrections, applied in PyMovie's order:

    gamma -> CMOS outlaw-pixel scrub -> hot-pixel erase -> line-noise median filter -> dark/flat

Extracted from PyMovie 4.2.8 (main.py: doGammaCorrection, scrubImage, applyHotPixelErasure,
getBackgroundFromImageCenter, applyMedianFilterToImage, applyDarkFlatCorrect; gammaUtils.py).
Loops over pixels/rows were replaced by numpy; results are the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .photometry import robust_mean_std


def gamma_lut(gamma: float, bits: int) -> np.ndarray:
    """Lookup table that undoes a camera gamma (PyMovie gammaDecode8bit/16bit)."""
    scale = 255 if bits == 8 else 65535
    x = np.arange(scale + 1, dtype=float)
    lut = (scale * np.power(x / scale, 1.0 / gamma)).astype(int)
    return lut.astype('uint8' if bits == 8 else 'uint16')


def scrub_outlaw_pixels(image: np.ndarray, points: Sequence[tuple[int, int]]) -> np.ndarray:
    """Replace each (row, col) with the mean of its 8 neighbours (CMOS defect pixels).
    Neighbours are taken from the uncorrected image."""
    out = image.copy()
    if len(points) == 0:
        return out
    pts = np.asarray(points)
    r, c = pts[:, 0], pts[:, 1]
    acc = np.zeros(len(pts), dtype=np.float64)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr or dc:
                acc += image[r + dr, c + dc]
    out[r, c] = acc / 8
    return out


def background_from_image_center(image: np.ndarray, size: int = 51) -> float:
    y0, x0 = image.shape[0] // 2, image.shape[1] // 2
    return robust_mean_std(image[y0:y0 + size, x0:x0 + size]).mean


def erase_hot_pixels(image: np.ndarray, points: Sequence[tuple[int, int]],
                     background: Optional[float] = None) -> np.ndarray:
    """Set each (row, col) to the background measured at the image centre."""
    if len(points) == 0:
        return image
    if background is None:
        background = background_from_image_center(image)
    out = image.copy()
    pts = np.asarray(points)
    out[pts[:, 0], pts[:, 1]] = background
    return out


def line_noise_filter(image: np.ndarray, horizontal: bool = True, vertical: bool = False,
                      top_rows_excluded: int = 0, bottom_rows_excluded: int = 0) -> np.ndarray:
    """Remove row/column banding by shifting each row (column) median to the median of medians.

    PyMovie quirk kept: excluded (timestamp) rows get a row median of 0, so they are
    shifted too and the zeros take part in the median of medians."""
    if image.dtype == np.uint16:
        max_pixel = 65535
    elif image.dtype == np.uint8:
        max_pixel = 255
    else:
        raise ValueError(f'unsupported dtype {image.dtype}')
    h, w = image.shape
    top, bot = top_rows_excluded, h - bottom_rows_excluded
    out = image.astype(int)

    hor = np.zeros(h, dtype=image.dtype)
    hor[top:bot] = np.median(image[top:bot, :], axis=1).astype(int)
    vert = np.median(image[top:bot, :], axis=0).astype(int).astype(image.dtype)

    if horizontal:
        mid = int(np.median(hor))
        out = np.clip(out + (mid - hor.astype(int))[:, None], 0, max_pixel)
    if vertical:
        mid = int(np.median(vert))
        out = np.clip(out + (mid - vert.astype(int))[None, :], 0, max_pixel)
    return out.astype(image.dtype)


def dark_flat_correct(image: np.ndarray, dark: np.ndarray, gain: np.ndarray,
                      dark_median: float) -> np.ndarray:
    """(frame - dark) * gain + median(dark), clipped at 0. Returns float."""
    if image.shape != gain.shape:
        raise ValueError('frame shape does not match dark/flat shape')
    return np.clip((image - dark) * gain + dark_median, 0.0, None)


@dataclass
class Preprocessor:
    """Bundle of the corrections. Leave a field as None to skip that step."""
    gamma: Optional[float] = None
    outlaw_pixels: Optional[Sequence[tuple[int, int]]] = None
    hot_pixels: Optional[Sequence[tuple[int, int]]] = None
    line_filter: Optional[dict] = None      # kwargs for line_noise_filter
    dark: Optional[np.ndarray] = None
    gain: Optional[np.ndarray] = None
    dark_median: float = 0.0

    _lut: Optional[np.ndarray] = None

    def __call__(self, image: np.ndarray) -> np.ndarray:
        if self.gamma is not None and self.gamma != 1.0:
            bits = 8 if image.dtype == np.uint8 else 16
            if self._lut is None or len(self._lut) != (256 if bits == 8 else 65536):
                self._lut = gamma_lut(self.gamma, bits)
            image = self._lut.take(image)
        if self.outlaw_pixels:
            image = scrub_outlaw_pixels(image, self.outlaw_pixels)
        if self.hot_pixels:
            image = erase_hot_pixels(image, self.hot_pixels)
        if self.line_filter is not None:
            image = line_noise_filter(image, **self.line_filter)
        if self.dark is not None and self.gain is not None:
            image = dark_flat_correct(image, self.dark, self.gain, self.dark_median)
        return image
