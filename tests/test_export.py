"""PyMovie-layout CSV export (for PyOTE)."""

import sys
from pathlib import Path

import pytest

from pystarlight import Aperture, analyze, open_movie
from pystarlight.export import csv_name
from pystarlight.globaltrack import analyze_global
from test_globaltrack import STARS, render, write_ser


@pytest.fixture(scope='module')
def movie(tmp_path_factory):
    import numpy as np
    rng = np.random.default_rng(3)
    p = tmp_path_factory.mktemp('e') / 'm.ser'
    write_ser(p, [render(i, rng, False) for i in range(30)])
    return str(p)


def aps():
    out = [Aperture.centered_at('my-target', 150, 110, role='target')]
    out += [Aperture.centered_at(f'ref{k}', c, r, role='reference')
            for k, (r, c, _) in enumerate(STARS[1:3], 1)]
    out.append(Aperture.centered_at('sky', 280, 130, role='sky'))
    return out


def lines(p):
    return Path(p).read_text().splitlines()


def test_global_mode_layout(movie, tmp_path):
    with open_movie(movie) as mv:
        lc = analyze_global(mv, aps())
    out = tmp_path / 'g.csv'
    lc.to_pymovie_csv(str(out))
    ln = lines(out)
    assert 'PyMovie' in ln[0]
    hdr = [x for x in ln if 'FrameNum' in x]
    assert len(hdr) == 1 and not hdr[0].startswith('#')                # only the column header
    cols = hdr[0].split(',')
    assert cols[:5] == ['FrameNum', 'timeInfo', 'signal-my_target', 'signal-ref1', 'signal-ref2']
    assert 'signal-sky' not in cols                                    # sky has no light curve
    assert 'hit-defect-ref2' in cols and all(c.count('-') >= 1 for c in cols[2:])
    data = ln[ln.index(hdr[0]) + 1:]
    assert len(data) == 30 and data[0].split(',')[1].startswith('[')
    assert all(len(r.split(',')) == len(cols) for r in data)


def test_pymovie_mode_layout(movie, tmp_path):
    with open_movie(movie) as mv:
        lc = analyze(mv, [Aperture.centered_at('ap00', 150, 110),
                          Aperture.centered_at('ap01', 60, 50, color='yellow')])
    out = tmp_path / 'p.csv'
    lc.to_pymovie_csv(str(out))
    ln = lines(out)
    cols = next(x for x in ln if x.startswith('FrameNum')).split(',')
    assert cols[:4] == ['FrameNum', 'timeInfo', 'signal-ap00', 'signal-ap01']
    assert cols[4:12] == ['appsum-ap00', 'avgbkg-ap00', 'stdbkg-ap00', 'nmaskpx-ap00', 'maxpx-ap00',
                          'xcentroid-ap00', 'ycentroid-ap00', 'hit-defect-ap00']


def test_name_sanitising():
    assert csv_name('ref-1 a,b') == 'ref_1_a_b'


PYOTE = Path(__file__).resolve().parents[2] / 'py-ote' / 'src' / 'pyoteapp'


@pytest.mark.skipif(not (PYOTE / 'csvreader.py').exists(), reason='PyOTE source not next to this repo')
def test_pyote_reads_it(movie, tmp_path):
    sys.path.insert(0, str(PYOTE))
    import csvreader
    with open_movie(movie) as mv:
        lc = analyze_global(mv, aps())
    out = tmp_path / 'g.csv'
    lc.to_pymovie_csv(str(out))
    frame, time, value, ref1, ref2, *_rest, names, headers = csvreader.readLightCurve(
        str(out), pymovieDict={})
    assert names == ['my_target', 'ref1', 'ref2']
    assert len(frame) == len(value) == len(ref1) == 30
    assert float(value[0]) == pytest.approx(lc['my-target']['signal'][0], abs=0.01)
