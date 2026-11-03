"""Station data: capture header (config + GPS), RAVF / SER files, recording.station, push to PySyzygos."""

import json
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from pystarlight import movie, report_push
from pystarlight.capture import ravf_writer, ser_writer, station_header
from pystarlight.station import movie_format, station_of

T0 = 1_792_000_000 * 10**9
W, H = 16, 12

OBSERVER = {"name": "A. Observer", "email": "a@example.org", "observer2": "B. Second",
            "more_observers": "C. Third, D. Fourth", "station": "Test Station (TS)", "telescope": "C-14 EHD",
            "telescope_type": "SCT", "aperture_cm": 35.6, "focal_length_cm": 62.2, "focal_reducer": "0.4x",
            "mount": "GM2000", "observing_method": "a", "camera_delay_s": 0.0, "camera_delay_applied": False,
            "latitude": 46.5, "longitude": 9.5, "altitude": 900.0}

# the metadata of a real DVTI CamControl ADV recording (as in its event.json)
ADV_META = {
    "ACQUISITION-DELAY": "0.0", "ALTITUDE": "972.8", "AUTHOR": "Carsten Ziolek", "BINNING-X": "2",
    "BINNING-Y": "2", "CAMERA-MODEL": "DVTI+CAM",
    "CAMERA-SENSOR-INFO": "Sony IMX430LLJ-C (9.2mm 1624x1240 4.5um CIS)",
    "CAMERA-SERIAL-NO": "43", "CAMERA-VENDOR-NAME": "DVTI",
    "INSTRUMENT": "DVTI+CAM #43 FW 1.3.2, Sensor IMX430",
    "LATITUDE": "46.9924869", "LONGITUDE": "9.6401519", "OBJNAME": "(709) Fringilla occ. UCAC4 644-038283",
    "OBSERVER": "Carsten Ziolek", "RECORDER-SOFTWARE": "DvtiCamControl",
    "RECORDER-SOFTWARE-VERSION": "5.9.0-61",
    "TELESCOPE": "C-14 EHD + 0.4x Red."}


class FakeGps:
    def __init__(self, fix=True, kind=3):
        self._fix = fix
        self.fix, self.sats_used, self.hdop = kind, 11, 0.8
        self.lat, self.lon, self.alt, self.geoid_sep = 46.99248691, 9.64015187, 972.84, 48.2
        self.utc = datetime(2026, 10, 17, 4, 36, 0, 500000, tzinfo=timezone.utc)

    def has_fix(self):
        return self._fix


# ---------------------------------------------------------------------------
# capture: config + GPS -> header metadata
# ---------------------------------------------------------------------------
def test_site_from_gps_only_with_a_fix():
    s = station_header.site_meta(FakeGps())
    assert s["site_source"] == "GPS" and s["gps_fix"] == "3D" and s["gps_satellites"] == 11
    assert s["latitude_text"] == "46.9924869" and s["longitude_text"] == "9.6401519"
    assert s["altitude"] == 972.84 and s["altitude_text"] == "972.8" and s["altitude_ref"] == "MSL"
    assert s["geoid_separation_m"] == 48.2 and s["datum"] == ""
    assert s["gps_position_utc"] == "2026-10-17T04:36:00.50Z"
    assert "altitude" not in station_header.site_meta(FakeGps(kind=2))      # 2D: no height
    # no fix: no position at all, or the config's site marked as such
    assert station_header.site_meta(FakeGps(fix=False)) == {"site_source": "none"}
    assert station_header.site_meta(None, {"latitude": 0.0, "longitude": 0.0}) == {"site_source": "none"}
    s = station_header.site_meta(FakeGps(fix=False), OBSERVER)
    assert s["site_source"] == "config" and s["latitude"] == 46.5 and "gps_fix" not in s


def test_station_meta_from_config():
    m = station_header.station_meta(OBSERVER)
    assert m["observer"] == "A. Observer" and m["observer2"] == "B. Second"
    assert m["observers_more"] == "C. Third, D. Fourth" and m["station"] == "Test Station (TS)"
    assert m["aperture_cm"] == 35.6 and m["focal_length_cm"] == 62.2 and m["focal_reducer"] == "0.4x"
    assert "camera_delay_s" not in m and "camera_delay_applied" not in m      # 0: not known
    assert "email" not in m
    m = station_header.station_meta({**OBSERVER, "camera_delay_s": 0.012, "camera_delay_applied": True})
    assert m["camera_delay_s"] == 0.012 and m["camera_delay_applied"] is True
    assert station_header.station_meta({}) == {}


def test_gga_geoid_separation_parsed():
    pytest.importorskip("serial")                         # pyserial (not in PyMovie's env)
    from pystarlight.capture import gps_ublox
    gps = gps_ublox.Gps.__new__(gps_ublox.Gps)            # no serial port
    gps.lock = threading.Lock()
    gps._on_nmea("GPGGA,043600.50,4659.5492,N,00938.4091,E,1,11,0.80,972.8,M,48.2,M,,".split(","))
    assert gps.alt == 972.8 and gps.geoid_sep == 48.2 and gps.sats_used == 11 and gps.hdop == 0.8
    gps._on_nmea("GPGGA,043601.00,,,,,0,00,,,M,,M,,".split(","))
    assert gps.alt is None and gps.geoid_sep is None


def header(fix=True):
    meta = {"observer": "A. Observer", "instrument": "MV-MIPI-IMX178M", "telescope": "C-14 EHD",
            "timestamp_source": "GPS trigger pulse, GPS UTC", "timing_accuracy_ns": 100_000,
            "trigger_delay_ns": 25_000, "exposure_ns": 75_000_000, "objname": "(709) Fringilla"}
    meta.update(station_header.station_meta(OBSERVER))
    meta.update(station_header.site_meta(FakeGps(fix=fix)))
    return meta


def write(cls, path, meta):
    w = cls(str(path), W, H, 12, meta)
    rng = np.random.default_rng(0)
    for i in range(3):
        w.write(rng.integers(0, 4096, (H, W), dtype=np.uint16), T0 + i * 10**8,
                {"frame_id": i, "exposure_ns": 75_000_000, "satellites": 11, "fix": 3, "almanac": 0})
    w.close()


def test_ravf_station_entries_and_text_copies(tmp_path):
    pytest.importorskip("ravf")
    path = tmp_path / "a.ravf"
    write(ravf_writer.RavfWriter, path, header())
    md = movie.open_movie(path).metadata
    assert md["STATION"] == "Test Station (TS)" and md["OBSERVER-2"] == "B. Second"
    assert md["TELESCOPE-TYPE"] == "SCT" and md["APERTURE-CM"] == 35.6 and md["FOCAL-REDUCER"] == "0.4x"
    assert md["LATITUDE-TEXT"] == "46.9924869" and md["ALTITUDE-TEXT"] == "972.8"
    assert abs(md["LATITUDE"] - 46.9924869) < 1e-5                       # float32
    assert md["SITE-SOURCE"] == "GPS" and md["GPS-FIX"] == "3D" and md["GPS-SATELLITES"] == 11
    assert md["GEOID-SEPARATION-M"] == 48.2 and md["ALTITUDE-REF"] == "MSL" and md["DATUM"] == ""


def test_ravf_without_fix_has_no_position(tmp_path):
    pytest.importorskip("ravf")
    path = tmp_path / "b.ravf"
    write(ravf_writer.RavfWriter, path, header(fix=False))
    md = movie.open_movie(path).metadata
    assert math.isnan(md["LATITUDE"]) and math.isnan(md["LONGITUDE"])
    assert md["SITE-SOURCE"] == "none" and "LATITUDE-TEXT" not in md and "GPS-FIX" not in md
    st = station_of(md, "RAVF")
    assert st["site"] == {"source": "none"}


def test_ser_sidecar_carries_the_header(tmp_path):
    path = tmp_path / "c.ser"
    write(ser_writer.SerWriter, path, header())
    side = json.loads((tmp_path / "c.ser.meta.json").read_text())
    assert side["STATION"] == "Test Station (TS)" and side["LATITUDE-TEXT"] == "46.9924869"
    assert side["RECORDING-START-UTC"].endswith("Z")
    md = movie.open_movie(path).metadata
    assert md["Observer"] == "A. Observer"                                 # SER header itself
    assert md["STATION"] == "Test Station (TS)" and md["TIMESTAMP-SOURCE"] == "GPS trigger pulse, GPS UTC"
    st = station_of(md, movie_format(path))
    assert st["format"] == "SER" and st["site"]["source"] == "GPS" and st["site"]["lat"] == 46.9924869


# ---------------------------------------------------------------------------
# recording.station
# ---------------------------------------------------------------------------
def test_station_of_ravf_metadata(tmp_path):
    pytest.importorskip("ravf")
    path = tmp_path / "d.ravf"
    write(ravf_writer.RavfWriter, path, header())
    st = station_of(movie.open_movie(path).metadata, movie_format(path))
    assert st["format"] == "RAVF" and st["recorder"].startswith("PyStarLight capture")
    assert st["site"] == {"lat": 46.9924869, "lon": 9.6401519, "alt_m": 972.8, "alt_ref": "MSL",
                          "geoid_separation_m": 48.2, "datum": "", "source": "GPS", "gps_fix": "3D",
                          "satellites": 11, "hdop": 0.8, "position_utc": "2026-10-17T04:36:00.50Z"}
    assert st["station"] == "Test Station (TS)"
    assert st["observers"] == ["A. Observer", "B. Second", "C. Third", "D. Fourth"]
    eq = st["equipment"]
    assert eq["camera"] == "MV-MIPI-IMX178M" and eq["telescope_type"] == "SCT" and eq["mount"] == "GM2000"
    assert eq["aperture_cm"] == 35.6 and eq["method"] == "a"
    tm = st["timing"]
    assert tm["source"] == "GPS" and tm["exposure_s"] == 0.075 and tm["accuracy_s"] == 1e-4
    assert tm["trigger_delay_s"] == 25e-6 and tm["trigger_delay_applied"] is True
    assert "camera_delay_s" not in tm


def test_station_of_adv_metadata():
    st = station_of(ADV_META, "ADV")
    assert st["recorder"] == "DvtiCamControl 5.9.0-61"
    assert st["site"] == {"lat": 46.9924869, "lon": 9.6401519, "alt_m": 972.8, "source": "recorder"}
    assert st["observers"] == ["Carsten Ziolek"]
    assert st["equipment"] == {"telescope": "C-14 EHD + 0.4x Red.", "camera": "DVTI+CAM", "serial": "43",
                               "sensor": "Sony IMX430LLJ-C (9.2mm 1624x1240 4.5um CIS)"}
    assert st["timing"] == {"camera_delay_s": 0.0}
    assert "acquisition_delay_raw" in station_of({**ADV_META, "ACQUISITION-DELAY": "16"}, "ADV")["timing"]
    ser = station_of({"Observer": "X", "FileID": "LUCAM-RECORDER"}, "SER")
    assert ser["recorder"] == "SER (LUCAM-RECORDER)" and ser["observers"] == ["X"]


# ---------------------------------------------------------------------------
# push to PySyzygos' observation GUI (a fake one on localhost)
# ---------------------------------------------------------------------------
@pytest.fixture
def obs_server():
    got = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._answer(200, {"ok": True, "version": "1.5.2", "inbox": len(got)})

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer good":
                return self._answer(401, {"ok": False, "msg": "token"})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "step_fit" not in body:
                return self._answer(400, {"ok": False, "msg": "not an analysis"})
            got.append(body)
            self._answer(200, {"ok": True, "file": "x.event.json", "asteroid": "709 Fringilla",
                               "date": "2025-01-16", "favorites": ["709_20250116T1755"],
                               "msg": "report of 709 Fringilla opened"})

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", got
    srv.shutdown()


def test_push_200_400_401_and_queue(tmp_path, obs_server, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    url, got = obs_server
    ev = tmp_path / "m.event.json"
    ev.write_text(json.dumps({"software": "PyStarLight", "step_fit": {"events": []}}))
    assert report_push.status(url)["version"] == "1.5.2"
    with pytest.raises(report_push.SendError, match="401"):
        report_push.send(ev, url, "bad")
    msg = report_push.push(ev, url, "good")
    assert msg.startswith("sent m.event.json") and "709 Fringilla" in msg and len(got) == 1
    bad = tmp_path / "n.event.json"
    bad.write_text(json.dumps({"software": "PyStarLight"}))
    assert "400" in report_push.push(bad, url, "good") and len(got) == 1
    # no server: queued, then sent with the next one
    assert "queued" in report_push.push(ev, "http://127.0.0.1:9", "good", timeout=2)
    assert (report_push.outbox() / "m.event.json").exists()
    msg = report_push.push(ev, url, "good")
    assert "(+1 queued)" in msg and len(got) == 3 and not list(report_push.outbox().glob("*.json"))
    assert "good" not in msg


@pytest.fixture
def no_env(monkeypatch, tmp_path):
    for k in ("PYSYZYGOS_OBS_URL", "PYSYZYGOS_OBS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(report_push.sys, "platform", "linux")
    monkeypatch.setattr(report_push.os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


def connection_file(tmp, url, token="pyobs_good"):
    """A stand-in for the file PySyzygos' observation GUI writes at its start."""
    f = report_push.connection_file()
    assert f == tmp / "config" / "pysyzygos" / "observation_api.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"app": "PySyzygos observation GUI", "version": "1.5.2", "url": url,
                             "send": url + "/api/obs/v1/analysis",
                             "inbox": str(tmp / "observations" / "inbox"),
                             "data_folder": str(tmp), "token": token,
                             "header": "Authorization: Bearer <token>", "written": "2026-11-02T10:00:00Z"}))
    return f


def test_connection_from_file_and_config(no_env):
    c = report_push.connection({})
    assert c["source"] == "none" and "never run" in report_push.no_connection_message(c)
    assert "not sent" in report_push.push_with(no_env / "x.event.json", c)
    connection_file(no_env, "http://127.0.0.1:8088")
    c = report_push.connection({"pysyzygos_observations": {"auto_send": True}})
    assert c["source"] == "file" and c["token"] == "pyobs_good" and c["auto_send"]
    assert c["send"] == "http://127.0.0.1:8088/api/obs/v1/analysis" and c["inbox"].endswith("inbox")
    c = report_push.connection({"pysyzygos_observations": {"url": "http://home:8088", "token": "t"}})
    assert (c["source"], c["send"], c["token"]) == ("config", "http://home:8088/api/obs/v1/analysis", "t")
    assert c["inbox"] == ""                                   # another computer: not its inbox


def test_push_through_the_connection_file(no_env, obs_server):
    url, got = obs_server
    ev = no_env / "m.event.json"
    ev.write_text(json.dumps({"created_utc": "2026-11-02T01:02:03Z", "step_fit": {"events": []}}))
    connection_file(no_env, url, token="good")
    msg = report_push.push_with(ev, report_push.connection({}))
    assert msg.startswith("sent m.event.json") and len(got) == 1 and "good" not in msg


def test_inbox_copy_when_nothing_listens(no_env):
    ev = no_env / "m.event.json"
    ev.write_text(json.dumps({"created_utc": "2026-11-02T01:02:03Z", "step_fit": {"events": []}}))
    connection_file(no_env, "http://127.0.0.1:9")            # the observation GUI is not running
    msg = report_push.push_with(ev, report_push.connection({}), timeout=2)
    assert "copied to the inbox" in msg
    box = no_env / "observations" / "inbox"
    assert [p.name for p in box.iterdir()] == ["20261102010203_m.event.json"]       # no .tmp left
    assert not report_push.outbox().exists()


# ---------------------------------------------------------------------------
# the camera's station dialog: required fields, writing [observer] back
# ---------------------------------------------------------------------------
COMPLETE = {k: v for k, v in OBSERVER.items()}


def test_required_station_fields():
    assert station_header.missing(COMPLETE) == []
    miss = station_header.missing({**COMPLETE, "station": " ", "aperture_cm": 0.0, "mount": ""})
    assert miss == ["Station", "Aperture (cm)", "Mount"]
    assert station_header.missing({**COMPLETE, "latitude": 0, "longitude": 0}) == []        # GPS
    assert station_header.missing({**COMPLETE, "latitude": 0, "longitude": 0}, need_site=True) == \
        ["site (no GPS: fallback latitude / longitude)"]
    assert "Observer" in station_header.missing({})
    eff = station_header.effective({**COMPLETE, "name": ""}, args_observer="CLI")
    assert eff["name"] == "CLI" and station_header.missing(eff) == []
    assert station_header.summary(COMPLETE) == \
        "Test Station (TS) - A. Observer - C-14 EHD - 35.6 cm / 62.2 cm"


def test_update_section_keeps_comments_and_other_tables(tmp_path):
    tomllib = pytest.importorskip("tomllib")              # Python 3.11+ (the Pi's)
    from pystarlight.capture import localconfig
    p = tmp_path / "pystarlight.toml"
    p.write_text('# top\n[observer]\nname = "Old"   # the observer\n'
                 'focal_length_cm = 34.3     # with 0.5x\n\n'
                 '# the next table\n[astrometry]\napi_key = "SECRET"\n')
    p.chmod(0o600)
    localconfig.update_section("observer", {"name": 'New "N"', "focal_length_cm": 40.0, "station": "S",
                                            "camera_delay_applied": True}, str(p))
    text = p.read_text()
    assert 'name = "New \\"N\\""   # the observer' in text and "focal_length_cm = 40.0   # with 0.5x" in text
    assert text.index('station = "S"') < text.index("# the next table")
    assert 'api_key = "SECRET"' in text and text.startswith("# top\n")
    d = tomllib.loads(text)
    assert d["observer"]["camera_delay_applied"] is True and d["astrometry"]["api_key"] == "SECRET"
    assert p.stat().st_mode & 0o777 == 0o600
    new = tmp_path / "sub" / "config.toml"                                   # no file yet
    localconfig.update_section("observer", {"name": "X"}, str(new))
    assert tomllib.loads(new.read_text()) == {"observer": {"name": "X"}}
    assert new.stat().st_mode & 0o777 == 0o600


def test_station_dialog(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("cv2")
    try:
        from pystarlight.capture import gui
        from pystarlight.capture.qt import QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = gui.StationDialog(None, {**COMPLETE, "mount": "", "observing_method": "a"}, "/x/pystarlight.toml")
    assert "Mount" in dlg.missing_label.text()
    dlg.widgets["mount"][0].setText("GM2000")
    v = dlg.values()
    assert dlg.missing_label.text() == "complete" and v["mount"] == "GM2000"
    assert v["observing_method"] == "a" and v["telescope_type"] == "SCT" and v["aperture_cm"] == 35.6
    assert v["camera_delay_applied"] is False and abs(v["latitude"] - 46.5) < 1e-9
    assert app is not None
