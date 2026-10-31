"""Offline star names from a (tiny, made-up) PySyzygos format-2 catalogue."""

import json

import numpy as np
import pytest

from pystarlight.wcs import identify as ident
from pystarlight.wcs import localcat as lc


def write_catalog(d, stars, xref=True, build='b1'):
    """stars: (source_id, ra, dec, pmra, pmdec, G, ucac4, tyc, hip), all in one or more cells."""
    d.mkdir()
    cells = [int(lc.N_RA * np.floor((dec + 90) / lc.CELL) + np.floor(ra / lc.CELL))
             for _, ra, dec, *_ in stars]
    order = np.lexsort(([s[5] for s in stars], cells))
    core = np.zeros(len(stars), lc.CORE)
    xr = np.zeros(len(stars), lc.XREF)
    for k, i in enumerate(order):
        sid, ra, dec, pmra, pmdec, g, u4, tyc, hip = stars[i]
        c = cells[i]
        dra = round((ra - (c % lc.N_RA) * lc.CELL) / lc.SCALE)
        ddec = round((dec + 90 - (c // lc.N_RA) * lc.CELL) / lc.SCALE)
        core[k] = (sid, dra, ddec, 1.0, pmra, pmdec, round(g * 1000), 0)
        xr[k] = (u4, tyc, hip)
    counts = np.bincount(np.array(cells)[order], minlength=lc.N_RA * lc.N_DEC)
    np.save(d / 'offsets.npy', np.concatenate([[0], np.cumsum(counts)]).astype(np.int64))
    np.save(d / 'core.npy', core)
    man = {'format': 2, 'gmax': 16.0, 'cell_deg': 0.25, 'n_stars': len(stars), 'build_id': build}
    (d / 'catalog.json').write_text(json.dumps(man))
    if xref:
        np.save(d / 'xref.npy', xr)
        (d / 'xref.json').write_text(json.dumps({'group': 'xref', 'build_id': build, 'n_stars': len(stars)}))
    return d


TYC = (3574 * 20000 + 1718) * 4 + 1          # TYC 3574-1718-1
U4 = 677 * 1_000_000 + 85205                 # UCAC4 677-085205
STARS = [(1, 310.36, 45.10, 0.0, 0.0, 9.5, U4, TYC, 0),             # target star
         (2, 310.3605, 45.1003, 0.0, 0.0, 15.8, 0, 0, 0),           # faint, nearer to a sloppy centre
         (3, 310.40, 45.12, 0.0, 0.0, 12.0, U4 + 1, 0, 0),          # UCAC4 only
         (4, 310.45, 45.05, 0.0, 0.0, 13.0, 0, 0, 0),               # Gaia only
         (5, 310.30, 45.00, 0.0, 3600.0, 11.0, 0, 0, 0)]            # 3.6"/yr north: 36" off by 2026


def test_names_tyc_then_ucac4_then_gaia(tmp_path):
    cat = lc.LocalGaia(str(write_catalog(tmp_path / 'cat', STARS)))
    hit = cat.lookup(310.3604, 45.1002, 10.0)               # nearer the faint one: the bright one wins
    assert hit['name'] == 'TYC 3574-1718-1' and hit['ids']['UCAC4'] == 'UCAC4 677-085205'
    assert hit['ids']['Gaia DR3'] == 'Gaia DR3 1' and hit['gmag'] == 9.5
    assert cat.lookup(310.40, 45.12)['name'] == 'UCAC4 677-085206'
    assert cat.lookup(310.45, 45.05)['name'] == 'Gaia DR3 4'
    assert cat.lookup(310.30, 45.00, epoch=2026.0) is None                  # moved away ...
    assert cat.lookup(310.30, 45.01, epoch=2026.0)['name'] == 'Gaia DR3 5'  # ... to here
    assert cat.lookup(20.0, -30.0) is None


def test_found_next_to_pysyzygos_sites(tmp_path):
    home = tmp_path / 'PyOccult'
    home.mkdir()
    (home / 'sites.py').write_text('sites = {}\n')
    write_catalog(home / 'gaia_dr3_g18', STARS, xref=False)
    write_catalog(home / 'gaia_dr3_g16.new', STARS)
    (home / 'gaia_dr3_g16').mkdir()                          # old format: ignored
    (home / 'gaia_dr3_g16' / 'catalog.json').write_text('{"gmax": 16}')
    cfg = {'pysyzygos_sites': str(home / 'sites.py'), 'catalog': {'gaia_dir': ''}}
    assert lc.find_catalog(cfg) == str(home / 'gaia_dr3_g16.new')          # the one with names
    assert lc.find_catalog({**cfg, 'catalog': {'gaia_dir': str(home / 'gaia_dr3_g18')}}).endswith('g18')
    assert lc.find_catalog({'catalog': {'gaia_dir': ''}}) is None


def test_name_star_prefers_local_names_and_skips_simbad(tmp_path, monkeypatch):
    from astropy.wcs import WCS
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval, w.wcs.crpix = (310.36, 45.10), (1, 1)
    w.wcs.cd = [[-2 / 3600, 0], [0, 2 / 3600]]
    cat = lc.LocalGaia(str(write_catalog(tmp_path / 'cat', STARS)))
    monkeypatch.setattr(ident, 'simbad_lookup', lambda *a, **k: pytest.fail('SIMBAD asked'))
    hd = [{'names': ['HD 197161'], 'x': 0.0, 'y': 0.0, 'type': 'hd', 'vmag': None}]
    s = ident.name_star(ident.FieldStar(0.0, 0.0, 1, 3, False), w, hd, catalog=cat)
    assert s.name == 'TYC 3574-1718-1' and s.source == 'Gaia DR3 (local)' and s.label().endswith('G=9.50')
    assert s.ids['astrometry.net'] == 'HD 197161'
    deneb = [{'names': ['α Cyg', 'Deneb'], 'x': 0.0, 'y': 0.0, 'type': 'bright', 'vmag': 1.25}]
    s = ident.name_star(ident.FieldStar(0.0, 0.0, 1, 3, False), w, deneb, catalog=cat)
    assert s.name == 'Deneb (α Cyg)' and s.ids['TYC'] == 'TYC 3574-1718-1'    # proper names stay
    monkeypatch.setattr(ident, 'simbad_lookup', lambda *a, **k: None)
    s = ident.name_star(ident.FieldStar(500.0, 500.0, 1, 3, False), w, [], catalog=cat)
    assert s.name == ''                                       # not in the catalogue: SIMBAD's turn
