"""The Pi's system clock from GPS UTC (in the field there is no network, so no NTP).

The app itself runs on the GPS clock (timing.GpsClock, timing.utc_now_ns): time stamps, start
and stop times. This sets the system clock to it as well, for everything else: file times, the
mount's sidereal time, other programs, and the next boot (the helper also sets the RTC).

GpsClock is anchored only on GPS UTC that the GPS marks valid (leap seconds known: up to
12.5 min after a cold start), so the clock is never set from GPS time with a wrong leap second.
Its accuracy is that of the anchoring: the trigger pulses through the camera, i.e. the
sensor's pulse-to-frame lag model (IMX178 measured to ~0.1 ms; models not calibrated: up to a
few ms), plus the frame-start stamp jitter (~10 us).

Setting the clock needs root: settime_helper.py installed root-owned as HELPER and allowed
through sudo without a password (CAPTURE.md). It adds an offset, so the delay of sudo does not
matter. Not while recording: a step between a frame's two clock readings would misplace it.
"""
import os
import subprocess
import time

from .timing import pi_clock_error_ns

HELPER = "/usr/local/sbin/pystarlight-settime"
CHECK_EVERY_S = 60.0


def offset_str(ns):
    """+12.3 ms / -4.512 s / +3 h 12 min."""
    a = abs(ns)
    sign = "+" if ns >= 0 else "-"
    if a < 1e9:
        return f"{sign}{a / 1e6:.1f} ms"
    if a < 600e9:
        return f"{sign}{a / 1e9:.3f} s"
    if a < 86400e9:
        minutes = round(a / 60e9)
        return f"{sign}{minutes // 60} h {minutes % 60} min"
    return f"{sign}{a / 86400e9:.1f} days"


class SystemClock:
    """Checks the Pi clock against the GPS clock (right after anchoring, then every minute)
    and steps it when it is off by more than tolerance_ms."""

    def __init__(self, tolerance_ms=20.0, enabled=True, helper=HELPER, run=subprocess.run):
        self.tolerance_ns = int(tolerance_ms * 1e6)
        self.enabled = enabled
        self.helper = helper
        self.run = run
        self.error_ns = None        # last measured Pi clock - GPS UTC
        self.steps = []             # offsets applied (ns)
        self.failed = None          # why the helper cannot be used (then not tried again)
        self.message = "not checked against GPS UTC yet"
        self._next = 0.0            # time.monotonic() of the next check
        self._anchor = None         # GpsClock.anchored_at seen last (a new anchor: check now)

    def check(self, clock, busy=False, now=None):
        """Status timer. clock: the GpsClock (None: no GPS); busy: recording (no step).
        Returns the message for the GUI."""
        now = time.monotonic() if now is None else now
        anchor = getattr(clock, "anchored_at", None)
        if anchor != self._anchor:
            self._anchor, self._next = anchor, 0.0
        if now < self._next:
            return self.message
        self._next = now + CHECK_EVERY_S
        err = pi_clock_error_ns(clock)
        if err is None:
            if self.error_ns is None:
                self.message = ("not checked: no GPS" if clock is None else
                                "not checked against GPS UTC yet (GPS clock not anchored)")
            return self.message
        self.error_ns = err
        if abs(err) <= self.tolerance_ns:
            self.message = f"within {offset_str(err)} of GPS UTC"
        elif not self.enabled:
            self.message = f"off GPS UTC by {offset_str(err)} (setting it is off in the config)"
        elif self.failed:
            self.message = f"off GPS UTC by {offset_str(err)}; not set: {self.failed}"
        elif busy:
            self.message = f"off GPS UTC by {offset_str(err)}; set after the recording"
            self._next = now + 2.0
        else:
            self.step(-err)
        return self.message

    def step(self, offset_ns):
        if not os.path.exists(self.helper):
            self.failed = f"{self.helper} is not installed (CAPTURE.md)"
        else:
            try:
                r = self.run(["sudo", "-n", self.helper, str(int(offset_ns))],
                             capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    self.steps.append(int(offset_ns))
                    self.error_ns = None
                    self.message = f"set from GPS UTC (stepped {offset_str(offset_ns)})"
                    print(f"System clock set from GPS UTC: stepped {offset_str(offset_ns)}"
                          + (f" ({r.stdout.strip()})" if r.stdout.strip() else ""))
                    self._next = time.monotonic() + 2.0     # check the result soon
                    return
                self.failed = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
            except (OSError, subprocess.TimeoutExpired) as e:
                self.failed = str(e)
        self.message = f"off GPS UTC by {offset_str(-offset_ns)}; not set: {self.failed}"
        print(f"System clock: {self.message}")
