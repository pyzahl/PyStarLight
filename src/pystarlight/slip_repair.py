"""TEMPORARY: repair recordings hit by the VEYE MV-MIPI-IMX265M word slip (capture/slipguard.py).

    pystarlight slip-repair FILE [-o OUT] [--shift PX|auto] [--check]

The camera delays its byte stream by 8 bytes per slip, adding up until the stream restarts
(one continuous stream: the delay runs across rows and frames). The Pi unpacks RAW12 per
line, so the stored 12-bit values re-pack exactly to the received bytes. Per frame:

  1. per row (central window): the byte phase d of the packing groups (0, 1, 2); d changes
     by one step per slip (d = delay mod 3), runs shorter than MIN_RUN rows are noise;
  2. where d changes, the byte position of the slip from the full rows around it (the split
     that fits both phases best);
  3. the delay of each piece: 8 bytes x the slips counted since the start of the recording,
     plus the start's own: the smallest consistent with the first frame's phase (0, 8 or 16
     bytes), plus --shift. Undo it, unpack: lossless except the 8 bytes per slip and, at the
     frame's end, the bytes the delay pushed out (filled from the row above).

Slips before the recording started leave a shift by a multiple of 16 px that the bytes
can't tell: the repaired frames all have the first frame's geometry. `--shift PX` (a
multiple of 16) moves them on; `--shift auto` looks for the wrap seam in the median image
(needs structure: a target, not a dark sky).

Output: a copy (default `<name>.repaired.<ext>`, sidecars copied) with only the pixel
blocks of changed frames rewritten: time stamps, frame headers and station entries stay as
recorded. `<out>.slips.json`: what was found and done. Delete this file and its line in
cli.py when VEYE fixes the firmware.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import struct
import sys
import time

import numpy as np

from .capture.slipguard import pack_mipi, row_phases, unpack_mipi_rows

BITS = 12
GROUP_BYTES = 3                 # RAW12: 2 pixels in 3 bytes
SLIP_BYTES = 8
WINDOW_PX = 768                 # per-row phase from a central window
MIN_RUN = 3                     # decided rows a phase must hold to count (else noise)


# ---------------------------------------------------------------------------- files
def frame_blocks(path):
    """(width, height, depth, [(offset of pixel data, length)]) of a RAVF or SER file."""
    with open(path, "rb") as f:
        if path.lower().endswith(".ravf"):
            from .capture import ravf_writer as rw
            entries = rw.read_header(f)[0]
            get = {e[0]: e[2] for e in entries}.get
            w, h, depth = get("IMAGE-WIDTH"), get("IMAGE-HEIGHT"), get("IMAGE-BIT-DEPTH", 16)
            blocks = []
            for off, _start in rw.scan_frames(f):
                f.seek(off)
                _m, hlen, dlen, *_ = rw.FRAME_HEAD.unpack(f.read(rw.FRAME_HEAD.size))
                blocks.append((off + hlen, dlen))
            return w, h, depth, blocks
        if path.lower().endswith(".ser"):
            from .capture import ser_writer as sw
            fields = struct.unpack(sw.SER_HEADER, f.read(sw.SER_HEADER_SIZE))
            w, h, depth, count = fields[4], fields[5], fields[6], fields[7]
            size = w * h * (2 if depth > 8 else 1)
            return w, h, depth, [(sw.SER_HEADER_SIZE + i * size, size) for i in range(count)]
    raise ValueError(f"{path}: RAVF or SER only")


def read_frame(f, block, w, h):
    f.seek(block[0])
    return np.frombuffer(f.read(block[1]), "<u2", count=w * h).reshape(h, w)


# ---------------------------------------------------------------------------- analysis
def window_phases(img):
    """Per row: byte phase from the central window, -1 undecided."""
    w = img.shape[1]
    px = min(WINDOW_PX, w) // 2 * 2
    x0 = (w - px) // 4 * 2
    return row_phases(pack_mipi(img[:, x0:x0 + px], BITS), BITS)


def phase_runs(ph):
    """[(first row, last row, phase)] of the decided rows, runs shorter than MIN_RUN dropped
    (a frame's last run is kept when it reaches the bottom: the slip came late)."""
    rows = np.flatnonzero(ph >= 0)
    if rows.size == 0:
        return []
    vals = ph[rows]
    cut = np.flatnonzero(np.diff(vals)) + 1
    runs = [(rows[a], rows[b - 1], int(vals[a]), b - a)
            for a, b in zip(np.r_[0, cut], np.r_[cut, vals.size])]
    keep = [r for i, r in enumerate(runs) if r[3] >= MIN_RUN
            or (i == len(runs) - 1 and r[1] >= len(ph) - 2 and len(runs) > 1)]
    merged = []
    for a, b, p, _n in keep:
        if merged and merged[-1][2] == p:
            merged[-1] = (merged[-1][0], b, p)
        else:
            merged.append((a, b, p))
    return merged


def locate(stream, row_bytes, r_from, r_to, d_old, d_new):
    """Byte position in the frame stream where the phase changes from d_old to d_new,
    between rows r_from .. r_to (whole rows): the split that fits both phases best."""
    s0, s1 = r_from * row_bytes, (r_to + 1) * row_bytes
    span = stream[s0:s1]
    m = (span.size - GROUP_BYTES) // GROUP_BYTES
    rough = []
    for d in (d_old, d_new):
        v = unpack_mipi_rows(span[None, d:d + m * GROUP_BYTES], BITS)[0].astype(np.int32)
        dv = np.abs(np.diff(v, append=v[-1]))
        rough.append(dv[0::2] + dv[1::2])            # per group
    cost = np.r_[0, np.cumsum(rough[0])] + (rough[1].sum() - np.r_[0, np.cumsum(rough[1])])
    c = int(np.argmin(cost))
    return s0 + GROUP_BYTES * c + d_new


class Tracker:
    """Slips counted over the recording: the phase at the end of the last frame, slips."""

    def __init__(self):
        self.phase = None           # None: before the first decided frame
        self.k = 0                  # slips since the start (relative)
        self.k0 = 0                 # slips before the start (smallest consistent)

    def pieces(self, img, frame_no, log):
        """[(start byte, slips)] for one frame, and the frame stream (packed) if needed."""
        runs = phase_runs(window_phases(img))
        h, w = img.shape
        row_bytes = w * BITS // 8
        if not runs:                # nothing decided (flat frame): as the last one
            return [(0, self.k)], None
        if self.phase is None:      # first frame: slips before the start, d = 8 k0 mod 3
            self.phase = runs[0][2]
            self.k0 = (2 * self.phase) % 3          # 8 k0 = d (mod 3)  <=>  k0 = 2 d (mod 3)
        out, stream = [(0, self.k)], None
        prev_end = -1
        for a, b, p in runs:
            if p != self.phase:
                dk = (self.phase - p) % 3            # each slip moves d one step back
                if stream is None:
                    stream = pack_mipi(img, BITS).ravel()
                r_from, r_to = max(prev_end, 0), min(a + 1, h - 1)
                pos = 0 if prev_end < 0 else locate(stream, row_bytes, r_from, r_to,
                                                     self.phase, p)
                self.k += dk
                if pos <= 0:
                    out[0] = (0, self.k)
                else:
                    out.append((pos, self.k))
                log.append({"frame": frame_no, "byte": int(pos), "row": int(pos // row_bytes),
                            "slips": int(dk), "total": int(self.k)})
                self.phase = p
            prev_end = b
        return out, stream


def rebuild(stream, pieces, k_extra, shape):
    """Undo the delays; returns (image, missing-pixel mask)."""
    n = stream.size
    true = np.zeros(n, np.uint8)
    have = np.zeros(n, bool)
    starts = [a for a, _ in pieces] + [n]
    for (a, k), b in zip(pieces, starts[1:]):
        delay = SLIP_BYTES * (k + k_extra)
        lo = max(a, delay)
        if lo < b:
            true[lo - delay:b - delay] = stream[lo:b]
            have[lo - delay:b - delay] = True
    h, w = shape
    img = unpack_mipi_rows(true.reshape(h, -1), BITS)
    missing = ~have.reshape(-1, GROUP_BYTES).all(axis=1)
    missing = np.repeat(missing, 2).reshape(h, w)
    if missing.any():                               # from the row above (below for row 0)
        for r in np.flatnonzero(missing.any(axis=1)):
            src = r - 1 if r > 0 else r + 1
            img[r, missing[r]] = img[src, missing[r]]
    return img, missing


def seam_shift(median_img):
    """Wrap seam in a repaired median image: (shift px multiple of 16, strength)."""
    m = median_img.astype(np.float64)
    e = np.median(np.abs(np.diff(m, axis=1)), axis=0)       # e[c-1]: between c-1 and c
    floor = max(float(np.median(e)), 1.0)                   # flat / saturated areas: 0
    scores = []
    for c in range(16, m.shape[1] - 16, 16):
        around = np.r_[e[c - 9:c - 2], e[c:c + 7]]
        scores.append((e[c - 1] / max(float(np.median(around)), floor), c))
    scores.sort(reverse=True)
    (s1, best), (s2, _) = scores[0], scores[1]
    # strength: above its surroundings, and clearly above the next candidate
    return best, min(s1, 2.0 * s1 / max(s2, 1e-9))


# ---------------------------------------------------------------------------- main
def repair(path, out=None, shift="0", check=False, progress=True):
    w, h, depth, blocks = frame_blocks(path)
    if depth != BITS:
        raise ValueError(f"{path}: {depth}-bit recording; the repair handles 12-bit (Y12/Y12P)")
    if (w * BITS) % 8 or w % 2:
        raise ValueError(f"{path}: width {w} not whole RAW12 groups")
    tr, log, changed, missing_px = Tracker(), [], 0, 0
    plan = []                       # (frame, pieces) of frames that need rewriting
    t0 = time.monotonic()
    with open(path, "rb") as f:
        for i, blk in enumerate(blocks):
            img = read_frame(f, blk, w, h)
            pieces, _ = tr.pieces(img, i, log)
            plan.append(pieces)
            if progress and (i % 200 == 0 or i == len(blocks) - 1):
                print(f"\r  analysed {i + 1}/{len(blocks)} frames, slips {tr.k}", end="",
                      file=sys.stderr, flush=True)
    if progress:
        print(file=sys.stderr)
    k_extra = tr.k0
    seam = None
    if shift == "auto":
        picks = np.linspace(0, len(blocks) - 1, min(25, len(blocks))).astype(int)
        with open(path, "rb") as f:
            stack = [rebuild(pack_mipi(read_frame(f, blocks[i], w, h), BITS).ravel(),
                             plan[i], k_extra, (h, w))[0] for i in picks]
        px, strength = seam_shift(np.median(stack, axis=0))
        seam = {"shift_px": int(px), "strength": round(float(strength), 2)}
        if strength >= 4:
            k_extra += px // 16 * 3
        else:
            seam["note"] = "no clear seam: geometry of the first frame kept"
    elif int(shift):
        if int(shift) % 16:
            raise ValueError("--shift: a multiple of 16 px (3 slips of 8 bytes)")
        k_extra += int(shift) // 16 * 3

    report = {"input": os.path.abspath(path), "frames": len(blocks), "width": w, "height": h,
              "slips_in_recording": tr.k, "slips_before_start_assumed": tr.k0,
              "extra_shift_px": (k_extra - tr.k0) // 3 * 16, "seam": seam, "events": log}
    todo = [i for i, p in enumerate(plan) if len(p) > 1 or p[0][1] + k_extra != 0]
    report["frames_changed"] = len(todo)
    if check:
        report["output"] = None
        print(json.dumps({k: v for k, v in report.items() if k != "events"}, indent=1))
        return report

    base, ext = os.path.splitext(path)
    out = out or f"{base}.repaired{ext}"
    if os.path.abspath(out) == os.path.abspath(path):
        raise ValueError("the output must not be the input")
    if os.path.exists(out):                               # an earlier repair: read-only
        os.chmod(out, 0o644)
    shutil.copyfile(path, out)
    os.chmod(out, 0o644)
    for side in glob.glob(glob.escape(path) + ".*"):      # SER: .times, .meta.json
        shutil.copyfile(side, out + side[len(path):])
    with open(path, "rb") as fin, open(out, "r+b") as fout:
        for n, i in enumerate(todo):
            img = read_frame(fin, blocks[i], w, h)
            fixed, miss = rebuild(pack_mipi(img, BITS).ravel(), plan[i], k_extra, (h, w))
            missing_px += int(miss.sum())
            fout.seek(blocks[i][0])
            fout.write(np.ascontiguousarray(fixed, "<u2").tobytes())
            changed += 1
            if progress and (n % 200 == 0 or n == len(todo) - 1):
                print(f"\r  repaired {n + 1}/{len(todo)} frames", end="", file=sys.stderr, flush=True)
    if progress and todo:
        print(file=sys.stderr)
    os.chmod(out, 0o444)                                  # like a finished recording
    report.update(output=os.path.abspath(out), frames_changed=changed,
                  missing_pixels_filled=missing_px, seconds=round(time.monotonic() - t0, 1))
    with open(out + ".slips.json", "w") as f:
        json.dump(report, f, indent=1)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pystarlight slip-repair", description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", help="RAVF / SER recordings (12 bit)")
    ap.add_argument("-o", "--output", help="output file (one input only)")
    ap.add_argument("--shift", default="0",
                    help="extra shift in px (multiple of 16), or 'auto' (wrap seam; needs structure)")
    ap.add_argument("--check", action="store_true", help="only report, write nothing")
    a = ap.parse_args(argv)
    if a.output and len(a.files) > 1:
        ap.error("-o with one input only")
    for p in a.files:
        print(p)
        r = repair(p, a.output, a.shift, a.check)
        print(f"  slips {r['slips_in_recording']} in {len(r['events'])} events, "
              f"{r['frames_changed']} of {r['frames']} frames "
              + ("to change" if a.check else f"changed -> {r['output']}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
