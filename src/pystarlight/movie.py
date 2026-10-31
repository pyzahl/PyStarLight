"""Movie readers: every format PyMovie opens, returned as 2-D numpy frames.

Formats
    .avi .mov (and anything else FFmpeg reads)  -> AviReader   (OpenCV, converted to 8-bit gray)
    .ser                                        -> SerReader   (pure numpy, 8/16 bit)
    .adv .aav                                   -> AdvReader   (Adv2 package, 16 bit)
    .ravf                                       -> RavfReader  (ravf package, 16 bit)
    folder of .fits/.fit/.fts                   -> FitsFolderReader (astropy, uint16)

Use ``open_movie(path)``. Every reader has ``len()``, ``shape``, ``metadata``,
``read(i) -> Frame`` and ``frames(start, stop)``, and works as a context manager.

Extracted from PyMovie 4.2.8 (main.py: readAviSerAdvAavRavfFile, showFrame, getFrame,
getSharpCapTimestring; SER.py). Frame pixels match what PyMovie analyses, before its
optional corrections (see preprocess.py).
"""

from __future__ import annotations

import glob
import os
import re
import threading
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import numpy as np


@dataclass
class Frame:
    index: int
    image: np.ndarray                 # 2-D, native dtype (uint8 / uint16)
    date: Optional[str] = None        # 'YYYY-MM-DD' when the format carries one
    time: Optional[str] = None        # 'HH:MM:SS.ffffff[f]' start of exposure, when known
    status: dict = field(default_factory=dict)  # per-frame status (ADV/AAV/RAVF/FITS header)


class MovieReader:
    path: str
    metadata: dict

    def __len__(self) -> int:
        raise NotImplementedError

    def read(self, index: int) -> Frame:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def shape(self) -> tuple[int, int]:
        return self.read(0).image.shape

    def frames(self, start: int = 0, stop: Optional[int] = None, step: int = 1) -> Iterator[Frame]:
        """Yield frames start..stop (stop inclusive, like PyMovie's 'stop at frame').
        A negative step runs backwards."""
        if stop is None:
            stop = len(self) - 1 if step > 0 else 0
        rng = range(start, stop + 1, step) if step > 0 else range(start, stop - 1, step)
        for i in rng:
            yield self.read(i)

    def __iter__(self) -> Iterator[Frame]:
        return self.frames()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _ticks_to_datetime(ticks: int) -> tuple[str, str]:
    """.NET ticks (100 ns since 0001-01-01), as written by SharpCap/FireCapture."""
    ticks = int(ticks)
    usecs = ticks // 10
    extra_digit = ticks - 10 * usecs
    ts = datetime(1, 1, 1) + timedelta(microseconds=usecs)
    return (f'{ts.year:04d}-{ts.month:02d}-{ts.day:02d}',
            f'{ts.hour:02d}:{ts.minute:02d}:{ts.second:02d}.{ts.microsecond:06d}{extra_digit}')


# --------------------------------------------------------------------------- AVI / MOV

class AviReader(MovieReader):
    """Any FFmpeg-readable video. Frames are converted to 8-bit gray (cv2.COLOR_BGR2GRAY).

    sharpcap_timestamps: SharpCap 8-bit AVIs carry a .NET-ticks timestamp in the first
    8 bytes of each frame (PyMovie VTI choice 'SharpCap 8 bit avi').
    """

    def __init__(self, path: str, sharpcap_timestamps: bool = False):
        import cv2
        self._cv2 = cv2
        self.path = str(path)
        self.sharpcap_timestamps = sharpcap_timestamps
        self._cap = cv2.VideoCapture(self.path, cv2.CAP_FFMPEG)
        if not self._cap.isOpened():
            raise IOError(f'{self.path} could not be opened')
        fourcc = int(self._cap.get(cv2.CAP_PROP_FOURCC))
        self.fourcc = ''.join(chr((fourcc >> s) & 0xff) for s in (0, 8, 16, 24))
        self.fps = self._cap.get(cv2.CAP_PROP_FPS)
        self._count = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._next = 0
        self.metadata = {'fourcc': self.fourcc, 'fps': self.fps, 'frame_count': self._count}

    def __len__(self):
        return self._count

    def _seek(self, index: int):
        cv2 = self._cv2
        if index == self._next:
            return
        if self.fourcc == 'dvsd':
            # DV seeking is unreliable: PyMovie reopens and reads forward.
            if index < self._next:
                self._cap.release()
                self._cap = cv2.VideoCapture(self.path, cv2.CAP_FFMPEG)
                self._next = 0
            while self._next < index:
                self._cap.grab()
                self._next += 1
        else:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            self._next = index

    def read(self, index: int) -> Frame:
        self._seek(index)
        ok, bgr = self._cap.read()
        if not ok:
            raise IOError(f'frame {index} could not be read')
        self._next = index + 1
        img = self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
        date = time = None
        if self.sharpcap_timestamps:
            date, time = _ticks_to_datetime(np.frombuffer(img.tobytes()[:8], dtype='<i8')[0])
        return Frame(index, img, date, time)

    def close(self):
        self._cap.release()


# --------------------------------------------------------------------------- SER

SER_HEADER_SIZE = 178


class SerReader(MovieReader):
    """SER v3 (Hahn 2014). As in PyMovie, LittleEndian == 0 means little-endian 16-bit data
    (the convention capture programs actually use, opposite to the spec).
    RGB/BGR files (ColorID 100/101) are converted to gray; Bayer data is read as mono."""

    def __init__(self, path: str):
        self.path = str(path)
        with open(self.path, 'rb') as f:
            hdr = f.read(SER_HEADER_SIZE)
        i4 = np.frombuffer(hdr, dtype='<i4', count=7, offset=14)
        lu_id, color_id, little_endian, width, height, depth, count = (int(v) for v in i4)
        dt_local, dt_utc = np.frombuffer(hdr, dtype='<i8', count=2, offset=162)
        text = lambda a, b: hdr[a:b].split(b'\0')[0].decode('latin-1').strip()

        self.width, self.height, self._count = width, height, count
        self.bytes_per_pixel = 2 if depth > 8 else 1
        self.planes = 3 if color_id in (100, 101) else 1
        self.color_id = color_id
        if self.bytes_per_pixel == 1:
            self.dtype = np.dtype('uint8')
        else:
            self.dtype = np.dtype('<u2' if little_endian == 0 else '>u2')
        self.frame_bytes = width * height * self.planes * self.bytes_per_pixel

        # Unfinished recording of PyStarLight's capture app (crash): no trailer yet, the
        # timestamps are in the '.times' sidecar, the header count may lag behind the frames
        sidecar = self.path + '.times'
        self.recovered = os.path.exists(sidecar)
        if self.recovered:
            ticks = np.fromfile(sidecar, dtype='<i8')
            count = min(len(ticks),
                        (os.path.getsize(self.path) - SER_HEADER_SIZE) // self.frame_bytes)
            self._count = count
            self.ticks = ticks[:count]
            n_ts = count
            warnings.warn(f'{self.path}: unfinished recording, {count} frames with the '
                          'timestamps of its .times sidecar', stacklevel=2)

        self._data = np.memmap(self.path, dtype=self.dtype, mode='r', offset=SER_HEADER_SIZE,
                               shape=(count, height, width, self.planes))

        if not self.recovered:
            # Optional trailer: one int64 UTC tick count per frame
            trailer_at = SER_HEADER_SIZE + count * self.frame_bytes
            n_ts = (os.path.getsize(self.path) - trailer_at) // 8
            self.ticks = (np.fromfile(self.path, dtype='<i8', count=n_ts, offset=trailer_at)
                          if n_ts > 0 else np.zeros(0, dtype='<i8'))

        self.metadata = dict(
            FileID=text(0, 14), LuID=lu_id, ColorID=color_id, LittleEndian=little_endian,
            ImageWidth=width, ImageHeight=height, PixelDepthPerPlane=depth, FrameCount=count,
            Observer=text(42, 82), Instrument=text(82, 122), Telescope=text(122, 162),
            DateTimeLocal=' '.join(_ticks_to_datetime(dt_local)),
            DateTimeUTC=' '.join(_ticks_to_datetime(dt_utc)),
            BytesPerPixel=self.bytes_per_pixel, NumTimestamps=int(n_ts),
        )

    def __len__(self):
        return self._count

    def read(self, index: int) -> Frame:
        raw = self._data[index]
        if self.planes == 1:
            img = np.array(raw[:, :, 0], dtype=self.dtype.newbyteorder('='))
        else:
            import cv2
            code = cv2.COLOR_RGB2GRAY if self.color_id == 100 else cv2.COLOR_BGR2GRAY
            img = cv2.cvtColor(np.ascontiguousarray(raw, dtype=self.dtype.newbyteorder('=')), code)
        date = time = None
        if index < len(self.ticks):
            date, time = _ticks_to_datetime(self.ticks[index])
        return Frame(index, img, date, time)

    def close(self):
        self.__dict__.pop('_data', None)        # safe to call twice


# --------------------------------------------------------------------------- ADV / AAV

class _NumpyNewshapeCompat:
    """numpy for the Adv2 module only: Adv2 1.2.0 calls np.reshape(a, newshape=...), a keyword
    numpy 2.x removed. Everything else is plain numpy."""

    def __init__(self, np_module):
        self._np = np_module

    def __getattr__(self, name):
        return getattr(self._np, name)

    def reshape(self, a, *args, newshape=None, **kw):
        if newshape is not None:
            args = (newshape,) + args
        return self._np.reshape(a, *args, **kw)


def _import_adv2_file():
    """Adv2.Adv2File; Adv2 loads its native library on import, which fails (OSError) where the
    wheel has no matching build, e.g. on Apple Silicon (Adv2 1.2.0 ships an x86_64 dylib only)."""
    try:
        import Adv2.Adv2File as adv_file
    except OSError as e:
        raise ImportError(
            'Adv2 is installed but its native library (libAdvCore) does not load on this '
            'machine; Adv2 1.2.0 has no arm64 build for macOS. Run under an x86_64 Python '
            '(Rosetta) or replace Adv2/Adv2DLLlibs/libAdvCore.dylib with an arm64 build of '
            f'ADVlib. Original error: {e}') from e
    return adv_file


def _patch_adv2_for_numpy2():
    adv_file = _import_adv2_file()
    try:
        np.reshape(np.zeros(1), newshape=(1,))
    except TypeError:                       # numpy >= 2.x: give Adv2 the old keyword back
        if not isinstance(adv_file.np, _NumpyNewshapeCompat):
            adv_file.np = _NumpyNewshapeCompat(np)


# Adv2 wraps a native library with ONE open file for the whole process: opening a second file
# replaces it, and closeFile() closes it for everyone. All Adv2 calls go through this lock, and a
# reader that is not the current owner re-opens its file before reading.
_ADV_LOCK = threading.RLock()
_ADV_OWNER = None


class AdvReader(MovieReader):
    """ADV2 (.adv) and AAV (.aav) via the Adv2 package.

    Adv2 has one global open file per process. Several AdvReaders (also in different threads)
    still work: access is serialised, and a reader re-opens its file when another one took the
    global slot (about 30 ms). close() only closes the file if this reader holds it, and is safe
    to call more than once; a later read() re-opens.

    For AAV files ``integrated_frames`` is native/effective frame rate. Frames whose
    status 'IntegratedFrames' differs are flagged with ``status['bad_integration']``
    (PyMovie's aav_bad_frames list)."""

    def __init__(self, path: str):
        _patch_adv2_for_numpy2()
        self.path = str(path)
        with _ADV_LOCK:
            self._open()
            self.metadata = self._r.getAdvFileMetaData()
        self.is_aav = Path(self.path).suffix.lower() == '.aav'
        self.integrated_frames = None
        if self.is_aav:
            self.integrated_frames = (float(self.metadata['NATIVE-FRAME-RATE']) /
                                      float(self.metadata['EFFECTIVE-FRAME-RATE']))

    def _open(self):
        """(Re)open this file in Adv2's global slot. Call with _ADV_LOCK held."""
        global _ADV_OWNER
        self._r = _import_adv2_file().Adv2reader(self.path)
        _ADV_OWNER = self

    def __len__(self):
        return self._r.CountMainFrames

    def read(self, index: int) -> Frame:
        with _ADV_LOCK:
            if _ADV_OWNER is not self:          # another reader (or a close) took the slot
                self._open()
            err, image, info, status = self._r.getMainImageAndStatusData(index)
            image = image.copy()                # Adv2 reuses its buffer
            lo, hi = info.UtcMidExposureTimestampLo, info.UtcMidExposureTimestampHi
            date, start = info.DateString, info.StartOfExposureTimestampString
        if err:
            raise IOError(f'ADV frame {index}: {err} for {self.path}')
        status = dict(status)
        if self.is_aav and 'IntegratedFrames' in status:
            status['bad_integration'] = int(status['IntegratedFrames']) != self.integrated_frames
        # Adv2 never recognises a missing timestamp (it compares `timedelta == 0`) and then
        # reports 2010-01-01: treat a zero mid-exposure time as "no timestamp"
        if lo == 0 and hi == 0:
            return Frame(index, image, None, None, status)
        time = start.strip('[]') or None
        return Frame(index, image, date or None, time, status)

    def close(self):
        global _ADV_OWNER
        with _ADV_LOCK:
            if _ADV_OWNER is self:
                self._r.closeFile()
                _ADV_OWNER = None


# --------------------------------------------------------------------------- RAVF

class RavfReader(MovieReader):
    """RAVF via the ravf package. A recording that was never finished (crash, power loss:
    no index, OFFSET-INDEX 0) is read by rebuilding the index from the frames, without
    changing the file (`capture.recorder.repair` finishes it in place); `recovered` is True."""

    def __init__(self, path: str):
        from ravf import RavfReader as _Ravf
        from ravf.ravf_header import RavfHeader
        from ravf.ravf_index import RavfIndex

        from .capture import ravf_writer
        self.path = str(path)
        self._fh = open(self.path, 'rb')
        self.recovered = not ravf_writer.is_finished(self._fh)
        if self.recovered:
            self._fh.seek(0)
            self._r = _Ravf.__new__(_Ravf)
            self._r.header = RavfHeader.deserialize(self._fh)
            self._r.index = RavfIndex()
            for offset, start in ravf_writer.scan_frames(self._fh):
                self._r.index.add_frame(offset, start)
            warnings.warn(f'{self.path}: unfinished recording, {self._r.frame_count()} '
                          'complete frames recovered (no index)', stacklevel=2)
        else:
            self._fh.seek(0)
            self._r = _Ravf(self._fh)
        self.metadata = {k: v for k, v in self._r.metadata()}
        self.timestamps = self._r.timestamps()

    def __len__(self):
        return self._r.frame_count()

    def read(self, index: int) -> Frame:
        err, image, info, status = self._r.getPymovieMainImageAndStatusData(self._fh, index)
        if err:
            raise IOError(f'RAVF frame {index}: {err}')
        return Frame(index, image, info.get('start_timestamp_date'),
                     info.get('start_timestamp_time'), dict(status))

    def close(self):
        self._fh.close()


# --------------------------------------------------------------------------- FITS folder

def _natural_sort_key(path):
    base = os.path.basename(path)
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r'(\d+)', base)]


class FitsFolderReader(MovieReader):
    """A folder of single-frame FITS files, in natural sort order (frame_2 before frame_10).

    Pixels are returned as uint16. Big-endian float data is clipped to 1..65535 first,
    as PyMovie does for processed (non-camera) FITS files.
    Timestamp: DATE-OBS, else MIDTIME. QHY174M frames with GPSSTAT 'PartialData'
    are rebuilt from GPS_ST/GPS_SU and flagged ``status['gps_partial']``."""

    PATTERNS = ('*.fits', '*.fit', '*.fts')

    def __init__(self, folder: str):
        from astropy.io import fits
        self._fits = fits
        self.path = str(folder)
        files = []
        for p in self.PATTERNS:
            files += glob.glob(os.path.join(self.path, p))
        self.files = sorted(set(files), key=_natural_sort_key)
        if not self.files:
            raise IOError(f'no FITS files in {self.path}')
        self.metadata = dict(self._fits.getheader(self.files[0], 0))

    def __len__(self):
        return len(self.files)

    def read(self, index: int) -> Frame:
        img, hdr = self._fits.getdata(self.files[index], 0, header=True)
        if img.dtype in (np.dtype('>f8'), np.dtype('>f4')):
            img = np.clip(img, 1, 65535)
        img = img.astype('uint16', casting='unsafe')

        status = {'file': os.path.basename(self.files[index])}
        date_time = hdr.get('DATE-OBS') or hdr.get('MIDTIME')
        if (str(hdr.get('INSTRUME', '')).startswith('QHY174M')
                and str(hdr.get('GPSSTAT', '')).startswith('PartialData')):
            date, t = str(hdr['GPS_ST']).split('T')
            date_time = f"{date}T{t.split('.')[0]}.{int(hdr['GPS_SU'] * 10):07d}"
            status['gps_partial'] = True
        date = time = None
        if date_time and 'T' in date_time:
            date, time = date_time.split('T', 1)
        return Frame(index, img, date, time, status)


# --------------------------------------------------------------------------- factory

def open_movie(path: str, **kwargs) -> MovieReader:
    """Open a movie file or a FITS folder. kwargs go to the reader (e.g. sharpcap_timestamps)."""
    p = Path(path)
    if p.is_dir():
        return FitsFolderReader(str(p))
    ext = p.suffix.lower()
    if ext == '.ser':
        return SerReader(str(p))
    if ext in ('.adv', '.aav'):
        return AdvReader(str(p))
    if ext == '.ravf':
        return RavfReader(str(p))
    return AviReader(str(p), **kwargs)


def split_fields(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Interlaced video: (top field = even rows, bottom field = odd rows)."""
    return image[0::2, :], image[1::2, :]
