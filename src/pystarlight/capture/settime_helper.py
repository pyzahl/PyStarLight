#!/usr/bin/python3
"""Root helper of the camera app: add OFFSET_NS to the system clock, then copy it to the RTC.

Installed root-owned as /usr/local/sbin/pystarlight-settime and allowed through sudo without a
password (CAPTURE.md, "System clock from the GPS"); the app (sysclock.py) runs it when the
Pi clock is off GPS UTC. An offset instead of an absolute time: the clock is read and set
within microseconds here, so the delay of sudo and of starting Python does not matter.

    pystarlight-settime OFFSET_NS

Standard library only (the system's python3). Never run from a user-writable path through
sudo: anyone who can edit the file could then run anything as root.
"""
import ctypes
import fcntl
import sys
import time

LIMIT_NS = 20 * 365 * 86400 * 10**9     # sanity: no step of more than ~20 years
RTC_DEV = "/dev/rtc0"


class _RtcTime(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int) for name in
                ("tm_sec", "tm_min", "tm_hour", "tm_mday", "tm_mon", "tm_year",
                 "tm_wday", "tm_yday", "tm_isdst")]


RTC_SET_TIME = (1 << 30) | (ctypes.sizeof(_RtcTime) << 16) | (ord("p") << 8) | 0x0A   # _IOW


def set_rtc(ns):
    """The RTC (UTC, whole seconds) from the system clock; it keeps the time over a power-off
    only with a battery on the Pi 5's J5. Not fatal when there is none."""
    t = time.gmtime(ns // 10**9)
    rtc = _RtcTime(t.tm_sec, t.tm_min, t.tm_hour, t.tm_mday, t.tm_mon - 1, t.tm_year - 1900,
                   0, 0, 0)
    try:
        with open(RTC_DEV, "wb", buffering=0) as f:
            fcntl.ioctl(f, RTC_SET_TIME, rtc)
        return True
    except OSError as e:
        print(f"RTC not set ({e})", file=sys.stderr)
        return False


def main(argv):
    try:
        offset = int(argv[1])
    except (IndexError, ValueError):
        print(__doc__.strip().splitlines()[0], "\nusage: pystarlight-settime OFFSET_NS", file=sys.stderr)
        return 2
    if abs(offset) > LIMIT_NS:
        print(f"refused: offset {offset} ns is more than 20 years", file=sys.stderr)
        return 2
    now = time.clock_gettime_ns(time.CLOCK_REALTIME)
    time.clock_settime_ns(time.CLOCK_REALTIME, now + offset)
    new = time.clock_gettime_ns(time.CLOCK_REALTIME)
    rtc = set_rtc(new)
    print(f"system clock stepped by {offset / 1e9:+.6f} s"
          + ("; RTC set" if rtc else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
