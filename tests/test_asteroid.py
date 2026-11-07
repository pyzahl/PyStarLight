"""Asteroid as the target (wcs.asteroid): names, the star on its path; Horizons is faked."""

import pytest

from pystarlight.wcs import asteroid, identify, localcat


@pytest.mark.parametrize('text, hid, idt, label', [
    ('(476) Hedwig', '476;', None, '(476) Hedwig'),
    ('(476)', '476;', None, '(476)'),
    ('476 Hedwig', '476;', None, '(476) Hedwig'),
    ('476-Hedwig', '476;', None, '(476) Hedwig'),
    ('2003 SB60', '2003 SB60', 'smallbody', '2003 SB60'),
    ('(158772) 2003 SB60', '158772;', None, '(158772) 2003 SB60'),
    ('158772-2003-SB60', '158772;', None, '(158772) 2003 SB60'),
])
def test_asteroid_names(text, hid, idt, label):
    b = asteroid.parse_asteroid(text)
    assert (b.horizons_id, b.id_type, b.label) == (hid, idt, label)


@pytest.mark.parametrize('text', ['UCAC4-593-004965', 'TYC 3574-1718-1', 'HD 197161', 'Deneb',
                                  '310.2 45.2', '20 40 52.0 +45 14 06'])
def test_stars_are_no_asteroids(text):
    assert asteroid.parse_asteroid(text) is None or localcat.parse_star_id(text) is not None


def test_horizons_names_become_labels():
    assert asteroid.label_of('476 Hedwig (A901 QB)', 'x') == '(476) Hedwig'
    assert asteroid.label_of('158772 (2003 SB60)', 'x') == '(158772) 2003 SB60'
    assert asteroid.label_of('odd', 'fallback') == 'fallback'
    jd = asteroid.julian_day('2000-01-01', '12:00:00.0')
    assert jd == pytest.approx(2451545.0)


PATH = [(2461303.0 + k / 1440, 320.70 + k * 0.0002, -1.75 + k * 0.0001) for k in range(5)]   # ~1"/min


class Cat:
    """Two stars near the path: one 3" off it, one 30" off; cone() as the local catalogue's."""

    def cone(self, ra, dec, radius_arcsec, epoch=None):
        rows = [{'source_id': 1, 'ra': 320.7004, 'dec': -1.7498 + 3 / 3600, 'g': 12.1,
                 'ucac4': 441_000123, 'tyc': 0, 'hip': 0},
                {'source_id': 2, 'ra': 320.7004, 'dec': -1.7498 - 30 / 3600, 'g': 9.0,
                 'ucac4': 441_000456, 'tyc': 0, 'hip': 0}]
        return [r for r in rows if asteroid._offset_arcsec(r['ra'], r['dec'], [(0, ra, dec)] * 2)
                <= radius_arcsec]

    def find(self, *a, **kw):
        return None


def test_the_star_on_the_path_is_the_target(monkeypatch):
    star = asteroid.occulted_star(PATH, Cat())
    assert star['name'] == 'UCAC4 441-000123' and star['path_offset_arcsec'] == pytest.approx(3.0, abs=0.5)
    assert asteroid.occulted_star(PATH, None) is None
    asked = []

    def fake_path(body, jd0, jd1, site=None, points=5):
        asked.append((body.horizons_id, jd0, jd1, site))
        return '476 Hedwig (A901 QB)', PATH
    monkeypatch.setattr(asteroid, 'path', fake_path)
    from astropy.wcs import WCS
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = (320.70, -1.75)
    w.wcs.crpix = (400, 300)
    w.wcs.cd = [[-1.2 / 3600, 0], [0, 1.2 / 3600]]
    site = {'lat': 40.9, 'lon': -72.9, 'alt_m': 79}
    st = identify.find_star('(476) Hedwig', w, (620, 816), Cat(), 2026.7, simbad=False,
                            times=(PATH[0][0], PATH[-1][0]), site=site)
    assert st['name'] == 'UCAC4 441-000123' and st['asteroid'] == '(476) Hedwig'
    assert asked == [('476;', PATH[0][0], PATH[-1][0], site)]
    st = identify.find_star('(476) Hedwig', w, (620, 816), None, 2026.7, simbad=False,
                            times=(PATH[0][0], PATH[-1][0]))
    assert st['name'] == '(476) Hedwig' and (st['ra'], st['dec']) == PATH[2][1:]    # no catalogue
    logged = []
    assert identify.find_star('(476) Hedwig', w, (620, 816), None, None, simbad=False,
                              log=logged.append) is None and 'no time stamps' in logged[0]
