"""Capture recording formats: RAVF / SER writers, crash recovery, repair, recorder thread."""
import calendar
import os
import shutil
import struct
import time

import numpy as np
import pytest

from pystarlight import movie
from pystarlight.capture import ravf_writer, recorder, ser_writer, timing
from pystarlight.capture.timing import GpsClock, PulseStamper, exposure_start

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
    # VEYE GPS: pulse + 0.9967 x delay + (period - exposure) + 0.51 ms
    assert exposure_start(0, pulse, 8000, 1000, True, 30_120_000) == \
        pulse + 996_700 + 30_120_000 - 8_000_000 + 510_000
    assert exposure_start(pulse, None, 8000, None, False) == pulse - 8_000_000


S0 = 1_792_000_000 * 10**9       # a whole UTC second (ns)
TRUE_OFF = S0 - 5_000 * 10**9    # UTC - CLOCK_MONOTONIC (the truth)
LAG = 33_000_000                 # pulse -> frame start
BURST = 80_000_000               # epoch -> start of the GPS message burst


def anchored_stamper(monkeypatch, pi_error_ns=400_000_000):
    """A 1 Hz stamper with a GpsClock, anchored on 4 seconds of pulses and GPS labels. The Pi
    clock is off by pi_error_ns: GPS stamps must not depend on it."""
    monkeypatch.setattr(timing, "pi_minus_mono_ns", lambda: TRUE_OFF + pi_error_ns)
    clock = GpsClock()
    st = PulseStamper(1, LAG, clock)
    out = []
    for k in range(4):
        pulse_mono = S0 + k * 10**9 - TRUE_OFF
        out.append(st(pulse_mono + LAG + 50_000 + TRUE_OFF + pi_error_ns))
        clock.label(pulse_mono + BURST, S0 + k * 10**9 + 1234)
    return st, clock, out


def test_gps_clock_anchors_without_pi_clock(monkeypatch):
    st, clock, out = anchored_stamper(monkeypatch)
    assert out[:3] == [None, None, None]       # 3 labelled seconds before anchoring
    assert clock.anchored and not clock.suspect
    assert out[3] == S0 + 3 * 10**9
    assert abs(clock.burst_lag_ns - BURST) < 100_000
    frame = S0 + 4 * 10**9 - TRUE_OFF + LAG + TRUE_OFF + 400_000_000
    assert st(frame) == S0 + 4 * 10**9


def test_gps_clock_needs_consistent_seconds(monkeypatch):
    monkeypatch.setattr(timing, "pi_minus_mono_ns", lambda: TRUE_OFF)
    clock = GpsClock()
    st = PulseStamper(1, LAG, clock)
    for k in range(4):                          # GPS seconds skip: no anchoring
        pulse_mono = S0 + k * 10**9 - TRUE_OFF
        st(pulse_mono + LAG + TRUE_OFF)
        clock.label(pulse_mono + BURST, S0 + 2 * k * 10**9)
    assert not clock.anchored
    clock.reset()
    for k in range(4):                          # bursts too late after the pulse: no anchoring
        pulse_mono = S0 + k * 10**9 - TRUE_OFF
        st(pulse_mono + LAG + TRUE_OFF)
        clock.label(pulse_mono + 700_000_000, S0 + k * 10**9)
    assert not clock.anchored


def test_gps_clock_anchors_on_every_other_pulse(monkeypatch):
    monkeypatch.setattr(timing, "pi_minus_mono_ns", lambda: TRUE_OFF)
    clock = GpsClock()
    st = PulseStamper(1, LAG, clock)
    for k in range(8):                          # long trigger delay: a frame every 2nd pulse
        pulse_mono = S0 + k * 10**9 - TRUE_OFF
        if k % 2 == 0:
            st(pulse_mono + LAG + TRUE_OFF)
        clock.label(pulse_mono + BURST, S0 + k * 10**9)
    assert clock.anchored
    assert st(S0 + 8 * 10**9 + LAG) == S0 + 8 * 10**9


def test_gps_clock_flags_wrong_second(monkeypatch):
    st, clock, _ = anchored_stamper(monkeypatch)
    for k in range(4, 7):                       # GPS now names the next second: inconsistent
        clock.label(S0 + k * 10**9 - TRUE_OFF + BURST, S0 + (k + 1) * 10**9)
    assert clock.suspect and clock.stale()


def test_rate_change_stamps_both_grids_then_new(monkeypatch):
    st, clock, _ = anchored_stamper(monkeypatch, pi_error_ns=0)
    pi = TRUE_OFF                               # frame_ns = mono + pi (Pi clock right here)

    def frame(utc_pulse):
        return utc_pulse - TRUE_OFF + LAG + pi

    st.change_rate(30)
    t5 = S0 + 5 * 10**9
    assert st(frame(t5)) == t5                  # old 1 Hz pulse
    p = t5 + (7 * 10**9 + 15) // 30              # GPS already switched: 7/30 s later
    assert st(frame(p)) == p
    assert not st.settled
    st.rate_confirmed(t5 + 300_000_000 - TRUE_OFF)
    q = t5 + 12 * 10**9 // 30                    # 400 ms: after the ACK
    assert st(frame(q)) == q and st.settled and st.rates == [30]
    assert st.off_grid == 0
    st.cancel_change()
    assert st.rates == [30]


def test_gps_clock_follows_pi_clock_drift(monkeypatch):
    st, clock, _ = anchored_stamper(monkeypatch, pi_error_ns=0)
    st.change_rate(30)
    st.rate_confirmed(S0 + 4 * 10**9 - TRUE_OFF)
    drift = 30e-6                               # the Pi's monotonic clock runs 30 ppm fast
    t_anchor = S0 + 3 * 10**9
    for i in range(30 * 300):                   # 5 minutes at 30 Hz
        utc = S0 + 5 * 10**9 + (i * 10**9 + 15) // 30
        mono = utc - TRUE_OFF + round((utc - t_anchor) * drift)
        assert st(mono + LAG + TRUE_OFF) == utc
    assert abs(clock.drift + drift) < 1e-6        # offset UTC - Pi falls
    assert st.off_grid == 0


def test_parse_rate():
    from fractions import Fraction

    from pystarlight.capture.timing import parse_rate, rate_str
    assert parse_rate("30") == parse_rate("30 fps") == parse_rate("30Hz") == 30
    assert parse_rate("250 ms") == parse_rate("0.25 s") == 4
    assert parse_rate("1.5") == parse_rate("666.667 ms") == Fraction(10**6, 666_667)
    assert parse_rate("0,5") == parse_rate("2 s") == Fraction(1, 2)
    assert rate_str(parse_rate("1.5")) == "1.5 fps (666.667 ms)"
    assert rate_str(30) == "30 fps"
    for bad in ("", "fast", "0", "5 ms", "2 min", "120 s"):
        with pytest.raises(ValueError):
            parse_rate(bad)


def test_non_whole_hz_rate_takes_grid_from_tim_tp(monkeypatch):
    from pystarlight.capture.timing import parse_rate
    st, clock, _ = anchored_stamper(monkeypatch, pi_error_ns=0)
    rate = parse_rate("1.5")
    period = 666_667_000
    st.change_rate(rate)
    st.rate_confirmed(S0 + 4 * 10**9 - TRUE_OFF)
    origin = S0 + 4 * 10**9 + 123_456_789          # the GPS's own phase for this period

    def frame(utc_pulse):
        return utc_pulse - TRUE_OFF + LAG + TRUE_OFF

    first = origin + period
    assert st(frame(first)) is None                 # no TIM-TP since the switch yet
    clock.timepulse(origin + 2 * period, origin + 2 * period - TRUE_OFF - 300_000_000)
    for n in range(2, 40):
        assert st(frame(origin + n * period)) == origin + n * period
    assert st.grid_moved == 0 and st.off_grid == 0
    clock.timepulse(origin + 50 * period + 5_000_000, origin + 50 * period - TRUE_OFF)
    st(frame(origin + 41 * period))
    assert st.grid_moved == 1
    clock.timepulse(S0 + 60 * 10**9, S0 + 30 * 10**9 - TRUE_OFF)    # 30 s off: GPS time?
    assert clock.tp[0] != S0 + 60 * 10**9


def test_tim_tp():
    from pystarlight.capture.gps_ublox import tim_tp
    week, tow_ms = 2441, 345_600_123
    sub = 2**31                                      # + 0.5 ms
    p = struct.pack("<IIiHBB", tow_ms, sub, 0, week, 0x03, 0)
    assert tim_tp(p) == (315_964_800 + week * 604_800) * 10**9 + tow_ms * 10**6 + 500_000
    assert tim_tp(struct.pack("<IIiHBB", tow_ms, sub, 0, week, 0x02, 0)) is None   # GNSS time


def test_nav_timeutc():
    from pystarlight.capture.gps_ublox import nav_timeutc

    def payload(valid, nano=-1234):
        return struct.pack("<IIiHBBBBBB", 0, 50, nano, 2026, 10, 28, 21, 15, 7, valid)
    secs = calendar.timegm((2026, 10, 28, 21, 15, 7, 0, 0, 0))
    assert nav_timeutc(payload(0x07)) == secs * 10**9 - 1234
    assert nav_timeutc(payload(0x03)) is None   # UTC not valid (leap seconds unknown)
    assert nav_timeutc(b"\0" * 10) is None


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
