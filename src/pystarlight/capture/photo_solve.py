"""Plate solve of a DSLR photo: the same solve and markers as the video camera's stack, written
into the photo's FITS (the DSLR window's Photo, or afterwards from the command line).

    python -m pystarlight.capture.photo_solve PHOTO.fits [...] [--target NAME] [--stars 5]

NEW CODE. The solve (pystarlight.wcs.solve_stack: local solve-field or nova, a hint then the
whole sky) runs on a luminance image of the photo at full size: the Bayer mosaic debayered (or
a JPEG's RGB), so the solution is in the FITS's own pixels. The markers (capture/marks.py: the
brightest stars named from the catalogues, the target T and verify star V) are the camera
window's. Written:
- into the FITS (its data, the uint16 mosaic, unchanged): the WCS (RA / Dec <-> pixel, SIP
  distortion), the field centre (RA, DEC, OBJCTRA, OBJCTDEC), plate scale and orientation,
  STAR1 .. (name ra dec x y), the target's pixel (TRGX, TRGY), PLTSOLVD;
- beside it: <photo>.wcs.fit (astrometry.net's solution), <photo>.solve.json (the result and
  the markers), <photo>_annotated.png (the photo in colour with the markers and N / E).
"""
from __future__ import annotations

import argparse
import json
import math
import os

import cv2
import numpy as np

from . import dslr as ds
from .marks import solution_marks
from .stacker import write_fits

STRUCTURAL = ("SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "NAXIS3", "EXTEND", "BZERO", "BSCALE")
SOLVED_KEYS = ("RA", "DEC", "OBJCTRA", "OBJCTDEC", "PLTSOLVD", "PIXSCALE", "NORTHDEG", "MIRRORED",
               "TRGX", "TRGY", "FIELDW", "FIELDH")
COLORS = {"star": (64, 196, 255), "target": (255, 64, 129), "verify": (255, 215, 64)}   # RGB, as gui.py
ANNOTATED_W = 1800


def luminance(img):
    """The photo as one 2-D float32 image at full size, black level off: a Bayer mosaic
    debayered and summed, RGB planes summed (the solve and the target check run on it)."""
    if img.kind == "rgb":
        return img.data.astype(np.float32).sum(axis=0)
    m = img.data
    if img.pattern is None:
        return m.astype(np.float32) - img.black
    from .decode import BAYER_CODES
    bgr = cv2.cvtColor(m, BAYER_CODES[ds.BAYER_GENICAM[img.pattern]])
    return bgr.astype(np.float32).sum(axis=2) - 3 * img.black


def read_photo(path):
    """(Image, header dict) of a photo FITS written by dslr.save (BAYERPAT, BLKLEVEL, ...)."""
    from astropy.io import fits
    with fits.open(path) as hdul:
        data, hdr = np.asarray(hdul[0].data), hdul[0].header
        cards = [(k, hdr[k], hdr.comments[k]) for k in hdr if k and k not in ("COMMENT", "HISTORY")]
    h = {k: v for k, v, _ in cards}
    if data.ndim == 3:                       # RGB planes: a JPEG's (8 bit) or a stack's (float)
        if np.issubdtype(data.dtype, np.floating):
            data = data.astype(np.float32)
            return ds.Image(data, kind="rgb", bits=32, white=float(np.nanmax(data))), h, cards
        return ds.Image(data.astype(np.uint8), kind="rgb", bits=8, white=255.0), h, cards
    wb = (float(h.get("WBRED", 1.0)), 1.0, float(h.get("WBBLUE", 1.0)))
    return (ds.Image(data.astype(np.uint16), h.get("BAYERPAT"), float(h.get("BLKLEVEL", 0)),
                     float(h.get("SATURATE", 65535)), wb, int(h.get("BITDEPTH", 16)), "raw"), h, cards)


SOLVE_TIMEOUT_S = 120.0     # the whole-sky solve of a photo: given up after this


def solve_photo(path, n_stars=5, n_marks=5, target="", verify="", use_simbad=True, center=None,
                radius_deg=3.0, scale_arcsec=None, log=print, cancel=None, img=None,
                timeout=SOLVE_TIMEOUT_S):
    """Solve a photo FITS, name its stars, find the target; write all of it (module doc).
    img: the decoded photo when at hand (else read from the FITS). Returns the solution dict
    with 'marks', 'notes', 'annotated'."""
    from pystarlight.wcs.solve_stack import solve_stack

    from . import localconfig
    photo_img, hdr, cards = read_photo(path)
    img = photo_img if img is None else img
    gray = luminance(img)
    stem = os.path.splitext(path)[0]
    log(f"plate solving {os.path.basename(path)} ({gray.shape[1]} x {gray.shape[0]})")
    if not scale_arcsec and not (hdr.get("FOCALLEN") and hdr.get("XPIXSZ")):
        log("no plate-scale hint ([dslr] focal_mm and pixel_um): a blind solve, given up after "
            f"{timeout:g} s")
    sol = solve_stack(gray, hdr, n_stars, workdir=os.path.dirname(path) or ".", dest=stem + ".wcs.fit",
                      scale_arcsec=scale_arcsec, use_simbad=use_simbad, site=localconfig.section("observer"),
                      log=log, cancel=cancel, center=center, radius_deg=radius_deg, timeout=timeout)
    sol["stack"] = path
    marks, notes, found = solution_marks(sol, n_marks, target, verify, log=log, image=gray)
    sol["marks"], sol["notes"] = marks, notes
    write_solution(path, img, cards, sol)
    with open(stem + ".solve.json", "w") as f:
        json.dump(sol, f, indent=1, default=_json)
    sol["annotated"] = annotate(img, sol, stem + "_annotated.png")
    return sol


def solution_cards(sol):
    """The FITS cards a solution adds: WCS, centre, scale, orientation, named stars, target."""
    from .photo import hms_dms, wcs_cards
    c = sol["center"]
    ra_s, dec_s = hms_dms(c["ra"], c["dec"])
    out = list(wcs_cards(sol["wcs"], (0.0, 0.0)))
    out += [("RA", round(c["ra"], 7), "deg, image centre (plate solve)"),
            ("DEC", round(c["dec"], 7), "deg, image centre (plate solve)"),
            ("OBJCTRA", ra_s), ("OBJCTDEC", dec_s), ("PLTSOLVD", True, "astrometry.net"),
            ("PIXSCALE", round(sol["scale_arcsec"], 4), "arcsec/px"),
            ("NORTHDEG", round(sol["north_deg"], 2), "deg, north from up"),
            ("MIRRORED", bool(sol["mirrored"])),
            ("FIELDW", round(sol["field_deg"][0], 4), "deg"),
            ("FIELDH", round(sol["field_deg"][1], 4), "deg")]
    stars = [m for m in sol.get("marks", []) if m["kind"] == "star"]
    for i, m in enumerate(stars[:99], 1):
        out.append((f"STAR{i}", f"{m['name']} {m['ra']:.6f} {m['dec']:+.6f} {m['x']:.1f} {m['y']:.1f}"[:68],
                    "name ra dec x y" if i == 1 else ""))
    t = next((m for m in sol.get("marks", []) if m["kind"] == "target"), None)
    if t is not None:
        out += [("TRGX", round(t["x"], 2), "px, target (0-based)"), ("TRGY", round(t["y"], 2), "px")]
    return out


def write_solution(path, img, cards, sol):
    """The FITS again, the same data, its header with the solution (old solution cards out)."""
    from .photo import WCS_PREFIXES
    keep = [c for c in cards if c[0] not in STRUCTURAL and not c[0].startswith(WCS_PREFIXES)
            and c[0] not in SOLVED_KEYS and not (c[0].startswith("STAR") and c[0][4:].isdigit())]
    end = next((i for i, c in enumerate(keep) if c[0] == "CREATOR"), len(keep))
    keep[end:end] = solution_cards(sol)
    write_fits(path, img.data, keep)


def annotate(img, sol, out_path, width=ANNOTATED_W):
    """The photo in colour (dslr.preview_rgb) with the camera window's markers (four ticks around
    each star, the label on a dark box up and to the right), N / E and the centre; a PNG."""
    rgb = np.ascontiguousarray(ds.preview_rgb(img, width))
    H, W = img.data.shape[-2:]
    s = rgb.shape[1] / W
    for m in sol.get("marks", []):
        color = COLORS.get(m["kind"], (255, 255, 255))
        x, y = m["x"] * s, m["y"] * s
        r0 = max(9.0, m["r"] * s + 4.0)              # the gap: the star stays visible
        r1 = r0 + 11.0
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cv2.line(rgb, (int(x + dx * r0), int(y + dy * r0)), (int(x + dx * r1), int(y + dy * r1)),
                     color, 2, cv2.LINE_AA)
        label_box(rgb, m["label"], int(x + 0.7 * r1), int(y - 0.7 * r1), color)
    for label, ux, uy in compass(sol) or []:
        ox, oy = rgb.shape[1] - 80, rgb.shape[0] - 80
        tip = (int(ox + 45 * ux), int(oy + 45 * uy))
        cv2.arrowedLine(rgb, (ox, oy), tip, (255, 167, 38), 2, cv2.LINE_AA, tipLength=0.25)
        label_box(rgb, label, int(ox + 58 * ux) - 6, int(oy + 58 * uy) + 8, (255, 167, 38))
    c = sol["center"]
    label_box(rgb, f"{c['ra_hms']}  {c['dec_dms']}   {sol['scale_arcsec']:.2f}\"/px   "
              f"{sol['field_deg'][0] * 60:.1f}' x {sol['field_deg'][1] * 60:.1f}'", 10, rgb.shape[0] - 12,
              (235, 235, 235))
    cv2.imwrite(out_path, rgb[..., ::-1])
    return out_path


def label_box(rgb, text, x, y, color, scale=0.55):
    """Text with its bottom-left at (x, y) on a dark, half transparent box."""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x = int(min(max(x, 0), rgb.shape[1] - tw - 6))
    y = int(min(max(y, th + 6), rgb.shape[0] - base - 2))
    box = rgb[y - th - 4:y + base + 2, x - 3:x + tw + 3]
    box[:] = (box * 0.35).astype(rgb.dtype)
    cv2.putText(rgb, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def compass(sol):
    """[('N', ux, uy), ('E', ux, uy)] on the image (y down) from the WCS at the centre (as the
    camera window's compass_of)."""
    try:
        from astropy.io import fits
        from astropy.wcs import WCS
        w = WCS(fits.getheader(sol["wcs"]))
        ra, dec = sol["center"]["ra"], sol["center"]["dec"]
        (x0,), (y0,) = w.wcs_world2pix([ra], [dec], 0)
        out = []
        east = ra + 1 / 60 / math.cos(math.radians(dec))
        for label, (r, d) in (("N", (ra, dec + 1 / 60)), ("E", (east, dec))):
            (x,), (y,) = w.wcs_world2pix([r], [d], 0)
            n = float(np.hypot(x - x0, y - y0)) or 1.0
            out.append((label, float(x - x0) / n, float(y - y0) / n))
        return out
    except Exception:
        return None


def _json(v):
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return str(v)


def fits_of_camera_file(path, out_dir, camera="", pixel_um=None, focal_mm=None, utc=None,
                        exposure_s=0.0, target=""):
    """A camera's file (ARW, CR2, NEF, DNG, JPG: e.g. from an archive) as the DSLR window's photo:
    the file copied and its FITS (dslr.save) in out_dir, named <file stem>_<UTC>Z_photo. Its
    time: utc, else the file's modification time (the camera's clock is not to be trusted)."""
    from datetime import datetime, timezone

    from . import photo
    with open(path, "rb") as f:
        data = f.read()
    when = (datetime.fromisoformat(utc.replace("Z", "+00:00")) if utc else
            datetime.fromtimestamp(os.path.getmtime(path), timezone.utc))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    shot = ds.Shot([(os.path.basename(path), data)], int(when.timestamp() * 1e9), float(exposure_s), {})
    info = {"instrument": camera or "DSLR", "pixel_um": pixel_um or 0.0, "focal_mm": focal_mm or 0.0}
    cards = photo.header(info, shot.start_ns, shot.exposure_s,
                         target=(target, None, None) if target else None)
    paths, _ = ds.save(shot, out_dir, os.path.splitext(os.path.basename(path))[0], cards)
    return paths[-1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Plate-solve DSLR photos (their FITS): WCS, named stars and "
                                 "the target into the FITS, an annotated PNG beside it.")
    ap.add_argument("photos", nargs="+", help="photo FITS (as the DSLR window writes them), or the camera's "
                    "files (ARW, CR2, NEF, DNG, JPG): their FITS is written to --out-dir first")
    ap.add_argument("-o", "--out-dir", default=".", help="where camera files' FITS go (never beside them)")
    ap.add_argument("--focal", type=float, help="focal length, mm (with the pixel pitch: the scale hint)")
    ap.add_argument("--pixel-um", type=float, help="pixel pitch, um (else a known --camera's)")
    ap.add_argument("--camera", default="", help="camera name for the FITS, e.g. 'Sony Alpha-A7r II'")
    ap.add_argument("--utc", help="exposure start, ISO UTC (camera files; default: the file's time)")
    ap.add_argument("--exposure", type=float, default=0.0, help="exposure, s (camera files)")
    ap.add_argument("--target", default="", help="target to mark (name or RA Dec)")
    ap.add_argument("--verify", default="", help="a second star to check the target against")
    ap.add_argument("--stars", type=int, default=5, help="brightest stars to name (default 5)")
    ap.add_argument("--scale", type=float, help="plate scale hint, arcsec/px (else FOCALLEN / XPIXSZ)")
    ap.add_argument("--no-simbad", action="store_true")
    ap.add_argument("--timeout", type=float, default=SOLVE_TIMEOUT_S, help="seconds for a blind solve")
    a = ap.parse_args(argv)
    scale = a.scale
    pixel = a.pixel_um or ds.pixel_um_of(a.camera)
    if scale is None and a.focal and pixel:
        scale = 206.265 * pixel / a.focal
    for p in a.photos:
        try:
            if not p.lower().endswith((".fits", ".fit", ".fts")):
                p = fits_of_camera_file(p, a.out_dir, a.camera, pixel, a.focal, a.utc, a.exposure,
                                        a.target)
                print(f"FITS {p}")
            sol = solve_photo(p, a.stars, a.stars, a.target, a.verify, not a.no_simbad,
                              scale_arcsec=scale, timeout=a.timeout)
            c = sol["center"]
            print(f"{os.path.basename(p)}: {c['ra_hms']} {c['dec_dms']}, {sol['scale_arcsec']:.2f}\"/px, "
                  f"{len(sol['marks'])} markers -> {os.path.basename(sol['annotated'])}")
            for n in sol["notes"]:
                print(f"  {n}")
        except Exception as e:
            print(f"{os.path.basename(p)}: not solved ({e})")


if __name__ == "__main__":
    main()
