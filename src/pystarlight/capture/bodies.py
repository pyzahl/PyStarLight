"""The Moon and the planets as targets (camera target, Mount panel goto, file names).

NEW CODE. Geocentric positions from astropy's built-in ephemeris (ERFA: Moon98 for the Moon,
EPV00 / Plan94 for the planets; offline, no IERS download), light time and aberration included,
on the ICRS (J2000) axes as the catalogue stars, so the mount's J2000 -> JNow precession treats
them alike. Then the topocentric shift for the site (WGS84, mean sidereal time): up to 1 deg for
the Moon, < 1' for the planets. Accuracy a few arcseconds (the Moon ~10"): well inside any field.
The Sun is refused on purpose (a goto at it would burn the camera).
"""
import math
from datetime import datetime, timezone

from . import mount as mt

BODIES = ("Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune")
ALIASES = {"luna": "Moon", "mond": "Moon"}
AU_KM = 149597870.7
WGS84_A_KM, WGS84_F = 6378.137, 1 / 298.257223563


def body_name(text):
    """'jupiter ' -> 'Jupiter'; None for anything else. ValueError for the Sun."""
    key = "".join(ch for ch in str(text or "").lower() if ch.isalpha())
    if key in ("sun", "sonne", "sol"):
        raise ValueError("the Sun is not a target here (no solar filter check): never point at it")
    if key in ALIASES:
        return ALIASES[key]
    for name in BODIES:
        if key == name.lower():
            return name
    return None


def is_body(text):
    try:
        return body_name(text) is not None
    except ValueError:
        return True                          # the Sun: a body, refused where it is looked up


def geocentric(name, when=None):
    """(ra_deg, dec_deg, distance_km) of the body from the Earth's centre at UTC `when` (a
    datetime, default now), ICRS axes."""
    import warnings

    from astropy.coordinates import get_body, solar_system_ephemeris
    from astropy.time import Time
    from astropy.utils import iers
    when = when or datetime.now(timezone.utc)
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), \
            iers.conf.set_temp("auto_max_age", None), solar_system_ephemeris.set("builtin"):
        warnings.simplefilter("ignore")
        c = get_body(name.lower(), Time(when, scale="utc"))
        return float(c.ra.deg), float(c.dec.deg), float(c.distance.to("km").value)


def observer_km(lat_deg, lon_deg, alt_m, jd):
    """The site's geocentric vector (km) on the ICRS axes at UTC JD: WGS84, mean sidereal time,
    precessed back to J2000 (nutation and polar motion left out: < 0.1 km)."""
    phi = math.radians(lat_deg)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A_KM / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    h = alt_m / 1000.0
    rxy, z = (n + h) * math.cos(phi), (n * (1 - e2) + h) * math.sin(phi)
    r = math.hypot(rxy, z)
    ra, dec = mt.jnow_to_j2000(mt.sidereal_deg(jd, lon_deg), math.degrees(math.atan2(z, rxy)), jd)
    a, d = math.radians(ra), math.radians(dec)
    return (r * math.cos(d) * math.cos(a), r * math.cos(d) * math.sin(a), r * math.sin(d))


def position(text, site=None, when=None):
    """(name, ra_deg, dec_deg) J2000 of the Moon or a planet as seen from site (lat, lon, alt_m;
    None: from the Earth's centre) at UTC `when` (default now). ValueError if it is none."""
    name = body_name(text)
    if name is None:
        raise ValueError(f"not the Moon or a planet: {text!r}")
    when = when or datetime.now(timezone.utc)
    ra, dec, dist = geocentric(name, when)
    if site is None:
        return name, ra, dec
    a, d = math.radians(ra), math.radians(dec)
    v = [dist * math.cos(d) * math.cos(a), dist * math.cos(d) * math.sin(a), dist * math.sin(d)]
    o = observer_km(site[0], site[1], site[2] if len(site) > 2 else 0.0, mt.julian_day(when))
    x, y, z = (v[i] - o[i] for i in range(3))
    return name, math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.atan2(z, math.hypot(x, y)))
