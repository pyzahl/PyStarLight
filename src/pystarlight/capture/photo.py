"""A single photo from the camera (its own exposure), saved as FITS with what is known of it:
time, exposure, camera, site, and, once the field is plate-solved, the WCS (moved by the drift the
markers have followed since the solve) with the field centre, the target and the named stars.

NEW CODE. numpy only here; the WCS cards are read with astropy when it is there (the full
package), else the photo goes without them. write_fits: capture/stacker.py.
"""
import os
from datetime import datetime, timezone

import numpy as np

from .stacker import iso_utc, write_fits

WCS_PREFIXES = ("WCSAXES", "CTYPE", "CUNIT", "CRVAL", "CRPIX", "CDELT", "CD1_", "CD2_", "PC1_",
                "PC2_", "A_", "B_", "AP_", "BP_", "LONPOLE", "LATPOLE", "RADESYS", "EQUINOX")


def wcs_cards(wcs_path, shift):
    """The solution's WCS cards with CRPIX moved by shift (dx, dy): where the stack's pixels lie
    on the photo (region offsets and tracked drift). [] when the file or astropy is missing."""
    try:
        from astropy.io import fits
        hdr = fits.getheader(wcs_path)
    except Exception:
        return []
    out = []
    for key in hdr:
        if not key or not key.startswith(WCS_PREFIXES):
            continue
        value = hdr[key]
        if key == "CRPIX1":
            value = float(value) + shift[0]
        elif key == "CRPIX2":
            value = float(value) + shift[1]
        out.append((key, value, ""))
    return out


def sky_of(cards, x, y):
    """(ra, dec) deg of pixel (x, y) (0-based) by these WCS cards; None without astropy / WCS."""
    if not cards:
        return None
    try:
        from astropy.io import fits
        from astropy.wcs import WCS
        hdr = fits.Header()
        for key, value, _ in cards:
            hdr[key] = value
        ra, dec = WCS(hdr).all_pix2world(x, y, 0)
        return float(ra), float(dec)
    except Exception:
        return None


def hms_dms(ra, dec):
    h = ra / 15.0
    hh, mm = int(h), int((h % 1) * 60)
    ss = ((h * 60) % 1) * 60
    sign = "-" if dec < 0 else "+"
    d = abs(dec)
    dd, dm = int(d), int((d % 1) * 60)
    ds = ((d * 60) % 1) * 60
    return f"{hh:02d} {mm:02d} {ss:05.2f}", f"{sign}{dd:02d} {dm:02d} {ds:04.1f}"


def header(info, exp_ns, exposure_s, wcs=(), stars=(), target=None, software=""):
    """FITS cards of a photo. info: the camera's header values (as for a stack: instrument,
    depth, gain_db, roi_x, roi_y, pixel_um, focal_mm, latitude, longitude, altitude); exp_ns: UTC
    exposure start; wcs: wcs_cards(); stars: [(name, ra, dec, x, y)] on the photo; target: (name,
    ra, dec) or None."""
    cards = [("IMAGETYP", "Light"), ("DATE-OBS", iso_utc(exp_ns), "UTC, exposure start"),
             ("TIMESYS", "UTC"), ("EXPTIME", round(float(exposure_s), 6), "s"),
             ("INSTRUME", info.get("instrument", "")), ("BITDEPTH", int(info.get("depth", 0))),
             ("ROIX", int(info.get("roi_x", 0)), "sensor x of column 0"),
             ("ROIY", int(info.get("roi_y", 0)), "sensor y of row 0")]
    for key, k, comment in (("XPIXSZ", "pixel_um", "um"), ("YPIXSZ", "pixel_um", "um"),
                            ("FOCALLEN", "focal_mm", "mm"), ("GAINDB", "gain_db", "dB"),
                            ("SITELAT", "latitude", "deg"), ("SITELONG", "longitude", "deg east"),
                            ("SITEELEV", "altitude", "m")):
        if info.get(k):
            cards.append((key, float(info[k]), comment))
    if target is not None:
        cards.append(("OBJECT", str(target[0])[:60]))
        if target[1] is not None:
            cards += [("TRGRA", round(float(target[1]), 7), "deg, target"),
                      ("TRGDEC", round(float(target[2]), 7), "deg, target")]
    cards += [tuple(c) for c in wcs]
    w, h = info.get("width"), info.get("height")
    centre = sky_of(list(wcs), (w - 1) / 2, (h - 1) / 2) if wcs and w and h else None
    if centre is not None:
        ra_s, dec_s = hms_dms(*centre)
        cards += [("RA", round(centre[0], 7), "deg, image centre (plate solve)"),
                  ("DEC", round(centre[1], 7), "deg, image centre (plate solve)"),
                  ("OBJCTRA", ra_s), ("OBJCTDEC", dec_s)]
    for i, (name, ra, dec, x, y) in enumerate(stars[:99], 1):
        cards.append((f"STAR{i}", f"{name} {ra:.6f} {dec:+.6f} {x:.1f} {y:.1f}"[:68],
                      "name ra dec x y" if i == 1 else ""))
    cards.append(("CREATOR", f"PyStarLight camera {software}".strip()))
    return cards


def save(out_dir, name, image, cards, exp_ns):
    """<name>_<UTC>Z_photo.fits in out_dir; returns the path."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(exp_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = os.path.join(out_dir, f"{name}_{stamp}Z_photo.fits")
    write_fits(path, np.asarray(image, dtype=np.float32), cards)
    return path
