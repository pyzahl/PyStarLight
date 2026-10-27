"""Plate-solve a stacked frame (FITS, e.g. from the capture app's live stack): RA/Dec of the field
centre, field size, plate scale and orientation, alt/az of the centre, and the brightest stars
with RA/Dec and names.

    uv run python -m pystarlight.wcs.solve_stack STACK.fits [--stars 5] [--scale ARCSEC]

Hints come from the FITS header: plate scale from FOCALLEN (mm) and XPIXSZ (um) unless --scale;
the time (DATE-OBS) and site (SITELAT / SITELONG / SITEELEV, else the config's default site)
give the centre's altitude and azimuth. Writes <stack>.wcs.fit (astrometry.net's solution) and
<stack>.solve.json (the result dict).
"""
from __future__ import annotations

import argparse
import json
import math
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .identify import identify
from .solve import load_wcs, orientation, solve_astrometry_net

# astrometry.net shrinks frames wider than this before finding stars (faster, same solution)
DOWNSAMPLE_ABOVE_PX = 2000


def scale_hint(header) -> Optional[float]:
    """Arcsec per pixel from FOCALLEN and XPIXSZ, None if either is missing."""
    f, pix = header.get('FOCALLEN'), header.get('XPIXSZ')
    return 206.265 * float(pix) / float(f) if f and pix else None


def _site(header, site=None):
    if header.get('SITELAT') is not None and header.get('SITELONG') is not None:
        return {'lat': float(header['SITELAT']), 'lon': float(header['SITELONG']),
                'ele': float(header.get('SITEELEV') or 0.0), 'source': 'GPS'}
    if site and site.get('lat') is not None:
        return {'lat': float(site['lat']), 'lon': float(site['lon']),
                'ele': float(site.get('ele') or 0.0), 'source': f"site {site.get('id', '')}"}
    return None


def alt_az(ra, dec, time_iso, site):
    """(altitude, azimuth) in degrees of RA/Dec at that UTC time and site. Offline: astropy's
    bundled Earth-orientation data, no download (arcsecond-level errors don't matter here)."""
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    loc = EarthLocation(lat=site['lat'] * u.deg, lon=site['lon'] * u.deg, height=site['ele'] * u.m)
    aa = SkyCoord(ra, dec, unit='deg').transform_to(AltAz(obstime=Time(time_iso, scale='utc'),
                                                          location=loc))
    return float(aa.alt.deg), float(aa.az.deg)


def field_size(wcs, shape) -> tuple[float, float]:
    """(width, height) of the field in degrees, between the midpoints of opposite edges."""
    from astropy.coordinates import SkyCoord
    h, w = shape

    def sep(x0, y0, x1, y1):
        a, b = wcs.all_pix2world([x0, x1], [y0, y1], 0)
        return float(SkyCoord(a[0], b[0], unit='deg').separation(SkyCoord(a[1], b[1], unit='deg')).deg)

    return sep(-0.5, (h - 1) / 2, w - 0.5, (h - 1) / 2), sep((w - 1) / 2, -0.5, (w - 1) / 2, h - 0.5)


def hms_dms(ra, dec) -> tuple[str, str]:
    from astropy.coordinates import SkyCoord
    c = SkyCoord(ra, dec, unit='deg')
    return (c.ra.to_string(unit='hour', sep='hms', precision=1, pad=True),
            c.dec.to_string(sep='dms', precision=0, alwayssign=True, pad=True))


def solve_stack(image: np.ndarray, header: dict, n_stars: int = 5, workdir: str = '.',
                dest: Optional[str] = None, scale_arcsec: Optional[float] = None,
                use_simbad: bool = True, site: Optional[dict] = None,
                log: Callable[[str], None] = print,
                cancel: Optional[threading.Event] = None) -> dict:
    """Solve `image` on nova.astrometry.net (API key: [astrometry] in the config) and describe
    the field; see the module doc. Blocks for up to the config's timeout: use a thread."""
    from ..config import load
    from ..config import site as get_site
    cfg = load()
    img = np.array(image, dtype=np.float32)
    if not np.isfinite(img).all():
        img[~np.isfinite(img)] = np.nanmedian(img)
    shape = img.shape
    scale = scale_arcsec or scale_hint(header)
    log(f'plate-scale hint {scale:.2f}"/px' if scale else 'no plate-scale hint (blind solve)')
    dest = dest or str(Path(workdir) / 'stack.wcs.fit')
    path, job = solve_astrometry_net(
        img, workdir=str(Path(workdir) / '.wcs-work'), log=log, cancel=cancel,
        scale_arcsec=scale, scale_err_pct=float(cfg['astrometry']['scale_err_pct']),
        dest=dest, return_job=True,
        downsample=2 if shape[1] > DOWNSAMPLE_ABOVE_PX else None)
    wcs = load_wcs(path)
    o = orientation(wcs, shape)
    fw, fh = field_size(wcs, shape)
    ra_s, dec_s = hms_dms(o.center_ra, o.center_dec)
    out = {'job_id': job, 'wcs': path, 'width': shape[1], 'height': shape[0],
           'center': {'ra': o.center_ra, 'dec': o.center_dec, 'ra_hms': ra_s, 'dec_dms': dec_s},
           'scale_arcsec': o.scale_arcsec, 'north_deg': o.north_deg, 'mirrored': o.mirrored,
           'field_deg': [fw, fh], 'time': header.get('DATE-OBS'), 'altaz': None, 'site': None}
    log(f'centre RA {ra_s}  Dec {dec_s}, field {fw:.2f} x {fh:.2f} deg')
    st = _site(header, get_site(cfg))
    if st and out['time']:
        try:
            alt, az = alt_az(o.center_ra, o.center_dec, out['time'], st)
            out['altaz'], out['site'] = {'alt': alt, 'az': az}, st
        except Exception as e:          # a bonus; never fail the solve for it
            log(f'alt/az not computed: {e}')
    if n_stars:
        log(f'identifying the {n_stars} brightest stars')
        stars = identify(img, wcs, n=n_stars, job_id=job, use_simbad=use_simbad, log=log)
        out['stars'] = []
        for s in stars:
            d = asdict(s)
            d['ra_hms'], d['dec_dms'] = hms_dms(s.ra, s.dec)
            out['stars'].append(d)
    return out


def solve_fits(path: str, n_stars: int = 5, scale_arcsec: Optional[float] = None,
               use_simbad: bool = True, log: Callable[[str], None] = print,
               cancel: Optional[threading.Event] = None) -> dict:
    """solve_stack for a FITS file; writes <stem>.wcs.fit and <stem>.solve.json next to it."""
    from astropy.io import fits
    p = Path(path)
    with fits.open(p) as hdul:
        img, header = hdul[0].data, dict(hdul[0].header)
    out = solve_stack(img, header, n_stars, workdir=str(p.parent),
                      dest=str(p.with_name(p.stem + '.wcs.fit')), scale_arcsec=scale_arcsec,
                      use_simbad=use_simbad, log=log, cancel=cancel)
    out['stack'] = str(p)
    p.with_name(p.stem + '.solve.json').write_text(json.dumps(out, indent=1, default=_json))
    return out


def _json(v):
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return str(v)


def describe(out: dict) -> str:
    c = out['center']
    lines = [f"Centre  RA {c['ra_hms']}  Dec {c['dec_dms']}   ({c['ra']:.4f}, {c['dec']:+.4f} deg)",
             f"Field   {out['field_deg'][0]:.2f} x {out['field_deg'][1]:.2f} deg, "
             f"{out['scale_arcsec']:.2f}\"/px, north {out['north_deg']:+.1f} deg from up"
             + (", mirrored" if out['mirrored'] else "")]
    if out.get('altaz'):
        lines.append(f"Pointing  alt {out['altaz']['alt']:.1f}  az {out['altaz']['az']:.1f} deg "
                     f"({out['site']['source']}, {out['time']} UTC)")
    for i, s in enumerate(out.get('stars', []), 1):
        v = f" V={s['vmag']:.2f}" if s.get('vmag') is not None else ""
        lines.append(f"{i}. {s['name'] or '?'}{v}  {s['ra_hms']} {s['dec_dms']}  "
                     f"(x {s['x']:.0f}, y {s['y']:.0f})")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('fits', help='stacked frame (FITS)')
    ap.add_argument('--stars', type=int, default=5, help='brightest stars to identify (default 5)')
    ap.add_argument('--scale', type=float, default=None, help='plate scale hint, arcsec/px')
    ap.add_argument('--no-simbad', action='store_true', help='no SIMBAD look-ups for names')
    a = ap.parse_args(argv)
    out = solve_fits(a.fits, a.stars, a.scale, not a.no_simbad)
    print(describe(out))


if __name__ == '__main__':
    main()
