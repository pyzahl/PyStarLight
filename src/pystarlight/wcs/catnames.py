"""Catalogue names for marked stars (UCAC4, else Tycho-2, else Gaia DR3) and target look-up.

Occultation predictions name stars by UCAC4 or TYC (Occult, OWC) or by their Gaia DR3 source
id. A star at a measured position gets the first of those that exists: from PySyzygos' local
Gaia DR3 catalogue (localcat, with its xref part) when there is one, else online from the VO:
UCAC4 and Gaia DR3 at GAVO (Heidelberg), Gaia DR3 also at ESA. Tycho-2 names come from the
local catalogue only (UCAC4 holds the Tycho-2 stars too).

A target is typed as 'Gaia DR3 <source id>' (or the bare id), 'UCAC4 zzz-nnnnnn', or RA and Dec
('21:24:59.4 +36:40:45', '21h24m59.4s +36d40m45s' or degrees). Catalogue positions are moved
to the observation epoch with their proper motions.

NEW CODE, not from PyMovie.
"""

from __future__ import annotations

import csv
import io
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

TAP_SERVICES = ('https://dc.zah.uni-heidelberg.de/tap/sync',          # GAVO: gaia.dr3lite, ucac4.main
                'https://gea.esac.esa.int/tap-server/tap/sync')    # ESA: gaiadr3.gaia_source, public.tycho2
VIZIER_TAP = 'https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync'     # Tycho-2 when ESA is busy
GAIA_TABLES = {TAP_SERVICES[0]: 'gaia.dr3lite', TAP_SERVICES[1]: 'gaiadr3.gaia_source'}
TAP_TIMEOUT_S = 20.0
GAIA_EPOCH, UCAC4_EPOCH, TYC_EPOCH = 2016.0, 2000.0, 2000.0
SAME_STAR_ARCSEC = 1.5      # two catalogue positions this close (at the epoch) are one star


class LookupError_(RuntimeError):
    """The target could not be found (bad text, not in the catalogue, no network)."""


def decimal_year(when=None) -> float:
    """ISO time string, datetime or None (now) -> decimal year."""
    if when is None:
        t = datetime.now(timezone.utc)
    elif isinstance(when, str):
        t = datetime.fromisoformat(when.replace('Z', '+00:00'))
    else:
        t = when
    return t.year + (t.timetuple().tm_yday - 0.5) / 365.25


def tap_query(adql: str, service: str = TAP_SERVICES[0], timeout: float = TAP_TIMEOUT_S) -> list[dict]:
    """Synchronous ADQL query -> rows as dicts of strings."""
    data = urllib.parse.urlencode({'REQUEST': 'doQuery', 'LANG': 'ADQL', 'FORMAT': 'csv',
                                   'QUERY': adql}).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(service, data=data),
                                        timeout=timeout) as r:
                text = r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:         # ESA answers 503 "overloaded" now and then
            if e.code != 503 or attempt == 2:
                raise
            time.sleep(2.0 * (attempt + 1))
            continue
        if not text.lstrip().startswith('<'):
            return list(csv.DictReader(io.StringIO(text)))
        msg = re.sub(r'<[^>]+>|\s+', ' ', text)
        if 'overloaded' not in msg or attempt == 2:  # an error page
            raise RuntimeError(f'{service}: ' + msg[:200].strip())
        time.sleep(2.0 * (attempt + 1))


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float('nan')


def _move(ra, dec, pmra_mas, pmdec_mas, years):
    """Position after `years` of proper motion (pmra includes cos dec), deg."""
    pmra, pmdec = (0.0 if not math.isfinite(v) else v for v in (pmra_mas, pmdec_mas))
    dec2 = dec + pmdec * years / 3.6e6
    return ra + pmra * years / 3.6e6 / math.cos(math.radians(dec2)), dec2


def _sep(ra1, dec1, ra2, dec2) -> float:
    """Small-angle separation, arcsec."""
    dra = ((ra2 - ra1 + 180.0) % 360.0 - 180.0) * math.cos(math.radians(dec1))
    return math.hypot(dra, dec2 - dec1) * 3600.0


def _pick(stars: list[dict], nearest: bool) -> Optional[dict]:
    """The star meant: the nearest, or (a measured centre near a bright star is that star, not
    a faint neighbour) the nearest of those within 2 mag of the brightest."""
    if not stars:
        return None
    stars = sorted(stars, key=lambda s: s['sep_arcsec'])
    if nearest:
        return stars[0]
    mags = [s['mag'] for s in stars if math.isfinite(s['mag'])]
    if not mags:
        return stars[0]
    bright = min(mags)
    return next(s for s in stars if math.isfinite(s['mag']) and s['mag'] <= bright + 2.0)


def _ucac4_rows(rows, epoch) -> list[dict]:
    out = []
    for r in rows:
        # GAVO's ucac4.main: positions at J2000, proper motions in deg/yr
        ra, dec = _move(_f(r['raj2000']), _f(r['dej2000']), _f(r['pmra']) * 3.6e6,
                        _f(r['pmde']) * 3.6e6, epoch - UCAC4_EPOCH)
        uid = r['ucacid'].replace('UCAC4-', '')
        mag = _f(r['magm'])
        out.append({'name': f'UCAC4 {uid}', 'ids': {'UCAC4': f'UCAC4 {uid}'}, 'ra': ra, 'dec': dec,
                    'mag': mag, 'band': 'UCAC', 'source': 'UCAC4 (GAVO)'})
    return out


def _gaia_rows(rows, epoch, source) -> list[dict]:
    out = []
    for r in rows:
        ra, dec = _move(_f(r['ra']), _f(r['dec']), _f(r['pmra']), _f(r['pmdec']), epoch - GAIA_EPOCH)
        name = f"Gaia DR3 {r['source_id']}"
        out.append({'name': name, 'ids': {'Gaia DR3': name}, 'ra': ra, 'dec': dec,
                    'mag': _f(r['phot_g_mean_mag']), 'band': 'G', 'source': source})
    return out


def _tyc_rows(rows, epoch, source) -> list[dict]:
    """ESA public.tycho2 or VizieR I/259/tyc2 rows (mean J2000 position and proper motion when
    Tycho-2 has them, else the observed position, ~1991)."""
    out = []
    for r in rows:
        tid = r.get('id') or f"{r['TYC1']}-{r['TYC2']}-{r['TYC3']}"
        ra0, dec0 = _f(r.get('ra_mdeg', r.get('RAmdeg'))), _f(r.get('de_mdeg', r.get('DEmdeg')))
        if math.isfinite(ra0) and math.isfinite(dec0):
            ra, dec = _move(ra0, dec0, _f(r.get('pm_ra', r.get('pmRA'))),
                            _f(r.get('pm_de', r.get('pmDE'))), epoch - TYC_EPOCH)
        else:
            ra, dec = _f(r.get('ra', r.get('RA_ICRS_'))), _f(r.get('dec', r.get('DE_ICRS_')))
        out.append({'name': f'TYC {tid}', 'ids': {'TYC': f'TYC {tid}'}, 'ra': ra, 'dec': dec,
                    'mag': _f(r.get('vt_mag', r.get('VTmag'))), 'band': 'VT', 'source': source})
    return out


def _tycho(where_esa: str, where_vizier: str, epoch) -> list[dict]:
    """Tycho-2 rows at ESA, else VizieR."""
    try:
        rows = tap_query('SELECT id, ra, dec, ra_mdeg, de_mdeg, pm_ra, pm_de, vt_mag FROM '
                         f'public.tycho2 WHERE {where_esa}', TAP_SERVICES[1])
        return _tyc_rows(rows, epoch, 'Tycho-2 (ESA)')
    except Exception as e:
        err = e
    try:
        rows = tap_query('SELECT TYC1, TYC2, TYC3, RAmdeg, DEmdeg, pmRA, pmDE, VTmag, '
                         '"RA(ICRS)" AS RA_ICRS_, "DE(ICRS)" AS DE_ICRS_ FROM "I/259/tyc2" '
                         f'WHERE {where_vizier}', VIZIER_TAP)
        return _tyc_rows(rows, epoch, 'Tycho-2 (VizieR)')
    except Exception as e:
        raise RuntimeError(f'Tycho-2: {err}; {e}') from None


def _cone_tyc(ra, dec, radius_arcsec, epoch) -> list[dict]:
    r = (radius_arcsec + 2.0) / 3600.0
    return _tycho(f'1=CONTAINS(POINT(ra, dec), CIRCLE({ra:.7f}, {dec:.7f}, {r:.7f}))',
                  f'1=CONTAINS(POINT("RA(ICRS)", "DE(ICRS)"), CIRCLE({ra:.7f}, {dec:.7f}, {r:.7f}))',
                  epoch)


def _cone_ucac4(ra, dec, radius_arcsec, epoch) -> list[dict]:
    r = (radius_arcsec + 2.0) / 3600.0              # UCAC4 moves up to a few " since 2000
    rows = tap_query('SELECT ucacid, raj2000, dej2000, pmra, pmde, magm FROM ucac4.main WHERE '
                     f'1=CONTAINS(POINT(raj2000, dej2000), CIRCLE({ra:.7f}, {dec:.7f}, {r:.7f}))')
    return _ucac4_rows(rows, epoch)


def _cone_gaia(ra, dec, radius_arcsec, epoch) -> list[dict]:
    r = (radius_arcsec + 1.0) / 3600.0
    err = None
    for service in TAP_SERVICES:
        try:
            rows = tap_query('SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag FROM '
                             f'{GAIA_TABLES[service]} WHERE 1=CONTAINS(POINT(ra, dec), '
                             f'CIRCLE({ra:.7f}, {dec:.7f}, {r:.7f}))', service)
            return _gaia_rows(rows, epoch, 'Gaia DR3 (' + service.split('/')[2] + ')')
        except Exception as e:
            err = e
    raise err


def _within(stars, ra, dec, radius_arcsec):
    for s in stars:
        s['sep_arcsec'] = _sep(ra, dec, s['ra'], s['dec'])
    return [s for s in stars if s['sep_arcsec'] <= radius_arcsec]


def _local(catalog):
    if catalog != 'auto':
        return catalog
    try:
        from .localcat import local_catalog
        return local_catalog()
    except Exception as e:          # an unreadable catalogue: names from the VO instead
        print(f'local star catalogue unavailable: {e}')
        return None


def catalog_name(ra: float, dec: float, radius_arcsec: float = 5.0, epoch: Optional[float] = None,
                 nearest: bool = False, catalog='auto', online: bool = True,
                 log=print) -> Optional[dict]:
    """Name of the star at (ra, dec): {'name', 'ids', 'ra', 'dec', 'mag', 'band', 'sep_arcsec',
    'source'} or None. name: UCAC4, else TYC, else Gaia DR3. nearest: the nearest star in the
    radius (a catalogue position), else the brightest-ish (a measured centre, see _pick)."""
    epoch = decimal_year() if epoch is None else epoch
    cat = _local(catalog)
    gaia = None
    if cat is not None:
        stars = cat.cone(ra, dec, radius_arcsec, epoch)
        if stars:
            s = _pick([{**x, 'mag': x['g'] if x['g'] is not None else float('nan')} for x in stars],
                      nearest)
            from .localcat import tyc_name, ucac4_name
            ids = {'Gaia DR3': f"Gaia DR3 {s['source_id']}"}
            if s['tyc']:
                ids['TYC'] = tyc_name(s['tyc'])
            if s['ucac4']:
                ids['UCAC4'] = ucac4_name(s['ucac4'])
            gaia = {'name': ids['Gaia DR3'], 'ids': ids, 'ra': s['ra'], 'dec': s['dec'],
                    'mag': s['mag'], 'band': 'G', 'sep_arcsec': s['sep_arcsec'],
                    'source': 'Gaia DR3 (local)'}
            name = ids.get('UCAC4') or ids.get('TYC')
            if name or cat.xref is not None:        # with the xref part: no UCAC4 / TYC there
                gaia['name'] = name or gaia['name']
                return gaia
    if not online:
        return gaia
    try:
        hit = _pick(_within(_cone_ucac4(ra, dec, radius_arcsec, epoch), ra, dec, radius_arcsec),
                    nearest)
        if hit:
            if gaia:
                hit['ids'] = {**gaia['ids'], **hit['ids']}
            return hit
    except Exception as e:
        log(f'UCAC4 look-up failed: {e}')
    try:
        hit = _pick(_within(_cone_tyc(ra, dec, radius_arcsec, epoch), ra, dec, radius_arcsec), nearest)
        if hit:
            if gaia:
                hit['ids'] = {**gaia['ids'], **hit['ids']}
            return hit
    except Exception as e:
        log(f'Tycho-2 look-up failed: {e}')
    if gaia:
        return gaia
    try:
        return _pick(_within(_cone_gaia(ra, dec, radius_arcsec, epoch), ra, dec, radius_arcsec),
                     nearest)
    except Exception as e:
        log(f'Gaia DR3 look-up failed: {e}')
    return None


def parse_target(text: str):
    """'Gaia DR3 123' / 'DR3 123' / '123' -> ('gaia', 123); 'UCAC4 634-113397' ->
    ('ucac4', '634-113397'); 'TYC 3185-1234-1' -> ('tyc', (3185, 1234, 1)); RA Dec ->
    ('radec', ra_deg, dec_deg). ValueError otherwise."""
    t = text.strip()
    m = re.fullmatch(r'(?:gaia\s*)?dr3\s*(\d{1,20})|(\d{6,20})', t, re.I)   # bare: a long number
    if m:
        return 'gaia', int(m.group(1) or m.group(2))
    m = re.fullmatch(r'ucac4[\s-]*(\d{3})-(\d{1,6})', t, re.I)
    if m:
        return 'ucac4', f'{int(m.group(1)):03d}-{int(m.group(2)):06d}'
    m = re.fullmatch(r'tyc(?:ho)?(?:-?2)?[\s-]*(\d{1,4})-(\d{1,5})-(\d)', t, re.I)
    if m:
        return 'tyc', tuple(int(g) for g in m.groups())
    parts = t.replace(',', ' ').split()
    if len(parts) == 2:
        try:
            return 'radec', float(parts[0]) % 360.0, float(parts[1])
        except ValueError:
            pass
    if len(parts) in (2, 6):
        import astropy.units as u
        from astropy.coordinates import SkyCoord
        try:
            c = SkyCoord(t, unit=(u.hourangle, u.deg))
            return 'radec', float(c.ra.deg), float(c.dec.deg)
        except Exception:
            pass
    raise ValueError(f"'{text}': give 'Gaia DR3 <id>', 'UCAC4 zzz-nnnnnn', 'TYC n-n-n' or RA Dec")


def cross_ids(star: dict, epoch: float, catalog='auto', log=print) -> dict:
    """All of a star's IDs (UCAC4, TYC, Gaia DR3): the catalogue entries nearest to its
    position within SAME_STAR_ARCSEC, with a magnitude within 1.5 of its own (when both are
    known). Look-ups that fail are left out (and logged)."""
    ids = dict(star.get('ids') or {})
    ra, dec, mag = star['ra'], star['dec'], star.get('mag', float('nan'))
    cat = _local(catalog)
    if cat is not None and not {'UCAC4', 'TYC', 'Gaia DR3'} <= ids.keys():
        hit = catalog_name(ra, dec, SAME_STAR_ARCSEC, epoch, nearest=True, catalog=cat,
                           online=False, log=log)
        if hit:
            ids = {**hit['ids'], **ids}
    for key, cone in (('UCAC4', _cone_ucac4), ('TYC', _cone_tyc), ('Gaia DR3', _cone_gaia)):
        if key in ids or (key == 'TYC' and math.isfinite(mag) and mag > 13.0):   # Tycho-2: V < ~12.5
            continue
        try:
            near = _within(cone(ra, dec, SAME_STAR_ARCSEC, epoch), ra, dec, SAME_STAR_ARCSEC)
        except Exception as e:
            log(f'{key} look-up failed: {e}')
            continue
        near = [n for n in near if not (math.isfinite(mag) and math.isfinite(n['mag'])
                                        and abs(n['mag'] - mag) > 1.5)]
        if near:
            ids[key] = _pick(near, nearest=True)['name']
    return ids


def verify(a: dict, b: dict) -> dict:
    """Are two resolved targets the same star? {'same', 'sep_arcsec', 'dmag', 'text'}."""
    sep = _sep(a['ra'], a['dec'], b['ra'], b['dec'])
    shared = [k for k in a['ids'] if k in b['ids'] and a['ids'][k] == b['ids'][k]]
    clash = [k for k in a['ids'] if k in b['ids'] and a['ids'][k] != b['ids'][k]]
    same = sep <= SAME_STAR_ARCSEC and not clash
    dmag = (a['mag'] - b['mag']) if a.get('band') == b.get('band') else float('nan')
    text = (f"{'SAME STAR' if same else 'DIFFERENT STARS'}: {a['query']} / {b['query']} "
            f"{sep:.2f}\" apart" + (f", same {', '.join(shared)}" if shared else '')
            + (f", different {', '.join(clash)}" if clash else ''))
    return {'same': same, 'sep_arcsec': sep, 'dmag': dmag, 'text': text}


def resolve_target(text: str, epoch: Optional[float] = None, catalog='auto', log=print) -> dict:
    """The target's position at `epoch` and its name (UCAC4, else TYC, else Gaia DR3):
    {'name', 'ids', 'ra', 'dec', 'mag', 'band', 'source', 'query'}. Raises LookupError_."""
    epoch = decimal_year() if epoch is None else epoch
    try:
        kind, *v = parse_target(text)
    except ValueError as e:
        raise LookupError_(str(e)) from None
    if kind == 'gaia':
        rows, err = [], None
        for service in TAP_SERVICES:
            try:
                rows = tap_query('SELECT source_id, ra, dec, pmra, pmdec, phot_g_mean_mag FROM '
                                 f'{GAIA_TABLES[service]} WHERE source_id = {v[0]}', service)
                break
            except Exception as e:
                err = e
        if not rows:
            raise LookupError_(f'Gaia DR3 {v[0]}: ' + (f'not found ({err})' if err else 'no such source'))
        star = _gaia_rows(rows, epoch, 'Gaia DR3')[0]
    elif kind == 'ucac4':
        try:
            rows = tap_query('SELECT ucacid, raj2000, dej2000, pmra, pmde, magm FROM ucac4.main '
                             f"WHERE ucacid = 'UCAC4-{v[0]}'")
        except Exception as e:
            raise LookupError_(f'UCAC4 {v[0]}: {e}') from None
        if not rows:
            raise LookupError_(f'UCAC4 {v[0]}: no such star')
        star = _ucac4_rows(rows, epoch)[0]
    elif kind == 'tyc':
        t1, t2, t3 = v[0]
        try:
            rows = _tycho(f"id = '{t1}-{t2}-{t3}'", f'TYC1 = {t1} AND TYC2 = {t2} AND TYC3 = {t3}',
                          epoch)
        except Exception as e:
            raise LookupError_(f'TYC {t1}-{t2}-{t3}: {e}') from None
        if not rows:
            raise LookupError_(f'TYC {t1}-{t2}-{t3}: no such star')
        star = rows[0]
    else:
        star = {'name': '', 'ids': {}, 'ra': v[0], 'dec': v[1], 'mag': float('nan'), 'band': '',
                'source': 'position'}
    if kind == 'radec':     # the star at a typed position
        hit = catalog_name(star['ra'], star['dec'], 2.0, epoch, nearest=True, catalog=catalog, log=log)
        if hit:
            star = {**hit, 'source': hit['source']}
    star['ids'] = cross_ids(star, epoch, catalog, log)
    ids = star['ids']
    star['name'] = ids.get('UCAC4') or ids.get('TYC') or ids.get('Gaia DR3') or star['name'] \
        or text.strip()
    star['query'] = text.strip()
    return star
