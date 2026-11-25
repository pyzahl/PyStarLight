"""Polar alignment from plate solves: where the mount's RA axis points, how far to turn the Alt
and Az knobs, and where a star must go on the image while they are turned (the camera's Polar
alignment window, polar_gui.py).

NEW CODE, numpy only (with mount.py's precession and sidereal time). The method:

1. Plate-solve the field, sync the mount.
2. Turn the RA axis only, by a known step (a goto to the mount's own Dec: the Dec axis stays),
   and plate-solve again (and once more for three points).
3. Every solved field centre, as a direction fixed to the Earth (east / north / up at the site,
   refraction put in: the telescope points at the refracted star), lies on a circle around the
   mount's RA axis, whatever the camera's offset from the axes:
   - two points: the axis is the one about which the first turns into the second by the angle
     the RA axis turned. That angle is the change of the mount's own hour angle (its reported
     RA against sidereal time): a mount synced once moves its axis exactly as it reports.
   - three points: the axis is the normal of the plane through them (no angle needed).
4. The axis against the celestial pole (the elevated one) gives the error in altitude and
   azimuth. The knobs turn the whole mount rigidly: Az about the vertical, Alt about the
   horizontal axis at right angles to the pole's azimuth.

While the knobs turn, the mount tracks (about its measured axis) and one star is followed on the
live frames. Its position on the image gives the knob turns made so far (two unknowns, two
coordinates: `Alignment.knobs_from_star`). From them comes the error left, and the pixels the
star has to reach: first the Az leg, then the Alt leg (`Alignment.arrow`). A new plate solve
re-bases the whole model (`Alignment.rebase`).

Directions are unit vectors in the site's ENU frame (x east, y north, z up); azimuth is from
north through east. Knob turns are angles in degrees: alt > 0 raises the axis, az > 0 moves it
east (azimuth up).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import mount as mt

SIDEREAL_DEG_PER_S = 360.98564736629 / 86400.0
STAR_BOX = 40                               # half size (px) of the followed star's box
POLARIS = (37.954561, 89.264109)            # J2000
SIGMA_OCT = (317.195229, -88.956499)        # J2000, the south pole's naked-eye marker


def unit(ra_deg, dec_deg):
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    return np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])


def angles(v):
    """(longitude, latitude) deg of a vector: (ra, dec) of an equatorial one."""
    v = np.asarray(v, float) / np.linalg.norm(v)
    return math.degrees(math.atan2(v[1], v[0])) % 360.0, math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))


def sep_deg(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return math.degrees(math.atan2(np.linalg.norm(np.cross(a, b)), float(np.dot(a, b))))


def rotation(axis, angle_deg):
    """Right-handed rotation about `axis` by angle_deg (Rodrigues), 3 x 3."""
    k = np.asarray(axis, float) / np.linalg.norm(axis)
    a = math.radians(angle_deg)
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(a) * kx + (1 - math.cos(a)) * kx @ kx


def _rz(angle_deg):
    return rotation((0.0, 0.0, 1.0), angle_deg)


def _hadec_to_enu(lat):
    """Rows: east, north, up in the hour-angle frame (x: the meridian on the equator, y: east
    (hour angle -90 deg), z: the north celestial pole)."""
    s, c = math.sin(math.radians(lat)), math.cos(math.radians(lat))
    return np.array([[0.0, 1.0, 0.0], [-s, 0.0, c], [c, 0.0, s]])


# ---- altitude, azimuth, refraction ----
def enu(alt_deg, az_deg):
    alt, az = math.radians(alt_deg), math.radians(az_deg)
    return np.array([math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)])


def altaz(v):
    """(altitude, azimuth from north through east) deg of an ENU direction."""
    v = np.asarray(v, float) / np.linalg.norm(v)
    return (math.degrees(math.asin(max(-1.0, min(1.0, v[2])))),
            math.degrees(math.atan2(v[0], v[1])) % 360.0)


def refraction_deg(alt_true):
    """Refraction (deg) of a star at true altitude alt_true, 10 C and 1010 hPa (Saemundsson);
    held at its -1 deg value below that."""
    h = max(alt_true, -1.0)
    return 1.02 / math.tan(math.radians(h + 10.3 / (h + 5.11))) / 60.0


def refract(v):
    """The direction a star at true direction v is seen at (raised by the refraction)."""
    alt, az = altaz(v)
    return enu(alt + refraction_deg(alt), az)


def unrefract(v):
    alt_seen, az = altaz(v)
    alt = alt_seen
    for _ in range(4):
        alt = alt_seen - refraction_deg(alt)
    return enu(alt, az)


# ---- sky <-> site ----
@dataclass
class Site:
    lat: float
    lon: float              # east positive

    @property
    def north(self):
        return self.lat >= 0.0

    def lst(self, jd):
        return mt.sidereal_deg(jd, self.lon)

    def to_enu(self, ra, dec, jd, seen=False):
        """J2000 (ra, dec) at UTC jd as an ENU direction; seen: refraction put in."""
        q = unit(*mt.j2000_to_jnow(ra, dec, jd))
        v = _hadec_to_enu(self.lat) @ (_rz(-self.lst(jd)) @ q)
        return refract(v) if seen else v

    def to_sky(self, v, jd, seen=False):
        """The inverse of to_enu: J2000 (ra, dec)."""
        if seen:
            v = unrefract(v)
        q = _rz(self.lst(jd)) @ (_hadec_to_enu(self.lat).T @ np.asarray(v, float))
        return mt.jnow_to_j2000(*angles(q), jd)

    def jnow_to_enu(self, ra_jnow, dec_jnow, jd):
        return _hadec_to_enu(self.lat) @ (_rz(-self.lst(jd)) @ unit(ra_jnow, dec_jnow))

    def pole(self):
        """The elevated celestial pole, ENU (the true one: polar alignment aims there)."""
        return enu(abs(self.lat), 0.0 if self.north else 180.0)

    def hour_angle(self, ra_jnow, jd):
        return (self.lst(jd) - ra_jnow + 180.0) % 360.0 - 180.0

    # ---- the knobs ----
    def alt_axis(self):
        """The horizontal axis the Alt knob turns about (at right angles to the pole's azimuth),
        oriented so that a positive turn raises the polar axis."""
        ph = np.array([0.0, 1.0, 0.0]) if self.north else np.array([0.0, -1.0, 0.0])
        return np.cross(ph, (0.0, 0.0, 1.0))

    def knob_rotation(self, alt_deg, az_deg):
        """The mount's rigid turn by the Alt knob (alt_deg, axis up) and then the Az knob
        (az_deg, the axis' azimuth up = east)."""
        return rotation((0.0, 0.0, 1.0), -az_deg) @ rotation(self.alt_axis(), alt_deg)


def solve_knobs(site, src, dst, start=(0.0, 0.0), iterations=20):
    """(alt, az) knob turns that move direction src onto dst (Gauss-Newton), and the angle left
    (deg). Two turns for the two coordinates of a direction: exact unless the knobs move it
    nowhere (then the step is held small)."""
    x = np.array(start, float)
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    h = 1e-4
    for _ in range(iterations):
        r = site.knob_rotation(*x) @ src - dst
        jac = np.column_stack([(site.knob_rotation(*(x + d)) @ src - site.knob_rotation(*(x - d)) @ src)
                               / (2 * h) for d in (np.array([h, 0.0]), np.array([0.0, h]))])
        step, *_ = np.linalg.lstsq(jac, -r, rcond=1e-9)
        step = np.clip(step, -10.0, 10.0)
        x += step
        if np.abs(step).max() < 1e-9:
            break
    return (float(x[0]), float(x[1])), sep_deg(site.knob_rotation(*x) @ src, dst)


# ---- the axis ----
def axis_two_points(v1, v2, theta_deg, hint):
    """The axis about which v1 turns into v2 by theta_deg (right-handed), the one of the two
    possible nearer `hint`; and the angle of the points from it (deg). ValueError if no axis
    turns them by that angle (the points are farther apart than the turn allows)."""
    v1, v2 = np.asarray(v1, float), np.asarray(v2, float)
    theta = math.radians(theta_deg)
    if abs(math.sin(theta / 2)) < math.sin(math.radians(0.5)):
        raise ValueError("the RA axis turned by less than 1 deg")
    delta = math.radians(sep_deg(v1, v2))
    s = math.sin(delta / 2) / abs(math.sin(theta / 2))
    if s > 1.0 + 1e-3:
        raise ValueError(f"the fields are {math.degrees(delta):.2f} deg apart, more than a turn of "
                         f"{theta_deg:.2f} deg allows: did the Dec axis move?")
    s = min(s, 1.0)
    c = np.cross(v1, v2)
    c /= np.linalg.norm(c)
    m = (v1 + v2) / np.linalg.norm(v1 + v2)
    cos_rho = math.sqrt(max(0.0, 1 - s * s))
    best = None
    for sign in (+1.0, -1.0):
        sin_phi = sign * cos_rho / math.cos(delta / 2)
        cos_phi = math.copysign(math.sqrt(max(0.0, 1 - sin_phi ** 2)), math.sin(theta))
        a = cos_phi * c + sin_phi * m
        a /= np.linalg.norm(a)
        if best is None or sep_deg(a, hint) < sep_deg(best, hint):
            best = a
    return best, sep_deg(best, v1)


def axis_three_points(v1, v2, v3, hint):
    """The normal of the plane through three points of a circle, on the side of `hint`; and
    the circle's radius (deg)."""
    v1, v2, v3 = (np.asarray(v, float) for v in (v1, v2, v3))
    n = np.cross(v2 - v1, v3 - v1)
    if np.linalg.norm(n) < 1e-9:
        raise ValueError("the three fields lie on a line")
    n /= np.linalg.norm(n)
    if np.dot(n, hint) < 0:
        n = -n
    return n, float(np.mean([sep_deg(n, v) for v in (v1, v2, v3)]))


@dataclass
class Point:
    """One plate-solved field: its centre (J2000), the UTC jd of the stack's middle, and the
    mount's own position then (JNow, as it reports it)."""
    ra: float
    dec: float
    jd: float
    mount_ra: float
    mount_dec: float


def measure_axis(site, points, refraction=True):
    """The RA axis (ENU) from 2 or 3 points, and a dict of what went into it."""
    vs = [site.to_enu(p.ra, p.dec, p.jd, seen=refraction) for p in points]
    hint = site.pole()
    dha = [((site.hour_angle(b.mount_ra, b.jd) - site.hour_angle(a.mount_ra, a.jd) + 180.0) % 360.0) - 180.0
           for a, b in zip(points, points[1:])]
    # a turn to a larger hour angle (west) is about the elevated pole by -dHA in the north
    sense = -1.0 if site.north else 1.0
    info = {"points": len(points), "ha_steps_deg": dha}
    if len(points) == 2:
        axis, radius = axis_two_points(vs[0], vs[1], sense * dha[0], hint)
    elif len(points) == 3:
        axis, radius = axis_three_points(*vs, hint)
        try:              # the two-point answer from the outer points: a check
            two, _ = axis_two_points(vs[0], vs[2], sense * (dha[0] + dha[1]), hint)
            info["two_point_diff_arcmin"] = sep_deg(two, axis) * 60.0
        except ValueError:
            pass
    else:
        raise ValueError("two or three points")
    info["radius_deg"] = radius
    return axis, info


def axis_error(site, axis):
    """How the axis sits against the pole: dict alt, az (the knob turns that remove it: alt > 0
    raise, az > 0 east), alt_err / az_err (axis minus pole, deg), total (deg), axis alt / az."""
    alt_a, az_a = altaz(axis)
    alt_p, az_p = altaz(site.pole())
    (alt, az), _ = solve_knobs(site, axis, site.pole())
    return {"alt": alt, "az": az, "alt_err": alt_a - alt_p,
            "az_err": ((az_a - az_p + 180.0) % 360.0) - 180.0,
            "total": sep_deg(axis, site.pole()), "axis_alt": alt_a, "axis_az": az_a}


# ---- the live model ----
@dataclass
class Alignment:
    """The measured axis and the reference plate solution (its WCS as two functions: pixel ->
    J2000 and J2000 -> pixel), taken at jd: where things appear on the image for a given knob
    turn at a later time, the mount tracking about the axis in between."""
    site: Site
    axis: np.ndarray
    jd: float
    pix2sky: object             # (x, y) -> (ra, dec)
    sky2pix: object             # (ra, dec) -> (x, y)
    knobs: tuple = (0.0, 0.0)   # turned since the reference (alt, az), deg
    star: tuple = None          # the followed star, J2000
    target: tuple = field(default=None)    # (alt, az) turns that put the axis on the pole

    def __post_init__(self):
        self.axis = np.asarray(self.axis, float) / np.linalg.norm(self.axis)
        self.target, _ = solve_knobs(self.site, self.axis, self.site.pole())

    def tracked(self, jd):
        """The mount's turn about its axis by tracking from the reference to jd."""
        sense = -1.0 if self.site.north else 1.0
        return rotation(self.axis, sense * SIDEREAL_DEG_PER_S * (jd - self.jd) * 86400.0)

    def telescope(self, jd, knobs):
        """The camera's turn from the reference: tracking about the measured axis, then the knobs
        (equal to tracking about the turned axis after the knobs)."""
        return self.site.knob_rotation(*knobs) @ self.tracked(jd)

    def _pixel_of_camera_dir(self, c):
        return self.sky2pix(*self.site.to_sky(c, self.jd))

    def _camera_dir_of_pixel(self, x, y):
        return self.site.to_enu(*self.pix2sky(x, y), self.jd)

    def pixel(self, ra, dec, jd, knobs=None):
        """Where a star (J2000) shows on the image at jd after the knob turns (now's if None)."""
        knobs = self.knobs if knobs is None else knobs
        u = self.site.to_enu(ra, dec, jd)
        return self._pixel_of_camera_dir(self.telescope(jd, knobs).T @ u)

    def pick(self, x, y, jd):
        """The star at pixel (x, y) at jd, with the knob turns known now, is the one followed."""
        c = self._camera_dir_of_pixel(x, y)
        self.star = self.site.to_sky(self.telescope(jd, self.knobs) @ c, jd)
        return self.star

    def knobs_from_star(self, x, y, jd):
        """The knob turns since the reference from where the followed star is now; also kept."""
        if self.star is None:
            raise ValueError("no star picked")
        w = self._camera_dir_of_pixel(x, y)
        u = self.site.to_enu(*self.star, jd)
        self.knobs, _ = solve_knobs(self.site, self.tracked(jd) @ w, u, start=self.knobs)
        return self.knobs

    def axis_now(self):
        return self.site.knob_rotation(*self.knobs) @ self.axis

    def remaining(self):
        """The turns still to make: (alt, az) deg, and the error of the axis now (axis_error)."""
        return ((self.target[0] - self.knobs[0], self.target[1] - self.knobs[1]),
                axis_error(self.site, self.axis_now()))

    def arrow(self, jd):
        """[(x, y)] of the followed star now (as the model has it), after the Az turn left, and
        after the Alt turn too (where it has to go)."""
        if self.star is None:
            return None
        a, b = self.knobs
        at, bt = self.target
        return [self.pixel(*self.star, jd, k) for k in ((a, b), (a, bt), (at, bt))]

    def sensitivity(self, jd):
        """(px per arcmin of the Alt knob, px per arcmin of the Az knob) for the followed star:
        small means that knob barely moves it here (pick another field: higher, nearer the
        meridian or the pole)."""
        if self.star is None:
            return None
        p0 = np.array(self.pixel(*self.star, jd))
        d = 1.0 / 60.0
        a, b = self.knobs
        return tuple(float(np.hypot(*(np.array(self.pixel(*self.star, jd, k)) - p0)))
                     for k in ((a + d, b), (a, b + d)))

    def pole_and_axis_pixels(self, jd):
        """Pixels of the pole (moves with the knobs) and of the RA axis (fixed on the image)."""
        pole_cam = self.telescope(jd, self.knobs).T @ self.site.pole()
        return self._pixel_of_camera_dir(pole_cam), self._pixel_of_camera_dir(self.axis)

    def rebase(self, pix2sky, sky2pix, jd, centre_pixel, centre_radec):
        """A new plate solution at the same mount position: the knob turns since the reference
        follow from where the solved centre is against the model; the axis becomes the turned
        one, the new solution the reference, the turns zero."""
        w = self._camera_dir_of_pixel(*centre_pixel)
        u = self.site.to_enu(*centre_radec, jd)
        knobs, _ = solve_knobs(self.site, self.tracked(jd) @ w, u, start=self.knobs)
        axis = self.site.knob_rotation(*knobs) @ self.tracked(jd) @ self.axis
        self.axis = axis / np.linalg.norm(axis)
        self.jd, self.pix2sky, self.sky2pix = jd, pix2sky, sky2pix
        self.knobs, self.star = (0.0, 0.0), None
        self.target, _ = solve_knobs(self.site, self.axis, self.site.pole())
        return knobs


# ---- where the RA axis may turn ----
def plan_moves(site, mount_ra, mount_dec, jd, step_deg, moves, direction="auto", min_alt=20.0,
               cross_meridian=False, prefer=None):
    """The RA turns for the next points from the mount's position now (JNow): (sign, [(ra, dec)
    JNow targets], lowest altitude on the way), sign +1 west (hour angle up), -1 east. The path
    (a circle at the mount's Dec) must stay above min_alt and, unless cross_meridian, on its
    side of the meridian (a German mount would flip: the Dec axis moves). With direction "auto",
    prefer (+1 / -1) is taken when it fits (repeated measurements turn back and forth, so the
    mount does not creep towards its limits), else the direction with the higher lowest altitude.
    ValueError with the reason when neither direction fits."""
    ha0 = site.hour_angle(mount_ra, jd)
    total = step_deg * moves
    signs = {"west": [1], "east": [-1]}.get(direction, [1, -1])
    tried = []
    best = None
    for sign in signs:
        has = [ha0 + sign * total * f for f in np.linspace(0.0, 1.0, max(3, int(total / 2) + 1))]
        low = min(_alt_at(site, ha, mount_dec) for ha in has)
        crosses = any((a < 0) != (ha0 < 0) for a in has) or abs(ha0 + sign * total) > 180.0
        name = "west" if sign > 0 else "east"
        if low < min_alt:
            tried.append(f"{name}: down to {low:.0f} deg altitude (min. {min_alt:g})")
            continue
        if crosses and not cross_meridian:
            tried.append(f"{name}: crosses the meridian")
            continue
        if best is None or low > best[2] or (direction == "auto" and sign == prefer):
            targets = [((mount_ra - sign * step_deg * (i + 1)) % 360.0, mount_dec) for i in range(moves)]
            best = (sign, targets, low)
            if direction == "auto" and sign == prefer:
                break
    if best is None:
        raise ValueError("no room to turn the RA axis by " + f"{total:g} deg: " + "; ".join(tried)
                         + ". A smaller step, a lower minimum altitude, or another field.")
    return best


def _alt_at(site, ha_deg, dec_deg):
    v = _hadec_to_enu(site.lat) @ unit(-ha_deg, dec_deg)
    return altaz(v)[0]


def describe(err):
    """Two lines for the knobs, e.g. 'Alt: raise 12.3′', 'Az: move east 4.1′'."""
    def amount(deg):
        m = abs(deg) * 60.0
        return f"{m / 60:.2f}°" if m >= 100 else f"{m:.1f}′"
    alt, az = err["alt"], err["az"]
    return (f"Alt: {'raise' if alt > 0 else 'lower'} {amount(alt)}",
            f"Az: move {'east' if az > 0 else 'west'} {amount(az)}")
