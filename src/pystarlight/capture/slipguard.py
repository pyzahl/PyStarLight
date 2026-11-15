"""TEMPORARY workaround: word slips of the VEYE MV-MIPI-IMX265M (firmware C 01.44 / L 02.07).

The camera now and then moves its output byte stream by one 64-bit word (8 bytes), always the
same way, adding up until the stream restarts. In RAW12 (2 px in 3 bytes) two slips out of
three leave the pixel packing misaligned (frames look like heavy noise), every third one
leaves a clean image shifted by 16 px. Reported to VEYE 2026-11-15 (test image 1).

The guard looks at a few rows of every frame: it re-packs them to the MIPI byte stream and
finds the byte phase (0, 1, 2 in RAW12; 0..4 in RAW10) at which the pixels make sense. Any
phase other than 0 is a slip: the frame is kept out of recording and stacking, and the
stream is reset (that clears the slip; the frames from the first slipped one to the reset
are lost and counted as `slipped`).

To remove: delete this file and the lines marked `slipguard` in acquisition.py, app.py and
gui.py. Off by default; live: `pystarlight-camera --slip-guard on` (or auto: IMX265 only).
"""
import time

import numpy as np

GROUP = {12: (2, 3), 10: (4, 5)}      # bits -> (pixels, bytes) per MIPI packing group
SAMPLE_ROWS = 16
SAMPLE_PX = 768         # pixels per sample row (a central window: ~1 ms per frame)
RESET = "restart"       # how to clear a slip: "restart" (STREAMOFF/ON) or "rebuild" (pipeline)
RESET_GAP_S = 2.0       # at most one reset per this many seconds
DECISIVE = 0.5          # best phase's roughness must be below this fraction of every other one


def pack_mipi(values, bits):
    """uint16 values (rows, w) of `bits` bits -> MIPI CSI-2 packed bytes (rows, w * bits / 8)."""
    n, g = GROUP[bits]
    lb = bits - 8
    p = values.reshape(values.shape[0], -1, n)
    out = np.empty((values.shape[0], p.shape[1], g), np.uint8)
    low = np.zeros(p.shape[:2], np.uint16)
    for i in range(n):
        out[:, :, i] = p[:, :, i] >> lb
        low |= (p[:, :, i] & ((1 << lb) - 1)) << (lb * i)
    out[:, :, n] = low
    return out.reshape(values.shape[0], -1)


def unpack_mipi_rows(b, bits):
    """MIPI packed bytes (rows, k * group bytes) -> uint16 (rows, k * group pixels)."""
    n, g = GROUP[bits]
    lb = bits - 8
    q = b.reshape(b.shape[0], -1, g).astype(np.uint16)
    out = np.empty(q.shape[:2] + (n,), np.uint16)
    for i in range(n):
        out[:, :, i] = (q[:, :, i] << lb) | ((q[:, :, n] >> (lb * i)) & ((1 << lb) - 1))
    return out.reshape(b.shape[0], -1)


def row_phases(packed, bits):
    """Per row: the phase d (bytes) at which the packing groups really start, 0 is aligned;
    -1 where the row doesn't tell (flat, empty or saturated)."""
    n, g = GROUP[bits]
    k = packed.shape[1] // g - 1                # whole groups available at every phase
    rough = np.empty((g, packed.shape[0]))
    for d in range(g):
        v = unpack_mipi_rows(packed[:, d:d + k * g], bits).astype(np.int32)
        rough[d] = np.abs(np.diff(v, axis=1)).mean(axis=1)
    best = rough.argmin(axis=0)
    r = np.sort(rough, axis=0)
    decided = (r[1] > 0) & (r[0] <= DECISIVE * r[1])
    return np.where(decided, best, -1)


def byte_phase(packed, bits):
    """Phase of a frame from its sample rows (top to bottom): the bottom-most decided row,
    since a slip mid-frame lasts to the end of the frame. None: no row tells."""
    p = row_phases(packed, bits)
    p = p[p >= 0]
    return int(p[-1]) if p.size else None


class SlipGuard:
    """Slip check for one stream (format, region); `check_rows(rows)` -> True: the frame slipped.

    A new stream starts aligned, so the grabber makes a new guard for every stream start.
    """

    FORMATS = {"Y12": (12, 4), "Y12P": (12, None), "Y10": (10, 6), "Y10P": (10, None)}

    def __init__(self, pixel_format, width, height, region_x=0):
        spec = self.FORMATS.get(pixel_format)
        n = GROUP[spec[0]][0] if spec else 1
        # a software x-crop that doesn't start on a packing group breaks the re-packing
        self.enabled = spec is not None and region_x % n == 0 and width % n == 0
        self.reason = ("" if self.enabled else
                       f"slip guard idle: {pixel_format} x={region_x} w={width} not supported")
        self.bits, self.shift = spec if spec else (None, None)
        self.width, self.height = width, height
        # spread over the frame, the last rows always in (a slip mid-frame shows to the end)
        self.rows = np.unique(np.r_[np.linspace(0, height - 1, min(SAMPLE_ROWS, height)).astype(int),
                                    np.arange(max(0, height - 3), height)])
        px = min(SAMPLE_PX, width) // (2 * n) * (2 * n)
        self.x0 = (width - px) // 2 // n * n            # starts on a packing group
        self.x1 = self.x0 + px
        self.phase = 0              # last decided phase

    def sample(self, rows):
        """Packed bytes of the sample rows; rows: uint8 (height, row bytes) without padding."""
        r = rows[self.rows]
        if self.shift is None:      # already MIPI packed
            return r[:, self.x0 * self.bits // 8:self.x1 * self.bits // 8]
        v = r.view(np.uint16)[:, self.x0:self.x1] >> self.shift
        return pack_mipi(v, self.bits)

    def check_rows(self, rows):
        if not self.enabled:
            return False
        p = row_phases(self.sample(rows), self.bits)
        p = p[p >= 0]
        if p.size:                  # undecided (flat image): keep the last verdict
            # any misaligned row: a slip mid-frame lasts to the frame's end, the rows above
            # it are fine, the frame isn't
            self.phase = int(p[-1]) if (p == 0).all() else int(p[p != 0][-1])
        return self.phase != 0

    def check(self, raw):
        """The same for a raw frame copy (bytes, h * row bytes)."""
        return self.check_rows(np.frombuffer(raw, np.uint8).reshape(self.height, -1))


def buffer_rows(buf, height):
    """uint8 (height, row bytes) of a stream buffer: the VEYE driver buffer in place (no copy),
    other backends through a copy."""
    s = getattr(buf, "stream", None)
    if s is not None and hasattr(s, "maps"):
        rows = np.frombuffer(s.maps[buf.index], np.uint8, count=s.height * s.bytesperline)
        return rows.reshape(s.height, s.bytesperline)[:, s.row_offset:s.row_offset + s.row_bytes]
    return np.frombuffer(buf.get_data(), np.uint8).reshape(height, -1)


def wanted(mode, model):
    """--slip-guard auto: on for the VEYE IMX265 only."""
    return mode == "on" or (mode == "auto" and "IMX265" in (model or "").upper())


def install(session, mode="auto"):
    """Arm the guard on the session's grabber (VEYE cameras); returns True when armed."""
    if not session.is_veye or not wanted(mode, session.camera.get_model_name()):
        return False
    g, s = session.grabber, session
    last = [-1e9]

    def factory():
        guard = SlipGuard(s.pixel_format, s.width, s.height, s.region[0])
        if not guard.enabled:
            print(guard.reason)
        return guard

    def on_slip():
        now = time.monotonic()
        if now - last[0] < RESET_GAP_S or s.controller is None:
            return
        last[0] = now
        print(f"Slip guard: camera word slip (frame {g.slipped} lost so far, event "
              f"{g.slip_events}); resetting the stream ({RESET})")
        if RESET == "rebuild":
            s.controller.request("reconfigure", {})
        else:
            s.controller.request("restart", True)

    g.slipguard_factory, g.on_slip = factory, on_slip
    print(f"Slip guard armed ({s.camera.get_model_name()}, reset: {RESET})")
    return True
