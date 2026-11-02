"""Target parsing and star choice of wcs.catnames (offline: no VO queries)."""

import pytest

from pystarlight.wcs import catnames
from pystarlight.wcs.catnames import _pick, catalog_name, parse_target, resolve_target, verify


def test_parse_target_forms():
    assert parse_target('Gaia DR3 1963716673315870848') == ('gaia', 1963716673315870848)
    assert parse_target('DR3 1963716673315870848') == ('gaia', 1963716673315870848)
    assert parse_target('1963716673315870848') == ('gaia', 1963716673315870848)
    assert parse_target('UCAC4-634-113397') == ('ucac4', '634-113397')
    assert parse_target('ucac4 634-1') == ('ucac4', '634-000001')
    assert parse_target('TYC 3185-1234-1') == ('tyc', (3185, 1234, 1))
    assert parse_target('tyc3185-12-2') == ('tyc', (3185, 12, 2))
    for text in ('21:24:59.4 +36:40:45', '21h24m59.4s +36d40m45s', '21 24 59.4 +36 40 45'):
        kind, ra, dec = parse_target(text)
        assert kind == 'radec'
        assert ra == pytest.approx(321.2475, abs=1e-4) and dec == pytest.approx(36.67917, abs=1e-4)
    assert parse_target('321.2475, 36.6792') == ('radec', 321.2475, 36.6792)
    with pytest.raises(ValueError):
        parse_target('Deneb')


def test_pick_prefers_bright_star_over_faint_neighbour():
    stars = [{'name': 'faint', 'mag': 15.0, 'sep_arcsec': 1.0},
             {'name': 'bright', 'mag': 7.0, 'sep_arcsec': 3.0}]
    assert _pick(stars, nearest=False)['name'] == 'bright'
    assert _pick(stars, nearest=True)['name'] == 'faint'
    assert _pick([], nearest=False) is None


def test_target_named_by_ucac4(monkeypatch):
    def tap(adql, service=catnames.TAP_SERVICES[0], timeout=0):
        if 'source_id =' in adql:
            return [{'source_id': '1963716673315870848', 'ra': '321.24744083508233',
                     'dec': '36.679111000755476', 'pmra': '-2.699', 'pmdec': '-0.577',
                     'phot_g_mean_mag': '11.1957'}]
        if 'tycho2' in adql or 'tyc2' in adql:
            return []
        if 'ucac4.main' in adql:
            return [{'ucacid': 'UCAC4-634-113397', 'raj2000': '321.2474658', 'dej2000': '36.6791219',
                     'pmra': '-1.4166667e-06', 'pmde': '-1.388889e-07', 'magm': '11.143'}]
        raise AssertionError(adql)
    monkeypatch.setattr(catnames, 'tap_query', tap)
    t = resolve_target('Gaia DR3 1963716673315870848', 2026.835, catalog=None)
    assert t['name'] == 'UCAC4 634-113397'
    assert t['ids']['Gaia DR3'] == 'Gaia DR3 1963716673315870848'
    assert t['mag'] == pytest.approx(11.1957) and t['band'] == 'G'
    assert t['ra'] == pytest.approx(321.24743, abs=1e-5)       # proper motion to 2026.8


def test_falls_back_to_gaia_name(monkeypatch):
    def tap(adql, service=catnames.TAP_SERVICES[0], timeout=0):
        if 'ucac4.main' in adql or 'tycho2' in adql or 'tyc2' in adql:
            return []
        return [{'source_id': '42', 'ra': '10.0', 'dec': '20.0', 'pmra': '', 'pmdec': '',
                 'phot_g_mean_mag': '16.5'}]
    monkeypatch.setattr(catnames, 'tap_query', tap)
    assert catalog_name(10.0, 20.0, 5.0, 2026.0, catalog=None)['name'] == 'Gaia DR3 42'


def test_tyc_target_and_verify(monkeypatch):
    def tap(adql, service=catnames.TAP_SERVICES[0], timeout=0):
        if 'public.tycho2' in adql:
            assert "id = '2717-1234-1'" in adql or 'CONTAINS' in adql
            return [{'id': '2717-1234-1', 'ra': '321.0', 'dec': '36.5', 'ra_mdeg': '321.0',
                     'de_mdeg': '36.5', 'pm_ra': '', 'pm_de': '', 'vt_mag': '10.4'}]
        if 'ucac4.main' in adql:
            return [{'ucacid': 'UCAC4-633-111111', 'raj2000': '321.0', 'dej2000': '36.5000001',
                     'pmra': '0', 'pmde': '0', 'magm': '10.3'}]
        return [{'source_id': '7', 'ra': '321.0', 'dec': '36.5', 'pmra': '', 'pmdec': '',
                 'phot_g_mean_mag': '10.2'}]
    monkeypatch.setattr(catnames, 'tap_query', tap)
    a = resolve_target('TYC 2717-1234-1', 2026.8, catalog=None)
    assert a['name'] == 'UCAC4 633-111111'          # UCAC4 first, then TYC, then Gaia DR3
    assert a['ids'] == {'TYC': 'TYC 2717-1234-1', 'UCAC4': 'UCAC4 633-111111',
                        'Gaia DR3': 'Gaia DR3 7'}
    b = resolve_target('Gaia DR3 7', 2026.8, catalog=None)
    v = verify(a, b)
    assert v['same'] and v['sep_arcsec'] < 0.01 and 'SAME STAR' in v['text']
    far = {**b, 'ra': 321.01, 'ids': {'Gaia DR3': 'Gaia DR3 8'}, 'query': 'Gaia DR3 8'}
    assert not verify(a, far)['same']
