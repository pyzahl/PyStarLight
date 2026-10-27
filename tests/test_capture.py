"""Capture recording formats: RAVF / SER writers, crash recovery, repair, recorder thread."""
import os
import shutil
import time

import numpy as np
import pytest

from pystarlight import movie
from pystarlight.capture import ravf_writer, recorder, ser_writer
from pystarlight.capture.timing import PulseStamper, exposure_start

T0 = 1_792_000_000 * 10**9 + 123_456_000        # a UTC time in 2026, Unix ns
W, H = 40, 24


def frames(n, depth=12):
    rng = np.random.default_rng(1)
    return [rng.integers(0, 2**depth, (H, W), dtype=np.uint16) for _ in range(n)]


def record(writer_cls, path, imgs, close=True):
    w = writer_cls(str(path), W, H, 12, {"observer": "Tester", "instrument": "Cam",
                                         "telescope": "Scope", "gain_db": 6.0})
    for i, img in enumerate(imgs):
        w.write(img, T0 + i * 100_000_000, {"frame_id": 10 + i, "exposure_ns": 20_000_000,
                                            "satellites": 9, "fix": 3, "almanac": 0})
    if close:
        w.close()
    else:
        w.sync()
    return w


def check(reader, imgs):
    assert len(reader) == len(imgs)
    for i, img in enumerate(imgs):
        fr = reader.read(i)
        np.testing.assert_array_equal(fr.image, img)
        assert fr.time.startswith(f"17:46:40.{123 + 100 * i:03d}")


# ---------------------------------------------------------------------------
# RAVF
# ---------------------------------------------------------------------------
def test_ravf_matches_ravf_package(tmp_path):
    ravf = pytest.importorskip("ravf")
    imgs = frames(5)
    path = tmp_path / "a.ravf"
    record(ravf_writer.RavfWriter, path, imgs)
    with open(path, "rb") as fh:
        r = ravf.RavfReader(fh)
        assert r.frame_count() == 5
        assert r.metadata_value("OBSERVER").txt == "Tester"
        assert r.metadata_value("IMAGE-BIT-DEPTH") == 12
        assert r.metadata_value("FRAMES-COUNT") == 5
        assert [ravf_writer.ravf_to_unix(t) for t in r.timestamps()] == \
            [T0 + i * 100_000_000 for i in range(5)]
        err, image, info, status = r.getPymovieMainImageAndStatusData(fh, 3)
        np.testing.assert_array_equal(image, imgs[3])
        assert status["sequence"] == 3 and status["satellites"] == 9
        assert status["satellite_fix_status"] == 3 and status["exposure_duration"] == 20_000_000
    rd = movie.open_movie(path)
    assert not rd.recovered
    check(rd, imgs)
    assert rd.read(0).time.startswith("17:46:40.123")    # 1_792_000_000 s = 17:46:40 UTC


def test_ravf_unfinished_is_read_and_repaired(tmp_path):
    ravf = pytest.importorskip("ravf")
    imgs = frames(6)
    live = tmp_path / "live.ravf"
    w = record(ravf_writer.RavfWriter, live, imgs, close=False)     # "crash": never closed
    crashed = tmp_path / "crashed.ravf"
    data = live.read_bytes()
    crashed.write_bytes(data[:len(data) - 100])                    # last frame cut short
    w.close()

    with pytest.warns(UserWarning, match="unfinished"):
        rd = movie.open_movie(crashed)
    assert rd.recovered
    check(rd, imgs[:5])
    rd.close()
    size = crashed.stat().st_size                       # reading left the file alone
    assert "repaired, 5 frames" in recorder.repair(str(crashed))
    assert crashed.stat().st_size < size + 4 + 16 * 5 + 1
    with open(crashed, "rb") as fh:
        r = ravf.RavfReader(fh)
        assert r.frame_count() == 5 and r.metadata_value("FRAMES-COUNT") == 5
    check(movie.open_movie(crashed), imgs[:5])
    assert "complete" in recorder.repair(str(crashed))


def test_ravf_no_frames(tmp_path):
    path = tmp_path / "empty.ravf"
    w = ravf_writer.RavfWriter(str(path), W, H, 12, {})
    w.sync()
    assert "repaired, 0 frames" in recorder.repair(str(path))
    w.close()


# ---------------------------------------------------------------------------
# SER
# ---------------------------------------------------------------------------
def test_ser_complete(tmp_path):
    imgs = frames(4)
    path = tmp_path / "a.ser"
    record(ser_writer.SerWriter, path, imgs)
    assert not os.path.exists(str(path) + ".times")
    rd = movie.open_movie(path)
    assert not rd.recovered and rd.metadata["NumTimestamps"] == 4
    check(rd, imgs)
    assert rd.read(1).time.startswith("17:46:40.223")
    assert "complete, 4 frames" in recorder.repair(str(path))


def test_ser_unfinished_keeps_timestamps(tmp_path, capsys):
    imgs = frames(5)
    live = tmp_path / "live.ser"
    w = record(ser_writer.SerWriter, live, imgs, close=False)
    crashed = tmp_path / "crashed.ser"
    data = live.read_bytes()
    crashed.write_bytes(data[:len(data) - 10])                     # last frame cut short
    shutil.copy(str(live) + ".times", str(crashed) + ".times")
    w.close()

    with pytest.warns(UserWarning, match="unfinished"):
        rd = movie.open_movie(crashed)
    check(rd, imgs[:4])
    assert rd.read(3).time.startswith("17:46:40.423")
    del rd
    recorder.main([str(crashed) + ".times"])            # the sidecar names its SER file
    assert "repaired, 4 frames" in capsys.readouterr().out
    assert not os.path.exists(str(crashed) + ".times")
    rd = movie.open_movie(crashed)
    assert not rd.recovered and rd.metadata["NumTimestamps"] == 4
    check(rd, imgs[:4])


# ---------------------------------------------------------------------------
# Recorder thread
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["ravf", "ser"])
def test_recorder_writes_frames(tmp_path, fmt):
    imgs = frames(8)
    rec = recorder.Recorder(lambda raw: np.frombuffer(raw, "<u2").reshape(H, W),
                            lambda: (W, H, 12), str(tmp_path), "cam", fmt=fmt)
    rec.arm(start_ns=T0, max_frames=6)
    for i, img in enumerate(imgs):
        if rec.wants(T0 + i):
            rec.put(img.tobytes(), i, T0 + i, T0 + i * 100_000_000, {"exposure_ns": 1000})
    deadline = time.monotonic() + 5
    while rec.frames < 6 and time.monotonic() < deadline:
        time.sleep(0.01)
    rec.close()
    assert rec.state == "idle" and rec.last_frames == 6
    assert rec.last_path.endswith("." + fmt)
    check(movie.open_movie(rec.last_path), imgs[:6])


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
def test_pulse_stamper_and_exposure_start():
    st = PulseStamper("10", lag_ns=33_000_000)
    pulse = 1_792_000_000 * 10**9 + 300_000_000
    assert st(pulse + 34_000_000) == pulse
    # VEYE GPS: pulse + 0.9967 x delay + (period - exposure) + 0.33 ms
    assert exposure_start(0, pulse, 8000, 1000, True, 30_120_000) == \
        pulse + 996_700 + 30_120_000 - 8_000_000 + 330_000
    assert exposure_start(pulse, None, 8000, None, False) == pulse - 8_000_000


# ---------------------------------------------------------------------------
# Review player: time stamps, problems (no GUI)
# ---------------------------------------------------------------------------
def test_review_timing_and_problems(tmp_path):
    pytest.importorskip("ravf")
    from pystarlight.capture.review import load_timing, problems
    imgs = frames(8)
    path = tmp_path / "gaps.ravf"
    w = ravf_writer.RavfWriter(str(path), W, H, 12, {})
    fid, t = 0, T0
    for i, img in enumerate(imgs):
        if i == 3:
            fid, t = fid + 2, t + 2 * 100_000_000     # two frames dropped
        if i == 6:
            t += 40_000_000                            # late frame
        w.write(img, t, {"frame_id": fid, "exposure_ns": 20_000_000, "satellites": 7, "fix": 3,
                         "almanac": 0})
        fid, t = fid + 1, t + 100_000_000
    w.close()
    times, info = load_timing(str(path))
    assert len(times) == 8 and times[0] == pytest.approx(T0 / 1e9)
    assert [d["sequence"] for d in info] == [0, 1, 2, 5, 6, 7, 8, 9]
    assert info[0]["satellites"] == 7 and info[0]["fix"] == 3 and info[0]["exposure_ms"] == 20.0
    flags, rep = problems(times, info)
    assert rep.step == pytest.approx(0.1, abs=1e-6)       # float64 Unix seconds: ~0.24 us
    assert set(flags) == {3, 6}
    assert "2 frame(s) missing" in flags[3] and "2 frame(s) dropped" in flags[3]
    assert flags[6] == "irregular step"


def test_review_timing_ser(tmp_path):
    from pystarlight.capture.review import load_timing, problems
    path = tmp_path / "a.ser"
    record(ser_writer.SerWriter, path, frames(5))
    times, info = load_timing(str(path))
    np.testing.assert_allclose(np.diff(times), 0.1, atol=1e-6)
    assert problems(times, info)[0] == {}


def test_review_command():
    from pystarlight.capture.gui import review_command
    cmd = review_command("/data/x.ravf")
    assert cmd[1:] == ["-m", "pystarlight.capture.review", "/data/x.ravf"]
