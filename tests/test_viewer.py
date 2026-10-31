"""Viewer and plots, driven headless (Agg backend)."""

from types import SimpleNamespace

import numpy as np
import pytest

mpl = pytest.importorskip('matplotlib')
mpl.use('Agg')

from pystarlight import config as config_mod  # noqa: E402
from pystarlight import plot  # noqa: E402
from pystarlight.viewer import Viewer, load_apertures, save_apertures  # noqa: E402
from test_pipeline import STARS, render, truth, write_ser  # noqa: E402


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


def test_savgol_checkbox_switches_peak_fit(movie):
    v = Viewer(str(movie))
    assert v.cfg['analysis']['savgol_fit'] is False
    v._savgol_box.set_active(0)
    assert v.cfg['analysis']['savgol_fit'] is True
    (r0, c0, _) = STARS[0]
    click(v, c0, r0)
    a = v.apertures[0]
    centred = []
    for _ in range(2):                               # SG fit, then the standard fit: same box
        a.x0 += 3
        a.y0 -= 2
        assert v.center_aperture(a)
        centred.append((a.x0, a.y0))
        if len(centred) == 1:
            v._savgol_box.set_active(0)
    assert v.cfg['analysis']['savgol_fit'] is False
    assert centred[0] == centred[1]


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
    for sg in (False, True):
        v.cfg['analysis']['savgol_fit'] = sg
        r = v.fit_frame()
        assert [(a.x0, a.y0) for a in v.apertures] == before
        assert set(r) == {v.apertures[0].name, v.apertures[1].name}
        for x in r.values():
            assert x['used'] == 1 and x['signal'] > 0 and x['fwhm'] > 2
        assert 'SG' in v.status.get_text() if sg else 'std' in v.status.get_text()


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
    assert texts() == ['ap1'] and v._names_button.label.get_text() == 'Names off'
    v.toggle_names()                                 # the button
    assert any('TYC 1-2-1' in t for t in texts())
