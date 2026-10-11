"""User configuration: a private TOML file created from the packaged template.

    pystarlight-config init      # write ~/.config/pystarlight/config.toml (mode 600)
    pystarlight-config path      # show which file is in use
    pystarlight-config show      # print the settings, secrets masked

    from pystarlight.config import load
    cfg = load()
    cfg['astrometry']['api_key']
"""

from __future__ import annotations

import copy
import os
import stat
import sys
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

TEMPLATE = Path(__file__).with_name('config_template.toml')
SECRET_KEYS = {'api_key', 'password', 'token'}


def user_config_path() -> Path:
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base / 'pystarlight' / 'config.toml'


def find_config() -> Optional[Path]:
    """$PYSTARLIGHT_CONFIG, ./pystarlight.toml, then the user config file."""
    env = os.environ.get('PYSTARLIGHT_CONFIG')
    for p in (Path(env) if env else None, Path.cwd() / 'pystarlight.toml', user_config_path()):
        if p is not None and p.is_file():
            return p
    return None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def defaults() -> dict:
    with open(TEMPLATE, 'rb') as f:
        return tomllib.load(f)


def load(path: Optional[str] = None) -> dict:
    """Template defaults overlaid with the active config file (if any)."""
    cfg = defaults()
    p = Path(path) if path else find_config()
    if p is not None:
        with open(p, 'rb') as f:
            cfg = _merge(cfg, tomllib.load(f))
        cfg['_path'] = str(p)
    return cfg


SITE_KEYS = ('name', 'lat', 'lon', 'ele', 'aperture_cm', 'focal_mm', 'sensor_mm', 'mpc_code')


def read_pyoccult_sites(path: str) -> tuple[dict, Optional[str]]:
    """`sites` dict and `default_site` from a PyOccult sites.py, read as data (never executed)."""
    import ast
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ('sites', 'default_site'):
                found[node.targets[0].id] = ast.literal_eval(node.value)
    return found.get('sites', {}), found.get('default_site')


def sites(cfg: dict) -> tuple[dict, Optional[str]]:
    """All sites: those from the PyOccult file (if configured), overridden by [sites.*] tables.
    Returns (sites, default site id). Site keys follow PyOccult: lat, lon, ele (m), name,
    aperture_cm, focal_mm, sensor_mm (width, height); PyStarLight adds binning, camera."""
    out, default = {}, None
    py = cfg.get('pyoccult_sites') or ''
    if py and Path(py).expanduser().is_file():
        out, default = read_pyoccult_sites(str(Path(py).expanduser()))
        out = {k: dict(v) for k, v in out.items()}
    for k, v in cfg.get('sites', {}).items():
        out[k] = {**out.get(k, {}), **v}
    default = cfg.get('default_site') or default
    return out, default


def site(cfg: dict, name: Optional[str] = None) -> Optional[dict]:
    """The named site, else $PYOCCULT_SITE, else the default; None if there is none."""
    all_sites, default = sites(cfg)
    key = name or os.environ.get('PYOCCULT_SITE') or default
    if key is None or key not in all_sites:
        return None
    return {'id': key, **all_sites[key]}


def init(path: Optional[str] = None, overwrite: bool = False) -> Path:
    """Copy the template to the user config file, readable by the owner only."""
    p = Path(path) if path else user_config_path()
    if p.exists() and not overwrite:
        raise FileExistsError(f'{p} exists (use --force to replace it)')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE.read_text())
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return p


def masked(cfg: dict) -> dict:
    """Copy with secrets replaced by '***' (for printing / logs)."""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out[k] = masked(v)
        else:
            out[k] = '***' if (k in SECRET_KEYS and v) else v
    return out


def main(argv=None):
    import argparse
    import pprint
    ap = argparse.ArgumentParser(prog='pystarlight-config', description='Manage the PyStarLight config file.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('init', help='create the private config from the template')
    i.add_argument('--path', help='where to write it (default: user config dir)')
    i.add_argument('--force', action='store_true', help='replace an existing file')
    sub.add_parser('path', help='show which config file is in use')
    sub.add_parser('show', help='print settings (secrets masked)')
    sub.add_parser('sites', help='list observing sites (incl. PyOccult sites.py)')
    a = ap.parse_args(argv)
    if a.cmd == 'init':
        print(f'wrote {init(a.path, a.force)}  (edit it; it is private to you)')
    elif a.cmd == 'path':
        print(find_config() or f'no config file; defaults only. Create one: pystarlight-config init '
                                f'(-> {user_config_path()})')
    elif a.cmd == 'sites':
        all_sites, default = sites(load())
        for k, v in all_sites.items():
            mark = '*' if k == default else ' '
            print(f"{mark} {k:30s} lat {v.get('lat', 0):9.5f} lon {v.get('lon', 0):10.5f} "
                  f"focal {v.get('focal_mm', '?')} mm  sensor {v.get('sensor_mm', '?')} mm")
    else:
        pprint.pprint(masked(load()), sort_dicts=False)


if __name__ == '__main__':
    sys.exit(main())
