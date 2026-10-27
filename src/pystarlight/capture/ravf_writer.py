"""RAVF (Raw Astro Video Format, as written by ASTRID) writer, frame scan and repair.

Spec: https://github.com/ChasinSpin/ravf/blob/main/docs/RawAstroVideoFormat.md; this writes
the same bytes as the `ravf` package's RavfWriter (which PyMovie and PyStarLight read with), but
without needing that package on the Pi.

Layout: header (magic 'RAVF', version, metadata entries: name, type, value), then frames,
each a 101-byte frame header (magic, lengths, start timestamp and exposure in ns since
2010-01-01 UTC, GPS satellites / almanac / fix status, sequence number) followed by the image
data, and at the end an index (frame count, then offset and timestamp of every frame), whose
position the header's OFFSET-INDEX holds.

Crash safety: every frame carries its own magic and timestamp, so the index can always be
rebuilt from the frames (scan_frames). While recording, the header's FRAMES-COUNT is updated
and the file flushed to disk every second; OFFSET-INDEX stays 0 until close(), which marks an
unfinished file. repair() appends the index to such a file (dropping a partial last frame);
PyStarLight's reader also scans unfinished files on its own, without changing them.

Pixels are stored as 16-bit little-endian (IMAGE-FORMAT 16BIT) with their raw values (not
scaled up to 16 bits; the user entry IMAGE-BIT-DEPTH gives the depth), also for 8-bit data:
the ravf reader decodes 16BIT as is, but debayers its UNPACKED_12BIT unconditionally and
rejects 8BIT.
"""
import os
import struct
import time

import numpy as np

HEADER_MAGIC = 0x46564152           # 'RAVF'
VERSION = 1
FRAME_MAGIC = 0xAF8ABD3C2A98CA3F
FRAME_HEAD = struct.Struct("<QIIBQQBBbBI")  # magic, header length, data length, type,
                                            # start, exposure, satellites, almanac status,
                                            # almanac offset, fix status, sequence
FRAME_HEADER_LEN = FRAME_HEAD.size + 60     # padded, as the ravf package writes it
EPOCH_2010_NS = 1262304000 * 10**9          # 2010-01-01T00:00:00Z in Unix ns
SYNC_INTERVAL_S = 1.0

# metadata entry types (RavfMetadataType)
INT8, UINT8, INT16, UINT16, INT32, UINT32, INT64, UINT64, TIMESTAMP, FLOAT32, FLOAT64, UTF8 = \
    range(12)
_PACK = {INT8: "<b", UINT8: "<B", INT16: "<h", UINT16: "<H", INT32: "<i", UINT32: "<I",
         INT64: "<q", UINT64: "<Q", TIMESTAMP: "<Q", FLOAT32: "<f", FLOAT64: "<d"}

COLOR_MONO = 0
COLOR_TYPES = {"BayerRG": 1, "BayerGR": 2, "BayerGB": 3, "BayerBG": 4}  # RGGB GRBG GBRG BGGR
FORMAT_16BIT = 1
LITTLE_ENDIAN = 1
EQUINOX_JNOW = 1
FIX_NONE, FIX_TIME_POSITION = 0, 3          # satellite fix status: 0 none ... 3 time + position


def unix_to_ravf(ns):
    return int(ns) - EPOCH_2010_NS


def ravf_to_unix(ns):
    return int(ns) + EPOCH_2010_NS


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
def _utf8(text):
    b = str(text).encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _serialize(entries):
    out = [struct.pack("<IHI", HEADER_MAGIC, VERSION, len(entries))]
    for name, typ, value in entries:
        out.append(_utf8(name) + struct.pack("<B", typ))
        out.append(_utf8(value) if typ == UTF8 else struct.pack(_PACK[typ], value))
    return b"".join(out)


def read_header(f):
    """(entries [[name, type, value]], header length) of the file at its start."""
    f.seek(0)
    magic, _version, n = struct.unpack("<IHI", f.read(10))
    if magic != HEADER_MAGIC:
        raise ValueError("not a RAVF file")

    def utf8():
        size = struct.unpack("<H", f.read(2))[0]
        return f.read(size).decode("utf-8")

    entries = []
    for _ in range(n):
        name = utf8()
        typ = f.read(1)[0]
        if typ == UTF8:
            value = utf8()
        else:
            fmt = _PACK[typ]
            value = struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
        entries.append([name, typ, value])
    return entries, f.tell()


def _set(entries, name, value):
    for e in entries:
        if e[0] == name:
            e[2] = value
            return
    raise KeyError(name)


def _get(entries, name, default=None):
    return next((e[2] for e in entries if e[0] == name), default)


def _f32(v):
    return float(v or 0.0)


def header_entries(width, height, depth, meta):
    """The entries the ravf package requires, in its order, then PyStarLight's own."""
    m = meta.get
    color = COLOR_TYPES.get(str(m("pixel_format", ""))[:7], COLOR_MONO)
    return [
        ["OFFSET-FRAMES", UINT64, 0],
        ["OFFSET-INDEX", UINT64, 0],
        ["FRAMES-COUNT", UINT32, 0],
        ["COLOR-TYPE", UINT8, color],
        ["IMAGE-ENDIANESS", UINT8, LITTLE_ENDIAN],
        ["IMAGE-WIDTH", UINT32, width],
        ["IMAGE-HEIGHT", UINT32, height],
        ["IMAGE-ROW-STRIDE", UINT32, width * 2],         # bytes
        ["IMAGE-FORMAT", UINT8, FORMAT_16BIT],
        ["IMAGE-BINNING-X", UINT8, 1],
        ["IMAGE-BINNING-Y", UINT8, 1],
        ["RECORDER-SOFTWARE", UTF8, m("software", "PyStarLight capture")],
        ["RECORDER-SOFTWARE-VERSION", UTF8, m("software_version", "")],
        ["RECORDER-HARDWARE", UTF8, m("hardware", "")],
        ["RECORDER-HARDWARE-VERSION", UTF8, ""],
        ["INSTRUMENT", UTF8, m("instrument", "")],
        ["INSTRUMENT-VENDOR", UTF8, m("vendor", "")],
        ["INSTRUMENT-VERSION", UTF8, ""],
        ["INSTRUMENT-SERIAL", UTF8, m("serial", "")],
        ["INSTRUMENT-FIRMWARE-VERSION", UTF8, ""],
        ["INSTRUMENT-SENSOR", UTF8, m("sensor", "")],
        ["INSTRUMENT-GAIN", FLOAT32, _f32(m("gain_db"))],
        ["INSTRUMENT-GAMMA", FLOAT32, 1.0],
        ["INSTRUMENT-SHUTTER", UINT64, int(m("exposure_ns") or 0)],
        ["INSTRUMENT-OFFSET", UINT32, 0],
        ["TELESCOPE", UTF8, m("telescope", "")],
        ["OBSERVER", UTF8, m("observer", "")],
        ["OBSERVER-ID", UTF8, m("observer_id", "")],
        ["LATITUDE", FLOAT32, _f32(m("latitude"))],
        ["LONGITUDE", FLOAT32, _f32(m("longitude"))],
        ["ALTITUDE", FLOAT32, _f32(m("altitude"))],
        ["OBJNAME", UTF8, m("objname", "")],
        ["RA", FLOAT32, _f32(m("ra"))],
        ["DEC", FLOAT32, _f32(m("dec"))],
        ["EQUINOX", UINT8, EQUINOX_JNOW],
        ["RECORDING-START-UTC", TIMESTAMP, unix_to_ravf(m("start_ns") or time.time_ns())],
        ["COMMENT", UTF8, m("comment", "")],
        ["FRAME-TIMING-ACCURACY", UINT64, int(m("timing_accuracy_ns") or 0)],
        # user entries
        ["IMAGE-BIT-DEPTH", UINT8, depth],
        ["TIMESTAMP-SOURCE", UTF8, m("timestamp_source", "")],
        ["TIMESTAMP-ROW", UTF8, "exposure start of image row 0"],
        ["ROW-TIME-NS", UINT32, int(m("row_time_ns") or 0)],     # rolling shutter, 0: unknown
        ["ROI-X", UINT32, int(m("roi_x") or 0)],
        ["ROI-Y", UINT32, int(m("roi_y") or 0)],
        ["TRIGGER", UTF8, m("trigger", "")],
        ["TRIGGER-DELAY-NS", UINT64, int(m("trigger_delay_ns") or 0)],
    ]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------
class RavfWriter:
    extension = ".ravf"

    def __init__(self, path, width, height, depth, meta=None):
        self.path = path
        self.width, self.height, self.depth = width, height, depth
        self.entries = header_entries(width, height, depth, meta or {})
        _set(self.entries, "OFFSET-FRAMES", len(_serialize(self.entries)))
        self.index = []                     # (offset, start timestamp) per frame
        self.first_seq = None
        self.f = open(path, "wb")
        self.f.write(_serialize(self.entries))
        self.last_sync = time.monotonic()

    def _write_header(self):
        _set(self.entries, "FRAMES-COUNT", len(self.index))
        pos = self.f.tell()
        self.f.seek(0)
        self.f.write(_serialize(self.entries))
        self.f.seek(pos)

    def write(self, image, exposure_ns, info=None):
        """image: 2-D array; exposure_ns: UTC exposure start (Unix ns); info: frame_id,
        exposure_ns (duration), satellites, fix (0 none .. 3 time + position), almanac
        (0 leap seconds certain, 1 uncertain), almanac_offset (s)."""
        info = info or {}
        data = np.ascontiguousarray(image, dtype="<u2").tobytes()
        start = unix_to_ravf(exposure_ns)
        fid = info.get("frame_id")
        if self.first_seq is None:
            self.first_seq = fid or 0
        # missing numbers mark dropped frames (by the camera link or the recorder queue)
        seq = (fid - self.first_seq) if fid is not None else len(self.index)
        offset = self.f.tell()
        self.f.write(FRAME_HEAD.pack(
            FRAME_MAGIC, FRAME_HEADER_LEN, len(data), 0, start,
            int(info.get("exposure_ns", 0)), min(255, int(info.get("satellites", 0))),
            int(info.get("almanac", 1)), int(info.get("almanac_offset", 0)),
            int(info.get("fix", FIX_NONE)), seq & 0xFFFFFFFF)
            + bytes(FRAME_HEADER_LEN - FRAME_HEAD.size))
        self.f.write(data)
        self.index.append((offset, start))
        if time.monotonic() - self.last_sync > SYNC_INTERVAL_S:
            self.sync()

    def sync(self):
        """Frame count into the header, everything onto the disk."""
        self._write_header()
        self.f.flush()
        os.fsync(self.f.fileno())
        self.last_sync = time.monotonic()

    def close(self):
        if self.f is None:
            return
        _finish(self.f, self.entries, self.index)
        self.f.close()
        self.f = None


def _finish(f, entries, index):
    """Append the index at the current position and point the header at it."""
    f.seek(0, 2)
    _set(entries, "OFFSET-INDEX", f.tell())
    _set(entries, "FRAMES-COUNT", len(index))
    f.write(struct.pack("<I", len(index))
            + b"".join(struct.pack("<QQ", o, t) for o, t in index))
    f.seek(0)
    f.write(_serialize(entries))


# ---------------------------------------------------------------------------
# Unfinished files
# ---------------------------------------------------------------------------
def scan_frames(f, offset_frames=None):
    """[(offset, start timestamp)] of the complete frames in an open RAVF file, read from the
    frames themselves (stops at the first partial or damaged frame)."""
    if offset_frames is None:
        offset_frames = _get(read_header(f)[0], "OFFSET-FRAMES")
    size = os.fstat(f.fileno()).st_size
    frames, off = [], offset_frames
    while off + FRAME_HEAD.size <= size:
        f.seek(off)
        magic, hlen, dlen, _t, start, *_ = FRAME_HEAD.unpack(f.read(FRAME_HEAD.size))
        end = off + hlen + dlen
        if magic != FRAME_MAGIC or hlen < FRAME_HEAD.size or end > size:
            break
        frames.append((off, start))
        off = end
    return frames


def is_finished(f):
    """True if the header points at an index that ends the file and matches its count."""
    entries, _ = read_header(f)
    off = _get(entries, "OFFSET-INDEX") or 0
    size = os.fstat(f.fileno()).st_size
    if not off or off + 4 > size:
        return False
    f.seek(off)
    n = struct.unpack("<I", f.read(4))[0]
    return off + 4 + 16 * n == size


def repair(path):
    """Finish an interrupted RAVF recording in place: drop a partial last frame, append the
    index rebuilt from the frames, update the header. Returns a message."""
    with open(path, "r+b") as f:
        if is_finished(f):
            n = _get(read_header(f)[0], "FRAMES-COUNT")
            return f"{path}: complete, {n} frames"
        entries, _ = read_header(f)
        frames = scan_frames(f, _get(entries, "OFFSET-FRAMES"))
        if frames:
            f.seek(frames[-1][0])
            _magic, hlen, dlen, *_ = FRAME_HEAD.unpack(f.read(FRAME_HEAD.size))
            end = frames[-1][0] + hlen + dlen
        else:
            end = _get(entries, "OFFSET-FRAMES")
        f.truncate(end)
        _finish(f, entries, frames)
    return f"{path}: repaired, {len(frames)} frames"
