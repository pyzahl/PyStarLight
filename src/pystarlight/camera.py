"""Live viewer and SER recorder for Aravis (GenICam: FLIR/Teledyne Grasshopper, Basler etc.)
cameras and VEYE MV-MIPI cameras on the Raspberry Pi 5 CSI ports (veye_mvcam driver, see
veye_mvcam.py), with GPS-timed triggering (u-blox timepulse -> camera trigger input, see
gps_ublox.py).

Threads:
  - Grabber:    pops buffers from the stream and keeps only the newest one, undecoded
                (older ones are recycled straight back to the camera); stamps and hands
                every frame to the recorder while recording.
  - Controller: applies exposure/gain/trigger changes to the camera in the background, so
                the GUI never blocks on slow register access or stream restarts.
  - Recorder:   writes SER files (ser_recorder.py); GPS: reads the GPS serial port.
  - Main:       Qt GUI only - renders the newest frame, control panel, status.

GPS trigger: the GPS timepulse (PPS pin) is wired to the camera's trigger input and set
to the chosen frame rate; every frame is then the pulse of a known UTC instant, and is
stamped with that instant (PulseStamper) instead of the Pi's clock. A recording armed for
a start time begins with the frame of exactly that pulse.

Keys: e/E exposure -/+, g/G gain -/+, d/D gamma -/+, t software trigger, r record on/off,
q quit. Recordings are SER files (16-bit, UTC timestamp per frame), see ser_recorder.py.
"""
import argparse
import collections
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import cv2
import gi
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

gi.require_version('Aravis', '0.8')
from gi.repository import Aravis  # noqa: E402

try:                            # as pystarlight.camera
    from . import gps_ublox, veye_mvcam  # noqa: E402
    from .ser_recorder import Recorder  # noqa: E402
except ImportError:             # as a plain script next to its helper modules
    import gps_ublox  # noqa: E402
    import veye_mvcam  # noqa: E402
    from ser_recorder import Recorder  # noqa: E402

GPS_RATES = ["30", "20", "15", "10", "8", "6", "5", "4", "3", "2", "1", "0.5"]     # fps

parser = argparse.ArgumentParser(description="Aravis / VEYE MIPI live view")
parser.add_argument("--camera", default=None,
                    help="which camera to open: any part of its ID, vendor, model or serial "
                         "(e.g. Basler, a2A1920, Grasshopper, 2316xxxx, VEYE, IMX178). "
                         "Default: first found")
parser.add_argument("--list", action="store_true", help="list connected cameras and exit")
parser.add_argument("--bits", choices=["12", "10", "8"], default="12",
                    help="pixel depth (default 12); 10 and 8 bit read out faster (IMX178 full "
                         "frame: 33.2 / 39.2 / 40.1 fps); also selectable in the GUI")
parser.add_argument("--format", default=None,
                    help="explicit GenICam PixelFormat (e.g. Mono12p, Mono16, Mono8), or for VEYE "
                         "cameras GREY, Y10, Y12 (unpacked by the Pi) or Y10P, Y12P (packed); "
                         "overrides --bits")
parser.add_argument("--exposure", type=float, default=None,
                    help="initial exposure time in ms (manual; auto-exposure is turned off)")
parser.add_argument("--gain", type=float, default=None,
                    help="initial gain in dB (manual; auto-gain is turned off)")
parser.add_argument("--max-exposure", type=float, default=2000.0,
                    help="upper end of the exposure slider in ms (default 2000)")
parser.add_argument("--scale", type=float, default=0.5,
                    help="initial window size as a fraction of the image (default 0.5); "
                         "the view follows the window, data is always full resolution")
parser.add_argument("--gamma", type=float, default=1.0,
                    help="initial display gamma (>1 brightens faint detail; display only)")
parser.add_argument("--fps", type=float, default=None,
                    help="fixed camera frame rate (default: free-running, as fast as exposure allows); "
                         "exposure is then limited to 1/fps")
parser.add_argument("--usb-mode", choices=["sync", "async"], default=None,
                    help="Aravis USB3 transfer mode (default: Aravis' own default)")
parser.add_argument("--bandwidth", type=float, default=None,
                    help="limit camera USB throughput in MB/s (e.g. 200)")
parser.add_argument("--buffers", type=int, default=16,
                    help="number of stream buffers (default 16)")
parser.add_argument("--trigger", choices=["off", "gps", "hw", "sw"], default="off",
                    help="start mode: off: free-running (default); gps: GPS timepulse on the "
                         "trigger input, frames stamped with the pulse time; hw: one frame per "
                         "edge on the trigger input (Line0); sw: one frame per 't' key press. "
                         "The GUI's GPS checkbox switches between gps and the others")
parser.add_argument("--gps-rate", choices=GPS_RATES, default="1",
                    help="GPS trigger frame rate in fps (default 1)")
parser.add_argument("--pps", dest="gps_rate_alias", choices=GPS_RATES, nargs="?", const="1",
                    default=None, help=argparse.SUPPRESS)   # = --trigger gps --gps-rate RATE
parser.add_argument("--gps-port", default="/dev/ttyAMA0",
                    help="u-blox GPS serial port (default /dev/ttyAMA0; 'none': no GPS - the "
                         "trigger pulses then must come at the selected rate by themselves)")
parser.add_argument("--gps-baud", type=int, default=9600, help="GPS baud rate (default 9600)")
parser.add_argument("--trigger-edge", choices=["rising", "falling"], default="rising",
                    help="active edge of the hardware trigger (default rising)")
parser.add_argument("--trigger-delay", type=float, default=None,
                    help="delay from trigger edge to exposure start, in the camera's units (us)")
parser.add_argument("--y-trigger", type=int, default=None, metavar="ROW",
                    help="VEYE, GPS trigger: set the trigger delay so the timepulse (the GPS LED "
                         "switching on) falls on this image row; follows exposure and ROI changes")
parser.add_argument("--roi", default=None,
                    help="sensor region x,y,w,h (default: full sensor); smaller = faster "
                         "(and needed for GPS rates above ~10 fps on a full IMX178)")
parser.add_argument("--record-dir", default=os.path.expanduser("~/data"),
                    help="where SER recordings go (default ~/data)")
parser.add_argument("--record-start", default=None,
                    help="arm recording to start at this UTC time: HH:MM[:SS] (next occurrence) "
                         "or YYYY-MM-DDTHH:MM:SS")
parser.add_argument("--record-duration", type=float, default=None,
                    help="stop recording after this many seconds")
parser.add_argument("--record-frames", type=int, default=None,
                    help="stop recording after this many frames")
parser.add_argument("--observer", default="", help="observer name for the SER header")
parser.add_argument("--telescope", default="", help="telescope for the SER header")
args = None                 # parsed in main()


EXP_STEPS = 1000            # exposure slider resolution (log scale)
GAIN_STEPS = 10             # gain slider: 0.1 dB per step
GAMMA_MIN, GAMMA_MAX = 0.1, 4.0
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
# pixel formats per bit depth, in order of preference ({b}: Mono / Bayer base name); packed
# 10-bit GenICam formats are left out (the decoder doesn't unpack them)
FORMATS_BY_BITS = {12: ["{b}12p", "{b}12Packed", "{b}16", "Y12", "Y12P"],
                   10: ["{b}10", "Y10", "Y10P"],
                   8: ["{b}8", "GREY"]}
# Smallest region the Pi's CSI-2 pipeline streams from a VEYE IMX178 (Y12): lower ones fail at
# STREAMON with EPIPE (measured; height 316 fails, 320 works). The sensor always reads full-width
# rows (veye_mvcam crops x in software), so any width works (8 measured; 368 failed before that)
VEYE_MIN_ROI = (8, 320)


def try_call(fn):
    """Call an optional camera feature, ignoring it if the camera doesn't have it."""
    try:
        fn()
        return True
    except Exception as e:
        print(f"  (skipped: {e})")
        return False


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


def format_bits(pixel_format):
    """Bit depth of a pixel format name (12 / 10 / 8; 16 counts as 12, the cameras' ADC)."""
    for bits in (12, 10, 8):
        if any(pixel_format == f.format(b=b) for f in FORMATS_BY_BITS[bits]
               for b in ("Mono", "BayerRG", "BayerBG", "BayerGR", "BayerGB")):
            return bits
    return 16 if "16" in pixel_format else 8


def format_for_bits(bits, available, current):
    """The preferred pixel format of a bit depth that the camera offers (None: none)."""
    base = "Bayer" + current[5:7] if current.startswith("Bayer") else "Mono"
    return next((f.format(b=base) for f in FORMATS_BY_BITS[bits]
                 if f.format(b=base) in available), None)


def utc_str(ns, digits=3):
    t = datetime.fromtimestamp(ns / 1e9, timezone.utc)
    return t.strftime("%H:%M:%S.%f")[:9 + digits] if digits else t.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
def unpack_12bit(raw, w, h, lsb_first):
    """Unpack 12-bit packed data (2 pixels in 3 bytes) to a new uint16 (h, w) array.

    lsb_first=True  -> GenICam 'Mono12p' / 'Bayer*12p' layout
    lsb_first=False -> legacy 'Mono12Packed' / 'Bayer*12Packed' layout
    """
    b = np.frombuffer(raw, np.uint8, count=w * h * 3 // 2).reshape(-1, 3)
    out = np.empty((h, w), np.uint16)
    o = out.reshape(-1, 2)
    b0 = b[:, 0]
    b1 = b[:, 1].astype(np.uint16)
    b2 = b[:, 2]
    if lsb_first:
        np.left_shift(b1 & 0x0F, 8, out=o[:, 0])
        o[:, 0] |= b0
        np.left_shift(b2, 4, out=o[:, 1], dtype=np.uint16)
        o[:, 1] |= b1 >> 4
    else:
        np.left_shift(b0, 4, out=o[:, 0], dtype=np.uint16)
        o[:, 0] |= b1 & 0x0F
        np.left_shift(b2, 4, out=o[:, 1], dtype=np.uint16)
        o[:, 1] |= b1 >> 4
    return out


def unpack_mipi(raw, w, h, bits):
    """Unpack MIPI CSI-2 packed RAW10 / RAW12 (V4L2 'Y10P' / 'Y12P') to a uint16 (h, w) array.

    Each group starts with the high 8 bits of every pixel, followed by one byte holding
    the low bits of all of them (pixel 0 in the least significant bits).
    """
    n = 4 if bits == 10 else 2              # pixels per group
    b = np.frombuffer(raw, np.uint8, count=w * h * bits // 8).reshape(-1, n + 1)
    low = b[:, n].astype(np.uint16)
    lbits = bits - 8
    out = np.empty((h, w), np.uint16)
    o = out.reshape(-1, n)
    for i in range(n):
        np.left_shift(b[:, i], lbits, out=o[:, i], dtype=np.uint16)
        o[:, i] |= (low >> (lbits * i)) & ((1 << lbits) - 1)
    return out


# GenICam Bayer names -> OpenCV codes (OpenCV's naming is shifted by one pixel)
BAYER_CODES = {
    "BayerRG": cv2.COLOR_BayerBG2BGR,
    "BayerBG": cv2.COLOR_BayerRG2BGR,
    "BayerGR": cv2.COLOR_BayerGB2BGR,
    "BayerGB": cv2.COLOR_BayerGR2BGR,
}


class Decoder:
    def __init__(self, pixel_format):
        pf = pixel_format
        self.mipi_bits = {"Y10P": 10, "Y12P": 12}.get(pf)     # V4L2 MIPI-packed (VEYE)
        self.msb_shift = {"Y10": 6, "Y12": 4}.get(pf)         # VEYE, unpacked by the Pi
        self.is_packed12 = self.mipi_bits is None and (
            "12p" in pf.lower() or "12packed" in pf.lower())
        self.lsb_first = pf.endswith("12p")
        if self.is_packed12 or "12" in pf:
            self.max_value = 4095
        elif "10" in pf:
            self.max_value = 1023
        elif "16" in pf:
            self.max_value = 65535
        else:
            self.max_value = 255
        self.dtype = np.uint8 if self.max_value == 255 else np.uint16
        self.bayer_code = BAYER_CODES.get(pf[:7]) if pf.startswith("Bayer") else None

    def __call__(self, buf):
        # PyGObject returns a copy, so the Aravis buffer can be recycled
        return self.raw(buf.get_data(), buf.get_image_width(), buf.get_image_height())

    def raw(self, raw, w, h):
        if self.msb_shift:
            return np.frombuffer(raw, np.uint16, count=w * h).reshape(h, w) >> self.msb_shift
        if self.mipi_bits:
            img = unpack_mipi(raw, w, h, self.mipi_bits)
        elif self.is_packed12:
            img = unpack_12bit(raw, w, h, self.lsb_first)
        else:
            img = np.frombuffer(raw, dtype=self.dtype, count=w * h).reshape(h, w)
        return img



# ---------------------------------------------------------------------------
# GPS pulse time stamps
# ---------------------------------------------------------------------------
def trigger_delay_ns(is_veye):
    """The real delay from trigger pulse to exposure; the setting is in camera µs."""
    d = (args.trigger_delay or 0) * 1000
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


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------
class Grabber(threading.Thread):
    """Pops frames from the stream and holds on to only the newest *undecoded* buffer.

    No pixel work happens here: at 160 fps, decoding every frame in this thread would
    hog Python's GIL and starve the GUI. The main thread decodes only what it displays.
    """

    def __init__(self, stream, success=Aravis.BufferStatus.SUCCESS, recorder=None):
        super().__init__(daemon=True)
        self.stream = stream
        self.recorder = recorder    # gets a raw copy of every frame while recording
        # Count frames missing from gaps in the frame timing too: the Pi's CSI-2 receiver
        # drops frames without a sequence gap when no buffer is free. frame_period_ns():
        # expected spacing when free-running (None: don't check, e.g. triggered).
        self.frame_period_ns = None
        # PulseStamper while GPS-triggered (None: free run etc.); the recorder then starts
        # and stops by pulse time
        self.stamper = None
        # exposure_start(frame_ns, pulse_ns or None) -> row-0 exposure start (UTC ns), the
        # time stamp written to the SER file
        self.exposure_start = lambda frame_ns, pulse_ns: frame_ns
        self.exp_ns = None          # exposure start of the newest frame
        self.pulse_ns = None        # its trigger pulse (GPS)
        self.success = success      # buffer status of a complete frame for this backend
        self.lock = threading.Lock()
        self.latest = None          # (buffer, frame_id, stamp_ns) not yet taken by the GUI
        self.dropped = 0            # frame-ID gaps (frames lost before reaching us)
        self.missed = 0             # GPS pulses without a frame
        self.bad = {}               # non-SUCCESS buffer statuses -> count
        self.cam_fps = 0.0
        self.intervals = collections.deque(maxlen=200)  # frame-start spacing (ns), recent frames
        self.running = True
        self.last_ok = time.monotonic()
        self.paused = False         # pause(): the stream is about to be replaced
        self.parked = False         # ... and this thread has let go of it
        self.generation = 0         # +1 per new stream (frame IDs and timing restart)

    def pause(self):
        """Stop using the stream (before it is replaced); returns once this thread is idle."""
        self.paused = True
        t = time.monotonic() + 2.0
        while not self.parked and time.monotonic() < t:
            time.sleep(0.005)

    def resume(self, stream):
        self.stream = stream
        self.generation += 1
        self.paused = False

    def run(self):
        last_id = None
        last_ns, last_starts = None, None
        last_stamper, last_pulse = None, None
        last_gen = self.generation
        t0, n = time.monotonic(), 0
        while self.running:
            if self.paused:
                self.parked = True
                time.sleep(0.01)
                continue
            self.parked = False
            if self.generation != last_gen:     # new stream: nothing to compare with
                last_gen = self.generation
                last_id = last_ns = last_starts = last_stamper = last_pulse = None
                self.intervals.clear()
            buf = self.stream.timeout_pop_buffer(200_000)  # short timeout -> quick shutdown
            dt = time.monotonic() - t0
            if dt >= 1.0:
                self.cam_fps, t0, n = n / dt, time.monotonic(), 0
            if buf is None:
                continue
            status = buf.get_status()
            if status != self.success:
                name = getattr(status, "value_nick", str(status))
                self.bad[name] = self.bad.get(name, 0) + 1
                if self.bad[name] in (1, 10, 100) or self.bad[name] % 1000 == 0:
                    print(f"Frame not complete: status '{name}' x{self.bad[name]}")
                self.stream.push_buffer(buf)
                continue
            self.last_ok = time.monotonic()
            fid = buf.get_frame_id()
            if last_id is not None and fid > last_id + 1:
                self.dropped += fid - last_id - 1
            last_id = fid
            frame_ns = buf.get_system_timestamp()
            starts = getattr(self.stream, "starts", 0)
            if self.frame_period_ns is not None:
                if last_ns is not None and starts == last_starts:   # not across a restart
                    period = self.frame_period_ns()
                    dt = frame_ns - last_ns
                    if dt > 1.5 * period:
                        self.dropped += round(dt / period) - 1
            if last_ns is not None and starts == last_starts:
                self.intervals.append(frame_ns - last_ns)
            elif starts != last_starts:
                self.intervals.clear()          # restarted: old spacing no longer applies
            last_ns, last_starts = frame_ns, starts
            stamper = self.stamper
            pulse = None
            if stamper is not None:
                pulse = stamper(frame_ns)
                if stamper is last_stamper and last_pulse is not None:
                    gap = pulse - last_pulse
                    if gap > (stamper.stride + 0.5) * stamper.period_ns:
                        self.missed += round(gap / stamper.period_ns) - stamper.stride
                last_stamper, last_pulse = stamper, pulse
            exp_ns = self.exposure_start(frame_ns, pulse)
            self.exp_ns, self.pulse_ns = exp_ns, pulse
            sel_ns = exp_ns if pulse is None else pulse     # what start/stop times refer to
            if self.recorder is not None:
                if self.recorder.wants(sel_ns):
                    self.recorder.put(buf.get_data(), fid, sel_ns, exp_ns)
            with self.lock:
                old, self.latest = self.latest, (buf, fid, exp_ns)
            if old is not None:                 # GUI didn't want it in time -> recycle
                self.stream.push_buffer(old[0])
            n += 1

    def take(self):
        """Hand the newest buffer to the caller, who must push it back to the stream."""
        with self.lock:
            item, self.latest = self.latest, None
        return item


class Controller(threading.Thread):
    """Applies camera settings off the GUI thread; only the newest request per key is sent."""

    def __init__(self, camera, apply_trigger, change_roi):
        super().__init__(daemon=True)
        self.camera = camera
        self.apply_trigger = apply_trigger  # mode -> configures the camera (not acquiring)
        # reconfigure(region=(x, y, w, h) or None, pixel_format=name or None): rebuilds the stream
        self.reconfigure = change_roi
        self.reconfigs = 0                  # +1 per rebuilt stream (the GUI follows)
        self.cond = threading.Condition()
        self.pending = {}
        self.running = True
        self.restarts = 0
        self.busy = False
        # cached values actually applied by the camera (read back after each change)
        self.exposure_us = camera.get_exposure_time()
        self.gain_db = camera.get_gain()

    def request(self, key, value):
        with self.cond:
            if key == "reconfigure" and key in self.pending:    # region and format both
                value = {**self.pending[key], **value}
            self.pending[key] = value
            self.cond.notify()

    def run(self):
        while self.running:
            with self.cond:
                while not self.pending and self.running:
                    self.cond.wait(0.5)
                todo, self.pending = self.pending, {}
                self.busy = bool(todo)
            try:
                if "reconfigure" in todo:
                    self.reconfigure(**todo["reconfigure"])
                    self.reconfigs += 1
                if "trigger" in todo:
                    print(f"Trigger mode: {todo['trigger']}")
                    self.camera.stop_acquisition()
                    self.apply_trigger(todo["trigger"])
                    self.camera.start_acquisition()
                if "delay" in todo:
                    # the camera ignores the delay register while acquiring
                    self.camera.stop_acquisition()
                    us = todo["delay"]
                    try_call(lambda us=us: self.camera.set_float("TriggerDelay", us))
                    self.camera.start_acquisition()
                if "exposure" in todo:
                    self.camera.set_exposure_time(todo["exposure"])
                    self.exposure_us = self.camera.get_exposure_time()
                if "gain" in todo:
                    self.camera.set_gain(todo["gain"])
                    self.gain_db = self.camera.get_gain()
                if "restart" in todo:
                    print("Restarting acquisition...")
                    self.camera.stop_acquisition()
                    self.camera.start_acquisition()
                    self.restarts += 1
            except Exception as e:
                print(f"Camera setting failed: {e}")
            self.busy = False

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class ImageView(QtWidgets.QWidget):
    """Shows the newest frame: fit to the window; mouse wheel zooms around the cursor, left
    drag pans, double-click fits again. In select mode a left drag draws a rectangle
    instead, emitted in image pixels. Past 1:1 pixels are shown as blocks (nearest)."""

    changed = QtCore.pyqtSignal()                       # view moved: render again
    selected = QtCore.pyqtSignal(int, int, int, int)    # x0, y0, x1, y1 (image pixels)
    MAX_ZOOM = 64.0

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.zoom = 1.0             # 1: the whole image fits
        self.center = None          # image point at the view centre (None: image centre)
        self.img_size = None        # (w, h) of the frame shown
        self.qimg = None
        # mapping of the drawn image: screen = (ox, oy) + (image - (x0, y0)) * (sx, sy)
        self.map = None
        self.select_mode = False
        self.drag = None            # ("pan", start pos, start centre) / ("select", image point)
        self.band = None            # rectangle being dragged (image pixels)
        self.marks = []             # [(x0, y0, x1, y1, QColor)] to draw (image pixels)
        self.hline = None           # (row, QColor): a dashed line across the image
        self.cursor_img = None      # image point under the mouse

    def reset_view(self):
        self.zoom, self.center = 1.0, None
        self.changed.emit()

    def _fit(self):
        w, h = self.img_size
        return min(max(1, self.width()) / w, max(1, self.height()) / h)

    def render(self, frame, lut):
        """frame: full-resolution (h, w) or (h, w, 3) raw values; lut: raw -> 8 bit."""
        H, W = frame.shape[:2]
        if self.img_size != (W, H):             # new region: start from the whole image
            self.img_size, self.zoom, self.center = (W, H), 1.0, None
        vw, vh = max(1, self.width()), max(1, self.height())
        scale = self._fit() * self.zoom
        sw, sh = vw / scale, vh / scale         # visible part, image pixels
        cx, cy = self.center or (W / 2, H / 2)
        cx = min(max(cx, sw / 2), W - sw / 2) if sw < W else W / 2
        cy = min(max(cy, sh / 2), H - sh / 2) if sh < H else H / 2
        self.center = (cx, cy) if self.zoom > 1.0 else None
        X0, Y0 = cx - sw / 2, cy - sh / 2       # view's top-left, image pixels (may be < 0)
        x0, y0 = max(0, int(np.floor(X0))), max(0, int(np.floor(Y0)))
        x1, y1 = min(W, int(np.ceil(X0 + sw)) + 1), min(H, int(np.ceil(Y0 + sh)) + 1)
        ow = max(1, round((x1 - x0) * scale))
        oh = max(1, round((y1 - y0) * scale))
        crop = frame[y0:y1, x0:x1]
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_NEAREST
        disp = np.ascontiguousarray(np.take(lut, cv2.resize(crop, (ow, oh), interpolation=interp),
                                            mode="clip"))
        fmt = QtGui.QImage.Format_Grayscale8 if disp.ndim == 2 else QtGui.QImage.Format_BGR888
        self.qimg = QtGui.QImage(disp.data, ow, oh, disp.strides[0], fmt).copy()
        sx, sy = ow / (x1 - x0), oh / (y1 - y0)
        self.map = ((x0 - X0) * scale, (y0 - Y0) * scale, x0, y0, sx, sy)
        self.update()

    def to_image(self, pos):
        ox, oy, x0, y0, sx, sy = self.map
        return x0 + (pos.x() - ox) / sx, y0 + (pos.y() - oy) / sy

    def to_screen(self, x, y):
        ox, oy, x0, y0, sx, sy = self.map
        return ox + (x - x0) * sx, oy + (y - y0) * sy

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtCore.Qt.black)
        if self.qimg is None:
            p.setPen(QtCore.Qt.gray)
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "waiting for frames...")
            return
        p.drawImage(QtCore.QPointF(self.map[0], self.map[1]), self.qimg)
        rects = list(self.marks)
        if self.band is not None:
            rects.append((*self.band, QtGui.QColor("#00e5ff")))
        for x0, y0, x1, y1, color in rects:
            pen = QtGui.QPen(color, 2)
            pen.setCosmetic(True)
            p.setPen(pen)
            a, b = self.to_screen(x0, y0), self.to_screen(x1, y1)
            p.drawRect(QtCore.QRectF(QtCore.QPointF(*a), QtCore.QPointF(*b)))
        if self.hline is not None:
            row, color = self.hline
            pen = QtGui.QPen(color, 1, QtCore.Qt.DashLine)
            pen.setCosmetic(True)
            p.setPen(pen)
            a, b = self.to_screen(0, row + 0.5), self.to_screen(self.img_size[0], row + 0.5)
            p.drawLine(QtCore.QPointF(*a), QtCore.QPointF(*b))
        if self.zoom > 1.0:
            p.setPen(QtCore.Qt.yellow)
            p.drawText(8, 18, f"zoom {self.zoom:.1f}×  (double-click: fit)")

    def wheelEvent(self, e):
        if self.map is None:
            return
        new = min(max(self.zoom * 1.25 ** (e.angleDelta().y() / 120), 1.0), self.MAX_ZOOM)
        ix, iy = self.to_image(e.pos())
        s = self._fit() * new                   # keep the image point under the cursor
        self.center = (ix - (e.pos().x() - self.width() / 2) / s,
                       iy - (e.pos().y() - self.height() / 2) / s)
        self.zoom = new
        self.changed.emit()

    def mousePressEvent(self, e):
        if self.map is None or e.button() != QtCore.Qt.LeftButton:
            return
        if self.select_mode:
            self.drag = ("select", self.to_image(e.pos()))
        else:
            W, H = self.img_size
            self.drag = ("pan", e.pos(), self.center or (W / 2, H / 2))

    def mouseMoveEvent(self, e):
        if self.map is None:
            return
        self.cursor_img = self.to_image(e.pos())
        if self.drag is None:
            return
        if self.drag[0] == "pan" and self.zoom > 1.0:
            s = self._fit() * self.zoom
            d = e.pos() - self.drag[1]
            self.center = (self.drag[2][0] - d.x() / s, self.drag[2][1] - d.y() / s)
            self.changed.emit()
        elif self.drag[0] == "select":
            (ax, ay), (bx, by) = self.drag[1], self.cursor_img
            self.band = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag is not None and self.drag[0] == "select" and self.band is not None:
            W, H = self.img_size
            x0, y0, x1, y1 = self.band
            self.selected.emit(int(max(0, x0)), int(max(0, y0)), int(min(W, x1)), int(min(H, y1)))
        self.drag, self.band = None, None
        self.update()

    def leaveEvent(self, e):
        self.cursor_img = None

    def mouseDoubleClickEvent(self, e):
        self.reset_view()


class CollapsibleBox(QtWidgets.QWidget):
    """A titled panel section whose body folds away; set_summary() fills the title line
    while it is folded (e.g. 'GPS - 3D, 9 sats')."""

    toggled = QtCore.pyqtSignal(bool)                   # True: open

    def __init__(self, title, opened=True):
        super().__init__()
        self.title = title
        self.summary = ""
        self.button = QtWidgets.QToolButton(checkable=True, checked=opened)
        self.button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.button.setStyleSheet("QToolButton { border: none; font-weight: bold; text-align: left; }")
        self.button.toggled.connect(self._toggled)
        self.body = QtWidgets.QFrame()
        self.body.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.grid = QtWidgets.QGridLayout(self.body)
        self.grid.setColumnStretch(1, 1)
        col = QtWidgets.QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        col.addWidget(self.button)
        col.addWidget(self.body)
        self._toggled(opened)

    def _toggled(self, opened):
        self.body.setVisible(opened)
        self.button.setArrowType(QtCore.Qt.DownArrow if opened else QtCore.Qt.RightArrow)
        self._title()
        self.toggled.emit(opened)

    def set_summary(self, text):
        if text != self.summary:
            self.summary = text
            self._title()

    def _title(self):
        folded = not self.button.isChecked() and self.summary
        self.button.setText(f"{self.title}  —  {self.summary}" if folded else self.title)


def grid_box(title):
    box = QtWidgets.QGroupBox(title)
    grid = QtWidgets.QGridLayout(box)
    grid.setColumnStretch(1, 1)
    return box, grid


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, ctx):
        super().__init__()
        self.c = ctx                        # camera, controller, grabber, recorder, gps ...
        self.setWindowTitle(f"{ctx.camera.get_vendor_name()} {ctx.camera.get_model_name()}"
                            f" - {ctx.width}x{ctx.height} {ctx.pixel_format}")
        self.gamma = float(np.clip(args.gamma, GAMMA_MIN, GAMMA_MAX))
        self.lut = self.build_lut(self.gamma)
        self.img = None                     # newest decoded frame (full resolution)
        self.frame_id = None
        self.redraw = False
        self.disp_fps, self.disp_t0, self.disp_n = 0.0, time.monotonic(), 0
        self.gps_busy = False               # timepulse rate being set (worker thread)
        self.gps_msg = ""
        self.last_exposure = None
        self.last_saved = ""
        self.roi_seen = 0                   # controller.roi_changes already followed
        self.roi_pending = None             # selected/typed region not applied yet
        # last requested values (keys step from these, not from a lagging camera read-back)
        self.target = {"exposure": ctx.controller.exposure_us, "gain": ctx.controller.gain_db}

        self.settings = QtCore.QSettings("PyStarLight", "camera")     # panel layout only
        self.view = ImageView()
        self.view.changed.connect(self.on_view_changed)
        self.view.selected.connect(self.on_roi_selected)
        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.addWidget(self.make_camera_box())
        col.addWidget(self.make_roi_box())
        col.addWidget(self.make_trigger_box())
        col.addWidget(self.make_gps_box())
        col.addWidget(self.make_record_box())
        col.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        # the labels are still empty here, so the size hint is too small; the splitter
        # handle lets the panel be widened (its width is remembered)
        panel_w = max(panel.sizeHint().width() + 30, 480)
        scroll.setMinimumWidth(min(panel_w, 360))

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        view_w = int(ctx.width * args.scale)
        self.resize(view_w + panel_w + 20, max(int(ctx.height * args.scale) + 40, 600))
        self.splitter.setSizes([view_w, panel_w])
        sizes = self.settings.value("splitter")
        if sizes:
            self.splitter.setSizes([int(v) for v in sizes])

        for key, fn in (("e", lambda: self.step_exposure(0.8)),
                        ("Shift+E", lambda: self.step_exposure(1.25)),
                        ("g", lambda: self.gain_spin.setValue(self.target["gain"] - 0.5)),
                        ("Shift+G", lambda: self.gain_spin.setValue(self.target["gain"] + 0.5)),
                        ("d", lambda: self.gamma_spin.setValue(self.gamma - 0.1)),
                        ("Shift+D", lambda: self.gamma_spin.setValue(self.gamma + 0.1)),
                        ("r", self.on_record_button),
                        ("t", self.software_trigger),
                        ("q", self.close)):
            QtWidgets.QShortcut(QtGui.QKeySequence(key), self, fn)

        self.frame_timer = QtCore.QTimer(self, timeout=self.on_frame_timer, interval=5)
        self.frame_timer.start()
        self.status_timer = QtCore.QTimer(self, timeout=self.on_status_timer, interval=250)
        self.status_timer.start()
        self.on_trigger_mode(initial=True)
        self.on_status_timer()

    # ---- panel: camera ----
    def make_camera_box(self):
        c = self.c
        box, g = grid_box("Camera")
        self.exp_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, maximum=EXP_STEPS)
        self.exp_spin = QtWidgets.QDoubleSpinBox(decimals=3, suffix=" ms", keyboardTracking=False)
        self.exp_spin.setRange(c.exp_min / 1000, c.exp_max / 1000)
        self.gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gain_slider.setRange(0, int(round((c.gain_max - c.gain_min) * GAIN_STEPS)))
        self.gain_spin = QtWidgets.QDoubleSpinBox(decimals=1, suffix=" dB", singleStep=0.5,
                                                  keyboardTracking=False)
        self.gain_spin.setRange(c.gain_min, c.gain_max)
        self.gamma_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.gamma_slider.setRange(int(GAMMA_MIN * 100), int(GAMMA_MAX * 100))
        self.gamma_spin = QtWidgets.QDoubleSpinBox(decimals=2, singleStep=0.1,
                                                   keyboardTracking=False)
        self.gamma_spin.setRange(GAMMA_MIN, GAMMA_MAX)
        for r, (name, slider, spin) in enumerate((("Exposure", self.exp_slider, self.exp_spin),
                                                  ("Gain", self.gain_slider, self.gain_spin),
                                                  ("Gamma", self.gamma_slider, self.gamma_spin))):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            g.addWidget(slider, r, 1)
            g.addWidget(spin, r, 2)
        self.cam_info = QtWidgets.QLabel()
        g.addWidget(self.cam_info, 3, 0, 1, 3)
        # measured frame timing (from the frame-start times) and the exposure actually set
        self.frame_labels = {}
        for r, (key, name) in enumerate((("frame", "Frame"), ("rate", "Rate"),
                                         ("interval", "Interval"), ("exp", "Exposure")), 4):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            lab = QtWidgets.QLabel("-")
            lab.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            g.addWidget(lab, r, 1, 1, 2)
            self.frame_labels[key] = lab
        self.bits_combo = QtWidgets.QComboBox()
        self.bits_choices = [b for b in (12, 10, 8)
                             if format_for_bits(b, self.c.available, self.c.pixel_format)]
        self.bits_combo.addItems([f"{b} bit" for b in self.bits_choices])
        self.bits_combo.setToolTip("lower depth reads out faster (IMX178 full frame: "
                                   "12 bit 33.2, 10 bit 39.2, 8 bit 40.1 fps)")
        self.show_bits()
        self.bits_combo.currentIndexChanged.connect(self.on_bits)
        g.addWidget(QtWidgets.QLabel("Depth"), 8, 0)
        g.addWidget(self.bits_combo, 8, 1, 1, 2)

        self.set_exposure_widgets(self.target["exposure"])
        self.set_gain_widgets(self.target["gain"])
        self.set_gamma_widgets(self.gamma)
        self.exp_slider.valueChanged.connect(lambda p: self.set_exposure(self.slider_to_exp(p)))
        self.exp_spin.valueChanged.connect(lambda ms: self.set_exposure(ms * 1000))
        self.gain_slider.valueChanged.connect(
            lambda p: self.set_gain(c.gain_min + p / GAIN_STEPS))
        self.gain_spin.valueChanged.connect(self.set_gain)
        self.gamma_slider.valueChanged.connect(lambda p: self.set_gamma(p / 100))
        self.gamma_spin.valueChanged.connect(self.set_gamma)
        return box

    def exp_to_slider(self, us):
        c = self.c
        return int(round(EXP_STEPS * np.log(us / c.exp_min) / np.log(c.exp_max / c.exp_min)))

    def slider_to_exp(self, pos):
        c = self.c
        return float(c.exp_min * (c.exp_max / c.exp_min) ** (pos / EXP_STEPS))

    @staticmethod
    def _quiet(widget, value):
        widget.blockSignals(True)
        widget.setValue(value)
        widget.blockSignals(False)

    def set_exposure_widgets(self, us):
        self._quiet(self.exp_slider, self.exp_to_slider(np.clip(us, self.c.exp_min, self.c.exp_max)))
        self._quiet(self.exp_spin, us / 1000)

    def set_gain_widgets(self, db):
        self._quiet(self.gain_slider, int(round((db - self.c.gain_min) * GAIN_STEPS)))
        self._quiet(self.gain_spin, db)

    def set_gamma_widgets(self, gamma):
        self._quiet(self.gamma_slider, int(round(gamma * 100)))
        self._quiet(self.gamma_spin, gamma)

    def set_exposure(self, us):
        us = float(np.clip(us, self.c.exp_min, self.c.exp_max))
        self.target["exposure"] = us
        self.set_exposure_widgets(us)
        self.c.controller.request("exposure", us)

    def step_exposure(self, factor):
        self.set_exposure(self.target["exposure"] * factor)

    def set_gain(self, db):
        db = float(np.clip(db, self.c.gain_min, self.c.gain_max))
        self.target["gain"] = db
        self.set_gain_widgets(db)
        self.c.controller.request("gain", db)

    def build_lut(self, g):
        x = np.arange(self.c.decode.max_value + 1, dtype=np.float32) / self.c.decode.max_value
        return np.round(255.0 * x ** (1.0 / g)).astype(np.uint8)

    def set_gamma(self, gamma):
        self.gamma = float(np.clip(gamma, GAMMA_MIN, GAMMA_MAX))
        self.set_gamma_widgets(self.gamma)
        self.lut = self.build_lut(self.gamma)
        self.redraw = True

    def show_bits(self):
        bits = format_bits(self.c.pixel_format)
        if bits in self.bits_choices:
            self.bits_combo.blockSignals(True)
            self.bits_combo.setCurrentIndex(self.bits_choices.index(bits))
            self.bits_combo.blockSignals(False)

    def on_bits(self, index):
        c = self.c
        fmt = format_for_bits(self.bits_choices[index], c.available, c.pixel_format)
        if fmt and fmt != c.pixel_format:
            self.roi_info.setText(f"switching to {fmt}...")
            c.controller.request("reconfigure", {"pixel_format": fmt})

    # ---- panel: sensor region ----
    def make_roi_box(self):
        c = self.c
        sw, sh = c.sensor
        box, g = grid_box("Sensor region (ROI)")
        g.setColumnStretch(3, 1)                # x/w and y/h boxes equally wide
        self.roi_spins = []
        # VEYE: x and width in steps of 8, y and height in steps of 4
        mw, mh = self.min_roi = VEYE_MIN_ROI if c.is_veye else (64, 64)
        for i, (name, top, step, lo) in enumerate((("x", sw - mw, 8, 0), ("y", sh - mh, 4, 0),
                                                   ("w", sw, 8, mw), ("h", sh, 4, mh))):
            spin = QtWidgets.QSpinBox(minimum=lo, maximum=top, singleStep=step,
                                      keyboardTracking=False)
            spin.valueChanged.connect(lambda _: self.on_roi_typed())
            g.addWidget(QtWidgets.QLabel(name), i // 2, 2 * (i % 2))
            g.addWidget(spin, i // 2, 2 * (i % 2) + 1)
            self.roi_spins.append(spin)
        self.roi_select = QtWidgets.QPushButton("Select", checkable=True)
        self.roi_select.setToolTip("drag a rectangle on the image")
        self.roi_select.toggled.connect(self.on_roi_select_mode)
        self.roi_apply = QtWidgets.QPushButton("Apply")
        self.roi_apply.clicked.connect(self.apply_roi)
        self.roi_full = QtWidgets.QPushButton("Full sensor")
        self.roi_full.clicked.connect(lambda: self.apply_roi((0, 0, sw, sh)))
        buttons = QtWidgets.QHBoxLayout()
        for w in (self.roi_select, self.roi_apply, self.roi_full):
            buttons.addWidget(w)
        g.addLayout(buttons, 2, 0, 1, 4)
        self.roi_info = QtWidgets.QLabel(wordWrap=True)
        g.addWidget(self.roi_info, 3, 0, 1, 4)
        self.show_roi(c.region)
        return box

    def show_roi(self, region):
        for spin, v in zip(self.roi_spins, region):
            self._quiet(spin, int(v))

    def snap_roi(self, x, y, w, h, sensor):
        """Round to the VEYE steps (x, w: 8; y, h: 4), at least the minimum size, keep it on
        the sensor (a too small selection grows around its centre)."""
        sw, sh = sensor
        mw, mh = self.min_roi
        cx, cy = x + w / 2, y + h / 2
        w = max(mw, min(sw, int(round(w / 8)) * 8))
        h = max(mh, min(sh, int(round(h / 4)) * 4))
        x, y = cx - w / 2, cy - h / 2
        x = max(0, min(sw - w, int(x) // 8 * 8))
        y = max(0, min(sh - h, int(y) // 4 * 4))
        return x, y, w, h

    def on_roi_typed(self):
        self.roi_pending = self.snap_roi(*(s.value() for s in self.roi_spins), self.c.sensor)
        self.update_roi_marks()

    def on_roi_select_mode(self, on):
        self.view.select_mode = on
        self.view.setCursor(QtCore.Qt.CrossCursor if on else QtCore.Qt.ArrowCursor)

    def on_roi_selected(self, x0, y0, x1, y1):
        rx, ry = self.c.region[:2]              # image pixels -> sensor pixels
        if x1 - x0 < 8 or y1 - y0 < 8:
            return                              # a click, not a rectangle
        self.roi_pending = self.snap_roi(rx + x0, ry + y0, x1 - x0, y1 - y0, self.c.sensor)
        self.show_roi(self.roi_pending)
        self.roi_select.setChecked(False)
        self.update_roi_marks()

    def update_roi_marks(self):
        """Pending region (yellow) on the image, in image pixels of the current region."""
        marks = []
        if self.roi_pending is not None and tuple(self.roi_pending) != tuple(self.c.region):
            rx, ry = self.c.region[:2]
            x, y, w, h = self.roi_pending
            marks.append((x - rx, y - ry, x - rx + w, y - ry + h, QtGui.QColor("#ffd600")))
        self.view.marks = marks
        self.view.update()

    def apply_roi(self, region=None):
        region = self.snap_roi(*(region or (s.value() for s in self.roi_spins)), self.c.sensor)
        if tuple(region) == tuple(self.c.region):
            return
        self.roi_pending = region
        self.roi_info.setText(f"applying {region[2]}x{region[3]} at ({region[0]}, {region[1]})...")
        self.c.controller.request("reconfigure", {"region": region})

    def after_reconfigure(self):
        """New region and/or pixel format (or the old ones back, if they failed)."""
        c = self.c
        self.setWindowTitle(f"{c.camera.get_vendor_name()} {c.camera.get_model_name()}"
                            f" - {c.width}x{c.height} {c.pixel_format}")
        self.show_roi(c.region)
        self.show_bits()
        self.roi_pending = None
        self.update_roi_marks()
        self.lut = self.build_lut(self.gamma)   # new value range
        self.update_rows_label()
        self.ytrig_spin.setMaximum(c.height - 1)
        self.img = None                         # old size/format; wait for a new frame
        self.last_exposure = None               # new frame period -> new pulse-to-frame lag

    # ---- view ----
    def on_view_changed(self):
        self.redraw = True

    # ---- panel: trigger ----
    def make_trigger_box(self):
        box, g = grid_box("Trigger")
        base = {"hw": "external trigger", "sw": "software trigger"}.get(args.trigger, "free run")
        self.base_mode = args.trigger if args.trigger in ("hw", "sw") else "free"
        self.gps_check = QtWidgets.QCheckBox(f"GPS trigger  (off: {base})")
        self.gps_check.setChecked(args.trigger == "gps")
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.addItems([f"{r} fps" for r in GPS_RATES])
        self.rate_combo.setCurrentIndex(GPS_RATES.index(args.gps_rate))
        self.trig_button = QtWidgets.QPushButton("Software trigger (t)")
        self.trig_button.clicked.connect(self.software_trigger)
        self.trig_info = QtWidgets.QLabel(wordWrap=True)
        self.delay_spin = QtWidgets.QDoubleSpinBox(decimals=3, maximum=10_000.0, suffix=" ms",
                                                   keyboardTracking=False)
        self.delay_spin.setValue((args.trigger_delay or 0) / 1000)
        self.delay_spin.setToolTip("trigger edge to exposure start, in camera units")
        self.ytrig_check = QtWidgets.QCheckBox("y-trigger row")
        self.ytrig_check.setToolTip("GPS trigger: set the delay so the timepulse (the GPS LED "
                                    "switching on) falls on this image row (orange line)")
        self.ytrig_check.setChecked(args.y_trigger is not None)
        self.ytrig_spin = QtWidgets.QSpinBox(maximum=self.c.height - 1, keyboardTracking=False)
        self.ytrig_spin.setValue(self.c.height // 2 if args.y_trigger is None else args.y_trigger)
        self.ytrig_check.setVisible(self.c.is_veye)
        self.ytrig_spin.setVisible(self.c.is_veye)
        g.addWidget(self.gps_check, 0, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("GPS frame rate"), 1, 0)
        g.addWidget(self.rate_combo, 1, 1)
        g.addWidget(QtWidgets.QLabel("Trigger delay"), 2, 0)
        g.addWidget(self.delay_spin, 2, 1)
        g.addWidget(self.ytrig_check, 3, 0)
        g.addWidget(self.ytrig_spin, 3, 1)
        g.addWidget(self.trig_button, 4, 0, 1, 2)
        g.addWidget(self.trig_info, 5, 0, 1, 2)
        self.gps_check.toggled.connect(lambda _: self.on_trigger_mode())
        self.rate_combo.currentIndexChanged.connect(lambda _: self.on_rate())
        self.delay_spin.valueChanged.connect(self.on_delay)
        self.ytrig_check.toggled.connect(lambda _: self.follow_y_trigger())
        self.ytrig_spin.valueChanged.connect(lambda _: self.follow_y_trigger())
        return box

    def mode(self):
        return "gps" if self.gps_check.isChecked() else self.base_mode

    def gps_rate(self):
        return GPS_RATES[self.rate_combo.currentIndex()]

    def predicted_lag_ns(self):
        """Frame start after the trigger pulse: VEYE frame period + ~3 ms (measured on an
        IMX178); elsewhere just a guess (exposure + 3 ms)."""
        c = self.c
        delay = trigger_delay_ns(c.is_veye)
        if c.is_veye:
            return c.camera.frame_period_ns() + LAG_EXTRA_NS + delay
        return c.controller.exposure_us * 1000 + LAG_EXTRA_NS + delay

    def on_trigger_mode(self, initial=False):
        c = self.c
        mode = self.mode()
        if not initial:
            c.controller.request("trigger", mode)
        c.grabber.frame_period_ns = (c.camera.frame_period_ns
                                     if c.is_veye and mode == "free" else None)
        self.trig_button.setVisible(mode == "sw")
        if mode == "gps":
            self.on_rate()
        else:
            c.grabber.stamper = None

    def pulse_rate(self):
        """The rate the pulses really come at: as the GPS confirmed it (its default 1 Hz
        until then), or without a GPS the selected one."""
        gps = self.c.gps
        if gps is None:
            return Fraction(self.gps_rate())
        return Fraction(gps.tp_rate).limit_denominator(1000) if gps.tp_rate else Fraction(1)

    def on_rate(self):
        c = self.c
        if self.mode() == "gps":
            c.grabber.stamper = PulseStamper(self.pulse_rate(), self.predicted_lag_ns())
            c.grabber.missed = 0
        self.last_exposure = c.controller.exposure_us
        rate = Fraction(self.gps_rate())
        if c.gps is None or self.gps_busy or rate == self.pulse_rate():
            return

        def work():
            ok = False
            try:
                ok = c.gps.set_timepulse(float(rate))
                self.gps_msg = "" if ok else "GPS rejected the timepulse setting (NAK)"
            except Exception as e:
                self.gps_msg = f"GPS: {e}"
            self.gps_busy = False
            if ok:      # stamp at the new rate (and catch up if the selection changed)
                QtCore.QMetaObject.invokeMethod(self, "on_rate_later", QtCore.Qt.QueuedConnection)

        self.gps_busy = True
        self.gps_msg = f"setting GPS timepulse to {rate} Hz..."
        threading.Thread(target=work, daemon=True).start()

    @QtCore.pyqtSlot()
    def on_rate_later(self):
        self.on_rate()

    def on_delay(self, ms):
        args.trigger_delay = ms * 1000                  # camera units (µs)
        if self.mode() != "free":
            self.c.controller.request("delay", args.trigger_delay)
        if self.mode() == "gps":
            self.on_rate()                              # new pulse-to-frame lag

    def y_trigger_delay_us(self):
        """The delay that puts the timepulse on the y-trigger row (None: off or not GPS)."""
        c = self.c
        if not (c.is_veye and self.mode() == "gps" and self.ytrig_check.isChecked()):
            return None
        line = VEYE_LINE_NS.get(format_bits(c.pixel_format), VEYE_LINE_NS[12])
        return onset_delay_us(self.ytrig_spin.value(), float(self.pulse_rate()),
                              c.camera.frame_period_ns(), c.controller.exposure_us * 1000, line)

    def follow_y_trigger(self, busy=False):
        """Keeps the delay on the y-trigger row as exposure, region, depth and rate change
        (not while recording: each change restarts the camera)."""
        us = self.y_trigger_delay_us()
        self.view.hline = None if us is None else (self.ytrig_spin.value(), QtGui.QColor("#ff9100"))
        self.view.update()
        if us is not None and not busy and abs(us - (args.trigger_delay or 0)) > 1:
            self.delay_spin.setValue(round(us) / 1000)  # -> on_delay

    def software_trigger(self):
        if self.mode() == "sw":
            self.c.camera.software_trigger()   # a single register write, fine on the GUI thread

    # ---- panel: GPS ----
    def make_gps_box(self):
        opened = self.settings.value("gps_open", True, type=bool)
        box = self.gps_box = CollapsibleBox("GPS", opened)
        box.toggled.connect(lambda o: self.settings.setValue("gps_open", o))
        g = box.grid
        self.gps_labels = {}
        rows = [("utc", "UTC (GPS)"), ("fix", "Fix"), ("sats", "Satellites"), ("dop", "DOP"),
                ("pos", "Position"), ("alt", "Altitude"), ("pulse", "Timepulse"),
                ("stamp", "Exposure start"), ("pulse_t", "Trigger pulse"),
                ("lag", "Pulse → frame"), ("rows", "Rolling shutter")]
        if self.c.compass is not None:
            rows.append(("heading", "Heading"))
        for r, (key, name) in enumerate(rows):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            lab = QtWidgets.QLabel("-", wordWrap=True)
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            g.addWidget(lab, r, 1)
            self.gps_labels[key] = lab
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.gps_labels["utc"].setFont(mono)
        self.gps_labels["stamp"].setFont(mono)
        self.gps_labels["pulse_t"].setFont(mono)
        self.update_rows_label()
        return box

    def update_rows_label(self):
        c = self.c
        if not c.is_veye:
            return
        bits = format_bits(c.pixel_format)
        line = VEYE_LINE_NS.get(bits, VEYE_LINE_NS[12])
        cal = bits in VEYE_CALIBRATED_BITS
        lab = self.gps_labels["rows"]
        lab.setText(f"row y +y×{line / 1000:.2f} µs ({c.height * line / 1e6:.1f} ms to the last row)"
                    + ("" if cal else " - estimate"))
        lab.setStyleSheet("" if cal else "color: #e65100;")
        lab.setToolTip("SER time stamps are the exposure start of row 0"
                       + ("" if cal else f"; timing measured for 12 bit only, {bits} bit "
                          "stamps may be off by a few ms"))

    # ---- panel: recording ----
    def make_record_box(self):
        box, g = grid_box("Recording")
        self.dir_edit = QtWidgets.QLineEdit(args.record_dir)
        browse = QtWidgets.QToolButton(text="…")
        browse.clicked.connect(self.browse_dir)
        self.observer_edit = QtWidgets.QLineEdit(args.observer)
        self.telescope_edit = QtWidgets.QLineEdit(args.telescope)

        self.start_now = QtWidgets.QRadioButton("Now")
        self.start_at = QtWidgets.QRadioButton("At UTC")
        self.start_time = QtWidgets.QTimeEdit(displayFormat="HH:mm:ss")
        next_min = QtWidgets.QToolButton(text="next min")
        next_min.setToolTip("next full minute at least 10 s away")
        next_min.clicked.connect(self.set_next_minute)
        if args.record_start:
            t = parse_utc(args.record_start)
            self.start_time.setTime(QtCore.QTime(t.hour, t.minute, t.second))
            self.start_at.setChecked(True)
        else:
            self.set_next_minute()
            self.start_now.setChecked(True)
        self.start_time.timeChanged.connect(lambda _: self.start_at.setChecked(True))

        self.dur_check = QtWidgets.QCheckBox("Stop after")
        self.dur_spin = QtWidgets.QDoubleSpinBox(decimals=1, suffix=" s", maximum=1e6)
        self.dur_spin.setValue(60)
        self.frames_check = QtWidgets.QCheckBox("Stop after")
        self.frames_spin = QtWidgets.QSpinBox(suffix=" frames", maximum=10**9)
        self.frames_spin.setValue(100)
        if args.record_duration:
            self.dur_check.setChecked(True)
            self.dur_spin.setValue(args.record_duration)
        if args.record_frames:
            self.frames_check.setChecked(True)
            self.frames_spin.setValue(args.record_frames)

        self.rec_button = QtWidgets.QPushButton()
        self.rec_button.setMinimumHeight(40)
        self.rec_button.clicked.connect(self.on_record_button)
        self.rec_info = QtWidgets.QLabel(wordWrap=True)
        self.rec_info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        g.addWidget(QtWidgets.QLabel("Folder"), 0, 0)
        g.addWidget(self.dir_edit, 0, 1, 1, 2)
        g.addWidget(browse, 0, 3)
        g.addWidget(QtWidgets.QLabel("Observer"), 1, 0)
        g.addWidget(self.observer_edit, 1, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Telescope"), 2, 0)
        g.addWidget(self.telescope_edit, 2, 1, 1, 3)
        g.addWidget(QtWidgets.QLabel("Start"), 3, 0)
        g.addWidget(self.start_now, 3, 1)
        g.addWidget(self.start_at, 4, 1)
        g.addWidget(self.start_time, 4, 2)
        g.addWidget(next_min, 4, 3)
        g.addWidget(self.dur_check, 5, 0, 1, 2)
        g.addWidget(self.dur_spin, 5, 2, 1, 2)
        g.addWidget(self.frames_check, 6, 0, 1, 2)
        g.addWidget(self.frames_spin, 6, 2, 1, 2)
        g.addWidget(self.rec_button, 7, 0, 1, 4)
        g.addWidget(self.rec_info, 8, 0, 1, 4)
        self.rec_widgets = [self.dir_edit, browse, self.observer_edit, self.telescope_edit,
                            self.start_now, self.start_at, self.start_time, next_min,
                            self.dur_check, self.dur_spin, self.frames_check, self.frames_spin]
        return box

    def browse_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder",
                                                       self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def set_next_minute(self):
        t = datetime.now(timezone.utc) + timedelta(seconds=10)
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.start_time.setTime(QtCore.QTime(t.hour, t.minute, 0))

    def on_record_button(self):
        rec = self.c.recorder
        if rec.state != "idle":
            rec.stop()
            return
        rec.out_dir = os.path.expanduser(self.dir_edit.text().strip() or args.record_dir)
        rec.text.update(observer=self.observer_edit.text(), telescope=self.telescope_edit.text())
        start_ns = None
        if self.start_at.isChecked():
            start = parse_utc(self.start_time.time().toString("HH:mm:ss"))
            start_ns = int(start.timestamp()) * 10**9
            print(f"Recording armed for {start.isoformat()}")
        rec.arm(start_ns, self.dur_spin.value() if self.dur_check.isChecked() else None,
                self.frames_spin.value() if self.frames_check.isChecked() else None)
        self.on_status_timer()

    # ---- timers ----
    def on_frame_timer(self):
        c = self.c
        if not c.stream_lock.acquire(blocking=False):
            return                              # the stream is being replaced (new ROI)
        try:
            item = c.grabber.take()
            if item is not None:
                buf, self.frame_id, _ = item
                try:
                    if c.recorder.state == "recording":
                        if c.recorder.latest is not None:     # already unpacked by the recorder
                            self.img, c.recorder.latest = c.recorder.latest, None
                    else:
                        self.img = c.decode(buf)    # copies the data out of the buffer
                finally:
                    c.stream.push_buffer(buf)
                self.redraw = True
        finally:
            c.stream_lock.release()
        if self.img is None or not self.redraw:
            return
        self.redraw = False
        img = self.img
        # Order: debayer (needs full res) -> crop + scale to the view -> gamma LUT
        frame = cv2.cvtColor(img, c.decode.bayer_code) if c.decode.bayer_code is not None else img
        self.view.render(frame, self.lut)
        self.disp_n += 1

    def on_status_timer(self):
        c = self.c
        now = time.monotonic()
        mode = self.mode()
        rec = c.recorder

        # ---- stall watchdog: no complete frame for a while -> report and restart ----
        # (not when triggered: then no frames just means no triggers)
        stall_s = max(3.0, 3.0 * c.controller.exposure_us / 1e6 + 1.0)
        if mode == "free" and not c.controller.busy and now - c.grabber.last_ok > stall_s:
            done, failed, underrun = c.stream.get_statistics()
            print(f"STALL: no complete frame for {now - c.grabber.last_ok:.1f} s | "
                  f"stream completed {done}, failed {failed}, underruns {underrun} | "
                  f"bad statuses {c.grabber.bad or 'none'} | lost IDs {c.grabber.dropped}")
            c.grabber.last_ok = now
            c.controller.request("restart", True)

        dt = now - self.disp_t0
        if dt >= 1.0:
            self.disp_fps, self.disp_t0, self.disp_n = self.disp_n / dt, now, 0

        if c.controller.reconfigs != self.roi_seen:
            self.roi_seen = c.controller.reconfigs
            self.after_reconfigure()

        # exposure changed -> new frame period -> new pulse-to-frame lag
        stamper = c.grabber.stamper
        if stamper is not None and c.controller.exposure_us != self.last_exposure:
            self.last_exposure = c.controller.exposure_us
            stamper.lag_ns = self.predicted_lag_ns()

        # ---- camera ----
        peak = int(self.img.max()) if self.img is not None else 0
        self.cam_info.setText(f"{c.width}x{c.height} {c.pixel_format}   "
                              f"peak {peak}/{c.decode.max_value}")
        self.update_frame_info(mode)

        # ---- trigger ----
        busy = rec.state != "idle"
        self.gps_check.setEnabled(not busy)
        self.rate_combo.setEnabled(not busy and mode == "gps" and not self.gps_busy)
        self.follow_y_trigger(busy)
        ytrig = self.view.hline is not None
        self.delay_spin.setEnabled(not busy and not ytrig)
        self.ytrig_check.setEnabled(not busy and mode == "gps")
        self.ytrig_spin.setEnabled(not busy and mode == "gps")
        info = []
        trig = ""
        if mode != "free" and c.is_veye:
            count, lost = c.camera.get_trigger_count()
            trig = f"  trig {count}" + (f" lost {lost}" if lost else "")
        if mode == "gps":
            rate = float(self.pulse_rate())
            exp_ms = c.controller.exposure_us / 1000
            if exp_ms * rate > 1000:
                info.append(f"<b style='color:#d33'>Exposure {exp_ms:.0f} ms is longer than "
                            f"the pulse period {1000 / rate:.0f} ms.</b>")
            elif c.is_veye:
                delay_ns = trigger_delay_ns(True)
                n = pulses_per_frame(rate, c.camera.frame_period_ns(), delay_ns)
                if stamper is not None:
                    stamper.stride = n
                if n > 1 and delay_ns < 1e9 / rate / 2:
                    fmax = 1e9 / (2 * c.camera.frame_period_ns() + LAG_EXTRA_NS)
                    info.append(f"<b style='color:#d33'>The camera follows only ~{fmax:.0f} fps "
                                f"here; use a smaller ROI or a lower rate.</b>")
                elif n > 1:
                    info.append(f"The delay runs past the next pulse: the camera takes every "
                                f"{n}. pulse ({rate / n:g} fps).")
                if ytrig:
                    line = VEYE_LINE_NS.get(format_bits(c.pixel_format), VEYE_LINE_NS[12])
                    info.append(f"y-trigger: timepulse on row {self.ytrig_spin.value()}; the "
                                f"edge spans ~{exp_ms * 1e6 / line:.0f} rows (shorter exposure: "
                                f"sharper)")
            if c.gps is None:
                info.append("No GPS: the trigger pulses must come at this rate by themselves.")
            elif not c.gps.has_fix():
                info.append("<b style='color:#d33'>No GPS fix: no timepulses.</b>")
            if c.grabber.missed:
                info.append(f"missed pulses: {c.grabber.missed}")
        if self.gps_msg:
            info.append(self.gps_msg)
        self.trig_info.setText("<br>".join(info))
        self.trig_info.setVisible(bool(info))

        # ---- GPS ----
        L = self.gps_labels
        if c.gps is not None:
            for key, text in c.gps.summary().items():
                if key in L:
                    L[key].setText(text)
            fix_ok = c.gps.has_fix()
            L["fix"].setStyleSheet("" if fix_ok else "color: #d33; font-weight: bold;")
            utc = c.gps.utc.strftime("%H:%M:%S") if c.gps.utc else "-"
            self.gps_box.set_summary(f"{L['fix'].text().split(',')[0]}, {c.gps.sats_used} sats, "
                                     f"{utc} UTC" + ("" if fix_ok else "  NO FIX"))
        else:
            L["utc"].setText("no GPS")
            self.gps_box.set_summary("no GPS")
        if c.compass is not None:
            h = c.compass.heading()
            L["heading"].setText(f"{h:.0f}° magnetic (uncalibrated)" if h is not None else "-")
        if c.grabber.exp_ns is not None:
            L["stamp"].setText(utc_str(c.grabber.exp_ns) + ("  GPS" if stamper else "  Pi clock"))
        L["pulse_t"].setText(utc_str(c.grabber.pulse_ns) if c.grabber.pulse_ns is not None else "-")
        L["lag"].setText(f"{stamper.last_lag_ns / 1e6:.1f} ms"
                         if stamper is not None and stamper.last_lag_ns is not None else "-")

        # ---- region ----
        for w in self.roi_spins + [self.roi_select, self.roi_apply, self.roi_full, self.bits_combo]:
            w.setEnabled(not busy and not c.controller.busy)
        x, y, w, h = c.region
        sw, sh = c.sensor
        if not c.controller.busy:
            self.roi_info.setText(f"now {w}x{h} at ({x}, {y})"
                                  + ("" if (w, h) != (sw, sh) else " - full sensor")
                                  + ("<br>locked while recording" if busy else ""))

        # ---- recording ----
        for w in self.rec_widgets:
            w.setEnabled(not busy)
        if rec.state == "recording":
            self.rec_button.setText("■  Stop recording  (r)")
            self.rec_button.setStyleSheet("background: #c62828; color: white; font-weight: bold;")
            secs = (rec.last_ns - rec.first_ns) / 1e9 if rec.first_ns else 0.0
            text = (f"<b>Recording</b> {rec.frames} frames, {secs:.1f} s"
                    + (f" <b style='color:#d33'>DROPPED {rec.dropped}</b>" if rec.dropped else "")
                    + f"<br>{os.path.basename(rec.path or '')}")
            if rec.stop_ns:
                text += f"<br>stops at {utc_str(rec.stop_ns, 0)} UTC"
        elif rec.state == "armed":
            self.rec_button.setText("Cancel  (r)")
            self.rec_button.setStyleSheet("background: #ef6c00; color: white; font-weight: bold;")
            wait = (rec.start_ns - time.time_ns()) / 1e9
            text = (f"<b>Armed</b>: first frame at {utc_str(rec.start_ns, 0)} UTC "
                    f"(in {wait:.0f} s)")
        else:
            self.rec_button.setText("●  Record  (r)")
            self.rec_button.setStyleSheet("font-weight: bold;")
            if rec.last_path:
                self.last_saved = (f"Saved {rec.last_frames} frames: "
                                   f"{os.path.basename(rec.last_path)}"
                                   + (f" ({rec.last_dropped} dropped!)" if rec.last_dropped else ""))
            text = self.last_saved
        self.rec_info.setText(text)

        # ---- status bar ----
        g = c.grabber
        self.status.setText(
            f"exp {c.controller.exposure_us / 1000:.3f} ms  gain {c.controller.gain_db:.1f} dB  "
            f"cam {g.cam_fps:.1f} fps  view {self.disp_fps:.1f} fps  lost {g.dropped}  "
            f"bad {sum(g.bad.values())}  restarts {c.controller.restarts}  "
            f"frame {self.frame_id}{trig}" + self.cursor_text())

    def cursor_text(self):
        """Sensor position and raw value under the mouse."""
        p, img = self.view.cursor_img, self.img
        if p is None or img is None:
            return ""
        ix, iy = int(p[0]), int(p[1])
        if not (0 <= ix < img.shape[1] and 0 <= iy < img.shape[0]):
            return ""
        v = img[iy, ix]
        rx, ry = self.c.region[:2]
        return f"   ({rx + ix}, {ry + iy}) = {v if np.ndim(v) == 0 else tuple(v)}"

    def update_frame_info(self, mode):
        """Frame number, measured rate and spacing of the last ~200 frames, actual exposure."""
        c, g, F = self.c, self.c.grabber, self.frame_labels
        F["frame"].setText(f"#{self.frame_id if self.frame_id is not None else '-'}"
                           f"   lost {g.dropped + g.missed}")
        iv = np.array(g.intervals, dtype=np.float64) / 1e6     # ms
        if len(iv) >= 2:
            F["rate"].setText(f"{1000 / iv.mean():.3f} fps   ({len(iv) + 1} frames)")
            F["interval"].setText(f"{iv.mean():.3f} ms  ±{iv.std():.3f}  "
                                  f"{iv.min():.2f}..{iv.max():.2f}")
        else:
            F["rate"].setText(f"{g.cam_fps:.2f} fps")
            F["interval"].setText("-")
        exp = c.controller.exposure_us / 1000
        text = f"{exp:.3f} ms"
        if abs(c.controller.exposure_us - self.target["exposure"]) > 1:
            text += f"  (asked {self.target['exposure'] / 1000:.3f})"
        if c.is_veye and mode == "free":
            text += f"  period {c.camera.frame_period_ns() / 1e6:.3f}"
        F["exp"].setText(text)
        F["exp"].setToolTip("exposure read back from the camera"
                            + ("; period: frame period the camera is set to (free run)"
                               if c.is_veye else ""))

    def closeEvent(self, event):
        self.settings.setValue("splitter", self.splitter.sizes())
        self.frame_timer.stop()
        self.status_timer.stop()
        event.accept()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
class Context:
    pass


def find_cameras():
    Aravis.update_device_list()
    cams = []
    for i in range(Aravis.get_n_devices()):
        cams.append({
            "id": Aravis.get_device_id(i),
            "vendor": Aravis.get_device_vendor(i) or "",
            "model": Aravis.get_device_model(i) or "",
            "serial": Aravis.get_device_serial_nbr(i) or "",
        })
    return cams + veye_mvcam.find_cameras()



def main(argv=None):
    global args
    args = parser.parse_args(argv)
    if args.gps_rate_alias:
        args.trigger, args.gps_rate = "gps", args.gps_rate_alias
    camera = grabber = controller = recorder = gps = None
    acquiring = False

    cams = find_cameras()
    for i, c in enumerate(cams):
        print(f"[{i}] {c['vendor']} {c['model']}  serial {c['serial']}  id '{c['id']}'")
    if args.list:
        return
    if not cams:
        raise SystemExit("No camera found.")

    if args.camera:
        key = args.camera.lower()
        matches = [c for c in cams
                   if any(key in c[f].lower() for f in ("id", "vendor", "model", "serial"))]
        if not matches:
            raise SystemExit(f"No camera matches '{args.camera}'.")
        if len(matches) > 1:
            raise SystemExit(f"'{args.camera}' matches {len(matches)} cameras; be more specific "
                             f"(e.g. use the serial number).")
        cam_info = matches[0]
    else:
        cam_info = cams[0]
        if len(cams) > 1:
            print("Several cameras found; opening the first. Use --camera to choose.")
    is_veye = cam_info.get("backend") == "veye"

    try:
        camera = veye_mvcam.Camera(cam_info) if is_veye else Aravis.Camera.new(cam_info["id"])
        print(f"Connected to: {camera.get_model_name()} ({camera.get_vendor_name()})")

        # ---- GPS ----
        if args.gps_port.lower() != "none":
            try:
                gps = gps_ublox.Gps(args.gps_port, args.gps_baud)
                print(f"GPS on {args.gps_port}")
            except Exception as e:
                print(f"No GPS ({e})")
        compass = gps_ublox.Compass.find()
        if compass is not None:
            print(f"Compass: {compass.kind}")

        # ---- Pixel format ----
        current = camera.get_pixel_format_as_string()
        available = list(camera.dup_available_pixel_formats_as_strings())
        print(f"Camera pixel formats: {', '.join(available)}")
        if args.format:
            wanted = [args.format]
        else:
            # GREY / Y10 / Y12 (P: packed) are the names a VEYE camera offers
            base = "Bayer" + current[5:7] if current.startswith("Bayer") else "Mono"
            wanted = [f.format(b=base) for f in FORMATS_BY_BITS[int(args.bits)]]
        chosen = next((f for f in wanted if f in available), None)
        if chosen is None:
            print(f"None of {wanted} supported; keeping {current}")
        elif chosen != current:
            camera.set_pixel_format_from_string(chosen)

        # ---- Region (full sensor unless --roi), no binning ----
        sensor_w, sensor_h = camera.get_sensor_size()
        print(f"Sensor: {sensor_w}x{sensor_h}, region before reset: {camera.get_region()}")
        if camera.is_binning_available():
            try_call(lambda: camera.set_binning(1, 1))
        if args.roi:
            rx, ry, rw, rh = (int(v) for v in args.roi.split(","))
            try_call(lambda: camera.set_region(rx, ry, rw, rh))
        else:
            try_call(lambda: camera.set_region(0, 0, sensor_w, sensor_h))

        x, y, width, height = camera.get_region()
        pixel_format = camera.get_pixel_format_as_string()
        print(f"Resolution: {width}x{height}, pixel format: {pixel_format}")
        decode = Decoder(pixel_format)

        # ---- Manual exposure and gain ----
        if camera.is_exposure_auto_available():
            camera.set_exposure_time_auto(Aravis.Auto.OFF)
        if camera.is_gain_auto_available():
            camera.set_gain_auto(Aravis.Auto.OFF)

        # FLIR cameras cap exposure by the frame-rate setting; release it so long
        # exposures work (frame rate then follows the exposure).
        if args.fps:
            # Fixed frame rate (Aravis handles the vendor-specific feature names)
            print(f"Setting frame rate to {args.fps} fps:")
            try_call(lambda: camera.set_frame_rate(args.fps))
        else:
            print("Disabling frame-rate limit:")
            try_call(lambda: camera.set_boolean("AcquisitionFrameRateEnable", False)) or \
                try_call(lambda: camera.set_frame_rate_enable(False))

        # ---- Trigger ----
        def apply_trigger(mode):
            """free / gps / hw / sw; call while not acquiring."""
            if mode == "free":
                try_call(lambda: camera.clear_triggers())
                return
            source = "Software" if mode == "sw" else "Line0"
            print(f"Trigger: {source}" + ("" if mode == "sw" else f", {args.trigger_edge} edge"))
            camera.set_trigger(source)
            if mode != "sw":
                try_call(lambda: camera.set_string("TriggerActivation",
                                                   args.trigger_edge.capitalize() + "Edge"))
            # always: the camera keeps a delay from an earlier session, which the time
            # stamps would not know about
            try_call(lambda: camera.set_float("TriggerDelay", args.trigger_delay or 0))

        apply_trigger("free" if args.trigger == "off" else args.trigger)

        exp_min, exp_max = camera.get_exposure_time_bounds()        # microseconds
        exp_max = min(exp_max, args.max_exposure * 1000.0)
        gain_min, gain_max = camera.get_gain_bounds()                # dB
        print(f"Exposure range: {exp_min / 1000:.3f} .. {exp_max / 1000:.1f} ms, "
              f"gain range: {gain_min:.1f} .. {gain_max:.1f} dB")

        if args.exposure is not None:
            camera.set_exposure_time(float(np.clip(args.exposure * 1000.0, exp_min, exp_max)))
        if args.gain is not None:
            camera.set_gain(float(np.clip(args.gain, gain_min, gain_max)))

        # ---- USB3 transfer settings (must be set before the stream is created) ----
        if camera.is_uv_device():
            if args.usb_mode:
                mode = Aravis.UvUsbMode.SYNC if args.usb_mode == "sync" else Aravis.UvUsbMode.ASYNC
                print(f"USB mode: {args.usb_mode}")
                try_call(lambda: camera.uv_set_usb_mode(mode))
            if args.bandwidth:
                print(f"USB bandwidth limit: {args.bandwidth} MB/s")
                try_call(lambda: camera.uv_set_bandwidth(int(args.bandwidth * 1e6)))

        # ---- Stream ----
        if is_veye:
            # configures the CSI-2 media pipeline and maps the driver's own buffers
            stream = camera.create_stream(args.buffers)
            success = veye_mvcam.SUCCESS
        else:
            stream = camera.create_stream(None, None)
            payload_size = camera.get_payload()
            for _ in range(args.buffers):
                stream.push_buffer(Aravis.Buffer.new_allocate(payload_size))
            success = Aravis.BufferStatus.SUCCESS

        stream_lock = threading.Lock()      # held while the stream is replaced

        def reconfigure(region=None, pixel_format=None):
            """New sensor region (x, y, w, h) and/or pixel format: stop, rebuild the stream,
            restart. The GUI locks this while recording, so the recorder is idle and its
            queue empty."""
            nonlocal stream, width, height, decode

            def rebuild(reg, fmt):
                nonlocal stream
                if stream is not None:
                    camera.stop_acquisition()
                    if is_veye:
                        stream.close()
                stream = ctx.stream = grabber.stream = None
                camera.set_pixel_format_from_string(fmt)
                camera.set_region(*reg)
                if is_veye:
                    stream = camera.create_stream(args.buffers)
                else:
                    stream = camera.create_stream(None, None)
                    payload = camera.get_payload()
                    for _ in range(args.buffers):
                        stream.push_buffer(Aravis.Buffer.new_allocate(payload))
                camera.start_acquisition()

            old = ctx.region, ctx.pixel_format
            new = region or ctx.region, pixel_format or ctx.pixel_format
            with stream_lock:
                grabber.pause()
                grabber.take()              # a buffer of the old stream goes with it
                try:
                    rebuild(*new)
                except Exception as e:
                    print(f"{new[1]} {new[0]} failed ({e}); back to {old[1]} {old[0]}")
                    rebuild(*old)
                ctx.region = tuple(camera.get_region())
                ctx.pixel_format = camera.get_pixel_format_as_string()
                width, height = ctx.region[2:]
                decode = ctx.decode = Decoder(ctx.pixel_format)
                ctx.stream, ctx.width, ctx.height = stream, width, height
                recorder.width, recorder.height = width, height
                recorder.depth = int(decode.max_value).bit_length()
                grabber.resume(stream)
            print(f"Region: {width}x{height} at ({ctx.region[0]}, {ctx.region[1]}), {ctx.pixel_format}")

        controller = Controller(camera, apply_trigger, reconfigure)
        controller.start()
        recorder = Recorder(
            lambda raw: decode.raw(raw, width, height), width, height,     # follows a new ROI
            int(decode.max_value).bit_length(), args.record_dir,
            name=camera.get_model_name().replace(" ", "_"),
            instrument=f"{camera.get_vendor_name()} {camera.get_model_name()}",
            observer=args.observer, telescope=args.telescope)
        grabber = Grabber(stream, success, recorder)

        def exposure_start(frame_ns, pulse_ns):
            """Row-0 exposure start: from the trigger pulse when GPS-triggered (exact, no Pi
            clock), else from the frame-start time (Pi clock). Cameras other than VEYE: not
            measured; assumes frame start - exposure, or the pulse (+ delay)."""
            exp_ns = controller.exposure_us * 1000
            if pulse_ns is None:
                return int(frame_ns - exp_ns - (VEYE_FREE_LEAD_NS if is_veye else 0))
            pulse_ns += int(trigger_delay_ns(is_veye))
            if is_veye:
                return int(pulse_ns + camera.frame_period_ns() - exp_ns + VEYE_TRIGGER_OFFSET_NS)
            return pulse_ns

        grabber.exposure_start = exposure_start
        grabber.start()

        camera.start_acquisition()
        acquiring = True

        ctx = Context()
        ctx.__dict__.update(camera=camera, controller=controller, grabber=grabber,
                            recorder=recorder, gps=gps, compass=compass, stream=stream,
                            decode=decode, is_veye=is_veye, width=width, height=height,
                            pixel_format=pixel_format, exp_min=exp_min, exp_max=exp_max,
                            gain_min=gain_min, gain_max=gain_max, stream_lock=stream_lock,
                            region=tuple(camera.get_region()), sensor=(sensor_w, sensor_h),
                            available=available)
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        signal.signal(signal.SIGINT, lambda *_: app.quit())     # Ctrl+C in the terminal
        win = MainWindow(ctx)
        if args.record_start:
            win.on_record_button()          # armed from the command line
        win.show()
        print("Streaming. Keys: e/E exposure, g/G gain, d/D gamma, r record, "
              + ("t trigger, " if args.trigger == "sw" else "") + "q quit.")
        app.exec_()

    finally:
        print("\nShutting down stream safely...")
        if grabber is not None:
            grabber.running = False
            grabber.join(timeout=2)
        if recorder is not None:
            recorder.close()            # writes the timestamp trailer
        if controller is not None:
            controller.stop()
            controller.join(timeout=2)
        if camera is not None and acquiring:
            camera.stop_acquisition()
        if camera is not None:
            try_call(lambda: camera.clear_triggers())   # leave the camera free-running
        if gps is not None:
            gps.close()                 # back to the GPS' default 1 Hz PPS


if __name__ == "__main__":
    main()
