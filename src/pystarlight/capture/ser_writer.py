"""SER v3 writer with a UTC timestamp per frame, and repair of interrupted SER files.

SER is read by PyStarLight, PyMovie, Tangra, SharpCap, FireCapture, PIPP etc.
Layout: 178-byte header, frames (16-bit little-endian for >8-bit data), then a trailer
of one int64 per frame: UTC in .NET ticks (100 ns since 0001-01-01). That time stamp is the
frame's exposure start as the caller works it out (of row 0, for rolling shutters).

Like the capture programs (and PyStarLight's reader), the header's LittleEndian field is
0 for little-endian 16-bit data - the opposite of the written spec.

Crash safety: the timestamps only reach the file in the trailer at the end, so an interrupted
recording would lose them all. While recording they are therefore also appended to a sidecar
file `<name>.ser.times` (the same int64 ticks), flushed every second together with the
header's frame count; close() writes the trailer and deletes the sidecar. repair() turns an
interrupted file back into a complete SER file from what is on disk.
"""
import os
import struct
import time
from datetime import datetime

import numpy as np

SER_HEADER_SIZE = 178
SER_HEADER = "<14s7i40s40s40sqq"
TICKS_UNIX_EPOCH = 621355968000000000      # .NET ticks at 1970-01-01T00:00:00Z
SYNC_INTERVAL_S = 1.0                      # header update + flush + fsync


def ns_to_ticks(unix_ns):
    return unix_ns // 100 + TICKS_UNIX_EPOCH


def times_path(path):
    return path + ".times"


def _local_ticks(utc_ticks):
    return utc_ticks + int(datetime.now().astimezone().utcoffset().total_seconds() * 1e7)


class SerWriter:
    extension = ".ser"

    def __init__(self, path, width, height, depth, meta=None):
        meta = meta or {}
        self.path = path
        self.width, self.height, self.depth = width, height, depth
        self.dtype = np.dtype("<u2") if depth > 8 else np.dtype("u1")
        self.frame_bytes = width * height * self.dtype.itemsize
        self.count = 0
        self.first = 0                      # ticks of the first frame
        self.text = tuple(meta.get(k, "") for k in ("observer", "instrument", "telescope"))
        self.f = open(path, "wb")
        self.times = open(times_path(path), "wb")
        self._write_header()
        self.last_sync = time.monotonic()

    def _write_header(self):
        pos = self.f.tell()
        self.f.seek(0)
        self.f.write(_header(self.width, self.height, self.depth, self.count, self.text,
                             self.first))
        self.f.seek(max(pos, SER_HEADER_SIZE))

    def write(self, image, exposure_ns, info=None):
        ticks = int(ns_to_ticks(exposure_ns))
        self.f.write(np.ascontiguousarray(image, dtype=self.dtype).tobytes())
        self.times.write(struct.pack("<q", ticks))
        if not self.count:
            self.first = ticks
        self.count += 1
        if time.monotonic() - self.last_sync > SYNC_INTERVAL_S:
            self.sync()

    def sync(self):
        """Frame count into the header, everything onto the disk."""
        self._write_header()
        for f in (self.f, self.times):
            f.flush()
            os.fsync(f.fileno())
        self.last_sync = time.monotonic()

    def close(self):
        if self.f is None:
            return
        self.times.close()
        with open(times_path(self.path), "rb") as t:
            self.f.write(t.read())          # the trailer
        self._write_header()
        self.f.close()
        self.f = None
        os.remove(times_path(self.path))


def _header(width, height, depth, count, text, first_ticks):
    local = _local_ticks(first_ticks) if first_ticks else 0
    hdr = struct.pack(SER_HEADER, b"LUCAM-RECORDER", 0, 0, 0, width, height, depth, count,
                      *(t.encode("latin-1", "replace")[:40] for t in text), local, first_ticks)
    assert len(hdr) == SER_HEADER_SIZE
    return hdr


def repair(path):
    """Make an interrupted SER recording complete: drop a partial last frame, write the
    trailer from the sidecar timestamps (if there), fix the frame count. Returns a message.

    Complete files (frame count and trailer match the size) are left alone.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        fields = struct.unpack(SER_HEADER, f.read(SER_HEADER_SIZE))
    width, height, depth, count = fields[4:8]
    text = tuple(t.rstrip(b"\0").decode("latin-1") for t in fields[8:11])
    frame_bytes = width * height * (2 if depth > 8 else 1)
    tpath = times_path(path)
    if size == SER_HEADER_SIZE + count * (frame_bytes + 8) and not os.path.exists(tpath):
        return f"{path}: complete, {count} frames"
    frames = (size - SER_HEADER_SIZE) // frame_bytes
    ticks = b""
    if os.path.exists(tpath):
        with open(tpath, "rb") as t:
            ticks = t.read()
        frames = min(frames, len(ticks) // 8)
        ticks = ticks[:frames * 8]
    with open(path, "r+b") as f:
        f.truncate(SER_HEADER_SIZE + frames * frame_bytes)
        f.seek(0, 2)
        f.write(ticks)
        first = struct.unpack_from("<q", ticks)[0] if ticks else 0
        f.seek(0)
        f.write(_header(width, height, depth, frames, text, first))
    if os.path.exists(tpath):
        os.remove(tpath)
    return (f"{path}: repaired, {frames} frames"
            + ("" if ticks else " - NO timestamps (no .times sidecar)"))
