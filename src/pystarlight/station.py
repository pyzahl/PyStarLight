"""Station data of a recording in one layout for every video format: `recording.station` of
`<movie>.event.json` (PySyzygos' docs/observation_interface.md, section 2).

NEW CODE. RAVF (PyStarLight's capture, ASTRID), ADV/AAV (DVTI CamControl, OccuRec) and SER
(header: observer, instrument, telescope; PyStarLight's capture adds a `.ser.meta.json`
sidecar with RAVF names) name the same things differently. `station_of(metadata, fmt)` reads
them into

    {"format", "recorder", "site": {lat, lon, alt_m, alt_ref, geoid_separation_m, datum, source,
     gps_fix, satellites, hdop, position_utc}, "station", "observers": [...],
     "equipment": {telescope, telescope_type, aperture_mm, focal_length_mm, focal_reducer, mount,
     camera, serial, sensor, method}, "timing": {source, source_text, exposure_s, accuracy_s,
     camera_delay_s, delay_applied, trigger_delay_s, trigger_delay_applied,
     timestamp_reference, timestamp_reference_source}}

timing.timestamp_reference: which instant of a frame's exposure its time stamp marks, "start",
"middle", "end" or "unknown" (time_reference): the recording's TIMESTAMP-REFERENCE (PyStarLight's
capture), else what the format defines (RAVF: start, as ASTRID's start_timestamp; ADV / AAV:
middle, Adv2's mid-exposure time; FITS: DATE-OBS start, MIDTIME middle); SER and AVI: unknown.
mid_exposure_shift_s() turns it into the shift that puts the time stamps on mid-exposure.

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

from .capture.station_header import check, implausible

FORMATS = {'.ravf': 'RAVF', '.adv': 'ADV', '.aav': 'AAV', '.ser': 'SER', '.avi': 'AVI', '.mov': 'MOV',
           '.mp4': 'MP4'}


def movie_format(path) -> str:
    """'RAVF', 'ADV', 'SER', ... from the file name; 'FITS' for a folder."""
    p = str(path)
    if os.path.isdir(p):
        return 'FITS'
    return FORMATS.get(os.path.splitext(p)[1].lower(), os.path.splitext(p)[1].lstrip('.').upper())


REFERENCES = ('start', 'middle', 'end', 'unknown')


def time_reference(metadata: dict, fmt: str = '') -> tuple[str, str]:
    """(reference, where it comes from): which instant of the exposure a frame's time stamp
    marks, 'start' / 'middle' / 'end' / 'unknown', see the module docstring."""
    m = _upper(metadata)
    ref = _text(m.get('TIMESTAMP-REFERENCE')).strip().lower()
    ref = {'mid': 'middle', 'mid-exposure': 'middle', 'center': 'middle', 'centre': 'middle'}.get(ref, ref)
    if ref in REFERENCES:
        return ref, 'recording (TIMESTAMP-REFERENCE)'
    fmt = (fmt or '').upper()
    if fmt == 'RAVF':
        return 'start', 'RAVF format (start_timestamp)'
    if fmt in ('ADV', 'AAV'):
        return 'middle', 'ADV format (mid-exposure time stamps)'
    if fmt == 'FITS':
        if m.get('DATE-OBS'):
            return 'start', 'FITS DATE-OBS'
        if m.get('MIDTIME'):
            return 'middle', 'FITS MIDTIME'
    return 'unknown', 'not in the recording'


def mid_exposure_shift_s(timing: dict, frame_interval_s=None) -> tuple[float, str]:
    """(seconds to add to the time stamps to get mid-exposure, how): +exposure / 2 for 'start',
    -exposure / 2 for 'end', 0 for 'middle' and 'unknown'. The exposure is the recording's,
    else the frame interval (the step fit's assumption)."""
    ref = timing.get('timestamp_reference', 'unknown')
    if ref not in ('start', 'end'):
        return 0.0, ('time stamps are mid-exposure' if ref == 'middle'
                     else 'time stamp position in the exposure unknown: taken as mid-exposure')
    exp, what = timing.get('exposure_s'), 'exposure'
    if not exp:
        exp, what = frame_interval_s, 'frame interval (exposure not recorded)'
    if not exp or not math.isfinite(exp):
        return 0.0, f'time stamps mark the exposure {ref}, but its length is unknown: not shifted'
    sign = 1.0 if ref == 'start' else -1.0
    return sign * exp / 2, f'time stamps mark the exposure {ref}: {sign * exp / 2:+.4f} s (half the {what})'


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
    for key, name in (('aperture_mm', 'APERTURE'), ('focal_length_mm', 'FOCAL-LENGTH')):
        x = _num(m.get(name + '-MM'))
        if not x and _num(m.get(name + '-CM')):          # recordings of earlier versions: cm
            x = round(_num(m.get(name + '-CM')) * 10, 3)
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
    timing['timestamp_reference'], timing['timestamp_reference_source'] = time_reference(metadata, fmt)
    out['timing'] = timing
    warn = implausible(eq)
    # essentials a report needs; missing in an odd or incomplete recording (another program's,
    # an older capture, a SER without its sidecar): PySyzygos / the report must fill them in
    for what, ok in (('observer', out['observers']), ('telescope', eq.get('telescope')),
                     ('aperture', eq.get('aperture_mm')), ('focal length', eq.get('focal_length_mm')),
                     ('position', site.get('source') not in ('none', None)),
                     ('time source', timing.get('source'))):
        if not ok:
            warn.append(f'no {what} in the recording')
    if warn:                            # e.g. mm typed into a cm field: as recorded, but flagged
        out['warnings'] = warn
    return out
