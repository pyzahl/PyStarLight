"""Find the target star's pixel position: plate-solve with nova.astrometry.net, or
use two reference stars.

Extracted from PyMovie 4.2.8 (main.py: getWCSsolution, resizeImage, setApertureFromWcsData,
doManualWcsCalibration, readManualCalibrationDataFile, queryVizier). The GUI dialogs and
the files PyMovie keeps in the AVI-WCS folder are left to the caller.

pixel_aspect is pixel width / height (1.0 for square pixels; analog video often is not).
"""

from __future__ import annotations

import glob
import math
import os
import shutil
import subprocess
import tempfile
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
                         return_job: bool = False, downsample: Optional[int] = None,
                         solver: Optional[str] = None):
    """Plate-solve a prepared frame with astrometry.net: uploaded to nova.astrometry.net, or with
    a local installation (solve-field + index files, see solve_local).

    solver: 'nova', 'local' or 'auto' (local when installed with index files, else nova);
    default: [astrometry] solver in the config. Local solves give no job id (no annotations).

    Returns the path of the downloaded WCS FITS file (``dest``, default
    workdir/wcs-<frame_num>.fit). Blocks while polling: run it in a worker thread.
    api_key, timeout and public default to the [astrometry] section of the config.

    scale_arcsec: expected plate scale (see plate_scale()); makes solving much faster.
    center: (ra, dec) in degrees if roughly known, searched within radius_deg.
    public: whether astrometry.net may show the image publicly (default: config, False).
    return_job: return (path, job id); the job id gives access to astrometry.net's annotations.
    downsample: let astrometry.net shrink the image by this factor before finding stars
    (large frames; the solution is still for the full image)."""
    from astropy.io import fits

    from ..config import load
    acfg = load()['astrometry']
    timeout = timeout or acfg['timeout_s']
    order = solver_order({**acfg, 'solver': solver or acfg.get('solver', 'auto')}, center, scale_arcsec)
    errors = []
    for name in order:
        if name == 'nova':
            break
        try:
            if name == 'astap':
                path = solve_astap(image, workdir, frame_num, timeout, log, cancel, scale_arcsec,
                                   center, radius_deg, dest, downsample, acfg)
            else:
                path = solve_local(image, workdir, frame_num, timeout, log, cancel, scale_arcsec,
                                   scale_err_pct, center, radius_deg, dest, downsample)
            return (path, None) if return_job else path
        except InterruptedError:
            raise
        except Exception as e:                  # the next offline solver, if there is one
            errors.append(f'{name}: {e}')
            if name != order[-1]:
                log(f'{"ASTAP" if name == "astap" else "astrometry.net"} did not solve it ({e}); '
                    'trying the next solver')
    if errors:
        raise RuntimeError('; '.join(errors))
    api_key = api_key or acfg['api_key']
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
    if downsample:
        hints['downsample_factor'] = int(downsample)
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


# ---------------------------------------------------------------------------- local astrometry.net

ASTROMETRY_CFG = '/etc/astrometry.cfg'      # Debian / Raspberry Pi OS; see astrometry_cfg()
# solve-field output lines worth passing on to the log
_LOCAL_LOG = ('solved with index', 'Field center', 'Field size', 'Field rotation', 'Did not solve',
              'pixel scale')


def astrometry_cfg() -> str:
    """The astrometry.net config solve-field reads: <prefix>/etc/astrometry.cfg next to the
    solve-field found (Homebrew: /opt/homebrew or /usr/local), else /etc/astrometry.cfg."""
    exe = shutil.which('solve-field')
    candidates = [str(Path(exe).resolve().parent.parent / 'etc' / 'astrometry.cfg')] if exe else []
    candidates += [ASTROMETRY_CFG, '/opt/homebrew/etc/astrometry.cfg', '/usr/local/etc/astrometry.cfg']
    return next((c for c in candidates if os.path.isfile(c)), ASTROMETRY_CFG)


def local_index_dirs(cfg_path: Optional[str] = None) -> list[str]:
    """Directories searched for index files ('add_path' lines of the astrometry.net config)."""
    try:
        with open(cfg_path or astrometry_cfg()) as f:
            return [ln.split()[1] for ln in f if ln.split()[:1] == ['add_path'] and len(ln.split()) > 1]
    except OSError:
        return []


def local_index_files(cfg_path: Optional[str] = None) -> list[str]:
    return sorted(f for d in local_index_dirs(cfg_path)
                  for f in glob.glob(os.path.join(d, '**', 'index-*.fits'), recursive=True))


def local_solver_available() -> bool:
    """solve-field installed and at least one index file found."""
    return shutil.which('solve-field') is not None and bool(local_index_files())


def uses_local_solver(acfg: dict) -> bool:
    """Plate solving works offline: [astrometry] solver is 'local' (astrometry.net) or 'astap', or
    'auto' with either installed (solve-field + index files, or ASTAP + a star database)."""
    solver = acfg.get('solver', 'auto')
    return solver in ('local', 'astrometry', 'astap') or (
        solver == 'auto' and (local_solver_available() or astap_available(acfg)))


def solver_order(acfg: dict, center=None, scale=None) -> list[str]:
    """The solvers to try, in order: 'astap', 'local' (astrometry.net), 'nova'.

    An explicit [astrometry] solver is used alone ('astrometry' = 'local'). 'auto': the offline
    solvers that are installed. With the position known: ASTAP first (seconds; without a scale it
    tries a series of field heights), astrometry.net when it fails. Without the position:
    astrometry.net first (blind solving is its strength; ASTAP's whole-sky search took over 90 s
    on 0.2 deg fields), ASTAP after it only with a plate scale (its blind scan over field heights
    found false solutions on the owner's recordings). nova when no offline solver can (it uploads
    the frame)."""
    solver = acfg.get('solver', 'auto')
    if solver == 'astrometry':
        solver = 'local'
    if solver in ('local', 'astap', 'nova'):
        return [solver]
    local = ['local'] if local_solver_available() else []
    astap = ['astap'] if astap_available(acfg) and (center is not None or scale) else []
    return (astap + local if center is not None else local + astap) or ['nova']


def solve_local(image: np.ndarray, workdir: str = 'wcs', frame_num: int = 0,
                timeout: float = 300.0, log: Callable[[str], None] = print,
                cancel: Optional[threading.Event] = None,
                scale_arcsec: Optional[float] = None, scale_err_pct: float = 20.0,
                center: Optional[tuple[float, float]] = None, radius_deg: float = 2.0,
                dest: Optional[str] = None, downsample: Optional[int] = None) -> str:
    """Plate-solve with a local astrometry.net (Debian: apt install astrometry.net plus index
    files, e.g. astrometry-data-2mass-02 .. -08-19 for 4' and larger quads). Offline; the
    index files must cover quads of ~10-100 % of the field width. Returns the WCS file path."""
    from astropy.io import fits
    os.makedirs(workdir, exist_ok=True)
    img_path = os.path.join(workdir, f'frame-{frame_num}-img.fit')
    fits.writeto(img_path, np.asarray(image, dtype=np.float32), overwrite=True)
    dest = dest or os.path.join(workdir, f'wcs-{frame_num}.fit')
    if os.path.exists(dest):
        os.remove(dest)                     # a stale solution must not pass for a new one
    cmd = ['solve-field', img_path, '--overwrite', '--no-plots', '--dir', workdir,
           '--wcs', dest, '--new-fits', 'none', '--corr', 'none', '--match', 'none',
           '--rdls', 'none', '--index-xyls', 'none', '--temp-axy', '--crpix-center',
           '--cpulimit', str(int(timeout))]
    # solve-field's own temp files (tmp.axy.*, tmp.ppm.*): in a folder of ours, removed after
    # (killed runs left them in /tmp)
    tmpdir = tempfile.mkdtemp(prefix='pystarlight-solve-')
    cmd += ['--temp-dir', tmpdir]
    if scale_arcsec:
        err = scale_err_pct / 100.0
        cmd += ['--scale-units', 'arcsecperpix', '--scale-low', f'{scale_arcsec * (1 - err):.4f}',
                '--scale-high', f'{scale_arcsec * (1 + err):.4f}']
    if center is not None:
        cmd += ['--ra', f'{center[0]:.6f}', '--dec', f'{center[1]:.6f}', '--radius', f'{radius_deg:g}']
    if downsample:
        cmd += ['--downsample', str(int(downsample))]
    log(f'Solving locally (solve-field, {len(local_index_files())} index files)')
    tail = []

    def reader(proc):
        for line in proc.stdout:
            line = line.rstrip()
            if line.strip():
                tail[:] = (tail + [line.strip()])[-5:]
            if any(k in line for k in _LOCAL_LOG):
                log(line.strip())

    t0 = time.monotonic()
    # a session of its own: cancel / timeout end solve-field and the astrometry-engine it starts
    # (which otherwise keeps a core busy until its CPU limit)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
    except OSError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    th = threading.Thread(target=reader, args=(proc,), daemon=True)
    th.start()
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.wait(0.2):
                raise InterruptedError('cancelled')
            if cancel is None:
                time.sleep(0.2)
            if time.monotonic() - t0 > timeout + 30:
                raise TimeoutError('local astrometry.net did not finish in time')
    finally:
        _end_group(proc)
        th.join(2)
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not os.path.exists(dest):
        raise RuntimeError('local astrometry.net could not solve the image'
                           + (f' ({tail[-1]})' if tail else ''))
    log(f'Wrote WCS solution to {dest} ({time.monotonic() - t0:.1f} s)')
    return dest


def _end_group(proc):
    """Terminate (then kill) a process and everything it started in its session."""
    if not hasattr(os, 'killpg'):                # Windows: no process groups / SIGKILL here
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(2)
        return
    import signal
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, AttributeError):
        pgid = None
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            elif proc.poll() is None:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(wait)
            if pgid is None:
                return
            os.killpg(pgid, 0)          # anything left in the group?
        except subprocess.TimeoutExpired:
            continue
        except (ProcessLookupError, OSError):
            return


# ---------------------------------------------------------------------------- ASTAP

ASTAP_NAMES = ('astap_cli', 'astap')
ASTAP_DIRS = (r'C:\Program Files\astap', r'C:\Program Files (x86)\astap', '/opt/astap',
              '/usr/share/astap/data', '/usr/local/opt/astap', '/opt/homebrew/opt/astap',
              '/Applications/ASTAP.app/Contents/MacOS', '/usr/local/bin', '/usr/bin')
_ASTAP_DB_FILE = r'^([a-z]\d\d)_\d{4}\.\d+$'      # e.g. d50_0101.1476, w08_0101.001


def astap_executable(acfg: Optional[dict] = None) -> Optional[str]:
    """ASTAP's command-line solver: [astrometry] astap (a path), else astap_cli / astap on PATH or
    in its install folders (Windows C:\\Program Files\\astap, Linux /opt/astap, macOS ...)."""
    acfg = acfg or {}
    given = str(acfg.get('astap') or '').strip()
    if given:
        return given if os.path.isfile(given) else shutil.which(given)
    for name in ASTAP_NAMES:
        found = shutil.which(name)
        if found:
            return found
    exe = '.exe' if os.name == 'nt' else ''
    for d in ASTAP_DIRS:
        for name in ASTAP_NAMES:
            p = os.path.join(d, name + exe)
            if os.path.isfile(p):
                return p
    return None


def astap_database_dirs(acfg: Optional[dict] = None) -> list[str]:
    """Folders that may hold ASTAP's star databases: [astrometry] astap_db, the program's own
    folder and ASTAP's default places."""
    acfg = acfg or {}
    dirs = [str(acfg.get('astap_db') or '').strip()]
    exe = astap_executable(acfg)
    if exe:
        dirs.append(os.path.dirname(os.path.realpath(exe)))
    dirs += list(ASTAP_DIRS[:6])
    out = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def astap_databases(acfg: Optional[dict] = None) -> dict[str, str]:
    """{database name ('d50', 'd80', 'w08', ...): folder} of the ASTAP star databases found."""
    import re
    found: dict[str, str] = {}
    for d in astap_database_dirs(acfg):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            m = re.match(_ASTAP_DB_FILE, n.lower())
            if m and m.group(1) not in found:
                found[m.group(1)] = d
    return found


def astap_available(acfg: Optional[dict] = None) -> bool:
    """ASTAP installed with at least one star database."""
    return astap_executable(acfg) is not None and bool(astap_databases(acfg))


def solve_astap(image: np.ndarray, workdir: str = 'wcs', frame_num: int = 0, timeout: float = 300.0,
                log: Callable[[str], None] = print, cancel: Optional[threading.Event] = None,
                scale_arcsec: Optional[float] = None, center: Optional[tuple[float, float]] = None,
                radius_deg: float = 2.0, dest: Optional[str] = None, downsample: Optional[int] = None,
                acfg: Optional[dict] = None) -> str:
    """Plate-solve with ASTAP (www.hnsky.org; Windows, Linux, macOS; offline with one of its star
    databases, D05..D80 for fields from ~0.3 deg, W08 / G05 for wide fields). The field height from
    the plate scale (else ASTAP finds it), the search around `center` (else the whole sky: slow).
    Writes a WCS FITS header file like solve_local (`dest`) and returns its path."""
    from astropy.io import fits
    from astropy.wcs import WCS
    acfg = acfg or {}
    exe = astap_executable(acfg)
    if exe is None:
        raise RuntimeError('ASTAP not found (www.hnsky.org; or set [astrometry] astap)')
    dbs = astap_databases(acfg)
    if not dbs:
        raise RuntimeError('no ASTAP star database found (www.hnsky.org; or set [astrometry] astap_db)')
    if center is None and not scale_arcsec:
        # whole sky and every field height: on the owner's recordings this either timed out or gave
        # a false solution (both times it "solved"); astrometry.net handles the blind case
        raise RuntimeError('ASTAP needs the position or the plate scale (a blind search over field '
                           'heights finds false solutions)')
    os.makedirs(workdir, exist_ok=True)
    img = np.asarray(image, dtype=np.float64)
    lo, hi = np.nanpercentile(img, [0.5, 99.95])
    data = np.clip((img - lo) / max(hi - lo, 1e-9), 0, 1) * 65000
    base = os.path.join(workdir, f'frame-{frame_num}-astap')
    img_path = base + '.fit'
    fits.writeto(img_path, np.nan_to_num(data).astype(np.uint16), overwrite=True)
    for ext in ('.wcs', '.ini'):
        if os.path.exists(base + ext):
            os.remove(base + ext)              # a stale solution must not pass for a new one
    h, w = img.shape[:2]
    # ASTAP needs the field height (it does not search scales as astrometry.net does): from the
    # plate scale, else a series of field heights tried in turn (the one that solved is logged)
    fovs = [scale_arcsec * h / 3600.0] if scale_arcsec else list(ASTAP_FOV_SCAN)
    common = ['-wcs', '-z', str(int(downsample or 0))]
    if center is not None:
        common += ['-ra', f'{center[0] / 15.0:.6f}', '-spd', f'{center[1] + 90.0:.6f}',
                   '-r', f'{radius_deg:g}']
    else:
        common += ['-r', '180']
    db = str(acfg.get('astap_database') or '').strip().lower()
    if db:
        common += ['-D', db]
        if db in dbs:
            common += ['-d', dbs[db]]
    elif str(acfg.get('astap_db') or '').strip():
        common += ['-d', str(acfg['astap_db'])]
    tried = ", ".join(f"{f:g}" for f in fovs)
    log(f'Solving locally (ASTAP, star databases {", ".join(sorted(dbs))}, field '
        + (f'{fovs[0]:.2f} deg)' if scale_arcsec else f'height unknown: trying {tried} deg)'))
    t0 = time.monotonic()
    why = ''
    rejected: list[str] = []
    for fov in fovs:
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            raise TimeoutError('ASTAP did not finish in time')
        for ext in ('.wcs', '.ini'):
            if os.path.exists(base + ext):
                os.remove(base + ext)
        rc = _run_astap([exe, '-f', img_path, '-o', base, '-fov', f'{fov:.4f}', *common], workdir, left,
                        cancel)
        ini = _read_astap_ini(base + '.ini')
        if rc == 0 and ini.get('PLTSOLVD', 'F').upper() == 'T' and os.path.exists(base + '.wcs'):
            if scale_arcsec:
                break
            height = _astap_height(base + '.wcs', h)
            if height is not None and abs(height / fov - 1) <= ASTAP_HEIGHT_TOL:
                log(f'ASTAP solved with a field height of {fov:g} deg')
                break
            # a match at a scale other than the one tried: a false solution
            rejected.append(f'{height:.3f} deg at {fov:g}' if height else f'no scale at {fov:g}')
            continue
        why = ini.get('ERROR') or ASTAP_EXIT.get(rc, f'exit code {rc}')
        if rc in (16, 32, 33):                  # image / database errors: other fields will not help
            break
    else:
        scan = '' if scale_arcsec else f'; field heights {fovs[0]:g}-{fovs[-1]:g} deg tried'
        if rejected:
            scan += f'; solutions at another field height rejected: {", ".join(rejected)}'
        raise RuntimeError(f'ASTAP could not solve the image ({why}{scan})')
    if not os.path.exists(base + '.wcs'):
        raise RuntimeError(f'ASTAP could not solve the image ({why})')
    with open(base + '.wcs', 'rb') as f:
        raw = f.read().decode('ascii', errors='replace').replace('\r', '').replace('\n', '')
    hdr = fits.Header.fromstring(raw)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        wcs = WCS(hdr, relax=True)
    out_hdr = wcs.to_header(relax=True)
    out_hdr['IMAGEW'] = (w, 'image width (as astrometry.net writes it)')
    out_hdr['IMAGEH'] = (h, 'image height')
    out_hdr['SOLVER'] = ('ASTAP', 'plate solution by ASTAP (www.hnsky.org)')
    dest = dest or os.path.join(workdir, f'wcs-{frame_num}.fit')
    fits.PrimaryHDU(header=out_hdr).writeto(dest, overwrite=True)
    log(f'Wrote WCS solution to {dest} ({time.monotonic() - t0:.1f} s, ASTAP)')
    return dest


ASTAP_EXIT = {1: 'no solution', 2: 'not enough stars', 16: 'error reading the image',
              32: 'no star database found', 33: 'error reading the star database'}
ASTAP_FOV_SCAN = (0.1, 0.15, 0.2, 0.3, 0.45, 0.65, 0.9, 1.3, 2.0, 3.0, 5.0)  # deg, tried without a scale
ASTAP_HEIGHT_TOL = 0.25     # a scan solution's own field height within 25 % of the height tried


def _astap_height(wcs_path: str, height_px: int) -> Optional[float]:
    """Field height (deg) of ASTAP's .wcs solution: pixel scale times the image height."""
    from astropy.io import fits
    from astropy.wcs import WCS
    from astropy.wcs.utils import proj_plane_pixel_scales
    try:
        with open(wcs_path, 'rb') as f:
            hdr = fits.Header.fromstring(f.read().decode('ascii', errors='replace')
                                         .replace('\r', '').replace('\n', ''))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            scale = float(np.sqrt(np.prod(proj_plane_pixel_scales(WCS(hdr, relax=True).celestial))))
        return scale * height_px if scale > 0 else None
    except Exception:                           # unreadable header: no height
        return None


def _run_astap(cmd: list[str], workdir: str, timeout: float, cancel: Optional[threading.Event]) -> int:
    """One ASTAP run with cancel and timeout; its exit code."""
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, cwd=workdir)
    out: list[str] = []
    th = threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True)
    th.start()
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.wait(0.2):
                raise InterruptedError('cancelled')
            if cancel is None:
                time.sleep(0.2)
            if time.monotonic() - t0 > timeout:
                raise TimeoutError('ASTAP did not finish in time')
    finally:
        _end_group(proc)
        th.join(2)
    return proc.returncode


def _read_astap_ini(path: str) -> dict:
    """KEY=value lines of ASTAP's .ini result file (PLTSOLVD, CRVAL1, ..., ERROR, WARNING)."""
    out = {}
    try:
        with open(path, encoding='ascii', errors='replace') as f:
            for line in f:
                if '=' in line:
                    k, v = line.split('=', 1)
                    out[k.strip().upper()] = v.strip()
    except OSError:
        pass
    return out


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


def stamp_rows(image: np.ndarray, edge_fraction: float = 0.15, min_fraction: float = 0.25,
               nsigma: float = 10.0) -> tuple[int, int]:
    """(top, bottom): rows taken by a burnt-in timestamp at the top / bottom edge. A stamp
    row has many pixels far off the sky (black background, saturated text); stars never fill
    a quarter of a row. Rows are counted from the outermost such row to the edge, plus one."""
    h = image.shape[0]
    st = robust_mean_std(np.asarray(image[h // 4:3 * h // 4], dtype=np.float64))
    off = (np.abs(image - st.mean) > nsigma * max(st.std, 1e-6)).mean(axis=1) >= min_fraction
    n = max(1, int(h * edge_fraction))
    top = np.flatnonzero(off[:n])
    bot = np.flatnonzero(off[h - n:])
    return (int(top.max()) + 2 if top.size else 0,
            n - int(bot.min()) + 1 if bot.size else 0)


def recorded_pixel_um(metadata: dict) -> Optional[float]:
    """Pixel pitch (um) of the recording's sensor: INSTRUMENT-SENSOR-PIXEL-SIZE-X (ASTRID,
    PyStarLight's capture), else from the sensor's name (capture.cameras.PIXEL_UM)."""
    from ..capture.cameras import pixel_um
    m = {str(k).upper(): v for k, v in (metadata or {}).items()}
    try:
        um = float(m.get('INSTRUMENT-SENSOR-PIXEL-SIZE-X') or 0)
    except (TypeError, ValueError):
        um = 0.0
    if 0.5 < um < 50:
        return round(um, 3)
    return next((pixel_um(str(m[k])) for k in ('INSTRUMENT-SENSOR', 'INSTRUMENT', 'CAMERA-SENSOR-INFO')
                 if m.get(k) and pixel_um(str(m[k]))), None)


def fallback_scale(metadata: dict, equipment: Optional[dict]) -> Optional[tuple[float, str]]:
    """(arcsec per pixel, how) from the config's [fallback_equipment] (focal_length_mm, pixel_um,
    binning) for a recording that does not carry its optics; the pixel pitch from the
    recording's sensor name where the table leaves it at 0. None when it is not set."""
    e = equipment or {}
    try:
        f, um = float(e.get('focal_length_mm') or 0), float(e.get('pixel_um') or 0)
        binning = int(e.get('binning') or 1)
    except (TypeError, ValueError):
        return None
    um = um or recorded_pixel_um(metadata) or 0
    if not f or not um:
        return None
    return 206.265 * um * binning / f, f'{f:g} mm, {um:g} um pixels' + (f' x{binning}' if binning > 1 else '')


def recorded_scale(metadata: dict) -> Optional[tuple[float, str]]:
    """(arcsec per pixel, how) from the recording's own optics: its effective focal length
    (FOCAL-LENGTH-MM, or FOCAL-LENGTH-CM of earlier captures) and the pixel pitch of its sensor
    (INSTRUMENT-SENSOR / INSTRUMENT / CAMERA-SENSOR-INFO, capture.cameras.PIXEL_UM), times the
    binning. None when it does not carry them."""
    m = {str(k).upper(): v for k, v in (metadata or {}).items()}

    def num(k):
        try:
            x = float(m[k])
        except (KeyError, TypeError, ValueError):
            return None
        return x if math.isfinite(x) and x > 0 else None
    f = num('FOCAL-LENGTH-MM') or ((num('FOCAL-LENGTH-CM') or 0) * 10) or None
    um = recorded_pixel_um(m)
    if not f or not um:
        return None
    binning = int(num('IMAGE-BINNING-X') or num('BINNING-X') or 1)
    return 206.265 * um * binning / f, f'{f:g} mm, {um:g} um pixels' + (f' x{binning}' if binning > 1 else '')


def movie_hints(metadata: dict, site: Optional[dict] = None, max_km: float = 20.0) -> dict:
    """Solve hints from a recording's own metadata (ADV: RA in hours, DEC, LATITUDE,
    LONGITUDE). 'center': (ra_deg, dec_deg) if given; 'other_site': True when the recording
    was made more than max_km from `site` (its optics, hence its plate scale, do not apply);
    'scale': (arcsec per pixel, how) from the recording's own optics (recorded_scale) or None."""
    def num(k):
        try:
            x = float(metadata[k])
        except (KeyError, TypeError, ValueError):
            return None
        return x if math.isfinite(x) else None          # NaN: PyStarLight capture without a GPS fix
    out = {'center': None, 'other_site': False, 'scale': recorded_scale(metadata)}
    ra, dec = num('RA'), num('DEC')
    if ra is not None and dec is not None and -90 <= dec <= 90 and (ra, dec) != (0, 0):
        out['center'] = (ra * 15.0 if 0 <= ra < 24 else ra, dec)
    lat, lon = num('LATITUDE'), num('LONGITUDE')
    if site and lat is not None and lon is not None and site.get('lat') is not None:
        dlat = math.radians(lat - float(site['lat']))
        dlon = math.radians(lon - float(site['lon'])) * math.cos(math.radians(lat))
        out['other_site'] = 6371.0 * math.hypot(dlat, dlon) > max_km
    return out


def stack_frames(reader, start: int = 0, count: int = 16) -> np.ndarray:
    """Median of `count` frames from `start`: deeper and free of cosmic rays and hot flickers."""
    stop = min(len(reader) - 1, start + count - 1)
    return np.median([f.image.astype(np.float32) for f in reader.frames(start, stop)], axis=0)


def solve_movie(movie_path: str, cfg: Optional[dict] = None, site_name: Optional[str] = None,
                start: int = 0, log: Callable[[str], None] = print,
                cancel: Optional[threading.Event] = None, force: bool = False):
    """Plate-solve a recording once and cache the solution as <movie>.wcs.fit.

    Uses a median stack of [astrometry] stack_frames frames. Hints: the plate scale from
    [astrometry] scale_arcsec, else from the recording's own optics (effective focal length and
    sensor, recorded_scale), else, with a WARNING, from [fallback_equipment] (fallback_scale)
    unless the recording was made more than 20 km from the station ([observer] latitude /
    longitude: someone else's optics); none with scale_hint = false. site_name: unused (kept
    for callers). The recording's RA/Dec as the
    field centre unless center_hint = false. Returns (astropy WCS, Orientation)."""
    from pathlib import Path

    from ..config import load
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
        meta = dict(getattr(mv, 'metadata', {}) or {})
    top, bottom = stamp_rows(img)
    if top or bottom:
        log(f'timestamp rows blanked: top {top}, bottom {bottom}')
        img = prepare_for_astrometry(img, top, bottom)
    scale = None
    obs = cfg.get('observer') or {}
    home = ({'lat': obs['latitude'], 'lon': obs['longitude']}
            if obs.get('latitude') or obs.get('longitude') else None)
    hints = movie_hints(meta, home)
    center = hints['center'] if acfg.get('center_hint', True) else None
    if center:
        log('field centre from the recording: RA {:.3f}, Dec {:.3f} deg'.format(*center))
    if not acfg.get('scale_hint', True):
        log('no plate-scale hint (scale_hint = false)')
    elif float(acfg.get('scale_arcsec') or 0) > 0:
        scale = float(acfg['scale_arcsec'])
        log(f'plate scale from the config: {scale:.2f}"/px')
    elif hints['scale']:
        scale, how = hints['scale']
        log(f'plate scale from the recording ({how}): {scale:.2f}"/px')
    elif hints['other_site']:
        log("WARNING: the recording carries no focal length / sensor and was made more than 20 km "
            "from the station (someone else's optics): no plate-scale hint")
    elif fallback_scale(meta, cfg.get('fallback_equipment')):
        scale, how = fallback_scale(meta, cfg.get('fallback_equipment'))
        log(f'WARNING: the recording carries no focal length / sensor: plate scale {scale:.2f}"/px '
            f'from [fallback_equipment] ({how})')
    else:
        log("WARNING: the recording carries no focal length / sensor and [fallback_equipment] is "
            "not set: no plate-scale hint (blind solve)")
    path, job = solve_astrometry_net(img, workdir=str(dest.parent / '.wcs-work'), frame_num=start, log=log,
                                     cancel=cancel, scale_arcsec=scale, center=center,
                                     scale_err_pct=float(acfg['scale_err_pct']), dest=str(dest),
                                     return_job=True, solver=acfg.get('solver', 'auto'),
                                     api_key=acfg.get('api_key') or None, timeout=acfg.get('timeout_s'))
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
    if any(s.name for s in stars):          # no names at all (offline): look them up next time
        save_stars(str(cache), stars)
    return stars
