"""Markers of a plate solution: the brightest stars named from the catalogues, the target and the
verify star (T, V), as the camera window shows them on its stack, and the DSLR window on its
photos (moved here from gui.py: one place for both).

NEW CODE, numpy + OpenCV; astropy and pystarlight.wcs (catalogue names, target look-up) are
imported when used. Network look-ups: call from a worker thread.
"""
import cv2
import numpy as np


def detect_text(img, scale_arcsec, x, y, half=15):
    """Is there a star at (x, y) of the image (2-D)? Offset and significance of the brightest
    smoothed peak within `half` px."""
    img = np.asarray(img, dtype=np.float32)
    H, W = img.shape[:2]
    x0, y0 = int(round(x)) - 3 * half, int(round(y)) - 3 * half
    if x0 < 0 or y0 < 0 or x0 + 6 * half >= W or y0 + 6 * half >= H:
        return "too near the edge to check in the stack"
    box = cv2.GaussianBlur(img[y0:y0 + 6 * half + 1, x0:x0 + 6 * half + 1], (7, 7), 2.0)
    sky = float(np.median(box))
    noise = 1.4826 * float(np.median(np.abs(box - sky))) + 1e-6
    inner = box[2 * half:4 * half + 1, 2 * half:4 * half + 1]
    j, i = np.unravel_index(int(np.argmax(inner)), inner.shape)
    snr = (inner[j, i] - sky) / noise
    dx, dy = x0 + 2 * half + i - x, y0 + 2 * half + j - y
    sep = float(np.hypot(dx, dy)) * scale_arcsec
    if snr < 5:
        return f"NO star detected at the target in the stack (best {snr:.1f} sigma)"
    return (f"detected in the stack: {snr:.0f} sigma, {np.hypot(dx, dy):.1f} px "
            f"({sep:.1f}\") from the catalogue position")


def solution_marks(sol, n, target="", verify="", log=print, image=None, offset=(0, 0)):
    """Markers of a solution (solve_stack's dict, its 'wcs' file): the n brightest stars with
    catalogue names and magnitudes, the target (T; a name to look up, or {'name', 'ra', 'dec'}
    known already: the Moon, a planet) and the verify star (V) looked up, where
    they fall on the image. image: the solved image (2-D) for the target's detection check
    (default: the FITS sol['stack']); offset: added to the positions the notes print (the
    camera's region: sensor pixels). Returns (marks, notes, found): marks [{x, y, r, label,
    kind ('star' / 'target' / 'verify'), ...}] in the solution's pixels; notes [text];
    found {kind: the look-up}."""
    from astropy.io import fits
    from astropy.wcs import WCS

    from pystarlight.wcs.catnames import catalog_name, decimal_year, resolve_target
    from pystarlight.wcs.catnames import verify as same_star
    marks, notes, found = [], [], {}
    epoch = decimal_year(sol["time"]) if sol.get("time") else None
    radius = max(5.0, 3.0 * sol["scale_arcsec"])
    for i, s in enumerate(sol.get("stars", [])[:n], 1):
        log(f"naming star {i}")
        hit = catalog_name(s["ra"], s["dec"], radius, epoch, log=log)
        name = hit["name"] if hit else (s.get("name") or s.get("ra_hms", ""))
        mag = f"  {hit['mag']:.1f}" if hit and np.isfinite(hit["mag"]) else ""
        marks.append({"x": s["x"], "y": s["y"], "r": max(4.0, 1.5 * (s.get("fwhm") or 3)),
                      "label": f"{i} {name}{mag}", "kind": "star",
                      "name": name, "ra": s["ra"], "dec": s["dec"]})
    wcs = WCS(fits.getheader(sol["wcs"]))
    for kind, query, tag in (("target", target, "T"), ("verify", verify, "V")):
        if not query:
            continue
        if isinstance(query, dict):
            t = {"ids": {}, "mag": float("nan"), "band": "", **query}
        else:
            log(f"looking up {query}")
            try:
                t = resolve_target(query, epoch, log=log)
            except Exception as e:
                notes.append(f"{kind}: {e}")
                continue
        found[kind] = t
        mag = f"  {t['band']} {t['mag']:.1f}" if np.isfinite(t["mag"]) else ""
        c = sol["center"]
        off = float(np.degrees(np.arccos(np.clip(
            np.sin(np.radians(c["dec"])) * np.sin(np.radians(t["dec"]))
            + np.cos(np.radians(c["dec"])) * np.cos(np.radians(t["dec"]))
            * np.cos(np.radians(t["ra"] - c["ra"])), -1, 1))))
        if off > 2 * max(sol.get("field_deg") or [1.0]):
            # far off the field: the distortion terms of the solution diverge there (and took
            # all the markers with them); just say where it is
            notes.append(f"{tag} {t['name']}{mag}: {off:.1f} deg from the field centre "
                         "(OUTSIDE the stacked field)")
            continue
        x, y = (float(v) for v in wcs.all_world2pix(t["ra"], t["dec"], 0, quiet=True))
        marks.append({"x": x, "y": y, "r": 8.0 if kind == "target" else 14.0,
                      "label": f"{tag} {t['name']}{mag}", "kind": kind, "ra": t["ra"], "dec": t["dec"],
                      "name": t["name"]})
        ids = ", ".join(v for k, v in t["ids"].items() if v != t["name"])
        notes.append(f"{tag} {t['name']}" + (f" = {ids}" if ids else "")
                     + f"{mag} at x {x + offset[0]:.0f}, y {y + offset[1]:.0f}"
                     + ("" if 0 <= x < sol["width"] and 0 <= y < sol["height"]
                        else "  (OUTSIDE the stacked field)"))
        if kind == "target":
            try:
                img = image if image is not None else fits.getdata(sol["stack"])
                notes.append("   " + detect_text(img, sol["scale_arcsec"], x, y))
            except Exception as e:
                notes.append(f"   not checked in the stack ({e})")
    if len(found) == 2:
        v = same_star(found["target"], found["verify"])
        notes.append(v["text"])
        if v["same"] and marks and marks[-1]["kind"] == "verify":   # one marker, the verify label under it
            vm = marks.pop()
            marks[-1]["label"] += "  ✓ " + vm["label"][2:].split("  ")[0]
    return marks, notes, found
