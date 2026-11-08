"""The app's clock (GPS UTC once anchored) and the Pi system clock set from it."""
import subprocess
import time

import pytest

from pystarlight.capture import recorder, settime_helper, sysclock
from pystarlight.capture.timing import GpsClock, pi_clock_error_ns, utc_now_ns

HOUR = 3600 * 10**9


class FakeClock:
    """A GpsClock stand-in: UTC = CLOCK_MONOTONIC + offset, chosen relative to the Pi clock."""

    def __init__(self, pi_error_ns, fresh=True):
        self.offset = time.clock_gettime_ns(time.CLOCK_REALTIME) - time.monotonic_ns() - pi_error_ns
        self.fresh = fresh
        self.anchored_at = 1

    def stale(self):
        return not self.fresh

    def utc(self, mono_ns):
        return mono_ns + self.offset


def test_utc_now_is_gps_utc_once_anchored():
    clock = FakeClock(pi_error_ns=3 * HOUR)       # Pi clock 3 h ahead
    assert abs(utc_now_ns(clock) - (time.time_ns() - 3 * HOUR)) < 50_000_000
    assert abs(pi_clock_error_ns(clock) - 3 * HOUR) < 50_000_000


def test_utc_now_falls_back_to_the_pi_clock():
    for clock in (None, FakeClock(3 * HOUR, fresh=False), GpsClock()):
        assert abs(utc_now_ns(clock) - time.time_ns()) < 50_000_000
        assert pi_clock_error_ns(clock) is None


def runner(calls, returncode=0, stderr=""):
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "stepped", stderr)
    return run


@pytest.fixture
def helper(tmp_path):
    p = tmp_path / "pystarlight-settime"
    p.write_text("")
    return str(p)


def test_system_clock_stepped_by_the_gps_error(helper):
    calls = []
    sc = sysclock.SystemClock(20.0, helper=helper, run=runner(calls))
    msg = sc.check(FakeClock(-2 * HOUR), now=0.0)  # Pi clock 2 h behind (fake-hwclock)
    assert calls and calls[0][:3] == ["sudo", "-n", helper]
    assert abs(int(calls[0][3]) - 2 * HOUR) < 50_000_000
    assert "stepped +2 h 0 min" in msg


def test_system_clock_within_tolerance_untouched(helper):
    calls = []
    sc = sysclock.SystemClock(20.0, helper=helper, run=runner(calls))
    assert sc.check(FakeClock(0), now=0.0).startswith("within")
    assert not calls


def test_system_clock_not_while_recording_nor_unanchored(helper):
    calls = []
    sc = sysclock.SystemClock(20.0, helper=helper, run=runner(calls))
    assert "not anchored" in sc.check(GpsClock(), now=0.0)
    assert "after the recording" in sc.check(FakeClock(5 * 10**9), busy=True, now=100.0)
    assert not calls
    sc.check(FakeClock(5 * 10**9), now=103.0)      # recording over: set now
    assert len(calls) == 1


def test_system_clock_checks_on_a_new_anchor_then_every_minute(helper):
    calls = []
    sc = sysclock.SystemClock(20.0, helper=helper, run=runner(calls, returncode=1))
    clock = FakeClock(0)
    sc.check(clock, now=0.0)
    clock.offset -= 10**9                           # Pi clock now 1 s ahead
    assert sc.check(clock, now=30.0).startswith("within")     # not due yet
    clock.anchored_at = 2                           # anchored again: checked at once
    assert "off GPS UTC by +1.000 s" in sc.check(clock, now=31.0)


def test_system_clock_reports_a_missing_helper_or_sudo(tmp_path, helper):
    sc = sysclock.SystemClock(20.0, helper=str(tmp_path / "missing"))
    assert "is not installed" in sc.check(FakeClock(HOUR), now=0.0)
    calls = []
    sc = sysclock.SystemClock(20.0, helper=helper,
                              run=runner(calls, 1, "sudo: a password is required"))
    assert "a password is required" in sc.check(FakeClock(HOUR), now=0.0)
    sc.check(FakeClock(HOUR), now=100.0)            # not tried again
    assert len(calls) == 1


def test_system_clock_off_in_the_config(helper):
    calls = []
    sc = sysclock.SystemClock(20.0, enabled=False, helper=helper, run=runner(calls))
    assert "off in the config" in sc.check(FakeClock(HOUR), now=0.0)
    assert not calls


def test_offset_str():
    assert sysclock.offset_str(12_300_000) == "+12.3 ms"
    assert sysclock.offset_str(-4_512_000_000) == "-4.512 s"
    assert sysclock.offset_str(3 * HOUR + 12 * 60 * 10**9) == "+3 h 12 min"


def test_settime_helper_refuses_bad_offsets(capsys):
    assert settime_helper.main(["x"]) == 2
    assert settime_helper.main(["x", "soon"]) == 2
    assert settime_helper.main(["x", str(25 * 365 * 24 * HOUR)]) == 2


def test_recorder_start_now_on_the_app_clock(tmp_path):
    rec = recorder.Recorder(lambda raw: raw, lambda: (8, 8, 12), str(tmp_path), "t")
    rec.now = lambda: 42 * 10**18
    rec.arm()
    assert rec.start_ns == 42 * 10**18
