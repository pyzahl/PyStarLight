"""VEYE MV-MIPI cameras (veye_mvcam driver) on Raspberry Pi 5, behind an Aravis-like API.

Frames come from the rp1-cfe V4L2 capture node (mmap streaming). Exposure, gain and frame
rate go straight to the camera's own I2C registers: the driver's V4L2 exposure/gain
controls are placeholders for libcamera and never reach the sensor.

Only the subset of Aravis.Camera / Aravis.Stream / Aravis.Buffer used by the capture app is
implemented, plus the backend extras described in cameras.py.
"""
import ctypes
import fcntl
import glob
import math
import mmap
import os
import re
import select
import struct
import subprocess
import threading
import time

import numpy as np

SUCCESS = "success"     # buffer status of a complete frame (compare with Grabber's success)

# ---------------------------------------------------------------------------
# Camera registers (see mv_tools_rpi/mv_mipi_i2c_new.sh); all values are 32-bit
# ---------------------------------------------------------------------------
REG_MODEL = 0x0004
REG_TRIGGER_MODE = 0x0404   # 0 free-running, 1 trigger, 2 high-speed trigger
REG_TRIGGER_SOURCE = 0x0408 # 0 software, 1 hardware (trigger input pin)
REG_TRIGGER_SOFTWARE = 0x0414   # write 1: one software trigger
REG_TRIGGER_COUNT = 0x0418  # low 16 bits: triggers received, high 16: lost; write 1 clears
REG_TRIGGER_DELAY = 0x1000
REG_TRIGGER_EDGE = 0x1004   # 0 rising, 1 falling
REG_SERIAL = 0x000C
REG_PIXEL_FORMAT = 0x0804   # 0 raw8, 1 raw10, 2 raw12
REG_SENSOR_W = 0x0808
REG_SENSOR_H = 0x080C
REG_MAX_FPS = 0x0810        # 1/100 fps, for the current ROI and format
REG_FPS = 0x0814            # 1/100 fps
REG_EXP_MODE = 0x0C04       # 0 manual, 1 auto once, 2 auto continuous (= Aravis.Auto)
REG_ME_TIME = 0x0C10        # manual exposure time, us
REG_GAIN_MODE = 0x0C1C      # 0 manual, 1 auto once, 2 auto continuous
REG_MANUAL_GAIN = 0x0C20    # 1/10 dB
REG_MAX_EXP = 0x0C8C        # us        (0xFFFFFFFF on older firmware)
REG_MIN_EXP = 0x0C90        # 1/10 us   (0xFFFFFFFF on older firmware)
UNSUPPORTED = 0xFFFFFFFF

GAIN_MAX_DB = 48.0          # the camera clamps manual gain here (read back on IMX178M)
MAX_EXPOSURE_GUESS_S = 1.9    # longest exposure seen on these cameras (IMX178M: 1.88 s)
FPS_MIN = 0.01              # register resolution (the camera clamps further, 0.1 fps on IMX178M)

MODELS = {
    662: "MV-MIPI-IMX296M", 376: "MV-MIPI-IMX178M", 304: "MV-MIPI-SC130M",
    613: "MV-MIPI-IMX265M", 612: "MV-MIPI-IMX264M", 647: "MV-MIPI-IMX287M",
    33074: "RAW-MIPI-SC132M", 33332: "RAW-MIPI-AR0234M", 33890: "RAW-MIPI-IMX462M",
    34101: "RAW-MIPI-SC535M", 16386: "MV-MIPI-GMAX4002M",
}

# pixel format name -> (Pixel_Format register value, sensor media bus code,
#                       bits per pixel in the buffer, V4L2 fourcc, CSI-2 receiver output code)
FORMATS = {
    "GREY": (0, "Y8_1X8", 8, "GREY", "Y8_1X8"),
    "Y10P": (1, "Y10_1X10", 10, "Y10P", "Y10_1X10"),    # MIPI packed: 4 pixels in 5 bytes
    "Y12P": (2, "Y12_1X12", 12, "Y12P", "Y12_1X12"),    # MIPI packed: 2 pixels in 3 bytes
    # unpacked by the rp1-cfe receiver into 16 bits, left-aligned (value << 6 / << 4):
    # no CPU unpacking, just a shift
    "Y10": (1, "Y10_1X10", 16, "Y16", "Y16_1X16"),
    "Y12": (2, "Y12_1X12", 16, "Y16", "Y16_1X16"),
}


# ---------------------------------------------------------------------------
# I2C register access (I2C_RDWR works while the kernel driver owns the address)
# ---------------------------------------------------------------------------
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001


class _I2cMsg(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16),
                ("len", ctypes.c_uint16), ("buf", ctypes.c_void_p)]


class _I2cRdwr(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(_I2cMsg)), ("nmsgs", ctypes.c_uint32)]


class Registers:
    def __init__(self, bus, addr):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        self.addr = addr
        self.lock = threading.Lock()

    def _transfer(self, *msgs):
        bufs = [ctypes.create_string_buffer(data, len(data)) for _, data in msgs]
        arr = (_I2cMsg * len(msgs))(*[
            _I2cMsg(self.addr, flags, len(data), ctypes.cast(b, ctypes.c_void_p))
            for (flags, data), b in zip(msgs, bufs)])
        with self.lock:
            fcntl.ioctl(self.fd, I2C_RDWR, _I2cRdwr(arr, len(msgs)))
        return bufs

    def read(self, reg):
        bufs = self._transfer((0, struct.pack(">H", reg)), (I2C_M_RD, bytes(4)))
        return struct.unpack(">I", bufs[1].raw)[0]

    def write(self, reg, value):
        self._transfer((0, struct.pack(">HI", reg, int(value) & 0xFFFFFFFF)))
        time.sleep(0.001)   # the vendor tools pace consecutive writes too


# ---------------------------------------------------------------------------
# Minimal V4L2 mmap capture (ctypes layouts for 64-bit Linux)
# ---------------------------------------------------------------------------
def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


def _iow(nr, size):
    return (1 << 30) | (size << 16) | (ord("V") << 8) | nr


V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 1
V4L2_BUF_FLAG_ERROR = 0x40


class _PixFormat(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "width", "height", "pixelformat", "field", "bytesperline", "sizeimage",
        "colorspace", "priv", "flags", "ycbcr_enc", "quantization", "xfer_func")]


class _FormatUnion(ctypes.Union):
    _fields_ = [("pix", _PixFormat), ("raw", ctypes.c_uint8 * 200), ("_align", ctypes.c_uint64)]


class _Format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("fmt", _FormatUnion)]


class _RequestBuffers(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("memory", ctypes.c_uint32), ("capabilities", ctypes.c_uint32),
                ("flags", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 3)]


class _Timecode(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("frames", ctypes.c_uint8), ("seconds", ctypes.c_uint8),
                ("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8),
                ("userbits", ctypes.c_uint8 * 4)]


class _BufferM(ctypes.Union):
    _fields_ = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_ulong), ("fd", ctypes.c_int32)]


class _Buffer(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("bytesused", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("field", ctypes.c_uint32),
                ("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long),
                ("timecode", _Timecode), ("sequence", ctypes.c_uint32),
                ("memory", ctypes.c_uint32), ("m", _BufferM),
                ("length", ctypes.c_uint32), ("reserved2", ctypes.c_uint32),
                ("request_fd", ctypes.c_int32)]


assert ctypes.sizeof(_Format) == 208 and ctypes.sizeof(_Buffer) == 88, "expects 64-bit Linux"

VIDIOC_S_FMT = _iowr(5, ctypes.sizeof(_Format))
VIDIOC_REQBUFS = _iowr(8, ctypes.sizeof(_RequestBuffers))
VIDIOC_QUERYBUF = _iowr(9, ctypes.sizeof(_Buffer))
VIDIOC_QBUF = _iowr(15, ctypes.sizeof(_Buffer))
VIDIOC_DQBUF = _iowr(17, ctypes.sizeof(_Buffer))
VIDIOC_STREAMON = _iow(18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _iow(19, ctypes.sizeof(ctypes.c_int))


def _fourcc(name):
    return struct.unpack("<I", name.encode().ljust(4)[:4])[0]


class Buffer:
    """A dequeued V4L2 buffer; hand it back with Stream.push_buffer()."""

    def __init__(self, stream, index, status, frame_id, timestamp_ns):
        self.stream, self.index = stream, index
        self.status, self.frame_id = status, frame_id
        self.timestamp_ns = timestamp_ns

    def get_system_timestamp(self):
        """UTC (ns) of the frame start on the CSI-2 link, i.e. about the end of exposure."""
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
        """Copy of the image with the row padding removed (h * row_bytes contiguous bytes)."""
        s = self.stream
        rows = np.frombuffer(s.maps[self.index], np.uint8,
                             count=s.height * s.bytesperline).reshape(s.height, s.bytesperline)
        return rows[:, s.row_offset:s.row_offset + s.row_bytes].copy()


class Stream:
    def __init__(self, device, width, height, pixel_format, bits, n_buffers, crop_x=0, crop_w=None):
        """Captures width x height; buffers hand out columns crop_x .. crop_x + crop_w."""
        self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        self.lock = threading.Lock()

        fmt = _Format(type=V4L2_BUF_TYPE_VIDEO_CAPTURE)
        fmt.fmt.pix.width, fmt.fmt.pix.height = width, height
        fmt.fmt.pix.pixelformat = _fourcc(pixel_format)
        fmt.fmt.pix.field = V4L2_FIELD_NONE
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        pix = fmt.fmt.pix
        if pix.pixelformat != _fourcc(pixel_format):
            raise RuntimeError(f"{device} refused pixel format {pixel_format}")
        self.width = pix.width if crop_w is None else crop_w
        self.height = pix.height
        self.bytesperline, self.payload = pix.bytesperline, pix.sizeimage
        # x on multiples of 8 keeps the offset on whole packed pixel groups
        self.row_offset = crop_x * bits // 8
        self.row_bytes = self.width * bits // 8

        req = _RequestBuffers(count=n_buffers, type=V4L2_BUF_TYPE_VIDEO_CAPTURE,
                              memory=V4L2_MEMORY_MMAP)
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
        self.maps = []
        for i in range(req.count):
            b = _Buffer(index=i, type=V4L2_BUF_TYPE_VIDEO_CAPTURE, memory=V4L2_MEMORY_MMAP)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, b)
            self.maps.append(mmap.mmap(self.fd, b.length, mmap.MAP_SHARED,
                                       mmap.PROT_READ, offset=b.m.offset))
        self.idle = set(range(req.count))   # with us, not queued and not handed out
        self.queued = set()
        self.streaming = False
        self.closed = False
        self.completed = self.failed = self.underruns = 0
        # frame numbers keep counting across restarts (V4L2 restarts at 0 on STREAMON)
        self.next_frame_id = 0
        self.stop_time = None           # time.monotonic() of the last STREAMOFF
        self.starts = 0                 # STREAMON count (timing restarts with each)
        self.seq_base = None

    def _qbuf(self, index):
        b = _Buffer(index=index, type=V4L2_BUF_TYPE_VIDEO_CAPTURE, memory=V4L2_MEMORY_MMAP)
        fcntl.ioctl(self.fd, VIDIOC_QBUF, b)
        self.queued.add(index)

    def start(self):
        with self.lock:
            for i in sorted(self.idle):
                self._qbuf(i)
            self.idle.clear()
            fcntl.ioctl(self.fd, VIDIOC_STREAMON, ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))
            self.streaming = True
            self.seq_base = None
            self.starts += 1

    def stop(self):
        with self.lock:
            if not self.streaming:
                return
            self.streaming = False
            self.stop_time = time.monotonic()
            fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(V4L2_BUF_TYPE_VIDEO_CAPTURE))
            self.idle |= self.queued    # STREAMOFF hands every queued buffer back
            self.queued.clear()

    def push_buffer(self, buf):
        with self.lock:
            if self.closed:                 # a buffer of a stream already replaced
                return
            if self.streaming:
                self._qbuf(buf.index)
            else:
                self.idle.add(buf.index)

    def timeout_pop_buffer(self, timeout_us):
        if not self.streaming:
            time.sleep(timeout_us / 1e6)
            return None
        ready, _, _ = select.select([self.fd], [], [], timeout_us / 1e6)
        if not ready:
            return None
        b = _Buffer(type=V4L2_BUF_TYPE_VIDEO_CAPTURE, memory=V4L2_MEMORY_MMAP)
        with self.lock:
            if not self.queued:             # poll() reports an error with an empty queue
                self.underruns += 1
                ready = False
            else:
                try:
                    fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
                    self.queued.discard(b.index)
                except OSError:             # EAGAIN, or STREAMOFF raced us
                    ready = False
        if not ready:
            time.sleep(0.001)
            return None
        if b.flags & V4L2_BUF_FLAG_ERROR:
            self.failed += 1
            status = "error"
        else:
            self.completed += 1
            status = SUCCESS
        if self.seq_base is None:
            self.seq_base = self.next_frame_id - b.sequence
        frame_id = self.seq_base + b.sequence
        self.next_frame_id = frame_id + 1
        # rp1-cfe stamps the frame start (CLOCK_MONOTONIC); convert to UTC
        mono_ns = b.tv_sec * 1_000_000_000 + b.tv_usec * 1000
        utc_ns = mono_ns + time.clock_gettime_ns(time.CLOCK_REALTIME) - time.monotonic_ns()
        return Buffer(self, b.index, status, frame_id, utc_ns)

    def get_statistics(self):
        return self.completed, self.failed, self.underruns

    def close(self):
        self.stop()
        with self.lock:
            self.closed = True
        for m in self.maps:
            m.close()
        os.close(self.fd)


# ---------------------------------------------------------------------------
# Discovery and the camera itself
# ---------------------------------------------------------------------------
def _run(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def find_cameras():
    """VEYE cameras bound to the veye_mvcam driver, in cameras.find_cameras' list format."""
    cams = []
    for name_file in sorted(glob.glob("/sys/class/video4linux/v4l-subdev*/name")):
        with open(name_file) as f:
            entity = f.read().strip()
        m = re.fullmatch(r"mvcam (\d+)-([0-9a-f]{4})", entity)
        if not m:
            continue
        bus, addr = int(m.group(1)), int(m.group(2), 16)
        try:
            regs = Registers(bus, addr)
            model_id, serial = regs.read(REG_MODEL), regs.read(REG_SERIAL)
            os.close(regs.fd)
        except OSError as e:
            print(f"{entity}: cannot read camera registers ({e})")
            continue
        cams.append({
            "id": f"veye:{entity}",
            "vendor": "VEYE",
            "model": MODELS.get(model_id, f"model {model_id}"),
            "serial": f"{serial:08x}",
            "backend": "veye",
            "entity": entity,
            "bus": bus,
            "addr": addr,
        })
    return cams


class Camera:
    kind = "veye"
    success = SUCCESS

    def __init__(self, info):
        self.info = info
        self.entity = info["entity"]
        self.regs = Registers(info["bus"], info["addr"])
        self.media_dev = next(
            (d for d in sorted(glob.glob("/dev/media*"))
             if self.entity in _run("media-ctl", "-d", d, "-p")), None)
        if self.media_dev is None:
            raise RuntimeError(f"no media device contains '{self.entity}'")
        self.subdev = _run("media-ctl", "-d", self.media_dev, "-e", self.entity)
        self.video_dev = _run("media-ctl", "-d", self.media_dev, "-e", "rp1-cfe-csi2_ch0")

        self.sensor_w = self.regs.read(REG_SENSOR_W)
        self.sensor_h = self.regs.read(REG_SENSOR_H)
        reg_fmt = self.regs.read(REG_PIXEL_FORMAT)
        self.pixel_format = next((n for n, f in FORMATS.items() if f[0] == reg_fmt), "GREY")
        self.region = (0, 0, self.sensor_w, self.sensor_h)
        self.fixed_fps = None       # None: frame rate follows the exposure time
        self.exposure_us = None     # last requested exposure (the read-back may be clipped)
        self.fps_reg = None         # last frame-rate register value written (1/100 fps)
        self.max_exposure_us = None
        self.stream = None

    # ---- identity ----
    def get_model_name(self):
        return self.info["model"]

    def get_vendor_name(self):
        return self.info["vendor"]

    def is_uv_device(self):
        return False

    # ---- format and region (applied to the media pipeline in create_stream) ----
    def get_pixel_format_as_string(self):
        return self.pixel_format

    def dup_available_pixel_formats_as_strings(self):
        codes = _run("v4l2-ctl", "-d", self.subdev, "--list-subdev-mbus-codes", "pad=0")
        return [n for n, (_, mbus, *_) in FORMATS.items() if f"MEDIA_BUS_FMT_{mbus}" in codes]

    def set_pixel_format_from_string(self, name):
        if name not in FORMATS:
            raise ValueError(f"unsupported pixel format {name}")
        self.pixel_format = name

    def get_sensor_size(self):
        return self.sensor_w, self.sensor_h

    def is_binning_available(self):
        return False

    def set_region(self, x, y, w, h):
        # the driver needs x and width on multiples of 8, y and height on multiples of 4
        # shift a region that runs off the sensor back onto it (rows are cropped in software)
        w, h = min(w, self.sensor_w) // 8 * 8, min(h, self.sensor_h) // 4 * 4
        x, y = min(x, self.sensor_w - w) // 8 * 8, min(y, self.sensor_h - h) // 4 * 4
        self.region = (x, y, w, h)

    def get_region(self):
        return self.region

    def _configure_pipeline(self):
        """Sets up the sensor for full-width rows y .. y + h; create_stream crops x.

        The camera firmware applies only the low 8 bits of the ROI x offset (measured on
        an IMX178M: x=1600 reads back as 1600 but the image starts at column 64), so the
        horizontal crop is done in software instead. The frame rate depends only on the row
        count (full-width and 640-wide 480-row regions both allow 163 fps), so nothing is lost.
        """
        _, mbus, _, _, csi2_out = FORMATS[self.pixel_format]
        _, y, _, h = self.region
        x, w = 0, self.sensor_w
        md = self.media_dev
        _run("media-ctl", "-d", md, "-r")
        _run("media-ctl", "-d", md, "-l", "'csi2':4 -> 'rp1-cfe-csi2_ch0':0 [1]")
        _run("v4l2-ctl", "-d", self.subdev, "--set-ctrl", f"roi_x={x},roi_y={y}")
        for pad, code in ((f"'{self.entity}':0", mbus), ("'csi2':0", mbus), ("'csi2':4", csi2_out)):
            _run("media-ctl", "-d", md, "-V", f"{pad} [fmt:{code}/{w}x{h} field:none]")

    # ---- exposure, gain, frame rate ----
    def is_exposure_auto_available(self):
        return True

    def set_exposure_time_auto(self, mode):
        self.regs.write(REG_EXP_MODE, int(mode))

    def is_gain_auto_available(self):
        return True

    def set_gain_auto(self, mode):
        self.regs.write(REG_GAIN_MODE, int(mode))

    def set_manual(self):
        self.set_exposure_time_auto(0)
        self.set_gain_auto(0)

    def _max_fps(self):
        return self.regs.read(REG_MAX_FPS) / 100.0

    def _write_fps(self, fps):
        # round down: the frame period must not end up shorter than the exposure
        fps = max(FPS_MIN, min(fps, self._max_fps()))
        self._write_fps_reg(max(1, math.floor(fps * 100 + 1e-6)))

    def _write_fps_reg(self, value):
        """The camera ignores frame-rate writes while acquiring, so stop the stream around
        the write.

        Raising the frame rate within about one old frame period of the last frame hangs
        the camera firmware (no frames until a camera reboot; measured: after 200 ms frames
        0.1 s is too soon, 0.3 s is fine), so let the sensor go idle for that long first.
        """
        cur = self.regs.read(REG_FPS)
        if value == cur:
            return
        streaming = self.stream is not None and self.stream.streaming
        if streaming:
            self.stream.stop()
        if value > cur:
            # the hazard scales with the old frame period, but exposures stop at ~1.9 s
            idle = 1.2 * min(100.0 / cur, MAX_EXPOSURE_GUESS_S) + 0.1
            # counted from when acquisition stopped (the sensor exposes until then); a
            # stream that never ran this session has nothing to wait for
            stopped = None if self.stream is None else self.stream.stop_time
            idle = 0 if stopped is None else idle - (time.monotonic() - stopped)
            if idle > 0:
                time.sleep(idle)
        self.regs.write(REG_FPS, value)
        self.fps_reg = value
        if streaming:
            self.stream.start()

    def frame_period_ns(self):
        """Expected time between frames when free-running (from the last frame rate set)."""
        if self.fps_reg is None:
            self.fps_reg = self.regs.read(REG_FPS)
        return 1e11 / self.fps_reg

    def set_frame_rate(self, fps):
        self.fixed_fps = fps
        self._write_fps(fps)

    def set_frame_rate_enable(self, enable):
        if not enable:
            self.fixed_fps = None
            self.set_exposure_time(self.exposure_us or self.get_exposure_time())

    def set_boolean(self, feature, value):
        if feature != "AcquisitionFrameRateEnable":
            raise ValueError(f"unknown feature {feature}")
        self.set_frame_rate_enable(value)

    def get_exposure_time_bounds(self):
        lo, hi = self.regs.read(REG_MIN_EXP), self.regs.read(REG_MAX_EXP)
        lo = 10.0 if lo == UNSUPPORTED else lo / 10.0
        hi = self._probe_max_exposure() if hi == UNSUPPORTED else float(hi)
        return lo, hi

    def _probe_max_exposure(self):
        """Older firmware doesn't report the limit: ask for 100 s at the slowest frame
        rate and read back what the camera allows (about 1.88 s on an IMX178M)."""
        if self.max_exposure_us is None:
            # direct writes: no frame is taken at these settings, so the frame-rate
            # hazard in _write_fps_reg doesn't apply (and its wait would be ~12 s here)
            fps, us = self.regs.read(REG_FPS), self.regs.read(REG_ME_TIME)
            self.regs.write(REG_FPS, 1)
            self.regs.write(REG_ME_TIME, 100_000_000)
            self.max_exposure_us = float(self.regs.read(REG_ME_TIME))
            self.regs.write(REG_ME_TIME, us)
            self.regs.write(REG_FPS, fps)
        return self.max_exposure_us

    def get_exposure_time(self):
        return float(self.regs.read(REG_ME_TIME))

    def set_exposure_time(self, us):
        us = max(1, int(round(us)))
        self.exposure_us = us
        if self.fixed_fps is not None:
            self.regs.write(REG_ME_TIME, us)
            return
        # Free-running: frame period follows the exposure. The camera clips the exposure to
        # the frame period in force when it is written, so lower the frame rate before the
        # exposure, and raise it only after.
        fps = 1e6 / us
        if fps < self.regs.read(REG_FPS) / 100.0:
            self._write_fps(fps)
            self.regs.write(REG_ME_TIME, us)
        else:
            self.regs.write(REG_ME_TIME, us)
            self._write_fps(fps)
        # The usable exposure is the frame period minus some readout overhead; stretch the
        # period by the shortfall so the requested time fits.
        short = us - self.get_exposure_time()
        if short > 0 and fps < self._max_fps():
            self._write_fps(1e6 / (us + short))
            self.regs.write(REG_ME_TIME, us)

    # ---- trigger (Aravis-style names; Line0 is the camera's trigger input pin) ----
    def set_trigger(self, source):
        """'Line0' (hardware input) or 'Software'.

        The frame-rate register still caps the exposure in trigger mode (and so limits
        the trigger rate to about 1/exposure), so it keeps following the exposure.
        """
        self.regs.write(REG_TRIGGER_SOURCE, {"Software": 0, "Line0": 1}[source])
        self.regs.write(REG_TRIGGER_MODE, 1)
        self.regs.write(REG_TRIGGER_COUNT, 1)

    def clear_triggers(self):
        self.regs.write(REG_TRIGGER_MODE, 0)

    def software_trigger(self):
        self.regs.write(REG_TRIGGER_SOFTWARE, 1)

    def get_trigger_count(self):
        """(triggers received, triggers lost) since set_trigger(); 16-bit counters."""
        v = self.regs.read(REG_TRIGGER_COUNT)
        return v & 0xFFFF, v >> 16

    def set_string(self, feature, value):
        if feature != "TriggerActivation":
            raise ValueError(f"unknown feature {feature}")
        self.regs.write(REG_TRIGGER_EDGE, {"RisingEdge": 0, "FallingEdge": 1}[value])

    def set_float(self, feature, value):
        if feature != "TriggerDelay":
            raise ValueError(f"unknown feature {feature}")
        self.regs.write(REG_TRIGGER_DELAY, round(value))

    def get_gain_bounds(self):
        return 0.0, GAIN_MAX_DB

    def get_gain(self):
        return self.regs.read(REG_MANUAL_GAIN) / 10.0

    def set_gain(self, db):
        self.regs.write(REG_MANUAL_GAIN, round(db * 10))

    # ---- streaming ----
    def create_stream(self, n_buffers):
        self._configure_pipeline()
        _, _, bits, fourcc, _ = FORMATS[self.pixel_format]
        x, y, w, h = self.region
        self.stream = Stream(self.video_dev, self.sensor_w, h, fourcc, bits, n_buffers, x, w)
        # max frame rate depends on the ROI and format just applied
        if self.fixed_fps is not None:
            self._write_fps(self.fixed_fps)
        else:
            self.set_exposure_time(self.exposure_us or self.get_exposure_time())
        return self.stream

    def close_stream(self, stream):
        stream.close()

    def get_payload(self):
        return self.stream.payload

    def start_acquisition(self):
        self.stream.start()

    def stop_acquisition(self):
        self.stream.stop()
