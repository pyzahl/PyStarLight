"""Background estimation, star masks, centroids and aperture photometry.

Extracted from PyMovie 4.2.8 (main.py: newRobustMeanStd, remove_stars, get_mask,
centre_of_gravity, brightest_pixel, buildDefaultMask, computeInitialThreshold,
bestNxNappsum, getApertureStats). Pure functions, no GUI state.

Python loops over pixels and blobs were replaced by numpy. The arithmetic is the same,
so results should match PyMovie; check against reference runs before relying on that.
PyMovie quirks that change numbers are kept and marked "PyMovie quirk".

Not extracted: NRE (Naylor/PSF-fit extraction, needs a PSF-gathering pass),
GCE (growth curve, marked a failure by its author), the 'psf-star' aperture path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from skimage import measure
from skimage.morphology import convex_hull_image

# --------------------------------------------------------------------------- background

@dataclass
class RobustStats:
    mean: float               # background level
    std: float                # background noise
    max: float                # brightest pixel in the data
    bkgnd_values: np.ndarray  # pixels judged to be background (used by TME hunt-bias)


def _blur_threshold(img: np.ndarray, ksize, cut) -> np.ndarray:
    """Gaussian blur (sigma 1.1) as uint16, then binary threshold > cut."""
    blurred = cv2.GaussianBlur(img.astype('uint16'), ksize=ksize, sigmaX=1.1)
    _, t_mask = cv2.threshold(blurred, cut, 1, cv2.THRESH_BINARY)
    return t_mask


def star_pixel_mask(img: np.ndarray, cut: float, ksize=(5, 5), min_pixels: int = 5) -> np.ndarray:
    """True where a blob of at least min_pixels lies above cut (PyMovie remove_stars)."""
    labels = measure.label(_blur_threshold(img, ksize, cut), connectivity=1, background=0)
    areas = np.bincount(labels.ravel())
    big = areas >= min_pixels
    big[0] = False
    return big[labels]


def robust_mean_std(data: np.ndarray, lunar: bool = False) -> RobustStats:
    """PyMovie's background estimator for an aperture thumbnail.

    1. noise estimate = median of per-row std (stars touch few rows)
    2. clip at mean + 4.5*noise, get mean/std of what is left
    3. find stars (blobs above mean + 2*std) and drop them
    4. background = mean of the star-free pixels

    lunar=True: background = mean of the pixels below the overall mean (bright limb)."""
    flat = data.ravel()
    if lunar:
        s = np.sort(flat)
        mean = round(np.mean(s))
        mean_at = np.where(s >= mean)[0][0]
        lower_mean = np.mean(s[:mean_at])
        mad = np.median(np.abs(s[:mean_at] - lower_mean)) * 1.486
        return RobustStats(lower_mean, mad, s[-1], s[s <= lower_mean + mad])

    row_std = np.median(np.std(data, axis=1))
    est_mean = np.mean(flat)
    clip_point = est_mean + 4.5 * row_std
    bk = flat[flat <= clip_point]
    calced_mean = np.mean(bk)
    bk_sigma = np.std(bk)

    stars = star_pixel_mask(data, cut=calced_mean + 2 * bk_sigma)
    n_star = int(np.count_nonzero(stars))
    starless = np.where(stars, 0, data)

    if n_star == data.size:
        starless_mean = est_mean
    else:
        starless_mean = np.sum(starless) / (data.size - n_star)

    # PyMovie quirk: star pixels are 0 here, not removed, so they fall below the clip
    # point and enlarge the std when stars are present.
    flat2 = starless.ravel()
    clip2 = round(np.mean(starless)) + 3 * bk_sigma
    bk2 = flat2[flat2 <= clip2]
    return RobustStats(starless_mean, float(np.std(bk2)), flat.max(), bk2)


# --------------------------------------------------------------------------- star mask

@dataclass
class Blob:
    area: int                 # pixels in the chosen blob, 0 if none qualifies
    mask: np.ndarray          # int16 0/1 mask of the chosen blob
    centroid: tuple           # (row, col) unweighted centroid, (None, None) if no blob
    convex_area: int
    n_blobs: int
    extent: int


def blob_mask(img: np.ndarray, cut: float, ksize=(5, 5), min_pixels: int = 9,
              max_centroid_distance: Optional[float] = None, lunar: bool = False,
              bkavg: Optional[float] = None) -> Blob:
    """Pick the blob with the largest net signal above cut (PyMovie get_mask).

    max_centroid_distance: ignore blobs whose centroid is further than this from the
    thumbnail centre (used for red/green apertures, not yellow).
    bkavg: background of img if already known (saves recomputing robust_mean_std)."""
    labels = measure.label(_blur_threshold(img, ksize, cut), connectivity=1, background=0)
    n_blobs = int(labels.max())
    empty = Blob(0, np.zeros(img.shape, 'int16'), (None, None), 0, n_blobs, 0)
    if n_blobs == 0:
        return empty

    if bkavg is None:
        bkavg = robust_mean_std(img, lunar=lunar).mean
    lab = labels.ravel()
    rows, cols = np.indices(img.shape)
    area = np.bincount(lab, minlength=n_blobs + 1)[1:]
    pix_sum = np.bincount(lab, weights=img.ravel().astype(np.float64), minlength=n_blobs + 1)[1:]
    cr = np.bincount(lab, weights=rows.ravel(), minlength=n_blobs + 1)[1:] / area
    cc = np.bincount(lab, weights=cols.ravel(), minlength=n_blobs + 1)[1:] / area
    signal = pix_sum - area * bkavg

    eligible = signal > 0
    if max_centroid_distance is not None:
        c = int(img.shape[0] / 2)
        eligible &= np.hypot(cr - c, cc - c) <= max_centroid_distance
    if not eligible.any():
        return empty

    k = int(np.argmax(np.where(eligible, signal, -np.inf)))   # first blob with max signal
    blob = labels == (k + 1)
    rr, cc_ = np.nonzero(blob)
    extent = max(cc_.max() + 1 - cc_.min(), rr.max() + 1 - rr.min())
    centroid = (cr[k], cc[k])
    if area[k] < min_pixels:
        return Blob(0, np.zeros(img.shape, 'int16'), centroid, int(convex_hull_image(blob).sum()),
                    n_blobs, extent)
    return Blob(int(area[k]), blob.astype('int16'), centroid, int(convex_hull_image(blob).sum()),
                n_blobs, extent)


# --------------------------------------------------------------------------- centroids

def centre_of_gravity(img: np.ndarray) -> np.ndarray:
    """Intensity-weighted centroid, returned as [x (col), y (row)]."""
    y, x = np.indices(img.shape)
    s = img.sum()
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.array([(x * img).sum() / s, (y * img).sum() / s])


def brightest_pixel(img: np.ndarray, n_pixels: int = 9) -> np.ndarray:
    """Brightest-pixel centroid (Basden et al. 2011): subtract the n-th brightest value,
    clip at 0, take the centre of gravity. Returns [x, y]."""
    img = img.astype(np.float64, copy=True)
    thresh = np.sort(img.ravel())[-n_pixels]
    if img.max() == thresh:
        thresh -= 1
    return centre_of_gravity(np.clip(img - thresh, 0, None))


# --------------------------------------------------------------------------- masks

def circular_mask(size: int, radius: float) -> tuple[np.ndarray, int]:
    """Centred disc of the given radius in a size x size box (PyMovie default mask)."""
    c = size // 2
    radius = min(radius, c - 1)
    y, x = np.indices((size, size))
    mask = ((y - c) ** 2 + (x - c) ** 2 <= radius ** 2).astype('int16')
    return mask, int(mask.sum())


def border_mask(size: int) -> np.ndarray:
    """All ones with a 1-pixel zero border (white apertures)."""
    m = np.ones((size, size), dtype=int)
    m[0, :] = m[-1, :] = m[:, 0] = m[:, -1] = 0
    return m


def initial_threshold(thumbnail: np.ndarray, sigma_level: float = 2.0, lunar: bool = False) -> int:
    """Mask threshold as an offset above background: sigma_level * ceil(std)."""
    st = robust_mean_std(thumbnail, lunar=lunar)
    return int(sigma_level * np.ceil(st.std))


NON_RECENTERING_TAGS = ('no-rc', 'no_rc', 'no rc', 'empty', 'no-star', 'no_star', 'no star')


def is_non_recentering(name: str) -> bool:
    n = name.lower()
    return any(t in n for t in NON_RECENTERING_TAGS)


# --------------------------------------------------------------------------- TME

def best_nxn_sum(image: np.ndarray, mask: np.ndarray, n: int = 3) -> tuple[float, int, int]:
    """Roll the image under a fixed mask over an n x n grid; return (max sum, drow, dcol)."""
    half = n // 2
    best = None
    for dr in range(-half, half + 1):
        for dc in range(-half, half + 1):
            s = np.sum(np.roll(image, (dr, dc), (0, 1)) * mask)
            if best is None or s > best[0]:
                best = (s, dr, dc)
    return best


# --------------------------------------------------------------------------- measurement

@dataclass
class PhotometryConfig:
    lunar: bool = False
    gaussian_blur: tuple = (5, 5)
    max_centroid_distance: float = 8     # red/green blobs further than this are ignored
    tme_search: int = 3                  # TME search grid (1, 3, 5, 7)
    tme_hunt_bias_correction: bool = False
    smoothing_count: int = 100           # background smoothing time constant (frames)


@dataclass
class Measurement:
    """One aperture at one frame. Matches the columns of PyMovie's CSV."""
    signal: float            # appsum - mskpx * bkavg
    appsum: float            # sum of pixels under the mask
    bkavg: float             # background per pixel
    bkstd: float
    mskpx: int               # mask pixel count; negative when the default mask was used
    xc_roi: Optional[float]  # centroid in thumbnail coordinates
    yc_roi: Optional[float]
    xc: Optional[float]      # centroid in image coordinates
    yc: Optional[float]
    maxpx: float
    cvxhull: int
    hit_defect: int = 0
    background_sample: float = 0.0   # value fed to the per-aperture background smoother
    track_xy: Optional[tuple] = None # yellow apertures: star position (col,row) in thumbnail
    mask: Optional[np.ndarray] = None
    blob_mask: Optional[np.ndarray] = None   # mask from blob_mask() before default fallback
    fields: Optional[tuple] = None   # (top, bottom) Measurement for field mode


def measure_aperture(image: np.ndarray, x0: int, y0: int, size: int, *,
                     color: str = 'red', thresh: float = 99999, name: str = '',
                     default_mask: Optional[np.ndarray] = None, default_mask_radius: float = 3.2,
                     method: str = 'aperture', smoothed_background: float = 0.0,
                     yellow_mask: Optional[np.ndarray] = None,
                     defect_frame: Optional[np.ndarray] = None,
                     fields: bool = False,
                     cfg: PhotometryConfig = PhotometryConfig(),
                     calibration=None) -> Measurement:
    """Measure one square aperture whose top-left corner is (x0, y0).

    color: 'red'/'green' (star must lie near the centre), 'yellow' (tracking star,
           reports track_xy), 'white' (whole box, no background subtraction).
    thresh: mask threshold above background. The PyMovie default 99999 means
           "never find a blob": the circular default mask is used, re-centred on the
           brightest pixels.
    method: 'aperture' (mask sum) or 'tme' (tight mask, best position in a search grid).
    yellow_mask: if given, use this mask for every aperture (PyMovie 'use yellow mask').
    calibration: a calibrate.Calibration; dark/flat are applied to this box only, and its
           defect map is used for hit_defect unless defect_frame is given.
    """
    c = int(size / 2)
    thumb = image[y0:y0 + size, x0:x0 + size]
    if calibration is not None:
        thumb = calibration.apply_roi(thumb, x0, y0)
        if defect_frame is None:
            defect_frame = calibration.defects
    st = robust_mean_std(thumb, lunar=cfg.lunar)
    mean, std, maxpx = st.mean, st.std, st.max
    background_sample = mean
    threshold = thresh + int(round(mean))
    if default_mask is None:
        default_mask, _ = circular_mask(size, default_mask_radius)
    track_xy = None
    blob_m = None

    if color == 'white':
        mask = border_mask(size)
        cvxhull = size * size
        area = int(mask.sum())
        centroid = (c, c)
    else:
        b = blob_mask(thumb, cut=threshold, ksize=cfg.gaussian_blur, lunar=cfg.lunar, bkavg=mean,
                      max_centroid_distance=None if color == 'yellow' else cfg.max_centroid_distance)
        area, mask, centroid, cvxhull = b.area, b.mask, b.centroid, b.convex_area
        blob_m = b.mask
        if color == 'yellow' and area:
            track_xy = (round(centroid[1]), round(centroid[0]))

    default_used = area == 0
    if default_used:
        mask = default_mask
        area = int(mask.sum())
        centroid = (c, c)

    thumb = thumb.astype('int32')

    if default_used:
        try:
            mc = brightest_pixel(mask * thumb, 5)
            x, y = round(mc[0]), round(mc[1])
        except ValueError:          # empty mask -> NaN centroid
            x, y = c, c
        dx, dy = x - c, y - c
        acc = round(default_mask_radius)
        # PyMovie quirk: no abs(), so large negative offsets are always accepted.
        if dx <= acc and dy <= acc:
            if color == 'yellow':
                track_xy = (x, y)
            if not is_non_recentering(name):
                max_roll = int(size // 2 - np.ceil(default_mask_radius))
                dx = int(np.clip(dx, -max_roll, max_roll))
                dy = int(np.clip(dy, -max_roll, max_roll))
                mask = np.roll(mask, dx, axis=1)
                mask = np.roll(mask, dy, axis=0)

    hit_defect = 0
    defect = None if defect_frame is None else defect_frame[y0:y0 + size, x0:x0 + size]

    if yellow_mask is not None:
        default_used = False
        appsum = float(np.sum(yellow_mask * thumb))
        area = int(np.sum(yellow_mask))
        signal = appsum - int(round(area * mean))
    else:
        if method == 'tme':
            search = 1 if is_non_recentering(name) else cfg.tme_search
            appsum, dr, dc = best_nxn_sum(thumb, default_mask, search)
            mask = np.roll(default_mask, (-dr, -dc), (0, 1))
            if search > 1:
                # Same search on resampled background pixels = bias from hunting for the max
                rng = np.random.RandomState(1)
                bg = rng.choice(st.bkgnd_values, size=(size, size), replace=True)
                empty, *_ = best_nxn_sum(bg - mean, default_mask, search)
                background_sample = empty / int(default_mask.sum())
                if cfg.tme_hunt_bias_correction:
                    mean += smoothed_background or background_sample
        else:
            appsum = float(np.sum(thumb * mask))

        if color == 'white':
            signal = appsum
        else:
            signal = appsum - int(np.round(area * mean))
            if defect is not None:
                hit_defect = int(np.sum(mask * defect))

    if centroid != (None, None):
        xc_roi, yc_roi = centroid[1], centroid[0]
        xc, yc = xc_roi + x0, yc_roi + y0
    else:
        xc_roi = yc_roi = xc = yc = None

    field_results = None
    if fields:
        field_results = _measure_fields(thumb, mask, y0, color, default_used, std, maxpx,
                                        cvxhull, cfg)

    return Measurement(signal, appsum, mean, std, -area if default_used else area,
                       xc_roi, yc_roi, xc, yc, maxpx, cvxhull, hit_defect,
                       background_sample, track_xy, mask, blob_m, field_results)


def _measure_fields(thumb, mask, y0, color, default_used, std, maxpx, cvxhull, cfg):
    """Split the aperture into top/bottom image fields (interlaced video)."""
    top_i, bot_i = (0, 1) if y0 % 2 == 0 else (1, 0)
    # PyMovie quirk: the field backgrounds come from thumbnail rows 0::2 / 1::2, while
    # the field pixels follow the image row parity, so they swap when y0 is odd.
    bk_top = robust_mean_std(thumb[0::2, :], lunar=cfg.lunar).mean
    bk_bot = robust_mean_std(thumb[1::2, :], lunar=cfg.lunar).mean
    out = []
    for i, bk in ((top_i, bk_top), (bot_i, bk_bot)):
        m = mask[i::2, :]
        t = thumb[i::2, :]
        n = int(np.sum(m))
        appsum = float(np.sum(m * t))
        signal = appsum if color == 'white' else appsum - int(round(n * bk))
        out.append(Measurement(signal, appsum, bk, std, -n if default_used else n,
                               None, None, None, None, maxpx, cvxhull))
    return tuple(out)
