"""Automatic reference stars (autopick): what is left out and why, and the spread."""

import math

import numpy as np

from pystarlight import autopick

H, W = 300, 400


def field(stars, noise=10.0, seed=1):
    """stars: (x, y, amp, sigma); 16-bit clipped like a camera."""
    rng = np.random.default_rng(seed)
    y, x = np.indices((H, W))
    img = rng.normal(500, noise, (H, W))
    for sx, sy, a, s in stars:
        img += a * np.exp(-((x - sx) ** 2 + (y - sy) ** 2) / (2 * s ** 2))
    return np.clip(img, 0, 65535)


GOOD = [(60, 60, 8000, 1.5), (340, 60, 7000, 1.5), (60, 240, 6000, 1.5), (340, 240, 5000, 1.5),
        (200, 150, 4000, 1.5)]


def by_xy(stars, x, y):
    return min(stars, key=lambda c: math.hypot(c.x - x, c.y - y))


def test_bad_stars_are_left_out_with_their_reason():
    img = field(GOOD + [(200, 60, 200000, 1.5),            # saturated
                        (130, 150, 9000, 4.0),             # fuzzy: galaxy, defocused double
                        (390, 150, 9000, 1.5),             # at the edge
                        (270, 150, 9000, 1.5), (282, 156, 3000, 1.5),   # pair: neighbour in the box
                        (200, 240, 120, 1.5)])             # faint
    stars = autopick.candidates(img, stamp=(0, 0))
    refs = autopick.pick_references(stars, img.shape, n=10, box=31, full_scale=65535, min_snr=20)
    notes = {xy: by_xy(stars, *xy).note for xy in
             [(200, 60), (130, 150), (390, 150), (270, 150), (200, 240)]}
    assert notes == {(200, 60): 'saturated', (130, 150): 'not star-shaped', (390, 150): 'at the edge',
                     (270, 150): 'neighbour in its box', (200, 240): 'faint'}
    assert sorted((round(c.x), round(c.y)) for c in refs) == sorted((x, y) for x, y, _, _ in GOOD)
    assert all(c.note == '' for c in refs)


def test_brightest_first_then_spread_over_the_field():
    img = field(GOOD + [(80, 70, 7500, 1.5)])              # bright, but next to the brightest
    stars = autopick.candidates(img, stamp=(0, 0))
    refs = autopick.pick_references(stars, img.shape, n=3, box=15, full_scale=65535, pool=3)
    assert (round(refs[0].x), round(refs[0].y)) == (60, 60)
    assert (round(refs[1].x), round(refs[1].y)) == (340, 240)      # the farthest of the pool
    assert by_xy(stars, 80, 70).note == 'spare'


def test_drift_and_target_keep_references_away():
    img = field(GOOD)
    stars = autopick.candidates(img, stamp=(0, 0))
    refs = autopick.pick_references(stars, img.shape, n=5, box=31, drift=(-50.0, 0.0),
                                    avoid=((200.0, 150.0),))
    # the field drifts 50 px to the right: a star at x=340 ends at x=390, its box leaves the frame
    assert by_xy(stars, 340, 60).note == by_xy(stars, 340, 240).note == 'at the edge'
    assert by_xy(stars, 200, 150).note == 'the target'
    assert sorted((round(c.x), round(c.y)) for c in refs) == [(60, 60), (60, 240)]
