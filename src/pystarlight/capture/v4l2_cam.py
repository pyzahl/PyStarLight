"""Any Linux video device (/dev/videoN: USB video grabbers for analog cameras, UVC webcams,
HDMI dongles) behind the same Aravis-like API as the other backends (see cameras.py).

Timing is limited: such devices cannot be triggered, and the time stamp of a frame is the
driver's (V4L2 buffer time, usually the start or end of the frame's transfer; CLOCK_MONOTONIC,
turned into UTC by the Pi clock, NTP), not the exposure. Analog cameras often carry their own
time: a VTI time code burned into the image (read later by timestamps.VtiReader:
`pipeline.analyze(..., vti=...)`), or a GPS-driven LED flashing in the field. So frames are
recorded as they come, full size and unscaled, both fields of an interlaced frame woven.

Pixel formats (mono only: the luminance is kept): GREY and Y16 as they are; YUYV / UYVY
(analog grabbers, webcams) give their Y bytes; MJPG is decoded to gray (compressed: worse for
photometry and for a time-code OCR; take YUYV when the device offers it). The region is cut
out of the full frame in software. Exposure and gain are the device's V4L2 controls where it
has them (an analog grabber has none: the camera sets them itself).

Device options (info dict, from the command line): "std" (PAL / NTSC / SECAM or a number,
VIDIOC_S_STD), "input" (VIDIOC_S_INPUT, e.g. composite vs S-video), "size" ((w, h)).
"""
import ctypes
import errno
import fcntl
import glob
import os
import select
import struct
import time
from fractions import Fraction

import numpy as np

from . import veye_mvcam as vm

SUCCESS = vm.SUCCESS
SUPPORTED = ("GREY", "Y16", "YUYV", "UYVY", "MJPG")     # in order of preference
NOT_CAMERAS = ("rp1-cfe", "pispbe", "bcm2835-codec", "bcm2835-isp", "rpi-hevc-dec", "unicam")

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_STREAMING = 0x04000000
V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC = 0x2000
V4L2_BUF_FLAG_TIMESTAMP_MASK = 0xE000
V4L2_BUF_FLAG_TSTAMP_SRC_SOE = 0x10000
V4L2_BUF_FLAG_TSTAMP_SRC_MASK = 0x70000
V4L2_CTRL_FLAG_DISABLED = 0x0001
V4L2_FRMSIZE_TYPE_DISCRETE = 1

CID_GAIN = 0x00980913
CID_EXPOSURE_AUTO = 0x009A0901        # menu: 0 auto, 1 manual, 2 shutter priority, 3 aperture priority
CID_EXPOSURE_ABSOLUTE = 0x009A0902    # 100 us units
STANDARDS = {"PAL": 0x000000FF, "NTSC": 0x0000B000, "SECAM": 0x00FF0000, "PAL-M": 0x00000100,
             "PAL-N": 0x00000600, "NTSC-JP": 0x00002000}


def _ior(nr, size):
    return (2 << 30) | (size << 16) | (ord("V") << 8) | nr


class _Capability(ctypes.Structure):
    _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
                ("bus_info", ctypes.c_char * 32), ("version", ctypes.c_uint32),
                ("capabilities", ctypes.c_uint32), ("device_caps", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 3)]


class _FmtDesc(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("description", ctypes.c_char * 32), ("pixelformat", ctypes.c_uint32),
                ("mbus_code", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3)]


class _FrmSize(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("pixel_format", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("u", ctypes.c_uint32 * 6), ("reserved", ctypes.c_uint32 * 2)]


class _CaptureParm(ctypes.Structure):
    _fields_ = [("capability", ctypes.c_uint32), ("capturemode", ctypes.c_uint32),
                ("numerator", ctypes.c_uint32), ("denominator", ctypes.c_uint32),
                ("extendedmode", ctypes.c_uint32), ("readbuffers", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 4)]


class _StreamParm(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("capture", _CaptureParm), ("raw", ctypes.c_uint8 * 200)]
    _fields_ = [("type", ctypes.c_uint32), ("parm", _U)]


class _QueryCtrl(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("type", ctypes.c_uint32), ("name", ctypes.c_char * 32),
                ("minimum", ctypes.c_int32), ("maximum", ctypes.c_int32), ("step", ctypes.c_int32),
                ("default_value", ctypes.c_int32), ("flags", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 2)]


class _Control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


assert (ctypes.sizeof(_Capability), ctypes.sizeof(_FmtDesc), ctypes.sizeof(_FrmSize),
        ctypes.sizeof(_StreamParm), ctypes.sizeof(_QueryCtrl)) == (104, 64, 44, 204, 68)

VIDIOC_QUERYCAP = _ior(0, ctypes.sizeof(_Capability))
VIDIOC_ENUM_FMT = vm._iowr(2, ctypes.sizeof(_FmtDesc))
VIDIOC_G_FMT = vm._iowr(4, ctypes.sizeof(vm._Format))
VIDIOC_G_PARM = vm._iowr(21, ctypes.sizeof(_StreamParm))
VIDIOC_S_PARM = vm._iowr(22, ctypes.sizeof(_StreamParm))
VIDIOC_S_STD = vm._iow(24, 8)
VIDIOC_G_CTRL = vm._iowr(27, ctypes.sizeof(_Control))
VIDIOC_S_CTRL = vm._iowr(28, ctypes.sizeof(_Control))
VIDIOC_QUERYCTRL = vm._iowr(36, ctypes.sizeof(_QueryCtrl))
VIDIOC_S_INPUT = vm._iowr(39, 4)
VIDIOC_ENUM_FRAMESIZES = vm._iowr(74, ctypes.sizeof(_FrmSize))
CAPTURE = vm.V4L2_BUF_TYPE_VIDEO_CAPTURE


def fourcc_name(code):
    return struct.pack("<I", code).decode("latin-1").strip()


def query(fd):
    """(driver, card, bus_info, device_caps) of an open video device."""
    cap = _Capability()
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
    caps = cap.device_caps or cap.capabilities
    return (cap.driver.decode(errors="replace"), cap.card.decode(errors="replace"),
            cap.bus_info.decode(errors="replace"), caps)


def formats(fd):
    """The device's pixel formats (fourcc names), in its order."""
    out, i = [], 0
    while True:
        d = _FmtDesc(index=i, type=CAPTURE)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, d)
        except OSError:
            return out
        out.append(fourcc_name(d.pixelformat))
        i += 1


def frame_sizes(fd, name):
    """Discrete frame sizes of a pixel format ([] when it is stepwise / continuous)."""
    out, i = [], 0
    while True:
        s = _FrmSize(index=i, pixel_format=vm._fourcc(name))
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, s)
        except OSError:
            return out
        if s.type != V4L2_FRMSIZE_TYPE_DISCRETE:
            return out
        out.append((s.u[0], s.u[1]))
        i += 1


def find_cameras():
    """Video capture devices that are not a VEYE / libcamera pipeline node or a codec."""
    cams = []
    for dev in sorted(glob.glob("/dev/video*"), key=lambda p: int("0" + p[len("/dev/video"):])):
        try:
            fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            driver, card, bus, caps = query(fd)
            fmts = formats(fd)
        except OSError:
            continue
        finally:
            os.close(fd)
        if (not caps & V4L2_CAP_VIDEO_CAPTURE or not caps & V4L2_CAP_STREAMING
                or any(driver.startswith(n) for n in NOT_CAMERAS)
                or not any(f in SUPPORTED for f in fmts)):
            continue
        cams.append({"id": f"v4l2:{dev}", "vendor": driver, "model": card, "serial": bus,
                     "backend": "v4l2", "device": dev})
    return cams


# ---------------------------------------------------------------------------
# Stream and buffers
# ---------------------------------------------------------------------------
class Buffer(vm.Buffer):
    """A dequeued buffer; get_data() gives the region's luminance (uint8 or uint16)."""

    def __init__(self, stream, index, status, frame_id, timestamp_ns, bytesused):
        super().__init__(stream, index, status, frame_id, timestamp_ns)
        self.bytesused = bytesused

    def get_image_width(self):
        return self.stream.region[2]

    def get_image_height(self):
        return self.stream.region[3]

    def get_data(self):
        s = self.stream
        x, y, w, h = s.region
        m = s.maps[self.index]
        if s.fourcc == "MJPG":
            import cv2
            img = cv2.imdecode(np.frombuffer(m, np.uint8, count=self.bytesused), cv2.IMREAD_GRAYSCALE)
            if img is None or img.shape != (s.height, s.full_width):
                img = np.zeros((s.height, s.full_width), np.uint8)
        else:
            rows = np.frombuffer(m, np.uint8, count=s.height * s.bytesperline)
            rows = rows.reshape(s.height, s.bytesperline)
            if s.fourcc == "YUYV":
                img = rows[:, 0:2 * s.full_width:2]
            elif s.fourcc == "UYVY":
                img = rows[:, 1:2 * s.full_width:2]
            elif s.fourcc == "Y16":
                img = rows[:, :2 * s.full_width].view("<u2")
            else:
                img = rows[:, :s.full_width]
        return np.ascontiguousarray(img[y:y + h, x:x + w])


class Stream(vm.Stream):
    """mmap streaming of the full frame; buffers hand out the region (cut in software)."""

    def __init__(self, device, width, height, fourcc, n_buffers, region, fps=None):
        bits = 16 if fourcc in ("Y16", "YUYV", "UYVY") else 8
        super().__init__(device, width, height, fourcc, bits, n_buffers)
        self.fourcc = fourcc
        self.full_width = self.width
        self.region = region
        self.ts_soe = None              # driver stamps the start (True) or the end of the frame
        if fps:
            set_fps(self.fd, fps)       # S_FMT can reset the frame interval (uvcvideo)

    def timeout_pop_buffer(self, timeout_us):
        if not self.streaming:
            time.sleep(timeout_us / 1e6)
            return None
        ready, _, _ = select.select([self.fd], [], [], timeout_us / 1e6)
        if not ready:
            return None
        b = vm._Buffer(type=CAPTURE, memory=vm.V4L2_MEMORY_MMAP)
        with self.lock:
            if not self.queued:
                self.underruns += 1
                ready = False
            else:
                try:
                    fcntl.ioctl(self.fd, vm.VIDIOC_DQBUF, b)
                    self.queued.discard(b.index)
                except OSError:
                    ready = False
        if not ready:
            time.sleep(0.001)
            return None
        if b.flags & vm.V4L2_BUF_FLAG_ERROR:
            self.failed += 1
            status = "error"
        else:
            self.completed += 1
            status = SUCCESS
        if self.seq_base is None:
            self.seq_base = self.next_frame_id - b.sequence
        frame_id = self.seq_base + b.sequence
        self.next_frame_id = frame_id + 1
        self.ts_soe = (b.flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK) == V4L2_BUF_FLAG_TSTAMP_SRC_SOE
        if (b.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) == V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC:
            mono_ns = b.tv_sec * 1_000_000_000 + b.tv_usec * 1000
            utc_ns = mono_ns + time.clock_gettime_ns(time.CLOCK_REALTIME) - time.monotonic_ns()
        else:                           # unknown clock: when we got it
            utc_ns = time.time_ns()
        return Buffer(self, b.index, status, frame_id, utc_ns, b.bytesused)


def get_fps(fd):
    p = _StreamParm(type=CAPTURE)
    fcntl.ioctl(fd, VIDIOC_G_PARM, p)
    c = p.parm.capture
    return c.denominator / c.numerator if c.numerator and c.denominator else None


def set_fps(fd, fps):
    f = Fraction(1 / fps).limit_denominator(100_000)
    p = _StreamParm(type=CAPTURE)
    p.parm.capture.numerator, p.parm.capture.denominator = f.numerator, f.denominator
    fcntl.ioctl(fd, VIDIOC_S_PARM, p)


# ---------------------------------------------------------------------------
# The camera
# ---------------------------------------------------------------------------
class Camera:
    kind = "v4l2"
    success = SUCCESS
    can_trigger = False

    def __init__(self, info):
        self.info = info
        self.device = info["device"]
        self.fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)     # controls; the stream has its own
        self.driver, self.card, self.bus, _ = query(self.fd)
        self.std = None
        if info.get("std"):
            name = str(info["std"]).upper()
            std = STANDARDS.get(name) or int(str(info["std"]), 0)
            fcntl.ioctl(self.fd, VIDIOC_S_STD, ctypes.c_uint64(std))
            self.std = name
        if info.get("input") is not None:
            fcntl.ioctl(self.fd, VIDIOC_S_INPUT, ctypes.c_int(int(info["input"])))
        self.formats = [f for f in SUPPORTED if f in formats(self.fd)]
        fmt = vm._Format(type=CAPTURE)
        fcntl.ioctl(self.fd, VIDIOC_G_FMT, fmt)
        current = fourcc_name(fmt.fmt.pix.pixelformat)
        self.pixel_format = current if current in self.formats else self.formats[0]
        self.size = tuple(info.get("size") or (fmt.fmt.pix.width, fmt.fmt.pix.height))
        self._fit_size()
        self.region = (0, 0, *self.size)
        self.fps = None                 # set by set_frame_rate (None: the device's)
        self.stream = None
        self.ctrls = {cid: q for cid in (CID_GAIN, CID_EXPOSURE_AUTO, CID_EXPOSURE_ABSOLUTE)
                      if (q := self._query_ctrl(cid)) is not None}
        self.exposure_fixed = CID_EXPOSURE_ABSOLUTE not in self.ctrls
        self.gain_fixed = CID_GAIN not in self.ctrls

    def _fit_size(self):
        """Keep the frame size if the format offers it, else its largest one."""
        sizes = frame_sizes(self.fd, self.pixel_format)
        if sizes and tuple(self.size) not in sizes:
            self.size = max(sizes, key=lambda s: s[0] * s[1])

    def _query_ctrl(self, cid):
        q = _QueryCtrl(id=cid)
        try:
            fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, q)
        except OSError:
            return None
        return None if q.flags & V4L2_CTRL_FLAG_DISABLED else q

    def _get(self, cid):
        c = _Control(id=cid)
        fcntl.ioctl(self.fd, VIDIOC_G_CTRL, c)
        return c.value

    def _set(self, cid, value):
        q = self.ctrls[cid]
        c = _Control(id=cid, value=int(min(max(round(value), q.minimum), q.maximum)))
        fcntl.ioctl(self.fd, VIDIOC_S_CTRL, c)

    # ---- identity ----
    def get_model_name(self):
        return self.card

    def get_vendor_name(self):
        return self.driver

    def is_uv_device(self):
        return False

    def describe_input(self):
        """'PAL, input 1' or '' (for the recording's comment)."""
        parts = [self.std or "", f"input {self.info['input']}" if self.info.get("input") is not None else ""]
        return ", ".join(p for p in parts if p)

    # ---- format and region ----
    def get_pixel_format_as_string(self):
        return self.pixel_format

    def dup_available_pixel_formats_as_strings(self):
        return list(self.formats)

    def set_pixel_format_from_string(self, name):
        if name not in self.formats:
            raise ValueError(f"{self.device}: no pixel format {name} (has {', '.join(self.formats)})")
        self.pixel_format = name
        self._fit_size()

    def get_sensor_size(self):
        return self.size

    def is_binning_available(self):
        return False

    def set_region(self, x, y, w, h):
        fw, fh = self.size
        w, h = max(8, min(int(w), fw)), max(8, min(int(h), fh))
        x, y = min(max(0, int(x)), fw - w), min(max(0, int(y)), fh - h)
        self.region = (x, y, w, h)

    def get_region(self):
        return self.region

    # ---- exposure, gain, frame rate (the device's controls, where it has them) ----
    def is_exposure_auto_available(self):
        return CID_EXPOSURE_AUTO in self.ctrls

    def set_exposure_time_auto(self, mode):
        if CID_EXPOSURE_AUTO in self.ctrls:
            self._set(CID_EXPOSURE_AUTO, 1 if int(mode) == 0 else 3)

    def is_gain_auto_available(self):
        return False

    def set_gain_auto(self, mode):
        pass

    def set_manual(self):
        self.set_exposure_time_auto(0)

    def frame_period_ns(self):
        fps = self.fps or get_fps(self.fd)
        return 1e9 / fps if fps else None

    def set_frame_rate(self, fps):
        set_fps(self.fd, fps)
        self.fps = get_fps(self.fd) or fps

    def set_frame_rate_enable(self, enable):
        pass

    def set_boolean(self, feature, value):
        if feature != "AcquisitionFrameRateEnable":
            raise ValueError(f"unknown feature {feature}")

    def get_exposure_time_bounds(self):
        if self.exposure_fixed:         # e.g. an analog grabber: one frame (field pair) period
            us = (self.frame_period_ns() or 40e6) / 1000
            return us, us * 1.001       # a range, so a slider still works (disabled in the GUI)
        q = self.ctrls[CID_EXPOSURE_ABSOLUTE]
        return max(q.minimum, 1) * 100.0, q.maximum * 100.0

    def get_exposure_time(self):
        if self.exposure_fixed:
            return (self.frame_period_ns() or 40e6) / 1000
        return self._get(CID_EXPOSURE_ABSOLUTE) * 100.0

    def set_exposure_time(self, us):
        if not self.exposure_fixed:
            self._set(CID_EXPOSURE_ABSOLUTE, us / 100.0)

    def get_gain_bounds(self):
        if self.gain_fixed:
            return 0.0, 0.001
        q = self.ctrls[CID_GAIN]
        return float(q.minimum), float(q.maximum)    # device units, not dB

    def get_gain(self):
        return 0.0 if self.gain_fixed else float(self._get(CID_GAIN))

    def set_gain(self, value):
        if not self.gain_fixed:
            self._set(CID_GAIN, value)

    # ---- no trigger input ----
    def set_trigger(self, source):
        raise RuntimeError(f"{self.card}: a video device has no trigger input")

    def clear_triggers(self):
        pass

    def software_trigger(self):
        raise RuntimeError(f"{self.card}: no software trigger")

    def get_trigger_count(self):
        return 0, 0

    def set_string(self, feature, value):
        raise ValueError(f"unknown feature {feature}")

    def set_float(self, feature, value):
        raise ValueError(f"unknown feature {feature}")

    # ---- streaming ----
    def create_stream(self, n_buffers):
        w, h = self.size
        try:
            self.stream = Stream(self.device, w, h, self.pixel_format, n_buffers, self.region, self.fps)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise RuntimeError(f"{self.device} is busy: another program (e.g. VLC, a browser) "
                                   "uses it; close that first") from None
            raise
        self.size = (self.stream.full_width, self.stream.height)    # what the driver gave
        if self.region[2] > self.size[0] or self.region[3] > self.size[1]:
            self.set_region(*self.region)
            self.stream.region = self.region
        return self.stream

    def close_stream(self, stream):
        stream.close()

    def get_payload(self):
        return self.stream.payload

    def start_acquisition(self):
        self.stream.start()

    def stop_acquisition(self):
        self.stream.stop()
