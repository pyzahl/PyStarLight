"""Frame time stamps: GPS trigger pulses, VEYE exposure-start calibration, UTC helpers.

GPS trigger: the GPS timepulse (PPS pin) is wired to the camera's trigger input and set to the
chosen frame rate; every frame is then the pulse of a known UTC instant, and is stamped with
that instant (PulseStamper). Which pulse it was comes from GpsClock (GPS UTC only); without a
GPS, from the Pi's clock (NTP).
"""
import collections
import dataclasses
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction

GPS_RATES = ["30", "20", "15", "10", "8", "6", "5", "4", "3", "2", "1.5", "1", "0.5"]     # fps
MIN_PERIOD_US, MAX_PERIOD_US = 10_000, 60_000_000      # entered rates: 100 fps .. 1 per minute
LAG_EXTRA_NS = 3_000_000    # VEYE: frame start comes frame period + this after the trigger
# Row-0 exposure start of a VEYE MV-MIPI-IMX178M frame (rolling shutter), measured with the
# GPS timepulse LED in view (LED lit 0..100 ms after each pulse; frames catching its edges
# mid-exposure, 8 and 20 ms exposures agreeing to ~0.1 ms):
#   free run:     frame_ns - exposure - 3.33 ms   (frame_ns: CSI-2 frame start, Pi clock)
#   GPS trigger:  pulse + trigger delay + (frame period - exposure) + 0.51 ms   (no Pi clock)
# The GPS-trigger offset was 0.33 ms; re-measured 2026-10-28 with a 1 ms timepulse (LED band with
# both edges in a 568/832-row ROI at 30 fps, 2 ms exposure): exposure start 0.18 ms and end
# 0.14 ms later than with 0.33 ms (end: also 0.13-0.17 ms at 1, 1.5, 21, 26 ms exposure, rows
# 50..1560, trigger delays 26..69 ms), the real exposure matching the setting to ~0.05 ms.
# Row y starts y * 14.33 us after row 0 (29.6 ms over all 2064 rows). Measured for 12 bit only;
# 10 and 8 bit read out faster: their line times are estimated from the minimum frame periods
# (frame period = rows x line + ~0.5 ms, which fits 12 bit at both full frame and 1280x720),
# and the two offsets are assumed unchanged - not calibrated.
VEYE_FREE_LEAD_NS = 3_330_000
VEYE_TRIGGER_OFFSET_NS = 510_000
VEYE_LINE_NS = {12: 14_330, 10: 12_130, 8: 11_800}      # by bit depth
VEYE_CALIBRATED_BITS = (12,)
# The camera's trigger-delay clock runs 0.33 % fast: real delay = register (us) x this. Measured
# 2026-10-26 with the GPS LED (12 bit, full frame, 2 ms): its on edge one pulse later needed
# 970.94 ms in the register for 968.10 ms real, two pulses later 1974.70 ms for 1968.10 ms;
# its off edge (100 ms after the pulse) agreed within the 0.5 ms steps.
VEYE_DELAY_SCALE = 0.9967


def parse_rate(text):
    """'30', '30 fps', '1.5 Hz', '250 ms', '2 s' -> pulses per second (Fraction). The GPS takes
    a frequency only in whole Hz, anything else as a period in whole us, so such a rate becomes
    10^6 / period_us (1.5 fps: 666667 us)."""
    t = str(text).strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d*\.?\d+)\s*(fps|hz|ms|s)?", t)
    if not m or Fraction(m.group(1)) == 0:
        raise ValueError(f"not a rate: '{text}' (e.g. 1.5 fps or 250 ms)")
    v, unit = Fraction(m.group(1)), m.group(2)
    rate = 1000 / v if unit == "ms" else 1 / v if unit == "s" else v
    if rate.denominator != 1:
        rate = Fraction(10**6, round(10**6 / rate))
    if not MIN_PERIOD_US <= 10**6 / rate <= MAX_PERIOD_US:
        raise ValueError(f"'{text}': the pulse period must be 10 ms .. 60 s")
    return rate


def rate_str(rate):
    """30 -> '30 fps'; 10^6/666667 -> '1.5 fps (666.667 ms)'."""
    rate = Fraction(rate)
    if rate.denominator == 1:
        return f"{rate} fps"
    return f"{float(rate):.6g} fps ({1000 / float(rate):.6g} ms)"


@dataclasses.dataclass(frozen=True)
class SensorTiming:
    """How a VEYE MV camera times its exposures, by sensor (CAPTURE.md "Timestamps").

    holds_to_period: in trigger mode the exposure is held so that it ends about one frame period
    after the trigger (IMX178, measured); else it starts at the trigger (+ delay + offset).
    busy_periods: frame periods the camera is busy after a trigger, so later triggers are lost
    (rolling shutter: 2, "half the video rate"; Sony global shutter: 1, its readout overlaps the
    next exposure, per VEYE's manual).
    """
    name: str
    global_shutter: bool
    line_ns: dict               # bits -> row-to-row exposure start, ns (global shutter: none)
    calibrated_bits: tuple      # depths whose timing was measured (empty: none, estimates)
    free_lead_ns: int           # free run: exposure start = frame start - exposure - this
    trigger_offset_ns: int      # GPS trigger: see exposure_start()
    holds_to_period: bool
    delay_scale: float          # real trigger delay / register value
    busy_periods: int
    busy_extra_ns: int          # ... plus this
    lag_extra_ns: int           # frame start after the trigger, beyond the model

    def line(self, bits):
        """Rolling-shutter line time at this depth (0: global shutter)."""
        if self.global_shutter:
            return 0
        return self.line_ns.get(bits, self.line_ns[12])

    def calibrated(self, bits):
        return bits in self.calibrated_bits

    def frame_lag_ns(self, period_ns, exp_ns, delay_ns):
        """CSI-2 frame start after the trigger pulse (delay_ns: real trigger delay)."""
        if self.holds_to_period:
            return period_ns + self.lag_extra_ns + delay_ns
        return delay_ns + self.trigger_offset_ns + exp_ns + self.lag_extra_ns

    def busy_ns(self, period_ns, delay_ns):
        """After a trigger the camera ignores triggers for this long."""
        return delay_ns + self.busy_periods * period_ns + self.busy_extra_ns


# IMX178M: measured (12 bit; 10/8 bit line times estimated), see the constants above
IMX178 = SensorTiming("IMX178", False, VEYE_LINE_NS, VEYE_CALIBRATED_BITS, VEYE_FREE_LEAD_NS,
                      VEYE_TRIGGER_OFFSET_NS, True, VEYE_DELAY_SCALE, 2, LAG_EXTRA_NS,
                      LAG_EXTRA_NS)
# Sony Pregius global shutter (IMX265M, IMX264M, IMX296M, IMX287M): NOT measured. All rows
# expose together; trigger mode overlaps readout and the next exposure (VEYE manual), so the
# camera follows triggers at about its video frame rate. Assumed until the LED test: exposure
# starts at the trigger (+ delay), frame start ~1 ms after the exposure ends, delay register
# exact, free-run frame start at the exposure end.
SONY_GLOBAL = SensorTiming("Sony Pregius global shutter", True, {}, (), 0, 0, False, 1.0, 1,
                           500_000, 1_000_000)
SONY_GLOBAL_SENSORS = ("IMX265", "IMX264", "IMX296", "IMX287", "IMX273", "IMX250", "IMX252")


def sensor_timing(model):
    """Timing profile of a VEYE camera from its model name (e.g. 'MV-MIPI-IMX265M')."""
    m = model.upper()
    if "IMX178" in m:
        return IMX178
    if any(s in m for s in SONY_GLOBAL_SENSORS):
        return SONY_GLOBAL
    # other VEYE models: the IMX178 model as a guess, uncalibrated
    return dataclasses.replace(IMX178, name=f"{model} (IMX178 model, not measured)",
                               calibrated_bits=())


def veye_line_ns(bits, sensor=IMX178):
    """Rolling-shutter line time at this bit depth (0: global shutter)."""
    return sensor.line(bits)


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


def trigger_delay_ns(delay_us, is_veye, sensor=IMX178):
    """The real delay from trigger pulse to exposure; the setting is in camera µs."""
    d = (delay_us or 0) * 1000
    return d * sensor.delay_scale if is_veye else d


def onset_delay_us(row, rate, period_ns, exp_ns, line_ns, sensor=IMX178):
    """VEYE trigger delay (camera µs) that centres image row `row`'s exposure on the next
    timepulse, i.e. on the GPS LED switching on. IMX178: row y starts exposing at pulse +
    delay + (frame period - exposure) + VEYE_TRIGGER_OFFSET_NS + y * line."""
    lead = sensor.trigger_offset_ns + row * line_ns + exp_ns / 2
    if sensor.holds_to_period:
        lead += period_ns - exp_ns
    return (-lead) % (1e9 / rate) / sensor.delay_scale / 1000


def pulses_per_frame(rate, period_ns, delay_ns, sensor=IMX178):
    """VEYE: a trigger that comes while the camera is busy (IMX178: its delay + about two frame
    periods + 3 ms after the last trigger) is lost, so the camera takes every n-th pulse."""
    return int(sensor.busy_ns(period_ns, delay_ns) * rate / 1e9) + 1


def exposure_start(frame_ns, pulse_ns, exposure_us, delay_us, is_veye, period_ns=None,
                   sensor=IMX178):
    """Row-0 exposure start (UTC ns): from the trigger pulse when GPS-triggered (exact, no Pi
    clock), else from the frame-start time (Pi clock). period_ns: the VEYE frame period;
    sensor: its timing profile. Cameras other than VEYE: not measured; assumes frame start -
    exposure, or the pulse (+ delay)."""
    exp_ns = exposure_us * 1000
    if pulse_ns is None:
        return int(frame_ns - exp_ns - (sensor.free_lead_ns if is_veye else 0))
    pulse_ns += int(trigger_delay_ns(delay_us, is_veye, sensor))
    if not is_veye:
        return pulse_ns
    if sensor.holds_to_period:
        return int(pulse_ns + period_ns - exp_ns + sensor.trigger_offset_ns)
    return int(pulse_ns + sensor.trigger_offset_ns)


def pi_minus_mono_ns():
    """Pi clock (CLOCK_REALTIME, NTP) minus CLOCK_MONOTONIC, now."""
    return time.clock_gettime_ns(time.CLOCK_REALTIME) - time.monotonic_ns()


class GpsClock:
    """UTC from the GPS alone: the Pi's monotonic clock plus an offset that the GPS sets and
    the trigger pulses keep. No NTP.

    Anchoring needs the timepulse at 1 Hz. Every second the GPS sends a burst of messages
    about the epoch that has just passed; its NAV-TIMEUTC names that UTC second S (used only
    when the GPS says UTC is valid, i.e. it knows the leap seconds). The burst starts some
    tens of ms after the epoch, so the pulse of S is the last trigger pulse (frame start
    minus the pulse-to-frame lag) less than MAX_BURST_LAG_NS before the burst. Serial latency
    is far too uncertain to pick one of 30 pulses a second, but fine for one of 1 per second.
    N_ANCHOR such seconds agreeing within AGREE_NS give the offset; they need not be
    consecutive (with a long trigger delay the camera takes only every n-th pulse), as a
    second taken for the wrong pulse would disagree by a whole second.

    From then on every stamped frame (its pulse exactly on the grid) measures the offset
    again; a line fitted over the last minutes follows the drift of the Pi's crystal, also
    across gaps without frames. The bursts keep coming at any pulse rate and catch gross
    errors (a wrong second), not single-pulse slips.
    """
    MAX_BURST_LAG_NS = 500_000_000
    AGREE_NS = 20_000_000
    N_ANCHOR = 3
    SAMPLE_NS = 500_000_000         # at most one offset sample per 0.5 s
    FIT_SPAN_NS = 30_000_000_000    # fit a drift only over at least 30 s of samples
    HOLDOVER_NS = 600_000_000_000   # older than this without samples: anchor again

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.labels = collections.deque(maxlen=10)  # (burst start mono ns, UTC second ns)
            self.pulses = collections.deque(maxlen=10)  # 1 Hz trigger pulses (mono ns)
            self.samples = collections.deque(maxlen=600)    # (mono ns, offset - base ns)
            self.base = None            # UTC - monotonic (ns, int) at anchoring
            self.t0 = 0                 # fit: offset = base + c0 + drift * (mono - t0)
            self.c0 = 0.0
            self.drift = 0.0            # ns per ns (the Pi clock's rate error)
            self.anchored_at = None     # UTC ns of the anchoring pulse
            self.burst_lag_ns = None    # last burst start after its epoch (diagnostic)
            self.tp = None              # (UTC ns of a pulse from TIM-TP, mono ns received)
            self.bad = 0                # bursts in a row that disagree with the clock
            self.suspect = False
            self.message = "waiting for GPS UTC and 1 Hz pulses"

    @property
    def anchored(self):
        return self.base is not None

    def stale(self, now_ns=None):
        """True if the clock needs anchoring again: never anchored, inconsistent, or no
        samples for HOLDOVER_NS."""
        if not self.anchored or self.suspect:
            return True
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        return now_ns - self.samples[-1][0] > self.HOLDOVER_NS

    def utc(self, mono_ns):
        """UTC (ns) of a CLOCK_MONOTONIC time; None until anchored."""
        with self.lock:
            if self.base is None:
                return None
            return int(mono_ns + self.base + round(self.c0 + self.drift * (mono_ns - self.t0)))

    def label(self, burst_ns, utc_ns):
        """GPS thread: a burst of messages started at burst_ns (mono) and names the UTC epoch
        utc_ns (rounded here to the second)."""
        second = round(utc_ns / 1e9) * 1_000_000_000
        with self.lock:
            if self.base is None:
                self.labels.append((burst_ns, second))
                self._try_anchor()
                return
            pulse = second - self.base - round(self.c0 + self.drift * (burst_ns - self.t0))
            lag = burst_ns - pulse
            self.burst_lag_ns = lag
            if -self.AGREE_NS < lag < self.MAX_BURST_LAG_NS:
                self.bad = 0
            else:
                self.bad += 1
                if self.bad >= self.N_ANCHOR and not self.suspect:
                    self.suspect = True
                    self.message = (f"GPS UTC disagrees with the clock by {lag / 1e6:.0f} ms: "
                                    "turn the GPS trigger off and on to anchor again")

    def timepulse(self, utc_ns, rx_ns):
        """GPS thread: UBX TIM-TP named the UTC of the next pulse (received at rx_ns, mono).
        Kept once anchored and if within 2 s of the clock (TIM-TP in GPS time would be 18 s
        off)."""
        with self.lock:
            if self.base is None:
                return
            now = rx_ns + self.base + round(self.c0 + self.drift * (rx_ns - self.t0))
            if abs(utc_ns - now) < 2_000_000_000:
                self.tp = (utc_ns, rx_ns)

    def pulse(self, mono_ns):
        """Grabber: a trigger pulse (frame start minus lag, mono) of the 1 Hz anchoring train."""
        with self.lock:
            if self.base is None:
                self.pulses.append(mono_ns)
                self._try_anchor()

    def _try_anchor(self):
        found = []
        for burst, second in self.labels:
            before = [p for p in self.pulses if p < burst]
            if before and burst - before[-1] < self.MAX_BURST_LAG_NS:
                found.append((second, before[-1], burst))
        if len(found) < self.N_ANCHOR:
            if len(self.labels) >= 5 and len(self.pulses) >= 2:
                self.message = (f"{len(found)} of {len(self.labels)} GPS seconds matched a "
                                f"trigger pulse so far")
            elif len(self.labels) >= 5:
                self.message = "GPS UTC is in, waiting for frames at 1 Hz"
            return
        last = found[-self.N_ANCHOR:]
        offsets = sorted(second - p for second, p, _ in last)
        if offsets[-1] - offsets[0] > self.AGREE_NS:
            self.message = (f"GPS seconds and pulses disagree by "
                            f"{(offsets[-1] - offsets[0]) / 1e6:.0f} ms; still trying")
            return
        self.base = offsets[len(offsets) // 2]
        self.samples.clear()
        for second, p, _ in last:
            self.samples.append((p, float(second - p - self.base)))
        self.t0, self.c0, self.drift = last[-1][1], 0.0, 0.0
        self.anchored_at = last[-1][0]
        self.burst_lag_ns = last[-1][2] - last[-1][1]
        self.message = ""

    def observe(self, mono_ns, utc_ns):
        """A frame whose trigger pulse is known: its start was at utc_ns (pulse + lag)."""
        with self.lock:
            if self.base is None:
                return
            if self.samples and mono_ns - self.samples[-1][0] < self.SAMPLE_NS:
                return
            self.samples.append((mono_ns, float(utc_ns - mono_ns - self.base)))
            self._fit()

    def _fit(self):
        s = self.samples
        self.t0 = s[-1][0]
        if s[-1][0] - s[0][0] < self.FIT_SPAN_NS:
            recent = [y for _, y in list(s)[-10:]]
            self.c0, self.drift = sum(recent) / len(recent), 0.0
            return
        n = len(s)
        xm = sum(t - self.t0 for t, _ in s) / n
        ym = sum(y for _, y in s) / n
        sxx = sum((t - self.t0 - xm) ** 2 for t, _ in s)
        sxy = sum((t - self.t0 - xm) * (y - ym) for t, y in s)
        self.drift = sxy / sxx
        self.c0 = ym - self.drift * xm

    def status(self):
        """One line for the GUI."""
        if self.base is None:
            return "anchoring: " + self.message
        lag = f", burst +{self.burst_lag_ns / 1e6:.0f} ms" if self.burst_lag_ns is not None else ""
        return (f"GPS UTC since {utc_str(self.anchored_at, 0)}, Pi clock {-self.drift * 1e6:+.2f} ppm"
                + lag + (f"  {self.message}" if self.message else ""))


class PulseStamper:
    """Finds the pulse of a UTC-aligned pulse train (GPS timepulse) that triggered a frame.

    A frame starts on the CSI-2 link a steady lag after its pulse (VEYE: the camera's frame
    period + ~3 ms, measured). Frame time minus that lag, rounded to the pulse grid (k/rate
    seconds, aligned to the UTC second), is the pulse - right while the lag guess and the
    clock are together off by less than half a pulse period.

    With a GpsClock the frame time is GPS UTC (the clock learns from each frame; returns None
    until it is anchored, and feeds it the pulses of a 1 Hz train meanwhile). Without one it is
    the Pi's clock (NTP: ~1 ms) and the lag guess follows the measured lag.

    Grid: whole-Hz rates are aligned to the UTC second by the GPS (k/rate s). Other rates are
    periods the GPS does not align (it does so only for fractions of a second); their grid
    is a pulse named by TIM-TP plus whole periods, so they need a clock and stamp nothing
    until a TIM-TP received after the switch (within ~1 s). Each later TIM-TP must land on
    that grid (else grid_moved counts it and the grid follows).

    Rate change: the GPS switches whenever it gets the command (not on a second). From
    change_rate() until a pulse after rate_confirmed() (the ACK) a frame goes to the nearest
    pulse of either grid; then the new one only.
    """

    def __init__(self, rate, lag_ns, clock=None):
        self.rates = [Fraction(rate)]       # pulses per second, exact (e.g. '0.5'); old, new
        self.lag_ns = float(lag_ns)
        self.clock = clock
        self.cut_ns = None                  # mono: pulses after this come at the new rate
        self.pending = False                # rate change under way
        self.last_lag_ns = None             # measured frame start - pulse
        self.stride = 1                     # pulses per frame (the camera skips the others)
        self.off_grid = 0                   # frames far from every pulse (not used to learn)
        self.grid_moved = 0                 # TIM-TP pulses off the grid (not whole-Hz rates)
        self.origins = {}                   # not whole-Hz rate -> UTC ns of one of its pulses
        self.since_ns = time.monotonic_ns() # mono: TIM-TP after this is about the newest rate

    @property
    def rate(self):
        return self.rates[-1]

    @property
    def period_ns(self):
        return 1e9 / float(self.rate)

    @property
    def settled(self):
        return not self.pending

    @property
    def anchored(self):
        return self.clock is None or self.clock.anchored

    @property
    def ready(self):
        """Stamping exactly at a single rate: anchored, no change under way, grid known."""
        return (self.anchored and not self.pending
                and all(self._origin(r) is not None for r in self.rates))

    def change_rate(self, rate):
        """Before the GPS is told to switch."""
        self.rates = [self.rates[0]] + ([Fraction(rate)] if Fraction(rate) != self.rates[0] else [])
        self.cut_ns, self.pending = None, True

    def rate_confirmed(self, mono_ns=None):
        """The GPS ACKed the change (at mono_ns): pulses after that are at the new rate."""
        self.cut_ns = time.monotonic_ns() if mono_ns is None else mono_ns
        self.since_ns = self.cut_ns

    def cancel_change(self):
        self.rates, self.cut_ns, self.pending = self.rates[:1], None, False

    def _origin(self, rate):
        if rate.denominator == 1 or self.clock is None:     # no GPS: assume the UTC second
            return 0
        return self.origins.get(rate)

    def _follow_timepulse(self):
        """Take the newest TIM-TP pulse as the grid of the newest rate (not whole Hz)."""
        rate = self.rates[-1]
        tp = self.clock.tp if self.clock is not None else None
        if rate.denominator == 1 or self.pending or tp is None or tp[1] <= self.since_ns:
            return
        origin = self.origins.get(rate)
        if origin is not None:
            period = 10**9 / rate
            d = (tp[0] - origin) % period
            if min(d, period - d) > 10_000:     # 10 us
                self.grid_moved += 1
        self.origins[rate] = tp[0]
        self.since_ns = tp[1]

    def _nearest(self, t):
        best = None
        for r in self.rates:
            o = self._origin(r)
            if o is None:
                continue
            p = round(o + round((t - o) * r / 10**9) * 10**9 / r)
            if best is None or abs(p - t) < abs(best - t):
                best = p
        return best

    def __call__(self, frame_ns):
        """frame_ns: frame start, Pi clock (as the camera backends give it). Returns the UTC
        (ns) of its pulse, or None (GPS clock not anchored yet, or grid not known yet)."""
        mono = frame_ns - pi_minus_mono_ns()
        if self.pending and self.cut_ns is not None and mono - self.lag_ns > self.cut_ns:
            self.rates, self.cut_ns, self.pending = self.rates[-1:], None, False
        self._follow_timepulse()
        clock = self.clock
        if clock is None:
            t = frame_ns
        else:
            t = clock.utc(mono)
            if t is None:
                if not self.pending and self.rates == [1]:
                    clock.pulse(mono - self.lag_ns)
                return None
        if any(self._origin(r) is None for r in self.rates):
            return None                 # TIM-TP has not named the new grid yet
        est = t - self.lag_ns
        pulse = self._nearest(est)
        self.last_lag_ns = t - pulse
        if abs(est - pulse) < 1e9 / float(max(self.rates)) / 4:
            if clock is not None:
                clock.observe(mono, pulse + self.lag_ns)
            else:
                self.lag_ns += (est - pulse) / 8
        else:
            self.off_grid += 1
        return pulse
