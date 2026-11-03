"""Favorites from a PySyzygos server: the events to record, read over its favorites API.

PySyzygos (pyzahl/PySyzygos, SERVER_MODE.md section 6) serves its favorites read-only as JSON:

    GET <server>/api/v1/favorites?from=<UTC>&to=<UTC>     {"version", "favorites": [event, ...]}
    header  Authorization: Bearer <token>                 (pysyzygos gui --new-token PyStarLight;
                                                           not needed on 127.0.0.1 in local mode)

event: {"key", "status", "target": {"name", ...}, "utc", "duration_s", "max_duration_s",
"mag_drop", "chance", "star": {"name", "other_names", "gaia_dr3", "mag_g", "ra_icrs", "dec_icrs",
"alt", "az", ...}, "site": {"name", ...}, ...}

Server and token: --pysyzygos URL, $PYSYZYGOS_URL / $PYSYZYGOS_TOKEN, else the private config's
[pysyzygos_server] url / token. Standard library
only, so the Pi's system Python runs it.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .localconfig import read as _read_config  # noqa: F401  (tests)
from .localconfig import section

LEAD_S = 40.0           # recording starts this long before the event ...
TRAIL_S = 40.0          # ... and ends this long after it


def server_settings(url=None, token=None):
    """(url, token): the arguments, else $PYSYZYGOS_URL / $PYSYZYGOS_TOKEN, else the private
    config's [pysyzygos_server]; '' where none."""
    cfg = _server_config() if not (url and token) else {}
    url = url or os.environ.get("PYSYZYGOS_URL") or cfg.get("url") or ""
    token = token or os.environ.get("PYSYZYGOS_TOKEN") or cfg.get("token") or ""
    return url.strip(), token.strip()


def _server_config():
    """The config's [pysyzygos_server] (package config, else the file read directly)."""
    return section("pysyzygos_server")


def past_hours():
    """[pysyzygos_server] past_hours: also list events that far back (tests; default 0)."""
    try:
        return max(0.0, float(_server_config().get("past_hours") or 0))
    except (TypeError, ValueError):
        return 0.0


def parse_utc(text):
    """'2026-10-17T04:36:28.345' (UTC, 'Z' optional) -> aware datetime."""
    t = datetime.fromisoformat(str(text).strip().rstrip("Z"))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def fetch_favorites(url, token="", hours=12.0, now=None, timeout=10.0, past=0.0):
    """The favorites with their event from now - past to now + hours (hours), by event time.
    Raises OSError (no server, timeout), PermissionError (token refused) or ValueError."""
    if not url:
        raise ValueError("no PySyzygos server: set [pysyzygos_server] url in the config "
                         "or --pysyzygos URL")
    now = now or datetime.now(timezone.utc)
    first = now - timedelta(hours=past)
    q = {"from": first.strftime("%Y-%m-%dT%H:%M:%S"),
         "to": (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")}
    req = urllib.request.Request(f"{url.rstrip('/')}/api/v1/favorites?{urllib.parse.urlencode(q)}",
                                 headers={"Accept": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise PermissionError(f"PySyzygos refused the token ({e.code}): "
                                  "pysyzygos gui --new-token PyStarLight") from None
        raise ValueError(f"PySyzygos answered {e.code}: {e.reason}") from None
    favs = data.get("favorites", []) if isinstance(data, dict) else data
    out = []
    for f in favs or []:
        try:
            t = parse_utc(f["utc"])
        except (KeyError, TypeError, ValueError):
            continue
        if first <= t <= now + timedelta(hours=hours):
            out.append(f)
    return sorted(out, key=lambda f: parse_utc(f["utc"]))


def describe(f):
    """One line for a choice list: '04:36:28 UTC  158772 (2003 SB60)  UCAC4 521-006590 G 9.3
    drop 10.4  0.26 s  alt 42  Home'."""
    star, target, site = f.get("star") or {}, f.get("target") or {}, f.get("site") or {}
    parts = [parse_utc(f["utc"]).strftime("%H:%M:%S UTC"), target.get("name") or "?",
             (star.get("name") or "") + (f"  G {star['mag_g']:.1f}" if star.get("mag_g") is not None else "")]
    if f.get("mag_drop") is not None:
        parts.append(f"drop {f['mag_drop']:.1f}")
    if f.get("duration_s") is not None:
        parts.append(f"{f['duration_s']:.2f} s")
    if star.get("alt") is not None:
        parts.append(f"alt {star['alt']:.0f}")
    if f.get("chance") is not None:
        parts.append(f"{100 * f['chance']:.0f} %")
    if site.get("name"):
        parts.append(site["name"])
    return "  ".join(p for p in parts if p)


def recording_plan(f, lead_s=LEAD_S, trail_s=TRAIL_S):
    """What to set for the event: {'target': star name, 'verify': its Gaia DR3 name,
    'asteroid': the occulting body, 'start': UTC datetime, 'duration_s', 'event': UTC datetime}."""
    star, t = f.get("star") or {}, parse_utc(f["utc"])
    dr3 = star.get("gaia_dr3")
    return {"target": star.get("name") or (f"Gaia DR3 {dr3}" if dr3 else ""),
            "verify": f"Gaia DR3 {dr3}" if dr3 and star.get("name") != f"Gaia DR3 {dr3}" else "",
            "asteroid": (f.get("target") or {}).get("name") or "",
            "start": t - timedelta(seconds=lead_s), "duration_s": lead_s + trail_s, "event": t}
