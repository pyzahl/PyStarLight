"""Star lock (capture/guider.py) on a synthetic field: choice of the lock stars, the error,
calibration and guiding against a fake mount, hold on clouds and lost stars, found again,
guiding stopped when corrections make it worse, the live flux."""

import numpy as np
import pytest

from pystarlight.capture import guider as gd

W, H = 320, 240
STARS = [(60, 50, 9000), (250, 60, 6000), (160, 190, 5000), (90, 160, 3000), (230, 170, 2500)]
TARGET = (200, 110, 2000)


class Sky:
    """Frames of a star field shifted by the mount's pointing error; pulses move it."""

    def __init__(self, drift=(0.0, 0.0), angle_deg=30.0, px_per_ms=0.004, flip=False, seed=1):
        self.rng = np.random.default_rng(seed)
        self.err = np.zeros(2)          # field offset (px)
        self.drift = np.array(drift)    # px / s, unguided
        a = np.radians(angle_deg)
        self.dirs = {"N": px_per_ms * np.array([np.cos(a), np.sin(a)]),
                     "W": px_per_ms * np.array([-np.sin(a), np.cos(a)])}
        self.dirs["S"], self.dirs["E"] = -self.dirs["N"], -self.dirs["W"]
        self.flip = flip
        self.pulses = []
        self.moving = []                # [px/s vector, seconds left] of running pulses
        self.transparency = 1.0
        self.target_level = 1.0
        self.hidden = set()             # indices of STARS not drawn
        self.yy, self.xx = np.mgrid[-7:8, -7:8]

    def pulse(self, direction, ms):
        """The field moves at the guide rate for the pulse's length (as a mount does)."""
        self.pulses.append((direction, ms))
        self.moving.append([(-1 if self.flip else 1) * self.dirs[direction] * 1000, ms / 1000])

    def frame(self, dt):
        self.err += self.drift * dt
        for m in self.moving:
            step = min(dt, m[1])
            self.err += m[0] * step
            m[1] -= step
        self.moving = [m for m in self.moving if m[1] > 0]
        img = self.rng.normal(500, 10, (H, W)).astype(np.float32)
        stars = [s for i, s in enumerate(STARS) if i not in self.hidden]
        stars.append((TARGET[0], TARGET[1], TARGET[2] * self.target_level))
        for x, y, a in stars:
            x, y = x + self.err[0], y + self.err[1]
            xi, yi = int(round(x)), int(round(y))
            if 7 <= xi < W - 8 and 7 <= yi < H - 8:
                img[yi - 7:yi + 8, xi - 7:xi + 8] += self.transparency * a * np.exp(
                    -((self.xx + xi - x) ** 2 + (self.yy + yi - y) ** 2) / (2 * 1.5 ** 2))
        return img


def run(lock, sky, seconds, t0, fps=20):
    t = t0
    for _ in range(int(seconds * fps)):
        t += 1 / fps
        lock.process(sky.frame(1 / fps), t, utc_s=t)
    return t


def new_lock(sky, **kw):
    s = gd.Settings(**{"lost_after_s": 3.0, **kw})
    return gd.StarLock(s, pulse=sky.pulse, full_scale=65535, log=lambda m: None)


def test_lock_stars_avoid_target_edge_and_saturation():
    sky = Sky()
    img = sky.frame(0)
    img[50, 60] = 65535                                  # the brightest star saturated
    lock = new_lock(sky)
    assert lock.lock(img, 0.0, target=TARGET[:2])
    xs = [(round(r["x"]), round(r["y"])) for r in lock.ref]
    assert len(xs) == 3 and (60, 50) not in xs and (200, 110) not in xs
    assert lock.state == gd.LOCKED and "3 stars" in lock.message


def test_hot_pixels_are_no_lock_stars():
    """A dark frame (lens cap): hot pixels only, nothing to lock on."""
    rng = np.random.default_rng(3)
    img = rng.normal(500, 10, (H, W)).astype(np.float32)
    for x, y in rng.integers(40, 200, (12, 2)):
        img[y, x] = 9000
    lock = gd.StarLock(gd.Settings(), full_scale=65535, log=lambda m: None)
    assert not lock.lock(img, 0.0) and "no lock star" in lock.message and lock.state == gd.IDLE


def test_a_noisy_dark_frame_has_no_lock_stars():
    """High gain, no stars: noise blobs are wider than half the box."""
    rng = np.random.default_rng(4)
    img = (128 + rng.gamma(1.2, 80, (H, W))).astype(np.float32)
    lock = gd.StarLock(gd.Settings(), full_scale=65535, log=lambda m: None)
    assert not lock.lock(img, 0.0) and lock.state == gd.IDLE


def test_many_hot_pixels_do_not_hide_the_stars():
    """IMX265 at 20 ms: hundreds of hot pixels, often several per box (they once made the stars
    look 'too narrow' and clusters of hot pixels were locked on)."""
    sky = Sky()
    img = sky.frame(0)
    rng = np.random.default_rng(5)
    for x, y in rng.integers(0, (W, H), (600, 2)):
        img[y, x] += rng.uniform(100, 2000)
    lock = new_lock(sky)
    assert lock.lock(img, 0.0, target=TARGET[:2])
    stars = {(round(x), round(y)) for x, y, _ in STARS}
    assert all((round(r["x"]), round(r["y"])) in stars for r in lock.ref), lock.ref


def test_error_follows_the_drift_without_guiding():
    sky = Sky(drift=(0.5, -0.3))
    lock = new_lock(sky)
    lock.lock(sky.frame(0), 0.0)
    run(lock, sky, 6, 0.0)
    assert lock.state == gd.LOCKED and not sky.pulses
    assert np.allclose(lock.offset, sky.err, atol=0.3)
    assert lock.rms() > 1


def test_calibrate_then_guide_holds_the_stars():
    sky = Sky(drift=(0.4, 0.25))
    lock = new_lock(sky)
    lock.lock(sky.frame(0), 0.0)
    assert lock.calibrate(0.0)
    t = run(lock, sky, 30, 0.0)
    assert lock.cal is not None and "calibrated" in lock.note, lock.note
    assert np.allclose(lock.cal, np.column_stack([sky.dirs["N"], sky.dirs["W"]]), rtol=0.15, atol=3e-4)
    lock.guide = True
    t = run(lock, sky, 50, t)
    unguided = np.hypot(0.4, 0.25) * 50
    assert lock.state == gd.LOCKED and lock.rms(20) < 1.0 < unguided    # (1 px saw tooth: 0.47 px/s)
    assert lock.guide and lock.pulses > 5


def test_clouds_hold_without_pulses_then_lock_again():
    sky = Sky(drift=(0.3, 0.0))
    lock = new_lock(sky, lost_after_s=60)
    lock.lock(sky.frame(0), 0.0)
    lock.cal, lock.guide = np.column_stack([sky.dirs["N"], sky.dirs["W"]]), True
    t = run(lock, sky, 5, 0.0)
    sky.transparency = 0.3                              # all stars at 30 %
    t = run(lock, sky, 2, t)                            # (a window from before may still pulse)
    n = len(sky.pulses)
    t = run(lock, sky, 5, t)
    assert lock.state == gd.HOLD and len(sky.pulses) == n
    sky.transparency = 1.0
    run(lock, sky, 5, t)
    assert lock.state == gd.LOCKED


def test_one_star_gone_is_fine_two_gone_hold():
    sky = Sky()
    lock = new_lock(sky, lost_after_s=60)
    lock.lock(sky.frame(0), 0.0)
    gone = [i for i, s in enumerate(STARS) if any(abs(s[0] - r["x"]) < 2 and abs(s[1] - r["y"]) < 2
                                                  for r in lock.ref)]
    t = run(lock, sky, 2, 0.0)
    sky.hidden = {gone[0]}
    t = run(lock, sky, 4, t)
    assert lock.state == gd.LOCKED and lock.stars_ok == 2
    sky.hidden = set(gone[:2])
    run(lock, sky, 4, t)
    assert lock.state == gd.HOLD


def test_lost_then_found_on_the_same_pattern_only():
    sky = Sky()
    lock = new_lock(sky)
    lock.lock(sky.frame(0), 0.0)
    t = run(lock, sky, 2, 0.0)
    sky.transparency = 0.0                              # thick cloud
    t = run(lock, sky, 6, t)
    assert lock.state == gd.LOST
    sky.err += (18.0, -11.0)                            # the mount drifted meanwhile
    sky.transparency = 1.0
    run(lock, sky, 3, t)
    assert lock.state == gd.LOCKED and "found again" in lock.note
    assert np.allclose(lock.offset, sky.err, atol=0.5)


def test_guiding_stops_when_corrections_make_it_worse():
    sky = Sky(drift=(0.3, 0.0), flip=True)              # the mount moves the wrong way
    lock = new_lock(sky, lost_after_s=60)
    lock.lock(sky.frame(0), 0.0)
    lock.cal, lock.guide = np.column_stack([sky.dirs["N"], sky.dirs["W"]]), True
    run(lock, sky, 30, 0.0)
    assert not lock.guide and "grow" in lock.note


def test_live_flux_shows_the_target_drop():
    sky = Sky()
    lock = new_lock(sky)
    lock.lock(sky.frame(0), 0.0, target=TARGET[:2])
    t = run(lock, sky, 3, 0.0)
    before = np.median([r for _, r in lock.history])
    sky.target_level = 0.4
    run(lock, sky, 3, t)
    after = np.median([r for _, r in list(lock.history)[-40:]])
    assert after / before == pytest.approx(0.4, abs=0.08)
    assert [k for *_, k in lock.boxes()].count("target") == 1
