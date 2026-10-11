"""Name the brightest stars of a plate-solved field.

astrometry.net's annotations name only stars from its bright-star and HD lists (typically one
to three per field). So the brightest stars are found in the image itself, given the
astrometry.net name when there is one within a few pixels, and otherwise looked up in SIMBAD
(CDS) by position: main identifier, the usual catalogue IDs (HD, HIP, TYC, Gaia DR3) and V.

SIMBAD lookups need network access and astroquery (pip install "starlight-photometry[catalog]").
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np
from scipy.ndimage import maximum_filter, median_filter

PREFERRED_IDS = ('HD', 'HIP', 'TYC', 'Gaia DR3', 'UCAC4', '2MASS')


@dataclass
class FieldStar:
    x: float
    y: float
    flux: float                      # 2 pi A sigma^2 of the Gaussian fit (instrumental)
    fwhm: float
    saturated: bool
    ra: float = float('nan')         # deg, from the plate solution
    dec: float = float('nan')
    name: str = ''                   # best name: astrometry.net name, else SIMBAD main id
    ids: dict = field(default_factory=dict)   # catalogue -> id, e.g. {'HD': 'HD 197161'}
    vmag: Optional[float] = None
    source: str = ''                 # 'astrometry.net' | 'SIMBAD' | ''
    sep_arcsec: Optional[float] = None

    @property
    def radec(self) -> str:
        from astropy.coordinates import SkyCoord
        return SkyCoord(self.ra, self.dec, unit='deg').to_string('hmsdms', precision=1, sep=':')

    def label(self) -> str:
        v = f' V={self.vmag:.2f}' if self.vmag is not None else ''
        return f'{self.name or "?"}{v}'


def brightest_stars(image: np.ndarray, n: int = 3, min_sep: int = 15, border: int = 10,
                    saturation: Optional[float] = None) -> list[FieldStar]:
    """The n brightest stars by fitted flux (not by peak, so wide and saturated stars rank right)."""
    from ..globaltrack import fit_peak
    img = np.asarray(image, dtype=np.float64)
    sm = median_filter(img, 3)
    bg = np.median(sm)
    noise = 1.4826 * np.median(np.abs(sm - bg)) or 1.0
    peaks = (sm == maximum_filter(sm, 2 * min_sep + 1)) & (sm > bg + 8 * noise)
    peaks[:border, :] = peaks[-border:, :] = False
    peaks[:, :border] = peaks[:, -border:] = False
    ys, xs = np.nonzero(peaks)
    order = np.argsort(sm[ys, xs])[::-1][:max(20 * n, 200)]   # bright stars' halos give many peaks
    sat = saturation if saturation is not None else (img.max() * 0.98)
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


def simbad_lookup(ra: float, dec: float, radius_arcsec: float = 10.0) -> Optional[dict]:
    """Nearest SIMBAD object with a V magnitude (else nearest at all) within the radius."""
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


def name_star(s: FieldStar, wcs, annotations: list, use_simbad: bool = True,
              match_px: float = 5.0, log=print) -> FieldStar:
    """Fill RA/Dec, and name/IDs/V from astrometry.net annotations and SIMBAD."""
    s.ra, s.dec = (float(v) for v in wcs.all_pix2world(s.x, s.y, 0))
    near = [a for a in annotations if a['x'] is not None and a['names']
            and math.hypot(a['x'] - s.x, a['y'] - s.y) <= match_px]
    near.sort(key=lambda a: a['type'] != 'bright')          # proper names first
    if near:
        s.name = _nice_name(near[0]['names'])
        s.ids = {'astrometry.net': ' / '.join(n for b in near for n in b['names'])}
        s.vmag = next((b['vmag'] for b in near if b.get('vmag')), None)
        s.source = 'astrometry.net'
    if use_simbad:
        try:
            hit = simbad_lookup(s.ra, s.dec)
        except Exception as e:
            log(f'SIMBAD lookup failed: {e}')
            hit = None
        if hit:
            s.ids = {**hit['ids'], **s.ids}
            s.sep_arcsec = hit['sep_arcsec']
            if s.vmag is None:
                s.vmag = hit['vmag']
            if not s.name:
                s.name, s.source = hit['main_id'], 'SIMBAD'
    return s


def identify(image: np.ndarray, wcs, n: int = 3, job_id: Optional[int] = None,
             annotations: Optional[list] = None, use_simbad: bool = True,
             match_px: float = 5.0, log=print) -> list[FieldStar]:
    """Brightest n stars of the (stacked) image with RA/Dec, and names where available."""
    stars = brightest_stars(image, n)
    ann = _annotations(job_id, annotations, log)
    return [name_star(s, wcs, ann, use_simbad, match_px, log) for s in stars]


def identify_positions(image: np.ndarray, wcs, positions: dict, search: float = 8.0,
                       job_id: Optional[int] = None, annotations: Optional[list] = None,
                       use_simbad: bool = True, log=print) -> dict:
    """Name the stars in given apertures: {name: (x, y)} -> {name: FieldStar}.

    Each position is refined with a peak fit on `image` (use the stack that was plate-solved,
    so the pixel position belongs to the same frames as the WCS). If no star is found the
    aperture centre is looked up as is."""
    from ..globaltrack import fit_peak
    img = np.asarray(image, dtype=np.float64)
    ann = _annotations(job_id, annotations, log)
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
        out[name] = name_star(st, wcs, ann, use_simbad, log=log)
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
