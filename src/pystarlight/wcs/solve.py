"""Find the target star's pixel position: plate-solve with nova.astrometry.net, or
use two reference stars.

Extracted from PyMovie 4.2.8 (main.py: getWCSsolution, resizeImage, setApertureFromWcsData,
doManualWcsCalibration, readManualCalibrationDataFile, queryVizier). The GUI dialogs and
the files PyMovie keeps in the AVI-WCS folder are left to the caller.

pixel_aspect is pixel width / height (1.0 for square pixels; analog video often is not).
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.request import urlopen

import numpy as np

from ..photometry import robust_mean_std
from . import astrometry_client
from .manual import new_solve_triangle


def sky_coord(text: str):
    """'18h12m10.2s +22d10m03s', '273.04 22.17' etc. -> astropy SkyCoord (ICRS)."""
    from astropy.coordinates import SkyCoord
    try:
        return SkyCoord(text, frame='icrs')
    except Exception:
        import astropy.units as u
        return SkyCoord(text, frame='icrs', unit=(u.deg, u.deg))


# --------------------------------------------------------------------------- two-star solution

@dataclass
class TwoStarSolution:
    x: float                      # target pixel position from the solution
    y: float
    aperture_xy: tuple[int, int]  # where PyMovie centres the target aperture (x - 0.5, rounded)
    plate_scale: float            # arcsec per pixel
    rotation_deg: float           # RA/Dec to x/y rotation


def solve_two_stars(ref1: tuple, ref2: tuple, target, pixel_aspect: float = 1.0,
                    mirrored: bool = False) -> TwoStarSolution:
    """ref1, ref2: (coord, x, y) with coord a SkyCoord or coordinate string, x/y the star's
    pixel position (the aperture centre in PyMovie). target: SkyCoord or string.

    mirrored: the image is a mirror image of the sky (an odd number of reflections in
    the light path, e.g. a star diagonal). PyMovie's solver only handles non-mirrored
    images; for a mirrored image it puts the target at the mirror position across the
    ref1-ref2 line. The two references cannot tell the cases apart: a third known star
    can (use the setting that places it correctly)."""
    sign = -1 if mirrored else 1

    def star(ref):
        c, x, y = ref
        c = sky_coord(c) if isinstance(c, str) else c
        return {'ra': c.ra.degree, 'dec': c.dec.degree, 'x': sign * x, 'y': y}

    t = sky_coord(target) if isinstance(target, str) else target
    targ = {'ra': t.ra.degree, 'dec': t.dec.degree, 'x': None, 'y': None}
    sol, plate_scale, rot = new_solve_triangle(star(ref1), star(ref2), targ, pixel_aspect)
    x, y = sign * sol['x'], sol['y']
    # RA/Dec belong to the pixel's upper-left corner; PyMovie moves half a pixel to its centre
    ap = (int(round(x - 0.5)), int(round(y - 0.5)))
    return TwoStarSolution(x, y, ap, plate_scale, rot)


# --------------------------------------------------------------------------- astrometry.net

def prepare_for_astrometry(image: np.ndarray, redact_top: int = 0, redact_bottom: int = 0,
                           pixel_aspect: float = 1.0) -> np.ndarray:
    """Copy of the frame ready for upload: timestamp rows set to the background, and
    resized to square pixels. Do not stretch or clip: the solver's star finder works
    best on the original pixel values."""
    h, w = image.shape
    if redact_top + redact_bottom > h - 4:
        raise ValueError(f'{redact_top + redact_bottom} is an unreasonable number of lines to redact')
    img = np.array(image, copy=True)
    y0, x0 = h // 2, w // 2
    bg = robust_mean_std(img[y0:y0 + 51, x0:x0 + 51]).mean
    if redact_top:
        img[:redact_top, :] = bg
    if redact_bottom:
        img[h - redact_bottom:, :] = bg
    if pixel_aspect != 1.0:
        from skimage.transform import resize
        if pixel_aspect <= 1.0:
            w = round(w * pixel_aspect)
        else:
            h = round(h / pixel_aspect)
        img = resize(img, (h, w), mode='edge', anti_aliasing=False, preserve_range=True, order=0)
    return img


def solve_astrometry_net(image: np.ndarray, api_key: Optional[str] = None, workdir: str = 'wcs',
                         frame_num: int = 0, poll_seconds: float = 5.0, timeout: Optional[float] = None,
                         log: Callable[[str], None] = print,
                         cancel: Optional[threading.Event] = None,
                         scale_arcsec: Optional[float] = None, scale_err_pct: float = 20.0,
                         center: Optional[tuple[float, float]] = None, radius_deg: float = 2.0,
                         public: Optional[bool] = None, dest: Optional[str] = None,
                         return_job: bool = False):
    """Upload a prepared frame to nova.astrometry.net and wait for the solution.

    Returns the path of the downloaded WCS FITS file (``dest``, default
    workdir/wcs-<frame_num>.fit). Blocks while polling: run it in a worker thread.
    api_key, timeout and public default to the [astrometry] section of the config.

    scale_arcsec: expected plate scale (see plate_scale()); makes solving much faster.
    center: (ra, dec) in degrees if roughly known, searched within radius_deg.
    public: whether astrometry.net may show the image publicly (default: config, False).
    return_job: return (path, job id); the job id gives access to astrometry.net's annotations."""
    from astropy.io import fits

    from ..config import load
    acfg = load()['astrometry']
    api_key = api_key or acfg['api_key']
    timeout = timeout or acfg['timeout_s']
    public = acfg.get('public', False) if public is None else public
    if not api_key:
        raise ValueError('no astrometry.net API key: set [astrometry] api_key in your config')

    os.makedirs(workdir, exist_ok=True)
    img_path = os.path.join(workdir, f'frame-{frame_num}-img.fit')
    hdr = fits.Header()
    hdr['OBSERVER'] = 'PyStarLight'
    # float32: PyMovie wrote int16, which wraps 16-bit pixels above 32767 to negative values
    fits.writeto(img_path, np.asarray(image, dtype=np.float32), hdr, overwrite=True)

    c = astrometry_client.Client(tracer=log)
    log('Logging in to nova.astrometry.net')
    c.login(api_key)
    log('Submitting image for WCS calibration')
    hints = {'crpix_center': True, 'publicly_visible': 'y' if public else 'n',
             'allow_commercial_use': 'n', 'allow_modifications': 'n'}
    if scale_arcsec:
        hints.update(scale_units='arcsecperpix', scale_type='ev', scale_est=scale_arcsec,
                     scale_err=scale_err_pct)
    else:
        hints['scale_units'] = 'degwidth'
    if center is not None:
        hints.update(center_ra=center[0], center_dec=center[1], radius=radius_deg)
    up = c.upload(img_path, **hints)
    if up is None:
        raise astrometry_client.RequestError('astrometry.net did not accept the image')
    sub_id = str(up['subid'])
    log(f'Submission {sub_id}; waiting for a job number')

    deadline = time.monotonic() + timeout

    def wait():
        if cancel is not None and cancel.wait(poll_seconds):
            raise InterruptedError('cancelled')
        if cancel is None:
            time.sleep(poll_seconds)
        if time.monotonic() > deadline:
            raise TimeoutError('astrometry.net did not answer in time')

    job_id = None
    while job_id is None:
        wait()
        jobs = c.sub_status(sub_id, justdict=True).get('jobs', [])
        job_id = next((j for j in jobs if j is not None), None)
    log(f'Job {job_id}; waiting for the solution')

    while True:
        wait()
        status = c.job_status(job_id, justdict=True).get('status', '')
        if status in ('success', 'failure'):
            break
    if status != 'success':
        raise RuntimeError(f'astrometry.net could not solve the image (job {job_id})')

    url = astrometry_client.Client.default_url.replace('/api/', f'/wcs_file/{int(job_id)}')
    dest = dest or os.path.join(workdir, f'wcs-{frame_num}.fit')
    with urlopen(url) as f, open(dest, 'wb') as out:
        out.write(f.read())
    log(f'Wrote WCS solution to {dest}')
    return (dest, int(job_id)) if return_job else dest


def load_wcs(path: str):
    """astropy WCS from a solution file."""
    from astropy.io import fits
    from astropy.utils.exceptions import AstropyWarning
    from astropy.wcs import WCS
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', AstropyWarning)
        with fits.open(path) as hdul:
            return WCS(hdul[0].header)


@dataclass
class Orientation:
    scale_arcsec: float      # arcsec per pixel (mean of x and y)
    north_deg: float         # direction of north in the image, degrees counter-clockwise from 'up'
    mirrored: bool           # image is a mirror image of the sky (odd number of reflections)
    center_ra: float
    center_dec: float

    def describe(self) -> str:
        return (f'{self.scale_arcsec:.3f}"/px, north {self.north_deg:+.1f} deg from up, '
                f'{"mirrored" if self.mirrored else "not mirrored"}, '
                f'centre RA {self.center_ra:.4f} Dec {self.center_dec:+.4f}')


def orientation(wcs, shape: tuple[int, int]) -> Orientation:
    """Plate scale, rotation and parity of a solved frame (pixel y = row, pointing down)."""
    h, w = shape
    cx, cy = (w - 1) / 2, (h - 1) / 2
    ra0, dec0 = (float(v) for v in wcs.all_pix2world(cx, cy, 0))
    # one arcminute north and east of the centre, in pixels
    d = 1 / 60
    xn, yn = (float(v) for v in wcs.all_world2pix(ra0, dec0 + d, 0))
    xe, ye = (float(v) for v in wcs.all_world2pix(ra0 + d / np.cos(np.radians(dec0)), dec0, 0))
    vn = np.array([xn - cx, yn - cy])
    ve = np.array([xe - cx, ye - cy])
    scale = 60.0 / ((np.hypot(*vn) + np.hypot(*ve)) / 2)
    # 'up' in the displayed image is -y; angle measured counter-clockwise as seen on screen
    north = float(np.degrees(np.arctan2(-vn[0], -vn[1])))
    # on the sky (north up), east is to the left: east = north rotated +90 deg on screen
    cross = vn[0] * (-ve[1]) - (-vn[1]) * ve[0]
    return Orientation(float(scale), north, bool(cross < 0), ra0, dec0)


def plate_scale(focal_mm: float, sensor_width_mm: float, width_px: int, binning: int = 1) -> float:
    """Expected arcsec per pixel from optics and sensor (206.265 * pixel_um / focal_mm)."""
    pixel_um = 1000.0 * sensor_width_mm / width_px * binning
    return 206.265 * pixel_um / focal_mm


def target_pixel(wcs, target, pixel_aspect: float = 1.0) -> tuple[float, float]:
    """Pixel (x, y) of a sky position in the original (not resized) frame."""
    t = sky_coord(target) if isinstance(target, str) else target
    x, y = (float(v) for v in t.to_pixel(wcs))
    if pixel_aspect < 1.0:
        x = x / pixel_aspect
    elif pixel_aspect > 1.0:
        y = y * pixel_aspect
    return x, y


def ucac4_position(star_id: str):
    """Look up a UCAC4 star ('123-456789') in VizieR. Needs astroquery and network access."""
    from astropy.coordinates import SkyCoord
    from astroquery.vizier import Vizier
    v = Vizier(columns=['_RAJ2000', '_DEJ2000', 'f.mag'], column_filters={'UCAC4': f'=={star_id}'})
    result = v.query_object(f'UCAC4 {star_id}', catalog=['I/322A'])
    if len(result) == 0:
        raise LookupError(f'UCAC4 {star_id} not found')
    row = result[0]
    return SkyCoord(row['_RAJ2000'], row['_DEJ2000'], frame='icrs')


def stack_frames(reader, start: int = 0, count: int = 16) -> np.ndarray:
    """Median of `count` frames from `start`: deeper and free of cosmic rays and hot flickers."""
    stop = min(len(reader) - 1, start + count - 1)
    return np.median([f.image.astype(np.float32) for f in reader.frames(start, stop)], axis=0)


def solve_movie(movie_path: str, cfg: Optional[dict] = None, site_name: Optional[str] = None,
                start: int = 0, log: Callable[[str], None] = print,
                cancel: Optional[threading.Event] = None, force: bool = False):
    """Plate-solve a recording once and cache the solution as <movie>.wcs.fit.

    Uses a median stack of [astrometry] stack_frames frames, and the plate scale of the site
    (focal_mm, sensor_mm, binning) as a hint. Returns (astropy WCS, Orientation)."""
    from pathlib import Path

    from ..config import load
    from ..config import site as get_site
    from ..movie import open_movie

    cfg = cfg or load()
    acfg = cfg['astrometry']
    p = Path(movie_path)
    dest = solution_path(p)
    with open_movie(str(p)) as mv:
        shape = mv.read(start).image.shape
        if dest.exists() and not force:
            w = load_wcs(str(dest))
            log(f'using cached plate solution {dest.name}')
            return w, orientation(w, shape)
        img = stack_frames(mv, start, int(acfg['stack_frames']))
    scale = None
    st = get_site(cfg, site_name)
    if st and st.get('focal_mm') and st.get('sensor_mm'):
        scale = plate_scale(st['focal_mm'], st['sensor_mm'][0], shape[1], int(st.get('binning', 1)))
        log(f"site {st['id']}: expected plate scale {scale:.2f}\"/px")
    path, job = solve_astrometry_net(img, workdir=str(dest.parent / '.wcs-work'), frame_num=start, log=log,
                                     cancel=cancel, scale_arcsec=scale,
                                     scale_err_pct=float(acfg['scale_err_pct']), dest=str(dest),
                                     return_job=True)
    w = load_wcs(path)
    o = orientation(w, shape)
    import json
    with open(dest.with_suffix('.json'), 'w') as f:
        json.dump({'job_id': job, 'stack_start': start, 'stack_frames': int(acfg['stack_frames']),
                   'orientation': o.__dict__}, f, indent=1)
    log('plate solution: ' + o.describe())
    return w, o


def solution_path(movie_path) -> Path:
    """<movie>.wcs.fit next to a movie file, or wcs.fit inside a FITS folder."""
    p = Path(movie_path)
    return p.with_name(p.stem + '.wcs.fit') if p.is_file() else p / 'wcs.fit'


def field_stars(movie_path: str, wcs, cfg: Optional[dict] = None, n: int = 3,
                log: Callable[[str], None] = print, force: bool = False):
    """The n brightest stars of the solved stack with RA/Dec and names, cached as
    <movie>.stars.json (astrometry.net annotations if the job id is known, then SIMBAD)."""
    import json

    from ..config import load
    from ..movie import open_movie
    from .identify import identify, load_stars, save_stars

    cfg = cfg or load()
    sol = solution_path(movie_path)
    cache = sol.with_name(sol.name.replace('.wcs.fit', '.stars.json')) if sol.name != 'wcs.fit' \
        else sol.with_name('stars.json')
    if cache.exists() and not force:
        return load_stars(str(cache))
    meta = {}
    if sol.with_suffix('.json').exists():
        meta = json.loads(sol.with_suffix('.json').read_text())
    start = int(meta.get('stack_start', 0))
    with open_movie(str(movie_path)) as mv:
        img = stack_frames(mv, start, int(cfg['astrometry']['stack_frames']))
    log('identifying the brightest stars')
    stars = identify(img, wcs, n=n, job_id=meta.get('job_id'), log=log)
    save_stars(str(cache), stars)
    return stars
