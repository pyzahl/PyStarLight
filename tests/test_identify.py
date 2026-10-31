"""Bright-star finding and naming, and RA/Dec in the exports (no network)."""

import json

import numpy as np
import pytest
from astropy.wcs import WCS

from pystarlight.wcs.identify import FieldStar, _nice_name, brightest_stars, identify


def field(stars, shape=(300, 400), seed=0):
    rng = np.random.default_rng(seed)
    y, x = np.indices(shape)
    img = rng.normal(500, 10, shape)
    for xs, ys, a, s in stars:
        img += a * np.exp(-((y - ys) ** 2 + (x - xs) ** 2) / (2 * s * s))
    return img


def tan_wcs(shape=(300, 400)):
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = (310.36, 45.1)
    w.wcs.crpix = ((shape[1] + 1) / 2, (shape[0] + 1) / 2)
    w.wcs.cd = [[-2.07 / 3600, 0], [0, -2.07 / 3600]]
    return w


def test_brightest_by_flux_and_halo_merged():
    # a huge saturated-looking star (wide), a compact bright one, a fainter one, noise
    img = field([(300, 150, 60000, 6.0), (80, 60, 9000, 1.5), (200, 250, 3000, 1.5), (330, 40, 800, 1.5)])
    img = np.minimum(img, 40000)
    stars = brightest_stars(img, 3)
    assert [(round(s.x), round(s.y)) for s in stars] == [(300, 150), (80, 60), (200, 250)]
    assert stars[0].saturated and not stars[1].saturated


def test_names_from_annotations_without_network():
    img = field([(100, 100, 9000, 1.5), (250, 200, 5000, 1.5)])
    ann = [{'names': ['HD 1234'], 'x': 250.4, 'y': 199.7, 'type': 'hd', 'vmag': None},
           {'names': ['α Cyg / 50 Cyg', 'Deneb / Arided'], 'x': 100.2, 'y': 100.1, 'type': 'bright',
            'vmag': 1.25},
           {'names': ['HD 197345'], 'x': 100.2, 'y': 100.1, 'type': 'hd', 'vmag': None}]
    stars = identify(img, tan_wcs(), n=2, annotations=ann, use_simbad=False)
    assert stars[0].name == 'Deneb (α Cyg)' and stars[0].vmag == 1.25
    assert stars[1].name == 'HD 1234' and stars[1].source == 'astrometry.net'
    assert np.isfinite(stars[0].ra) and abs(stars[0].dec - 45.1) < 0.2


def test_nice_name():
    assert _nice_name(['HD 197161']) == 'HD 197161'
    assert _nice_name(['α Cyg / 50 Cyg', 'Deneb / Arided / Aridif']) == 'Deneb (α Cyg)'


def test_csv_has_sky_mapping(tmp_path):
    from pystarlight import Aperture, open_movie
    from pystarlight.globaltrack import analyze_global
    from test_pipeline import render, write_ser
    rng = np.random.default_rng(1)
    p = tmp_path / 'm.ser'
    write_ser(p, [render(i, rng) for i in range(12)])
    aps = [Aperture.centered_at('target', 160, 120, role='target'),
           Aperture.centered_at('ref1', 80, 60, role='reference')]
    with open_movie(str(p)) as mv:
        lc = analyze_global(mv, aps)
    w = tan_wcs((240, 320))
    star = FieldStar(80, 60, 1e5, 3.0, False, *map(float, w.all_pix2world(80, 60, 0)), name='HD 1', vmag=8.0)
    lc.to_pymovie_csv(str(tmp_path / 'a.csv'), wcs=w, stars=[star])
    lc.to_csv(str(tmp_path / 'b.csv'), wcs=w, stars=[star])
    a = (tmp_path / 'a.csv').read_text()
    assert '# plate solution (astrometry.net): 2.070"/px' in a
    assert '# bright star: HD 1 V=8.00' in a and '(HD 1 V=8.00)' in a
    assert a.count('# ____ RA,Dec (J2000):') == 2
    assert sum('FrameNum' in ln for ln in a.splitlines()) == 1          # still PyOTE-readable
    assert '# ref1 RA,Dec (J2000):' in (tmp_path / 'b.csv').read_text()


def test_apertures_json_with_radec(tmp_path):
    from pystarlight.tracking import Aperture
    from pystarlight.viewer import load_apertures, save_apertures
    f = tmp_path / 'a.json'
    save_apertures(f, [Aperture.centered_at('t', 200, 150, role='target')], tan_wcs())
    d = json.loads(f.read_text())[0]
    assert d['ra'] == pytest.approx(310.36, abs=0.01) and 'dec' in d
    assert load_apertures(f)[0].role == 'target'          # extra keys ignored on load


def test_identify_positions_refines_and_names(monkeypatch):
    import pystarlight.wcs.identify as ident
    calls = []

    def fake_simbad(ra, dec, radius_arcsec=10.0):
        calls.append((ra, dec))
        k = len(calls)
        return {'main_id': f'TYC {k}', 'ids': {'TYC': f'TYC {k}'}, 'vmag': 10.0 + k, 'sep_arcsec': 0.5}
    monkeypatch.setattr(ident, 'simbad_lookup', fake_simbad)
    img = field([(100.3, 80.6, 6000, 1.5), (250, 200, 3000, 1.5)])
    found = ident.identify_positions(img, tan_wcs(), {'target': (103, 78), 'ref1': (248, 203)},
                                     annotations=[], log=lambda m: None)
    t = found['target']
    assert t.x == pytest.approx(100.3, abs=0.05) and t.y == pytest.approx(80.6, abs=0.05)
    assert found['target'].name == 'TYC 1' and found['ref1'].vmag == 12.0
    assert len(calls) == 2


def test_viewer_identifies_apertures_through_drift(tmp_path, monkeypatch):
    import pystarlight.wcs.identify as ident
    from pystarlight.tracking import Aperture
    from pystarlight.viewer import Viewer, load_aperture_stars
    from test_pipeline import STARS, render, write_ser
    monkeypatch.setattr(ident, 'simbad_lookup', lambda ra, dec, radius_arcsec=10.0:
                        {'main_id': f'star {ra:.4f}', 'ids': {}, 'vmag': 9.0, 'sep_arcsec': 0.1})
    rng = np.random.default_rng(4)
    p = tmp_path / 'm.ser'
    write_ser(p, [render(i, rng) for i in range(40)])
    v = Viewer(str(p))
    v.wcs = tan_wcs((240, 320))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    v.apertures = [Aperture.centered_at('target', c0, r0, role='target'),
                   Aperture.centered_at('ref1', c1, r1, role='reference')]
    a = v.identify_apertures(frame=0)
    v.go(30)                                     # field has moved by several px (wind shake)
    for ap in v.apertures:                       # user re-places apertures on frame 30
        ap.x0 += round(5 * np.cos(30 / 4) - 5 * np.cos(0))
        ap.y0 += round(4 * np.sin(30 / 3))
    b = v.identify_apertures(frame=30)
    for n in a:                                  # same stars, same sky positions
        assert abs(a[n].ra - b[n].ra) * 3600 < 1.5 and abs(a[n].dec - b[n].dec) * 3600 < 1.5
    v._poll_identify = lambda: None
    from pystarlight.viewer import save_apertures
    save_apertures(v.aperture_file, v.apertures, v.wcs, v.ap_stars)
    assert set(load_aperture_stars(v.aperture_file)) == {'target', 'ref1'}


def test_simbad_failure_pauses_lookups(monkeypatch):
    import pystarlight.wcs.identify as ident
    calls = []

    def down(ra, dec, radius_arcsec=10.0):
        calls.append(ra)
        raise ConnectionError('no network')
    monkeypatch.setattr(ident, 'simbad_lookup', down)
    logs = []
    stars = [ident.name_star(ident.FieldStar(x, 50.0, 1.0, 3.0, False), tan_wcs(), [], log=logs.append)
             for x in (40.0, 60.0, 80.0)]
    assert len(calls) == 1 and all(not s.name for s in stars)       # one timeout, not three
    assert 'SIMBAD skipped' in logs[-1]


def test_radec_text_without_position_is_empty_and_quiet():
    import warnings

    from pystarlight.wcs.identify import FieldStar, radec_text
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        assert radec_text(float('nan'), 10.0) == '' and FieldStar(1, 2, 3, 4, False).radec == ''
        assert radec_text(310.36, 45.1).startswith('20:41:')


def test_simbad_lookup_gives_up(monkeypatch):
    import threading
    import time

    import pystarlight.wcs.identify as ident
    never = threading.Event()
    monkeypatch.setattr(ident, '_simbad_query', lambda *a: never.wait(30))
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        ident.simbad_lookup(310.0, 45.0, timeout=0.2)
    assert time.monotonic() - t0 < 2
    never.set()
    monkeypatch.setattr(ident, '_simbad_query', lambda *a: {'main_id': 'HD 1'})
    assert ident.simbad_lookup(310.0, 45.0)['main_id'] == 'HD 1'
