import atexit
import os
import shutil
import sys
import tempfile

# Every temporary file of this test run in one folder, removed at exit (TMPDIR too, for child processes);
# pytest's own tmp_path stays in /tmp/pytest-of-<user> (it keeps the last 3 runs).
_ROOT = tempfile.mkdtemp(prefix='pystarlight-test-')
tempfile.tempdir = _ROOT
os.environ['TMPDIR'] = _ROOT
atexit.register(shutil.rmtree, _ROOT, True)
# Headless: never open windows during tests.
os.environ.setdefault('MPLBACKEND', 'Agg')
# Never read the user's private config: point the lookup at an empty directory.
os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp(prefix='config-')
os.environ['PYSTARLIGHT_CONFIG'] = 'none'      # also ignore <project root>/pystarlight.toml
os.environ.pop('PYSYZYGOS_CATALOG', None)     # no local star catalogue unless a test makes one
# Let tests import helpers from each other (test_viewer uses test_pipeline's movie maker).
sys.path.insert(0, os.path.dirname(__file__))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def simbad_reachable(monkeypatch):
    """A failed SIMBAD lookup pauses SIMBAD for minutes (module state): start each test fresh."""
    from pystarlight.wcs import identify
    monkeypatch.setattr(identify, '_simbad_down_until', 0.0)


@pytest.fixture(autouse=True)
def no_installed_astap(monkeypatch):
    """An ASTAP installed on this computer must not take part in the tests (solver 'auto' would pick
    it): only a fake one a test sets up (ASTAP_NAMES / ASTAP_DIRS / [astrometry] astap)."""
    from pystarlight.wcs import solve
    monkeypatch.setattr(solve, 'ASTAP_NAMES', ())
    monkeypatch.setattr(solve, 'ASTAP_DIRS', ())
