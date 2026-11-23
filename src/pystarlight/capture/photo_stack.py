"""Stack DSLR photos in colour, aligned by their plate solutions; the stack solved and annotated as
a photo (photo_solve).

    python -m pystarlight.capture.photo_stack PHOTOS... -o DIR [--focal 3000] [--camera NAME]
        [--bin 2] [--kappa 3] [--target NAME]

NEW CODE. Each photo (our FITS, or a camera file: ARW, CR2, NEF, DNG, JPG, its FITS written to
DIR first) is plate-solved (photo_solve, unless its FITS has a WCS already), debayered with the
camera's white balance and its black level off, and mapped onto the first photo's pixels by a
homography fitted through the two plate solutions (on a grid of points): shifts, field rotation
(an hour of tracking) and scale alike, in one warp. The frames are averaged with a weight map
(where a frame does not reach, it does not count); from KAPPA_MIN_FRAMES on, a second pass
leaves out what lies more than kappa sigma from the first pass's mean (hot pixels that moved
with the drift, satellites, planes). Written to DIR: <first>_stack<N>.fits (R, G, B planes,
float32; exposure, NCOMBINE, DATE-OBS / DATE-END), then solved as a photo: WCS and markers into
it, .wcs.fit, .solve.json, _annotated.png.

Memory: a 42 MP frame in RGB float32 is about 0.5 GB; the stack holds three or four such
images. --bin 2 (2 x 2 pixels to one, a quarter of the memory) for a small computer.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import cv2
import numpy as np

from . import dslr as ds
from . import photo_solve as ps
from .decode import BAYER_CODES
from .stacker import iso_utc, write_fits

KAPPA_MIN_FRAMES = 5
GRID = (24, 16)                 # points per axis for the homography fit


def clip_limits(kappa, n):
    """Thresholds (in the others' sample sigma) by the number of other frames m = 0 .. n: the
    value of one frame against the mean of m others follows Student's t with m - 1 degrees of
    freedom, widened by sqrt(1 + 1 / m); the threshold leaves out the share of the noise that
    kappa sigma leaves of a normal distribution (kappa 3: 0.27 %). m < 2: no limit."""
    from scipy.stats import norm, t
    tail = 2 * norm.sf(kappa)
    out = np.full(n + 1, np.inf, np.float32)
    for m in range(2, n + 1):
        out[m] = t.ppf(1 - tail / 2, m - 1) * np.sqrt(1 + 1 / m)
    return out


def frame_rgb(img, bin_=1):
    """(h, w, 3) float32 RGB of a photo: debayered, black level off, the camera's white balance;
    bin_ 2: 2 x 2 pixels averaged."""
    if img.kind == "rgb":
        rgb = img.data.transpose(1, 2, 0).astype(np.float32)
    else:
        bgr = cv2.cvtColor(img.data, BAYER_CODES[ds.BAYER_GENICAM[img.pattern]])
        rgb = bgr[..., ::-1].astype(np.float32) - img.black
        rgb *= np.array(img.wb, np.float32)
    if bin_ > 1:
        h, w = rgb.shape[:2]
        rgb = cv2.resize(rgb, (w // bin_, h // bin_), interpolation=cv2.INTER_AREA)
    return rgb


def homography(ref_wcs, wcs, shape, bin_=1):
    """3 x 3 matrix taking a pixel of the reference (binned) to the same sky in the frame
    (binned): fitted through both plate solutions on a GRID of points."""
    h, w = shape
    xs, ys = np.meshgrid(np.linspace(0, w * bin_ - 1, GRID[0]), np.linspace(0, h * bin_ - 1, GRID[1]))
    ref = np.column_stack([xs.ravel(), ys.ravel()])
    sky = ref_wcs.all_pix2world(ref, 0)
    pix = wcs.all_world2pix(sky, 0, quiet=True)
    ok = np.all(np.isfinite(pix), axis=1)
    to_bin = (lambda p: (p + 0.5) / bin_ - 0.5)             # full-size pixel -> binned pixel
    H, _ = cv2.findHomography(to_bin(ref[ok]).astype(np.float32), to_bin(pix[ok]).astype(np.float32), 0)
    return H


def warp(rgb, H, shape):
    """The frame on the reference's pixels (H: reference -> frame) and where it has data."""
    h, w = shape
    flags = cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP
    out = cv2.warpPerspective(rgb, H, (w, h), flags=flags, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    valid = cv2.warpPerspective(np.ones(rgb.shape[:2], np.float32), H, (w, h), flags=flags,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0.999
    return out, valid


def solved(path, log=print, **solve):
    """(Image, WCS, header) of a photo FITS, plate-solved first when it has no WCS."""
    from astropy.io import fits
    from astropy.wcs import WCS
    hdr = fits.getheader(path)
    if not hdr.get("PLTSOLVD"):
        ps.solve_photo(path, log=log, **solve)
        hdr = fits.getheader(path)
    img, h, _ = ps.read_photo(path)
    return img, WCS(hdr), h


def stack(paths, out_dir, bin_=1, kappa=3.0, log=print, **solve):
    """Stack the photos (module doc); returns the stack's FITS path and its solution."""
    frames = []
    for p in paths:
        log(f"frame {os.path.basename(p)}")
        img, wcs, h = solved(p, log=lambda m: None, **solve)
        frames.append((p, wcs, h))
    ref_path, ref_wcs, ref_h = frames[0]
    shape = None
    total = weight = None
    mats = []
    for p, wcs, _ in frames:                                   # pass 1: the mean
        rgb = frame_rgb(ps.read_photo(p)[0], bin_)
        if shape is None:
            shape = rgb.shape[:2]
            total, weight = np.zeros_like(rgb), np.zeros(shape, np.float32)
            total2 = np.zeros_like(rgb)
        H = homography(ref_wcs, wcs, shape, bin_)
        mats.append(H)
        img, valid = warp(rgb, H, shape)
        total += img * valid[..., None]
        total2 += img * img * valid[..., None]
        weight += valid
        log(f"  aligned {os.path.basename(p)}: shift {H[0, 2]:+.1f}, {H[1, 2]:+.1f} px, "
            f"rotation {np.degrees(np.arctan2(H[1, 0], H[0, 0])):+.3f} deg")
    n = np.maximum(weight, 1.0)[..., None]
    mean = total / n
    if len(frames) >= KAPPA_MIN_FRAMES and kappa:              # pass 2: kappa-sigma clipped
        # each value against the mean and spread of the OTHER frames (leave one out): with the
        # spread of all n, one outlier can never lie more than (n - 1) / sqrt(n) sigma off (1.8
        # for 5 frames), so a plain kappa = 3 would clip nothing
        # ... and judged as the spread of a few samples is: Student's t, the threshold that
        # leaves out as much of the noise as kappa sigma would of a normal distribution
        s1, s2, w = total, total2, weight[..., None]
        limit = clip_limits(kappa, len(frames))
        out_total, count = np.zeros_like(total), np.zeros_like(total)
        for (p, _, _), H in zip(frames, mats):
            img, valid = warp(frame_rgb(ps.read_photo(p)[0], bin_), H, shape)
            v = valid[..., None]
            m = w - v                                        # the others: n - 1 where this one counts
            mm = np.maximum(m, 2.0)
            mo = (s1 - img * v) / mm
            so = np.sqrt(np.maximum(((s2 - img * img * v) - mm * mo * mo) / (mm - 1.0), 0.0))
            thr = np.take(limit, np.clip(m, 0, len(frames)).astype(np.int32))
            keep = (v & (m < 2)) | (v & (np.abs(img - mo) <= thr * so + 1e-3))
            out_total += np.where(keep, img, 0.0)
            count += keep
        total = out_total
        mean = np.where(count > 0, total / np.maximum(count, 1.0), mean)
        log(f"  {kappa:g}-sigma clipping left out {100 * (1 - count.sum() / (3 * weight.sum())):.2f} % "
            "of the values")
    planes = np.ascontiguousarray(mean.transpose(2, 0, 1)).astype(np.float32)
    starts = [int(datetime.fromisoformat(h["DATE-OBS"]).replace(tzinfo=timezone.utc).timestamp() * 1e9)
              for _, _, h in frames if h.get("DATE-OBS")]
    exp = sum(float(h.get("EXPTIME") or 0.0) for _, _, h in frames)
    keep = ("INSTRUME", "XPIXSZ", "YPIXSZ", "FOCALLEN", "SITELAT", "SITELONG", "SITEELEV", "OBJECT",
            "TRGRA", "TRGDEC", "ISOSPEED", "TIMESYS")
    cards = [("IMAGETYP", "Light")]
    if starts:
        cards += [("DATE-OBS", iso_utc(min(starts)), "UTC, first photo's release"),
                  ("DATE-END", iso_utc(max(starts)), "UTC, last photo's release")]
    cards += [("EXPTIME", round(exp, 3), "s, sum of the photos"), ("NCOMBINE", len(frames)),
              ("STACKBIN", bin_, "pixels binned per axis"), ("KAPPA", kappa if len(frames) >= KAPPA_MIN_FRAMES
                                                              else 0.0, "sigma clipping (0: none)")]
    cards += [(k, ref_h[k], "") for k in keep if ref_h.get(k) not in (None, "")]
    if bin_ > 1 and ref_h.get("XPIXSZ"):
        cards = [(k, (v * bin_ if k in ("XPIXSZ", "YPIXSZ") else v), *c) for k, v, *c in cards]
    cards += [("ROWORDER", "TOP-DOWN"), ("CREATOR", "PyStarLight photo_stack")]
    base = os.path.splitext(os.path.basename(ref_path))[0].replace("_photo", "")
    out = os.path.join(out_dir, f"{base}_stack{len(frames)}.fits")
    write_fits(out, planes, cards)
    log(f"stack: {out}")
    stack_img = ds.Image(planes, kind="rgb", bits=32, white=float(planes.max()))
    if solve.get("scale_arcsec"):
        solve = {**solve, "scale_arcsec": solve["scale_arcsec"] * bin_}
    sol = ps.solve_photo(out, img=stack_img, log=log, **solve)
    return out, sol


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("photos", nargs="+", help="photo FITS or camera files (ARW, CR2, NEF, DNG, JPG)")
    ap.add_argument("-o", "--out-dir", default=".", help="the camera files' FITS and the stack (never "
                    "beside the camera files)")
    ap.add_argument("--bin", type=int, default=1, help="2: 2 x 2 pixels to one (a quarter of the memory)")
    ap.add_argument("--kappa", type=float, default=3.0, help="sigma clipping (0: plain mean)")
    ap.add_argument("--focal", type=float, help="focal length, mm (the scale hint)")
    ap.add_argument("--pixel-um", type=float, help="pixel pitch, um (else a known --camera's)")
    ap.add_argument("--camera", default="", help="camera name, e.g. 'Sony Alpha-A7r II'")
    ap.add_argument("--target", default="", help="target to mark")
    ap.add_argument("--stars", type=int, default=8, help="brightest stars to name")
    ap.add_argument("--timeout", type=float, default=ps.SOLVE_TIMEOUT_S)
    a = ap.parse_args(argv)
    pixel = a.pixel_um or ds.pixel_um_of(a.camera)
    scale = 206.265 * pixel / a.focal if a.focal and pixel else None
    os.makedirs(a.out_dir, exist_ok=True)
    paths = []
    for p in a.photos:
        if not p.lower().endswith((".fits", ".fit", ".fts")):
            p = ps.fits_of_camera_file(p, a.out_dir, a.camera, pixel, a.focal, target=a.target)
        paths.append(p)
    out, sol = stack(paths, a.out_dir, a.bin, a.kappa, n_stars=a.stars, n_marks=a.stars,
                     target=a.target, scale_arcsec=scale, timeout=a.timeout, use_simbad=False)
    c = sol["center"]
    print(f"{os.path.basename(out)}: {c['ra_hms']} {c['dec_dms']}, {sol['scale_arcsec']:.2f}\"/px -> "
          f"{os.path.basename(sol['annotated'])}")


if __name__ == "__main__":
    main()
