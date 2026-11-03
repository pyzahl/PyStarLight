"""Station data of a recording in one layout for every video format: `recording.station` of
`<movie>.event.json` (PySyzygos' docs/observation_interface.md, section 2).

NEW CODE. RAVF (PyStarLight's capture, ASTRID), ADV/AAV (DVTI CamControl, OccuRec) and SER
(header: observer, instrument, telescope; PyStarLight's capture adds a `.ser.meta.json`
sidecar with RAVF names) name the same things differently. `station_of(metadata, fmt)` reads
them into

    {"format", "recorder", "site": {lat, lon, alt_m, alt_ref, geoid_separation_m, datum, source,
     gps_fix, satellites, hdop, position_utc}, "station", "observers": [...],
     "equipment": {telescope, telescope_type, aperture_cm, focal_length_cm, focal_reducer, mount,
     camera, serial, sensor, method}, "timing": {source, source_text, exposure_s, accuracy_s,
     camera_delay_s, delay_applied, trigger_delay_s, trigger_delay_applied}}

Values that are not in the recording are left out (never filled from the config). site.source
says where the position came from: "GPS" (the capture's GPS fix), "config" (the capture's
fallback, no fix), "recorder" (an ADV or older file: GPS or typed in, unknown) or "none".

"integrity": the recording's STATION-DIGEST checked against its values
(capture.station_header.check): "ok" (as recorded), "changed" (a value was edited after
recording), "none" (no digest: another program's file, an older capture, or a SER file whose
.ser.meta.json did not come along).
"""

from __future__ import annotations

import math
import os

from .capture.station_header import check

FORMATS = {'.ravf': 'RAVF', '.adv': 'ADV', '.aav': 'AAV', '.ser': 'SER', '.avi': 'AVI', '.mov': 'MOV',
           '.mp4': 'MP4'}


def movie_format(path) -> str:
    """'RAVF', 'ADV', 'SER', ... from the file name; 'FITS' for a folder."""
    p = str(path)
    if os.path.isdir(p):
        return 'FITS'
    return FORMATS.get(os.path.splitext(p)[1].lower(), os.path.splitext(p)[1].lstrip('.').upper())


def _upper(metadata: dict) -> dict:
    return {str(k).upper(): v for k, v in (metadata or {}).items()}


def _text(v) -> str:
    if v is None:
        return ''
    if isinstance(v, bytes):
        v = v.decode('utf-8', 'replace')
    return str(v).strip().rstrip('\0')


def _num(v) -> float | None:
    try:
        x = float(_text(v)) if not isinstance(v, (int, float)) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _first(m: dict, *keys) -> str:
    for k in keys:
        t = _text(m.get(k))
        if t:
            return t
    return ''


def _time_source(m: dict) -> tuple[str, str]:
    """(GPS / NTP / '' , the recording's own text)."""
    own = _first(m, 'TIME-SOURCE')
    raw = _first(m, 'TIMESTAMP-SOURCE')
    if own:
        return own, raw or own
    if 'GPS UTC' in raw or raw.upper() == 'GPS':
        return 'GPS', raw
    if 'NTP' in raw:
        return 'NTP', raw
    return '', raw


def station_of(metadata: dict, fmt: str = '') -> dict:
    """The station block of event.json from a movie's metadata (keys in any case)."""
    m = _upper(metadata)
    fmt = (fmt or '').upper()
    out: dict = {'format': fmt, 'integrity': check(metadata)}
    rec = _first(m, 'RECORDER-SOFTWARE', 'RECORDER')
    ver = _first(m, 'RECORDER-SOFTWARE-VERSION')
    if rec:
        out['recorder'] = f'{rec} {ver}'.strip()
    elif fmt == 'SER' and _first(m, 'FILEID'):
        out['recorder'] = f'SER ({_first(m, "FILEID")})'

    # site
    lat = _num(m.get('LATITUDE-TEXT')) if _first(m, 'LATITUDE-TEXT') else _num(m.get('LATITUDE'))
    lon = _num(m.get('LONGITUDE-TEXT')) if _first(m, 'LONGITUDE-TEXT') else _num(m.get('LONGITUDE'))
    alt = _num(m.get('ALTITUDE-TEXT')) if _first(m, 'ALTITUDE-TEXT') else _num(m.get('ALTITUDE'))
    source = _first(m, 'SITE-SOURCE')
    site: dict = {}
    if source.lower() != 'none' and lat is not None and lon is not None and (lat or lon):
        site = {'lat': lat, 'lon': lon}
        if alt is not None:
            site['alt_m'] = alt
        if _first(m, 'ALTITUDE-REF'):
            site['alt_ref'] = _first(m, 'ALTITUDE-REF')
        if _num(m.get('GEOID-SEPARATION-M')) is not None:
            site['geoid_separation_m'] = _num(m.get('GEOID-SEPARATION-M'))
        if 'DATUM' in m:
            site['datum'] = _text(m['DATUM'])
        site['source'] = source or 'recorder'
        if _first(m, 'GPS-FIX'):
            site['gps_fix'] = _first(m, 'GPS-FIX')
        if _num(m.get('GPS-SATELLITES')) is not None:
            site['satellites'] = int(_num(m.get('GPS-SATELLITES')))
        if _num(m.get('GPS-HDOP')) is not None:
            site['hdop'] = _num(m.get('GPS-HDOP'))
        if _first(m, 'GPS-POSITION-UTC'):
            site['position_utc'] = _first(m, 'GPS-POSITION-UTC')
    else:
        site = {'source': 'none'}
    out['site'] = site

    if _first(m, 'STATION'):
        out['station'] = _first(m, 'STATION')
    observers = [_first(m, 'OBSERVER', 'AUTHOR'), _first(m, 'OBSERVER-2')]
    more = _first(m, 'OBSERVERS-MORE')
    observers += [s.strip() for s in more.replace(';', ',').split(',')] if more else []
    out['observers'] = [o for o in observers if o]

    eq: dict = {}
    for key, *names in (('telescope', 'TELESCOPE'), ('telescope_type', 'TELESCOPE-TYPE'),
                        ('focal_reducer', 'FOCAL-REDUCER'), ('mount', 'MOUNT'),
                        ('camera', 'CAMERA-MODEL', 'INSTRUMENT'),
                        ('serial', 'INSTRUMENT-SERIAL', 'CAMERA-SERIAL-NO'),
                        ('sensor', 'INSTRUMENT-SENSOR', 'CAMERA-SENSOR-INFO'),
                        ('method', 'OBSERVING-METHOD')):
        t = _first(m, *names)
        if t:
            eq[key] = t
    for key, name in (('aperture_cm', 'APERTURE-CM'), ('focal_length_cm', 'FOCAL-LENGTH-CM')):
        x = _num(m.get(name))
        if x:
            eq[key] = x
    out['equipment'] = eq

    timing: dict = {}
    src, raw = _time_source(m)
    if src:
        timing['source'] = src
    if raw:
        timing['source_text'] = raw
    exp = _num(m.get('EXPOSURE-S'))
    if exp is None and _num(m.get('INSTRUMENT-SHUTTER')):
        exp = _num(m.get('INSTRUMENT-SHUTTER')) / 1e9               # ns
    if exp:
        timing['exposure_s'] = exp
    acc = _num(m.get('FRAME-TIMING-ACCURACY'))
    if acc:
        timing['accuracy_s'] = acc / 1e9                              # ns
    delay = _num(m.get('CAMERA-DELAY-S'))
    if delay is not None:
        timing['camera_delay_s'] = delay
        applied = _first(m, 'CAMERA-DELAY-APPLIED').lower()
        timing['delay_applied'] = applied in ('true', '1', 'yes')
    elif 'ACQUISITION-DELAY' in m:                                      # ADV: unit not documented
        acq = _num(m.get('ACQUISITION-DELAY'))
        if acq == 0:
            timing['camera_delay_s'] = 0.0
        else:
            timing['acquisition_delay_raw'] = _text(m.get('ACQUISITION-DELAY'))
    trig = _num(m.get('TRIGGER-DELAY-NS'))
    if trig is not None:
        timing['trigger_delay_s'] = trig / 1e9
        # PyStarLight's capture stamps a GPS-triggered frame at pulse + trigger delay
        timing['trigger_delay_applied'] = 'trigger pulse' in raw
    out['timing'] = timing
    return out
