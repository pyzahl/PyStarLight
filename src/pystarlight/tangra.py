"""Tangra 3 light-curve files (.lc) and a compressed copy of the movie.

NEW CODE. A Tangra .lc file holds, for every measured frame and object (at most 4), the
35 x 35 pixel box around it as recorded, plus the measurement Tangra shows. Tangra opens it
without the video and can re-measure the light curve from those boxes (other aperture,
background method, PSF photometry: "Re-process" in its light-curve window). So the .lc written
here carries PyStarLight's measurement and lets anybody redo the photometry in Tangra.

    write_lc(lc, reader, 'run.lc')       # lc from analyze() / analyze_global(); reader: open_movie
    info = read_lc('run.lc')             # our reader of the same layout (tests, inspection)
    write_movie(reader, 'run.mp4')       # H.264, 8 bit, asinh stretch: to look at, not to measure
    write_movie(reader, 'run.mkv', lossless=True)   # FFV1, the recorded 16-bit pixels kept

The layout follows Tangra 3's LCFile.cs (github.com/hpavlov/tangra3, MPL 2.0): a raw deflate
stream of .NET BinaryWriter values, file version 4, header 8, frame timing 2, measurement 9,
footer 14 (reduction context 9, tracked object 4). Pixel boxes are stored x-major (Tangra's
uint[x, y]) and centred on the rounded position, pixels off the frame are 0. Times are
mid-exposure (lc.time_shift_s, as the figures), as .NET ticks (100 ns since 0001-01-01).

Choices Tangra needs and PyStarLight does not have: the target is the "occulted star",
references "guiding stars", check stars "comparison stars" (Asteroidal reduction, aperture
photometry, average background, which is what PyMovie's robust mean is closest to); Tangra's
reading is the aperture sum and its background the background per pixel times the aperture
area, so reading - background is our signal. A recording Tangra cannot open (RAVF, a FITS
folder) is labelled FITS: Tangra then looks for no video and shows the stored averaged frame.
Tangra renumbers the frames from the first one on loading, so the rows must be consecutive.
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np

BOX = 35                                  # Tangra's MATRIX_SIZE
MAX_OBJECTS = 4
TICKS_EPOCH = datetime(1, 1, 1)
DATETIME_MAX_TICKS = 3155378975999999999  # DateTime.MaxValue
LC_FILE_VERSION, HEADER_VERSION, TIMING_VERSION, MEASUREMENT_VERSION = 4, 8, 2, 9
FOOTER_VERSION, CONTEXT_VERSION, OBJECT_VERSION = 14, 9, 4

# Tangra enums (values as in its source)
TRACKING = {'target': 2, 'reference': 0, 'check': 1}       # OccultedStar, GuidingStar, ComparisonStar
ROLE_ORDER = {'target': 0, 'reference': 1, 'check': 2}
REDUCTION_ASTEROIDAL, SUBTYPE_OCCULTATION = 2, 1
TIMING_USER, TIMING_EMBEDDED = 0, 2
BACKGROUND_AVERAGE = 0
FLAG_TRACKED, FLAG_PSF_FAILED, FLAG_DISAPPEARED, FLAG_OFFSCREEN = 1, 4, 6, 0x100
GRAYSCALE = 3


# --------------------------------------------------------------------------- .NET BinaryWriter

class _Writer:
    def __init__(self):
        self.buf = io.BytesIO()

    def _p(self, fmt, *v):
        self.buf.write(struct.pack('<' + fmt, *v))

    def i16(self, v): self._p('h', int(v))
    def i32(self, v): self._p('i', int(v))
    def u32(self, v): self._p('I', int(v))
    def i64(self, v): self._p('q', int(v))
    def u8(self, v): self._p('B', int(v))
    def bool(self, v): self._p('?', bool(v))
    def f32(self, v): self._p('f', float(v))
    def f64(self, v): self._p('d', float(v))

    def str(self, s: str):
        """7-bit encoded byte count, then UTF-8 (BinaryWriter.Write(string))."""
        b = (s or '').encode('utf-8')
        n = len(b)
        while True:
            byte, n = n & 0x7F, n >> 7
            self.u8(byte | (0x80 if n else 0))
            if not n:
                break
        self.buf.write(b)


class _Reader:
    def __init__(self, data: bytes):
        self.b, self.pos = data, 0

    def _u(self, fmt):
        size = struct.calcsize('<' + fmt)
        if self.pos + size > len(self.b):
            raise EOFError('the .lc file ends early')
        v = struct.unpack_from('<' + fmt, self.b, self.pos)
        self.pos += size
        return v[0] if len(v) == 1 else v

    def i16(self): return self._u('h')
    def i32(self): return self._u('i')
    def u32(self): return self._u('I')
    def i64(self): return self._u('q')
    def u8(self): return self._u('B')
    def bool(self): return self._u('?')
    def f32(self): return self._u('f')
    def f64(self): return self._u('d')

    def bytes(self, n):
        v = self.b[self.pos:self.pos + n]
        self.pos += n
        return v

    def str(self):
        n = shift = 0
        while True:
            byte = self.u8()
            n |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return self.bytes(n).decode('utf-8')


def ticks(t: datetime) -> int:
    """.NET DateTime ticks (100 ns since 0001-01-01) of a naive UTC datetime."""
    d = t - TICKS_EPOCH
    return (d.days * 86400 + d.seconds) * 10_000_000 + d.microseconds * 10


def from_ticks(n: int) -> datetime:
    return TICKS_EPOCH + timedelta(microseconds=n // 10)


# --------------------------------------------------------------------------- what goes in

def pick_objects(lc, max_objects: int = MAX_OBJECTS) -> list[str]:
    """The apertures for Tangra (at most 4): target first, then references, then check stars,
    in the run's order; sky apertures never."""
    names = [n for n in lc.names if lc.roles.get(n, '') in ROLE_ORDER or
             (not lc.roles and 'xc' in lc.columns[n])]
    names = [n for n in names if 'xc' in lc.columns[n]]
    names.sort(key=lambda n: ROLE_ORDER.get(lc.roles.get(n, ''), 3))
    return names[:max_objects]


def source_info(path, bit_pix: int) -> str:
    """Tangra's SourceInfo 'Video (<FORMAT>.<bits>)'. Formats Tangra opens keep their name, the
    others are labelled FITS so Tangra looks for no video file (it shows the averaged frame)."""
    from .station import movie_format
    fmt = movie_format(path)
    if fmt in ('SER', 'AVI'):
        return f'Video ({fmt}.{bit_pix})'
    if fmt in ('ADV', 'AAV'):
        return f'Video ({fmt}2.{bit_pix})'
    return f'Video (FITS.{bit_pix}::{fmt or "unknown"} via PyStarLight)'


def frame_times(lc) -> list[Optional[datetime]]:
    """Mid-exposure UTC per row (lc.time_shift_s added, midnight unwrapped), None when unknown."""
    from .plot import seconds
    t = seconds(lc)
    date = next((d for d in lc.date if d), None)
    if date is None:
        return [None] * len(t)
    day = datetime.strptime(date[:10], '%Y-%m-%d')
    return [day + timedelta(seconds=float(s)) if np.isfinite(s) else None for s in t]


def _radius(col: dict, i: int, fallback: float) -> float:
    area = col.get('area', col.get('mskpx'))
    if area is not None and np.isfinite(area[i]) and area[i] != 0:
        return math.sqrt(abs(float(area[i])) / math.pi)
    return fallback


def _not_found(col: dict, i: int) -> bool:
    """Global mode: the star's own fit failed or it dimmed (left out of the tracking)."""
    if 'used' not in col:
        return False
    return not col['used'][i] > 0 or bool(col.get('dimmed') is not None and col['dimmed'][i] > 0)


def _area(col: dict, i: int, radius: float) -> float:
    """Pixels in the aperture (the run's own count; pi r^2 without one)."""
    area = col.get('area', col.get('mskpx'))
    if area is not None and np.isfinite(area[i]) and area[i] != 0:
        return abs(float(area[i]))
    return math.pi * radius * radius


def _box(img: np.ndarray, cx: int, cy: int) -> np.ndarray:
    """35 x 35 pixels centred on (cx, cy), [y, x], zero outside the frame (GetMeasurableAreaPixels)."""
    h, w = img.shape
    half = BOX // 2
    out = np.zeros((BOX, BOX), dtype=np.uint32)
    y0, x0 = cy - half, cx - half
    ys, xs = max(0, y0), max(0, x0)
    ye, xe = min(h, y0 + BOX), min(w, x0 + BOX)
    if ys < ye and xs < xe:
        out[ys - y0:ye - y0, xs - x0:xe - x0] = img[ys:ye, xs:xe]
    return out


def _display_bytes(image: np.ndarray) -> bytes:
    from .plot import zscale
    lo, hi = zscale(image)
    scaled = np.clip((image.astype(float) - lo) / max(hi - lo, 1e-9) * 255, 0, 255)
    return np.round(scaled).astype(np.uint8).tobytes()


def _bitpix(dtype, full_scale: Optional[float]) -> tuple[int, int]:
    """(Tangra BitPix 8/12/14/16, max pixel value)."""
    if np.dtype(dtype).itemsize == 1:
        return 8, 255
    top = int(full_scale) if full_scale else 65535
    return 16, max(1, min(top, 65535))


# --------------------------------------------------------------------------- writer

def write_lc(lc, reader, path, *, names: Optional[list[str]] = None, average_frames: int = 64,
             camera: str = '', fwhm: Optional[float] = None, full_scale: Optional[float] = None,
             progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Write lc (a LightCurves) as a Tangra 3 .lc file, the pixel boxes read again from reader
    (the movie lc was measured on). Returns a summary dict (objects, frames, path).

    names: the apertures (default pick_objects: target, references, check stars, at most 4);
    average_frames: the averaged frame Tangra shows is the mean of the first so many frames;
    full_scale: the recording's maximum pixel value (16-bit data; default 65535)."""
    names = names or pick_objects(lc)
    if not names:
        raise ValueError('no star apertures to write')
    if len(names) > MAX_OBJECTS:
        raise ValueError(f'Tangra holds at most {MAX_OBJECTS} objects, got {len(names)}')
    cols = [lc.columns[n] for n in names]
    frames = np.asarray(cols[0]['frame'], dtype=int)
    if frames.size == 0:
        raise ValueError('empty light curve')
    if np.any(np.diff(frames) != 1):
        raise ValueError('Tangra needs consecutive frames (the run skipped some)')
    n = frames.size
    if fwhm is None and 'psf_fwhm' in lc.frame_info:          # global mode: the run's seeing
        psf = np.asarray(lc.frame_info['psf_fwhm'], dtype=float)
        fwhm = float(np.nanmedian(psf)) if np.isfinite(psf).any() else None
    times = frame_times(lc)
    timed = all(t is not None for t in times)
    dt = np.diff([ticks(t) for t in times]) / 1e7 if timed and n > 1 else np.array([])
    interval = float(np.median(dt)) if dt.size else None
    fps = 1.0 / interval if interval and interval > 0 else 25.0
    first = reader.read(int(frames[0])).image
    bit_pix, max_pixel = _bitpix(first.dtype, full_scale)
    radius_default = float(np.nanmedian(lc.frame_info['radius'])) if 'radius' in lc.frame_info else 3.0
    radii = []
    for c in cols:
        r = [_radius(c, i, radius_default) for i in range(n)]
        radii.append(float(np.nanmedian(r)) if r else radius_default)

    body = _Writer()
    acc, n_acc = np.zeros(first.shape, dtype=float), 0
    for i, fno in enumerate(frames):
        if cancel and cancel():
            raise RuntimeError('cancelled')
        img = first if i == 0 else reader.read(int(fno)).image
        if n_acc < average_frames:
            acc += img
            n_acc += 1
        if timed:
            body.i32(TIMING_VERSION)
            body.i64(ticks(times[i]))
            body.i32(round((times[i + 1] - times[i]).total_seconds() * 1000) if i + 1 < n
                     else round((interval or 0) * 1000))
            body.i32(0)
            body.i32(0)                        # no OCR pixels
            body.bool(False)                   # no NTP / Windows times
        for k, (name, c) in enumerate(zip(names, cols)):
            x, y = float(c['xc'][i]), float(c['yc'][i])
            ok = np.isfinite(x) and np.isfinite(y)
            px, py = (int(round(x)), int(round(y))) if ok else (0, 0)
            box = _box(img, px, py) if ok else np.zeros((BOX, BOX), dtype=np.uint32)
            total = float(c.get('sum', c.get('appsum'))[i])
            bkg = float(c['bkavg'][i])
            r = _radius(c, i, radii[k])
            good = ok and np.isfinite(total) and np.isfinite(bkg)
            flags = FLAG_TRACKED
            if not ok:
                flags = FLAG_OFFSCREEN | FLAG_PSF_FAILED
            elif not good:
                flags = FLAG_PSF_FAILED
            elif lc.roles.get(name) == 'target' and _not_found(c, i):
                flags = FLAG_DISAPPEARED           # placed by the field and drift, not found itself
            body.i32(MEASUREMENT_VERSION)
            body.u32(fno)
            body.u8(k)
            body.u32(min(max(round(total), 0), 2 ** 32 - 1) if good else 0)
            body.u32(min(max(round(bkg * _area(c, i, r)), 0), 2 ** 32 - 1) if good else 0)
            body.buf.write(box.T.astype('<u4').tobytes())   # Tangra's [x, y], x-major
            body.u8(0)                                       # byte flags unused: DWORD flags
            body.f32(x if ok else 0.0)
            body.f32(y if ok else 0.0)
            body.i32(px)
            body.i32(py)
            body.i64(0)                                      # no OSD time stamp
            body.u32(flags)
            body.f32(x if ok else 0.0)
            body.f32(y if ok else 0.0)
            body.f32(r)
            body.i32(0)                                      # no FITS file name
        if progress:
            progress(i, n)

    w = _Writer()
    w.i16(LC_FILE_VERSION)
    _header(w, lc, reader, names, frames, times if timed else None, fps, radii, bit_pix)
    w.u8(len(names))
    w.i32(n)
    w.buf.write(body.buf.getvalue())
    _footer(w, lc, names, cols, acc / max(n_acc, 1), bit_pix, max_pixel, radii, camera, fwhm,
            timed)
    comp = zlib.compressobj(6, zlib.DEFLATED, -15)          # raw deflate (DeflateStream)
    data = comp.compress(w.buf.getvalue()) + comp.flush()
    Path(path).write_bytes(data)
    return {'path': str(path), 'objects': names, 'frames': n, 'timed': timed,
            'bit_pix': bit_pix, 'fps': fps}


def _header(w, lc, reader, names, frames, times, fps, radii, bit_pix):
    w.i32(HEADER_VERSION)
    w.u32(frames[0])
    w.u32(frames[-1])
    w.u32(frames.size)
    w.u8(len(names))
    w.i32(REDUCTION_ASTEROIDAL)
    src = getattr(reader, 'path', '') or lc.source
    w.str(str(src))
    w.str(source_info(src, bit_pix))
    w.i32(0)
    w.i32(len(reader))
    w.i64(ticks(times[0]) if times else DATETIME_MAX_TICKS)
    w.i64(ticks(times[-1]) if times else 0)
    w.f64(fps)
    w.u32(1)                                   # every frame measured
    w.i32(BACKGROUND_AVERAGE)
    w.i32(0)                                   # no digital filter
    for r in radii:
        w.i32(11)                              # PSF fit matrix (Tangra's default)
        w.f32(r)
        w.bool(False)
    w.f32(2.0)                                 # position tolerance
    w.i32(frames[0])
    w.i32(frames[-1])
    w.u8(TIMING_EMBEDDED if times else TIMING_USER)
    for _ in names:
        w.i32(-1)                              # no PSF groups
    for _ in names:
        w.f64(float('nan'))                    # reference magnitudes
    w.f64(float('nan'))                        # reference intensity
    w.i32(0)                                   # SerUseTimeStamp.None
    w.i32(1)                                   # SerTimeStampReference.MidFrame


def _footer(w, lc, names, cols, average, bit_pix, max_pixel, radii, camera, fwhm, timed):
    h, wd = average.shape
    w.i32(FOOTER_VERSION)
    w.i32(wd)
    w.i32(h)
    w.i32(bit_pix)
    disp = _display_bytes(average)
    w.i32(len(disp))
    w.buf.write(disp)
    w.str('')                                  # Tangra config: its defaults
    # LightCurveReductionContext v9
    w.u8(CONTEXT_VERSION)
    w.i32(0)                                   # DigitalFilter NoFilter
    w.i32(BACKGROUND_AVERAGE)
    w.i32(REDUCTION_ASTEROIDAL)
    w.i32(0)                                   # AperturePhotometry
    w.bool(True)                               # FullDisappearance
    w.bool(False)
    w.bool(False)
    w.bool(False)
    w.i32(0)                                   # NoIntegration
    w.i32(0)                                   # Mean
    w.i32(1)
    w.bool(False)                              # colour
    w.i32(GRAYSCALE)
    for _ in range(4):
        w.i32(0)                               # OSD frame
    w.bool(False)
    w.bool(False)
    w.bool(False)
    w.u8(0)
    w.u8(255)
    w.i32(0)
    w.i32(0)
    w.bool(False)                              # drift through
    w.i32(bit_pix)
    w.u32(max_pixel)
    w.str('')                                  # display converter: default for bit_pix
    w.bool(timed)                              # embedded time stamps
    w.str('')                                  # tracker
    w.i32(0)                                   # PSF quadrature NumericalInAperture
    w.i32(SUBTYPE_OCCULTATION)
    # tracked objects v4
    w.i32(len(names))
    for name, c, r in zip(names, cols, radii):
        ok = np.isfinite(c['xc']) & np.isfinite(c['yc'])
        j = int(np.argmax(ok)) if ok.any() else 0
        x0, y0 = (float(c['xc'][j]), float(c['yc'][j])) if ok.any() else (0.0, 0.0)
        w.u8(OBJECT_VERSION)
        w.i32(TRACKING.get(lc.roles.get(name, ''), 1))
        w.bool(True)
        w.f32(r)
        w.i32(11)
        w.f32(x0)
        w.f32(y0)
        w.i32(round(x0))
        w.i32(round(y0))
        w.bool(False)                          # no Gaussian
        w.f32(2.0)
        w.bool(False)
        for _ in range(4):
            w.f32(0.0)                         # aperture matrix x0 / y0, dx / dy
        w.i32(0)                               # auto stars in area
        w.f32(fwhm if fwhm else float('nan'))
        w.bool(False)                          # fixed aperture
        w.i32(-1)                              # group
    w.f32(fwhm if fwhm else float('nan'))      # refined average FWHM
    w.str('')                                  # timestamp OCR
    w.i32(0)                                   # thumbprints
    w.i32(0)                                   # instrumental delays
    w.str('')
    w.str(camera or '')
    w.i32(-1)                                  # AAV frame integration
    w.str('')
    w.i32(bit_pix)
    w.u32(max_pixel if bit_pix == 16 else 0)
    w.i32(-1)                                  # AAV NTP error
    w.i32(0)                                   # AAV stacked frame rate
    w.f32(float('nan'))
    w.bool(False)
    w.i32(-1)                                  # FITS dynamic range
    w.i32(-1)
    w.i32(0)                                   # RotateNoneFlipNone
    w.bool(False)                              # no digital timing corrections
    w.f64(float('nan'))                        # acquisition delay
    w.f64(float('nan'))                        # reference time offset


# --------------------------------------------------------------------------- reader

def read_lc(path, pixels: bool = True) -> dict:
    """Read a Tangra 3 .lc file (current versions: file 4, header 8, measurement 9, footer 14,
    as Tangra 3.7 writes). Returns a dict: header, timing (list), data ({object: list of
    measurement dicts, 'pixels' as [y, x]}), footer. pixels=False skips the boxes."""
    raw = zlib.decompress(Path(path).read_bytes(), -15)
    r = _Reader(raw)
    version = r.i16()
    if version > LC_FILE_VERSION:
        raise ValueError(f'.lc file version {version}: newer than this reader')
    hdr = _read_header(r)
    n_obj = r.u8()
    data = {k: [] for k in range(n_obj)}
    timing = []
    if n_obj:
        n = r.i32()
        for _ in range(n):
            if hdr['timing_type'] != TIMING_USER:
                timing.append(_read_timing(r))
            for _ in range(n_obj):
                m = _read_measurement(r, pixels)
                data[m['target']].append(m)
    footer = _read_footer(r)
    return {'version': version, 'header': hdr, 'timing': timing, 'data': data, 'footer': footer}


def _read_header(r):
    v = r.i32()
    if v != HEADER_VERSION:
        raise ValueError(f'header version {v} not supported (only {HEADER_VERSION})')
    h = {'version': v, 'min_frame': r.u32(), 'max_frame': r.u32(), 'measured_frames': r.u32()}
    n = h['objects'] = r.u8()
    h['reduction_type'] = r.i32()
    h['path'], h['source_info'] = r.str(), r.str()
    h['first_frame'], h['count_frames'] = r.i32(), r.i32()
    h['first_time_ticks'], h['second_time_ticks'] = r.i64(), r.i64()
    h['fps'], h['interval'] = r.f64(), r.u32()
    h['background_type'], h['filter_type'] = r.i32(), r.i32()
    h['psf_sizes'], h['apertures'], h['fixed'] = [], [], []
    for _ in range(n):
        h['psf_sizes'].append(r.i32())
        h['apertures'].append(r.f32())
        h['fixed'].append(r.bool())
    h['position_tolerance'] = r.f32()
    h['first_timed_frame'], h['last_timed_frame'] = r.i32(), r.i32()
    h['timing_type'] = r.u8()
    h['groups'] = [r.i32() for _ in range(n)]
    h['reference_mags'] = [r.f64() for _ in range(n)]
    h['reference_intensity'] = r.f64()
    h['ser_timing'], h['ser_reference'] = r.i32(), r.i32()
    return h


def _read_timing(r):
    v = r.i32()
    t = {'version': v, 'mid_time': from_ticks(r.i64()), 'duration_ms': r.i32()}
    wd, ht = r.i32(), r.i32()
    r.bytes(wd * ht)
    if v > 1 and r.bool():
        r.i64(), r.i64(), r.i64()
    return t


def _read_measurement(r, pixels):
    v = r.i32()
    if v != MEASUREMENT_VERSION:
        raise ValueError(f'measurement version {v} not supported (only {MEASUREMENT_VERSION})')
    m = {'frame': r.u32(), 'target': r.u8(), 'reading': r.u32(), 'background': r.u32()}
    box = r.bytes(BOX * BOX * 4)
    if pixels:
        m['pixels'] = np.frombuffer(box, dtype='<u4').reshape(BOX, BOX).T.copy()   # -> [y, x]
    m['flags_byte'] = r.u8()
    m['x0'], m['y0'] = r.f32(), r.f32()
    m['pixel_x0'], m['pixel_y0'] = r.i32(), r.i32()
    m['osd_ticks'] = r.i64()
    m['flags'] = r.u32()
    m['aperture_x'], m['aperture_y'], m['aperture'] = r.f32(), r.f32(), r.f32()
    if r.i32() > 0:
        m['file'] = r.str()
    return m


def _read_footer(r):
    v = r.i32()
    if v != FOOTER_VERSION:
        raise ValueError(f'footer version {v} not supported (only {FOOTER_VERSION})')
    f = {'version': v, 'width': r.i32(), 'height': r.i32(), 'bpp': r.i32()}
    f['averaged_frame'] = np.frombuffer(r.bytes(r.i32()), dtype=np.uint8)
    if f['averaged_frame'].size == f['width'] * f['height']:
        f['averaged_frame'] = f['averaged_frame'].reshape(f['height'], f['width'])
    f['config'] = r.str()
    cv = r.u8()
    ctx = f['context'] = {'version': cv}
    for k in ('filter', 'noise', 'reduction_type', 'reduction_method'):
        ctx[k] = r.i32()
    for k in ('full_disappearance', 'flickering', 'wind', 'all_faint'):
        ctx[k] = r.bool()
    ctx['integrating'], ctx['pixel_integration'], ctx['n_integrate'] = r.i32(), r.i32(), r.i32()
    if cv > 1:
        ctx['colour'], ctx['channel'] = r.bool(), r.i32()
        ctx['osd'] = (r.i32(), r.i32(), r.i32(), r.i32())
    if cv > 2:
        ctx['stretch'] = (r.bool(), r.bool(), r.bool(), r.u8(), r.u8(), r.i32(), r.i32())
    if cv > 3:
        ctx['drift_through'] = r.bool()
    if cv > 4:
        ctx['bit_pix'], ctx['max_pixel'], ctx['converter'] = r.i32(), r.u32(), r.str()
    if cv > 5:
        ctx['embedded_times'] = r.bool()
    if cv > 6:
        ctx['tracker'] = r.str()
    if cv > 7:
        ctx['psf_quadrature'] = r.i32()
    if cv > 8:
        ctx['subtype'] = r.i32()
    f['objects'] = [_read_object(r) for _ in range(r.i32())]
    f['fwhm'] = r.f32()
    f['ocr'] = r.str()
    f['thumbprints'] = {r.i32(): r.i64() for _ in range(r.i32())}
    f['delays'] = {r.i32(): r.f32() for _ in range(r.i32())}
    f['delay_name'], f['camera'] = r.str(), r.str()
    f['aav_integration'], f['aav_format'] = r.i32(), r.str()
    f['data_bit_pix'], f['norm_value'] = r.i32(), r.u32()
    f['ntp_error'], f['stacked_rate'], f['ntp_sigma'], f['ntp_direct'] = r.i32(), r.i32(), r.f32(), r.bool()
    f['fits_range'] = (r.i32(), r.i32())
    f['rotate_flip'] = r.i32()
    f['timing_corrections'] = r.bool()
    f['acquisition_delay_ms'], f['reference_offset_ms'] = r.f64(), r.f64()
    return f


def _read_psf(r):
    v = r.u8()
    p = {'version': v, 'background': r.f64(), 'max': r.f64(), 'x0': r.f64(), 'y0': r.f64(),
         'r0': r.f64(), 'matrix': r.i32(), 'half_width': r.i32(), 'saturation': r.u32(),
         'x_center': r.i32(), 'y_center': r.i32()}
    r.bytes(p['matrix'] * p['matrix'] * 8)
    if v > 1:
        r.i32(), r.i32(), r.i32()
    if v > 2:
        r.f64(), r.f64()
    if v > 3:
        r.bool()
    return p


def _read_object(r):
    v = r.u8()
    o = {'version': v, 'tracking': r.i32(), 'measure': r.bool(), 'aperture': r.f32(),
         'psf_matrix': r.i32(), 'x': r.f32(), 'y': r.f32(), 'field_x': r.i32(), 'field_y': r.i32()}
    o['gaussian'] = _read_psf(r) if r.bool() else None
    o['tolerance'], o['weak'] = r.f32(), r.bool()
    o['matrix'] = (r.f32(), r.f32(), r.f32(), r.f32())
    o['auto_stars'] = [_read_psf(r) for _ in range(r.i32())]
    if v > 1:
        o['fwhm'] = r.f32()
    if v > 2:
        o['fixed'] = r.bool()
    if v > 3:
        o['group'] = r.i32()
    return o


# --------------------------------------------------------------------------- movie

def binned(image: np.ndarray, factor: int) -> np.ndarray:
    """Mean of factor x factor pixel blocks (edge rows / columns that do not fill one dropped)."""
    if factor <= 1:
        return image
    h, w = (image.shape[0] // factor) * factor, (image.shape[1] // factor) * factor
    return image[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def stretch_of(reader, start: int = 0, stop: Optional[int] = None, n: int = 16,
               factor: int = 1) -> tuple[float, float, float]:
    """One display stretch for the whole movie (an occultation shows as a dimming, not as a
    re-scaled frame): (black, soft, white) for stretch(). From the median of n frames spread
    over the range: black 2 sigma below the sky, soft 3 sigma (sky noise of the frames, binned
    as the movie is), white the brightest star."""
    stop = len(reader) - 1 if stop is None else stop
    idx = np.unique(np.linspace(start, stop, min(n, stop - start + 1)).round().astype(int))
    frames = [binned(reader.read(int(i)).image.astype(np.float32), factor) for i in idx]
    med = np.median(frames, axis=0)
    sky = float(np.median(med))
    sigma = 1.4826 * float(np.median(np.abs(frames[0] - sky))) or 1.0
    black = sky - 2 * sigma
    white = max(float(np.percentile(med, 99.99)), black + 100 * sigma)
    return black, 3 * sigma, white


def stretch(image: np.ndarray, black: float, soft: float, white: float) -> np.ndarray:
    """asinh stretch to 8 bit: linear near the sky (faint stars show), logarithmic for bright
    stars (not burnt out), the sky noise compressed into a few grey levels (smaller files)."""
    x = np.maximum(image.astype(np.float32) - black, 0) / soft
    out = np.arcsinh(x) * (255.0 / math.asinh((white - black) / soft))
    return np.clip(out, 0, 255).astype(np.uint8)


def write_movie(reader, path, start: int = 0, stop: Optional[int] = None, *, fps: Optional[float] = None,
                lossless: bool = False, crf: int = 23, bin_factor: int = 2, denoise: bool = True,
                limits: Optional[tuple[float, float, float]] = None, overlay: bool = True,
                target: bool = False, curve: bool = False, analysis: Optional[dict] = None,
                progress: Optional[Callable[[int, int], None]] = None,
                cancel: Optional[Callable[[], bool]] = None) -> dict:
    """The movie (frames start..stop) as a compressed file through FFmpeg.

    Default: H.264 (.mp4), 8 bit, one asinh stretch for all frames (stretch_of), spatial
    denoising (FFmpeg hqdn3d, none in time: the D and R stay sharp), frame number and time stamp
    burnt in, binned 2 x 2 (bin_factor); plays everywhere, but lossy: to look at, not to
    measure. Sky noise is what costs: an ASTRID recording (1456 x 1088) takes about 0.1 MB per
    frame binned, 0.45 MB with bin_factor=1.
    target / curve (as the player shows them, from the analysis next to the recording,
    <movie>.pystarlight.csv + .event.json, capture.playdata.load_analysis; or analysis= its
    result): the target's open cross and name on each frame, and below the frame its light
    curve (target / references, step-fit levels, D / R) with a time cursor at the frame shown.
    Without an analysis they are left out (the result's 'note' says so).
    lossless=True: FFV1 (.mkv) of the recorded pixels (16 bit gray kept, never binned, no
    overlay), about a quarter of the raw size, readable by FFmpeg / OpenCV.
    fps: playback rate (default the recording's)."""
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise RuntimeError('ffmpeg not found (sudo apt install ffmpeg)')
    stop = len(reader) - 1 if stop is None else min(stop, len(reader) - 1)
    first = reader.read(start)
    factor = 1 if lossless else max(1, int(bin_factor))
    h, w = binned(first.image, factor).shape
    fps = fps or _movie_fps(reader, start, stop) or 25.0
    sixteen = lossless and first.image.dtype.itemsize > 1
    note = ''
    if (target or curve) and not lossless and analysis is None:
        analysis = analysis_of(getattr(reader, 'path', ''))
        if analysis is None:
            note = 'no analysis next to the recording: no target mark / light curve'
    if lossless or analysis is None:
        target = curve = False
    if target and not analysis.get('xy'):
        target, note = False, 'the analysis has no target positions: no target mark'
    panel = _curve_panel(analysis, w, start, stop, factor) if curve else None
    colour = target or curve
    H = h + (panel.shape[0] if panel is not None else 0)
    if lossless:
        pix = 'gray16le' if sixteen else 'gray'
        codec = ['-c:v', 'ffv1', '-level', '3', '-g', '1', '-slices', '4', '-slicecrc', '1']
        vf = []
    else:
        pix = 'bgr24' if colour else 'gray'
        codec = ['-c:v', 'libx264', '-preset', 'slow', '-crf', str(int(crf)), '-pix_fmt', 'yuv420p',
                 '-movflags', '+faststart']
        pad = 'pad=ceil(iw/2)*2:ceil(ih/2)*2'
        if denoise and panel is not None:           # the frame only: the panel stays crisp
            vf = ['-filter_complex', f'[0]split[a][b];[a]crop={w}:{h}:0:0,hqdn3d=6:6:0:0[im];'
                                     f'[b][im]overlay=0:0,{pad}']
        else:
            vf = ['-vf', ('hqdn3d=6:6:0:0,' if denoise else '') + pad]
        if limits is None:
            limits = stretch_of(reader, start, stop, factor=factor)
    cmd = [ffmpeg, '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', pix, '-s', f'{w}x{H}',
           '-r', f'{fps:.6f}', '-i', '-', *vf, *codec, str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    n = 0
    try:
        for i in range(start, stop + 1):
            if cancel and cancel():
                raise RuntimeError('cancelled')
            fr = first if i == start else reader.read(i)
            if lossless:
                out = fr.image.astype('<u2' if sixteen else np.uint8, copy=False)
            else:
                out = stretch(binned(fr.image, factor), *limits)
                if colour:
                    import cv2
                    out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
                    if target:
                        _mark_target(out, analysis, fr.index, factor)
                if overlay:
                    _stamp(out, f'{fr.index}  {fr.date or ""} {fr.time or ""}'.strip())
                if panel is not None:
                    out = np.vstack([out, _cursor(panel, fr.index, start, stop)])
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            n += 1
            if progress:
                progress(i, stop)
        proc.stdin.close()
        err = proc.stderr.read().decode(errors='replace')
        if proc.wait() != 0:
            raise RuntimeError(f'ffmpeg failed: {err.strip()}')
    except BaseException:
        proc.kill()
        proc.wait()
        Path(path).unlink(missing_ok=True)
        raise
    return {'path': str(path), 'frames': n, 'fps': fps, 'lossless': lossless, 'bin': factor,
            'target': target, 'curve': panel is not None, 'note': note,
            'bytes': Path(path).stat().st_size}


# player colours (capture/player.py, imageview.py), as BGR
TARGET_BGR = (0x7f, 0x3d, 0xff)        # #ff3d7f
FLUX_BGR = (0x4d, 0xb7, 0xff)          # #ffb74d
LEVEL_BGR = (0x6a, 0xbb, 0x66)         # #66bb6a
EVENT_BGR = (0xfb, 0x40, 0xe0)         # #e040fb
CURSOR_BGR = (0x00, 0xd6, 0xff)        # #ffd600
PANEL_BGR = (0x1e, 0x1e, 0x1e)


def analysis_of(path) -> Optional[dict]:
    """The player's view of the analysis next to the recording (load_analysis with the frames'
    UTC, so the step-fit levels and D / R fall on their frames); None without one."""
    from .capture.playdata import load_analysis, load_timing
    if not path or load_analysis(path) is None:
        return None
    times, _ = load_timing(str(path))
    return load_analysis(path, times)


def _mark_target(img: np.ndarray, c: dict, index: int, factor: int):
    """The player's target mark: four ticks around the star (it stays visible), the name up
    and to the right on a dark box."""
    import cv2
    frames = c['frames']
    k = int(np.searchsorted(frames, index))
    if k >= len(frames) or frames[k] != index:
        return                                      # a frame the analysis did not cover
    x, y = c['xy'][0][k], c['xy'][1][k]
    if not (np.isfinite(x) and np.isfinite(y)):
        return
    cx, cy = (x + 0.5) / factor - 0.5, (y + 0.5) / factor - 0.5
    s = max(1.0, img.shape[0] / 600)                 # ticks scale with the movie, not the zoom
    r0, r1, th = 10 * s, 22 * s, max(1, round(1.5 * s))
    sh = 4                                           # sub-pixel line ends (cv2 shift bits)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        p0 = (round((cx + dx * r0) * 16), round((cy + dy * r0) * 16))
        p1 = (round((cx + dx * r1) * 16), round((cy + dy * r1) * 16))
        cv2.line(img, p0, p1, TARGET_BGR, th, cv2.LINE_AA, sh)
    text = str(c.get('label') or '')
    if text:
        scale = 0.45 * s
        (tw, tht), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        x0, y0 = int(cx + 0.7 * r1), int(cy - 0.7 * r1)
        x0 = min(x0, img.shape[1] - tw - 4)
        y0 = max(y0, tht + base + 2)
        box = img[max(0, y0 - tht - base - 2):y0 + 1, max(0, x0 - 3):x0 + tw + 3]
        box[:] = (box * 0.4).astype(np.uint8)
        cv2.putText(img, text, (x0, y0 - base), cv2.FONT_HERSHEY_SIMPLEX, scale, TARGET_BGR, 1,
                    cv2.LINE_AA)


def _curve_panel(c: dict, w: int, start: int, stop: int, factor: int) -> np.ndarray:
    """The player's light-curve track over frames start..stop, w wide: flux (min / max per
    pixel column), step-fit levels, D / R with their times, the title; no cursor."""
    import cv2
    H = max(120, int(round(w * 0.22 / 2)) * 2)
    p = np.empty((H, w, 3), np.uint8)
    p[:] = PANEL_BGR
    s = max(0.8, w / 900)
    font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.5 * s
    frames, flux = np.asarray(c['frames'], float), np.asarray(c['flux'], float)
    ok = np.isfinite(flux) & (frames >= start) & (frames <= stop)
    cv2.line(p, (0, 0), (w, 0), (0x44, 0x44, 0x44), 1)
    title = f"{c.get('label') or c.get('target', '')}: flux relative to its median ({c.get('what', '')})"
    if cv2.getTextSize(title, font, scale, 1)[0][0] > w - 12:       # narrow movie: the short form
        title = f"{c.get('label') or c.get('target', '')}: flux / median"
    cv2.putText(p, title, (6, int(17 * s)), font, scale, (0xd3, 0xd3, 0xd3), 1, cv2.LINE_AA)
    if not ok.any():
        return p
    levels = [lv for lv in c.get('levels') or [] if lv[1] >= start and lv[0] <= stop]
    lo, hi = np.percentile(flux[ok], (0.2, 99.8))
    for _, _, f in levels:
        lo, hi = min(lo, f), max(hi, f)
    pad = 0.06 * max(hi - lo, 1e-6)
    lo, hi = lo - pad, hi + pad
    y0, y1 = int(48 * s), H - 6

    def y(v):
        return int(round(y1 - (y1 - y0) * (min(max(v, lo), hi) - lo) / max(hi - lo, 1e-9)))

    def x(i):
        return int(round(_px(i, start, stop, w)))
    inside = (frames >= start) & (frames <= stop)
    if inside.sum() <= w - 8:                       # fewer frames than columns: a line
        pts = [(x(f), y(v)) if np.isfinite(v) else None for f, v in zip(frames[inside], flux[inside])]
        run = []
        for pt in pts + [None]:                     # broken where the flux is missing
            if pt is None:
                if len(run) > 1:
                    cv2.polylines(p, [np.array(run, np.int32)], False, FLUX_BGR, 1, cv2.LINE_AA)
                run = []
            else:
                run.append(pt)
    else:                                           # min / max per pixel column (the player)
        cols = np.clip([x(f) for f in frames[ok]], 0, w - 1)
        vals = flux[ok]
        for k in np.unique(cols):
            seg = vals[cols == k]
            cv2.line(p, (int(k), y(seg.min())), (int(k), y(seg.max())), FLUX_BGR, 1)
    for i0, i1, f in levels:
        cv2.line(p, (x(max(i0, start)), y(f)), (x(min(i1, stop)), y(f)), LEVEL_BGR, 2)
    for k, (i, kind, txt) in enumerate(c.get('marks') or []):
        if not start <= i <= stop:
            continue
        xi = x(i)
        cv2.line(p, (xi, y0 - 12), (xi, y1), EVENT_BGR, 1)
        cv2.putText(p, f'{kind} {txt}', (xi + 3, int((42 if k % 2 else 30) * s)), font, scale,
                    EVENT_BGR, 1, cv2.LINE_AA)
    for v in (0.0, 1.0):
        if lo <= v <= hi:
            (tw, _), _ = cv2.getTextSize(f'{v:g}', font, scale, 1)
            cv2.putText(p, f'{v:g}', (w - 8 - tw, y(v) - 3), font, scale, (0x9e, 0x9e, 0x9e), 1,
                        cv2.LINE_AA)
    return p


def _px(i: float, start: int, stop: int, w: int) -> float:
    return 4 + (w - 8) * (i - start) / max(1, stop - start)


def _cursor(panel: np.ndarray, index: int, start: int, stop: int) -> np.ndarray:
    import cv2
    out = panel.copy()
    xi = int(round(_px(index, start, stop, panel.shape[1])))
    cv2.line(out, (xi, 0), (xi, panel.shape[0]), CURSOR_BGR, 1)
    return out


def _movie_fps(reader, start, stop) -> Optional[float]:
    """Frame rate from the time stamps of up to 50 frames, None when they are missing."""
    from .plot import parse_times
    idx = range(start, min(stop, start + 50) + 1)
    t = parse_times([reader.read(i).time for i in idx])
    dt = np.diff(t[np.isfinite(t)])
    dt = dt[dt > 0]
    return float(1.0 / np.median(dt)) if dt.size else None


def _stamp(img: np.ndarray, text: str):
    """Frame number and time in the bottom left corner, white on a black band."""
    import cv2
    h = img.shape[0]
    scale = max(0.4, h / 1400)
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    img[h - th - base - 8:h, :tw + 10] = 0
    white = 255 if img.ndim == 2 else (255, 255, 255)
    cv2.putText(img, text, (5, h - base - 4), cv2.FONT_HERSHEY_SIMPLEX, scale, white, 1, cv2.LINE_AA)


# --------------------------------------------------------------------------- command line

def main(argv=None) -> int:
    """pystarlight movie FILE: the compressed movie (H.264 MP4, or --lossless FFV1 MKV)."""
    import argparse

    from .movie import open_movie
    p = argparse.ArgumentParser(prog='pystarlight movie',
                                description='Compressed copy of a recording: H.264 MP4 to look at '
                                            '(lossy), or --lossless FFV1 MKV (16-bit pixels kept).')
    p.add_argument('file', help='recording (RAVF, ADV, SER, AVI, FITS folder)')
    p.add_argument('-o', '--output', help='output file (default next to the recording)')
    p.add_argument('--start', type=int, default=0, help='first frame')
    p.add_argument('--stop', type=int, help='last frame (inclusive)')
    p.add_argument('--fps', type=float, help='playback rate (default: the recording\'s)')
    p.add_argument('--lossless', action='store_true', help='FFV1 in MKV, the recorded pixels')
    p.add_argument('--crf', type=int, default=23, help='H.264 quality (lower = better, larger)')
    p.add_argument('--bin', type=int, default=2, help='bin N x N pixels (MP4 only; 1: full size, ~4x larger)')
    p.add_argument('--no-denoise', action='store_true', help='no spatial denoising (MP4)')
    p.add_argument('--no-overlay', action='store_true', help='no frame number / time burnt in')
    p.add_argument('--target', action='store_true',
                   help="mark the analysis' target (open cross + name), as the player does")
    p.add_argument('--curve', action='store_true',
                   help="the target's light curve below the frame, with a time cursor (as the player)")
    a = p.parse_args(argv)
    src = Path(a.file)
    out = Path(a.output) if a.output else src.with_suffix('.mkv' if a.lossless else '.mp4')
    if out.resolve() == src.resolve():
        p.error('the output would overwrite the recording')
    with open_movie(str(src)) as reader:
        res = write_movie(reader, out, a.start, a.stop, fps=a.fps, lossless=a.lossless, crf=a.crf,
                          bin_factor=a.bin, denoise=not a.no_denoise, overlay=not a.no_overlay,
                          target=a.target, curve=a.curve,
                          progress=lambda i, last: print(f'\rframe {i} / {last}', end='', flush=True))
    if res.get('note'):
        print(f'\n{res["note"]}', end='')
    print(f'\nwrote {res["path"]}: {res["frames"]} frames at {res["fps"]:.3f} fps, '
          f'{res["bytes"] / 1e6:.1f} MB')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
