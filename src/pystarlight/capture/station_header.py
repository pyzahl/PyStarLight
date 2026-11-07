"""Station, equipment and site for a new recording's header (PySyzygos'
docs/observation_interface.md, section 1).

`station_meta(observer)`: the config's [observer] table (station, observers, telescope,
camera delay ...) as recorder metadata. `site_meta(gps, observer)`: the position, from the GPS
only with a fix; without one the config's site, marked SITE-SOURCE "config"; else none at
all. The keys are the recorder's lower-case metadata names, which ravf_writer.header_entries
writes as upper-case RAVF entries (and the SER writer into its `.meta.json` sidecar).
Standard library only.
"""

FIX_NAMES = {2: "2D", 3: "3D"}

# [observer] key -> metadata key, kind
STATION_KEYS = (
    ("observer2", "observer2", str),
    ("more_observers", "observers_more", str),
    ("station", "station", str),
    ("telescope_type", "telescope_type", str),
    ("aperture_mm", "aperture_mm", float),
    ("focal_length_mm", "focal_length_mm", float),
    ("focal_reducer", "focal_reducer", str),
    ("mount", "mount", str),
    ("observing_method", "observing_method", str),
    ("camera_delay_s", "camera_delay_s", float),
    ("camera_delay_applied", "camera_delay_applied", bool),
)


LEGACY_CM = (("aperture_mm", "aperture_cm"), ("focal_length_mm", "focal_length_cm"))


def migrate(observer):
    """[observer] with the optics in mm: aperture_mm / focal_length_mm, from the cm keys of
    earlier versions (x 10) where the mm ones are missing or 0; the cm keys dropped."""
    out = dict(observer or {})
    for mm, cm in LEGACY_CM:
        old = out.pop(cm, None)
        try:
            if not float(out.get(mm) or 0) and old:
                out[mm] = round(float(old) * 10, 3)
        except (TypeError, ValueError):
            pass
    return out


def station_meta(observer):
    """Recorder metadata from the config's [observer] table; observer and telescope too (the
    command line and the GUI override those). Empty / zero values are left out."""
    observer = migrate(observer)
    out = {}
    if observer.get("name"):
        out["observer"] = str(observer["name"])
    if observer.get("telescope"):
        out["telescope"] = str(observer["telescope"])
    for key, meta, kind in STATION_KEYS:
        v = observer.get(key)
        if v in (None, ""):
            continue
        try:
            v = kind(v)
        except (TypeError, ValueError):
            continue
        if kind is float and v == 0.0:      # 0: not known
            continue
        out[meta] = v
    if "camera_delay_s" in out:
        out.setdefault("camera_delay_applied", False)
    else:
        out.pop("camera_delay_applied", None)
    return out


LAST_FIX_MAX_AGE_S = 600.0     # a fix lost at the start: its last position, if this recent


def site_meta(gps, observer=None, now=None):
    """The site of a new recording, taken when it starts. With a GPS fix: its position (altitude
    above mean sea level only with a 3D fix), the geoid separation, the fix, satellites, HDOP and
    the fix's UTC, SITE-SOURCE 'GPS'. The fix lost just then (the GUI arms only with a fix): the
    receiver's last fixed position if at most LAST_FIX_MAX_AGE_S old, still 'GPS', with
    GPS-FIX-AGE-S. Else the config's [observer] latitude / longitude / altitude if set,
    SITE-SOURCE 'config'; else SITE-SOURCE 'none' and no position."""
    import time
    fix = None
    if gps is not None and gps.has_fix() and gps.lat is not None and gps.lon is not None:
        fix = {"lat": gps.lat, "lon": gps.lon, "alt": gps.alt, "geoid_sep": getattr(gps, "geoid_sep", None),
               "fix": getattr(gps, "fix", 3), "sats": gps.sats_used, "hdop": gps.hdop, "utc": gps.utc}
        age = 0.0
    elif gps is not None and getattr(gps, "last_fix", None):
        age = (time.monotonic() if now is None else now) - gps.last_fix["mono"]
        if age <= LAST_FIX_MAX_AGE_S:
            fix = gps.last_fix
    if fix is not None:
        kind = fix["fix"]
        out = {"latitude": fix["lat"], "longitude": fix["lon"], "site_source": "GPS", "datum": "",
               "gps_fix": FIX_NAMES.get(kind, str(kind)), "gps_satellites": int(fix["sats"] or 0)}
        if fix["hdop"] is not None:
            out["gps_hdop"] = float(fix["hdop"])
        if kind >= 3 and fix["alt"] is not None:
            out.update(altitude=float(fix["alt"]), altitude_ref="MSL")
            if fix["geoid_sep"] is not None:
                out["geoid_separation_m"] = float(fix["geoid_sep"])
        if fix["utc"] is not None:
            out["gps_position_utc"] = fix["utc"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4] + "Z"
        if age > 0:
            out["gps_fix_age_s"] = round(age, 1)
        return _texts(out)
    observer = observer or {}
    try:
        lat, lon = float(observer.get("latitude") or 0), float(observer.get("longitude") or 0)
        alt = float(observer.get("altitude") or 0)
    except (TypeError, ValueError):
        lat = lon = alt = 0.0
    if lat or lon:
        out = {"latitude": lat, "longitude": lon, "site_source": "config", "datum": ""}
        if alt:
            out.update(altitude=alt, altitude_ref="MSL")
        return _texts(out)
    return {"site_source": "none"}


def _texts(site):
    """Text copies of the position (RAVF keeps the numbers as float32, ~0.5 m)."""
    site["latitude_text"] = f"{site['latitude']:.7f}"
    site["longitude_text"] = f"{site['longitude']:.7f}"
    if "altitude" in site:
        site["altitude_text"] = f"{site['altitude']:.1f}"
    return site


# The station dialog of the camera GUI: [observer] key, label, kind, required, choices, hint.
# kind: str / float / bool; choices: offered in an editable list (the IOTA-ES report form's).
TELESCOPE_TYPES = ("Refractor", "Newtonian", "SCT", "Dobsonian", "Binoculars", "Other", "None",
                   "Electronic")
OBSERVING_METHODS = (("a", "video"), ("d", "sequential images"))
FIELDS = (
    ("name", "Observer", str, True, (), "observer 1, responsible for this station"),
    ("email", "Email", str, False, (), "for the report"),
    ("observer2", "Observer 2", str, False, (), "further operator of this station"),
    ("more_observers", "More observers", str, False, (), "comma separated"),
    ("station", "Station", str, True, (), 'station name, e.g. "Sternwarte Seewis Dorf (SWD)"'),
    ("telescope", "Telescope", str, True, (), 'description, e.g. "C-14 EHD" or "APO107"'),
    ("telescope_type", "Telescope type", str, True, TELESCOPE_TYPES, "as in the IOTA-ES report form"),
    ("aperture_mm", "Aperture (mm)", float, True, (), ""),
    ("focal_length_mm", "Focal length (mm, effective)", float, True, (),
     "EFFECTIVE focal length, with the reducer / Barlow (e.g. 750 mm x 0.5 = 375 mm)"),
    ("focal_reducer", "Focal reducer / Barlow", str, False, (), 'e.g. "0.5x"; empty = none'),
    ("mount", "Mount", str, True, (), ""),
    ("camera", "Camera (notes)", str, False, (), "the recording names the camera model itself"),
    ("observing_method", "Observing method", str, True, tuple(f"{c} ({t})" for c, t in OBSERVING_METHODS),
     "IOTA-ES code: a = video, d = sequential images, ..."),
    ("camera_delay_s", "Camera delay (s)", float, False, (), "known timing offset; 0 = none known"),
    ("camera_delay_applied", "Delay already in the time stamps", bool, False, (), ""),
    ("latitude", "Fallback latitude (deg)", float, False, (),
     "only used without a GPS fix (marked SITE-SOURCE config); + north"),
    ("longitude", "Fallback longitude (deg)", float, False, (), "+ east"),
    ("altitude", "Fallback altitude (m, MSL)", float, False, (), ""),
)
LABELS = {k: label for k, label, *_ in FIELDS}


def effective(observer, args_observer="", args_telescope=""):
    """[observer] with the command line's --observer / --telescope over name / telescope."""
    out = dict(observer or {})
    if args_observer:
        out["name"] = args_observer
    if args_telescope:
        out["telescope"] = args_telescope
    return out


def optics_mm(observer):
    """(aperture, effective focal length) in mm, 0 where unknown (earlier cm keys converted)."""
    o = migrate(observer)
    try:
        return float(o.get("aperture_mm") or 0), float(o.get("focal_length_mm") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def implausible(observer):
    """Values that cannot be right (cm typed where mm are asked, a native focal length...):
    aperture 10 mm .. 1 m, focal length 50 mm .. 20 m, focal ratio f/1 .. f/40, telescope type
    one of TELESCOPE_TYPES."""
    o = observer or {}
    out = []
    d, f = optics_mm(o)
    if d and not 10 <= d <= 1000:
        out.append(f"aperture {d:g} mm?")
    if f and not 50 <= f <= 20000:
        out.append(f"focal length {f:g} mm?")
    if d and f and not 1 <= f / d <= 40:
        out.append(f"focal ratio f/{f / d:.1f}?")
    t = str(o.get("telescope_type") or "").strip()
    if t and t not in TELESCOPE_TYPES:
        out.append(f'telescope type "{t}" (one of {", ".join(TELESCOPE_TYPES)})')
    return out


def optics_line(observer, pixel_um=None):
    """'107 mm, effective 375 mm f/3.5, 1.32"/px (2.4 um pixels)' (what the values imply)."""
    d, f = optics_mm(observer)
    parts = [f"{d:g} mm" if d else "", f"effective {f:g} mm" if f else ""]
    if d and f:
        parts.append(f"f/{f / d:.1f}")
    if f and pixel_um:
        parts.append(f'{206.265 * pixel_um / f:.2f}"/px ({pixel_um:g} um pixels)')
    return ", ".join(p for p in parts if p)


def missing(observer, need_site=False):
    """Labels of the required station fields that are empty (or 0), and values that cannot be
    right (implausible); need_site: no GPS receiver, so the fallback latitude / longitude are
    required too."""
    observer = migrate(observer)
    out = []
    for key, label, kind, required, *_ in FIELDS:
        if not required:
            continue
        v = observer.get(key)
        if kind is float:
            try:
                ok = float(v or 0) > 0
            except (TypeError, ValueError):
                ok = False
        else:
            ok = bool(str(v or "").strip())
        if not ok:
            out.append(label)
    if need_site:
        try:
            ok = bool(float(observer.get("latitude") or 0) or float(observer.get("longitude") or 0))
        except (TypeError, ValueError):
            ok = False
        if not ok:
            out.append("site (no GPS: fallback latitude / longitude)")
    return out + implausible(observer)


def summary(observer):
    """One line for the GUI: 'Station - Observer - telescope - 107 mm / 343 mm'."""
    o = observer or {}
    parts = [str(o.get("station") or "").strip(), str(o.get("name") or "").strip(),
             str(o.get("telescope") or "").strip()]
    d, f = optics_mm(o)
    if d and f:
        parts.append(f"{d:g} mm / {f:g} mm")
    return " - ".join(p for p in parts if p)


# ---- light protection: a digest of what describes the observation ----
# Not a signature (anyone can recompute it): it catches a value changed by accident, e.g. by
# an editor or another program rewriting the header or the SER sidecar, on the way from the
# camera to the report. The RAVF header / SER sidecar carry it as STATION-DIGEST.
DIGEST_KEYS = (
    "OBSERVER", "OBSERVER-2", "OBSERVERS-MORE", "STATION", "TELESCOPE", "TELESCOPE-TYPE", "APERTURE-MM",
    "FOCAL-LENGTH-MM", "FOCAL-REDUCER", "MOUNT", "OBSERVING-METHOD", "CAMERA-DELAY-S", "CAMERA-DELAY-APPLIED",
    "SITE-SOURCE", "LATITUDE-TEXT", "LONGITUDE-TEXT", "ALTITUDE-TEXT", "ALTITUDE-REF", "GEOID-SEPARATION-M",
    "DATUM", "GPS-FIX", "GPS-SATELLITES", "GPS-HDOP", "GPS-POSITION-UTC", "GPS-FIX-AGE-S",
    "INSTRUMENT", "INSTRUMENT-VENDOR", "INSTRUMENT-SERIAL", "INSTRUMENT-SENSOR", "INSTRUMENT-SHUTTER",
    "RECORDER-SOFTWARE", "RECORDER-SOFTWARE-VERSION", "TIMESTAMP-SOURCE", "TIMESTAMP-ROW",
    "FRAME-TIMING-ACCURACY", "TRIGGER-DELAY-NS", "OBJNAME",
    # added later: older recordings lack them, which leaves their digest as it was
    "TIMESTAMP-REFERENCE", "INSTRUMENT-SENSOR-PIXEL-SIZE-X",
)
DIGEST_ENTRY = "STATION-DIGEST"


def _norm(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    return str(v).rstrip("\0")


def digest(entries):
    """'sha256:<32 hex>' over the DIGEST_KEYS present in entries (RAVF names -> values)."""
    import hashlib
    import json
    data = {k: _norm(entries[k]) for k in DIGEST_KEYS if k in entries and entries[k] is not None}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def check(metadata):
    """'ok' (as recorded), 'changed' (a value differs from the recording's digest) or 'none'
    (no digest: another program's recording, or a SER file without its .meta.json)."""
    m = {str(k).upper(): v for k, v in (metadata or {}).items()}
    want = m.get(DIGEST_ENTRY)
    if not want:
        return "none"
    return "ok" if _norm(want) == digest(m) else "changed"


def protect(path):
    """Make a finished file read-only (owner, group, others): no accidental overwrite or edit;
    a copy (cp, rsync) keeps the mode. chmod u+w undoes it."""
    import os
    try:
        os.chmod(path, os.stat(path).st_mode & ~0o222)
    except OSError:
        pass


# ---- the first time: defaults from the PySyzygos site ----
STATION_DATA = ("station", "telescope", "aperture_mm", "focal_length_mm", "latitude", "longitude")


def unconfigured(observer):
    """No station data configured yet ([observer] has none of STATION_DATA)."""
    o = migrate(observer)
    return not any(str(o.get(k) or "").strip() not in ("", "0", "0.0") for k in STATION_DATA)


def site_defaults(site):
    """[observer] values from a PySyzygos site (sites.py / [sites.<id>]: name, lat, lon, ele,
    aperture_cm, focal_mm, camera), for the station dialog the first time only."""
    s = site or {}
    out = {}
    for key, src, scale in (("station", "name", None), ("latitude", "lat", 1), ("longitude", "lon", 1),
                            ("altitude", "ele", 1), ("aperture_mm", "aperture_cm", 10),
                            ("focal_length_mm", "focal_mm", 1), ("camera", "camera", None)):
        v = s.get(src)
        if v in (None, ""):
            continue
        try:
            out[key] = str(v) if scale is None else round(float(v) * scale, 7)
        except (TypeError, ValueError):
            continue
    return out
