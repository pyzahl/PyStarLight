"""WCS: two-star solution and WCS-file target placement against a known TAN projection."""

import os
from pathlib import Path

import numpy as np
import pytest
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

from pystarlight.wcs import load_wcs, prepare_for_astrometry, sky_coord, solve_two_stars, target_pixel


def make_wcs(scale_arcsec=2.0, rot_deg=23.0, crval=(273.0, 22.0), crpix=(360, 240), mirrored=False):
    """Pixel y is the row index (down). Non-mirrored sky: east left with north up."""
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = crval
    w.wcs.crpix = crpix
    s = scale_arcsec / 3600
    r = np.radians(rot_deg)
    rot = np.array([[np.cos(r), -np.sin(r)], [np.sin(r), np.cos(r)]])
    w.wcs.cd = np.diag([-s, s if mirrored else -s]) @ rot
    return w


@pytest.mark.parametrize('mirrored', [False, True])
@pytest.mark.parametrize('rot', [0.0, 23.0, 160.0, -75.0])
def test_two_star_solution(rot, mirrored):
    w = make_wcs(rot_deg=rot, mirrored=mirrored)
    px = [(120.0, 80.0), (600.0, 400.0), (350.0, 260.0)]
    sky = [SkyCoord.from_pixel(x, y, w) for x, y in px]
    sol = solve_two_stars((sky[0], *px[0]), (sky[1], *px[1]), sky[2], mirrored=mirrored)
    assert sol.plate_scale == pytest.approx(2.0, rel=1e-3)
    # the flat two-star model is good to a fraction of a pixel over a small field
    assert sol.x == pytest.approx(px[2][0], abs=0.5)
    assert sol.y == pytest.approx(px[2][1], abs=0.5)


def test_two_star_wrong_parity_is_far_off():
    """Documents the PyMovie limitation: a mirrored image solved as non-mirrored."""
    w = make_wcs(mirrored=True)
    px = [(120.0, 80.0), (600.0, 400.0), (350.0, 260.0)]
    sky = [SkyCoord.from_pixel(x, y, w) for x, y in px]
    sol = solve_two_stars((sky[0], *px[0]), (sky[1], *px[1]), sky[2])
    assert np.hypot(sol.x - 350, sol.y - 260) > 20


def test_target_pixel_from_wcs_file(tmp_path):
    from astropy.io import fits
    w = make_wcs()
    path = tmp_path / 'wcs.fit'
    fits.PrimaryHDU(header=w.to_header()).writeto(path)
    target = SkyCoord.from_pixel(410.3, 199.7, w)
    x, y = target_pixel(load_wcs(str(path)), target)
    assert (x, y) == pytest.approx((410.3, 199.7), abs=1e-6)
    # solution computed on an image resized for 0.9 W/H pixels maps back to original x
    x2, _ = target_pixel(load_wcs(str(path)), target, pixel_aspect=0.9)
    assert x2 == pytest.approx(410.3 / 0.9)


def test_prepare_for_astrometry():
    rng = np.random.default_rng(0)
    img = rng.normal(300, 5, (480, 720)).astype(np.uint16)
    img[:20] = 4000
    out = prepare_for_astrometry(img, redact_top=20, pixel_aspect=0.9)
    assert out.shape == (480, 648)
    assert out[:20].max() < 400
    assert img[:20].min() == 4000          # input untouched


def test_sky_coord_formats():
    a = sky_coord('18h12m00s +22d00m00s')
    b = sky_coord('273.0 22.0')
    assert a.separation(b).arcsec < 1e-6


@pytest.mark.parametrize('mirrored', [False, True])
@pytest.mark.parametrize('rot', [0.0, 30.0, -88.7, 170.0])
def test_orientation(rot, mirrored):
    from pystarlight.wcs import orientation
    w = make_wcs(scale_arcsec=2.07, rot_deg=rot, mirrored=mirrored, crpix=(361, 241))
    o = orientation(w, (481, 721))
    assert o.scale_arcsec == pytest.approx(2.07, rel=1e-3)
    assert o.mirrored is mirrored
    if not mirrored:                    # north angle of a plain rotation equals the rotation
        assert (o.north_deg - rot + 180) % 360 - 180 == pytest.approx(0, abs=0.05)
    assert (o.center_ra, o.center_dec) == pytest.approx((273.0, 22.0), abs=1e-6)


def test_plate_scale():
    from pystarlight.wcs import plate_scale
    assert plate_scale(360, 5.02, 1456) == pytest.approx(1.975, abs=0.002)   # ASTRID IMX296 @ 360 mm


def test_pysyzygos_sites(tmp_path, monkeypatch):
    from pystarlight import config
    f = tmp_path / 'sites.py'
    f.write_text("import os\nos.system('echo never runs')\n"
                 "sites = {'home': {'lat': 1.0, 'lon': 2.0, 'ele': 3.0, 'focal_mm': 360.0,"
                 " 'sensor_mm': (5.6, 3.2)}}\ndefault_site = 'home'\n")
    c = tmp_path / 'c.toml'
    for key in ('pysyzygos_sites', 'pyoccult_sites'):               # old key still read
        c.write_text(f'{key} = "{f}"\n[sites.home]\nsensor_mm = [5.02, 3.75]\n')
        monkeypatch.setenv('PYSTARLIGHT_CONFIG', str(c))
        s = config.site(config.load())
        assert s['id'] == 'home' and s['lat'] == 1.0 and s['sensor_mm'] == (5.6, 3.2)   # [sites.*]: ignored
    f.write_text("sites = {'home': {'lat': 1.0, 'lon': 2.0}, 'away': {'lat': 3.0, 'lon': 4.0}}\n")
    c.write_text(f'pysyzygos_sites = "{f}"\n')
    for env in ('PYSYZYGOS_SITE', 'PYOCCULT_SITE'):
        monkeypatch.setenv(env, 'away')
        assert config.site(config.load())['id'] == 'away'
        monkeypatch.delenv(env)


def test_stamp_rows_finds_burnt_in_timestamp():
    from pystarlight.wcs.solve import stamp_rows
    rng = np.random.default_rng(3)
    img = rng.normal(760, 20, (620, 816))
    y, x = np.indices(img.shape)
    for cy, cx in [(300, 400), (100, 700), (590, 50)]:          # stars, one near the bottom
        img += 30000 * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / 8)
    assert stamp_rows(img) == (0, 0)
    img[604:619] = 0                                            # black strip with saturated text
    img[606:616, ::3] = 65520
    img[619, :300] = 0
    assert stamp_rows(img) == (0, 17)
    img[:12] = 0
    assert stamp_rows(img)[0] == 13


def test_movie_hints_from_adv_metadata():
    from pystarlight.wcs.solve import movie_hints
    home = {'lat': 40.95497, 'lon': -72.92809}
    meta = {'RA': '6.623745', 'DEC': '38.736677', 'LATITUDE': '46.9924869', 'LONGITUDE': '9.6401519'}
    h = movie_hints(meta, home)
    assert h['center'] == pytest.approx((99.356175, 38.736677))
    assert h['other_site']                                      # someone else's optics: no scale hint
    assert not movie_hints({'LATITUDE': '40.96', 'LONGITUDE': '-72.93'}, home)['other_site']
    assert movie_hints({}, home) == {'center': None, 'other_site': False, 'scale': None}


@pytest.mark.parametrize('opts, scale', [({}, None), ({'scale_arcsec': 1.19}, 1.19),
                                         ({'scale_hint': False}, None), ({'_fallback': True}, 'fallback'),
                                         ({'_fallback': True, '_away': True}, None)])
def test_solve_movie_hint_options(tmp_path, monkeypatch, opts, scale):
    import sys

    from pystarlight.config import load
    from pystarlight.wcs import solve
    sys.path.insert(0, str(Path(__file__).parent))
    from test_pipeline import render, write_ser
    p = tmp_path / 'm.ser'
    write_ser(p, [render(i, np.random.default_rng(0)) for i in range(4)])
    sent = {}

    def fake(img, **kw):
        sent.update(kw)
        raise RuntimeError('stop here')
    monkeypatch.setattr(solve, 'solve_astrometry_net', fake)
    cfg = load()
    opts = dict(opts)
    fallback, away = opts.pop('_fallback', False), opts.pop('_away', False)
    cfg['astrometry'].update(opts)
    if fallback:                    # a video without its optics: [fallback_equipment]
        cfg['fallback_equipment'] = {'focal_length_mm': 360.0, 'pixel_um': 3.45, 'binning': 1}
    cfg['observer'].update(latitude=40.0, longitude=-73.0)          # the station's position
    if away:                        # recorded elsewhere: someone else's optics, no hint
        monkeypatch.setattr(solve, 'movie_hints', lambda m, home: {'center': None, 'other_site': True,
                                                                   'scale': None})
    with pytest.raises(RuntimeError, match='stop here'):
        solve.solve_movie(str(p), cfg, log=lambda m: None)
    expect = 206.265 * 3.45 / 360.0 if scale == 'fallback' else scale
    assert sent['scale_arcsec'] == (pytest.approx(expect) if expect else None)


# ---------------------------------------------------------------------------- local solve-field

def fake_solve_field(tmp_path, monkeypatch, wcs_file=None):
    """A 'solve-field' on PATH that records its arguments and writes `wcs_file` to --wcs
    (none: prints 'Did not solve'). The astrometry.net config points at one fake index file."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    args_file = tmp_path / 'args.txt'
    script = bin_dir / 'solve-field'
    script.write_text(f'''#!/bin/sh
echo "$@" > {args_file}
while [ $# -gt 0 ]; do [ "$1" = "--wcs" ] && out="$2"; shift; done
''' + (f'cp {wcs_file} "$out"\necho "Field center: (RA,Dec) = (273.0, 22.0) deg."\n' if wcs_file
       else 'echo "Did not solve (or no WCS file was written)."\n'))
    script.chmod(0o755)
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}' + os.environ['PATH'])
    idx = tmp_path / 'index'
    idx.mkdir()
    (idx / 'index-4208.fits').write_bytes(b'')
    cfg = tmp_path / 'astrometry.cfg'
    cfg.write_text(f'cpulimit 300\nadd_path {idx}\nautoindex\n')
    from pystarlight.wcs import solve
    monkeypatch.setattr(solve, 'ASTROMETRY_CFG', str(cfg))
    return args_file


def test_local_solver_used_and_given_hints(tmp_path, monkeypatch):
    from astropy.io import fits

    from pystarlight.wcs import solve
    truth = tmp_path / 'truth.wcs'
    fits.PrimaryHDU(header=make_wcs().to_header()).writeto(truth)
    args_file = fake_solve_field(tmp_path, monkeypatch, truth)
    assert solve.local_solver_available()
    logs = []
    path, job = solve.solve_astrometry_net(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'),
                                           scale_arcsec=2.0, scale_err_pct=10, center=(273.0, 22.0),
                                           downsample=2, return_job=True, log=logs.append)
    assert job is None and Path(path).exists()               # local: no astrometry.net job
    w = load_wcs(path)
    assert np.allclose(w.wcs.crval, [273.0, 22.0])
    args = args_file.read_text().split()
    assert args[args.index('--scale-low') + 1] == '1.8000'
    assert args[args.index('--scale-high') + 1] == '2.2000'
    assert args[args.index('--ra') + 1] == '273.000000' and args[args.index('--downsample') + 1] == '2'
    assert any('Field center' in m for m in logs)


def test_local_solver_failure_and_nova_choice(tmp_path, monkeypatch):
    from pystarlight.wcs import solve
    fake_solve_field(tmp_path, monkeypatch, None)
    with pytest.raises(RuntimeError, match='could not solve'):
        solve.solve_astrometry_net(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'),
                                   log=lambda m: None)
    # solver = nova never runs solve-field (and needs the API key, none here)
    with pytest.raises(ValueError, match='API key'):
        solve.solve_astrometry_net(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'),
                                   solver='nova', log=lambda m: None)


def test_astrometry_cfg_next_to_solve_field(tmp_path, monkeypatch):
    """Homebrew (macOS): <prefix>/bin/solve-field reads <prefix>/etc/astrometry.cfg."""
    from pystarlight.wcs import solve
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'etc').mkdir()
    exe = tmp_path / 'bin' / 'solve-field'
    exe.write_text('#!/bin/sh\n')
    exe.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    monkeypatch.setattr(solve, 'ASTROMETRY_CFG', str(tmp_path / 'missing.cfg'))
    (tmp_path / 'etc' / 'astrometry.cfg').write_text('add_path /x\n')
    assert solve.astrometry_cfg() == str(tmp_path / 'etc' / 'astrometry.cfg')
    assert solve.local_index_dirs() == ['/x']


def fake_astap(tmp_path, monkeypatch, wcs=None, exit_code=0):
    """An 'astap_cli' in a fake ASTAP folder with one star database file: records its arguments
    and writes <-o>.ini (PLTSOLVD) and <-o>.wcs (80-byte cards, no line ends, as ASTAP does) from
    `wcs`; without `wcs` it fails as ASTAP does (exit 1, PLTSOLVD=F, ERROR=...)."""
    import sys
    d = tmp_path / 'astap'
    d.mkdir()
    (d / 'd50_0101.1476').write_bytes(b'')
    args_file = tmp_path / 'astap_args.txt'
    hdr_text = '' if wcs is None else ''.join(c.image for c in wcs.to_header().cards) + 'END'.ljust(80)
    script = d / 'astap_cli'
    script.write_text(f'''#!{sys.executable}
import sys
a = sys.argv[1:]
open({str(args_file)!r}, 'w').write(' '.join(a))
base = a[a.index('-o') + 1]
ok = {wcs is not None!r}
with open(base + '.ini', 'w') as f:
    f.write('PLTSOLVD=T\\n' if ok else 'PLTSOLVD=F\\nERROR=No solution found!\\n')
if ok:
    open(base + '.wcs', 'w').write({hdr_text!r})
sys.exit(0 if ok else 1)
''')
    script.chmod(0o755)
    from pystarlight.wcs import solve
    monkeypatch.setattr(solve, 'ASTAP_NAMES', ('astap_cli',))
    monkeypatch.setattr(solve, 'ASTAP_DIRS', (str(d),))
    return args_file


def test_astap_found_with_its_database(tmp_path, monkeypatch):
    from pystarlight.wcs import solve
    assert not solve.astap_available()                         # conftest: no real ASTAP here
    fake_astap(tmp_path, monkeypatch, make_wcs())
    assert solve.astap_executable().endswith('astap_cli')
    assert solve.astap_databases() == {'d50': str(tmp_path / 'astap')}
    assert solve.astap_available() and solve.uses_local_solver({'solver': 'auto'})


def test_solver_order_auto_and_explicit(tmp_path, monkeypatch):
    from pystarlight.wcs import solve
    monkeypatch.setattr(solve, 'local_solver_available', lambda: False)   # (this computer may have one)
    assert solve.solver_order({'solver': 'auto'}) == ['nova']                   # nothing installed
    fake_astap(tmp_path, monkeypatch, make_wcs())
    assert solve.solver_order({'solver': 'auto'}, scale=2.0) == ['astap']
    assert solve.solver_order({'solver': 'auto'}) == ['nova']                   # blind: never ASTAP
    monkeypatch.setattr(solve, 'local_solver_available', lambda: True)
    assert solve.solver_order({'solver': 'auto'}, center=(1.0, 2.0), scale=2.0) == ['astap', 'local']
    assert solve.solver_order({'solver': 'auto'}, center=(1.0, 2.0)) == ['astap', 'local']   # no scale
    assert solve.solver_order({'solver': 'auto'}, scale=2.0) == ['local', 'astap']           # no position
    assert solve.solver_order({'solver': 'auto'}) == ['local']                  # blind: astrometry.net
    assert solve.solver_order({'solver': 'astap'}) == ['astap']
    assert solve.solver_order({'solver': 'astrometry'}) == ['local']
    assert solve.solver_order({'solver': 'nova'}) == ['nova']


def test_astap_used_with_hints_and_wcs_read(tmp_path, monkeypatch):
    from pystarlight.wcs import solve
    truth = make_wcs(scale_arcsec=2.07, crval=(310.36, 45.1))
    args_file = fake_astap(tmp_path, monkeypatch, truth)
    logs = []
    path = solve.solve_astrometry_net(np.random.default_rng(1).normal(100, 5, (240, 320)).astype(np.float32),
                                      workdir=str(tmp_path / 'w'), scale_arcsec=2.07, center=(310.36, 45.1),
                                      radius_deg=3.0, log=logs.append)
    args = args_file.read_text().split()
    assert args[args.index('-ra') + 1] == f'{310.36 / 15:.6f}'                # hours
    assert args[args.index('-spd') + 1] == f'{45.1 + 90:.6f}'                 # south pole distance
    assert float(args[args.index('-fov') + 1]) == pytest.approx(2.07 * 240 / 3600, abs=1e-4)   # height
    assert args[args.index('-r') + 1] == '3' and '-wcs' in args
    w = solve.load_wcs(path)
    ra, dec = w.wcs_pix2world([[360, 240]], 0)[0]
    tra, tdec = truth.wcs_pix2world([[360, 240]], 0)[0]
    assert ra == pytest.approx(tra, abs=1e-6) and dec == pytest.approx(tdec, abs=1e-6)
    from astropy.io import fits
    h = fits.getheader(path)
    assert h['SOLVER'] == 'ASTAP' and (h['IMAGEW'], h['IMAGEH']) == (320, 240)   # as astrometry.net's
    assert any('ASTAP' in m for m in logs)


def test_astap_failure_falls_back_to_astrometry_net(tmp_path, monkeypatch):
    from astropy.io import fits

    from pystarlight.wcs import solve
    fake_astap(tmp_path, monkeypatch, None)                                  # ASTAP: no solution
    truth = tmp_path / 'truth.wcs'
    fits.PrimaryHDU(header=make_wcs().to_header()).writeto(truth)
    fake_solve_field(tmp_path, monkeypatch, truth)
    logs = []
    path = solve.solve_astrometry_net(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'),
                                      center=(273.0, 22.0), scale_arcsec=2.0, log=logs.append)
    assert Path(path).exists()
    assert any('ASTAP did not solve it' in m and 'No solution' in m for m in logs)
    with pytest.raises(RuntimeError, match='ASTAP could not solve.*field heights'):
        solve.solve_astap(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w2'), center=(273.0, 22.0))


def test_astap_refuses_a_blind_search_without_a_scale(tmp_path, monkeypatch):
    """No position and no scale: ASTAP's scan found false solutions on real recordings."""
    from pystarlight.wcs import solve
    args_file = fake_astap(tmp_path, monkeypatch, make_wcs())
    with pytest.raises(RuntimeError, match='needs the position or the plate scale'):
        solve.solve_astap(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'))
    assert not args_file.exists()                                           # not even started


def test_astap_scans_field_heights_without_a_scale(tmp_path, monkeypatch):
    from pystarlight.wcs import solve
    fake_astap(tmp_path, monkeypatch, make_wcs(scale_arcsec=0.9 * 3600 / 48 * 1.1))   # 0.99 deg high
    script = tmp_path / 'astap' / 'astap_cli'          # solves only at a field height of 0.9 deg
    text = script.read_text().replace("ok = True", "ok = a[a.index('-fov') + 1] == '0.9000'")
    script.write_text(text)
    logs = []
    path = solve.solve_astap(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'), center=(273.0, 22.0),
                             log=logs.append)
    assert Path(path).exists()
    assert any('field height of 0.9 deg' in m for m in logs)


def test_astap_scan_rejects_a_solution_at_another_scale(tmp_path, monkeypatch):
    """A scan 'solution' whose own field height is far from the height tried is a false match."""
    from pystarlight.wcs import solve
    fake_astap(tmp_path, monkeypatch, make_wcs(scale_arcsec=0.1 * 3600 / 48 * 1.29))   # as seen on Fringilla
    script = tmp_path / 'astap' / 'astap_cli'          # 'solves' only when 0.1 deg is tried
    script.write_text(script.read_text().replace("ok = True", "ok = a[a.index('-fov') + 1] == '0.1000'"))
    with pytest.raises(RuntimeError, match='rejected'):
        solve.solve_astap(np.ones((48, 64), np.float32), workdir=str(tmp_path / 'w'), center=(273.0, 22.0),
                          log=lambda m: None)
