"""Frame time stamps: GPS trigger pulses, VEYE exposure-start calibration, UTC helpers.

GPS trigger: the GPS timepulse (PPS pin) is wired to the camera's trigger input and set to the
chosen frame rate; every frame is then the pulse of a known UTC instant, and is stamped with
that instant (PulseStamper) instead of the Pi's clock.
"""
from datetime import datetime, timedelta, timezone
from fractions import Fraction

GPS_RATES = ["30", "20", "15", "10", "8", "6", "5", "4", "3", "2", "1", "0.5"]     # fps
LAG_EXTRA_NS = 3_000_000    # VEYE: frame start comes frame period + this after the trigger
# Row-0 exposure start of a VEYE MV-MIPI-IMX178M frame (rolling shutter), measured with the
# GPS timepulse LED in view (LED lit 0..100 ms after each pulse; frames catching its edges
# mid-exposure, 8 and 20 ms exposures agreeing to ~0.1 ms):
#   free run:     frame_ns - exposure - 3.33 ms   (frame_ns: CSI-2 frame start, Pi clock)
#   GPS trigger:  pulse + trigger delay + (frame period - exposure) + 0.33 ms   (no Pi clock)
# Row y starts y * 14.33 us after row 0 (29.6 ms over all 2064 rows). Measured for 12 bit only;
# 10 and 8 bit read out faster: their line times are estimated from the minimum frame periods
# (frame period = rows x line + ~0.5 ms, which fits 12 bit at both full frame and 1280x720),
# and the two offsets are assumed unchanged - not calibrated.
VEYE_FREE_LEAD_NS = 3_330_000
VEYE_TRIGGER_OFFSET_NS = 330_000
VEYE_LINE_NS = {12: 14_330, 10: 12_130, 8: 11_800}      # by bit depth
VEYE_CALIBRATED_BITS = (12,)
# The camera's trigger-delay clock runs 0.33 % fast: real delay = register (us) x this. Measured
# 2026-10-26 with the GPS LED (12 bit, full frame, 2 ms): its on edge one pulse later needed
# 970.94 ms in the register for 968.10 ms real, two pulses later 1974.70 ms for 1968.10 ms;
# its off edge (100 ms after the pulse) agreed within the 0.5 ms steps.
VEYE_DELAY_SCALE = 0.9967


def veye_line_ns(bits):
    """Rolling-shutter line time of a VEYE IMX178 at this bit depth."""
    return VEYE_LINE_NS.get(bits, VEYE_LINE_NS[12])


def parse_utc(text):
    """'HH:MM[:SS[.f]]' -> next such UTC time; 'YYYY-MM-DDTHH:MM:SS[.f]' -> that UTC time."""
    now = datetime.now(timezone.utc)
    if "T" in text or "-" in text:
        t = datetime.fromisoformat(text.rstrip("Z")).replace(tzinfo=timezone.utc)
    else:
        parts = [float(v) for v in text.split(":")]
        h, m, sec = (parts + [0, 0])[:3]
        t = now.replace(hour=int(h), minute=int(m), second=int(sec),
                        microsecond=int(round((sec % 1) * 1e6)))
        if t < now:
            t += timedelta(days=1)
    return t


def utc_str(ns, digits=3):
    t = datetime.fromtimestamp(ns / 1e9, timezone.utc)
    return t.strftime("%H:%M:%S.%f")[:9 + digits] if digits else t.strftime("%H:%M:%S")


def trigger_delay_ns(delay_us, is_veye):
    """The real delay from trigger pulse to exposure; the setting is in camera µs."""
    d = (delay_us or 0) * 1000
    return d * VEYE_DELAY_SCALE if is_veye else d


def onset_delay_us(row, rate, period_ns, exp_ns, line_ns):
    """VEYE trigger delay (camera µs) that centres image row `row`'s exposure on the next
    timepulse, i.e. on the GPS LED switching on. Row y starts exposing at pulse + delay +
    (frame period - exposure) + VEYE_TRIGGER_OFFSET_NS + y * line."""
    lead = period_ns - exp_ns + VEYE_TRIGGER_OFFSET_NS + row * line_ns + exp_ns / 2
    return (-lead) % (1e9 / rate) / VEYE_DELAY_SCALE / 1000


def pulses_per_frame(rate, period_ns, delay_ns):
    """VEYE: a trigger that comes while the camera is busy (its delay + about two frame periods
    + 3 ms after the last trigger) is lost, so the camera takes every n-th pulse."""
    return int((delay_ns + 2 * period_ns + LAG_EXTRA_NS) * rate / 1e9) + 1


def exposure_start(frame_ns, pulse_ns, exposure_us, delay_us, is_veye, period_ns=None):
    """Row-0 exposure start (UTC ns): from the trigger pulse when GPS-triggered (exact, no Pi
    clock), else from the frame-start time (Pi clock). period_ns: the VEYE frame period.
    Cameras other than VEYE: not measured; assumes frame start - exposure, or the pulse
    (+ delay)."""
    exp_ns = exposure_us * 1000
    if pulse_ns is None:
        return int(frame_ns - exp_ns - (VEYE_FREE_LEAD_NS if is_veye else 0))
    pulse_ns += int(trigger_delay_ns(delay_us, is_veye))
    if is_veye:
        return int(pulse_ns + period_ns - exp_ns + VEYE_TRIGGER_OFFSET_NS)
    return pulse_ns


class PulseStamper:
    """Finds the pulse of a UTC-aligned pulse train (GPS timepulse) that triggered a frame.

    A frame starts on the CSI-2 link a steady lag after its pulse (VEYE: the camera's frame
    period + ~3 ms, measured). Frame time minus that lag, rounded to the pulse grid (k/rate
    seconds, aligned to the UTC second), is the pulse - right while the lag guess and the
    system clock (NTP: ~1 ms) are together off by less than half a pulse period. The lag
    guess then follows the measured lag.
    """

    def __init__(self, rate, lag_ns):
        self.rate = Fraction(rate)          # pulses per second, exact (e.g. '0.5')
        self.period_ns = 1e9 / float(self.rate)
        self.lag_ns = float(lag_ns)
        self.last_lag_ns = None             # measured frame start - pulse
        self.stride = 1                     # pulses per frame (the camera skips the others)

    def __call__(self, frame_ns):
        k = round((frame_ns - self.lag_ns) * self.rate / 10**9)
        pulse = round(k * 10**9 / self.rate)
        lag = frame_ns - pulse
        self.last_lag_ns = lag
        if abs(lag - self.lag_ns) < self.period_ns / 4:
            self.lag_ns += (lag - self.lag_ns) / 8
        return pulse
