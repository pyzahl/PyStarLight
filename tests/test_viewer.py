"""Viewer and plots, driven headless (Agg backend)."""

from types import SimpleNamespace

import numpy as np
import pytest

mpl = pytest.importorskip('matplotlib')
mpl.use('Agg')

from pystarlight import plot  # noqa: E402
from pystarlight.viewer import Viewer, load_apertures  # noqa: E402
from test_pipeline import STARS, render, write_ser  # noqa: E402


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
