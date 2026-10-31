"""Name the brightest stars of a plate-solved field.

astrometry.net's annotations name only stars from its bright-star and HD lists (typically one
to three per field). So the brightest stars are found in the image itself, given the
astrometry.net name when there is one within a few pixels (proper names are kept), and looked up
by position: offline in PySyzygos' local Gaia DR3 catalogue when there is one (localcat: the
Tycho-2 or UCAC4 name, as Occult and OWC show it, and G), else online in SIMBAD (CDS): main
identifier, the usual catalogue IDs (HD, HIP, TYC, Gaia DR3) and V.

SIMBAD lookups need network access and astroquery (pip install "starlight-photometry[catalog]").
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np
from scipy.ndimage import maximum_filter, median_filter

PREFERRED_IDS = ('HD', 'HIP', 'TYC', 'Gaia DR3', 'UCAC4', '2MASS')
SIMBAD_TIMEOUT_S = 20.0     # per lookup (astroquery itself waits as long as the network does)
SIMBAD_RETRY_S = 300.0      # after a failed query SIMBAD is skipped this long (offline in the field)
_simbad_down_until = 0.0


def radec_text(ra: float, dec: float) -> str:
    """'hh:mm:ss.s +dd:mm:ss.s', or '' without a position."""
    if not (math.isfinite(ra) and math.isfinite(dec)):
        return ''
    from astropy.coordinates import SkyCoord
    # astropy 8 with numpy 2.5 warns "invalid value encountered in do_format" for any angle
    with np.errstate(invalid='ignore'):
        return SkyCoord(ra, dec, unit='deg').to_string('hmsdms', precision=1, sep=':')


@dataclass
class FieldStar:
    x: float
    y: float
    flux: float                      # 2 pi A sigma^2 of the Gaussian fit (instrumental)
    fwhm: float
    saturated: bool
    ra: float = float('nan')         # deg, from the plate solution
    dec: float = float('nan')
    name: str = ''                   # best name: proper name, else TYC / UCAC4 (local), else SIMBAD's
    ids: dict = field(default_factory=dict)   # catalogue -> id, e.g. {'HD': 'HD 197161'}
    vmag: Optional[float] = None
    source: str = ''                 # 'astrometry.net' | 'Gaia DR3 (local)' | 'SIMBAD' | ''
    sep_arcsec: Optional[float] = None
    gmag: Optional[float] = None     # Gaia G (local catalogue)

    @property
    def radec(self) -> str:
        return radec_text(self.ra, self.dec)

    def label(self) -> str:
        return f'{self.name or "?"}{self.mag_text()}'

    def mag_text(self) -> str:
        if self.vmag is not None:
            return f' V={self.vmag:.2f}'
        return f' G={self.gmag:.2f}' if self.gmag is not None else ''


def brightest_stars(image: np.ndarray, n: int = 3, min_sep: int = 15, border: int = 10,
                    saturation: Optional[float] = None,
                    stamp: Optional[tuple[int, int]] = None) -> list[FieldStar]:
    """The n brightest stars by fitted flux (not by peak, so wide and saturated stars rank right).
    stamp: (top, bottom) rows of a burnt-in timestamp (VTI, ADV) never searched; default:
    detected (solve.stamp_rows), else its saturated digits would be the "brightest stars"."""
    from ..globaltrack import fit_peak
    from .solve import stamp_rows
    img = np.asarray(image, dtype=np.float64)
    top, bottom = stamp if stamp is not None else stamp_rows(img)
    sm = median_filter(img, 3)
    bg = np.median(sm)
    noise = 1.4826 * np.median(np.abs(sm - bg)) or 1.0
    peaks = (sm == maximum_filter(sm, 2 * min_sep + 1)) & (sm > bg + 8 * noise)
    peaks[:top + border, :] = peaks[img.shape[0] - bottom - border:, :] = False
    peaks[:, :border] = peaks[:, -border:] = False
    ys, xs = np.nonzero(peaks)
    order = np.argsort(sm[ys, xs])[::-1][:max(20 * n, 200)]   # bright stars' halos give many peaks
    sky = img[top:img.shape[0] - bottom]
    sat = saturation if saturation is not None else (sky.max() * 0.98)
    stars = []
    for j in order:
        f = fit_peak(img, xs[j], ys[j], 3, half=10)
        if not f.ok:
            continue
        flux = 2 * math.pi * f.amp * f.sigma ** 2
        # a peak in the halo of a star already found belongs to it
        if any(math.hypot(s.x - f.x, s.y - f.y) < max(min_sep, 3 * s.fwhm) for s in stars):
            continue
        stars.append(FieldStar(f.x, f.y, flux, f.fwhm, bool(img[int(round(f.y)), int(round(f.x))] >= sat)))
    stars.sort(key=lambda s: s.flux, reverse=True)
    return stars[:n]


def astrometry_annotations(job_id: int, api_key: Optional[str] = None) -> list[dict]:
    """Named objects astrometry.net found in a solved job: [{'names', 'x', 'y', 'type', 'vmag'}]."""
    from ..config import load
    from .astrometry_client import Client
    c = Client(tracer=lambda m: None)
    c.login(api_key or load()['astrometry']['api_key'])
    out = []
    for a in c.annotate_data(int(job_id)).get('annotations', []):
        out.append({'names': a.get('names', []), 'x': a.get('pixelx'), 'y': a.get('pixely'),
                    'type': a.get('type', ''), 'vmag': a.get('vmag')})
    return out


def simbad_lookup(ra: float, dec: float, radius_arcsec: float = 10.0,
                  timeout: float = SIMBAD_TIMEOUT_S) -> Optional[dict]:
    """Nearest SIMBAD object with a V magnitude (else nearest at all) within the radius.
    Gives up after `timeout` s (TimeoutError): astroquery's network calls have no time limit."""
    box = {}

    def work():
        try:
            box['hit'] = _simbad_query(ra, dec, radius_arcsec)
        except Exception as e:
            box['error'] = e
    t = threading.Thread(target=work, daemon=True)      # left behind if SIMBAD never answers
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f'SIMBAD did not answer within {timeout:.0f} s')
    if 'error' in box:
        raise box['error']
    return box['hit']


def _simbad_query(ra: float, dec: float, radius_arcsec: float) -> Optional[dict]:
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from astroquery.simbad import Simbad
    s = Simbad()
    s.add_votable_fields('V', 'ids')
    c = SkyCoord(ra, dec, unit='deg')
    r = s.query_region(c, radius=radius_arcsec * u.arcsec)
    if r is None or len(r) == 0:
        return None
    sep = c.separation(SkyCoord(r['ra'], r['dec'], unit='deg')).arcsec
    has_v = np.array([not np.ma.is_masked(v) and np.isfinite(v) for v in r['V']])
    k = int(np.argmin(np.where(has_v, sep, sep + 1e6)))
    ids = {}
    for ident in str(r['ids'][k]).split('|'):
        ident = ident.strip()
        for cat in PREFERRED_IDS:
            if ident.startswith(cat + ' ') and cat not in ids:
                ids[cat] = ident
    v = r['V'][k]
    return {'main_id': ' '.join(str(r['main_id'][k]).split()), 'ids': ids,
            'vmag': None if np.ma.is_masked(v) else float(v), 'sep_arcsec': float(sep[k])}


def _annotations(job_id, annotations, log):
    if annotations is None and job_id is not None:
        try:
            return astrometry_annotations(job_id)
        except Exception as e:      # names are a bonus; never fail the solve for them
            log(f'astrometry.net annotations unavailable: {e}')
    return annotations or []


def _catalog(catalog):
    """'auto': PySyzygos' local catalogue from the config (or None); else as given."""
    if catalog != 'auto':
        return catalog
    try:
        from .localcat import local_catalog
        return local_catalog()
    except Exception as e:          # an unreadable catalogue: names from SIMBAD instead
        print(f'local star catalogue unavailable: {e}')
        return None


def name_star(s: FieldStar, wcs, annotations: list, use_simbad: bool = True,
              match_px: float = 5.0, log=print, radius_arcsec: float = 10.0, catalog=None) -> FieldStar:
    """Fill RA/Dec, and name/IDs/magnitude from astrometry.net annotations, the local catalogue
    (a localcat.LocalGaia; 'auto' finds it) and, if that has no star there, SIMBAD (searched
    within radius_arcsec; wide fields need more than the default, see simbad_radius)."""
    global _simbad_down_until
    catalog = _catalog(catalog)
    s.ra, s.dec = (float(v) for v in wcs.all_pix2world(s.x, s.y, 0))
    near = [a for a in annotations if a['x'] is not None and a['names']
            and math.hypot(a['x'] - s.x, a['y'] - s.y) <= match_px]
    near.sort(key=lambda a: a['type'] != 'bright')          # proper names first
    if near:
        s.name = _nice_name(near[0]['names'])
        s.ids = {'astrometry.net': ' / '.join(n for b in near for n in b['names'])}
        s.vmag = next((b['vmag'] for b in near if b.get('vmag')), None)
        s.source = 'astrometry.net'
    local = catalog.lookup(s.ra, s.dec, radius_arcsec) if catalog is not None else None
    if local:
        s.ids = {**local['ids'], **s.ids}
        s.gmag, s.sep_arcsec = local['gmag'], local['sep_arcsec']
        if not (near and near[0]['type'] == 'bright'):      # a proper name stays, HD / BD give way
            s.name, s.source = local['name'], 'Gaia DR3 (local)'
        return s
    if use_simbad and time.monotonic() < _simbad_down_until:
        log('SIMBAD skipped (unreachable a moment ago)')
    elif use_simbad:
        try:
            hit = simbad_lookup(s.ra, s.dec, radius_arcsec)
        except Exception as e:
            log(f'SIMBAD lookup failed: {e}')
            _simbad_down_until = time.monotonic() + SIMBAD_RETRY_S
            hit = None
        if hit:
            s.ids = {**hit['ids'], **s.ids}
            s.sep_arcsec = hit['sep_arcsec']
            if s.vmag is None:
                s.vmag = hit['vmag']
            if not s.name:
                s.name, s.source = hit['main_id'], 'SIMBAD'
    return s


def simbad_radius(wcs) -> float:
    """SIMBAD search radius for a solution: 10", or 2 pixels on coarse scales (a 24 mm lens:
    20"/px, where peak fit and solution are good to a pixel or so)."""
    from astropy.wcs.utils import proj_plane_pixel_scales
    scale = float(np.mean(proj_plane_pixel_scales(wcs.celestial))) * 3600.0
    return max(10.0, 2.0 * scale)


def identify(image: np.ndarray, wcs, n: int = 3, job_id: Optional[int] = None,
             annotations: Optional[list] = None, use_simbad: bool = True,
             match_px: float = 5.0, log=print, catalog='auto') -> list[FieldStar]:
    """Brightest n stars of the (stacked) image with RA/Dec, and names where available."""
    stars = brightest_stars(image, n)
    ann = _annotations(job_id, annotations, log)
    radius = simbad_radius(wcs)
    catalog = _catalog(catalog)
    return [name_star(s, wcs, ann, use_simbad, match_px, log, radius, catalog) for s in stars]


def identify_positions(image: np.ndarray, wcs, positions: dict, search: float = 8.0,
                       job_id: Optional[int] = None, annotations: Optional[list] = None,
                       use_simbad: bool = True, log=print, catalog='auto') -> dict:
    """Name the stars in given apertures: {name: (x, y)} -> {name: FieldStar}.

    Each position is refined with a peak fit on `image` (use the stack that was plate-solved,
    so the pixel position belongs to the same frames as the WCS). If no star is found the
    aperture centre is looked up as is."""
    from ..globaltrack import fit_peak
    img = np.asarray(image, dtype=np.float64)
    ann = _annotations(job_id, annotations, log)
    catalog = _catalog(catalog)
    sat = img.max() * 0.98
    out = {}
    for name, (x, y) in positions.items():
        f = fit_peak(img, x, y, search, half=10)
        if f.ok:
            st = FieldStar(f.x, f.y, 2 * math.pi * f.amp * f.sigma ** 2, f.fwhm,
                           bool(img[int(round(f.y)), int(round(f.x))] >= sat))
        else:
            st = FieldStar(float(x), float(y), float('nan'), float('nan'), False)
        log(f'looking up {name}')
        out[name] = name_star(st, wcs, ann, use_simbad, log=log, radius_arcsec=simbad_radius(wcs),
                              catalog=catalog)
    return out


def _nice_name(names: list[str]) -> str:
    """['α Cyg / 50 Cyg', 'Deneb / Arided / ...'] -> 'Deneb (α Cyg)'; ['HD 197161'] -> 'HD 197161'."""
    first = [n.split(' / ')[0].strip() for n in names]
    return f'{first[-1]} ({first[0]})' if len(first) > 1 else first[0]


def save_stars(path: str, stars: list[FieldStar]):
    with open(path, 'w') as f:
        json.dump([asdict(s) for s in stars], f, indent=1)


def load_stars(path: str) -> list[FieldStar]:
    with open(path) as f:
        return [FieldStar(**d) for d in json.load(f)]
