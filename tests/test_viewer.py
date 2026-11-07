"""Viewer and plots, driven headless (Agg backend)."""

import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

mpl = pytest.importorskip('matplotlib')
mpl.use('Agg')

from pystarlight import config as config_mod  # noqa: E402
from pystarlight import plot  # noqa: E402
from pystarlight.viewer import Viewer, load_apertures, save_apertures  # noqa: E402
from test_pipeline import STARS, render, truth, write_ser  # noqa: E402


def movie_shape(path):
    from pystarlight import open_movie
    with open_movie(path) as m:
        return m.shape


@pytest.fixture(autouse=True)
def no_local_solver(monkeypatch):
    """solve-field may be installed here: the viewer would plate-solve every test movie."""
    from pystarlight.wcs import solve
    monkeypatch.setattr(solve, 'local_solver_available', lambda: False)


@pytest.fixture
def movie(tmp_path):
    rng = np.random.default_rng(0)
    p = tmp_path / 'shake.ser'
    write_ser(p, [render(i, rng) for i in range(40)])
    from pystarlight.capture import ser_writer  # the sidecar as the capture writes it
    h, w = movie_shape(str(p))
    ser_writer.write_meta(str(p), w, h, 16, {
        'station': 'Test (T)', 'site_source': 'GPS', 'latitude': 46.99249, 'longitude': 9.64015,
        'latitude_text': '46.9924869', 'longitude_text': '9.6401519', 'gps_fix': '3D',
        'timing_accuracy_ns': 100000, 'timestamp_source': 'GPS trigger pulse, GPS UTC'})
    return p


def click(v, x, y, button=1):
    v.on_click(SimpleNamespace(inaxes=v.ax, xdata=x, ydata=y, button=button))


def key(v, k, x=None, y=None):
    v.on_key(SimpleNamespace(key=k, inaxes=v.ax, xdata=x, ydata=y))


def test_viewer_workflow(movie):
    v = Viewer(str(movie))
    assert 'UTC' in v.info.get_text()
    key(v, 'right')
    key(v, 'pagedown')
    assert v.index == 26
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0 + 3, r0 - 2)                 # sloppy click snaps onto the star
    click(v, c1, r1)
    click(v, 30, 30)
    assert len(v.apertures) == 3
    a = v.apertures[0]
    assert abs(a.center[0] - c0) <= 6 and abs(a.center[1] - r0) <= 6
    key(v, 'Y', c1, r1)
    assert v.apertures[1].color == 'yellow'
    click(v, 30, 30, button=3)
    assert len(v.apertures) == 2
    key(v, 't', c0, r0)
    key(v, 'r', c1, r1)
    assert [(x.name, x.role) for x in v.apertures] == [('target', 'target'), ('ref1', 'reference')]
    click(v, *STARS[2][1::-1])
    key(v, 't', *STARS[2][1::-1])                 # only one target: the old one is demoted
    assert [x.role for x in v.apertures] == ['', 'reference', 'target']
    key(v, 't', c0, r0)
    key(v, 'r', *STARS[2][1::-1])
    key(v, 's')
    assert [x.name for x in load_apertures(v.aperture_file)] == ['target', 'ref1', 'ref2']

    key(v, '1', c0, r0)
    key(v, '2', c1, r1)
    assert v.zoom_names == ['target', 'ref1']
    assert v.zoom_axes[0].get_title(loc='left').startswith('1: target')
    assert v.zoom_ims[0].get_array().shape == (51, 51)

    key(v, 'home')
    v.run()
    v._job.join()
    v.poll()
    assert 'wrote' in v.status.get_text(), v.status.get_text()
    assert movie.with_suffix('.csv').exists()
    assert movie.with_name('shake-summary.pdf').exists()
    assert movie.with_name('shake-summary-mag.pdf').exists()          # the same in magnitudes
    assert v._summary_mag_fig.axes[1].get_ylabel() == '\u0394mag to the references in mag'   # no catalogue
    assert v._summary_mag_fig.axes[1].yaxis_inverted()
    assert v.step_fit is not None and 'step fit: ' in v.status.get_text()
    assert movie.with_name('shake-mag-hist.pdf').exists() and v._hist_fig.axes[0].get_legend()
    ev = json.loads(movie.with_name('shake.event.json').read_text())
    assert ev['movie'] == movie.name and ev['target']['aperture'] == 'target'
    assert ev['references'] and all(r['role'] == 'reference' for r in ev['references'])
    assert ev['step_fit']['levels'] and 'transitions' in ev['step_fit'] and ev['notes']
    meta = ev['recording']['metadata']                    # the SER header and its sidecar, unchanged
    assert meta['STATION'] == 'Test (T)' and meta['LATITUDE-TEXT'] == '46.9924869' and meta['Observer'] == ''
    st = ev['recording']['station']
    assert st['format'] == 'SER' and st['station'] == 'Test (T)' and st['site']['source'] == 'GPS'
    assert st['integrity'] == 'ok' and meta['STATION-DIGEST'].startswith('sha256:')   # as recorded
    assert ev['recording']['site_source'] == 'GPS' and ev['recording']['gps_fix'] == '3D'
    assert st['timing']['accuracy_s'] == 1e-4 and ev['step_fit']['time_base']['time_base_accuracy_s'] == 1e-4
    assert ev['light_curve']['pyote_csv'] == 'shake.csv'
    assert ev['step_fit']['searched_start_utc'] and ev['step_fit']['searched_end_utc']
    assert all(e['time_base_accuracy_s'] == 1e-4 for e in ev['step_fit']['events'])
    key(v, 'e')                                           # no server in the test config: says so
    assert 'pysyzygos_observations' in v.job_status.get_text()
    assert 'PyMovie' in movie.with_suffix('.csv').read_text().splitlines()[0]            # for PyOTE
    assert 'roles: target=target' in movie.with_suffix('.pystarlight.csv').read_text().splitlines()[0]
    key(v, 'right')                              # zoom now follows the tracked position
    tx = v._track['target'][1][list(v._track['target'][0]).index(v.index)]
    assert f'({tx:.1f},' in v.zoom_axes[0].get_title(loc='left')


def test_plot_panels(movie):
    import matplotlib.pyplot as plt

    from pystarlight import Aperture, analyze, open_movie
    aps = [Aperture.centered_at('t', 160, 120), Aperture.centered_at('r', 80, 60, color='yellow')]
    with open_movie(str(movie)) as mv:
        lc = analyze(mv, aps)
        img = mv.read(0).image
    with plot.style():
        fig = plot.summary(lc, img, aps, 't', ['r'], window=9, bin_size=4)
        fig2, ax = plt.subplots()
        plot.timing(ax, lc)
    assert len(fig.axes) == 4
    from matplotlib.colors import to_hex
    raw, curve, drift = fig.axes[1], fig.axes[2], fig.axes[3]
    t, r = raw.get_lines()                          # target red, twice as wide, on top
    assert to_hex(t.get_color()) == plot.TARGET_COLOR and to_hex(r.get_color()) != plot.TARGET_COLOR
    assert t.get_linewidth() == 2 * r.get_linewidth() and t.get_zorder() > r.get_zorder()
    assert all(to_hex(ln.get_color()) == plot.TARGET_COLOR for ln in curve.get_lines() + drift.get_lines())
    for ax in (raw, curve, drift):                  # legends top left, opaque
        leg = ax.get_legend()
        assert leg._loc == 2 and leg.get_frame().get_alpha() == 1.0
    from pystarlight import normalize as nz
    mags, ok = nz.magnitudes({n: lc[n]['signal'] for n in ('t', 'r')}, ['r'], {'r': 10.0}, window=9)
    with plot.style():
        fig = plot.summary(lc, img, aps, 't', ['r'], window=9, bin_size=4, mags=mags, mag_label='G')
    raw, curve = fig.axes[1], fig.axes[2]
    assert ok and len(fig.axes) == 4 and raw.get_ylabel() == curve.get_ylabel() == 'G'
    assert raw.yaxis_inverted() and curve.yaxis_inverted()          # fainter is down
    assert np.allclose(raw.get_lines()[0].get_ydata(), mags['t'], equal_nan=True)
    from pystarlight import steps
    t = plot.seconds(lc)
    n = t.size
    flux = np.where((np.arange(n) >= n // 3) & (np.arange(n) < 2 * n // 3), 0.2, 1.0)
    flux = flux + np.random.default_rng(0).normal(0, 0.02, n)
    fit = steps.fit_steps(t, flux)
    assert [tr.kind for tr in fit.transitions] == ['D', 'R']
    with plot.style():
        fig = plot.summary(lc, img, aps, 't', ['r'], mags=mags, mag_label='G', steps=(fit, 10.0))
    curve = fig.axes[2]
    assert len(fig.axes) == 6                                       # + a close-up per transition
    assert fig.axes[4].get_title().startswith('D ') and fig.axes[5].get_title().startswith('R ')
    assert 'step fit' in [ln.get_label() for ln in curve.get_lines()]
    assert any(t.get_text().startswith('D ') for t in curve.texts)
    with plot.style():
        fig = plot.mag_histograms(lc, {**mags, 't': 10.0 - 2.5 * np.log10(np.clip(flux, 1e-3, None))},
                                  't', ['r'], fit, mag_label='G')
    top, below = fig.axes
    labels = [t.get_text() for t in top.get_legend().get_texts()]
    assert len(labels) == 3 and labels[1].startswith('t ')           # the target's 3 levels
    assert '11.7' in labels[1]                                       # the 20 % level: 1.75 mag fainter
    assert below.get_title(loc='left').startswith('References nearest')      # no check stars
    assert [t.get_text()[:3] for t in below.get_legend().get_texts()] == ['r: ']
    fig = plot.mag_histograms(lc, mags, 't', [], fit, checks=['r'])
    assert fig.axes[1].get_title(loc='left') == 'Check stars'
    env = plot.gaussian_envelope(np.random.default_rng(1).normal(5.0, 0.1, 2000), np.linspace(4, 6, 81))
    assert env[1] == pytest.approx(5.0, abs=0.01) and env[2] == pytest.approx(0.1, abs=0.01)


def test_viewer_shows_ra_dec(movie, tmp_path):
    from astropy.wcs import WCS

    from pystarlight.wcs import orientation
    v = Viewer(str(movie))
    assert v.wcs is None and 'x=' in v.ax.format_coord(10, 20)     # no key in test config: no solve
    import matplotlib.pyplot as plt
    assert 'k' not in plt.rcParams['keymap.xscale'] and 'r' not in plt.rcParams['keymap.home']
    assert 'f' in plt.rcParams['keymap.fullscreen']
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = (310.36, 45.1)
    w.wcs.crpix = (160, 120)
    w.wcs.cd = [[0, 2.07 / 3600], [2.07 / 3600, 0]]
    v.wcs, v.orient = w, orientation(w, v.image.shape)
    assert '+45:' in v.ax.format_coord(159, 119)
    v.draw_compass()


def test_live_zoom_during_run(movie):
    import time

    from pystarlight.viewer import readings_of, readout_text
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    key(v, 't', c0, r0)
    key(v, 'r', c1, r1)
    key(v, '1', c0, r0)
    v.run()
    shown = []
    while v._job.is_alive() or not shown:
        v._live_shown = 0.0                        # do not wait 0.5 s in the test
        v.poll()
        if v._live_frame is not None and v._live_frame not in shown:
            shown.append(v._live_frame)
            text = [m for m in v.zoom_marks[0] if hasattr(m, 'get_text')]
            assert any('fit FWHM' in t.get_text() or 'fit: no star' in t.get_text() for t in text)
        time.sleep(0.01)
        if not v._job.is_alive():
            break
    v.poll()
    assert shown and len(shown) == len(set(shown))   # each live frame drawn once
    key(v, 'right')                                   # stored readings after the run
    assert any('S ' in m.get_text() for m in v.zoom_marks[0] if hasattr(m, 'get_text'))
    # helpers on both modes' values
    assert 'bkg' in readout_text(readings_of({'signal': 5.0, 'bkavg': 1.0, 'bkstd': 0.5, 'mskpx': 9}), 3)
    assert 'faded' in readout_text({'signal': 1.0, 'fwhm': 3.0, 'peak_snr': 4, 'dimmed': 1, 'used': 0}, 3)


def test_stack_export_runs_on_main_thread(movie, monkeypatch):
    import threading

    from pystarlight import stacking

    main_thread = threading.get_ident()
    stack_thread = []
    export_thread = []
    result = object()

    def fake_stack(*args, **kwargs):
        stack_thread.append(threading.get_ident())
        return result

    def fake_export(*args, **kwargs):
        export_thread.append(threading.get_ident())
        return ['stack.svg']

    monkeypatch.setattr(stacking, 'stack_field', fake_stack)
    monkeypatch.setattr(stacking, 'save_field_stack', fake_export)
    v = Viewer(str(movie))
    v.all_stars = lambda: []
    v.say('wrote shake.csv and shake-summary (pdf)')
    v.start_stack(object())
    v._stack_job.join()

    assert stack_thread and stack_thread[0] != main_thread
    assert export_thread == []
    v._poll_stack()
    assert export_thread == [main_thread]
    assert v._stack_done == ['stack.svg']
    assert 'summary' in v.status.get_text()            # stacking reports on its own line
    assert 'stacked field: stack.svg' in v.job_status.get_text()


def test_display_levels_and_gamma(movie):
    v = Viewer(str(movie))
    assert v.levels_auto and v.gamma == 1.0
    a, b = v.levels.valmin, v.levels.valmax
    lo, hi = round(a + 0.1 * (b - a)), round(a + 0.6 * (b - a))
    v.levels.set_val((lo, hi))                       # user drags: manual, kept across frames
    assert not v.levels_auto and (v.im.norm.vmin, v.im.norm.vmax) == (lo, hi)
    key(v, 'right')
    assert (v.lo, v.hi) == (lo, hi)
    v.gamma_slider.set_val(0.5)
    assert v.im.norm.gamma == 0.5
    v.set_levels(100, 50000)                         # beyond the slider: range grows
    assert v.levels.valmax >= 50000
    v.auto_levels()
    assert v.levels_auto and v.levels.valmax < 50000
    v._per_frame_box.set_active(0)
    key(v, 'right')
    assert v.per_frame and v.levels_auto


def test_optimal_checkbox_switches_photometry(movie):
    v = Viewer(str(movie))
    assert v.cfg['analysis']['photometry'] == 'optimal'                  # the default: ticked
    assert v._optimal_box.get_status() == [True]
    v._optimal_box.set_active(0)
    assert v.cfg['analysis']['photometry'] == 'aperture' and 'circular aperture' in v.status.get_text()
    v._optimal_box.set_active(0)
    assert v.cfg['analysis']['photometry'] == 'optimal'


def test_aperture_size_controls_resize_boxes(movie):
    from pystarlight.tracking import Aperture

    v = Viewer(str(movie))
    aperture = Aperture.centered_at('test', 100, 100, size=v.size)
    v.apertures.append(aperture)
    center = aperture.center

    v.adjust_aperture_size(2)
    assert v.size == aperture.size == 53
    assert aperture.center == center
    assert aperture.default_mask.shape == (53, 53)
    assert v.aperture_size_text.get_text() == 'box 53px'

    v.adjust_aperture_size(-2)
    assert v.size == aperture.size == 51
    assert aperture.center == center


def test_viewer_defaults_zoom_previews_to_target_and_ref1(movie):
    from pystarlight.tracking import Aperture

    apertures = [Aperture.centered_at('target', 100, 100),
                 Aperture.centered_at('ref1', 160, 120, color='yellow')]
    save_apertures(movie.with_suffix('.apertures.json'), apertures)
    v = Viewer(str(movie))

    assert v.zoom_names == ['target', 'ref1']
    assert v.zoom_axes[0].get_title(loc='left').startswith('1: target')
    assert v.zoom_axes[1].get_title(loc='left').startswith('2: ref1')


def test_target_preview_falls_back_to_red_aperture(movie):
    from pystarlight.tracking import Aperture

    x0, y0 = truth(0, 0)
    x1, y1 = truth(0, 1)
    apertures = [Aperture.centered_at('ap1', round(x0), round(y0), color='red'),
                 Aperture.centered_at('ref1', round(x1), round(y1), color='yellow')]
    save_apertures(movie.with_suffix('.apertures.json'), apertures)
    v = Viewer(str(movie))

    assert v.zoom_names == ['target', 'ref1']
    assert v.zoom_axes[0].get_title(loc='left').startswith('1: ap1')
    assert v.zoom_ims[0].get_array().size > 4
    assert v.zoom_axes[1].get_title(loc='left').startswith('2: ref1')


def test_default_previews_activate_when_roles_are_assigned(movie):
    v = Viewer(str(movie))
    assert v.zoom_names == ['target', 'ref1']

    x, y = truth(0, 0)
    v.add(x, y)
    v.set_role(x, y, 'target')
    assert v.zoom_axes[0].get_title(loc='left').startswith('1: target')

    x, y = truth(0, 1)
    v.add(x, y)
    v.set_role(x, y, 'reference')
    assert v.zoom_axes[1].get_title(loc='left').startswith('2: ref1')


def test_auto_center_individual_and_all_apertures(movie):
    from pystarlight.tracking import Aperture

    v = Viewer(str(movie))
    col0, row0 = truth(0, 0)
    col1, row1 = truth(0, 1)
    target = Aperture.centered_at('target', col0 + 4, row0 - 3, role='target')
    reference = Aperture.centered_at('ref1', col1 - 4, row1 + 3, role='reference')
    sky = Aperture.centered_at('sky1', 30, 30, role='sky')
    v.apertures = [target, reference, sky]
    reference_before = reference.center
    sky_before = sky.center

    v.center_nearest_aperture(*target.center)
    assert np.hypot(target.center[0] - col0, target.center[1] - row0) < 1
    assert reference.center == reference_before

    v.center_all_apertures()
    assert np.hypot(reference.center[0] - col1, reference.center[1] - row1) < 1
    assert sky.center == sky_before


def test_playback_advances_one_frame_and_toggles(movie):
    v = Viewer(str(movie))
    labels = [button.label.get_text() for button in v._buttons]
    assert labels[3:6] == ['<', 'Play', '>']
    left = min(button.ax.get_position().x0 for button in v._buttons)
    right = max(button.ax.get_position().x1 for button in v._buttons)
    image_center = v.ax.get_position().x0 + v.ax.get_position().width / 2
    assert (left + right) / 2 == pytest.approx(image_center)
    center_box = v._center_all_button.ax.get_position()
    assert center_box.x0 >= v.zoom_axes[0].get_position().x0
    assert center_box.x1 <= v.zoom_axes[0].get_position().x1
    assert center_box.y0 > v.zoom_axes[0].get_position().y1 + 0.035
    assert center_box.y1 < v._size_plus.ax.get_position().y0
    assert v._play_timer.interval > 0
    assert v._play_button.label.get_text() == 'Play'

    v.toggle_playback()
    assert v._playing
    assert v._play_button.label.get_text() == 'Pause'
    v.playback_step()
    assert v.index == 1

    v.toggle_playback()
    assert not v._playing
    assert v._play_button.label.get_text() == 'Play'

    v.go(v.n - 1)
    v.toggle_playback()
    assert v.index == 0
    v.playback_step()
    assert v.index == 1


def test_display_defaults_from_config(movie, monkeypatch, tmp_path):
    f = tmp_path / 'c.toml'
    f.write_text('[display]\nlevels = [320, 700]\ngamma = 0.7\n')
    monkeypatch.setenv('PYSTARLIGHT_CONFIG', str(f))
    v = Viewer(str(movie))
    assert not v.levels_auto and (v.lo, v.hi) == (320, 700) and v.im.norm.gamma == 0.7


def test_fit_frame_button_fits_without_moving(movie):
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    click(v, 30, 30)
    key(v, 'k', 30, 30)                              # sky: not fitted
    before = [(a.x0, a.y0) for a in v.apertures]
    for method in ('aperture', 'optimal'):
        v.cfg['analysis']['photometry'] = method
        r = v.fit_frame()
        assert [(a.x0, a.y0) for a in v.apertures] == before
        assert set(r) == {v.apertures[0].name, v.apertures[1].name}
        for x in r.values():
            assert x['used'] == 1 and x['signal'] > 0 and x['fwhm'] > 2
        assert ('optimal' in v.status.get_text()) == (method == 'optimal')


def test_open_other_movie_replaces_the_window(movie, tmp_path):
    import matplotlib.pyplot as plt
    other = tmp_path / 'other.ser'
    write_ser(other, [render(i, np.random.default_rng(1)) for i in range(12)])
    v = Viewer(str(movie))
    new = v.open_other(str(other))
    assert new.path == other and new.n == 12 and new.cfg is v.cfg
    assert not plt.fignum_exists(v.fig.number) and plt.fignum_exists(new.fig.number)
    assert new.open_other(str(tmp_path / 'missing.ser')) is None     # bad file: stays open
    assert plt.fignum_exists(new.fig.number) and 'cannot open' in new.status.get_text()
    new.close()


def test_file_dialog_result(monkeypatch, tmp_path):
    import subprocess

    from pystarlight import viewer

    def fake(stdout):
        return lambda *a, **k: SimpleNamespace(stdout=stdout)
    monkeypatch.setattr(subprocess, 'run', fake(f'{tmp_path}/run.ravf\n'))
    assert viewer.ask_movie_path(tmp_path) == f'{tmp_path}/run.ravf'
    monkeypatch.setattr(subprocess, 'run', fake(f'{tmp_path}/frames/f_0001.fits\n'))
    assert viewer.ask_movie_path() == f'{tmp_path}/frames'          # a FITS frame -> its folder
    monkeypatch.setattr(subprocess, 'run', fake('\n'))
    assert viewer.ask_movie_path() is None                            # cancelled
    assert viewer.main([]) == 1                                       # no movie, dialog cancelled


def test_offline_solver_solves_on_open_upload_only_on_key(movie, monkeypatch):
    from pystarlight.wcs import solve
    started = []
    monkeypatch.setattr(Viewer, 'start_solve', lambda self, force=False: started.append(force))
    v = Viewer(str(movie))                                   # no local solver: never uploads on its own
    assert not v.local_solver and started == [] and 'nova' in v.job_status.get_text()
    monkeypatch.setattr(solve, 'local_solver_available', lambda: True)
    v = Viewer(str(movie))
    assert v.local_solver and started == [False]             # offline: solved right away
    cfg = config_mod.load()
    cfg['astrometry']['solver'] = 'nova'                     # forced nova: wait for key p
    v = Viewer(str(movie), cfg)
    assert not v.local_solver and started == [False]


def test_esc_stops_hanging_star_names_and_run_goes_ahead(movie, monkeypatch):
    import threading

    from astropy.wcs import WCS

    from pystarlight.wcs import orientation, solve
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval, w.wcs.crpix = (310.36, 45.1), (160, 120)
    w.wcs.cd = [[0, 2.07 / 3600], [2.07 / 3600, 0]]
    hang = threading.Event()
    monkeypatch.setattr(solve, 'solve_movie', lambda path, cfg, **kw: (w, orientation(w, (240, 320))))
    monkeypatch.setattr(solve, 'field_stars', lambda *a, **kw: hang.wait(30) or [])   # SIMBAD hangs
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    key(v, 't', c0, r0)
    key(v, 'r', c1, r1)
    v.start_solve()
    while v.orient is None:
        v._solve_job.join(0.05)
    v._poll_solve()
    assert 'Esc stops' in v.job_status.get_text()
    key(v, 'escape')
    assert v._solve_job is None and v.wcs is w and 'names skipped' in v.job_status.get_text()
    assert v._unidentified() and v.run() is None
    v._job.join(60)                                    # no identification: straight to the analysis
    v.poll()
    assert v._lc is not None and v.ap_stars == {}
    hang.set()


def test_new_apertures_match_loaded_box_size_after_resizing(movie):
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    v = Viewer(str(movie))
    v.size = 21                                    # saved with another size than the config's
    click(v, c0, r0)
    key(v, 's')
    v = Viewer(str(movie))
    assert v.size == 21 and 'box 21px' in v.aperture_size_text.get_text()
    v.adjust_aperture_size(2)
    click(v, c1, r1)
    assert [a.size for a in v.apertures] == [23, 23]
    key(v, 's')
    v.size = 51                                    # an old file with mixed sizes
    click(v, STARS[2][1], STARS[2][0])
    centre = v.apertures[-1].center
    key(v, 's')
    v = Viewer(str(movie))
    assert v.size == 23 and [a.size for a in v.apertures] == [23, 23, 23]
    assert np.allclose(v.apertures[-1].center, centre) and 'was different' in v.status.get_text()


def test_failed_lookups_are_tried_again(movie):
    from pystarlight.wcs.identify import FieldStar
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    key(v, 't', c0, r0)
    key(v, 'r', c1, r1)
    v.wcs = object()                                   # solved
    t, r = v.apertures
    v.ap_stars = {t.name: FieldStar(c0, r0, 1, 3, False),                       # saved offline: no name
                  r.name: FieldStar(c1, r1, 1, 3, False, name='TYC 1-2-1')}
    assert [a.name for a in v._unidentified()] == [t.name]


def test_star_names_toggle(movie):
    from pystarlight.wcs.identify import FieldStar
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    v.ap_stars = {v.apertures[0].name: FieldStar(c0, r0, 1, 3, False, name='TYC 1-2-1', gmag=9.5)}
    v.stars = [FieldStar(c1, r1, 1, 3, False, name='UCAC4 600-000001')]

    def texts():
        v.draw_apertures()
        v.draw_stars()
        return [t.get_text() for t in v.patches + v._star_marks if hasattr(t, 'get_text')]
    assert any('TYC 1-2-1' in t for t in texts()) and any('UCAC4' in t for t in texts())
    assert v._names_button.label.get_text() == 'Names on'
    key(v, 'N')
    assert texts() == ['target'] and v._names_button.label.get_text() == 'Names off'
    v.toggle_names()                                 # the button
    assert any('TYC 1-2-1' in t for t in texts())


def test_result_plot_follows_role_and_colour_keys(movie):
    import matplotlib.pyplot as plt
    from matplotlib.colors import to_hex
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _), (r2, c2, _) = STARS
    for x, y, k in ((c0, r0, 't'), (c1, r1, 'r'), (c2, r2, 'r')):
        click(v, x, y)
        key(v, k, x, y)
    v.run()
    v._job.join(120)
    v.poll()
    fig = v._summary_fig

    def raw():
        return {ln.get_label(): to_hex(ln.get_color()) for ln in fig.axes[1].get_lines()}
    assert set(raw()) == {'target', 'ref1', 'ref2'} and raw()['target'] == plot.TARGET_COLOR
    key(v, 'Y', c1, r1)                                       # ref1 yellow, live
    assert v._summary_fig is fig and raw()['ref1'] == plot.APERTURE_COLORS['yellow']
    assert any('ref1 [Y]' in t.get_text() for t in v.patches if hasattr(t, 'get_text'))
    ref1 = v._lc['ref1']['signal']
    key(v, 't', c1, r1)                                       # ref1 becomes the target ...
    assert v._summary_fig is fig and plt.fignum_exists(fig.number)
    assert v._lc['target']['signal'] is ref1                  # its data went along with the name
    assert abs(v.nearest(c1, r1).center[0] - c1) < 8 and v.nearest(c1, r1).name == 'target'
    assert set(raw()) == {'target', 'ref2'}                   # ... the old target is no reference
    key(v, 'r', c0, r0)                                       # and back in as a reference
    assert set(raw()) == {'target', 'ref2', 'ref1'}


def test_rename_in_a_text_box(movie):
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    assert [(a.name, a.role) for a in v.apertures] == [('target', 'target'), ('ref1', 'reference')]
    key(v, 'n', c0, r0)
    assert v._rename_ax.get_visible() and v._rename_box.text == 'target'
    key(v, 'r', c0, r0)                                # typing: no role change, no frame step
    key(v, 'right')
    assert v.apertures[0].role == 'target' and v.index == 0
    v._rename_box.set_val('ref1')                      # taken
    assert 'already' in v.status.get_text() and v.apertures[0].name == 'target'
    key(v, 'n', c0, r0)
    v._rename_box.set_val('star7')                     # as Enter does
    assert v.apertures[0].name == 'star7' and not v._rename_ax.get_visible()
    key(v, 'n', c1, r1)
    key(v, 'escape')                                   # cancelled
    assert v.apertures[1].name == 'ref1' and not v._rename_ax.get_visible()
    key(v, 't', c1, r1)                                # keys work again
    assert v.apertures[1].role == 'target'


def test_astrometry_summary_flags_an_aperture_off_its_star(movie):
    from pystarlight.wcs.reduce import ApertureCheck, Reduction
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    key(v, 't', c0, r0)
    key(v, 'r', c1, r1)
    t, r = v.apertures
    assert v.astrometry_summary() == ''                 # no local catalogue: nothing reduced
    v.reduction = Reduction(1, False, 0, 0, 0, 0, 1, [], [], [], [], 0.5, 0.4, 2026.8)
    v.ap_checks = {t.name: ApertureCheck(t.name, c0, r0, 0, 0, 0.5, 0.4, 'TYC 1-2-1', 9.0, 0.1, -0.2),
                   r.name: ApertureCheck(r.name, c1, r1, 0, 0, 0.5, 0.4, 'TYC 1-2-2', 9.5, 3.0, 2.0)}
    s = v.astrometry_summary()
    assert s.startswith('Gaia O-C 0.50"/0.40"')
    assert f'{t.name} 0.22"' in s and 'OFF' not in s.split(';')[1]
    assert f'{r.name} 3.61"' in s and s.endswith('OFF - check the star')


def test_labels_show_the_frame_magnitudes(movie):
    from pystarlight.wcs.reduce import ApertureCheck, FrameMags, MagScale
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c0, r0)
    click(v, c1, r1)
    key(v, 't', c0, r0)
    key(v, 'r', c1, r1)
    t, r = v.apertures
    v.frame_mags = FrameMags(v.index, MagScale(20.0, 0.2, 12, 4.0, 2.0), {
        t.name: ApertureCheck(t.name, c0, r0, 0, 0, 0.5, 0.5, 'TYC 1-2-1', 10.0, mag=10.9, mag_err=0.05,
                              mag_sigma=0.2),
        r.name: ApertureCheck(r.name, c1, r1, 0, 0, 0.5, 0.5, 'TYC 1-2-2', 9.0, mag=None, mag_sigma=0.2)})
    v.draw_apertures()
    labels = [p.get_text() for p in v.patches if hasattr(p, 'get_text')]
    assert any(s.startswith(t.name) and 'm=10.90±0.05 (+0.90, 4.4σ)' in s for s in labels)
    assert any(s.startswith(r.name) and s.endswith('m=- (no flux)') for s in labels)
    assert 'zero point 20.00, scatter 0.20 (12 Gaia stars)' in v.frame_mag_summary()
    v.index += 1                                     # another frame: not measured yet, no stale values
    assert v.frame_mag_text(t.name) == '' and v.frame_mag_summary() == ''


def test_result_title_names_the_candidate_star(movie):
    from pystarlight.wcs.identify import FieldStar
    v = Viewer(str(movie))
    assert v._result_title('target') == movie.stem                   # not identified: the movie only
    v.ap_stars = {'target': FieldStar(1, 1, 1, 3, False, name='TYC 3574-1718-1')}
    assert v._result_title('target') == f'{movie.stem} - candidate TYC 3574-1718-1'


def test_star_field_marks_a_catalogue_star_and_sets_the_target(movie, monkeypatch):
    from astropy.wcs import WCS

    from pystarlight.wcs import identify, localcat, orientation
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    click(v, c1, r1)
    key(v, 't', c1, r1)                             # the target on the wrong star first
    v._star_box.set_val('TYC 3574-1718-1')
    assert 'needs the plate solution' in v.status.get_text()
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = (310.36, 45.1)
    w.wcs.crpix = (160, 120)
    w.wcs.cd = [[0, 2.07 / 3600], [2.07 / 3600, 0]]
    v.wcs, v.orient = w, orientation(w, v.image.shape)
    ra, dec = (float(q) for q in w.wcs_pix2world(c0, r0, 0))

    class Cat:
        def find(self, text, ra0, dec0, radius_arcsec, epoch=None):
            if localcat.parse_star_id(text) == localcat.parse_star_id('TYC 3574-1718-1'):
                return localcat.star_entry({'source_id': 7, 'tyc': localcat.parse_star_id(text)[1],
                                            'ucac4': 0, 'hip': 0, 'g': 9.89, 'ra': ra, 'dec': dec})

        def cone(self, *a, **kw):
            return []
    monkeypatch.setattr(localcat, 'local_catalog', lambda cfg=None: Cat())
    monkeypatch.setattr(identify, 'simbad_object', lambda name, timeout=0: None)
    v._star_box.set_val('tyc 3574 1718 1')
    assert v.status.get_text() == 'looking up tyc 3574 1718 1 ...'     # in the background
    v.finish_jobs()
    assert v.status.get_text().startswith('marked TYC 3574-1718-1  G 9.9 at')
    x, y = v.marker_xy()
    dx, dy = v._marker_shift[0]                     # the solution's stack vs this (shaken) frame
    assert abs(x - (c0 - dx)) < 0.01 and abs(y - (r0 - dy)) < 0.01 and abs(dx) < 5
    assert len(v._marker_artists) == 5 and v._marker_artists[-1].get_text() == 'T TYC 3574-1718-1  G 9.9'
    assert json.loads(v.marker_file.read_text())['query'] == 'tyc 3574 1718 1'
    v.target_to_marker()
    t = next(a for a in v.apertures if a.role == 'target')
    assert len(v.apertures) == 1 and abs(t.center[0] - x) <= 1 and abs(t.center[1] - y) <= 1
    assert v.ap_stars[t.name].name == 'TYC 3574-1718-1' and v.ap_stars[t.name].gmag == 9.89
    v._star_box.set_val('HD 1')                     # not in the field, SIMBAD has nothing
    v.finish_jobs()
    assert 'not found' in v.status.get_text() and v.marker['name'] == 'TYC 3574-1718-1'
    v2 = Viewer(str(movie))                         # the entry comes back with the movie
    assert v2.marker['name'] == 'TYC 3574-1718-1' and v2._star_box.text == 'tyc 3574 1718 1'
    v2._star_box.set_val('')
    assert v2.marker is None and not v2.marker_file.exists() and v2._marker_artists == []


def test_missing_tk_is_explained(monkeypatch):
    import sys

    import matplotlib

    from pystarlight import viewer
    monkeypatch.setattr(matplotlib, 'get_backend', lambda: 'agg')     # matplotlib's silent fallback
    monkeypatch.delenv('MPLBACKEND', raising=False)
    monkeypatch.setitem(sys.modules, 'tkinter', None)                 # a Python without Tk
    monkeypatch.setattr(sys, '_base_executable', '/usr/bin/python3', raising=False)   # the system's,
    monkeypatch.setattr(sys, 'base_prefix', sys.prefix)                               # no venv
    msg = viewer.gui_problem()
    assert 'python3-tk' in msg and 'brew install python-tk' in msg and 'tcl/tk' in msg
    monkeypatch.setattr(matplotlib, 'get_backend', lambda: 'tkagg')
    assert viewer.gui_problem() is None
    monkeypatch.setattr(matplotlib, 'get_backend', lambda: 'agg')
    monkeypatch.setenv('MPLBACKEND', 'agg')
    assert 'MPLBACKEND' in viewer.gui_problem()


def test_first_click_is_the_target_later_clicks_references(movie):
    v = Viewer(str(movie))
    for r, c, _ in STARS[:3]:
        click(v, c, r)
    assert [(a.name, a.role, a.color) for a in v.apertures] == [
        ('target', 'target', 'red'), ('ref1', 'reference', 'yellow'), ('ref2', 'reference', 'yellow')]
    key(v, 't', *STARS[0][1::-1])                 # already the target: keeps its name
    assert v.apertures[0].name == 'target'
    click(v, *STARS[0][1::-1], button=3)          # the target removed: the next click is the target
    click(v, *STARS[0][1::-1])
    assert (v.apertures[-1].name, v.apertures[-1].role) == ('target', 'target')


def test_tk_hint_names_the_python_and_the_fix(monkeypatch):
    """A uv environment on the system's Python: Tk for that Python, or the venv rebuilt on uv's own."""
    pytest.importorskip('matplotlib')
    from pystarlight import viewer
    monkeypatch.setattr(sys, '_base_executable', '/usr/bin/python3.11', raising=False)
    monkeypatch.setattr(sys, 'base_prefix', '/usr')
    monkeypatch.setattr(sys, 'prefix', '/home/x/pystarlight/.venv')
    hint = viewer.tk_hint()
    assert '/usr/bin/python3.11' in hint and 'sudo apt install python3-tk' in hint
    assert 'rm -rf .venv && uv sync --python-preference only-managed' in hint
    monkeypatch.setattr(sys, '_base_executable', '/home/x/.local/share/uv/python/cpython-3.12/bin/python3.12')
    assert 'uv python install --reinstall' in viewer.tk_hint()


def test_auto_key_picks_references_and_keeps_the_clicked_target(movie):
    v = Viewer(str(movie))                                  # no plate solution: no names, no upload
    key(v, 'A')
    v.finish_jobs()
    assert 'NO target' in v.status.get_text() and len(v.apertures) == 3
    assert {a.role for a in v.apertures} == {'reference'}
    (r0, c0, _), (r1, c1, _) = STARS[0], STARS[1]
    v.apertures = []
    click(v, c1, r1)                                        # the first click is the target
    key(v, 'A')
    v.finish_jobs()
    t = [a for a in v.apertures if a.role == 'target']
    refs = [a for a in v.apertures if a.role == 'reference']
    assert len(t) == 1 and abs(t[0].center[0] - c1) <= 6 and abs(t[0].center[1] - r1) <= 6
    assert len(refs) == 2 and min(abs(a.center[0] - c0) + abs(a.center[1] - r0) for a in refs) <= 12
    msg = v.status.get_text()
    assert msg.startswith('auto: target ') and '2 references of 3 stars' in msg


def test_batch_auto_finds_the_recorded_target_and_writes_the_results(movie, monkeypatch, capsys):
    """The camera's TARGET-STAR / -RA / -DEC (SER sidecar) -> target aperture via the plate
    solution; references picked; analysed headless; files next to the movie."""
    from astropy.wcs import WCS

    from pystarlight import viewer
    from pystarlight.capture import ser_writer
    from pystarlight.wcs import identify, localcat, orientation, solve
    w = WCS(naxis=2)
    w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
    w.wcs.crval = (310.36, 45.1)
    w.wcs.crpix = (160, 120)
    w.wcs.cd = [[0, 2.07 / 3600], [2.07 / 3600, 0]]
    (r0, c0, _) = STARS[0]
    ra, dec = (float(q) for q in w.wcs_pix2world(c0, r0, 0))
    h, wd = movie_shape(str(movie))
    meta = movie.with_name(movie.name + '.meta.json')
    meta.chmod(0o644)                                       # the capture leaves it read-only
    ser_writer.write_meta(str(movie), wd, h, 16, {'target_star': 'UCAC4 676-090000', 'target_ra': ra,
                                                  'target_dec': dec})

    class Cat:                                  # the name is not in it: found by the recorded RA/Dec
        def find(self, *a, **kw):
            return None

        def cone(self, *a, **kw):
            return []
    monkeypatch.setattr(localcat, 'local_catalog', lambda cfg=None: Cat())
    monkeypatch.setattr(identify, 'simbad_object', lambda name, timeout=0: None)
    monkeypatch.setattr(identify, 'simbad_lookup', lambda *a, **kw: None)
    monkeypatch.setattr(solve, 'solve_movie', lambda *a, **kw: (w, orientation(w, (h, wd))))
    monkeypatch.setattr(solve, 'field_stars', lambda *a, **kw: [])
    monkeypatch.setattr(Viewer, 'batch', False)             # main sets it; undone after the test
    assert viewer.main(['--batch', '--auto', str(movie)]) == 0
    out = capsys.readouterr().out
    assert 'auto: target UCAC4 676-090000; 2 references of 3 stars' in out
    aps = load_apertures(movie.with_suffix('.apertures.json'))
    t = next(a for a in aps if a.role == 'target')
    assert abs(t.center[0] - c0) <= 6 and abs(t.center[1] - r0) <= 6
    assert sum(a.role == 'reference' for a in aps) == 2
    ev = json.loads(movie.with_suffix('.event.json').read_text())
    assert ev['target']['name'] == 'UCAC4 676-090000'
    assert movie.with_suffix('.csv').exists()


def test_batch_without_a_target_analyses_nothing(movie, monkeypatch, capsys):
    from pystarlight import viewer
    from pystarlight.wcs import solve

    def fail(*a, **kw):
        raise RuntimeError('no solver here')
    monkeypatch.setattr(solve, 'solve_movie', fail)
    monkeypatch.setattr(Viewer, 'batch', False)
    assert viewer.main(['--batch', '--auto', str(movie)]) == 1
    out = capsys.readouterr().out
    assert 'plate solve failed: no solver here' in out and 'nothing analysed' in out
    assert not movie.with_suffix('.csv').exists()


def test_auto_button_takes_the_star_typed_in_the_field(movie, monkeypatch):
    v = Viewer(str(movie))
    calls = []
    monkeypatch.setattr(Viewer, 'request_auto', lambda self, target=None, run=False, solve=True:
                        calls.append((target, solve)))
    v._star_setting = True                          # typed, Enter not pressed
    v._star_box.set_val('  TYC 3574-1718-1 ')
    v._star_setting = False
    v._auto_ap_button._observers.process('clicked', None)
    v.marker = {'name': 'TYC 3574-1718-1', 'query': 'TYC 3574-1718-1'}   # already marked: use it
    key(v, 'A')
    assert calls == [('TYC 3574-1718-1', False), (None, False)]


def test_a_reference_on_the_target_star_is_removed(movie):
    """Set target / t onto a star that has a reference already: the reference goes (it would put
    the target into its own normalisation and hide the drop); Run checks again."""
    v = Viewer(str(movie))
    (r0, c0, _), (r1, c1, _), (r2, c2, _) = STARS
    click(v, c1, r1)                                # target
    click(v, c0, r0)                                # ref1
    click(v, c2, r2)                                # ref2
    key(v, 't', c0, r0)                             # the target onto ref1's star: ap1 kept as target
    assert [(a.name, a.role) for a in v.apertures] == [('ap1', ''), ('target', 'target'),
                                                       ('ref2', 'reference')]
    v.apertures.append(v.apertures[2].__class__(**{k: getattr(v.apertures[2], k) for k in
                       ('name', 'x0', 'y0', 'size', 'color', 'role')}))
    v.apertures[-1].name = 'ref3'                   # loaded from a file: twice on one star
    assert v.drop_doubles() == ['ref3'] and 'ref3 (on the same star as ref2)' in v.status.get_text()
