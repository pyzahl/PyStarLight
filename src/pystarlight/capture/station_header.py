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
    ("aperture_cm", "aperture_cm", float),
    ("focal_length_cm", "focal_length_cm", float),
    ("focal_reducer", "focal_reducer", str),
    ("mount", "mount", str),
    ("observing_method", "observing_method", str),
    ("camera_delay_s", "camera_delay_s", float),
    ("camera_delay_applied", "camera_delay_applied", bool),
)


def station_meta(observer):
    """Recorder metadata from the config's [observer] table; observer and telescope too (the
    command line and the GUI override those). Empty / zero values are left out."""
    observer = observer or {}
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


def site_meta(gps, observer=None):
    """The site of a new recording. With a GPS fix: its position (altitude above mean sea level
    only with a 3D fix), the geoid separation, the fix, satellites, HDOP and the fix's UTC,
    SITE-SOURCE 'GPS'. Without a fix: the config's [observer] latitude / longitude / altitude
    if set, SITE-SOURCE 'config'; else SITE-SOURCE 'none' and no position."""
    if gps is not None and gps.has_fix() and gps.lat is not None and gps.lon is not None:
        fix = getattr(gps, "fix", 3)
        out = {"latitude": gps.lat, "longitude": gps.lon, "site_source": "GPS", "datum": "",
               "gps_fix": FIX_NAMES.get(fix, str(fix)), "gps_satellites": int(gps.sats_used or 0)}
        if gps.hdop is not None:
            out["gps_hdop"] = float(gps.hdop)
        if fix >= 3 and gps.alt is not None:
            out.update(altitude=float(gps.alt), altitude_ref="MSL")
            if getattr(gps, "geoid_sep", None) is not None:
                out["geoid_separation_m"] = float(gps.geoid_sep)
        if gps.utc is not None:
            out["gps_position_utc"] = gps.utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-4] + "Z"
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
    ("aperture_cm", "Aperture (cm)", float, True, (), ""),
    ("focal_length_cm", "Focal length (cm)", float, True, (),
     "EFFECTIVE focal length, with the reducer / Barlow"),
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


def missing(observer, need_site=False):
    """Labels of the required station fields that are empty (or 0); need_site: no GPS receiver,
    so the fallback latitude / longitude are required too."""
    observer = observer or {}
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
    return out


def summary(observer):
    """One line for the GUI: 'Station - Observer - telescope 10.7 cm / 34.3 cm'."""
    o = observer or {}
    parts = [str(o.get("station") or "").strip(), str(o.get("name") or "").strip(),
             str(o.get("telescope") or "").strip()]
    try:
        if float(o.get("aperture_cm") or 0) and float(o.get("focal_length_cm") or 0):
            parts.append(f'{float(o["aperture_cm"]):g} cm / {float(o["focal_length_cm"]):g} cm')
    except (TypeError, ValueError):
        pass
    return " - ".join(p for p in parts if p)
