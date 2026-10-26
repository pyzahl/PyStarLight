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

GPS_RATES = ["30", "20", "10", "8", "6", "4", "2", "1", "0.5"]     # fps

parser = argparse.ArgumentParser(description="Aravis / VEYE MIPI live view")
parser.add_argument("--camera", default=None,
                    help="which camera to open: any part of its ID, vendor, model or serial "
                         "(e.g. Basler, a2A1920, Grasshopper, 2316xxxx, VEYE, IMX178). "
                         "Default: first found")
parser.add_argument("--list", action="store_true", help="list connected cameras and exit")
parser.add_argument("--bits", choices=["12", "8"], default="12",
                    help="12 = keep full 12-bit data (packed, unpacked on the host); "
                         "8 = switch camera to 8-bit")
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
# Row y starts y * 14.33 us after row 0 (29.6 ms over all 2064 rows).
VEYE_FREE_LEAD_NS = 3_330_000
VEYE_TRIGGER_OFFSET_NS = 330_000
VEYE_LINE_NS = 14_330


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
        self.running = True
        self.last_ok = time.monotonic()

    def run(self):
        last_id = None
        last_ns, last_starts = None, None
        last_stamper, last_pulse = None, None
        t0, n = time.monotonic(), 0
        while self.running:
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
            last_ns, last_starts = frame_ns, starts
            stamper = self.stamper
            pulse = None
            if stamper is not None:
                pulse = stamper(frame_ns)
                if stamper is last_stamper and last_pulse is not None:
                    gap = pulse - last_pulse
                    if gap > 1.5 * stamper.period_ns:
                        self.missed += round(gap / stamper.period_ns) - 1
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

    def __init__(self, camera, apply_trigger):
        super().__init__(daemon=True)
        self.camera = camera
        self.apply_trigger = apply_trigger  # mode -> configures the camera (not acquiring)
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
                if "trigger" in todo:
                    print(f"Trigger mode: {todo['trigger']}")
                    self.camera.stop_acquisition()
                    self.apply_trigger(todo["trigger"])
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
class ImageView(QtWidgets.QLabel):
    """Shows the newest frame scaled to fit (aspect kept)."""

    def __init__(self):
        super().__init__()
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.setMinimumSize(320, 240)
        self.setStyleSheet("background: black; color: gray;")
        self.setText("waiting for frames...")

    def fit_scale(self, w, h):
        return min(self.width() / w, self.height() / h)

    def show_frame(self, disp):
        """disp: uint8 (h, w) grey or (h, w, 3) BGR, already at display size."""
        disp = np.ascontiguousarray(disp)
        h, w = disp.shape[:2]
        fmt = QtGui.QImage.Format_Grayscale8 if disp.ndim == 2 else QtGui.QImage.Format_BGR888
        img = QtGui.QImage(disp.data, w, h, disp.strides[0], fmt).copy()
        self.setPixmap(QtGui.QPixmap.fromImage(img))


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
        # last requested values (keys step from these, not from a lagging camera read-back)
        self.target = {"exposure": ctx.controller.exposure_us, "gain": ctx.controller.gain_db}

        self.view = ImageView()
        panel = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(panel)
        col.addWidget(self.make_camera_box())
        col.addWidget(self.make_trigger_box())
        col.addWidget(self.make_gps_box())
        col.addWidget(self.make_record_box())
        col.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(panel.sizeHint().width() + 24)

        central = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(central)
        row.setContentsMargins(4, 4, 4, 4)
        row.addWidget(self.view, 1)
        row.addWidget(scroll)
        self.setCentralWidget(central)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.resize(int(ctx.width * args.scale) + scroll.width() + 20,
                    max(int(ctx.height * args.scale) + 40, 600))

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
        g.addWidget(self.gps_check, 0, 0, 1, 2)
        g.addWidget(QtWidgets.QLabel("GPS frame rate"), 1, 0)
        g.addWidget(self.rate_combo, 1, 1)
        g.addWidget(self.trig_button, 2, 0, 1, 2)
        g.addWidget(self.trig_info, 3, 0, 1, 2)
        self.gps_check.toggled.connect(lambda _: self.on_trigger_mode())
        self.rate_combo.currentIndexChanged.connect(lambda _: self.on_rate())
        return box

    def mode(self):
        return "gps" if self.gps_check.isChecked() else self.base_mode

    def gps_rate(self):
        return GPS_RATES[self.rate_combo.currentIndex()]

    def predicted_lag_ns(self):
        """Frame start after the trigger pulse: VEYE frame period + ~3 ms (measured on an
        IMX178); elsewhere just a guess (exposure + 3 ms)."""
        c = self.c
        delay = (args.trigger_delay or 0) * 1000
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

    def software_trigger(self):
        if self.mode() == "sw":
            self.c.camera.software_trigger()   # a single register write, fine on the GUI thread

    # ---- panel: GPS ----
    def make_gps_box(self):
        box, g = grid_box("GPS")
        self.gps_labels = {}
        rows = [("utc", "UTC (GPS)"), ("fix", "Fix"), ("sats", "Satellites"), ("dop", "DOP"),
                ("pos", "Position"), ("alt", "Altitude"), ("pulse", "Timepulse"),
                ("stamp", "Exposure start"), ("pulse_t", "Trigger pulse"),
                ("lag", "Pulse → frame"), ("rows", "Rolling shutter")]
        if self.c.compass is not None:
            rows.append(("heading", "Heading"))
        for r, (key, name) in enumerate(rows):
            g.addWidget(QtWidgets.QLabel(name), r, 0)
            lab = QtWidgets.QLabel("-")
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            g.addWidget(lab, r, 1)
            self.gps_labels[key] = lab
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.gps_labels["utc"].setFont(mono)
        self.gps_labels["stamp"].setFont(mono)
        self.gps_labels["pulse_t"].setFont(mono)
        if self.c.is_veye:
            self.gps_labels["rows"].setText(
                f"row y +y×{VEYE_LINE_NS / 1000:.2f} µs "
                f"({self.c.height * VEYE_LINE_NS / 1e6:.1f} ms to the last row)")
            self.gps_labels["rows"].setToolTip("SER time stamps are the exposure start of row 0")
        return box

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
        if self.img is None or not self.redraw:
            return
        self.redraw = False
        img = self.img
        # Order: debayer (needs full res) -> downscale -> gamma LUT
        frame = cv2.cvtColor(img, c.decode.bayer_code) if c.decode.bayer_code is not None else img
        h, w = frame.shape[:2]
        s = self.view.fit_scale(w, h)
        if s < 1.0:
            frame = cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))),
                               interpolation=cv2.INTER_AREA)
        self.view.show_frame(np.take(self.lut, frame, mode="clip"))
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

        # exposure changed -> new frame period -> new pulse-to-frame lag
        stamper = c.grabber.stamper
        if stamper is not None and c.controller.exposure_us != self.last_exposure:
            self.last_exposure = c.controller.exposure_us
            stamper.lag_ns = self.predicted_lag_ns()

        # ---- camera ----
        peak = int(self.img.max()) if self.img is not None else 0
        self.cam_info.setText(f"{c.width}x{c.height} {c.pixel_format}   "
                              f"peak {peak}/{c.decode.max_value}")

        # ---- trigger ----
        busy = rec.state != "idle"
        self.gps_check.setEnabled(not busy)
        self.rate_combo.setEnabled(not busy and mode == "gps" and not self.gps_busy)
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
                fmax = 1e9 / (2 * c.camera.frame_period_ns() + LAG_EXTRA_NS)
                if rate > fmax:
                    info.append(f"<b style='color:#d33'>The camera follows only ~{fmax:.0f} fps "
                                f"here; use a smaller ROI or a lower rate.</b>")
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
        else:
            L["utc"].setText("no GPS")
        if c.compass is not None:
            h = c.compass.heading()
            L["heading"].setText(f"{h:.0f}° magnetic (uncalibrated)" if h is not None else "-")
        if c.grabber.exp_ns is not None:
            L["stamp"].setText(utc_str(c.grabber.exp_ns) + ("  GPS" if stamper else "  Pi clock"))
        L["pulse_t"].setText(utc_str(c.grabber.pulse_ns) if c.grabber.pulse_ns is not None else "-")
        L["lag"].setText(f"{stamper.last_lag_ns / 1e6:.1f} ms"
                         if stamper is not None and stamper.last_lag_ns is not None else "-")

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
            f"frame {self.frame_id}{trig}")

    def closeEvent(self, event):
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
            base = "Bayer" + current[5:7] if current.startswith("Bayer") else "Mono"
            # GREY / Y12 / Y12P are the names a VEYE camera offers
            wanted = ([base + "8", "GREY"] if args.bits == "8"
                      else [base + "12p", base + "12Packed", base + "16", "Y12", "Y12P"])
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
            if args.trigger_delay is not None:
                try_call(lambda: camera.set_float("TriggerDelay", args.trigger_delay))

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

        controller = Controller(camera, apply_trigger)
        controller.start()
        recorder = Recorder(
            lambda raw: decode.raw(raw, width, height), width, height,
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
            pulse_ns += int((args.trigger_delay or 0) * 1000)
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
                            gain_min=gain_min, gain_max=gain_max)
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
