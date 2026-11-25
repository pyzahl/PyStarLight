"""The Moon and the planets as targets (capture/bodies.py)."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("astropy")
from pystarlight.capture import bodies  # noqa: E402
from pystarlight.capture.mount import separation  # noqa: E402

WHEN = datetime(2026, 11, 25, 5, 45, 14, 970000, tzinfo=timezone.utc)
# JPL Horizons, astrometric RA / Dec (ICRF), geocentric (500), at WHEN
HORIZONS = {"Moon": (69.003350, 26.862620), "Mercury": (221.927960, -13.937470),
            "Venus": (203.233710, -9.072590), "Mars": (152.218670, 13.732000),
            "Jupiter": (148.649860, 13.538260), "Saturn": (8.168370, 0.685940),
            "Uranus": (61.322370, 20.671090), "Neptune": (1.769780, -0.761340)}
# the built-in ephemeris against those (arcsec); DE440s: < 1"
BUILTIN_TOL = {"Moon": 10, "Mercury": 3, "Venus": 3, "Mars": 3, "Jupiter": 25, "Saturn": 21,
               "Uranus": 7, "Neptune": 6}
GREENWICH = (51.4769, 0.0, 0.0)
MOON_GREENWICH = (68.301950, 26.193120)        # Horizons, topocentric at GREENWICH, WHEN


def test_names():
    assert bodies.body_name(" jupiter ") == "Jupiter" and bodies.body_name("Luna") == "Moon"
    assert bodies.body_name("UCAC4 634-113397") is None and bodies.body_name("") is None
    with pytest.raises(ValueError, match="Sun"):
        bodies.body_name("Sun")
    assert bodies.is_body("sun") and bodies.is_body("MARS") and not bodies.is_body("20 41 25 +45 16 49")
    with pytest.raises(ValueError):
        bodies.position("Sun", GREENWICH, WHEN)


@pytest.mark.parametrize("name", bodies.BODIES)
def test_astrometric_matches_horizons(name):
    """Astrometric (no aberration: 20" in astropy's get_body), as plate solutions are."""
    _, ra, dec = bodies.position(name, None, WHEN)
    tol = 1.0 if bodies.ephemeris() == "de440s" else BUILTIN_TOL[name]
    assert separation(ra, dec, *HORIZONS[name]) * 3600 < tol


def test_moon_from_a_site_matches_horizons():
    """The topocentric shift (here 50') as Horizons has it for the same site."""
    _, ra0, dec0 = bodies.position("Moon", None, WHEN)
    _, ra, dec = bodies.position("Moon", GREENWICH, WHEN)
    assert separation(ra0, dec0, ra, dec) > 0.5
    tol = 1.0 if bodies.ephemeris() == "de440s" else BUILTIN_TOL["Moon"]
    assert separation(ra, dec, *MOON_GREENWICH) * 3600 < tol


def test_planet_named_among_solved_stars():
    """The 2026-11-25 Uranus stack: the brightest solved 'star' is Uranus (no catalogue has it);
    a star 2' away is left to the catalogues."""
    from pystarlight.capture.marks import bodies_in_field
    sol = {"time": "2026-11-25T05:44:42.910", "center": {"ra": 61.32217, "dec": 20.66877},
           "field_deg": [0.544, 0.410], "site": None,
           "stars": [{"ra": 61.235620, "dec": 20.856182},             # a star
                     {"ra": 61.322245, "dec": 20.671183},             # Uranus, measured
                     {"ra": 61.322245, "dec": 20.704516}]}            # 2' north
    assert bodies_in_field(sol) == {1: "Uranus"}
    sol["time"] = "2026-06-01T00:00:00.000"                          # Uranus elsewhere
    assert bodies_in_field(sol) == {}
