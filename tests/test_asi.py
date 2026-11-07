"""ZWO ASI backend (capture.asi_cam) against a fake libASICamera2: no camera, no ZWO library."""

import ctypes
import time

import numpy as np

from pystarlight.capture import asi_cam


class FakeASI:
    """The SDK calls the backend makes, with the pointer arguments it passes."""

    def __init__(self, width=640, height=480):
        self.w, self.h = width, height
        self.controls = {asi_cam.GAIN: 0, asi_cam.EXPOSURE: 10000, asi_cam.TEMPERATURE: 215}
        self.roi = None
        self.capturing = False
        self.frames = 0
        self.dropped = 0
        self.limit = 10**9

    def ASIGetSDKVersion(self):
        return b"1, 27, 0, 0"

    def ASIGetNumOfConnectedCameras(self):
        return 1

    def ASIGetCameraProperty(self, p, index):
        i = p.contents
        i.Name, i.CameraID, i.MaxWidth, i.MaxHeight = b"ZWO ASI178MM", 0, self.w, self.h
        i.PixelSize, i.BitDepth, i.IsUSB3Camera, i.IsUSB3Host = 2.4, 14, 1, 1
        for k, t in enumerate((asi_cam.IMG_RAW8, asi_cam.IMG_RAW16, asi_cam.IMG_Y8, asi_cam.IMG_END)):
            i.SupportedVideoFormat[k] = t
        return 0

    def ASIOpenCamera(self, cid):
        return 0

    ASIInitCamera = ASICloseCamera = ASIOpenCamera

    def ASIGetNumOfControls(self, cid, n):
        n.contents.value = 3
        return 0

    def ASIGetControlCaps(self, cid, index, p):
        c = p.contents
        c.ControlType = (asi_cam.GAIN, asi_cam.EXPOSURE, asi_cam.TEMPERATURE)[index]
        c.MinValue, c.MaxValue = {0: (0, 510), 1: (32, 2_000_000_000), 2: (-500, 1000)}[index]
        return 0

    def ASIGetControlValue(self, cid, ctrl, v, auto):
        v.contents.value = self.controls[ctrl]
        return 0

    def ASISetControlValue(self, cid, ctrl, v, auto):
        self.controls[ctrl] = v.value
        return 0

    def ASISetROIFormat(self, cid, w, h, binning, img_type):
        if w % 8 or h % 2:
            return 8                    # INVALID_SIZE
        self.roi = (w, h, img_type)
        return 0

    def ASISetStartPos(self, cid, x, y):
        self.start = (x, y)
        return 0

    def ASIStartVideoCapture(self, cid):
        self.capturing = True
        return 0

    def ASIStopVideoCapture(self, cid):
        self.capturing = False
        return 0

    def ASIGetDroppedFrames(self, cid, n):
        n.contents.value = self.dropped
        return 0

    def ASIGetVideoData(self, cid, buf, size, wait_ms):
        if not self.capturing:
            return 12                   # INVALID_SEQUENCE
        if self.frames >= self.limit:   # no more frames: the SDK's timeout
            time.sleep(0.001)
            return asi_cam.ERROR_TIMEOUT
        w, h, t = self.roi
        bpp = 2 if t == asi_cam.IMG_RAW16 else 1
        assert size == w * h * bpp
        self.frames += 1
        if self.frames == 3:
            self.dropped += 2           # the SDK lost two frames before this one
        data = (np.full(w * h, self.frames * 4, np.uint16) if bpp == 2
                else np.full(w * h, self.frames, np.uint8)).tobytes()
        ctypes.memmove(buf, data, size)
        return 0


def test_camera_controls_and_formats():
    lib = FakeASI()
    cam = asi_cam.Camera({"index": 0}, lib=lib)
    assert (cam.kind, cam.can_trigger, cam.get_vendor_name(), cam.get_model_name()) == \
        ("asi", False, "ZWO", "ASI178MM")
    assert cam.dup_available_pixel_formats_as_strings() == ["Mono8", "Mono16"]
    assert cam.get_pixel_format_as_string() == "Mono16"
    assert cam.pixel_um == 2.4 and cam.get_sensor_size() == (640, 480)
    assert cam.get_gain_bounds() == (0.0, 51.0)           # 0.1 dB units
    cam.set_gain(12.3)
    assert lib.controls[asi_cam.GAIN] == 123 and cam.get_gain() == 12.3
    cam.set_exposure_time(5e9)                            # clamped to the camera's maximum
    assert lib.controls[asi_cam.EXPOSURE] == 2_000_000_000
    assert cam.describe() == "ZWO ASI SDK 1.27, USB3, 14-bit ADC, 2.4 um pixels, sensor 21.5 C"
    cam.set_region(13, 7, 101, 71)                       # width to 8s, height to 2s
    assert cam.get_region() == (13, 7, 96, 70)
    assert asi_cam.format_name(asi_cam.IMG_RAW16, True, 1) == "BayerBG16"


def _frames(st, n, timeout_s=2.0):
    out, t0 = [], time.monotonic()
    while len(out) < n and time.monotonic() - t0 < timeout_s:
        b = st.timeout_pop_buffer(50_000)
        if b is not None:
            out.append(b)
    return out


def test_stream_frames_ids_and_buffers(monkeypatch):
    from pystarlight.capture.decode import Decoder
    lib = FakeASI()
    lib.limit = 5
    cam = asi_cam.Camera({"index": 0}, lib=lib)
    cam.set_region(0, 0, 64, 64)
    st = cam.create_stream(2)
    assert lib.roi == (64, 64, asi_cam.IMG_RAW16)
    assert len(st.buffers) == 256                         # a ring of ~RING_BYTES, at most 256
    assert st.timeout_pop_buffer(1000) is None            # not started: nothing
    cam.start_acquisition()
    got = _frames(st, 5)
    assert [b.get_frame_id() for b in got] == [0, 1, 4, 5, 6]   # 2 dropped by the SDK: a gap
    img = Decoder("Mono16").raw(got[0].get_data(), got[0].get_image_width(), got[0].get_image_height())
    assert img.shape == (64, 64) and img.dtype == np.uint16 and img[0, 0] == 4
    assert got[-1].get_status() == asi_cam.SUCCESS and got[-1].get_system_timestamp() > 0
    assert st.timeout_pop_buffer(20_000) is None          # no more frames
    for b in got:
        st.push_buffer(b)
    cam.stop_acquisition()
    assert st.reader is None and not lib.capturing and len(st.idle) == 256
    cam.close_stream(st)
    cam.set_pixel_format_from_string("Mono8")
    st = cam.create_stream(2)
    assert st.payload == 64 * 64 and lib.roi[2] == asi_cam.IMG_RAW8


def test_stream_ring_full_counts_underruns(monkeypatch):
    """The grabber holding every buffer: the reader waits (and counts), frames resume after."""
    monkeypatch.setattr(asi_cam.Stream, "RING_BYTES", 1)  # just n_buffers
    lib = FakeASI()
    cam = asi_cam.Camera({"index": 0}, lib=lib)
    cam.set_region(0, 0, 64, 64)
    st = cam.create_stream(3)
    cam.start_acquisition()
    held = _frames(st, 3)
    time.sleep(0.02)
    assert len(held) == 3 and st.underruns > 0 and st.timeout_pop_buffer(10_000) is None
    st.push_buffer(held[0])
    assert len(_frames(st, 1)) == 1
    cam.stop_acquisition()


def test_session_with_an_asi_camera():
    from pystarlight.capture.cameras import CameraSession
    s = CameraSession(asi_cam.Camera({"index": 0}, lib=FakeASI()))
    assert s.is_asi and not s.is_v4l2 and not s.can_trigger and s.pixel_um == 2.4
    s.configure(bits=12, exposure_ms=20, gain_db=10)      # 12 bit -> Mono16; no frame-rate setting
    assert s.pixel_format == "Mono16" and s.decode.dtype == np.uint16
    assert not s.exposure_fixed()
