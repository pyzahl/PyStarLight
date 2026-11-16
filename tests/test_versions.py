"""The versions in the README's overview are the ones in the code."""

from pathlib import Path

from pystarlight import __version__
from pystarlight.capture.version import CAMERA_VERSION, PLAYER_VERSION

README = Path(__file__).resolve().parents[1] / 'README.md'


def test_readme_names_the_current_versions():
    text = README.read_text(encoding='utf-8')
    for shown in (f'(PyStarLight {__version__})', f'(camera {CAMERA_VERSION})', f'(player {PLAYER_VERSION})'):
        assert shown in text, f'README: {shown} missing (versions bumped? update the overview)'
