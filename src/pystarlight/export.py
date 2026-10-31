"""Light-curve CSV in PyMovie's layout, for PyOTE (and anything else that reads PyMovie files).

The layout follows PyMovie 4.2.8 writeCsvFile(), which is what PyOTE's csvreader expects:

    # PyMovie Version ...           first line must contain 'PyMovie'
    # source: <movie path>          PyOTE uses the extension (.ravf / .adv metadata)
    # ... aperture descriptions ...
    # date at frame 0: YYYY-MM-DD   (not for AVI, as in PyMovie)
    #KEY: value                     movie metadata (PyOTE fills its report form from RAVF/ADV)
    FrameNum,timeInfo,signal-<a>,...,appsum-<a>,avgbkg-<a>,stdbkg-<a>,nmaskpx-<a>,maxpx-<a>,
        xcentroid-<a>,ycentroid-<a>,hit-defect-<a>,...

Rules PyOTE relies on: the column header is the first line containing 'FrameNum' (so no comment
may contain it), all signal columns come first (the first one is the default light curve), and
the aperture name is the text after the first '-' (so names must not contain '-').
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional

from . import __version__

PYMOVIE_VERSION = '4.2.8'

# PyMovie column -> (PyStarLight PyMovie-mode column, global-mode column)
COLUMN_MAP = {
    'appsum': ('appsum', 'sum'),
    'avgbkg': ('bkavg', 'bkavg'),
    'stdbkg': ('bkstd', 'bkstd'),
    'nmaskpx': ('mskpx', 'area'),
    'maxpx': ('maxpx', 'maxpx'),
    'xcentroid': ('xc', 'xc'),
    'ycentroid': ('yc', 'yc'),
    'hit-defect': ('hit_defect', 'hit_defect'),
}
ROLE_ORDER = {'target': 0, 'reference': 1, 'check': 2, '': 3}


def csv_name(name: str) -> str:
    """Aperture name safe for PyOTE: no '-', ',' or whitespace."""
    return re.sub(r'[-,\s]+', '_', name.strip()) or 'ap'


def _comment_safe(text: str) -> str:
    return str(text).replace('FrameNum', 'Frame-Num').replace('BinNo', 'Bin-No').replace('\n', ' ')


def _num(v, fmt='0.2f') -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ''
    return format(v, fmt)


def sky_lines(lc, wcs, stars=None, frame: float = 7.5) -> tuple[list[str], dict]:
    """Header lines describing the plate solution, and {aperture: 'RA Dec  (name V=..)'}.

    Aperture positions are taken at the row nearest `frame` (default: middle of the 16-frame
    stack that was solved), so drift during the run does not shift the coordinates."""
    from astropy.coordinates import SkyCoord

    from .wcs.solve import orientation
    frames = lc.columns[lc.names[0]]['frame']
    i = int(min(range(len(frames)), key=lambda k: abs(frames[k] - frame)))
    if getattr(wcs, 'pixel_shape', None):
        w, h = wcs.pixel_shape
    else:   # solved with the reference pixel at the image centre (crpix_center)
        w, h = (int(round(2 * v - 1)) for v in wcs.wcs.crpix)
    o = orientation(wcs, (h, w))
    lines = ['# plate solution (astrometry.net): ' + o.describe(),
             '# pixel -> RA/Dec: x,y = image column,row, 0 at the top-left pixel centre (WCS origin 0)']
    for card in ('CRVAL', 'CRPIX'):
        v = getattr(wcs.wcs, card.lower())
        lines.append(f'# {card}: {v[0]:.6f} {v[1]:.6f}')
    cd = wcs.wcs.cd if wcs.wcs.has_cd() else wcs.pixel_scale_matrix
    lines.append(f'# CD (deg/px): {cd[0][0]:.6e} {cd[0][1]:.6e} {cd[1][0]:.6e} {cd[1][1]:.6e}')
    for s in stars or []:
        lines.append(f'# bright star: {s.label()}  at x,y {s.x:.1f},{s.y:.1f}  {s.radec}')
    where = {}
    for n in lc.names:
        c = lc.columns[n]
        x, y = float(c['xc'][i]), float(c['yc'][i])
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        ra, dec = (float(v) for v in wcs.all_pix2world(x, y, 0))
        txt = radec_text(ra, dec)
        near = [st for st in stars or [] if math.hypot(st.x - x, st.y - y) < max(5.0, 2 * st.fwhm)]
        if near:
            txt += f'  ({near[0].label()})'
        where[n] = txt
    return lines, where


def write_pymovie_csv(lc, path: str, apertures: Optional[list[str]] = None,
                      source: Optional[str] = None, at_frame: Optional[int] = None,
                      wcs=None, stars=None):
    """Write `lc` (from analyze() or analyze_global()) as a PyMovie-style CSV.

    apertures: names to include, in column order. Default: target, references, checks, then
        the rest; sky apertures are left out (they have no signal).
    at_frame: frame for the aperture positions in the header (default: first row).
    wcs, stars: plate solution and identified bright stars (wcs.field_stars); adds the
        pixel -> RA/Dec mapping and each aperture's RA/Dec (and name, if it is one of them)."""
    glob = lc.mode == 'global'
    roles = lc.roles
    names = apertures or sorted((n for n in lc.names if roles.get(n) != 'sky'),
                                key=lambda n: (ROLE_ORDER.get(roles.get(n, ''), 3), lc.names.index(n)))
    out_names = [csv_name(n) for n in names]
    if len(set(out_names)) != len(out_names):
        raise ValueError(f'aperture names collide after removing "-": {out_names}')

    frames = lc.columns[names[0]]['frame']
    for i in range(len(frames) - 1):
        if frames[i] == frames[i + 1]:
            raise ValueError(f'duplicate frame {frames[i]:g}: PyOTE cannot read this')
    row0 = 0 if at_frame is None else int(min(range(len(frames)), key=lambda i: abs(frames[i] - at_frame)))
    aps = {a.name: a for a in lc.apertures}
    src = source or lc.source
    ext = Path(src).suffix.lower() if src else ''

    method = ('Aperture photometry was used to extract the lightcurves')
    lines = [f'# PyMovie Version {PYMOVIE_VERSION} compatible file, written by PyStarLight {__version__}',
             f'# source: {src}',
             '#',
             f'# {method}',
             '#']
    if glob:
        r = lc.frame_info.get('radius')
        lines += ['# PyStarLight global mode: all star apertures tracked together (peak fits),',
                  f'# circular apertures of radius {r[0]:.2f} px with sub-pixel weights.' if r is not None
                  else '# circular apertures with sub-pixel weights.',
                  '# nmaskpx is the effective (fractional) pixel count of the circle.']
    lines += ['# lunar background: False', '# yellow mask = default: False']
    sky = {}
    if wcs is not None:
        extra, sky = sky_lines(lc, wcs, stars)
        lines += ['#'] + extra
    for n, on in zip(names, out_names):
        a = aps.get(n)
        c = lc.columns[n]
        lines += ['#', f'# aperture name: {on}']
        if a is not None:
            lines += [f'# ____ aperture size: {a.size}',
                      f'# ____ x,y: ({c["xc"][row0]:.0f}, {c["yc"][row0]:.0f}) at frame {frames[row0]:g}',
                      f'# ____ threshold: {a.thresh:g}',
                      f'# ____ def mask radius: {a.default_mask_radius}',
                      f'# ____ color: {a.color}']
        if roles.get(n):
            lines.append(f'# ____ role: {roles[n]}')
        if n in sky:
            lines.append(f'# ____ RA,Dec (J2000): {sky[n]}')
        lines.append(f'# ____ csv output order: {names.index(n)}')
    lines.append('#')
    if ext not in ('.avi', '.mov', '.mp4', ''):
        date = next((d for d in lc.date if d), '')
        lines.append(f'# date at frame 0: {date}')
        for k, v in lc.metadata.items():
            lines.append(_comment_safe(f'#{k}: {v}'))
    lines += ['#', f'# {method}']

    header = ['FrameNum', 'timeInfo'] + [f'signal-{on}' for on in out_names]
    for on in out_names:
        header += [f'{col}-{on}' for col in COLUMN_MAP]

    with open(path, 'w', newline='\n') as f:
        for ln in lines:
            f.write(_comment_safe(ln) + '\n' if ln.startswith('#') else ln + '\n')
        f.write(','.join(header) + '\n')
        for i in range(len(frames)):
            t = lc.time[i]
            row = [f'{frames[i]:0.2f}', f'[{t}]' if t else '']
            row += [_num(lc.columns[n]['signal'][i]) for n in names]
            for n in names:
                c = lc.columns[n]
                for col, (pm_col, gl_col) in COLUMN_MAP.items():
                    key = gl_col if glob else pm_col
                    v = c.get(key)
                    v = None if v is None else float(v[i])
                    if col in ('nmaskpx', 'maxpx', 'hit-defect') and not glob:
                        row.append('' if v is None or math.isnan(v) else f'{v:.0f}')
                    elif col == 'hit-defect':
                        row.append('0' if v is None or math.isnan(v) else f'{v:.0f}')
                    else:
                        row.append(_num(v))
            f.write(','.join(row) + '\n')
