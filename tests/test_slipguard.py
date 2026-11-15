"""TEMPORARY: the VEYE IMX265 word-slip guard (capture/slipguard.py). Delete with it."""
import numpy as np
import pytest

from pystarlight.capture import slipguard
from pystarlight.capture.acquisition import Grabber
from pystarlight.capture.slipguard import SlipGuard, pack_mipi, unpack_mipi_rows

W, H = 640, 120


def star_field(seed=1):
    """12-bit sky: pedestal, noise, a few stars, one saturated."""
    rng = np.random.default_rng(seed)
    img = rng.normal(250, 6, (H, W))
    yy, xx = np.mgrid[:H, :W]
    for x, y, a in [(100, 30, 800), (400, 60, 3000), (550, 100, 9000)]:
        img += a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 8)
    return np.clip(img, 0, 4094).astype(np.uint16)


def as_received(img, slip_bytes, bits=12, fmt="Y12"):
    """The camera's byte stream delayed by slip_bytes (whole frame, across rows), then
    split into lines and unpacked line by line as the Pi does for Y12 (value << 4)."""
    stream = pack_mipi(img, bits).ravel()
    stream = np.concatenate([np.zeros(slip_bytes, np.uint8), stream])[:stream.size]
    rows = stream.reshape(H, -1)
    if fmt.endswith("P"):
        return rows.tobytes()
    shift = 16 - bits
    return (unpack_mipi_rows(rows, bits) << shift).astype("<u2").tobytes()


def test_pack_unpack_roundtrip():
    img = star_field()
    for bits in (12, 10):
        v = img >> (12 - bits)
        assert (unpack_mipi_rows(pack_mipi(v, bits), bits) == v).all()


@pytest.mark.parametrize("fmt,bits", [("Y12", 12), ("Y12P", 12), ("Y10", 10), ("Y10P", 10)])
def test_slips_detected(fmt, bits):
    img = star_field() >> (12 - bits)
    group = slipguard.GROUP[bits][1]
    for slip in (0, 8, 16, 24, 32, 40):
        g = SlipGuard(fmt, W, H)
        assert g.check(as_received(img, slip, bits, fmt)) == (slip % group != 0), (fmt, slip)


def test_flat_frame_keeps_last_verdict():
    g = SlipGuard("Y12", W, H)
    flat = np.zeros((H, W), np.uint16)
    assert not g.check(as_received(flat, 0))
    assert g.check(as_received(star_field(), 8))
    assert g.check(as_received(flat, 8))         # undecided: still slipped


def test_unsupported_formats_stay_idle():
    assert not SlipGuard("GREY", W, H).enabled
    assert not SlipGuard("Y12", W, H, region_x=3).enabled
    assert SlipGuard("Y12", W, H, region_x=64).enabled


def test_wanted():
    assert slipguard.wanted("auto", "MV-MIPI-IMX265M")
    assert not slipguard.wanted("auto", "MV-MIPI-IMX178M")
    assert slipguard.wanted("on", "MV-MIPI-IMX178M")
    assert not slipguard.wanted("off", "MV-MIPI-IMX265M")


class FakeBuffer:
    def __init__(self, i, data):
        self.i, self.data = i, data

    def get_status(self):
        return "ok"

    def get_frame_id(self):
        return self.i

    def get_system_timestamp(self):
        return 1_000_000_000 + self.i * 66_666_667

    def get_data(self):
        return self.data


class FakeStream:
    def __init__(self, frames, grabber):
        self.frames, self.grabber, self.pushed, self.starts = list(frames), grabber, [], 1

    def timeout_pop_buffer(self, timeout_us):
        if not self.frames:
            self.grabber.running = False
            return None
        return self.frames.pop(0)

    def push_buffer(self, buf):
        self.pushed.append(buf.i)


class FakeRecorder:
    def __init__(self):
        self.ids = []

    def wants(self, ns):
        return True

    def put(self, raw, fid, *a):
        self.ids.append(fid)


def test_grabber_drops_slipped_frames_and_asks_for_a_reset():
    img = star_field()
    slips = [0, 0, 0, 8, 8, 16, 24, 0, 0]       # 24: clean but shifted (not detectable)
    rec = FakeRecorder()
    g = Grabber(None, "ok", rec)
    g.stream = FakeStream([FakeBuffer(i, as_received(img, s)) for i, s in enumerate(slips)], g)
    calls = []
    g.slipguard_factory = lambda: SlipGuard("Y12", W, H)
    g.on_slip = lambda: calls.append(1)
    g.run()
    assert rec.ids == [0, 1, 2, 6, 7, 8]
    assert g.slipped == 3 and g.slip_events == 1 and len(calls) == 3


def test_slip_mid_frame_is_caught():
    """The slip starts at some row: the rows above are fine, everything below is not."""
    img = star_field()
    group_bytes = img.shape[1] * 3 // 2
    for row in (5, 60, H - 2):
        stream = pack_mipi(img, 12).ravel()
        at = row * group_bytes + 100            # mid-row
        stream = np.concatenate([stream[:at], np.zeros(8, np.uint8), stream[at:]])[:stream.size]
        rows = stream.reshape(H, -1)
        raw = (unpack_mipi_rows(rows, 12) << 4).astype("<u2").tobytes()
        assert SlipGuard("Y12", W, H).check(raw), row


@pytest.mark.parametrize("sky", [3.0, 40.0, 250.0])
def test_no_false_alarms_on_faint_frames(sky):
    rng = np.random.default_rng(7)
    for _ in range(50):
        img = np.clip(rng.normal(sky, 2.0, (H, W)), 0, 4095).astype(np.uint16)
        assert not SlipGuard("Y12", W, H).check(as_received(img, 0))
        assert SlipGuard("Y12", W, H).check(as_received(img, 8))
