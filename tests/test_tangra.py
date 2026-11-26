"""Tangra .lc export (layout of Tangra 3's LCFile.cs) and the compressed movie."""

import shutil
import zlib
from datetime import datetime, timedelta

import numpy as np
import pytest

from pystarlight import open_movie, tangra
from pystarlight.globaltrack import analyze_global
from test_globaltrack import STARS, apertures, render
from test_pipeline import write_ser

N = 60


@pytest.fixture(scope='module')
def run(tmp_path_factory):
    rng = np.random.default_rng(3)
    p = tmp_path_factory.mktemp('t') / 'm.ser'
    write_ser(p, [render(i, rng, False) for i in range(N)])
    with open_movie(str(p)) as mv:
        lc = analyze_global(mv, apertures())
    return str(p), lc


def test_dotnet_string_and_ticks():
    w = tangra._Writer()
    w.str('a' * 200)                         # 2-byte length prefix
    raw = w.buf.getvalue()
    assert raw[:2] == bytes([200 & 0x7F | 0x80, 1])
    assert tangra._Reader(raw).str() == 'a' * 200
    t = datetime(2026, 10, 11, 4, 23, 39, 123456)
    assert tangra.from_ticks(tangra.ticks(t)) == t
    assert tangra.ticks(datetime(1970, 1, 1)) == 621355968000000000     # .NET's Unix epoch


def test_pick_objects_target_first_max_four(run):
    _, lc = run
    names = tangra.pick_objects(lc)
    assert names[0] == 'target' and len(names) == 4 and 'sky' not in names
    assert all(lc.roles[n] == 'reference' for n in names[1:])


def test_lc_round_trip(run, tmp_path):
    path, lc = run
    lc.time_shift_s = 0.02
    out = tmp_path / 'm.lc'
    with open_movie(path) as mv:
        res = tangra.write_lc(lc, mv, out, fwhm=3.3)
        img7 = mv.read(7).image
    assert res['frames'] == N and res['timed'] and res['bit_pix'] == 16
    # a raw deflate stream (DeflateStream), not zlib / gzip
    raw = zlib.decompress(out.read_bytes(), -15)
    assert raw[:2] == (4).to_bytes(2, 'little')
    f = tangra.read_lc(out)
    h = f['header']
    assert h['objects'] == 4 and h['min_frame'] == 0 and h['max_frame'] == N - 1
    assert h['source_info'] == 'Video (SER.16)' and h['timing_type'] == tangra.TIMING_EMBEDDED
    assert h['fps'] == pytest.approx(25.0)              # write_ser: 40 ms steps
    assert len(f['timing']) == N
    t0 = f['timing'][0]['mid_time']
    assert f['timing'][1]['mid_time'] - t0 == timedelta(milliseconds=40)
    assert f['timing'][0]['duration_ms'] == 40
    names = tangra.pick_objects(lc)
    for k, name in enumerate(names):
        col = lc[name]
        ms = f['data'][k]
        assert len(ms) == N and all(m['target'] == k for m in ms)
        m = ms[7]
        assert m['x0'] == pytest.approx(col['xc'][7], abs=1e-4)
        px, py = round(col['xc'][7]), round(col['yc'][7])
        assert (m['pixel_x0'], m['pixel_y0']) == (px, py)
        # the box as recorded, centred on the rounded position
        half = tangra.BOX // 2
        want = img7[py - half:py + half + 1, px - half:px + half + 1]
        assert np.array_equal(m['pixels'], want)
        # Tangra's reading - background is our signal
        assert m['reading'] - m['background'] == pytest.approx(col['signal'][7], abs=2)
    obj = f['footer']['objects']
    assert [o['tracking'] for o in obj] == [2, 0, 0, 0]          # occulted star, guiding stars
    assert f['footer']['data_bit_pix'] == 16 and f['footer']['norm_value'] == 65535
    assert f['footer']['averaged_frame'].shape == img7.shape
    assert f['footer']['fwhm'] == pytest.approx(3.3)
    assert f['footer']['context']['bit_pix'] == 16
    # star at the frame centre of the averaged frame is bright
    r, c = STARS[1][0], STARS[1][1]
    assert f['footer']['averaged_frame'][r - 8:r + 8, c - 8:c + 8].max() > 200


def test_occulted_target_flagged_not_lost(run, tmp_path):
    path, lc = run
    with open_movie(path) as mv:
        tangra.write_lc(lc, mv, tmp_path / 'm.lc')
    f = tangra.read_lc(tmp_path / 'm.lc', pixels=False)
    flags = np.array([m['flags'] for m in f['data'][0]])
    dimmed = (lc['target']['used'] == 0) | (lc['target']['dimmed'] > 0)
    assert dimmed[list(range(40, 55))].all()
    assert np.all(flags[dimmed] == tangra.FLAG_DISAPPEARED)      # a good flag in Tangra
    assert np.all(flags[~dimmed] == tangra.FLAG_TRACKED)


def test_box_edges_are_zero():
    img = np.arange(100, dtype=np.uint16).reshape(10, 10) + 1
    b = tangra._box(img, 0, 0)
    assert b.shape == (35, 35) and b[17, 17] == img[0, 0]
    assert b[:17].sum() == 0 and b[:, :17].sum() == 0


def test_non_tangra_formats_are_labelled_fits():
    assert tangra.source_info('/x/run.ravf', 16).startswith('Video (FITS.16')
    assert tangra.source_info('/x/run.adv', 16) == 'Video (ADV2.16)'


def test_rejects_skipped_frames(run, tmp_path):
    path, lc = run
    import copy
    lc2 = copy.deepcopy(lc)
    for n in lc2.names:
        lc2.columns[n] = {c: v[::2] for c, v in lc2.columns[n].items()}
    with open_movie(path) as mv, pytest.raises(ValueError, match='consecutive'):
        tangra.write_lc(lc2, mv, tmp_path / 'x.lc')


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='no ffmpeg')
@pytest.mark.parametrize('lossless', [False, True], ids=['h264', 'ffv1'])
def test_movie(run, tmp_path, lossless):
    import cv2
    path, _ = run
    out = tmp_path / ('m.mkv' if lossless else 'm.mp4')
    with open_movie(path) as mv:
        res = tangra.write_movie(mv, out, 0, 19, lossless=lossless)
        first = mv.read(0).image
    assert res['frames'] == 20 and res['fps'] == pytest.approx(25.0) and out.stat().st_size > 0
    cap = cv2.VideoCapture(str(out))
    if lossless:
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        cap.set(cv2.CAP_PROP_FORMAT, -1)
    ok, frame = cap.read()
    n = 1 + sum(1 for _ in iter(lambda: cap.read()[0], False))
    cap.release()
    assert ok and n == 20
    if lossless and frame.dtype == np.uint16:
        assert np.array_equal(frame.reshape(first.shape), first)      # 16-bit pixels kept


def _first_frame(path):
    import cv2
    cap = cv2.VideoCapture(str(path))
    ok, frame = cap.read()
    cap.release()
    assert ok
    return frame


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='no ffmpeg')
def test_movie_target_mark_and_light_curve(run, tmp_path):
    path, lc = run
    t = lc['target']
    flux = lc.normalized(window=9)
    analysis = {'frames': t['frame'], 'flux': flux, 'xy': (t['xc'], t['yc']), 'label': 'TYC 1-2-3',
                'target': 'target target', 'what': 'target / 5 references',
                'levels': [(0, 39, 1.0), (40, 54, 0.0), (55, N - 1, 1.0)],
                'marks': [(39.5, 'D', '00:00:01.580'), (54.5, 'R', '00:00:02.180')]}
    out = tmp_path / 'a.mp4'
    with open_movie(path) as mv:
        res = tangra.write_movie(mv, out, 0, 9, bin_factor=1, target=True, curve=True,
                                 analysis=analysis, denoise=False)
        h, w = mv.shape
    assert res['target'] and res['curve'] and not res['note']
    f = _first_frame(out).astype(int)
    assert f.shape[0] > h and f.shape[1] == w                    # the light curve below the frame
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    cx, cy = round(t['xc'][0]), round(t['yc'][0])
    near = (slice(cy - 30, cy + 30), slice(cx - 30, cx + 30))
    assert np.count_nonzero((r[near] > 150) & (r[near] - g[near] > 80)) > 10     # the red cross
    panel = (slice(h + 5, f.shape[0]), slice(0, w))
    yellow = (r[panel] > 150) & (g[panel] > 120) & (b[panel] < 100)
    assert yellow.any() and np.flatnonzero(yellow.any(axis=0)).min() < 12          # cursor at frame 0
    green = (g[panel] > 120) & (r[panel] < 150) & (b[panel] < 150)
    assert green.any()                                                              # step levels


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='no ffmpeg')
def test_movie_without_analysis_says_so(run, tmp_path):
    path, _ = run
    with open_movie(path) as mv:
        res = tangra.write_movie(mv, tmp_path / 'b.mp4', 0, 4, target=True, curve=True)
        h = mv.shape[0] // 2
    assert not res['target'] and not res['curve'] and 'no analysis' in res['note']
    assert _first_frame(tmp_path / 'b.mp4').shape[0] == h       # binned frame only
