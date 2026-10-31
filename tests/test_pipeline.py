"""End-to-end: synthetic movies with wind-shake / field rotation through readers + analyze()."""

import math

import numpy as np
import pytest

from pystarlight import Aperture, analyze, open_movie
from pystarlight import normalize as nz
from pystarlight.movie import SER_HEADER_SIZE

H, W = 240, 320
STARS = [(120, 160, 4000), (60, 80, 2500), (180, 250, 3000)]   # (row, col, amp)
EVENT = range(40, 50)                                         # star 0 occulted


def render(i, rng, rotate=False, peak=None):
    y, x = np.indices((H, W))
    img = rng.normal(300, 8, (H, W))
    jy, jx = 4 * math.sin(i / 3), 5 * math.cos(i / 4)          # wind shake
    th = math.radians(0.15 * i) if rotate else 0.0
    cy, cx = STARS[0][0], STARS[0][1]
    for k, (r, c, a) in enumerate(STARS):
        rr = cy + (r - cy) * math.cos(th) + (c - cx) * math.sin(th)
        cc = cx - (r - cy) * math.sin(th) + (c - cx) * math.cos(th)
        amp = a * (0.1 if (k == 0 and i in EVENT) else 1.0) * (0.7 + 0.3 * math.cos(i / 15))  # clouds
        img += amp * np.exp(-((y - rr - jy) ** 2 + (x - cc - jx) ** 2) / (2 * 1.6 ** 2))
    if peak:
        img = img * peak / img.max()
    return img


def truth(i, k, rotate=False):
    """True (x, y) of star k in frame i."""
    jy, jx = 4 * math.sin(i / 3), 5 * math.cos(i / 4)
    th = math.radians(0.15 * i) if rotate else 0.0
    cy, cx = STARS[0][0], STARS[0][1]
    r, c = STARS[k][0], STARS[k][1]
    rr = cy + (r - cy) * math.cos(th) + (c - cx) * math.sin(th)
    cc = cx - (r - cy) * math.sin(th) + (c - cx) * math.cos(th)
    return cc + jx, rr + jy


def position_error(lc, name, k, rotate=False):
    xy = np.array([truth(int(i), k, rotate) for i in lc[name]['frame']])
    return np.hypot(lc[name]['xc'] - xy[:, 0], lc[name]['yc'] - xy[:, 1])


def write_ser(path, frames, ticks0=638000000000000000):
    hdr = bytearray(SER_HEADER_SIZE)
    hdr[0:14] = b'LUCAM-RECORDER'
    hdr[14:42] = np.array([0, 0, 0, W, H, 16, len(frames)], '<i4').tobytes()
    with open(path, 'wb') as f:
        f.write(bytes(hdr))
        for fr in frames:
            f.write(np.clip(fr, 0, 65535).astype('<u2').tobytes())
        f.write((ticks0 + 400000 * np.arange(len(frames))).astype('<i8').tobytes())


@pytest.fixture(scope='module')
def ser_file(tmp_path_factory):
    rng = np.random.default_rng(0)
    p = tmp_path_factory.mktemp('m') / 'shake.ser'
    write_ser(p, [render(i, rng) for i in range(80)])
    return str(p)


def apertures(two_yellow=False):
    aps = [Aperture.centered_at('target', 160, 120, color='red', default_mask_radius=4.0),
           Aperture.centered_at('ref1', 80, 60, color='yellow', default_mask_radius=4.0),
           Aperture.centered_at('ref2', 250, 180, color='yellow' if two_yellow else 'red',
                                default_mask_radius=4.0),
           Aperture.centered_at('sky', 30, 200, color='white')]
    return aps


def test_ser_reader(ser_file):
    with open_movie(ser_file) as mv:
        assert len(mv) == 80
        f = mv.read(5)
        assert f.image.shape == (H, W) and f.image.dtype == np.uint16
        assert f.date and f.time
        assert mv.metadata['FrameCount'] == 80


def test_one_yellow_tracks_and_detects_event(ser_file):
    with open_movie(ser_file) as mv:
        lc = analyze(mv, apertures())
    t = lc['target']
    for k, name in enumerate(('target', 'ref1', 'ref2')):
        assert position_error(lc, name, k).max() <= 1.0
    norm = nz.normalize(t['signal'], lc['ref1']['signal'] + lc['ref2']['signal'], window=9)
    base = np.r_[0:35, 55:80]
    ev = np.r_[42:48]
    assert norm[ev].mean() < 0.3 * norm[base].mean()
    # normalisation removes the slow 'cloud' modulation
    raw_scatter = np.std(t['signal'][base]) / np.mean(t['signal'][base])
    norm_scatter = np.std(norm[base]) / np.mean(norm[base])
    assert norm_scatter < raw_scatter / 3


def test_backwards_run_is_ascending(ser_file):
    with open_movie(ser_file) as mv:
        lc = analyze(mv, apertures(), start=60, stop=20)
    np.testing.assert_array_equal(lc['target']['frame'], np.arange(20, 61))


def test_two_yellow_follows_rotation(tmp_path):
    rng = np.random.default_rng(1)
    p = tmp_path / 'rot.ser'
    write_ser(p, [render(i, rng, rotate=True) for i in range(80)])
    with open_movie(str(p)) as mv:
        lc = analyze(mv, apertures(two_yellow=True))
    for k, name in enumerate(('target', 'ref1', 'ref2')):
        err = position_error(lc, name, k, rotate=True)
        assert err.mean() < 1.0 and err.max() < 7.0
    base = np.r_[0:35, 55:80]
    sig = lc['target']['signal']
    assert sig[42:48].mean() < 0.3 * sig[base].mean()


def test_fits_folder(tmp_path):
    from astropy.io import fits
    rng = np.random.default_rng(2)
    for i in range(12):
        hdu = fits.PrimaryHDU(np.clip(render(i, rng), 0, 65535).astype('uint16'))
        hdu.header['DATE-OBS'] = f'2026-10-11T05:05:{i:02d}.1234567'
        hdu.writeto(tmp_path / f'frame_{i}.fits')
    with open_movie(str(tmp_path)) as mv:
        assert len(mv) == 12
        f = mv.read(10)
        assert f.status['file'] == 'frame_10.fits'       # natural sort
        assert f.time == '05:05:10.1234567'
        lc = analyze(mv, apertures())
    assert len(lc['target']['frame']) == 12


def test_avi(tmp_path):
    import cv2
    rng = np.random.default_rng(3)
    p = str(tmp_path / 'm.avi')
    vw = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*'FFV1'), 25, (W, H), isColor=False)
    if not vw.isOpened():
        pytest.skip('no FFV1 encoder')
    for i in range(30):
        vw.write(np.clip(render(i, rng, peak=250), 0, 255).astype('uint8'))
    vw.release()
    with open_movie(p) as mv:
        assert len(mv) == 30
        a, b = mv.read(7).image, mv.read(3).image          # random access, backwards seek
        assert a.dtype == np.uint8 and a.shape == (H, W) and b.shape == (H, W)
        lc = analyze(mv, apertures())
    assert np.all(lc['target']['signal'][:5] > 0)


def test_adv2_numpy2_compat_shim():
    from pystarlight.movie import _NumpyNewshapeCompat
    c = _NumpyNewshapeCompat(np)
    a = np.arange(6, dtype='uint16')
    assert c.reshape(a, newshape=(2, 3)).shape == (2, 3)       # Adv2's call
    assert c.reshape(a, (3, 2)).shape == (3, 2)                 # normal call
    assert c.uint16 is np.uint16                                # everything else is numpy
    from pystarlight.movie import _import_adv2_file, _patch_adv2_for_numpy2
    try:
        adv = _import_adv2_file()
    except ImportError as e:                    # not installed, or no native build (Apple Silicon)
        pytest.skip(str(e))
    _patch_adv2_for_numpy2()
    assert adv.np.reshape(a, newshape=(2, 3)).shape == (2, 3)


def test_close_is_idempotent_for_all_readers(ser_file, tmp_path):
    import cv2
    from astropy.io import fits
    paths = [ser_file]
    p = str(tmp_path / 'a.avi')
    vw = cv2.VideoWriter(p, cv2.VideoWriter_fourcc(*'FFV1'), 25, (32, 24), isColor=False)
    if vw.isOpened():
        for _ in range(3):
            vw.write(np.zeros((24, 32), np.uint8))
        vw.release()
        paths.append(p)
    d = tmp_path / 'fits'
    d.mkdir()
    fits.PrimaryHDU(np.zeros((4, 4), np.uint16)).writeto(d / 'f_0.fits')
    paths.append(str(d))
    for path in paths:
        mv = open_movie(path)
        mv.read(0)
        mv.close()
        mv.close()                                    # second close must not raise
        with open_movie(path) as m2:
            m2.read(0)
            m2.close()                                # close inside `with`, then __exit__ closes again
