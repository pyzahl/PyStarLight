"""Global tracking with roles, peak fits and sub-pixel photometry."""

import math

import numpy as np
import pytest

from pystarlight import Aperture, open_movie
from pystarlight.globaltrack import (
    analyze_global,
    aperture_weights,
    circle_sum,
    fit_peak,
    fit_rigid,
)
from test_pipeline import write_ser

H, W = 240, 320
CY, CX = 120, 160
# (row, col, amplitude): target first
STARS = [(110, 150, 900), (50, 60, 3000), (60, 260, 2500), (190, 80, 2000), (200, 270, 3500),
         (150, 220, 1500)]
EVENT = range(40, 55)
SIGMA = 1.4


def truth(i, k, rotate):
    jy, jx = 6 * math.sin(i / 2.5), 6 * math.cos(i / 3.3)
    th = math.radians(0.15 * i) if rotate else 0.0
    r, c = STARS[k][0], STARS[k][1]
    rr = CY + (r - CY) * math.cos(th) + (c - CX) * math.sin(th)
    cc = CX - (r - CY) * math.sin(th) + (c - CX) * math.cos(th)
    return cc + jx, rr + jy


def render(i, rng, rotate):
    y, x = np.indices((H, W))
    img = rng.normal(300, 8, (H, W))
    cloud = 0.7 + 0.3 * math.cos(i / 15)
    for k, (_, _, a) in enumerate(STARS):
        if k == 0 and i in EVENT:
            continue                                   # target fully occulted
        cx, cy = truth(i, k, rotate)
        img += a * cloud * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * SIGMA ** 2))
    if i == 20:
        img[60, 64] = 60000                            # cosmic ray next to a reference star
    return img


@pytest.fixture(scope='module', params=[False, True], ids=['shake', 'shake+rotation'])
def movie(request, tmp_path_factory):
    rng = np.random.default_rng(5)
    p = tmp_path_factory.mktemp('g') / 'm.ser'
    write_ser(p, [render(i, rng, request.param) for i in range(90)])
    return str(p), request.param


def apertures():
    aps = [Aperture.centered_at('target', 150, 110, role='target')]
    aps += [Aperture.centered_at(f'ref{k}', c, r, role='reference')
            for k, (r, c, _) in enumerate(STARS[1:], 1)]
    aps.append(Aperture.centered_at('sky', 280, 130, role='sky'))
    return aps


def test_tracks_through_occultation(movie):
    path, rotate = movie
    with open_movie(path) as mv:
        lc = analyze_global(mv, apertures())
    t = lc['target']
    xy = np.array([truth(i, 0, rotate) for i in range(90)])
    err = np.hypot(t['xc'] - xy[:, 0], t['yc'] - xy[:, 1])
    assert err.max() < 0.3, err.max()                  # aperture stays on the (invisible) target
    ev = np.array(list(EVENT))
    assert np.all(t['used'][ev] == 0)                  # target dropped out of the tracking fit
    assert np.all(lc.frame_info['n_stars'] >= 5)
    if rotate:
        assert lc.frame_info['rot_deg'][-1] == pytest.approx(0.15 * 89, abs=0.05)  # +: x->y, y down

    flux = lc.normalized(window=9)
    base = np.r_[0:38, 57:90]
    assert np.abs(flux[ev]).max() < 0.05               # occulted: zero flux
    assert np.std(flux[base]) < 0.03                   # clouds removed by references


def test_sky_background(movie):
    path, _ = movie
    with open_movie(path) as mv:
        lc = analyze_global(mv, apertures(), background='sky')
    assert np.allclose(lc['target']['bkavg'], lc['sky']['bkavg'], atol=2)
    assert lc['sky']['bkavg'].mean() == pytest.approx(300, abs=2)


def test_peak_fit_subpixel():
    rng = np.random.default_rng(1)
    y, x = np.indices((40, 40))
    for x0, y0 in [(20.0, 20.0), (19.3, 21.7), (22.5, 18.25)]:
        star = 2000 * np.exp(-((y - y0) ** 2 + (x - x0) ** 2) / (2 * 1.6 ** 2))
        img = 100 + star + rng.normal(0, 5, (40, 40))
        f = fit_peak(img, 20, 20)
        assert f.ok and abs(f.x - x0) < 0.05 and abs(f.y - y0) < 0.05
        assert f.fwhm == pytest.approx(2.3548 * 1.6, rel=0.05)


def test_peak_fit_ignores_hot_pixel():
    img = np.full((40, 40), 100.0)
    y, x = np.indices((40, 40))
    img += 800 * np.exp(-((y - 25) ** 2 + (x - 15) ** 2) / (2 * 1.5 ** 2))
    img[10, 30] = 30000
    f = fit_peak(img, 20, 20)
    assert abs(f.x - 15) < 0.1 and abs(f.y - 25) < 0.1


def test_rigid_fit():
    rng = np.random.default_rng(2)
    ref = rng.uniform(0, 300, (6, 2))
    th, dx, dy = math.radians(3), 4.2, -1.7
    cur = np.c_[math.cos(th) * ref[:, 0] - math.sin(th) * ref[:, 1] + dx,
                math.sin(th) * ref[:, 0] + math.cos(th) * ref[:, 1] + dy]
    t = fit_rigid(ref, cur, np.ones(6))
    assert (t.dx, t.dy, t.theta) == pytest.approx((dx, dy, th))


def test_subpixel_aperture_area():
    for r in (2.0, 3.3, 5.0):
        w = aperture_weights(10.37, 9.81, r, 0, 0, (21, 21), ss=10)
        assert w.sum() == pytest.approx(math.pi * r * r, rel=0.01)
    img = np.ones((30, 30))
    s, area = circle_sum(img, 14.6, 15.2, 4.0)
    assert s == pytest.approx(area)


def render_drift(i, rng, depth):
    """Target drifts 0.03 px/frame relative to the field and dims to `depth` in EVENT."""
    y, x = np.indices((H, W))
    img = rng.normal(300, 8, (H, W))
    cloud = 0.7 + 0.3 * math.cos(i / 15)
    for k, (_, _, a) in enumerate(STARS):
        cx, cy = truth(i, k, False)
        if k == 0:
            cx += 0.03 * i
            if i in EVENT:
                a = a * depth
        img += a * cloud * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * SIGMA ** 2))
    return img


@pytest.mark.parametrize('depth', [0.15, 0.0])
def test_partial_occultation_and_drift(tmp_path, depth):
    rng = np.random.default_rng(9)
    p = tmp_path / 'drift.ser'
    write_ser(p, [render_drift(i, rng, depth) for i in range(90)])
    with open_movie(str(p)) as mv:
        lc = analyze_global(mv, apertures())
    t = lc['target']
    ev = np.array(list(EVENT))
    assert np.all((t['dimmed'][ev] == 1) | (t['used'][ev] == 0))   # never steered tracking in the event
    xy = np.array([truth(i, 0, False) for i in range(90)])
    true_x = xy[:, 0] + 0.03 * np.arange(90)
    err = np.hypot(t['xc'] - true_x, t['yc'] - xy[:, 1])
    assert err.max() < 0.25, err.max()                              # drift followed, also across the gap
    flux = lc.normalized(window=9)
    assert np.median(flux[ev]) == pytest.approx(depth, abs=0.05)


def test_explicit_event_window(movie):
    path, _ = movie
    with open_movie(path) as mv:
        lc = analyze_global(mv, apertures(), event=(38, 56))
    assert np.all(lc['target']['used'][38:57] == 0)
