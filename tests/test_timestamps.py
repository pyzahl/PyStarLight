"""VTI OCR and timing checks."""

import math

import cv2
import numpy as np
import pytest

from pystarlight import Aperture, analyze, open_movie
from pystarlight.timestamps import VtiReader, check_timing, parse_time, unwrap_midnight

H, W = 480, 720
FIELD_DT = 1 / 59.94


def glyph(d, h, w):
    g = np.zeros((h, w), np.uint8)
    cv2.putText(g, str(d), (5, h - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 255, 1, cv2.LINE_AA)
    return g


def iota_digits(t, field_count):
    """IOTA string: hhmmss + two 4-digit 0.1 ms fields (one is blank in each field)."""
    hh, mm = int(t // 3600), int(t % 3600 // 60)
    ss = int(t % 60)
    ff = int(round((t - math.floor(t)) * 10000)) % 10000
    s = f'{hh:02d}{mm:02d}{ss:02d}'
    return s + (f'{ff:04d}    ' if field_count % 2 == 0 else f'    {ff:04d}')


def draw_field(field_img, boxes, text):
    for (xL, xR, yL, yU), ch in zip(boxes, text):
        if ch != ' ':
            field_img[yL:yU + 1, xL:xR + 1] = glyph(int(ch), yU - yL + 1, xR - xL + 1)


def vti_frame(t0, upper_boxes, lower_boxes, rng, top_first=True):
    img = rng.normal(40, 3, (H, W)).clip(0, 255).astype(np.uint8)
    t_up, t_lo = (t0, t0 + FIELD_DT) if top_first else (t0 + FIELD_DT, t0)
    up, lo = img[0::2].copy(), img[1::2].copy()
    draw_field(up, upper_boxes, iota_digits(t_up, 0 if top_first else 1))
    draw_field(lo, lower_boxes, iota_digits(t_lo, 1 if top_first else 0))
    img[0::2], img[1::2] = up, lo
    return img


@pytest.fixture
def trained():
    vti = VtiReader('iota3-full', width=720)
    rng = np.random.default_rng(0)
    img = rng.normal(40, 3, (H, W)).clip(0, 255).astype(np.uint8)
    up = img[0::2].copy()
    draw_field(up, vti.upper_boxes_left, '01234567890123')
    img[0::2] = up
    for d in range(10):
        vti.train(img, 'upper', d, d)
    assert vti.ready
    return vti


@pytest.mark.parametrize('top_first', [True, False])
def test_vti_reads_both_fields(trained, top_first):
    rng = np.random.default_rng(1)
    t0 = 5 * 3600 + 5 * 60 + 24.3317
    for k in range(6):
        t = t0 + 2 * k * FIELD_DT
        r = trained.read(vti_frame(t, trained.upper_boxes_left, trained.lower_boxes_left, rng, top_first))
        assert r.upper.ok and r.lower.ok
        first, second = (r.upper, r.lower) if top_first else (r.lower, r.upper)
        assert first.time == pytest.approx(t, abs=1e-4)
        assert second.time == pytest.approx(t + FIELD_DT, abs=1e-4)
        assert r.top_field_first is top_first


def test_vti_save_load_roundtrip(trained, tmp_path):
    trained.save(tmp_path)
    again = VtiReader.from_pymovie_folder(str(tmp_path))
    assert again.ready and again.profile.code == 'iota'
    rng = np.random.default_rng(2)
    img = vti_frame(3600.5, trained.upper_boxes_left, trained.lower_boxes_left, rng)
    assert again.read(img).upper.timestamp == trained.read(img).upper.timestamp


def test_analyze_with_vti(trained, tmp_path):
    rng = np.random.default_rng(3)
    p = str(tmp_path / 'vti.avi')
    vw = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*'FFV1'), 29.97, (W, H), isColor=False)
    if not vw.isOpened():
        pytest.skip('no FFV1 encoder')
    t0 = 7200.0
    for k in range(20):
        img = vti_frame(t0 + 2 * k * FIELD_DT, trained.upper_boxes_left, trained.lower_boxes_left, rng)
        cv2.circle(img, (360, 120), 3, 255, -1)
        vw.write(img)
    vw.release()
    aps = [Aperture.centered_at('star', 360, 120)]
    with open_movie(p) as mv:
        lc = analyze(mv, aps, vti=trained, fields=True)
    assert lc.top_field_first is True
    rep = check_timing(lc.time)
    assert rep.ok, rep.summary()
    assert rep.step == pytest.approx(FIELD_DT, abs=2e-4)
    assert parse_time(lc.time[0]) == pytest.approx(t0, abs=1e-4)


# --------------------------------------------------------------------------- timing

def test_parse_time():
    assert parse_time('[05:05:24.3317]') == pytest.approx(18324.3317)
    assert parse_time('05:05:24.331755') == pytest.approx(18324.331755)
    assert math.isnan(parse_time('[00:00:00.0000]'))
    assert math.isnan(parse_time(None))
    assert math.isnan(parse_time('garbage'))


def test_check_timing_finds_problems():
    dt = 0.04
    t = list(np.arange(50) * dt + 1000)
    del t[10:12]                       # 2 dropped frames before original row 12
    t.insert(20, t[19])                # frame repeated by the recorder
    t[30] = float('nan')               # unreadable
    t[40] = t[40] + 0.5 * dt           # irregular
    rep = check_timing(t)
    assert rep.step == pytest.approx(dt)
    assert rep.dropped == [(10, 2)]
    assert rep.duplicates == [20]
    assert rep.unreadable == [30]
    assert 40 in rep.irregular
    assert not rep.ok


def test_midnight_rollover():
    t = np.array([86399.96, 86399.98, 0.0 + 1e-9, 0.02])
    u = unwrap_midnight(t)
    assert np.all(np.diff(u) > 0)
    assert check_timing(['[23:59:59.9600]', '[23:59:59.9800]', '[00:00:00.0000]', '[00:00:00.0200]',
                         '[00:00:00.0400]']).unreadable == [2]


# --------------------------------------------------------------------------- parity with PyMovie

pm = None
try:
    import pymovie.main as pm  # noqa: F811
except Exception:  # pragma: no cover
    pass


@pytest.mark.skipif(pm is None, reason='needs PyMovie importable')
@pytest.mark.parametrize('profile', ['iota3-full', 'kiwi-left', 'kiwi-PAL-right', 'boxsprite', 'GHS'])
def test_vti_reader_matches_pymovie(profile):
    """Same frames through VtiReader.read and PyMovie.extractTimestamps."""
    from unittest.mock import MagicMock

    rng = np.random.default_rng(7)
    h = 576 if 'PAL' in profile else H
    vti = VtiReader(profile, width=720)
    vti.digits = [rng.integers(0, 255, np.shape(vti.box_image(np.zeros((h, W), np.uint8), 'upper', 0)))
                  .astype(np.uint8) for _ in range(10)]

    f = MagicMock()
    f.timestampReadingEnabled = True
    f.kiwiInUse = vti.profile.kiwi and vti.profile.slant
    f.kiwiPALinUse = vti.profile.kiwi and not vti.profile.slant
    f.upperOcrBoxesLeft, f.lowerOcrBoxesLeft = vti.upper_boxes_left, vti.lower_boxes_left
    f.upperOcrBoxesRight, f.lowerOcrBoxesRight = vti.upper_boxes_right, vti.lower_boxes_right
    f.modelDigits = vti.digits
    f.timestampFormatter = vti.profile.formatter
    f.upper_left_count = f.upper_right_count = f.lower_left_count = f.lower_right_count = 0
    f.currentUpperBoxPos = f.currentLowerBoxPos = vti.upper_pos
    f.analysisPaused = False
    f.viewFieldsCheckBox.isChecked.return_value = False
    f.detectFieldTimeOrder = False

    for _ in range(8):
        img = rng.integers(0, 255, (h, W)).astype(np.uint8)
        # paint a few digit templates into boxes so scores vary
        up = img[0::2].copy()
        for (xL, xR, yL, yU) in vti.upper_boxes_left[:6]:
            d = vti.digits[rng.integers(10)]
            if d.shape == (yU - yL + 1, xR - xL + 1):
                up[yL:yU + 1, xL:xR + 1] = d
        img[0::2] = up
        f.upper_field, f.lower_field = img[0::2], img[1::2]
        ut, utime, _us, ucum, lt, ltime, _ls, lcum = pm.PyMovie.extractTimestamps(f, printresults=False)
        r = vti.read(img)
        assert (r.upper.timestamp, r.upper.cum_score, r.lower.timestamp, r.lower.cum_score) == \
            (ut, ucum, lt, lcum)
        assert r.upper.time == pytest.approx(utime) and r.lower.time == pytest.approx(ltime)
        assert (vti.upper_left_count, vti.upper_right_count, vti.lower_left_count, vti.lower_right_count) == \
            (f.upper_left_count, f.upper_right_count, f.lower_left_count, f.lower_right_count)
