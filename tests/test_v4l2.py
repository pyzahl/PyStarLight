"""Camera: Linux video devices (/dev/videoN) without a device: pixel conversions, discovery filter,
time stamps of a device without exposure control."""

from types import SimpleNamespace

import numpy as np
import pytest

from pystarlight.capture import cameras, v4l2_cam
from pystarlight.capture.decode import Decoder
from pystarlight.capture.recorder import file_tag

W, H = 16, 6


def fake_stream(fourcc, data, bytesperline, region=(0, 0, W, H)):
    s = SimpleNamespace(fourcc=fourcc, maps=[data], height=H, full_width=W, bytesperline=bytesperline,
                        region=region)
    return s


def buffer(stream, bytesused=0):
    return v4l2_cam.Buffer(stream, 0, v4l2_cam.SUCCESS, 7, 123, bytesused)


@pytest.fixture
def luma():
    return np.arange(W * H, dtype=np.uint8).reshape(H, W)


def test_yuyv_and_uyvy_give_the_luminance(luma):
    pad = 8                                     # row padding the driver may add
    yuyv = np.zeros((H, 2 * W + pad), np.uint8)
    yuyv[:, 0:2 * W:2], yuyv[:, 1:2 * W:2] = luma, 128
    uyvy = np.zeros((H, 2 * W + pad), np.uint8)
    uyvy[:, 1:2 * W:2], uyvy[:, 0:2 * W:2] = luma, 128
    for name, raw in (("YUYV", yuyv), ("UYVY", uyvy)):
        b = buffer(fake_stream(name, raw.tobytes(), 2 * W + pad))
        np.testing.assert_array_equal(b.get_data(), luma)
        cut = buffer(fake_stream(name, raw.tobytes(), 2 * W + pad, region=(3, 2, 8, 3)))
        assert (cut.get_image_width(), cut.get_image_height()) == (8, 3)
        np.testing.assert_array_equal(cut.get_data(), luma[2:5, 3:11])
    img = Decoder("YUYV").raw(buffer(fake_stream("YUYV", yuyv.tobytes(), 2 * W + pad)).get_data(), W, H)
    assert img.dtype == np.uint8 and Decoder("YUYV").max_value == 255
    np.testing.assert_array_equal(img, luma)


def test_grey_y16_and_mjpg(luma):
    grey = np.zeros((H, W + 4), np.uint8)
    grey[:, :W] = luma
    np.testing.assert_array_equal(buffer(fake_stream("GREY", grey.tobytes(), W + 4)).get_data(), luma)
    y16 = (luma.astype("<u2") * 200)
    d = buffer(fake_stream("Y16", y16.tobytes(), 2 * W)).get_data()
    assert d.dtype == np.dtype("<u2") and Decoder("Y16").max_value == 65535
    np.testing.assert_array_equal(Decoder("Y16").raw(d, W, H), y16)
    cv2 = pytest.importorskip("cv2")
    flat = np.full((H, W), 90, np.uint8)
    ok, jpg = cv2.imencode(".jpg", flat, [cv2.IMWRITE_JPEG_QUALITY, 100])
    raw = bytearray(4096)
    raw[:jpg.size] = jpg.tobytes()
    d = buffer(fake_stream("MJPG", bytes(raw), 0), bytesused=jpg.size).get_data()
    assert d.shape == (H, W) and abs(int(d.mean()) - 90) <= 2
    bad = buffer(fake_stream("MJPG", bytes(4096), 0), bytesused=100).get_data()
    assert bad.shape == (H, W) and not bad.any()              # undecodable: a black frame


def test_find_cameras_keeps_capture_devices_only(monkeypatch):
    devs = {"/dev/video0": ("uvcvideo", "USB Video: USB Video", "usb-1", 0x04200001, ["MJPG", "YUYV"]),
            "/dev/video1": ("uvcvideo", "USB Video: USB Video", "usb-1", 0x04800000, []),   # metadata
            "/dev/video2": ("rp1-cfe", "rp1-cfe", "platform", 0x04200001, ["Y16"]),         # VEYE node
            "/dev/video3": ("em28xx", "EM2860/SAA711X", "usb-2", 0x05200001, ["YUYV"]),
            "/dev/video10": ("bcm2835-codec", "codec", "platform", 0x04208000, ["YUYV"]),
            "/dev/video4": ("cam", "H.264 only", "usb-3", 0x04200001, ["H264"])}
    monkeypatch.setattr(v4l2_cam.glob, "glob", lambda pat: list(devs))
    monkeypatch.setattr(v4l2_cam.os, "open", lambda dev, flags: dev)
    monkeypatch.setattr(v4l2_cam.os, "close", lambda fd: None)
    monkeypatch.setattr(v4l2_cam, "query", lambda fd: devs[fd][:4])
    monkeypatch.setattr(v4l2_cam, "formats", lambda fd: devs[fd][4])
    cams = v4l2_cam.find_cameras()
    assert [c["device"] for c in cams] == ["/dev/video0", "/dev/video3"]
    assert cams[1] == {"id": "v4l2:/dev/video3", "vendor": "em28xx", "model": "EM2860/SAA711X",
                       "serial": "usb-2", "backend": "v4l2", "device": "/dev/video3"}
    assert file_tag(cams[0]["model"]) == "USB-Video-USB-Video"           # no ':' in file names


def test_video_device_stamps_without_exposure_shift():
    cam = SimpleNamespace(kind="v4l2", can_trigger=False, exposure_fixed=True,
                          get_model_name=lambda: "USB Video", get_sensor_size=lambda: (W, H),
                          dup_available_pixel_formats_as_strings=lambda: ["YUYV"],
                          get_region=lambda: (0, 0, W, H), get_pixel_format_as_string=lambda: "YUYV")
    s = cameras.CameraSession(cam)
    s.controller = SimpleNamespace(exposure_us=40_000.0)
    assert s.is_v4l2 and not s.can_trigger and s.exposure_fixed()
    assert s.exposure_start(1_000_000_000, None) == 1_000_000_000              # driver time as it is
    cam.exposure_fixed = False                                                 # a webcam's control
    assert s.exposure_start(1_000_000_000, None) == 1_000_000_000 - 40_000_000


def test_struct_layouts():
    assert v4l2_cam.VIDIOC_QUERYCAP == 0x80685600 and v4l2_cam.VIDIOC_ENUM_FMT == 0xC0405602
    assert v4l2_cam.VIDIOC_G_PARM == 0xC0CC5615 and v4l2_cam.VIDIOC_QUERYCTRL == 0xC0445624
    assert v4l2_cam.VIDIOC_ENUM_FRAMESIZES == 0xC02C564A and v4l2_cam.VIDIOC_S_STD == 0x40085618
    assert v4l2_cam.fourcc_name(0x56595559) == "YUYV"
