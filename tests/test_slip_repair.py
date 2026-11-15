"""TEMPORARY: slip_repair (VEYE IMX265 word slips). Delete with src/pystarlight/slip_repair.py."""
import json

import numpy as np
import pytest

from pystarlight import slip_repair
from pystarlight.capture.ravf_writer import RavfWriter
from pystarlight.capture.slipguard import pack_mipi, unpack_mipi_rows
from pystarlight.movie import open_movie

W, H, N = 640, 96, 12


def frames(seed=3):
    """Dark sky with noise and a few faint stars, a new noise draw per frame."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:H, :W]
    stars = sum(a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 6)
                for x, y, a in [(80, 20, 300), (333, 50, 120), (600, 80, 900)])
    return [np.clip(rng.normal(120, 4, (H, W)) + stars, 0, 4095).astype(np.uint16) for _ in range(N)]


def camera(truth, slips_at, k0):
    """The camera's continuous stream: k0 slips before the start, then 8 bytes repeated at
    each position in slips_at (bytes into the whole stream); cut into frames, unpacked per
    line as the Pi does."""
    stream = np.concatenate([pack_mipi(t, 12).ravel() for t in truth])
    out = [np.zeros(8 * k0, np.uint8)]
    last = 0
    for p in sorted(slips_at):
        out += [stream[last:p], stream[p - 8:p]]       # a word repeated
        last = p
    out.append(stream[last:])
    s = np.concatenate(out)
    per = truth[0].size * 3 // 2
    return [unpack_mipi_rows(s[i * per:(i + 1) * per].reshape(H, -1), 12) for i in range(len(truth))]


def write_ravf(path, imgs):
    w = RavfWriter(str(path), W, H, 12, {})
    for i, im in enumerate(imgs):
        w.write(im, 1_700_000_000_000_000_000 + i * 66_666_667, {"frame_id": i})
    w.close()


def read(path):
    m = open_movie(str(path))
    try:
        return [m.read(i).image.copy() for i in range(len(m))], m.timestamps
    finally:
        m.close()


@pytest.mark.parametrize("k0", [0, 1, 2])
def test_repair_restores_frames(tmp_path, k0):
    truth = frames()
    per = W * H * 3 // 2
    # slips: mid-frame (frame 2), two in frame 5, one at a frame boundary, one late in frame 9
    slips = [2 * per + 41_000, 5 * per + 9_000, 5 * per + 70_000, 7 * per, 9 * per + per - 2_000]
    src = tmp_path / "rec.ravf"
    write_ravf(src, camera(truth, slips, k0))
    rep = slip_repair.repair(str(src), progress=False)
    assert rep["slips_in_recording"] == len(slips)
    assert rep["slips_before_start_assumed"] == k0
    fixed, ts = read(rep["output"])
    _, ts0 = read(src)
    assert list(ts) == list(ts0)                        # time stamps untouched
    for i, (f, t) in enumerate(zip(fixed, truth)):
        bad = (f != t).mean()
        assert bad < 0.004, (i, bad)                    # only the bytes around slips / the tail
    saved = json.loads(open(rep["output"] + ".slips.json").read())
    assert saved["frames_changed"] == rep["frames_changed"]


def test_slips_before_the_start_leave_16px_steps_and_shift_takes_them(tmp_path):
    truth = frames()
    src = tmp_path / "rec.ravf"
    write_ravf(src, camera(truth, [], 3 * 9 + 2))       # 29 slips: 144 px + 2 slips
    rep = slip_repair.repair(str(src), progress=False)
    assert rep["slips_before_start_assumed"] == 2
    fixed, _ = read(rep["output"])
    # content moved right by 144 px along the stream (wrapping into the row below)
    exp = np.concatenate([np.zeros(144, np.uint16), truth[3].ravel()])[:W * H].reshape(H, W)
    assert (fixed[3][2:, :] == exp[2:, :]).mean() > 0.99
    rep = slip_repair.repair(str(src), out=str(tmp_path / "s.ravf"), shift="144", progress=False)
    fixed, _ = read(rep["output"])
    assert (fixed[3] != truth[3]).mean() < 0.004


def test_check_writes_nothing(tmp_path):
    src = tmp_path / "rec.ravf"
    write_ravf(src, camera(frames(), [W * H * 3 // 2 * 4 + 500], 0))
    rep = slip_repair.repair(str(src), check=True, progress=False)
    assert rep["output"] is None and rep["slips_in_recording"] == 1
    assert not list(tmp_path.glob("*.repaired.*"))
