"""Star lock: hold 2-3 reference stars where they were by small mount corrections, and the
target's flux live as a quick look.

NEW CODE. The grabber offers frames whenever the lock is free (as the live stacker), so it never
holds up capture. Each frame: the lock stars are measured in small boxes where they should be
(centroid, flux, width, SNR), and the frame counts only when every check passes:
  - each star: SNR >= min_snr, flux 0.5 .. 2 x and width 0.6 .. 1.6 x its value at lock, no
    jump of more than max_jump px since its last good position;
  - the stars moved together (each within agree_px of their median shift): rejects hot pixels,
    cosmic rays, satellites and a wrong star; at least min_stars of them.
The frames of a window (interval_s) give the error: the median shift of the valid frames, used
only when >= min_valid of the window's frames are valid. Then:
  LOCKED   corrections are sent (if guiding is on and calibrated): pulse = -gain x the error in
           the calibrated directions, dead band, capped; the next window starts after the pulse
           and a settle time.
  HOLD     any doubt (clouds dim all stars at once, a star gone, too few valid frames): no
           pulses, the mount just tracks. Valid windows again: LOCKED.
  LOST     HOLD for more than lost_after_s: the boxes are given up; once a second the whole
           frame is searched for the same star pattern (all lock stars at their separations,
           fluxes alike). Found: LOCKED again. Never another star.
  CALIBRATING  pulses north, then west (each lengthened until the stars move cal_px), the
           response measured, and the same pulses back. Directions with too little motion, or
           north and west not roughly at right angles: guiding is refused.
Corrections that make the error grow window after window stop the guiding (the lock keeps
measuring). Frame time stamps are never touched; pulses go out through a callback
(pulse(direction, ms): 'N' 'S' 'W' 'E'), so this module knows nothing of INDI.

Live flux: target / the lock stars' summed flux in fixed circular apertures, per valid frame
(the target's aperture is not re-centred: it may vanish). Without a target: the lock stars'
summed flux relative to the value at lock (transparency). A quick look, not the analysis.

numpy / OpenCV only: runs with the Pi's system Python too.
"""
import collections
import queue
import threading
import time

import cv2
import numpy as np

IDLE, LOCKED, HOLD, LOST, CALIBRATING = "idle", "locked", "hold", "lost", "calibrating"


class Settings:
    """Everything tunable, with the defaults (config [lock] overrides any of them)."""
    stars = 3               # lock stars wanted
    min_stars = 2           # valid and agreeing stars per frame (1 when only one was found)
    box = 12                # half size of the measuring box (px)
    margin = 30             # lock stars at least this far from the frame edge (drift room)
    min_snr = 8.0           # peak / sky noise of a lock star
    max_peak = 0.85         # brightest pixel < this x full scale (saturated stars are refused)
    min_fwhm = 1.0          # px: narrower is a hot pixel or a cosmic ray, not a star
    flux_range = (0.5, 2.0)
    fwhm_range = (0.6, 1.6)
    max_jump = 5.0          # px per frame
    agree_px = 1.0
    interval_s = 1.5        # one correction per window
    min_valid = 0.8         # fraction of a window's frames
    min_frames = 3          # valid frames per window
    settle_s = 0.5          # after a pulse
    gain = 0.6
    integral = 0.15         # x the summed error: takes out a steady drift (polar alignment);
                            # summed only below integral_px, its share capped at half a full pulse
    integral_px = 2.0
    dead_band_px = 0.3
    max_pulse_ms = 1500
    lost_after_s = 30.0
    cal_px = 6.0            # calibration: move at least this far per direction
    cal_start_ms = 250
    cal_max_ms = 8000       # per direction, in all
    cal_min_px = 2.0        # less motion than this: the mount did not respond
    diverge_windows = 4     # corrections in a row with a growing error: guiding off

    def __init__(self, **kw):
        for k, v in kw.items():
            default = getattr(Settings, k, None)
            if default is not None and not callable(default):
                setattr(self, k, tuple(v) if isinstance(default, tuple) else type(default)(v))


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------
def measure(img, x, y, r, ap=None):
    """One star in the box of half size r around (x, y): dict x, y (centroid), flux (circular
    aperture `ap` around the centroid, sky from the box edge), fwhm, snr, peak (raw), or None
    (outside the frame, nothing above the noise). Centroid and width from the pixels above 20 %
    of the peak (and 3 sigma): the width is that of the core, smaller than the true FWHM but
    the same for every frame; a hot pixel is one pixel, no width."""
    h, w = img.shape
    xi, yi = int(round(x)), int(round(y))
    if xi - r < 0 or yi - r < 0 or xi + r >= w or yi + r >= h:
        return None
    raw = img[yi - r:yi + r + 1, xi - r:xi + r + 1].astype(np.float32)
    s = cv2.medianBlur(raw, 3)          # hot pixels out (a sensor can have hundreds); stars stay
    edge = np.concatenate([s[0], s[-1], s[1:-1, 0], s[1:-1, -1]])
    sky = float(np.median(edge))
    noise = 1.4826 * float(np.median(np.abs(edge - sky))) or 1.0
    d = s - sky
    wgt = np.where(d > max(3 * noise, 0.2 * float(d.max())), d, 0.0)    # the star's core only:
                                                                          # noise in the box stays out
    tot = float(wgt.sum())
    if tot <= 0:
        return None
    ax = np.arange(-r, r + 1, dtype=np.float32)
    cy = float((wgt.sum(1) * ax).sum() / tot)
    cx = float((wgt.sum(0) * ax).sum() / tot)
    var = float((wgt.sum(1) * (ax - cy) ** 2).sum() + (wgt.sum(0) * (ax - cx) ** 2).sum()) / (2 * tot)
    fwhm = 2.3548 * np.sqrt(max(var, 0.01))
    out = {"x": xi + cx, "y": yi + cy, "fwhm": float(fwhm), "snr": float(d.max() / noise),
           "peak": float(raw.max()), "sky": sky, "noise": noise}     # peak raw: saturation
    out["flux"] = aperture(d, cx, cy, ap if ap else max(3.0, 2.0 * fwhm))
    return out


def aperture(d, cx, cy, radius):
    """Sum of the sky-subtracted box d inside a circle (centre relative to the box centre)."""
    r = d.shape[0] // 2
    ax = np.arange(-r, r + 1, dtype=np.float32)
    inside = (ax[None, :] - cx) ** 2 + (ax[:, None] - cy) ** 2 <= radius ** 2
    return float(d[inside].sum())


def target_flux(img, x, y, r, ap):
    """The target's flux in a fixed aperture at (x, y) (not re-centred), sky from the box edge."""
    h, w = img.shape
    xi, yi = int(round(x)), int(round(y))
    if xi - r < 0 or yi - r < 0 or xi + r >= w or yi + r >= h:
        return None
    s = img[yi - r:yi + r + 1, xi - r:xi + r + 1].astype(np.float32)
    edge = np.concatenate([s[0], s[-1], s[1:-1, 0], s[1:-1, -1]])
    return aperture(s - float(np.median(edge)), x - xi, y - yi, ap)


def find_stars(img, r, nsigma=6.0, limit=200):
    """Star candidates of a whole frame: local maxima of the high-passed image above nsigma x
    the noise, measured (brightest first). Hot pixels are filtered out first (3 x 3 median)."""
    f = cv2.medianBlur(np.ascontiguousarray(img, dtype=np.float32), 3)
    hp = f - cv2.GaussianBlur(f, (0, 0), 8)
    sample = hp[::4, ::4]
    med = float(np.median(sample))
    noise = 1.4826 * float(np.median(np.abs(sample - med))) or 1.0
    peaks = (hp == cv2.dilate(hp, np.ones((7, 7), np.uint8))) & (hp > med + nsigma * noise)
    ys, xs = np.nonzero(peaks)
    order = np.argsort(hp[ys, xs])[::-1][:limit]
    out = []
    for i in order:
        m = measure(img, xs[i], ys[i], r)
        if m is not None:
            out.append(m)
    out.sort(key=lambda m: -m["flux"])
    return out


def pick(stars, shape, s, full_scale, target=None):
    """Lock stars from find_stars: unsaturated, bright enough, star-shaped, isolated, away from
    the edges and from the target; the brightest, then always the one farthest from those chosen
    (among the brighter half). Returns (chosen, notes)."""
    h, w = shape
    bright = [m["fwhm"] for m in stars if m["snr"] >= 2 * s.min_snr and s.min_fwhm <= m["fwhm"] <= s.box / 2]
    fw = np.median(bright[:20]) if bright else 0    # the stars' width (not that of noise / hot pixels)
    good, notes = [], collections.Counter()
    for m in stars:
        if full_scale and m["peak"] >= s.max_peak * full_scale:
            notes["saturated"] += 1
        elif m["snr"] < 2 * s.min_snr:
            notes["faint"] += 1
        elif not s.min_fwhm <= m["fwhm"] <= s.box / 2 or not 0.6 * fw <= m["fwhm"] <= 1.6 * fw:
            notes["not star-shaped"] += 1
        elif not (s.margin <= m["x"] < w - s.margin and s.margin <= m["y"] < h - s.margin):
            notes["at the edge"] += 1
        elif target is not None and np.hypot(m["x"] - target[0], m["y"] - target[1]) < 3 * s.box:
            notes["the target"] += 1
        elif any(o is not m and abs(o["x"] - m["x"]) < 1.5 * s.box and abs(o["y"] - m["y"]) < 1.5 * s.box
                 and o["flux"] > 0.05 * m["flux"] for o in stars):
            notes["a neighbour in the box"] += 1
        else:
            good.append(m)
    pool = good[:max(s.stars, len(good) // 2)]
    chosen = pool[:1]
    while len(chosen) < min(s.stars, len(pool)):
        rest = [m for m in pool if m not in chosen]
        chosen.append(max(rest, key=lambda m: min(np.hypot(m["x"] - c["x"], m["y"] - c["y"])
                                                  for c in chosen)))
    return chosen, notes


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
class StarLock:
    """The lock's logic, frame by frame: process(img, t) with t in seconds (monotonic). The GUI
    reads the public fields; LockThread feeds it from the grabber."""

    def __init__(self, settings=None, pulse=None, full_scale=None, log=print):
        self.s = settings or Settings()
        self.pulse = pulse              # pulse(direction, ms) or None: measure only
        self.full_scale = full_scale
        self.log = log
        self.guide = False              # send corrections (needs calibration)
        self.cal = None                 # 2x2: px per ms of a pulse N (column 0) and W (column 1)
        self.history = collections.deque(maxlen=20000)   # (t_utc_s, ratio) of valid frames
        self.errors = collections.deque(maxlen=600)      # (t, dx, dy) of the windows
        self.csv = None                 # open file: one row per window
        self._clear()

    def _clear(self):
        self.state = IDLE
        self.message = ""
        self.ref = []                   # lock stars at lock: dicts x, y, flux, fwhm
        self.pos = []                   # their last good positions
        self.offset = (0.0, 0.0)        # shift of the field from the lock (px)
        self.target = None              # (x, y) at lock
        self.target_ap = 4.0
        self.ens0 = None                # summed lock flux at lock
        self.window = []                # (dx, dy) or None per frame of the current window
        self.window_t0 = None
        self.next_window = 0.0          # no measuring before (pulse + settle)
        self.hold_since = None
        self.last_search = 0.0
        self.pulses = 0
        self.grown = []                 # errors after corrections (divergence check)
        self.sum_err = np.zeros(2)      # integral of the errors while guiding
        self.frames_valid = 0
        self.frames = 0
        self.cal_job = None
        self.stars_ok = 0
        self.min_stars = self.s.min_stars
        self.note = ""                  # the last event (calibration, guiding stopped, lost,
                                        # found again): stays until the next one

    # ---- control ----
    def lock(self, img, t, target=None):
        """Choose the lock stars on this frame. target (x, y): never a lock star; its flux is
        plotted. Returns True when locked."""
        cands = find_stars(img, self.s.box)
        chosen, notes = pick(cands, img.shape, self.s, self.full_scale, target)
        if not chosen:
            left = ", ".join(f"{k} {v}" for k, v in notes.items()) or "no stars found"
            self._clear()
            self.message = f"no lock star ({left})"
            return False
        self._clear()
        self.ref = [{k: m[k] for k in ("x", "y", "flux", "fwhm")} for m in chosen]
        self.pos = [(m["x"], m["y"]) for m in chosen]
        self.min_stars = min(self.s.min_stars, len(chosen))
        self.target = tuple(target) if target is not None else None
        self.target_ap = max(3.0, 2.0 * float(np.median([m["fwhm"] for m in chosen])))
        self.ens0 = sum(m["flux"] for m in chosen)
        self.state = LOCKED
        self.window_t0 = t
        self.message = (f"locked on {len(chosen)} star{'s' if len(chosen) > 1 else ''}"
                        + (" (only one: less certain)" if len(chosen) == 1 else ""))
        self.log(f"lock: {self.message} at " + ", ".join(f"({m['x']:.0f}, {m['y']:.0f})" for m in chosen))
        return True

    def release(self, note=""):
        self._clear()
        self.guide = False
        self.message = "released"
        self.note = note
        f, self.csv = self.csv, None
        if f is not None:
            f.close()

    def event(self, text):
        self.note = text
        self.log(f"lock: {text}")

    def calibrate(self, t):
        """Start the calibration (needs a lock and the pulse callback): the field's own drift
        first (two windows 2 s apart), then pulses north and west."""
        if self.pulse is None or not self.ref:
            self.event("calibration needs a lock and a mount that takes guide pulses")
            return False
        self.cal = None
        self.cal_job = {"dir": "N", "ms": 0, "step": self.s.cal_start_ms, "start": None,
                        "phase": "drift", "drift0": None, "rate": np.zeros(2), "result": {}}
        self.state = CALIBRATING
        self.message = "calibrating: the field's drift"
        self.next_window = t
        self.window, self.window_t0 = [], t
        return True

    # ---- per frame ----
    def process(self, img, t, utc_s=None):
        """One frame (2-D array) at monotonic time t; utc_s: its time stamp for the flux plot."""
        if self.state == IDLE:
            return
        self.frames += 1
        if self.state == LOST:
            if t - self.last_search >= 1.0:
                self.last_search = t
                self._search(img, t)
            return
        shift = self._frame(img, utc_s, moving=t < self.next_window)
        if t < self.next_window:            # a pulse is moving the field: not part of a window
            return
        if self.window_t0 is None or self.window_t0 < self.next_window:
            self.window, self.window_t0 = [], t
        self.window.append(shift)
        if t - self.window_t0 >= (0.5 if self.state == CALIBRATING else self.s.interval_s):
            self._end_window(t)

    def _frame(self, img, utc_s, moving=False):
        """Measure the lock stars; the field shift (dx, dy) when the frame is valid, else None."""
        shifts = {}                         # star index -> its shift from the lock, if it passed
        meas = {}
        for i, ((px, py), ref) in enumerate(zip(self.pos, self.ref)):
            m = measure(img, px, py, self.s.box)
            if m is not None and self._star_ok(m, ref, (px, py), moving):
                meas[i], shifts[i] = m, (m["x"] - ref["x"], m["y"] - ref["y"])
        self.stars_ok = 0
        if len(shifts) < self.min_stars:
            return None
        med = np.median(np.array(list(shifts.values())), axis=0)
        agree = [i for i, d in shifts.items() if np.hypot(d[0] - med[0], d[1] - med[1]) <= self.s.agree_px]
        self.stars_ok = len(agree)
        if len(agree) < self.min_stars:
            return None
        dx, dy = (float(v) for v in np.mean([shifts[i] for i in agree], axis=0))
        for i, ref in enumerate(self.ref):  # stars that did not count follow the field
            self.pos[i] = (meas[i]["x"], meas[i]["y"]) if i in agree else (ref["x"] + dx, ref["y"] + dy)
        self.offset = (dx, dy)
        self.frames_valid += 1
        ens = sum(meas[i]["flux"] for i in agree) * len(self.ref) / len(agree)
        if utc_s is not None and ens > 0:
            if self.target is not None:
                tf = target_flux(img, self.target[0] + dx, self.target[1] + dy, self.s.box, self.target_ap)
                if tf is not None:
                    self.history.append((utc_s, tf / ens))
            else:
                self.history.append((utc_s, ens / self.ens0))
        return dx, dy

    def _star_ok(self, m, ref, last, moving=False):
        """moving: a pulse is running, the field may step further (up to the box)."""
        lo, hi = self.s.flux_range
        wlo, whi = self.s.fwhm_range
        jump = self.s.box / 2 if moving else self.s.max_jump
        return (m["snr"] >= self.s.min_snr and lo <= m["flux"] / ref["flux"] <= hi
                and wlo <= m["fwhm"] / ref["fwhm"] <= whi
                and np.hypot(m["x"] - last[0], m["y"] - last[1]) <= jump)

    def _end_window(self, t):
        win = self.window
        valid = [v for v in win if v is not None]
        self.window, self.window_t0 = [], t
        ok = len(valid) >= self.s.min_frames and len(valid) >= self.s.min_valid * len(win)
        err = tuple(float(v) for v in np.median(np.array(valid), axis=0)) if ok else None
        if self.state == CALIBRATING:
            self._calibrate_step(t, err)
            return
        sent = (0, 0)
        if err is None:
            self.sum_err = np.zeros(2)
            if self.state != HOLD:
                self.hold_since = t
                self.state = HOLD
                self.message = (f"hold: {len(valid)} of {len(win)} frames certain "
                                f"({self.stars_ok} star{'s' if self.stars_ok != 1 else ''} ok)")
            elif t - self.hold_since > self.s.lost_after_s:
                self.state = LOST
                self.message = "lost: looking for the stars"
                self.event(f"lost: nothing certain for {self.s.lost_after_s:.0f} s")
        else:
            self.state = LOCKED
            self.hold_since = None
            self.errors.append((t, *err))
            self.message = f"error {err[0]:+.2f}, {err[1]:+.2f} px"
            if self.guide and self.cal is not None and self.pulse is not None:
                sent = self._correct(t, err)
        if self.csv is not None:
            self.csv.write(f"{time.time():.3f},{self.state},"
                           + (f"{err[0]:.3f},{err[1]:.3f}" if err else ",")
                           + f",{len(valid)},{len(win)},{sent[0]},{sent[1]}\n")
            self.csv.flush()

    def _correct(self, t, err):
        """Pulses that take out gain x the error; (ms north (+) / south (-), ms west / east)."""
        e = np.hypot(*err)
        self.grown = (self.grown + [e])[-self.s.diverge_windows:]
        if len(self.grown) == self.s.diverge_windows and all(
                b > a for a, b in zip(self.grown, self.grown[1:])) and e > 2 * self.s.dead_band_px + 1:
            self.guide = False
            self.event("guiding stopped: the corrections make the error grow (calibration wrong?)")
            return 0, 0
        if e < self.s.dead_band_px:         # seeing: neither pulsed nor summed
            return 0, 0
        if e < self.s.integral_px and self.s.integral > 0:
            cap = 0.5 * self.s.max_pulse_ms * float(np.linalg.norm(self.cal, axis=0).min()) / self.s.integral
            self.sum_err = np.clip(self.sum_err + err, -cap, cap)
        ms = -np.linalg.solve(self.cal, self.s.gain * np.array(err) + self.s.integral * self.sum_err)
        ms = np.clip(ms, -self.s.max_pulse_ms, self.s.max_pulse_ms)
        ns, we = int(round(ms[0])), int(round(ms[1]))
        if ns:
            self.pulse("N" if ns > 0 else "S", abs(ns))
        if we:
            self.pulse("W" if we > 0 else "E", abs(we))
        self.pulses += bool(ns) + bool(we)
        self.next_window = t + max(abs(ns), abs(we)) / 1000 + self.s.settle_s
        return ns, we

    def _calibrate_step(self, t, err):
        job = self.cal_job
        if err is None:
            self.state, self.cal_job = HOLD, None
            self.hold_since = t
            self.event("calibration stopped: stars not certain")
            return
        back = {"N": "S", "W": "E"}
        if job["phase"] == "drift":                 # unguided drift, taken out of the response
            if job["drift0"] is None:
                job["drift0"] = (t, np.array(err))
            elif t - job["drift0"][0] >= 2.0:
                job["rate"] = (np.array(err) - job["drift0"][1]) / (t - job["drift0"][0])
                job["phase"] = "measure"
                self.message = "calibrating: north"
            return
        if job["start"] is None:
            job["start"] = (t, np.array(err))
        moved = np.array(err) - job["start"][1] - job["rate"] * (t - job["start"][0])
        dist = float(np.hypot(*moved))
        if dist >= self.s.cal_px or job["ms"] >= self.s.cal_max_ms:
            self.pulse(back[job["dir"]], job["ms"])     # and back
            self.next_window = t + job["ms"] / 1000 + self.s.settle_s
            if dist < self.s.cal_min_px:
                self.cal_job, self.state = None, LOCKED
                self.event(f"calibration failed: {job['ms']} ms {job['dir']} moved the stars {dist:.1f} px")
                return
            job["result"][job["dir"]] = moved / job["ms"]
            if job["dir"] == "N":
                job.update(dir="W", ms=0, step=self.s.cal_start_ms, start=None)
                self.message = "calibrating: west"
                return
            self._finish_calibration(job["result"])
            return
        step = min(job["step"], self.s.cal_max_ms - job["ms"])
        self.pulse(job["dir"], step)
        job["ms"] += step
        if dist < 1.5:                      # hardly moved: longer pulses
            job["step"] = min(2 * job["step"], 2000)
        self.next_window = t + step / 1000 + self.s.settle_s

    def _finish_calibration(self, res):
        n, w = res["N"], res["W"]
        self.cal_job = None
        self.state = LOCKED
        cos = abs(np.dot(n, w)) / (np.hypot(*n) * np.hypot(*w))
        if cos > 0.5:                       # > 60 deg off a right angle
            angle = np.degrees(np.arccos(cos))
            self.event(f"calibration failed: north and west only {angle:.0f} deg apart")
            return
        self.cal = np.column_stack([n, w])
        self.grown = []
        self.sum_err = np.zeros(2)
        self.message = "locked"
        self.event(f"calibrated: north {np.hypot(*n) * 1000:.1f} px/s, west {np.hypot(*w) * 1000:.1f} "
                   f"px/s, {np.degrees(np.arccos(cos)):.0f} deg apart")

    def _search(self, img, t):
        """LOST: the same pattern anywhere in the frame (every lock star at its separation from
        the first, fluxes within the range)."""
        cands = find_stars(img, self.s.box, limit=100)
        lo, hi = self.s.flux_range
        r0 = self.ref[0]
        for c in cands:
            if not lo <= c["flux"] / r0["flux"] <= hi:
                continue
            dx, dy = c["x"] - r0["x"], c["y"] - r0["y"]
            hits = 1
            for r in self.ref[1:]:
                if any(np.hypot(o["x"] - r["x"] - dx, o["y"] - r["y"] - dy) <= 2 * self.s.agree_px
                       and lo <= o["flux"] / r["flux"] <= hi for o in cands):
                    hits += 1
            if hits == len(self.ref) and (len(self.ref) > 1 or self.min_stars == 1):
                self.pos = [(r["x"] + dx, r["y"] + dy) for r in self.ref]
                self.offset = (dx, dy)
                self.state, self.hold_since = LOCKED, None
                self.window, self.window_t0 = [], t
                self.grown = []
                self.message = "locked"
                self.event(f"found again, {dx:+.1f}, {dy:+.1f} px from the lock")
                return True
        return False

    # ---- for the GUI ----
    def rms(self, seconds=60.0):
        """RMS of the window errors over the last `seconds` (px), None without any."""
        if not self.errors:
            return None
        t_end = self.errors[-1][0]
        e = np.array([(dx, dy) for t, dx, dy in self.errors if t >= t_end - seconds])
        return float(np.sqrt((e ** 2).sum(axis=1).mean()))

    def boxes(self):
        """[(x0, y0, x1, y1, kind)] of the lock stars ('star') and the target ('target')."""
        b = self.s.box
        out = [(x - b, y - b, x + b, y + b, "star") for x, y in self.pos]
        if self.target is not None and self.state != IDLE:
            x, y = self.target[0] + self.offset[0], self.target[1] + self.offset[1]
            out.append((x - b, y - b, x + b, y + b, "target"))
        return out


class LockThread(threading.Thread):
    """Feeds a StarLock from the grabber (wants() / put(), as the live stacker) in a thread of
    its own. lock_request(target) chooses the stars on the next frame."""

    def __init__(self, lock, decode):
        super().__init__(daemon=True)
        self.lock = lock
        self.decode = decode
        self.inbox = queue.Queue(maxsize=1)
        self.request = None             # ('lock', target) / ('release',) / ('calibrate',)
        self.running = True
        self.error = None
        self.start()

    def wants(self):
        return (self.lock.state != IDLE or self.request is not None) and self.inbox.empty()

    def put(self, raw, exposure_ns, info=None):
        try:
            self.inbox.put_nowait((raw, exposure_ns))
        except queue.Full:
            pass

    def ask(self, *request):
        self.request = request

    def run(self):
        while self.running:
            req = self.request
            if req is not None and req[0] in ("release", "calibrate"):
                self.request = None
                if req[0] == "release":
                    self.lock.release(*req[1:])
                else:
                    self.lock.calibrate(time.monotonic())
            try:
                raw, exposure_ns = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                img = np.asarray(self.decode(raw))
                if img.ndim != 2:
                    img = img[..., 0] if img.ndim == 3 else img
                req, t = self.request, time.monotonic()
                if req is not None and req[0] == "lock":
                    self.lock.lock(img, t, req[1])
                    self.request = None             # after: the GUI sees the result, not idle
                else:
                    self.lock.process(img, t, exposure_ns / 1e9 if exposure_ns else None)
                self.error = None
            except Exception as e:          # e.g. the region changed under the lock
                self.error = f"lock: {e}"

    def close(self):
        self.running = False
