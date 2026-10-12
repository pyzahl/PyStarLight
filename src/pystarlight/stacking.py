"""Stacked reference image of the star field, registered with the tracking solution.

Every analysed frame (or an evenly spaced subset) is mapped back onto the reference frame of
the run with its tracked shift and rotation (global mode), or with a measured shift (phase
correlation) when no tracking transform is available, and averaged. The result is deeper and,
unlike a plain average of a shaking/drifting field, sharp.

    st = stack_field(reader, lc)                     # FieldStack
    save_field_stack(st, 'run-stack', apertures, lc, stars=..., ap_stars=...)
    # -> run-stack.fits (float, with WCS when available), run-stack.svg/.png/.pdf (annotated)
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from scipy import ndimage


@dataclass
class FieldStack:
    image: np.ndarray                 # float32 mean, in the run's reference-frame coordinates
    n: int                            # frames combined
    frames: list = field(default_factory=list)
    method: str = 'tracking'          # 'tracking' (shift + rotation) | 'phase correlation' (shift)
    wcs: object = None                # plate solution moved onto this image (set by attach_wcs)
    shift_to_solution: tuple = (0.0, 0.0)   # (dx, dy): stack pixel + this = solved-stack pixel
    date: str = ''
    time_first: str = ''
    time_last: str = ''


def _transforms(lc) -> Optional[dict]:
    """{frame: (dx, dy, theta)} from a global-mode result (maps reference -> frame)."""
    fi = getattr(lc, 'frame_info', {}) or {}
    if not {'dx', 'dy', 'rot_deg'} <= set(fi):
        return None
    frames = lc.columns[lc.names[0]]['frame']
    return {int(f): (float(dx), float(dy), math.radians(float(r)))
            for f, dx, dy, r in zip(frames, fi['dx'], fi['dy'], fi['rot_deg'])}


def _pick(frames, max_frames):
    frames = list(frames)
    if max_frames and len(frames) > max_frames:
        idx = np.linspace(0, len(frames) - 1, max_frames).round().astype(int)
        frames = [frames[i] for i in sorted(set(idx))]
    return frames


def remove_hot_pixels(img: np.ndarray, k: float = 6.0) -> np.ndarray:
    """Replace isolated single bright pixels (hot pixels, cosmic rays) by their 3x3 median.

    A pixel is replaced only if it is far above the local background and none of its 8
    neighbours is: stars (several pixels wide) always have bright neighbours and are untouched."""
    import cv2
    img = np.ascontiguousarray(img, dtype=np.float32)
    med = cv2.medianBlur(img, 3)                       # much faster than scipy for 3x3
    bg = float(np.median(med[::4, ::4]))
    # pixel noise from the raw image (the median-filtered image is smoother)
    noise = 1.4826 * float(np.median(np.abs(img[::4, ::4] - med[::4, ::4]))) * 1.2 or 1.0
    bright = img > bg + k * noise
    n_bright = ndimage.uniform_filter(bright.astype(np.float32), 3) * 9 - bright
    hot = bright & (n_bright < 0.5) & (img - med > k * noise)
    return np.where(hot, med, img)


def sensor_pattern(reader, frames, n: int = 50, preprocess=None) -> np.ndarray:
    """Fixed sensor pattern from the recording itself: median of `n` unregistered frames, minus
    its own median level. Stars drift across the sensor, so the median keeps the pattern (dark
    non-uniformity, warm pixels) and drops the stars. Needs a drift of several star widths."""
    pick = _pick(frames, n)
    cube = []
    for f in pick:
        img = reader.read(f).image
        cube.append(preprocess(img) if preprocess else img)
    m = np.median(np.asarray(cube), axis=0).astype(np.float32)
    return m - float(np.median(m))


def stack_field(reader, lc=None, frames=None, max_frames: int = 300, calibration=None,
                preprocess: Optional[Callable] = None, progress: Optional[Callable[[int, int], None]] = None,
                cancel: Optional[threading.Event] = None, cosmetic: bool = True,
                self_dark: bool = False) -> FieldStack:
    """Mean of the registered frames. frames: frame numbers (default: those in `lc`, else all).

    cosmetic: remove isolated hot pixels / cosmic rays before registering.
    self_dark: subtract the fixed sensor pattern measured from the recording itself
        (sensor_pattern), which would otherwise be smeared into streaks along the drift path.
        Off by default: it only works when the field drifts many star widths (tens of px);
        with a small drift the median keeps part of each star and the result shows dark
        'shadows' next to every star. A real dark clip ([calibration] dark) is the proper fix.
    These clean the stacked image only (display, astrometry); photometry is unaffected."""
    tr = _transforms(lc) if lc is not None else None
    if frames is None:
        frames = ([int(f) for f in lc.columns[lc.names[0]]['frame']] if lc is not None
                  else list(range(len(reader))))
    frames = _pick(sorted(set(frames)), max_frames)

    pattern = sensor_pattern(reader, frames, preprocess=preprocess) if self_dark else None

    def load(i):
        """read -> preprocess -> dark/flat -> sensor pattern -> cosmetic."""
        img = reader.read(i).image
        img = preprocess(img) if preprocess else img
        img = calibration.apply(img) if calibration is not None else img
        img = np.asarray(img, dtype=np.float32)
        if pattern is not None:
            img = img - pattern
        return remove_hot_pixels(img) if cosmetic else img

    ref = load(frames[0]) if tr is None else None
    acc = cnt = None
    used = []
    for k, f in enumerate(frames):
        if cancel is not None and cancel.is_set():
            break
        img = load(f)
        if acc is None:
            acc = np.zeros(img.shape, np.float64)
            cnt = np.zeros(img.shape, np.float32)
        if tr is not None and f in tr:
            dx, dy, th = tr[f]
            c, s = math.cos(th), math.sin(th)
            # output (row, col) in reference coords -> input = R (x, y) + t, in (row, col) order
            out = ndimage.affine_transform(img, [[c, s], [-s, c]], offset=(dy, dx), order=1,
                                           mode='constant', cval=np.nan)
        else:
            from skimage.registration import phase_cross_correlation
            shift, _, _ = phase_cross_correlation(ref, img, upsample_factor=10, normalization=None)
            out = ndimage.shift(img, shift, order=1, mode='constant', cval=np.nan)
        good = np.isfinite(out)
        acc[good] += out[good]
        cnt[good] += 1
        used.append(f)
        if progress:
            progress(k + 1, len(frames))
    image = (acc / np.maximum(cnt, 1)).astype(np.float32)
    image[cnt == 0] = np.nan
    st = FieldStack(image, len(used), used, 'tracking' if tr is not None else 'phase correlation')
    if pattern is not None:
        st.method += ', sensor pattern removed'
    if lc is not None and lc.time:
        times = [t for t in lc.time if t]
        st.date = next((d for d in lc.date if d), '')
        st.time_first, st.time_last = (times[0], times[-1]) if times else ('', '')
    return st


def attach_wcs(st: FieldStack, wcs, solved_stack: np.ndarray):
    """Move a plate solution of `solved_stack` (e.g. the first frames) onto the field stack."""
    from skimage.registration import phase_cross_correlation
    a = np.nan_to_num(st.image, nan=float(np.nanmedian(st.image)))
    shift, _, _ = phase_cross_correlation(np.asarray(solved_stack, np.float32), a, upsample_factor=10,
                                          normalization=None)
    dy, dx = float(shift[0]), float(shift[1])
    w = wcs.deepcopy()
    w.wcs.crpix = [w.wcs.crpix[0] - dx, w.wcs.crpix[1] - dy]
    st.wcs = w
    st.shift_to_solution = (dx, dy)
    return st


def reference_positions(lc) -> dict:
    """{aperture: (x, y)} in the stack's (reference-frame) coordinates."""
    tr = _transforms(lc)
    out = {}
    for n in lc.names:
        c = lc.columns[n]
        j = int(np.flatnonzero(np.isfinite(c['xc']))[0]) if np.isfinite(c['xc']).any() else None
        if j is None:
            continue
        x, y = float(c['xc'][j]), float(c['yc'][j])
        if tr is not None:
            dx, dy, th = tr[int(c['frame'][j])]
            cs, sn = math.cos(-th), math.sin(-th)
            x, y = cs * (x - dx) - sn * (y - dy), sn * (x - dx) + cs * (y - dy)
        out[n] = (x, y)
    return out


def save_field_stack(st: FieldStack, stem: str, apertures, lc, stars=(), ap_stars=None, title: str = '',
                     formats=('svg', 'png', 'pdf'), fits: bool = True,
                     radius: Optional[float] = None) -> list:
    """Write <stem>.fits and the annotated figure(s). Returns the written paths.

    The figure is drawn with matplotlib; in the SVG the image and the annotation layers are
    separate groups (ids 'field-image', 'apertures', 'aperture-labels', 'stars', 'compass')."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Rectangle

    from . import plot
    written = []
    if fits:
        written.append(_write_fits(st, f'{stem}.fits', title))

    ap_stars = ap_stars or {}
    dx, dy = st.shift_to_solution
    pos = reference_positions(lc)
    if radius is None and 'radius' in getattr(lc, 'frame_info', {}) and len(lc.frame_info['radius']):
        radius = float(lc.frame_info['radius'][0])
    img = np.nan_to_num(st.image, nan=float(np.nanmedian(st.image)))
    lo, hi = plot.zscale(img)
    h, w = img.shape
    with plot.style():
        fig = plt.figure(figsize=(9, 9 * h / w + 0.8))
        ax = fig.add_axes([0.07, 0.06, 0.9, 0.86])
        ax.imshow(img, cmap='gray', vmin=lo, vmax=hi, origin='upper', interpolation='nearest',
                  gid='field-image')
        ax.set_xlim(-0.5, w - 0.5)
        ax.set_ylim(h - 0.5, -0.5)
        ax.set_xlabel('x (px)')
        ax.set_ylabel('y (px)')
        by_name = {a.name: a for a in apertures}
        for n, (x, y) in pos.items():
            a = by_name.get(n)
            if a is None:
                continue
            col = plot.aperture_color(a)
            if radius and a.role != 'sky':
                ax.add_patch(Circle((x, y), radius, fill=False, ec=col, lw=0.8, gid='apertures'))
            else:
                ax.add_patch(Rectangle((x - a.size / 2, y - a.size / 2), a.size, a.size, fill=False, ec=col,
                                       lw=0.8, gid='apertures'))
            s = ap_stars.get(n)
            label = n + (f'\n{s.label()}' if s is not None and s.name else '')
            ax.annotate(label, (x, y), xytext=(10, 8), textcoords='offset points', color=col, fontsize=7,
                        gid='aperture-labels')
        named = [(s.x - dx, s.y - dy) for s in ap_stars.values()]
        for s in stars or []:
            x, y = s.x - dx, s.y - dy
            if any(math.hypot(x - u, y - v) < 3 for u, v in named) or not (0 <= x < w and 0 <= y < h):
                continue
            ax.add_patch(Circle((x, y), max(6.0, 2 * (s.fwhm or 3)), fill=False, ec='orange', lw=0.6,
                                ls='--', gid='stars'))
            ax.annotate(s.label(), (x, y), xytext=(10, -12), textcoords='offset points', color='orange',
                        fontsize=7, gid='stars')
        if st.wcs is not None:
            _compass_and_scale(ax, st.wcs, w, h)
        when = f'{st.date} {st.time_first} - {st.time_last} UTC'.strip() if st.time_first else ''
        sub = f'{st.n} frames stacked ({st.method})' + (f', {when}' if when else '')
        fig.suptitle((title + '\n' if title else '') + sub, fontsize=8)
        for ext in formats:
            path = f'{stem}.{ext}'
            fig.savefig(path)
            written.append(path)
        plt.close(fig)
    return written


def _compass_and_scale(ax, wcs, w, h):
    x0, y0 = 0.07 * w, 0.12 * h
    ra, dec = (float(v) for v in wcs.all_pix2world(x0, y0, 0))
    L = 0.06 * w
    for lab, (r, d) in (('N', (ra, dec + 1 / 60)), ('E', (ra + 1 / 60 / math.cos(math.radians(dec)), dec))):
        xe, ye = (float(v) for v in wcs.all_world2pix(r, d, 0))
        v = np.array([xe - x0, ye - y0])
        v = v / np.hypot(*v) * L
        ax.annotate('', (x0 + v[0], y0 + v[1]), (x0, y0),
                    arrowprops=dict(arrowstyle='->', color='orange', lw=1.0), gid='compass')
        ax.text(x0 + 1.3 * v[0], y0 + 1.3 * v[1], lab, color='orange', ha='center', va='center', fontsize=8,
                gid='compass')
    # scale bar: 1 arcmin
    x1, y1 = (float(v) for v in wcs.all_world2pix(ra, dec + 1 / 60, 0))
    px = math.hypot(x1 - x0, y1 - y0)
    xs, ys = 0.95 * w - px, 0.95 * h
    ax.plot([xs, xs + px], [ys, ys], color='orange', lw=1.5, gid='compass')
    ax.text(xs + px / 2, ys - 0.015 * h, "1'", color='orange', ha='center', va='bottom', fontsize=8,
            gid='compass')


def _write_fits(st: FieldStack, path: str, title: str = '') -> str:
    from astropy.io import fits
    hdr = fits.Header()
    if st.wcs is not None:
        hdr.update(st.wcs.to_header())
    hdr['NCOMBINE'] = (st.n, 'frames averaged')
    hdr['REGISTR'] = (st.method, 'frame registration')
    if st.date and st.time_first:
        hdr['DATE-OBS'] = (f'{st.date}T{st.time_first}', 'first stacked frame (UTC)')
        hdr['DATE-END'] = (f'{st.date}T{st.time_last}', 'last stacked frame (UTC)')
    if title:
        hdr['OBJECT'] = title[:68]
    hdr['ORIGIN'] = 'PyStarLight'
    fits.writeto(path, st.image, hdr, overwrite=True)
    return path
