"""Offline star names from PySyzygos' local Gaia DR3 catalogue (format 2, ../PyOccult).

PySyzygos keeps all Gaia DR3 stars to a G limit in one folder; its optional `xref` part adds the
UCAC4, Tycho-2 and Hipparcos numbers that Occult and OWC show. Read here with numpy only, from
the layout documented in PySyzygos' starcat.py (no PySyzygos import):

    catalog.json   {"format": 2, "gmax", "cell_deg": 0.25, "n_stars", "build_id", ...}
    offsets.npy    int64, n_cells + 1: rows of each 0.25 deg cell (cell = dec band * 1440 + ra band)
    core.npy       CORE rows sorted by cell, then G; position inside the cell in steps of SCALE
    xref.npy       XREF rows in the same order (0 = none); xref.json from the same build

Folder: [catalog] gaia_dir, else $PYSYZYGOS_CATALOG, else a format-2 catalogue next to the
PySyzygos sites.py named by pysyzygos_sites (one with the xref part first).
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

CELL = 0.25                                         # deg
N_RA, N_DEC = int(round(360 / CELL)), int(round(180 / CELL))
SCALE = CELL / 2.0 ** 32                            # deg per position step
G_MMAG_NONE = 0xFFFF
GAIA_EPOCH = 2016.0
CORE = np.dtype([('source_id', '<i8'), ('dra', '<u4'), ('ddec', '<u4'), ('parallax', '<f4'), ('pmra', '<f4'),
                 ('pmdec', '<f4'), ('g', '<u2'), ('flags', 'u1')])
XREF = np.dtype([('ucac4', '<u4'), ('tyc', '<u4'), ('hip', '<u4')])


def ucac4_name(v: int) -> str:
    return f'UCAC4 {v // 1_000_000:03d}-{v % 1_000_000:06d}' if v else ''


def tyc_name(v: int) -> str:
    return f'TYC {v // 4 // 20000}-{v // 4 % 20000}-{v % 4}' if v else ''


def parse_star_id(text: str) -> Optional[tuple[str, int]]:
    """('source_id' | 'ucac4' | 'tyc' | 'hip', number as stored in the catalogue) of a star
    name as Occult, OWC and Gaia write it: 'Gaia DR3 2071...', 'DR3 2071...', a bare source id,
    'UCAC4 677-089123', 'TYC 3574-1718-1', 'HIP 101769'; also hyphenated as in file names
    ('UCAC4-593-004965', 'Gaia-DR3-2071...'). None if it is none of these."""
    t = ' '.join(text.strip().upper().replace('_', ' ').split())
    t = re.sub(r'^(GAIA|DR3|UCAC4|TYC|HIP)-', r'\1 ', re.sub(r'^GAIA-DR3-', 'GAIA DR3 ', t))
    m = re.fullmatch(r'(?:GAIA\s*)?(?:DR3\s*)?(\d{6,20})', t)
    if m:
        return 'source_id', int(m.group(1))
    m = re.fullmatch(r'UCAC4\s*(\d{1,3})[-\s]+(\d{1,6})', t)
    if m:
        return 'ucac4', int(m.group(1)) * 1_000_000 + int(m.group(2))
    m = re.fullmatch(r'TYC\s*(\d{1,4})[-\s]+(\d{1,5})[-\s]+(\d)', t)
    if m:
        return 'tyc', (int(m.group(1)) * 20000 + int(m.group(2))) * 4 + int(m.group(3))
    m = re.fullmatch(r'HIP\s*(\d{1,6})', t)
    if m:
        return 'hip', int(m.group(1))
    return None


def star_entry(s: dict) -> dict:
    """A cone() row as a named star: {'name': TYC, else UCAC4, else Gaia DR3, 'ids', 'gmag',
    'sep_arcsec', 'ra', 'dec'}."""
    ids = {'Gaia DR3': f"Gaia DR3 {s['source_id']}"}
    if s.get('tyc'):
        ids['TYC'] = tyc_name(s['tyc'])
    if s.get('ucac4'):
        ids['UCAC4'] = ucac4_name(s['ucac4'])
    if s.get('hip'):
        ids['HIP'] = f"HIP {s['hip']}"
    name = ids.get('TYC') or ids.get('UCAC4') or ids['Gaia DR3']
    return {'name': name, 'ids': ids, 'gmag': s['g'], 'sep_arcsec': s.get('sep_arcsec'), 'ra': s['ra'],
            'dec': s['dec']}


def _manifest(d) -> dict:
    with open(os.path.join(d, 'catalog.json')) as f:
        return json.load(f)


def is_catalog(d) -> bool:
    try:
        return _manifest(d).get('format') == 2 and os.path.isfile(os.path.join(d, 'core.npy'))
    except (OSError, ValueError):
        return False


def has_xref(d) -> bool:
    """xref part present and from the same build as the core."""
    try:
        man = _manifest(d)
        with open(os.path.join(d, 'xref.json')) as f:
            x = json.load(f)
    except (OSError, ValueError):
        return False
    return (x.get('build_id') == man.get('build_id') and x.get('n_stars') == man.get('n_stars')
            and os.path.isfile(os.path.join(d, 'xref.npy')))


def find_catalog(cfg: Optional[dict] = None) -> Optional[str]:
    """The catalogue folder to use, or None (see the module doc)."""
    from ..config import load, resolve_path
    cfg = cfg if cfg is not None else load()
    explicit = (cfg.get('catalog') or {}).get('gaia_dir') or os.environ.get('PYSYZYGOS_CATALOG')
    if explicit:
        d = str(resolve_path(cfg, explicit))
        return d if is_catalog(d) else None
    sites = cfg.get('pysyzygos_sites') or cfg.get('pyoccult_sites')
    if not sites:
        return None
    home = Path(resolve_path(cfg, sites)).parent
    found = [str(d) for d in sorted(home.iterdir()) if d.is_dir() and is_catalog(d)] if home.is_dir() else []
    found.sort(key=lambda d: (not has_xref(d), -float(_manifest(d).get('gmax', 0))))
    return found[0] if found else None


@functools.lru_cache(maxsize=2)
def open_catalog(d: str) -> 'LocalGaia':
    return LocalGaia(d)


def local_catalog(cfg: Optional[dict] = None) -> Optional['LocalGaia']:
    """The local catalogue from the config, opened once per process; None if there is none."""
    d = find_catalog(cfg)
    return open_catalog(d) if d else None


class LocalGaia:
    def __init__(self, d: str):
        self.dir = d
        self.man = _manifest(d)
        self.gmax = float(self.man['gmax'])
        self.offsets = np.load(os.path.join(d, 'offsets.npy'), mmap_mode='r')
        self.core = np.load(os.path.join(d, 'core.npy'), mmap_mode='r')
        self.xref = np.load(os.path.join(d, 'xref.npy'), mmap_mode='r') if has_xref(d) else None

    def _cells(self, ra: float, dec: float, radius_deg: float) -> list[int]:
        out = []
        d0 = max(0, int(math.floor((dec - radius_deg + 90.0) / CELL)))
        d1 = min(N_DEC - 1, int(math.floor((dec + radius_deg + 90.0) / CELL)))
        for di in range(d0, d1 + 1):
            band_dec = max(abs(di * CELL - 90.0), abs((di + 1) * CELL - 90.0))   # pole side of the band
            cosd = math.cos(math.radians(min(band_dec, 89.999)))
            half = radius_deg / cosd
            if half >= 180.0 or band_dec >= 89.999:
                out.extend(di * N_RA + r for r in range(N_RA))
                continue
            r0 = int(math.floor((ra - half) / CELL))
            r1 = int(math.floor((ra + half) / CELL))
            out.extend(di * N_RA + (r % N_RA) for r in range(r0, r1 + 1))
        return sorted(set(out))

    def cone(self, ra: float, dec: float, radius_arcsec: float, epoch: Optional[float] = None) -> list[dict]:
        """Stars within the radius at `epoch` (decimal year; default now; proper motion applied),
        nearest first: {'source_id', 'ra', 'dec', 'g', 'sep_arcsec', 'ucac4', 'tyc', 'hip'}."""
        if epoch is None:
            now = datetime.now(timezone.utc)
            epoch = now.year + (now.timetuple().tm_yday - 0.5) / 365.25
        dt = epoch - GAIA_EPOCH
        r_deg = radius_arcsec / 3600.0
        pad = r_deg + abs(dt) * 10.0 / 3600.0          # stars moving up to 10"/yr come into the cone
        rows = []
        for c in self._cells(ra, dec, pad):
            lo, hi = int(self.offsets[c]), int(self.offsets[c + 1])
            if hi > lo:
                rows.append((c, lo, hi))
        if not rows:
            return []
        idx = np.concatenate([np.arange(lo, hi) for _, lo, hi in rows])
        cell = np.concatenate([np.full(hi - lo, c) for c, lo, hi in rows])
        s = np.asarray(self.core[idx])
        sra = (cell % N_RA) * CELL + s['dra'] * SCALE
        sdec = (cell // N_RA) * CELL - 90.0 + s['ddec'] * SCALE
        pmra = np.nan_to_num(s['pmra'].astype(float))      # mas/yr, includes cos(dec)
        pmdec = np.nan_to_num(s['pmdec'].astype(float))
        sdec = sdec + pmdec * dt / 3.6e6
        sra = sra + pmra * dt / 3.6e6 / np.cos(np.radians(sdec))
        cosd = math.cos(math.radians(dec))
        dra = ((sra - ra + 180.0) % 360.0 - 180.0) * cosd
        sep = np.hypot(dra, sdec - dec) * 3600.0
        keep = np.flatnonzero(sep <= radius_arcsec)
        keep = keep[np.argsort(sep[keep])]
        x = np.asarray(self.xref[idx[keep]]) if self.xref is not None else None
        out = []
        for k, i in enumerate(keep):
            g = int(s['g'][i])
            out.append({'source_id': int(s['source_id'][i]), 'ra': float(sra[i]), 'dec': float(sdec[i]),
                        'g': None if g == G_MMAG_NONE else g / 1000.0, 'sep_arcsec': float(sep[i]),
                        'ucac4': int(x['ucac4'][k]) if x is not None else 0,
                        'tyc': int(x['tyc'][k]) if x is not None else 0,
                        'hip': int(x['hip'][k]) if x is not None else 0})
        return out

    def lookup(self, ra: float, dec: float, radius_arcsec: float = 10.0,
               epoch: Optional[float] = None) -> Optional[dict]:
        """The star at a measured position: nearest of those within 2 mag of the brightest in
        the radius (a measured centre near a bright star is that star, not a faint neighbour).
        {'name': TYC, else UCAC4, else Gaia DR3, 'ids', 'gmag', 'sep_arcsec', 'ra', 'dec'} or None
        (ra, dec: the star at `epoch`)."""
        stars = [s for s in self.cone(ra, dec, radius_arcsec, epoch) if s['g'] is not None]
        if not stars:
            return None
        bright = min(s['g'] for s in stars)
        return star_entry(next(s for s in stars if s['g'] <= bright + 2.0))

    def find(self, text: str, ra: float, dec: float, radius_arcsec: float = 2400.0,
             epoch: Optional[float] = None) -> Optional[dict]:
        """The star named `text` (parse_star_id) within the radius of (ra, dec), e.g. the plate
        solution's field: star_entry at `epoch`, or None (not in the field, fainter than the
        catalogue, or a UCAC4/TYC/HIP name without the xref part)."""
        key = parse_star_id(text)
        if key is None or (key[0] != 'source_id' and self.xref is None):
            return None
        hit = next((s for s in self.cone(ra, dec, radius_arcsec, epoch) if s[key[0]] == key[1]), None)
        return star_entry(hit) if hit else None
