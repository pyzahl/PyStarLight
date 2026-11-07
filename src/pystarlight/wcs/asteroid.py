"""An asteroid as the target: the star it occults, found from its path during the recording.

    parse_asteroid('(476) Hedwig')     -> Body('476;', None, '(476) Hedwig')
    path(body, jds, site)              -> [(jd, ra, dec)]: JPL Horizons, astrometric (ICRF),
                                          topocentric when the site is known
    occulted_star(points, catalog, ep) -> the catalogue star closest to the path

Names taken: '(476) Hedwig', '(476)', '476 Hedwig', '476-Hedwig', a provisional designation
'2003 SB60', '(158772) 2003 SB60' / '158772-2003-SB60' (as the camera writes file names).
A bare name ('Hedwig') is asked from Horizons only after SIMBAD found no star of that name
(body_by_name).

Horizons needs the network (astroquery, extra "catalog"); the star comes from the local Gaia
catalogue when there is one (wcs.localcat).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

PATH_POINTS = 5          # positions over the recording (its motion is a straight line by then)
MAX_OFFSET_ARCSEC = 15.0  # a star farther than this from the path is not the occulted one


@dataclass
class Body:
    horizons_id: str             # '476;' (record number) or a name / designation
    id_type: Optional[str]       # Horizons id_type: None for a number, 'smallbody' else
    label: str                   # '(476) Hedwig' (Horizons' name once asked)


def parse_asteroid(text: str) -> Optional[Body]:
    """The asteroid named in `text`, or None (a star name, RA Dec or anything else)."""
    t = ' '.join(text.replace('_', ' ').split())
    m = re.fullmatch(r'(\d{4})[\s-]*([A-Z]{2}\d{0,4})', t)                 # 2003 SB60
    if m:
        des = f'{m.group(1)} {m.group(2)}'
        return Body(des, 'smallbody', des)
    m = (re.fullmatch(r'\((\d{1,7})\)(?:\s+(.+))?', t)                   # (476) Hedwig
         or re.fullmatch(r'(\d{1,7})[\s-]+([A-Za-z0-9-]*[A-Za-z].*)', t))   # 476 Hedwig
    if m and not re.search(r'[+:]', m.group(2) or ''):              # not RA Dec
        number, rest = int(m.group(1)), ' '.join((m.group(2) or '').replace('-', ' ').split())
        return Body(f'{number};', None, f'({number}) {rest}'.strip())
    return None


def label_of(targetname: str, fallback: str) -> str:
    """Horizons' '476 Hedwig (A901 QB)' -> '(476) Hedwig'; '158772 (2003 SB60)' ->
    '(158772) 2003 SB60'; else the fallback."""
    m = re.fullmatch(r'(\d+) ([^(]+?) \(.*\)', targetname.strip())
    if m:
        return f'({m.group(1)}) {m.group(2)}'
    m = re.fullmatch(r'(\d+) \((.+)\)', targetname.strip())
    return f'({m.group(1)}) {m.group(2)}' if m else fallback


def target(body: Body, catalog, epoch: Optional[float], times: Optional[tuple[float, float]],
           site: Optional[dict] = None, log=print) -> Optional[dict]:
    """The star field's entry for an asteroid: the occulted star (occulted_star) named as in the
    catalogue, with 'asteroid' and 'path_offset_arcsec'; else the asteroid's own position in the
    middle of the recording. None (and a log line) without recording times or Horizons."""
    if times is None:
        log(f'{body.label}: the recording has no time stamps for its position')
        return None
    try:
        name, pts = path(body, *times, site)
    except Exception as e:                  # unknown body, ambiguous name, no network, no astroquery
        log(f'JPL Horizons, {body.label}: {str(e).splitlines()[0][:160] if str(e) else type(e).__name__}')
        return None
    label = label_of(name, body.label)
    star = occulted_star(pts, catalog, epoch)
    if star is not None:
        return {**star, 'vmag': None, 'asteroid': label,
                'source': f'Gaia DR3 (local), {star["path_offset_arcsec"]:.1f}" from the path of {label}'}
    _, ra, dec = pts[len(pts) // 2]
    return {'name': label, 'ids': {}, 'gmag': None, 'vmag': None, 'ra': ra, 'dec': dec, 'asteroid': label,
            'source': 'JPL Horizons: the asteroid (no catalogue star on its path)'}


def body_by_name(text: str) -> Body:
    """A bare asteroid name for Horizons ('Hedwig'), tried after SIMBAD knew no such star."""
    return Body(text.strip(), 'smallbody', text.strip())


def julian_day(date: str, time: str) -> Optional[float]:
    """'YYYY-MM-DD', 'HH:MM:SS.ffffff' (UTC) -> Julian day; None if unreadable."""
    try:
        d = datetime.strptime(date[:10], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        h, mi, s = time.split(':')
    except (ValueError, TypeError, AttributeError):
        return None
    secs = d.timestamp() + int(h) * 3600 + int(mi) * 60 + float(s)
    return secs / 86400.0 + 2440587.5


def path(body: Body, jd_first: float, jd_last: float, site: Optional[dict] = None,
         points: int = PATH_POINTS) -> tuple[str, list[tuple[float, float, float]]]:
    """(Horizons' name of the body, [(jd, ra, dec)]) over the recording. site: {'lat', 'lon' (deg
    east), 'alt_m'} for topocentric positions (parallax: a few arcsec for main-belt asteroids),
    else geocentric. Raises when Horizons does not know the body or cannot be reached."""
    from astroquery.jplhorizons import Horizons
    n = max(2, points)
    jds = [jd_first + (jd_last - jd_first) * k / (n - 1) for k in range(n)]
    loc = '500'                                 # geocentre
    if site:
        loc = {'lon': float(site['lon']), 'lat': float(site['lat']),
               'elevation': float(site.get('alt_m') or 0) / 1000}
    query = Horizons(id=body.horizons_id, id_type=body.id_type, location=loc, epochs=jds)
    eph = query.ephemerides(quantities='1')
    pts = [(float(j), float(r), float(d)) for j, r, d in zip(eph['datetime_jd'], eph['RA'], eph['DEC'])]
    return str(eph['targetname'][0]), pts


def _offset_arcsec(ra, dec, points) -> float:
    """Distance of (ra, dec) from the path through `points` (straight in the tangent plane), arcsec."""
    ra0, dec0 = points[len(points) // 2][1:]
    c = math.cos(math.radians(dec0))

    def xy(r, d):
        return ((r - ra0 + 180) % 360 - 180) * c * 3600, (d - dec0) * 3600
    px, py = xy(ra, dec)
    best = math.inf
    for (_, r1, d1), (_, r2, d2) in zip(points, points[1:]):
        (x1, y1), (x2, y2) = xy(r1, d1), xy(r2, d2)
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        u = 0.0 if L2 == 0 else min(1.0, max(0.0, ((px - x1) * dx + (py - y1) * dy) / L2))
        best = min(best, math.hypot(px - x1 - u * dx, py - y1 - u * dy))
    return best


def occulted_star(points, catalog, epoch: Optional[float] = None,
                  max_offset: float = MAX_OFFSET_ARCSEC) -> Optional[dict]:
    """The catalogue star (localcat.star_entry, with 'path_offset_arcsec') closest to the
    asteroid's path, within max_offset; None if there is none (or no catalogue)."""
    if catalog is None or not points:
        return None
    from .localcat import star_entry
    _, ra0, dec0 = points[len(points) // 2]
    c = math.cos(math.radians(dec0))
    half = max(math.hypot(((r - ra0 + 180) % 360 - 180) * c, d - dec0) * 3600 for _, r, d in points)
    best = None
    for row in catalog.cone(ra0, dec0, half + max_offset, epoch):
        off = _offset_arcsec(row['ra'], row['dec'], points)
        if off <= max_offset and (best is None or off < best[0]):
            best = (off, row)
    if best is None:
        return None
    return {**star_entry(best[1]), 'path_offset_arcsec': round(best[0], 2)}
