"""The Moon and the planets as targets (capture/bodies.py)."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("astropy")
from pystarlight.capture import bodies  # noqa: E402

WHEN = datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc)
SITE = (40.955, -72.928, 60.0)


def test_names():
    assert bodies.body_name(" jupiter ") == "Jupiter" and bodies.body_name("Luna") == "Moon"
    assert bodies.body_name("UCAC4 634-113397") is None and bodies.body_name("") is None
    with pytest.raises(ValueError, match="Sun"):
        bodies.body_name("Sun")
    assert bodies.is_body("sun") and bodies.is_body("MARS") and not bodies.is_body("20 41 25 +45 16 49")
    with pytest.raises(ValueError):
        bodies.position("Sun", SITE, WHEN)


@pytest.mark.parametrize("name", bodies.BODIES)
def test_topocentric_matches_astropy(name):
    """Our parallax (WGS84, mean sidereal time) against astropy's topocentric GCRS: < 1"."""
    import astropy.units as u
    from astropy.coordinates import EarthLocation, SkyCoord, get_body
    from astropy.time import Time
    from astropy.utils import iers
    loc = EarthLocation(lon=SITE[1] * u.deg, lat=SITE[0] * u.deg, height=SITE[2] * u.m)
    with iers.conf.set_temp("auto_download", False), iers.conf.set_temp("auto_max_age", None), \
            iers.conf.set_temp("iers_degraded_accuracy", "ignore"):
        ref = get_body(name.lower(), Time(WHEN), loc)
    got = bodies.position(name, SITE, WHEN)
    assert got[0] == name
    assert SkyCoord(got[1] * u.deg, got[2] * u.deg).separation(SkyCoord(ref.ra, ref.dec)).arcsec < 1.0


def test_moon_parallax():
    """The Moon from the site differs from the geocentric position by up to ~1 deg."""
    _, ra0, dec0 = bodies.position("Moon", None, WHEN)
    _, ra, dec = bodies.position("Moon", SITE, WHEN)
    from pystarlight.capture.mount import separation
    assert 0.1 < separation(ra0, dec0, ra, dec) < 1.05
