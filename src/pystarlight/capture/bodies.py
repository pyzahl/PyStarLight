"""The Moon and the planets as targets (camera target, Mount panel goto, file names).

NEW CODE. Astrometric geocentric positions from astropy's built-in ephemeris (ERFA: Moon98 for
the Moon, EPV00 / Plan94 for the planets; offline, no IERS download): barycentric, light time
from the Earth, no aberration, on the ICRS (J2000) axes, as the catalogue stars and the plate
solutions (JPL Horizons' "astrometric RA/Dec"), so they compare on the image and the mount's
J2000 -> JNow precession treats them alike. (astropy's get_body is the apparent GCRS place:
annual aberration in it, up to 20".) Then the topocentric shift for the site (WGS84, mean
sidereal time): up to 1 deg for the Moon, < 1' for the planets. Accuracy: see ephemeris() (up to
~20" built in, < 1" with DE440s): well inside any field.
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


C_KM_S = 299792.458
DE_URL = "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp"
_ephem = None


def ephemeris():
    """'de440s' when jplephem is installed and the file is in astropy's cache already (< 1"),
    else 'builtin' (vs JPL Horizons: Mercury-Mars < 2", Uranus, Neptune ~5", the Moon ~8",
    Saturn ~18", Jupiter ~22"). Never downloads: no stall in the field."""
    global _ephem
    if _ephem is None:
        _ephem = "builtin"
        try:
            import jplephem  # noqa: F401
            from astropy.utils.data import is_url_in_cache
            if is_url_in_cache(DE_URL):
                _ephem = "de440s"
        except Exception:
            pass
    return _ephem


def geocentric(name, when=None):
    """(ra_deg, dec_deg, distance_km): astrometric position of the body from the Earth's centre
    at UTC `when` (a datetime, default now), ICRS axes, light time included."""
    import warnings

    import astropy.units as u
    from astropy.coordinates import get_body_barycentric, solar_system_ephemeris
    from astropy.time import Time
    from astropy.utils import iers
    when = when or datetime.now(timezone.utc)
    with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), \
            iers.conf.set_temp("auto_max_age", None), solar_system_ephemeris.set(ephemeris()):
        warnings.simplefilter("ignore")
        t = Time(when, scale="utc")
        earth = get_body_barycentric("earth", t).xyz.to(u.km).value
        lt = 0.0
        for _ in range(3):                      # light time: where the body was when it sent the light
            v = get_body_barycentric(name.lower(), t - lt * u.s).xyz.to(u.km).value - earth
            lt = math.sqrt(sum(c * c for c in v)) / C_KM_S
    x, y, z = (float(c) for c in v)
    dist = math.sqrt(x * x + y * y + z * z)
    return math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.asin(z / dist)), dist


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
