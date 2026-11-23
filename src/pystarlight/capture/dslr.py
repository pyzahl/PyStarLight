"""A DSLR (or mirrorless camera) as a second, photo-only camera through libgphoto2, beside the
video camera, which keeps streaming, recording and tracking (the camera's DSLR window,
dslr_gui.py).

NEW CODE. Needs python-gphoto2 (libgphoto2's binding; pip `gphoto2`, Debian `python3-gphoto2`)
and, for RAW files, rawpy (LibRaw); extra `dslr`. Imported only when a DSLR is used.

- Settings are the camera's own (gphoto2 config widgets, names differ by maker): ISO, shutter
  speed (bulb for longer exposures), aperture (a lens with contacts), image format, where the
  files go (the camera's RAM: downloaded only; the card: kept there too).
- capture(): one photo. The shutter speed nearest the exposure asked for, or bulb (pressed and
  released by our timer) beyond the longest timed one; every file the camera made (RAW, RAW +
  JPEG) downloaded. The time stamp is the UTC (GPS clock when anchored) the release was sent:
  the shutter lag (tens to hundreds of ms, the camera's) is not known. Not for event timing.
- decode(): RAW -> the Bayer mosaic as it is (uint16, LibRaw's visible area, black level kept),
  its pattern, black / white level, white balance; JPEG -> RGB planes.
- save(): the camera's own files (CR2, NEF, ARW, JPG ...) and a FITS next to them: the mosaic
  (BAYERPAT, ROWORDER TOP-DOWN, ISO, exposure, site, ...) or the RGB planes.
- DslrThread owns the camera (gphoto2 is not thread-safe): requests go in, results come out as
  attributes; between requests it takes live-view frames when asked. Sequences (repeated
  photos) are a loop of capture requests on it.
"""
from __future__ import annotations

import io
import math
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

# gphoto2 config names per meaning, first found wins (Canon EOS, Nikon, Sony, generic)
NAMES = {"iso": ("iso", "isospeed"),
         "shutter": ("shutterspeed", "shutterspeed2", "exposuretime"),
         "aperture": ("aperture", "f-number", "fnumber"),
         "format": ("imageformat", "imagequality", "capturequality"),
         "target": ("capturetarget",),
         "battery": ("batterylevel", "battery"),
         "model": ("cameramodel", "model"),
         "viewfinder": ("viewfinder",),
         "drive": ("capturemode", "drivemode", "stillcapturemode"),
         "focus": ("focusmode", "focusmode2")}
BULB_PRESS = (("eosremoterelease", "Press Full", "Release Full"), ("bulb", 1, 0))
GP_ERROR_IO_USB_CLAIM = -53
GVFS_HINT = ("the desktop's file manager holds the camera (gvfs): close its window and run "
             "'pkill -f gvfs-gphoto2-volume-monitor; pkill -f gvfsd-gphoto2', or set it to "
             "PC / PTP mode without the card mounted")
MAX_BULB_S = 1800.0
BAYER_GENICAM = {"RGGB": "BayerRG", "BGGR": "BayerBG", "GRBG": "BayerGR", "GBRG": "BayerGB"}


def gp():
    import gphoto2
    return gphoto2


def available():
    """'' when python-gphoto2 imports, else why not (for the window and pystarlight doctor)."""
    try:
        gp()
    except Exception as e:
        return (f"python-gphoto2 not installed ({e}): uv sync --extra dslr "
                "(Debian: apt install python3-gphoto2)")
    return ""


def find_dslrs():
    """[(model, port)] of the cameras gphoto2 sees ([] without python-gphoto2)."""
    try:
        g = gp()
    except Exception:
        return []
    found = g.Camera.autodetect()
    return [(str(found.get_name(i)), str(found.get_value(i))) for i in range(found.count())]


# ---- shutter speeds and ISO as the camera names them ----
def shutter_seconds(text):
    """Seconds of a shutter-speed choice ('1/4000', '0.3', '30', '30"', '2.5s', '1/3s'); None for
    bulb / time or what is no speed."""
    t = str(text).strip().lower().replace(",", ".").rstrip("s\"″' ").strip()
    if t in ("bulb", "b", "time", "t", "") or "bulb" in t:
        return None
    try:
        if "/" in t:
            a, b = t.split("/", 1)
            return float(a) / float(b)
        return float(t)
    except ValueError:
        return None


def iso_value(text):
    """The number of an ISO choice ('1600', 'ISO 1600', '1600 (H)'); None for Auto / others."""
    m = re.search(r"\d+", str(text))
    return int(m.group()) if m and "auto" not in str(text).lower() else None


def nearest(choices, value, key):
    """The choice whose key(choice) is nearest to value on a log scale; None if none has one."""
    best, dist = None, math.inf
    for c in choices:
        k = key(c)
        if k is None or k <= 0:
            continue
        d = abs(math.log(k / value))
        if d < dist:
            best, dist = c, d
    return best


def fnumber_value(text):
    """4.5 of 'f/4.5' / 'F4.5' / '4.5'; None for 'implicit auto', 'f/0' and the like."""
    m = re.search(r"\d+(\.\d+)?", str(text))
    v = float(m.group()) if m else 0.0
    return v if v > 0 else None


def raw_choice(choices):
    """The image format that is RAW alone ('RAW', 'NEF (Raw)'), None if the camera has none."""
    raw = [c for c in choices if "raw" in c.lower() and "+" not in c and "jpeg" not in c.lower()
           and "jpg" not in c.lower() and "fine" not in c.lower()]
    return raw[0] if raw else None


# ---- the photo ----
@dataclass
class Shot:
    """One capture: the camera's files (name, bytes), the UTC ns the release was sent, the
    exposure (s, as set), and the camera's settings at the time."""
    files: list
    start_ns: int
    exposure_s: float
    settings: dict = field(default_factory=dict)
    bulb: bool = False

    def raw_file(self):
        """(name, data) of the RAW file, else of the first file."""
        for name, data in self.files:
            if not name.lower().endswith((".jpg", ".jpeg")):
                return name, data
        return self.files[0]


@dataclass
class Image:
    """A decoded photo: a Bayer mosaic (uint16, pattern 'RGGB' ...; None: not Bayer, e.g.
    X-Trans) or RGB planes (3, h, w) uint8 from a JPEG."""
    data: np.ndarray
    pattern: Optional[str] = None
    black: float = 0.0
    white: float = 65535.0
    wb: tuple = (1.0, 1.0, 1.0)         # R, G, B multipliers (the camera's, as shot)
    bits: int = 16
    kind: str = "raw"                   # "raw" / "rgb"


def decode(name, data):
    """Image of a camera file: RAW through rawpy (LibRaw), JPEG through OpenCV."""
    if name.lower().endswith((".jpg", ".jpeg")):
        import cv2
        bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError(f"{name}: not a JPEG")
        return Image(np.ascontiguousarray(bgr[..., ::-1].transpose(2, 0, 1)), kind="rgb", bits=8,
                     white=255.0)
    try:
        import rawpy
    except ImportError as e:
        raise RuntimeError(f"RAW files need rawpy ({e}): uv sync --extra dslr") from e
    with rawpy.imread(io.BytesIO(bytes(data))) as raw:
        mosaic = np.array(raw.raw_image_visible, dtype=np.uint16)
        desc = raw.color_desc.decode()
        colors = raw.raw_colors_visible
        pattern = None
        if raw.raw_pattern is not None and raw.raw_pattern.shape == (2, 2):
            pattern = "".join(desc[int(colors[r, c])] for r in (0, 1) for c in (0, 1)).replace("g", "G")
            pattern = pattern if pattern in BAYER_GENICAM else None
        black = float(np.mean(raw.black_level_per_channel))
        white = float(raw.white_level)
        wb = tuple(float(v) for v in raw.camera_whitebalance[:3])
    g = wb[1] or 1.0
    return Image(mosaic, pattern, black, white, (wb[0] / g, 1.0, wb[2] / g),
                 max(8, int(math.ceil(math.log2(white + 1)))), "raw")


def preview_rgb(img, width=900):
    """An 8-bit RGB (h, w, 3) quick look of a photo: debayered with the camera's white balance,
    black level off, stretched (99.7 % of the light, square root), at most `width` wide."""
    import cv2

    from .decode import BAYER_CODES
    if img.kind == "rgb":
        rgb = img.data.transpose(1, 2, 0)
        scale = width / rgb.shape[1]
        return cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    m = img.data
    if img.pattern is None:                     # not Bayer: grey
        bgr = np.repeat(m[..., None], 3, axis=2)
    else:
        bgr = cv2.cvtColor(m, BAYER_CODES[BAYER_GENICAM[img.pattern]])
    scale = width / bgr.shape[1]
    if scale < 1:
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    rgb = bgr[..., ::-1].astype(np.float32) - img.black
    rgb *= np.array(img.wb, np.float32)
    lo, hi = np.percentile(rgb[::2, ::2].mean(axis=2), (5.0, 99.7))
    out = np.sqrt(np.clip((rgb - lo) / max(hi - lo, 1e-3), 0.0, 1.0))
    return (out * 255).astype(np.uint8)


def fits_cards(img, shot, header_cards):
    """FITS cards of a DSLR photo: photo.header's (time, site, target, ...) and the camera's."""
    cards = list(header_cards)
    s = shot.settings
    cards = [c for c in cards if c[0] not in ("DATE-OBS", "BITDEPTH")]
    from .stacker import iso_utc
    cards[1:1] = [("DATE-OBS", iso_utc(shot.start_ns), "UTC, release sent (shutter lag not known)"),
                  ("BITDEPTH", int(img.bits))]
    cards += [("ROWORDER", "TOP-DOWN", "row 0 is the image's top")]
    if img.kind == "raw" and img.pattern:
        cards += [("BAYERPAT", img.pattern, "Bayer pattern from the top-left pixel"),
                  ("XBAYROFF", 0), ("YBAYROFF", 0)]
    if img.kind == "raw":
        cards += [("BLKLEVEL", round(img.black, 1), "ADU, black level (not subtracted)"),
                  ("SATURATE", round(img.white, 1), "ADU, white level"),
                  ("WBRED", round(img.wb[0], 4), "camera white balance, R / G"),
                  ("WBBLUE", round(img.wb[2], 4), "camera white balance, B / G")]
    for key, k, comment in (("ISOSPEED", "iso", ""), ("SHUTTER", "shutter", "camera setting"),
                            ("APERTURE", "aperture", "f-number (lens)"), ("IMGFMT", "format", "")):
        if s.get(k) not in (None, ""):
            cards.append((key, str(s[k])[:60], comment))
    if shot.bulb:
        cards.append(("BULB", True, "timed by the computer"))
    cards.append(("CAMFILE", os.path.basename(shot.raw_file()[0])[:60], "the camera's file"))
    return cards


def save_files(shot, out_dir, name):
    """The camera's own files as <name>_<UTC>Z_photo.<ext> in out_dir (-1, -2 ... for a second
    photo in the same second); [paths], in the order of shot.files."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(shot.start_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S")
    base = os.path.join(out_dir, f"{name}_{stamp}Z_photo")
    paths = []
    for cam_name, data in shot.files:
        ext = os.path.splitext(cam_name)[1].lower() or ".raw"
        p, n = base + ext, 1
        while os.path.exists(p):
            p = f"{base}-{n}{ext}"
            n += 1
        with open(p, "wb") as f:
            f.write(bytes(data))
        paths.append(p)
    return paths


def save_fits(shot, paths, header_cards):
    """The FITS of a shot beside its saved files (the RAW one's name, .fits): (path, Image)."""
    from .stacker import write_fits
    raw_name = shot.raw_file()[0]
    k = next(i for i, (n, _) in enumerate(shot.files) if n == raw_name)
    img = decode(*shot.files[k])
    fits_path = os.path.splitext(paths[k])[0] + ".fits"
    write_fits(fits_path, img.data, fits_cards(img, shot, header_cards))
    return fits_path, img


def save(shot, out_dir, name, header_cards):
    """The camera's files and a FITS: <name>_<UTC>Z_photo.<ext> and _photo.fits in out_dir.
    header_cards: photo.header(...) of the main camera's knowledge (site, target, ...).
    Returns (paths, Image)."""
    paths = save_files(shot, out_dir, name)
    fits_path, img = save_fits(shot, paths, header_cards)
    return paths + [fits_path], img


class FitsSaver(threading.Thread):
    """FITS of a program's photos, written in the background (decoding a 42 MP RAW takes about
    a second: the program's cadence must not wait for it). put(shot, paths, cards)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.inbox = queue.Queue()
        self.written, self.errors = 0, []
        self.start()

    def put(self, shot, paths, cards):
        self.inbox.put((shot, paths, cards))

    def run(self):
        while True:
            item = self.inbox.get()
            if item is None:
                return
            try:
                save_fits(*item)
                self.written += 1
            except Exception as e:
                self.errors.append(f"FITS of {os.path.basename(item[1][0])}: {e}")

    def close(self, timeout=30.0):
        self.inbox.put(None)
        self.join(timeout)


class ProgramCamera:
    """A Dslr as schedule.Runner's camera: prepare() sets ISO / aperture / shutter; release()
    takes the photo and keeps its files (files 'fits': a FITS too, in the background; 'card':
    left on the camera's card, where the camera has a card target, else downloaded)."""

    def __init__(self, dslr, out_dir, name, header=lambda shot: []):
        self.dslr, self.out_dir, self.name, self.header = dslr, out_dir, name, header
        self.saver = None
        self.has_card = any("card" in str(c).lower() for c in dslr.choices("target"))
        self.notes = []

    def prepare(self, exposure, shutter):
        self.dslr.prepare(exposure, shutter)

    def release(self, shutter, files):
        if files == "card" and not self.has_card:
            if not self.notes:
                self.notes.append(f"{self.dslr.model}: no card target in gphoto2: files downloaded")
            files = "files"
        if files == "card":
            if "card" not in str(self.dslr.read("target") or "").lower():
                card = next(c for c in self.dslr.choices("target") if "card" in str(c).lower())
                self.dslr.write("target", card)
            shot = self.dslr.capture(shutter, download=False)
            return [], shot.start_ns, shot.settings
        shot = self.dslr.capture(shutter)
        paths = save_files(shot, self.out_dir, self.name)
        if files == "fits":
            if self.saver is None:
                self.saver = FitsSaver()
            self.saver.put(shot, paths, self.header(shot))
        return paths, shot.start_ns, shot.settings

    def close(self):
        if self.saver is not None:
            self.saver.close()


# ---- the camera ----
class Dslr:
    """One camera through gphoto2. Not thread-safe: use it from one thread (DslrThread)."""

    def __init__(self, model="", port="", clock: Callable[[], int] = time.time_ns, g=None):
        self.g = g or gp()
        self.clock = clock
        self.names = {}               # meaning -> the camera's config name
        self.warnings = []            # settings the camera did not take (prepare)
        self.cam = self.g.Camera()
        if port:
            ports = self.g.PortInfoList()
            ports.load()
            self.cam.set_port_info(ports[ports.lookup_path(port)])
            if model:
                abil = self.g.CameraAbilitiesList()
                abil.load()
                self.cam.set_abilities(abil[abil.lookup_model(model)])
        try:
            self.cam.init()
        except self.g.GPhoto2Error as e:
            why = GVFS_HINT if e.code == GP_ERROR_IO_USB_CLAIM else f"{model or 'DSLR'}: {e}"
            raise RuntimeError(why) from e
        # 'Sony Alpha-A7r II (Control)': gphoto2's driver mode in brackets, not the camera's name
        self.model = re.sub(r"\s*\([^)]*\)\s*$", "", model or str(self.read("model") or "")) or "DSLR"

    # ---- config widgets ----
    def _widget(self, meaning):
        """(config or None, widget, name) of a meaning ('iso', 'shutter', ...), None when the
        camera has none. One setting at a time where libgphoto2 can (get / set_single_config):
        no whole tree sent, so a value the camera refuses cannot block the next changes (as a
        whole config sent back with it in would)."""
        single = hasattr(self.cam, "get_single_config")
        cfg = None if single else self.cam.get_config()
        for name in ([self.names[meaning]] if meaning in self.names else NAMES.get(meaning, (meaning,))):
            try:
                w = self.cam.get_single_config(name) if single else cfg.get_child_by_name(name)
            except self.g.GPhoto2Error:
                continue
            self.names[meaning] = name
            return cfg, w, name
        return None

    def read(self, meaning):
        found = self._widget(meaning)
        return None if found is None else found[1].get_value()

    def choices(self, meaning):
        found = self._widget(meaning)
        if found is None:
            return []
        w = found[1]
        try:
            return [w.get_choice(i) for i in range(w.count_choices())]
        except self.g.GPhoto2Error:
            return []

    def write(self, meaning, value):
        found = self._widget(meaning)
        if found is None:
            raise RuntimeError(f"{self.model}: no {meaning} setting")
        cfg, w, name = found
        if w.get_value() == value:
            return
        w.set_value(value)
        if cfg is None:
            self.cam.set_single_config(name, w)
        else:
            self.cam.set_config(cfg)

    def settings(self):
        """{'iso', 'shutter', 'aperture', 'format', 'target', 'battery'} as the camera has them
        now (missing ones: None), with the choices of each in 'choices'."""
        out = {k: self.read(k) for k in ("iso", "shutter", "aperture", "format", "target", "battery")}
        out["choices"] = {k: self.choices(k) for k in ("iso", "shutter", "aperture", "format", "target")}
        return out

    # ---- exposure ----
    def timed_max_s(self):
        return max((v for v in map(shutter_seconds, self.choices("shutter")) if v), default=30.0)

    def has_bulb(self):
        return any(shutter_seconds(c) is None and "bulb" in str(c).lower() for c in self.choices("shutter"))

    def set_exposure(self, seconds):
        """The shutter speed for `seconds`: the nearest timed one, or Bulb beyond the longest
        (then the computer times it). Returns (setting, bulb)."""
        choices = self.choices("shutter")
        if seconds > self.timed_max_s() * 1.05 and self.has_bulb():
            bulb = next(c for c in choices if "bulb" in str(c).lower())
            self.write("shutter", bulb)
            return bulb, True
        best = nearest(choices, seconds, shutter_seconds)
        if best is None:
            raise RuntimeError(f"{self.model}: no shutter speeds to choose from (manual mode, M?)")
        self.write("shutter", best)
        return best, False

    def set_iso(self, iso):
        """The nearest plain ISO (not one of the multi-shot modes some cameras list, e.g. Sony's
        '12800 Multi Frame Noise Reduction+')."""
        choices = self.choices("iso")
        plain = [c for c in choices if re.fullmatch(r"(iso\s*)?\d+", str(c).strip().lower())]
        best = nearest(plain or choices, iso, iso_value)
        if best is not None:
            self.write("iso", best)
        return best

    def set_fnumber(self, f):
        """The nearest aperture ('f/4'); None when the camera offers none (no lens contacts)."""
        best = nearest(self.choices("aperture"), f, fnumber_value)
        if best is not None:
            self.write("aperture", best)
        return best

    def single_drive(self):
        """Single-shot drive when the camera is in a self-timer or bracket drive mode (SoFiMagic:
        a timer drive mode spoils programmed shooting). Returns what was changed, or ''."""
        now = str(self.read("drive") or "")
        if not now or not re.search(r"timer|self|bracket|delay", now, re.I):
            return ""
        single = next((c for c in self.choices("drive") if re.search(r"single", str(c), re.I)), None)
        if single is None:
            return f"drive mode {now!r}: set single shot on the camera"
        self.write("drive", single)
        return f"drive mode {now!r} -> {single!r}"

    def manual_focus(self):
        """Manual focus (SoFiMagic's MF: focused beforehand, the camera must not hunt). Returns
        what was changed, or ''."""
        now = str(self.read("focus") or "")
        if not now or re.fullmatch(r"(manual|mf)", now, re.I):
            return ""
        mf = next((c for c in self.choices("focus") if re.fullmatch(r"(manual|mf)", str(c), re.I)), None)
        if mf is None:
            return f"focus mode {now!r}: set manual focus on the camera / lens"
        self.write("focus", mf)
        return f"focus mode {now!r} -> {mf!r}"

    def prepare(self, exposure, shutter):
        """A program's exposure (schedule.Exposure) set on the camera: ISO, aperture, shutter."""
        if exposure.iso:
            self.set_iso(exposure.iso)
        if exposure.fnumber:
            # a warning, not a failed photo: a lens without contacts (a telescope) has no aperture
            # to set, and some cameras (the A7R II) list f-numbers anyway and ignore the change
            f = exposure.fnumber
            if self.set_fnumber(f) is None:
                self.warn(f"f/{f:g}: the camera offers no aperture (a lens without contacts?)")
            else:
                now = fnumber_value(self.read("aperture"))
                if now is None or abs(now - f) > 0.06 * f:
                    self.warn(f"f/{f:g} not taken: the camera reports {self.read('aperture')} "
                              "(a lens without contacts?)")
        if shutter:
            self.set_exposure(shutter)

    def warn(self, text):
        """A warning for the program's status (each once)."""
        if text not in self.warnings:
            self.warnings.append(text)

    # ---- photos ----
    def capture(self, seconds=None, timeout_s=60.0, download=True):
        """One photo (see the module doc). seconds: the exposure (None: as the camera is set).
        download False: the files stay on the camera's card (released by trigger_capture; Shot
        without files)."""
        bulb = False
        if seconds is not None:
            _, bulb = self.set_exposure(seconds)
        settings = {k: self.read(k) for k in ("iso", "shutter", "aperture", "format", "target")}
        g = self.g
        files = []
        if bulb:
            press = next(((n, on, off) for n, on, off in BULB_PRESS if self._widget(n) is not None), None)
            if press is None:
                raise RuntimeError(f"{self.model}: Bulb without a remote release in gphoto2")
            name, on, off = press
            start = self.clock()
            self.write(name, on)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                time.sleep(min(0.05, max(0.0, end - time.monotonic())))
            self.write(name, off)
            exposure = float(seconds)
        elif not download:
            start = self.clock()
            self.cam.trigger_capture()
            exposure = shutter_seconds(settings.get("shutter")) or float(seconds or 0.0)
        else:
            start = self.clock()
            path = self.cam.capture(g.GP_CAPTURE_IMAGE)
            files.append(self._download(path))
            exposure = shutter_seconds(settings.get("shutter")) or float(seconds or 0.0)
        if not download:                       # on the card: wait for the camera, take no file
            until = time.monotonic() + exposure + (timeout_s if bulb else 5.0)
            while time.monotonic() < until:
                kind, data = self.cam.wait_for_event(200)
                if kind in (g.GP_EVENT_FILE_ADDED, g.GP_EVENT_CAPTURE_COMPLETE):
                    break
            return Shot([], start, exposure, settings, bulb)
        # the files: RAW + JPEG come as more events; bulb: all of them
        until = time.monotonic() + (timeout_s if bulb else 3.0)
        while time.monotonic() < until:
            kind, data = self.cam.wait_for_event(500)
            if kind == g.GP_EVENT_FILE_ADDED:
                files.append(self._download(data))
                until = min(until, time.monotonic() + 1.5)       # one more may follow
            elif kind == g.GP_EVENT_CAPTURE_COMPLETE and files:
                until = min(until, time.monotonic() + 1.0)
            elif kind == g.GP_EVENT_TIMEOUT and files:
                break
        if not files:
            raise RuntimeError(f"{self.model}: no file came from the camera")
        return Shot(files, start, exposure, settings, bulb)

    def _download(self, path):
        f = self.cam.file_get(path.folder, path.name, self.g.GP_FILE_TYPE_NORMAL)
        return path.name, bytes(memoryview(f.get_data_and_size()))

    def preview(self):
        """A live-view frame: RGB (h, w, 3) uint8 (the camera's JPEG)."""
        import cv2
        f = self.cam.capture_preview()
        data = bytes(memoryview(f.get_data_and_size()))
        bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError("live view: no image")
        return np.ascontiguousarray(bgr[..., ::-1])

    def end_preview(self):
        """Live view off (the mirror down), where the camera has the switch."""
        try:
            if self._widget("viewfinder") is not None:
                self.write("viewfinder", 0)
        except Exception:
            pass

    def close(self):
        try:
            self.end_preview()
            self.cam.exit()
        except Exception:
            pass


class DslrThread(threading.Thread):
    """Owns the Dslr; ask(...) queues a request, done in order:
      ("connect", model, port)  ("settings",)  ("set", meaning, value)  ("photo", seconds, save)
      ("program", make)  ("close",)
    save: a function (Shot) -> result (the window saves and decodes there, off the GUI thread);
    make: a function (Dslr) -> schedule.Runner, run here to its end (runner.stop() from any
    thread ends it; live view pauses meanwhile).
    State for the GUI: state ('closed', 'idle', 'busy', 'exposing'), settings, error, last
    (the save function's result), shots (count), live (newest live-view frame, while
    live_view is set), exposing_until (monotonic end of an exposure)."""

    def __init__(self, clock=time.time_ns, factory=Dslr):
        super().__init__(daemon=True)
        self.clock, self.factory = clock, factory
        self.runner = None              # the program running (schedule.Runner)
        self.inbox = queue.Queue()
        self.dslr = None
        self.state, self.error, self.settings, self.last = "closed", "", {}, None
        self.shots = 0
        self.live, self.live_view, self.live_count = None, False, 0
        self.exposing_until = None
        self.running = True
        self.start()

    def ask(self, *request):
        self.inbox.put(request)

    def pending(self):
        return self.inbox.qsize()

    def run(self):
        while self.running:
            try:
                req = self.inbox.get(timeout=0.05)
            except queue.Empty:
                if self.live_view and self.dslr is not None:
                    self._live()
                elif not self.live_view and self.live is not None and self.dslr is not None:
                    self.dslr.end_preview()
                    self.live = None
                continue
            try:
                self.do(*req)
            except Exception as e:
                self.error = str(e)
                print(f"DSLR: {e}")
            finally:
                self.exposing_until = None
                if self.state in ("busy", "exposing", "program"):
                    self.state = "idle" if self.dslr is not None else "closed"

    def do(self, what, *args):
        if what == "connect":
            if self.dslr is not None:
                self.dslr.close()
            self.state, self.dslr = "busy", None
            self.dslr = self.factory(*args, clock=self.clock)
            self.settings, self.error = self.dslr.settings(), ""
        elif what == "close":
            if self.dslr is not None:
                self.dslr.close()
            self.dslr, self.settings, self.live = None, {}, None
            self.state = "closed"
        elif self.dslr is None:
            raise RuntimeError("no DSLR connected")
        elif what == "settings":
            self.settings = self.dslr.settings()
        elif what == "set":
            self.state = "busy"
            self.dslr.write(*args)
            self.settings = self.dslr.settings()
        elif what == "program":
            self.state = "program"
            self.runner = args[0](self.dslr)
            try:
                self.runner.run()
            finally:
                cam = getattr(self.runner, "camera", None)
                if hasattr(cam, "close"):
                    cam.close()
                self.settings = self.dslr.settings()
        elif what == "photo":
            seconds, save = args
            self.state = "exposing"
            self.exposing_until = time.monotonic() + (seconds or 0.0)
            shot = self.dslr.capture(seconds)
            self.shots += 1
            self.state = "busy"                 # downloaded: saving
            self.last = save(shot) if save is not None else shot
            self.error = ""
            self.settings = self.dslr.settings()

    def _live(self):
        try:
            self.live = self.dslr.preview()
            self.live_count += 1
        except Exception as e:
            self.error, self.live_view = f"live view: {e}", False

    def close(self):
        self.ask("close")
        self.running = False
        self.join(timeout=3.0)
        if self.dslr is not None:
            self.dslr.close()
