"""Recorder thread: writes every frame to a RAVF (default) or SER file, see ravf_writer.py and
ser_writer.py; and repair of recordings interrupted by a crash or power loss.

The grabber hands the recorder raw frames through a bounded queue, the recorder decodes
(unpacks) and writes them. If it falls behind, frames are dropped and counted, never silently
lost.

Repair from the command line:  python3 -m pystarlight.capture.recorder FILE.ravf|FILE.ser ...
(or `python3 src/pystarlight/camera.py --repair FILE ...` with the system Python on the Pi).
"""
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone

from . import ravf_writer, ser_writer

WRITERS = {"ravf": ravf_writer.RavfWriter, "ser": ser_writer.SerWriter}
RECORD_FORMATS = list(WRITERS)       # the first is the default


class Recorder(threading.Thread):
    """States: idle -> (armed: waiting for start time) -> recording -> idle.

    decode(raw) -> 2-D image; frame_format() -> (width, height, depth) of the frames now;
    header_info() -> dict of metadata for a new file (see ravf_writer.header_entries), asked
    for when a recording starts.
    """

    def __init__(self, decode, frame_format, out_dir, name, fmt="ravf", header_info=dict,
                 max_queue_bytes=1.0e9):
        super().__init__(daemon=True)
        self.decode = decode
        self.frame_format = frame_format
        self.out_dir, self.name = out_dir, name
        self.fmt = fmt                  # "ravf" / "ser", for the next recording
        self.header_info = header_info
        self.text = {}                  # observer, telescope ... set by the GUI (override)
        w, h, _ = frame_format()
        self.queue = queue.Queue(maxsize=max(4, int(max_queue_bytes // (w * h * 2))))

        self.lock = threading.Lock()
        self.state = "idle"
        self.start_ns = None            # armed: first frame at/after this UTC time
        self.stop_ns = None             # stop at this UTC time (frame timestamp)
        self.max_frames = None
        self.writer = None
        self.path = None
        self.frames = 0
        self.synced = 0                 # frames on the disk for sure
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
    # exposure_ns: its exposure start, the time stamp written to the file;
    # info: per-frame values for the file (exposure duration, GPS status)
    def wants(self, frame_ns):
        state, stop_ns = self.state, self.stop_ns
        if stop_ns is not None and frame_ns >= stop_ns:
            return False
        return state == "recording" or (state == "armed" and frame_ns >= self.start_ns)

    def put(self, raw, frame_id, frame_ns, exposure_ns, info=None):
        try:
            self.queue.put_nowait((raw, frame_id, frame_ns, exposure_ns, info or {}))
        except queue.Full:
            self.dropped += 1

    # ---- writer thread ----
    def run(self):
        while self.running:
            try:
                raw, frame_id, frame_ns, exposure_ns, info = self.queue.get(timeout=0.2)
            except queue.Empty:
                with self.lock:
                    if self.writer is not None and self.synced != self.frames:
                        self.writer.sync()      # idle (e.g. slow triggers): onto the disk
                        self.synced = self.frames
                    # queue drained and past the stop time (no more frames, e.g. no triggers)
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
            self.writer.write(image, exposure_ns, {"frame_id": frame_id, **info})
            if self.first_ns is None:
                self.first_ns = exposure_ns
            self.last_ns = exposure_ns
            self.frames += 1
            self.latest = image

    def _open(self, frame_ns):
        stamp = datetime.fromtimestamp(frame_ns / 1e9, timezone.utc).strftime("%Y%m%dT%H%M%S")
        writer = WRITERS[self.fmt]
        os.makedirs(self.out_dir, exist_ok=True)
        self.path = os.path.join(self.out_dir, f"{self.name}_{stamp}Z{writer.extension}")
        width, height, depth = self.frame_format()
        meta = {**self.header_info(), **self.text, "start_ns": frame_ns}
        self.writer = writer(self.path, width, height, depth, meta)
        self.frames = self.dropped = self.synced = 0
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


def repair(path):
    """Finish an interrupted .ravf or .ser recording in place; returns a message."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ravf":
        return ravf_writer.repair(path)
    if ext == ".ser":
        return ser_writer.repair(path)
    raise ValueError(f"{path}: not a .ravf or .ser file")


def main(argv=None):
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        raise SystemExit(__doc__)
    for p in paths:
        if p.endswith(".times"):                    # SER sidecar: repair its SER file
            p = p[:-len(".times")]
        try:
            print(repair(p))
        except (OSError, ValueError) as e:
            print(f"{p}: {e}")


if __name__ == "__main__":
    main()
