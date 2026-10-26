"""Record every frame to a SER file (SER v3) with a UTC timestamp per frame.

SER is read by PyStarLight, PyMovie, Tangra, SharpCap, FireCapture, PIPP etc.
Layout: 178-byte header, frames (16-bit little-endian for >8-bit data), then a trailer
of one int64 per frame: UTC in .NET ticks (100 ns since 0001-01-01). That time stamp is the
frame's exposure start as the caller works it out (camera.py: of row 0, for rolling shutters).

Like the capture programs (and PyStarLight's reader), the header's LittleEndian field is
0 for little-endian 16-bit data - the opposite of the written spec.

The Recorder runs in its own thread: the grabber hands it raw frames through a bounded
queue, the recorder decodes (unpacks) and writes them. If it falls behind, frames are
dropped and counted, never silently lost.
"""
import os
import queue
import struct
import threading
import time
from datetime import datetime, timezone

import numpy as np

SER_HEADER_SIZE = 178
TICKS_UNIX_EPOCH = 621355968000000000      # .NET ticks at 1970-01-01T00:00:00Z


def ns_to_ticks(unix_ns):
    return unix_ns // 100 + TICKS_UNIX_EPOCH


class SerWriter:
    def __init__(self, path, width, height, depth, observer="", instrument="", telescope=""):
        self.path = path
        self.width, self.height, self.depth = width, height, depth
        self.dtype = np.dtype("<u2") if depth > 8 else np.dtype("u1")
        self.frame_bytes = width * height * self.dtype.itemsize
        self.ticks = []
        self.text = (observer, instrument, telescope)
        self.f = open(path, "wb")
        self._write_header()
        self.last_header_update = time.monotonic()

    def _write_header(self):
        first = self.ticks[0] if self.ticks else 0
        local = first + int(datetime.now().astimezone().utcoffset().total_seconds() * 1e7) if first else 0
        hdr = struct.pack("<14s7i40s40s40sqq", b"LUCAM-RECORDER", 0, 0, 0,
                          self.width, self.height, self.depth, len(self.ticks),
                          *(t.encode("latin-1", "replace")[:40] for t in self.text),
                          local, first)
        assert len(hdr) == SER_HEADER_SIZE
        pos = self.f.tell()
        self.f.seek(0)
        self.f.write(hdr)
        self.f.seek(pos if pos else SER_HEADER_SIZE)

    def write(self, image, ticks):
        self.f.write(np.ascontiguousarray(image, dtype=self.dtype).tobytes())
        self.ticks.append(int(ticks))
        # keep the frame count current, so a crash still leaves a readable file
        # (only the timestamp trailer would be missing)
        if time.monotonic() - self.last_header_update > 1.0:
            self._write_header()
            self.last_header_update = time.monotonic()

    def close(self):
        if self.f is None:
            return
        self.f.write(np.asarray(self.ticks, dtype="<i8").tobytes())
        self._write_header()
        self.f.close()
        self.f = None


class Recorder(threading.Thread):
    """States: idle -> (armed: waiting for start time) -> recording -> idle."""

    def __init__(self, decode, width, height, depth, out_dir, name,
                 instrument="", observer="", telescope="", max_queue_bytes=1.0e9):
        super().__init__(daemon=True)
        self.decode = decode            # raw frame bytes -> 2-D numpy image
        self.width, self.height = width, height
        self.depth = depth              # significant bits per pixel
        self.out_dir, self.name = out_dir, name
        self.text = dict(observer=observer, instrument=instrument, telescope=telescope)
        raw_bytes = width * height * 2
        self.queue = queue.Queue(maxsize=max(4, int(max_queue_bytes // raw_bytes)))

        self.lock = threading.Lock()
        self.state = "idle"
        self.start_ns = None            # armed: first frame at/after this UTC time
        self.stop_ns = None             # stop at this UTC time (frame timestamp)
        self.max_frames = None
        self.writer = None
        self.path = None
        self.frames = 0
        self.dropped = 0
        self.first_ns = self.last_ns = None     # time stamps of the first/last frame written
        self.last_path = None           # the file finished last, and its frame counts
        self.last_frames = self.last_dropped = 0
        self.latest = None              # newest decoded image (the GUI shows it while recording)
        self.running = True
        self.start()

    # ---- control (GUI thread) ----
    def arm(self, start_ns=None, duration_s=None, max_frames=None):
        """Start now (start_ns None) or at a UTC time; stop after duration / frame count."""
        with self.lock:
            if self.state != "idle":
                return
            self.start_ns = start_ns if start_ns is not None else time.time_ns()
            self.stop_ns = (self.start_ns + int(duration_s * 1e9)) if duration_s else None
            self.max_frames = max_frames
            self.state = "armed"

    def stop(self):
        """Frames taken before now are still written (they may be queued), then the file
        is closed by the writer thread."""
        with self.lock:
            if self.state == "armed":
                self.state = "idle"
            elif self.state == "recording":
                now = time.time_ns()
                self.stop_ns = now if self.stop_ns is None else min(self.stop_ns, now)

    # ---- feeding (grabber thread) ----
    # frame_ns: the frame's time for start/stop decisions (e.g. its trigger pulse);
    # exposure_ns: its exposure start, the time stamp written to the file
    def wants(self, frame_ns):
        state, stop_ns = self.state, self.stop_ns
        if stop_ns is not None and frame_ns >= stop_ns:
            return False
        return state == "recording" or (state == "armed" and frame_ns >= self.start_ns)

    def put(self, raw, frame_id, frame_ns, exposure_ns):
        try:
            self.queue.put_nowait((raw, frame_id, frame_ns, exposure_ns))
        except queue.Full:
            self.dropped += 1

    # ---- writer thread ----
    def run(self):
        while self.running:
            try:
                raw, frame_id, frame_ns, exposure_ns = self.queue.get(timeout=0.2)
            except queue.Empty:
                # queue drained and past the stop time (no more frames, e.g. no triggers)
                with self.lock:
                    if self.state == "recording" and self.stop_ns is not None and \
                            time.time_ns() > self.stop_ns + 300_000_000:
                        self._close()
                continue
            with self.lock:
                if self.state == "armed" and frame_ns >= self.start_ns:
                    self._open(frame_ns)
                if self.state != "recording":
                    continue
                if (self.stop_ns is not None and frame_ns >= self.stop_ns) or \
                        (self.max_frames is not None and self.frames >= self.max_frames):
                    self._close()
                    continue
            image = self.decode(raw)
            self.writer.write(image, ns_to_ticks(exposure_ns))
            if self.first_ns is None:
                self.first_ns = exposure_ns
            self.last_ns = exposure_ns
            self.frames += 1
            self.latest = image

    def _open(self, frame_ns):
        stamp = datetime.fromtimestamp(frame_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S")
        os.makedirs(self.out_dir, exist_ok=True)
        self.path = os.path.join(self.out_dir, f"{self.name}_{stamp}Z.ser")
        self.writer = SerWriter(self.path, self.width, self.height, self.depth, **self.text)
        self.frames = self.dropped = 0
        self.first_ns = self.last_ns = None
        self.state = "recording"
        print(f"Recording to {self.path}")

    def _close(self):
        if self.writer is not None:
            self.writer.close()
            print(f"Recorded {self.frames} frames to {self.path}"
                  + (f" ({self.dropped} dropped: disk/CPU too slow)" if self.dropped else ""))
            self.writer = None
            self.last_path, self.last_frames, self.last_dropped = \
                self.path, self.frames, self.dropped
        self.state = "idle"

    def close(self):
        """Finish the file (called on exit); waits for queued frames."""
        self.stop()
        deadline = time.monotonic() + 10
        while self.state != "idle" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.running = False
