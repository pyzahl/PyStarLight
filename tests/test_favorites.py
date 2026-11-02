"""Camera: favorites from a PySyzygos server (a fake one on localhost)."""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pystarlight.capture import favorites as fv
from pystarlight.capture.recorder import file_tag

NOW = datetime(2026, 10, 17, 1, 0, 0, tzinfo=timezone.utc)


def fav(key, utc, asteroid, star, dr3):
    return {"key": key, "status": "planned", "utc": utc,
            "target": {"id": key.split("_")[0], "name": asteroid, "kind": "asteroid"},
            "duration_s": 0.261, "max_duration_s": 0.493, "mag_drop": 10.35, "chance": 0.113,
            "star": {"name": star, "other_names": [f"Gaia DR3 {dr3}"], "gaia_dr3": dr3, "mag_g": 9.333,
                     "ra_icrs": 56.36, "dec_icrs": 14.05, "alt": 41.8, "az": 233.35},
            "site": {"name": "Home"}}


FAVS = [fav("158772_20261017T0436", "2026-10-17T04:36:28.345", "158772 (2003 SB60)", "UCAC4 521-006590",
            "38249436846981120"),
        fav("4337_20261017T0215", "2026-10-17T02:15:00.5", "(4337) Arecibo", "TYC 664-451-1", "123"),
        fav("9_20261018T0300", "2026-10-18T03:00:00", "(9) Metis", "UCAC4 1-1", "9")]   # beyond 12 h


@pytest.fixture
def server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer good":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"version": "1", "favorites": FAVS}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/", seen
    srv.shutdown()


def test_fetch_the_next_12_hours_sorted(server):
    url, seen = server
    got = fv.fetch_favorites(url, "good", now=NOW)
    assert [f["key"] for f in got] == ["4337_20261017T0215", "158772_20261017T0436"]   # by time, not day 2
    path, auth = seen[0]
    assert path.startswith("/api/v1/favorites?from=2026-10-17T01%3A00%3A00&to=2026-10-17T13%3A00%3A00")
    assert auth == "Bearer good"
    with pytest.raises(PermissionError):
        fv.fetch_favorites(url, "bad", now=NOW)
    with pytest.raises(ValueError):
        fv.fetch_favorites("", "good")
    with pytest.raises(OSError):
        fv.fetch_favorites("http://127.0.0.1:9", "good", timeout=2)        # nobody there


def test_recording_plan_and_description():
    p = fv.recording_plan(FAVS[0])
    assert p["asteroid"] == "158772 (2003 SB60)" and p["target"] == "UCAC4 521-006590"
    assert p["verify"] == "Gaia DR3 38249436846981120"
    assert p["start"] == datetime(2026, 10, 17, 4, 35, 48, 345000, tzinfo=timezone.utc)
    assert p["event"] - p["start"] == timedelta(seconds=40) and p["duration_s"] == 80
    d = fv.describe(FAVS[0])
    assert d.startswith("04:36:28 UTC  158772 (2003 SB60)  UCAC4 521-006590  G 9.3  drop 10.3")
    assert "alt 42" in d and d.endswith("Home")
    assert "_".join([file_tag(p["asteroid"]), file_tag(p["target"])]) == "158772-2003-SB60_UCAC4-521-006590"


def test_server_settings_from_args_and_environment(monkeypatch):
    monkeypatch.setenv("PYSYZYGOS_URL", "http://env:8080")
    monkeypatch.setenv("PYSYZYGOS_TOKEN", "envtok")
    assert fv.server_settings() == ("http://env:8080", "envtok")
    assert fv.server_settings("http://arg:1", "argtok") == ("http://arg:1", "argtok")


def test_config_read_without_the_package(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[pysyzygos_server]\nurl = "http://home:8080"\ntoken = "t"\n')
    monkeypatch.setenv("PYSTARLIGHT_CONFIG", str(p))
    assert fv._read_config()["pysyzygos_server"]["url"] == "http://home:8080"
    monkeypatch.setenv("PYSTARLIGHT_CONFIG", "none")
    assert fv._read_config() == {}


def test_past_hours_widen_the_window_back(server, tmp_path, monkeypatch):
    url, seen = server
    later = NOW + timedelta(hours=2, minutes=30)            # 03:30: Arecibo (02:15) is over
    assert [f["key"] for f in fv.fetch_favorites(url, "good", now=later)] == ["158772_20261017T0436"]
    got = fv.fetch_favorites(url, "good", now=later, past=4)
    assert [f["key"] for f in got] == ["4337_20261017T0215", "158772_20261017T0436"]
    assert "from=2026-10-16T23%3A30%3A00" in seen[-1][0]
    p = tmp_path / "c.toml"
    p.write_text('[pysyzygos_server]\npast_hours = 4\n')
    monkeypatch.setenv("PYSTARLIGHT_CONFIG", str(p))
    assert fv.past_hours() == 4.0
    monkeypatch.setenv("PYSTARLIGHT_CONFIG", "none")
    assert fv.past_hours() == 0.0
