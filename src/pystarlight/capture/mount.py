"""A telescope mount through INDI (capture/indi.py): goto, sync, tracking, park, site and time.

NEW CODE, standard library only. Positions in and out are J2000 (ICRS; plate solutions and
catalogues): INDI mounts take the current epoch (EQUATORIAL_EOD_COORD, "JNow"), so they are
precessed both ways (IAU 1976, Meeus ch. 21; nutation and aberration, under 1', are left to
the plate-solve sync). A mount that offers EQUATORIAL_COORD (J2000) is given J2000 directly.

goto() follows ASTRID: the slew always runs with ON_COORD_SET = TRACK (some mounts ignore
SLEW-without-tracking, and the end of a slew is seen as the coordinates' state leaving Busy);
tracking is switched off afterwards when the slew was asked for without it (pre-pointing).
"""
import math
import threading
import time
from datetime import datetime, timezone

from .indi import TELESCOPE

J2000_JD = 2451545.0


def julian_day(when=None):
    """JD of a UTC datetime (now if None)."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp() / 86400.0 + 2440587.5


def _matrix(jd):
    """IAU 1976 precession matrix J2000 -> mean equator and equinox of jd (Meeus 21.2-21.4)."""
    t = (jd - J2000_JD) / 36525.0
    a = math.radians(1 / 3600)
    zeta = (2306.2181 * t + 0.30188 * t * t + 0.017998 * t ** 3) * a
    z = (2306.2181 * t + 1.09468 * t * t + 0.018203 * t ** 3) * a
    theta = (2004.3109 * t - 0.42665 * t * t - 0.041833 * t ** 3) * a
    cz, sz, cZ, sZ, ct, st = (math.cos(zeta), math.sin(zeta), math.cos(z), math.sin(z),
                              math.cos(theta), math.sin(theta))
    return ((cZ * ct * cz - sZ * sz, -cZ * ct * sz - sZ * cz, -cZ * st),
            (sZ * ct * cz + cZ * sz, -sZ * ct * sz + cZ * cz, -sZ * st),
            (st * cz, -st * sz, ct))


def _rotate(m, ra_deg, dec_deg, inverse=False):
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    v = (math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec))
    if inverse:
        m = tuple(zip(*m))
    x, y, zz = (sum(m[i][k] * v[k] for k in range(3)) for i in range(3))
    return math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.asin(max(-1.0, min(1.0, zz))))


def j2000_to_jnow(ra_deg, dec_deg, jd=None):
    return _rotate(_matrix(jd or julian_day()), ra_deg, dec_deg)


def jnow_to_j2000(ra_deg, dec_deg, jd=None):
    return _rotate(_matrix(jd or julian_day()), ra_deg, dec_deg, inverse=True)


def sidereal_deg(jd, lon_east_deg):
    """Local mean sidereal time (deg) at a UTC JD (IAU 1982 GMST, Meeus 12.4)."""
    t = (jd - J2000_JD) / 36525.0
    gmst = (280.46061837 + 360.98564736629 * (jd - J2000_JD) + 0.000387933 * t * t
            - t ** 3 / 38710000.0)
    return (gmst + lon_east_deg) % 360.0


class Mount:
    """The mount device of an INDI client (the first with the TELESCOPE interface)."""

    def __init__(self, client, device=None, timeout=10.0):
        self.client = client
        self.device = device or client.find_device(TELESCOPE, timeout)
        if self.device is None:
            raise RuntimeError("no mount among the INDI devices")
        self._untrack = None            # a goto without tracking: switch it off once there

    # ---- connection, site, time ----
    def set_link(self, mode="", port="", timeout=5.0):
        """How the driver reaches the mount, before connecting: mode 'usb' (serial) / 'wifi' (TCP) /
        '' (the driver's default; the AM5 driver's is Wi-Fi). port: usb: a serial device, or '' for
        the listed system port named ZWO, else the only one listed (the driver's auto search stays
        on); wifi: 'host' or 'host:port' of the mount ('': the driver's, the AM5's own hotspot
        192.168.4.1:4030). Returns the port or address used, if known."""
        c, dev = self.client, self.device
        if mode and c.wait_property(dev, "CONNECTION_MODE", timeout):
            want = "CONNECTION_SERIAL" if mode == "usb" else "CONNECTION_TCP"
            if want in (c.switch(dev, "CONNECTION_MODE") or {}):
                c.set_switch(dev, "CONNECTION_MODE", {want: True})
        if mode == "wifi":
            if not c.wait_property(dev, "DEVICE_ADDRESS", timeout):
                return None
            if port:
                host, _, tcp = port.partition(":")
                tcp = tcp or (c.text(dev, "DEVICE_ADDRESS") or {}).get("PORT", "")
                c.set_text(dev, "DEVICE_ADDRESS", {"ADDRESS": host, "PORT": tcp})
            a = c.text(dev, "DEVICE_ADDRESS") or {}
            return f"{a.get('ADDRESS', '')}:{a.get('PORT', '')}"
        if not c.wait_property(dev, "DEVICE_PORT", timeout):
            return None
        if port:
            c.set_text(dev, "DEVICE_PORT", {"PORT": port})
            return port
        c.wait_property(dev, "SYSTEM_PORTS", 2.0)
        ports = list(c.switch(dev, "SYSTEM_PORTS") or {})
        pick = next((p for p in ports if "zwo" in p.lower()), ports[0] if len(ports) == 1 else None)
        if pick:
            c.set_switch(dev, "SYSTEM_PORTS", {pick: True})
            c.wait(lambda: (c.text(dev, "DEVICE_PORT") or {}).get("PORT", "").endswith(pick)
                   or pick in (c.text(dev, "DEVICE_PORT") or {}).get("PORT", ""), 2.0)
        return (c.text(dev, "DEVICE_PORT") or {}).get("PORT")

    def connect(self, timeout=20.0):
        if not self.client.connect_device(self.device, timeout):
            raise RuntimeError(f"{self.device}: could not connect")
        self.client.wait_property(self.device, "EQUATORIAL_EOD_COORD", timeout)
        return self

    def set_site(self, lat, lon, elev_m=0.0):
        """lon east positive (INDI wants 0 .. 360 east)."""
        if self.client.get(self.device, "GEOGRAPHIC_COORD") is not None:
            self.client.set_number(self.device, "GEOGRAPHIC_COORD",
                                   {"LAT": lat, "LONG": lon % 360.0, "ELEV": elev_m or 0.0})

    def set_time(self, when=None):
        when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.client.get(self.device, "TIME_UTC") is not None:
            self.client.set_text(self.device, "TIME_UTC",
                                 {"UTC": when.strftime("%Y-%m-%dT%H:%M:%S"), "OFFSET": "0"})

    # ---- state ----
    def _j2000_property(self):
        return self.client.get(self.device, "EQUATORIAL_COORD") is not None

    def position(self):
        """(ra_deg, dec_deg) J2000 where the mount points, or None."""
        if self._j2000_property():
            v = self.client.number(self.device, "EQUATORIAL_COORD")
            return (v["RA"] * 15.0, v["DEC"]) if v else None
        v = self.client.number(self.device, "EQUATORIAL_EOD_COORD")
        return jnow_to_j2000(v["RA"] * 15.0, v["DEC"]) if v else None

    def slewing(self):
        name = "EQUATORIAL_COORD" if self._j2000_property() else "EQUATORIAL_EOD_COORD"
        return self.client.state(self.device, name) == "Busy"

    def tracking(self):
        return bool((self.client.switch(self.device, "TELESCOPE_TRACK_STATE") or {}).get("TRACK_ON"))

    def parked(self):
        return bool((self.client.switch(self.device, "TELESCOPE_PARK") or {}).get("PARK"))

    def status(self):
        """Everything the GUI shows, in one dict."""
        pos = self.position()
        return {"device": self.device, "connected": bool(
                    (self.client.switch(self.device, "CONNECTION") or {}).get("CONNECT")),
                "ra": pos[0] if pos else None, "dec": pos[1] if pos else None,
                "slewing": self.slewing() or self.homing(), "tracking": self.tracking(),
                "parked": self.parked(), "can_home": self.can_home(),
                "message": self.client.messages[-1][1] if self.client.messages else ""}

    # ---- motion ----
    def _send_coords(self, ra_deg, dec_deg):
        if self._j2000_property():
            self.client.set_number(self.device, "EQUATORIAL_COORD", {"RA": ra_deg / 15.0, "DEC": dec_deg})
        else:
            ra, dec = j2000_to_jnow(ra_deg, dec_deg)
            self.client.set_number(self.device, "EQUATORIAL_EOD_COORD", {"RA": ra / 15.0, "DEC": dec})

    def goto(self, ra_deg, dec_deg, track=True):
        """Slew to a J2000 position; track=False switches tracking off once there (pre-point)."""
        self.client.set_switch(self.device, "ON_COORD_SET", {"TRACK": True})
        self._send_coords(ra_deg, dec_deg)
        self._untrack = None
        if not track:
            self._untrack = threading.Thread(target=self._track_off_after_slew, daemon=True)
            self._untrack.start()

    def _track_off_after_slew(self, timeout=600.0):
        self.client.wait(lambda: self.slewing(), 3.0)              # the slew has started
        if self.client.wait(lambda: not self.slewing(), timeout):
            self.set_tracking(False)

    def wait_slew(self, timeout=600.0):
        """Block until a slew has ended (True) or timeout."""
        time.sleep(0.5)
        return self.client.wait(lambda: not self.slewing(), timeout)

    def sync(self, ra_deg, dec_deg):
        """Tell the mount it points at this J2000 position (from a plate solve)."""
        self.client.set_switch(self.device, "ON_COORD_SET", {"SYNC": True})
        self._send_coords(ra_deg, dec_deg)
        self.client.wait(lambda: not self.slewing(), 5.0)
        self.client.set_switch(self.device, "ON_COORD_SET", {"TRACK": True})

    # ---- manual motion (jog) ----
    JOG = {"N": ("TELESCOPE_MOTION_NS", "MOTION_NORTH"), "S": ("TELESCOPE_MOTION_NS", "MOTION_SOUTH"),
           "W": ("TELESCOPE_MOTION_WE", "MOTION_WEST"), "E": ("TELESCOPE_MOTION_WE", "MOTION_EAST")}

    def rates(self):
        """[(element, label)] of the driver's slew rates (AM5: 0.25x .. 1440x sidereal), and the
        current element ([], None when the driver has none)."""
        prop = self.client.get(self.device, "TELESCOPE_SLEW_RATE")
        if prop is None:
            return [], None
        return ([(k, prop.labels.get(k, k)) for k in prop.values],
                next((k for k, on in prop.values.items() if on), None))

    def set_rate(self, element):
        self.client.set_switch(self.device, "TELESCOPE_SLEW_RATE", {element: True})

    def jog(self, direction, on):
        """Move N / S / W / E at the slew rate while on; on=False stops that axis."""
        name, element = self.JOG[direction]
        prop = self.client.get(self.device, name)
        if prop is None:
            return
        self.client.set_switch(self.device, name, {element: True} if on else {k: False for k in prop.values})

    def stop_jog(self):
        for name in ("TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE"):
            prop = self.client.get(self.device, name)
            if prop is not None and any(prop.values.values()):
                self.client.set_switch(self.device, name, {k: False for k in prop.values})

    def set_tracking(self, on):
        if self.client.get(self.device, "TELESCOPE_TRACK_STATE") is not None:
            self.client.set_switch(self.device, "TELESCOPE_TRACK_STATE",
                                   {"TRACK_ON": True} if on else {"TRACK_OFF": True})

    def abort(self):
        self.client.set_switch(self.device, "TELESCOPE_ABORT_MOTION", {"ABORT": True})

    # home: INDI 2's standard TELESCOPE_HOME (FIND / SET / GO), else a driver's own GO_HOME (the
    # AM5 driver of INDI 1.9); the element to switch on is read from the driver
    HOME_PROPERTIES = ("TELESCOPE_HOME", "GO_HOME")

    def _home_property(self):
        for name in self.HOME_PROPERTIES:
            prop = self.client.get(self.device, name)
            if prop is not None and prop.values:
                return prop
        return None

    def can_home(self):
        return self._home_property() is not None

    def homing(self):
        prop = self._home_property()
        return prop is not None and prop.state == "Busy"

    def home(self):
        """Slew to the mount's home position."""
        prop = self._home_property()
        if prop is None:
            raise RuntimeError(f"{self.device}: the driver offers no home position")
        element = "GO" if "GO" in prop.values else next(iter(prop.values))
        self.client.set_switch(self.device, prop.name, {element: True})

    def park(self, on=True):
        if self.client.get(self.device, "TELESCOPE_PARK") is not None:
            self.client.set_switch(self.device, "TELESCOPE_PARK", {"PARK": True} if on else {"UNPARK": True})


# ---------------------------------------------------------------------------
# Coordinates as typed and shown
# ---------------------------------------------------------------------------
def parse_radec(text):
    """'310.357 45.28' (degrees), '20:41:25.7 +45:16:49', '20 41 25.7 45 16 49' or
    '20h41m25.7s +45d16m49s' -> (ra_deg, dec_deg); ValueError if it is none of them."""
    s = text.strip().lower()
    for ch in "hmd°'\"s,":
        s = s.replace(ch, " ")
    s = s.replace(":", " ").replace("+", " +").replace("−", "-")
    parts = s.split()
    if len(parts) == 2:
        ra, dec = float(parts[0]), float(parts[1])
    elif len(parts) == 6 or (len(parts) > 3 and ":" in text):
        h, m, sec = (float(p) for p in parts[:3])
        d = [float(p) for p in parts[3:6]] + [0.0] * (6 - len(parts))
        sign = -1.0 if parts[3].startswith("-") else 1.0
        ra = 15.0 * (h + m / 60 + sec / 3600)
        dec = sign * (abs(d[0]) + d[1] / 60 + d[2] / 3600)
    else:
        raise ValueError(f"not RA Dec: {text!r}")
    if not (0 <= ra < 360 and -90 <= dec <= 90):
        raise ValueError(f"out of range: {text!r}")
    return ra, dec


def hms(ra_deg):
    h = (ra_deg % 360.0) / 15.0
    hh, rem = divmod(round(h * 36000), 36000)
    mm, ss = divmod(rem, 600)
    return f"{int(hh) % 24:02d}h{int(mm):02d}m{ss / 10:04.1f}s"


def dms(dec_deg):
    sign = "-" if dec_deg < 0 else "+"
    dd, rem = divmod(round(abs(dec_deg) * 3600), 3600)
    mm, ss = divmod(rem, 60)
    return f"{sign}{int(dd):02d}°{int(mm):02d}′{int(ss):02d}″"


def altaz(ra_deg, dec_deg, lat, lon, when=None):
    """(altitude, azimuth from north through east), degrees, of a J2000 position (no refraction)."""
    jd = julian_day(when)
    ra, dec = j2000_to_jnow(ra_deg, dec_deg, jd)
    h = math.radians(sidereal_deg(jd, lon) - ra)
    phi, d = math.radians(lat), math.radians(dec)
    alt = math.asin(math.sin(phi) * math.sin(d) + math.cos(phi) * math.cos(d) * math.cos(h))
    az = math.atan2(-math.sin(h) * math.cos(d),
                    math.cos(phi) * math.sin(d) - math.sin(phi) * math.cos(d) * math.cos(h))
    return math.degrees(alt), math.degrees(az) % 360.0


def separation(ra1, dec1, ra2, dec2):
    """Angle between two positions, degrees."""
    a1, d1, a2, d2 = (math.radians(v) for v in (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))
