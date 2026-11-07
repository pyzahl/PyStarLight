"""ZWO ASI cameras (USB2 / USB3) through ZWO's own library (libASICamera2, the ASI SDK), behind
the same Aravis-like API as the other backends (see cameras.py). ctypes only, no Python
package: it runs with the Pi's system Python like the rest of the capture code.

The library is ZWO's (closed source, free download: https://www.zwoastro.com/software/ →
"For Developers", Linux SDK, lib/<arch>/libASICamera2.so). Found via $ZWO_ASI_LIB (the name
the zwoasi package uses), the linker's search path, /usr/local/lib or /usr/lib/<arch>-linux-gnu
(INDI's libasi package puts it there). Without it no ASI
camera is listed. Linux also needs ZWO's udev rule (lib/asi.rules) for access without root,
and more USB buffer memory for USB3 cameras (see CAPTURE.md, "ZWO ASI cameras").

Timing is limited, as for a video device: an ASI camera free-runs (the trigger modes of the
few trigger cameras are not used) and the SDK gives no time stamp. A frame is stamped with
the Pi clock (NTP) when it has arrived over USB; the exposure start is taken as that minus the
exposure (the readout and transfer time are not known, some ms to a frame). Frames the SDK
dropped (ASIGetDroppedFrames) leave a gap in the frame numbers.

Pixel formats: RAW8 and RAW16 as Mono8 / Mono16 (colour cameras: Bayer<pattern>8 / 16).
RAW16 holds the ADC's bits left-aligned in 16 (a 12-bit camera reads 0 .. 65520), as ASTRID.
Gain is in ZWO's unit, 0.1 dB, shown as dB. The region is the camera's own (ASISetROIFormat,
width a multiple of 8, height of 2), set while not streaming.
"""
import collections
import ctypes
import ctypes.util
import os
import threading
import time

import numpy as np

SUCCESS = "success"
LIB_NAMES = ("/usr/local/lib/libASICamera2.so", "/usr/lib/libASICamera2.so",
             f"/usr/lib/{os.uname().machine}-linux-gnu/libASICamera2.so")   # Debian / INDI's libasi

# ASI_IMG_TYPE, ASI_BAYER_PATTERN, ASI_CONTROL_TYPE (ASICamera2.h)
IMG_RAW8, IMG_RGB24, IMG_RAW16, IMG_Y8, IMG_END = 0, 1, 2, 3, -1
BAYER = {0: "BayerRG", 1: "BayerBG", 2: "BayerGR", 3: "BayerGB"}
GAIN, EXPOSURE, GAMMA, WB_R, WB_B, OFFSET, BANDWIDTH, OVERCLOCK, TEMPERATURE, FLIP = range(10)
HIGH_SPEED_MODE = 14
ERRORS = ("SUCCESS", "INVALID_INDEX", "INVALID_ID", "INVALID_CONTROL_TYPE", "CAMERA_CLOSED",
          "CAMERA_REMOVED", "INVALID_PATH", "INVALID_FILEFORMAT", "INVALID_SIZE", "INVALID_IMGTYPE",
          "OUTOF_BOUNDARY", "TIMEOUT", "INVALID_SEQUENCE", "BUFFER_TOO_SMALL", "VIDEO_MODE_ACTIVE",
          "EXPOSURE_IN_PROGRESS", "GENERAL_ERROR", "INVALID_MODE")
ERROR_TIMEOUT = 11


class _CameraInfo(ctypes.Structure):
    _fields_ = [("Name", ctypes.c_char * 64), ("CameraID", ctypes.c_int),
                ("MaxHeight", ctypes.c_long), ("MaxWidth", ctypes.c_long),
                ("IsColorCam", ctypes.c_int), ("BayerPattern", ctypes.c_int),
                ("SupportedBins", ctypes.c_int * 16), ("SupportedVideoFormat", ctypes.c_int * 8),
                ("PixelSize", ctypes.c_double), ("MechanicalShutter", ctypes.c_int),
                ("ST4Port", ctypes.c_int), ("IsCoolerCam", ctypes.c_int), ("IsUSB3Host", ctypes.c_int),
                ("IsUSB3Camera", ctypes.c_int), ("ElecPerADU", ctypes.c_float),
                ("BitDepth", ctypes.c_int), ("IsTriggerCam", ctypes.c_int), ("Unused", ctypes.c_char * 16)]


class _ControlCaps(ctypes.Structure):
    _fields_ = [("Name", ctypes.c_char * 64), ("Description", ctypes.c_char * 128),
                ("MaxValue", ctypes.c_long), ("MinValue", ctypes.c_long), ("DefaultValue", ctypes.c_long),
                ("IsAutoSupported", ctypes.c_int), ("IsWritable", ctypes.c_int),
                ("ControlType", ctypes.c_int), ("Unused", ctypes.c_char * 32)]


_lib = None


def library():
    """ZWO's libASICamera2 (loaded once), or None when it is not installed."""
    global _lib
    if _lib is None:
        names = [os.environ.get("ZWO_ASI_LIB"), ctypes.util.find_library("ASICamera2"), *LIB_NAMES]
        for name in filter(None, names):
            try:
                lib = ctypes.CDLL(name)
            except OSError:
                continue
            lib.ASIGetSDKVersion.restype = ctypes.c_char_p
            _lib = lib
            break
    return _lib


def check(code, what):
    if code:
        name = ERRORS[code] if 0 <= code < len(ERRORS) else str(code)
        raise RuntimeError(f"ASI {what}: {name}")


def sdk_version(lib=None):
    lib = lib or library()
    try:
        v = (lib.ASIGetSDKVersion() or b"").decode(errors="replace")
    except Exception:
        return ""
    try:                                # "1, 27, 0, 0" -> "1.27"
        parts = [int(x) for x in v.replace(",", " ").split()]
        while len(parts) > 2 and parts[-1] == 0:
            parts.pop()
        return ".".join(map(str, parts))
    except ValueError:
        return v


def camera_info(lib, index):
    info = _CameraInfo()
    check(lib.ASIGetCameraProperty(ctypes.pointer(info), index), "camera property")
    return info


def find_cameras():
    """The connected ASI cameras ([] without ZWO's library)."""
    lib = library()
    if lib is None:
        return []
    cams = []
    for i in range(lib.ASIGetNumOfConnectedCameras()):
        info = camera_info(lib, i)
        name = info.Name.decode(errors="replace")
        cams.append({"id": f"asi:{info.CameraID}:{name}", "vendor": "ZWO",
                     "model": name.removeprefix("ZWO ").strip(),
                     "serial": "", "backend": "asi", "index": i})
    return cams


def format_name(img_type, color, bayer):
    base = BAYER.get(bayer, "BayerRG") if color else "Mono"
    return {IMG_RAW8: f"{base}8", IMG_RAW16: f"{base}16"}.get(img_type)


# ---------------------------------------------------------------------------
# Stream and buffers
# ---------------------------------------------------------------------------
class Buffer:
    """A filled buffer; hand it back with Stream.push_buffer()."""

    def __init__(self, stream, index, status, frame_id, timestamp_ns):
        self.stream, self.index = stream, index
        self.status, self.frame_id = status, frame_id
        self.timestamp_ns = timestamp_ns

    def get_system_timestamp(self):
        """UTC (ns, Pi clock) when the frame had arrived over USB."""
        return self.timestamp_ns

    def get_status(self):
        return self.status

    def get_frame_id(self):
        return self.frame_id

    def get_image_width(self):
        return self.stream.width

    def get_image_height(self):
        return self.stream.height

    def get_data(self):
        return self.stream.buffers[self.index].tobytes()


class Stream:
    """Video capture into a ring of our own buffers. A reader thread of its own does nothing
    but take frames from the SDK (ASIGetVideoData copies each into a free buffer), so the SDK's
    few internal buffers never wait for the rest of the pipeline (live view, recorder); frames
    ready for the grabber queue up here. The ring holds about RING_BYTES (at least n_buffers).
    """

    ts_soe = False                      # the time is the end of the transfer
    RING_BYTES = 512e6

    def __init__(self, camera, n_buffers):
        self.lib, self.cam_id = camera.lib, camera.cam_id
        self.width, self.height = camera.region[2:]
        self.payload = self.width * self.height * camera.bytes_per_pixel
        n = max(2, n_buffers, min(256, int(self.RING_BYTES // self.payload)))
        self.buffers = [np.empty(self.payload, np.uint8) for _ in range(n)]
        self.pointers = [b.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)) for b in self.buffers]
        self.idle = collections.deque(range(n))
        self.ready = collections.deque()          # (index, utc_ns, frame_id) for the grabber
        self.cond = threading.Condition()
        self.streaming = self.closed = False
        self.completed = self.failed = self.underruns = 0
        self.dropped_base = 0
        self.reader = None

    def dropped(self):
        n = ctypes.c_int(0)
        self.lib.ASIGetDroppedFrames(self.cam_id, ctypes.pointer(n))
        return n.value

    def start(self):
        if self.streaming:
            return
        check(self.lib.ASIStartVideoCapture(self.cam_id), "start video capture")
        self.dropped_base = self.dropped()
        self.streaming = True
        self.reader = threading.Thread(target=self._read, name="asi-reader", daemon=True)
        self.reader.start()

    def _read(self):
        """The reader thread: SDK -> free buffer -> ready, as fast as frames come."""
        while self.streaming:
            with self.cond:
                index = self.idle.popleft() if self.idle else None
            if index is None:           # the grabber holds every buffer: the SDK may drop
                self.underruns += 1
                time.sleep(0.001)
                continue
            code = self.lib.ASIGetVideoData(self.cam_id, self.pointers[index], self.payload, 200)
            utc_ns = time.time_ns()
            if code:
                with self.cond:
                    self.idle.appendleft(index)
                if code != ERROR_TIMEOUT:
                    self.failed += 1
                    time.sleep(0.001)
                continue
            self.completed += 1
            frame_id = self.completed - 1 + self.dropped() - self.dropped_base
            with self.cond:
                self.ready.append((index, utc_ns, frame_id))
                self.cond.notify()

    def stop(self):
        if not self.streaming:
            return
        self.streaming = False
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=2.0)
        self.reader = None
        self.lib.ASIStopVideoCapture(self.cam_id)
        with self.cond:                 # frames not taken: back to the free ones
            self.idle.extend(i for i, _, _ in self.ready)
            self.ready.clear()

    def push_buffer(self, buf):
        with self.cond:
            if not self.closed and buf.index not in self.idle:
                self.idle.append(buf.index)

    def timeout_pop_buffer(self, timeout_us):
        with self.cond:
            if not self.ready:
                self.cond.wait(timeout_us / 1e6)
            if not self.ready:
                return None
            index, utc_ns, frame_id = self.ready.popleft()
        return Buffer(self, index, SUCCESS, frame_id, utc_ns)

    def get_statistics(self):
        return self.completed, self.failed, self.underruns

    def close(self):
        self.stop()
        with self.cond:
            self.closed = True


# ---------------------------------------------------------------------------
# The camera
# ---------------------------------------------------------------------------
class Camera:
    kind = "asi"
    success = SUCCESS
    can_trigger = False
    exposure_fixed = gain_fixed = False

    def __init__(self, info, lib=None):
        self.info = info
        self.lib = lib or library()
        if self.lib is None:
            raise RuntimeError("ZWO's libASICamera2 not found (set ZWO_ASI_LIB)")
        props = camera_info(self.lib, info.get("index", 0))
        self.cam_id = props.CameraID
        self.name = props.Name.decode(errors="replace")
        check(self.lib.ASIOpenCamera(self.cam_id), "open camera")
        check(self.lib.ASIInitCamera(self.cam_id), "init camera")
        self.sensor = (int(props.MaxWidth), int(props.MaxHeight))
        self.pixel_um = float(props.PixelSize) or None
        self.bit_depth = int(props.BitDepth)
        self.usb3 = bool(props.IsUSB3Camera and props.IsUSB3Host)
        self.img_types = {}
        for t in props.SupportedVideoFormat:
            if t == IMG_END:
                break
            name = format_name(t, props.IsColorCam, props.BayerPattern)
            if name:
                self.img_types[name] = t
        if not self.img_types:
            raise RuntimeError(f"{self.name}: neither RAW8 nor RAW16 offered")
        self.pixel_format = next((f for f in self.img_types if f.endswith("16")), next(iter(self.img_types)))
        self.caps = {}
        n = ctypes.c_int(0)
        self.lib.ASIGetNumOfControls(self.cam_id, ctypes.pointer(n))
        for i in range(n.value):
            c = _ControlCaps()
            if not self.lib.ASIGetControlCaps(self.cam_id, i, ctypes.pointer(c)):
                self.caps[c.ControlType] = c
        self.region = (0, 0, *self.sensor)
        self.stream = None

    @property
    def bytes_per_pixel(self):
        return 2 if self.pixel_format.endswith("16") else 1

    def _get(self, ctrl):
        v, auto = ctypes.c_long(0), ctypes.c_int(0)
        check(self.lib.ASIGetControlValue(self.cam_id, ctrl, ctypes.pointer(v), ctypes.pointer(auto)),
              f"get control {ctrl}")
        return v.value

    def _set(self, ctrl, value, auto=False):
        c = self.caps.get(ctrl)
        if c is not None:
            value = min(max(int(round(value)), c.MinValue), c.MaxValue)
        check(self.lib.ASISetControlValue(self.cam_id, ctrl, ctypes.c_long(int(value)), int(auto)),
              f"set control {ctrl}")

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.lib.ASICloseCamera(self.cam_id)

    # ---- identity ----
    def get_model_name(self):
        return self.name.removeprefix("ZWO ").strip()

    def get_vendor_name(self):
        return "ZWO"

    def is_uv_device(self):
        return False

    def describe(self):
        """'ZWO ASI SDK 1.36, USB3, 12-bit ADC, 5.86 um pixels, sensor 21.5 C' (recording comment)."""
        parts = [f"ZWO ASI SDK {sdk_version(self.lib)}".strip(), "USB3" if self.usb3 else "USB2",
                 f"{self.bit_depth}-bit ADC"]
        if self.pixel_um:
            parts.append(f"{self.pixel_um:g} um pixels")
        try:
            usb = self.usb_settings()
        except RuntimeError:
            usb = {}
        if "bandwidth_pct" in usb:
            parts.append(f"USB bandwidth {usb['bandwidth_pct']} %")
        if "high_speed" in usb:
            parts.append("high speed " + ("on" if usb["high_speed"] else "off"))
        if TEMPERATURE in self.caps:
            try:
                parts.append(f"sensor {self._get(TEMPERATURE) / 10:.1f} C")
            except RuntimeError:
                pass
        return ", ".join(parts)

    # ---- USB bandwidth, high-speed mode (the camera keeps another program's settings) ----
    def usb_settings(self):
        """{'bandwidth_pct', 'high_speed'} as the camera has them now (missing: no such control)."""
        out = {}
        if BANDWIDTH in self.caps:
            out["bandwidth_pct"] = self._get(BANDWIDTH)
        if HIGH_SPEED_MODE in self.caps:
            out["high_speed"] = bool(self._get(HIGH_SPEED_MODE))
        return out

    def set_usb_bandwidth(self, pct):
        """USB bandwidth the camera may use, % (ZWO: 40 .. 100; more: faster, fewer margins)."""
        if BANDWIDTH in self.caps:
            self._set(BANDWIDTH, pct)

    def set_high_speed(self, on):
        """High-speed mode: faster readout (on some sensors a lower ADC depth)."""
        if HIGH_SPEED_MODE in self.caps:
            self._set(HIGH_SPEED_MODE, 1 if on else 0)

    # ---- format and region ----
    def get_pixel_format_as_string(self):
        return self.pixel_format

    def dup_available_pixel_formats_as_strings(self):
        return list(self.img_types)

    def set_pixel_format_from_string(self, name):
        if name not in self.img_types:
            raise ValueError(f"{self.name}: no pixel format {name} (has {', '.join(self.img_types)})")
        self.pixel_format = name

    def get_sensor_size(self):
        return self.sensor

    def is_binning_available(self):
        return False

    def set_region(self, x, y, w, h):
        """The SDK wants a width in steps of 8 and a height in steps of 2; applied at the next stream."""
        fw, fh = self.sensor
        w = max(64, min(int(w), fw)) // 8 * 8
        h = max(64, min(int(h), fh)) // 2 * 2
        x, y = min(max(0, int(x)), fw - w), min(max(0, int(y)), fh - h)
        self.region = (x, y, w, h)

    def get_region(self):
        return self.region

    # ---- exposure, gain, frame rate ----
    def is_exposure_auto_available(self):
        return False

    def set_exposure_time_auto(self, mode):
        pass

    def is_gain_auto_available(self):
        return False

    def set_gain_auto(self, mode):
        pass

    def set_manual(self):
        for ctrl in (EXPOSURE, GAIN):
            if ctrl in self.caps:
                self._set(ctrl, self._get(ctrl), auto=False)

    def frame_period_ns(self):
        return None                     # free-running: as fast as exposure and USB allow

    def set_frame_rate(self, fps):
        raise ValueError("an ASI camera runs as fast as its exposure allows (no frame-rate setting)")

    def set_frame_rate_enable(self, enable):
        pass

    def set_boolean(self, feature, value):
        if feature != "AcquisitionFrameRateEnable":
            raise ValueError(f"unknown feature {feature}")

    def get_exposure_time_bounds(self):
        c = self.caps[EXPOSURE]
        return float(max(c.MinValue, 1)), float(c.MaxValue)        # microseconds

    def get_exposure_time(self):
        return float(self._get(EXPOSURE))

    def set_exposure_time(self, us):
        self._set(EXPOSURE, us)

    def get_gain_bounds(self):
        c = self.caps.get(GAIN)
        return (0.0, 0.001) if c is None else (c.MinValue / 10.0, c.MaxValue / 10.0)

    def get_gain(self):
        return self._get(GAIN) / 10.0 if GAIN in self.caps else 0.0

    def set_gain(self, db):
        if GAIN in self.caps:
            self._set(GAIN, db * 10.0)

    # ---- no trigger (free run only) ----
    def set_trigger(self, source):
        raise RuntimeError(f"{self.name}: free run only (the ASI trigger modes are not used)")

    def clear_triggers(self):
        pass

    def software_trigger(self):
        raise RuntimeError(f"{self.name}: no software trigger")

    def get_trigger_count(self):
        return 0, 0

    def set_string(self, feature, value):
        raise ValueError(f"unknown feature {feature}")

    def set_float(self, feature, value):
        raise ValueError(f"unknown feature {feature}")

    # ---- streaming ----
    def create_stream(self, n_buffers):
        x, y, w, h = self.region
        check(self.lib.ASISetROIFormat(self.cam_id, w, h, 1, self.img_types[self.pixel_format]),
              f"region {w}x{h} {self.pixel_format}")
        check(self.lib.ASISetStartPos(self.cam_id, x, y), f"region start ({x}, {y})")
        self.stream = Stream(self, n_buffers)
        return self.stream

    def close_stream(self, stream):
        stream.close()

    def get_payload(self):
        return self.stream.payload

    def start_acquisition(self):
        self.stream.start()

    def stop_acquisition(self):
        self.stream.stop()
