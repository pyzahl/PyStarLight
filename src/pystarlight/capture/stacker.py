"""Live stacking: average a given number of frames, aligned on the stars, into a FITS file
for the plate solve (see pystarlight.wcs.solve_stack).

The grabber offers frames while a stack is running; the stacker takes the next one whenever it
is free (so on a fast stream it takes every k-th frame, at the pace it can align them). Each
frame is aligned to the first by phase correlation of "star maps": 2x downsampled, median-
filtered (hot pixels), minus a blurred background (sky glow, tree silhouettes), clipped
above the noise, under a Hann window (edges count less). The sky drifts ~15"/s x cos(dec)
without tracking, i.e. ~0.7 px/s at 24 mm focal length on a 2.4 um pixel; a frame whose
correlation is too weak keeps the last good shift. Pixels are a weighted mean (borders that
drifted out are averaged over the frames that cover them).

Pure numpy / OpenCV (no astropy): runs with the Pi's system Python too; write_fits is a
minimal FITS writer (float32 image + header cards).
"""
import os
import queue
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np

DOWN = 2                    # downsampling of the star maps
MIN_RESPONSE = 0.02         # phase-correlation peak below this: alignment not trusted


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------
def star_map(img, down=DOWN):
    """High-passed, noise-clipped, downsampled image: little else than the stars."""
    h, w = img.shape
    small = cv2.resize(np.asarray(img, np.float32), (w // down, h // down),
                       interpolation=cv2.INTER_AREA)
    small = cv2.medianBlur(small, 3)
    hp = small - cv2.GaussianBlur(small, (0, 0), 8)
    med = float(np.median(hp))
    noise = 1.4826 * float(np.median(np.abs(hp - med))) or 1.0
    return np.clip(hp - med - 3 * noise, 0, None).astype(np.float32)


def measure_shift(ref_map, cur_map, window, down=DOWN):
    """(dx, dy, response): the current frame shows the sky moved by (dx, dy) pixels
    (full resolution) from the reference."""
    (dx, dy), response = cv2.phaseCorrelate(ref_map, cur_map, window)
    return dx * down, dy * down, float(response)


def shift_image(img, dx, dy):
    """img moved by (-dx, -dy): undoes a measured shift. Returns (image, weight)."""
    h, w = img.shape
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    out = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
    weight = cv2.warpAffine(np.ones((h, w), np.float32), m, (w, h), flags=cv2.INTER_LINEAR,
                            borderValue=0)
    return out, weight


def display_stretch(img, max_value):
    """Stack for the screen: background (median) to black, the brightest 0.05 % to white,
    as integers 0..max_value (the GUI's gamma LUT then applies)."""
    sample = img[::4, ::4]
    lo, hi = np.percentile(sample, (50, 99.95))
    if hi - lo < 1.0:                   # flat (e.g. saturated) image: show it as it is
        return np.clip(img, 0, max_value).astype(np.uint16)
    scale = max_value / (hi - lo)
    return np.clip((img - lo) * scale, 0, max_value).astype(np.uint16)


# ---------------------------------------------------------------------------
# FITS
# ---------------------------------------------------------------------------
def _card(key, value, comment=""):
    if isinstance(value, bool):
        v = f"{'T' if value else 'F':>20}"
    elif isinstance(value, (int, np.integer)):
        v = f"{int(value):>20d}"
    elif isinstance(value, (float, np.floating)):
        v = f"{float(value):>20.10G}"
    else:
        v = "'" + str(value).replace("'", "''").ljust(8) + "'"
    card = f"{key:<8}= {v}" + (f" / {comment}" if comment else "")
    return card[:80].ljust(80)


def write_fits(path, image, header=()):
    """2-D float32 FITS (BITPIX -32). header: [(key, value, comment)], keys <= 8 chars.
    Array row 0 is FITS row 1 (as astropy writes and reads it)."""
    img = np.asarray(image, dtype=">f4")
    h, w = img.shape
    cards = [_card("SIMPLE", True, "conforms to FITS standard"), _card("BITPIX", -32),
             _card("NAXIS", 2), _card("NAXIS1", w), _card("NAXIS2", h)]
    cards += [_card(*c) for c in header]
    cards.append("END".ljust(80))
    head = "".join(cards).encode("ascii", "replace")
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(head + b" " * (-len(head) % 2880))
        data = img.tobytes()
        f.write(data + b"\0" * (-len(data) % 2880))
    os.replace(tmp, path)


def iso_utc(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23]


# ---------------------------------------------------------------------------
# Stacker thread
# ---------------------------------------------------------------------------
class LiveStacker(threading.Thread):
    """States: idle -> stacking -> done (stack saved) / idle (cancelled).

    decode(raw) -> 2-D image; header_info() -> dict for the FITS header (instrument,
    pixel_um, focal_mm, gain_db, latitude, longitude, altitude, roi_x, roi_y), asked for when
    a stack starts."""

    def __init__(self, decode, out_dir, name, header_info=dict):
        super().__init__(daemon=True)
        self.decode = decode
        self.out_dir, self.name = out_dir, name
        self.header_info = header_info
        self.inbox = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.state = "idle"
        self.target = 0                 # frames wanted
        self.align = True
        self.count = 0
        self.shift = (0.0, 0.0)         # of the newest frame (px)
        self.unaligned = 0              # frames that kept the previous shift
        self.version = 0                # +1 per frame added (the GUI redraws)
        self.path = None                # FITS file of the finished stack
        self.error = None
        self.running = True
        self._reset()
        self.start()

    def _reset(self):
        self.sum = self.weight = None
        self.ref_map = self.window = None
        self.first_ns = self.last_ns = None
        self.exposure_s = 0.0
        self.meta = {}

    # ---- control (GUI thread) ----
    def begin(self, frames, align=True):
        with self.lock:
            self._reset()
            self.target, self.align = int(frames), align
            self.count = self.unaligned = 0
            self.shift = (0.0, 0.0)
            self.path = self.error = None
            self.meta = dict(self.header_info())
            self.state = "stacking"

    def cancel(self):
        with self.lock:
            if self.state == "stacking":
                self.state = "idle"

    def preview(self):
        """Mean of the frames so far (float32), None before the first."""
        with self.lock:
            if self.sum is None:
                return None
            return self.sum / np.maximum(self.weight, 1e-3)

    # ---- feeding (grabber thread) ----
    def wants(self):
        return self.state == "stacking" and self.inbox.empty()

    def put(self, raw, exposure_ns, info=None):
        try:
            self.inbox.put_nowait((raw, exposure_ns, info or {}))
        except queue.Full:
            pass

    # ---- worker ----
    def run(self):
        while self.running:
            try:
                raw, exposure_ns, info = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.state != "stacking":
                continue
            try:
                self._add(np.asarray(self.decode(raw), np.float32), exposure_ns, info)
            except Exception as e:          # e.g. the region changed under a running stack
                self.error = f"stacking failed: {e}"
                self.state = "idle"

    def _add(self, img, exposure_ns, info):
        dx = dy = 0.0
        if self.align:
            cur = star_map(img)
            if self.ref_map is None:
                self.ref_map = cur
                self.window = cv2.createHanningWindow(cur.shape[::-1], cv2.CV_32F)
            else:
                dx, dy, resp = measure_shift(self.ref_map, cur, self.window)
                if resp < MIN_RESPONSE:
                    (dx, dy), self.unaligned = self.shift, self.unaligned + 1
        aligned, weight = shift_image(img, dx, dy) if (dx or dy) else (img, None)
        with self.lock:
            if self.state != "stacking":
                return
            if self.sum is None:
                self.sum = np.zeros_like(img)
                self.weight = np.zeros_like(img)
            self.sum += aligned * (weight if weight is not None else 1.0)
            self.weight += weight if weight is not None else 1.0
            self.shift = (dx, dy)
            self.count += 1
            self.version += 1
            if exposure_ns is not None:         # None: the frame has no time stamp
                if self.first_ns is None:
                    self.first_ns = exposure_ns
                self.last_ns = exposure_ns
            self.exposure_s += info.get("exposure_ns", 0) / 1e9
            done = self.count >= self.target
        if done:
            self._finish()

    def _finish(self):
        img = self.preview()
        m = self.meta
        t0 = self.first_ns if self.first_ns is not None else time.time_ns()
        stamp = datetime.fromtimestamp(t0 / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S")
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"{self.name}_{stamp}Z_stack{self.count}.fits")
        hdr = [("IMAGETYP", "Light")]
        if self.first_ns is not None:
            hdr += [("DATE-OBS", iso_utc(self.first_ns), "UTC, first frame start"),
                    ("DATE-END", iso_utc(self.last_ns), "UTC, last frame start")]
        hdr += [("EXPTIME", round(self.exposure_s, 6), "s, sum of the frames"),
               ("NCOMBINE", self.count, "frames averaged"),
               ("ALIGNED", bool(self.align), "frames aligned on the stars"),
               ("DRIFTX", round(self.shift[0], 2), "px, last frame vs first"),
               ("DRIFTY", round(self.shift[1], 2), "px, last frame vs first"),
               ("INSTRUME", m.get("instrument", "")), ("BITDEPTH", int(m.get("depth", 0))),
               ("ROIX", int(m.get("roi_x", 0)), "sensor x of column 0"),
               ("ROIY", int(m.get("roi_y", 0)), "sensor y of row 0")]
        for key, k, comment in (("XPIXSZ", "pixel_um", "um"), ("YPIXSZ", "pixel_um", "um"),
                                ("FOCALLEN", "focal_mm", "mm"), ("GAINDB", "gain_db", "dB"),
                                ("SITELAT", "latitude", "deg, GPS"),
                                ("SITELONG", "longitude", "deg east, GPS"),
                                ("SITEELEV", "altitude", "m, GPS")):
            if m.get(k):
                hdr.append((key, float(m[k]), comment))
        hdr.append(("CREATOR", "PyStarLight capture"))
        write_fits(path, img, hdr)
        with self.lock:
            self.path = path
            self.state = "done"
        print(f"Stacked {self.count} frames ({self.exposure_s:.1f} s"
              + (f", drift {self.shift[0]:+.1f}, {self.shift[1]:+.1f} px" if self.align else "")
              + (f", {self.unaligned} not aligned" if self.unaligned else "") + f") to {path}")

    def close(self):
        self.cancel()
        self.running = False
