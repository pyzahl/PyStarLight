"""INDI client (capture/indi.py) and mount (capture/mount.py): the XML stream against a fake
server, precession and sidereal time against astropy, goto / sync / tracking against INDI's
telescope simulator (skipped without indiserver)."""

import math
import shutil
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

from pystarlight.capture import indi
from pystarlight.capture import mount as mt


def test_numbers():
    assert indi.number("5.5") == 5.5
    assert indi.number("12:30:00") == 12.5
    assert indi.number("-0 30 0") == -0.5
    assert math.isnan(indi.number(""))


def fake_server(chunks):
    """A one-client server that sends `chunks` (pieces of an INDI stream) and records what it gets."""
    srv = socket.socket()
    srv.bind(("localhost", 0))
    srv.listen(1)
    got = []

    def run():
        conn, _ = srv.accept()
        got.append(conn.recv(4096).decode())
        for c in chunks:
            conn.sendall(c.encode())
            time.sleep(0.01)
        time.sleep(0.3)
        conn.settimeout(0.5)
        try:
            got.append(conn.recv(4096).decode())
        except OSError:
            pass
        conn.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], got


def test_client_parses_the_stream():
    stream = ('<defTextVector device="Scope" name="DRIVER_INFO" perm="ro" state="Idle">'
              '<defText name="DRIVER_INTERFACE">5</defText></defTextVector>'
              '<defNumberVector device="Scope" name="EQUATORIAL_EOD_COORD" state="Idle" perm="rw">'
              '<defNumber name="RA" label="RA">\n 5.5\n</defNumber>'
              '<defNumber name="DEC">-10:30:00</defNumber></defNumberVector>'
              '<defSwitchVector device="Scope" name="TELESCOPE_TRACK_STATE" rule="OneOfMany" state="Ok" '
              'perm="rw"><defSwitch name="TRACK_ON">On</defSwitch>'
              '<defSwitch name="TRACK_OFF">Off</defSwitch></defSwitchVector>')
    split = len(stream) // 2                        # an element cut in two by the network
    port, got = fake_server([stream[:split], stream[split:],
                             '<setNumberVector device="Scope" name="EQUATORIAL_EOD_COORD" state="Busy" '
                             'message="slewing"><oneNumber name="RA">6.0</oneNumber></setNumberVector>'])
    c = indi.Client(port=port).connect()
    assert c.wait(lambda: c.state("Scope", "EQUATORIAL_EOD_COORD") == "Busy", 3)
    assert c.find_device(indi.TELESCOPE, 1) == "Scope" and c.find_device(indi.FOCUSER, 0.1) is None
    assert c.number("Scope", "EQUATORIAL_EOD_COORD") == {"RA": 6.0, "DEC": -10.5}
    assert c.switch("Scope", "TELESCOPE_TRACK_STATE") == {"TRACK_ON": True, "TRACK_OFF": False}
    assert c.messages[-1] == ("Scope", "slewing")
    c.set_switch("Scope", "TELESCOPE_TRACK_STATE", {"TRACK_OFF": True})
    time.sleep(0.4)
    c.close()
    assert got[0].startswith("<getProperties")
    assert ('<newSwitchVector device="Scope" name="TELESCOPE_TRACK_STATE"><oneSwitch name="TRACK_OFF">On'
            '</oneSwitch></newSwitchVector>') in got[1]


def test_precession_and_sidereal_time_against_astropy():
    pytest.importorskip("astropy")
    import astropy.units as u
    from astropy.coordinates import FK5, EarthLocation, SkyCoord
    from astropy.time import Time
    when = datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc)
    jd = mt.julian_day(when)
    assert jd == pytest.approx(Time(when).jd, abs=1e-8)
    for ra, dec in ((310.357, 45.28), (83.82, -5.39), (10.0, 85.0), (200.0, -60.0)):
        a = SkyCoord(ra * u.deg, dec * u.deg, frame=FK5(equinox="J2000"))
        a = a.transform_to(FK5(equinox=Time(when)))
        r, d = mt.j2000_to_jnow(ra, dec, jd)
        assert abs((r - a.ra.deg + 180) % 360 - 180) * math.cos(math.radians(dec)) * 3600 < 0.5
        assert abs(d - a.dec.deg) * 3600 < 0.5
        back = mt.jnow_to_j2000(r, d, jd)
        assert back[0] == pytest.approx(ra, abs=1e-9) and back[1] == pytest.approx(dec, abs=1e-9)
    lst = Time(when, location=EarthLocation(lon=-72.88 * u.deg, lat=40.87 * u.deg)).sidereal_time("mean").deg
    assert abs((mt.sidereal_deg(jd, -72.88) - lst + 180) % 360 - 180) * 3600 < 1.0


def test_drivers_installed_lists_groups(tmp_path, monkeypatch):
    (tmp_path / "drivers.xml").write_text(
        '<driversList><devGroup group="Telescopes"><device label="ZWO AM5"><driver name="ZWO AM5">'
        'indi_lx200am5</driver></device><device label="Gone"><driver>indi_not_here</driver></device>'
        '</devGroup><devGroup group="Focusers"><device label="ASI EAF"><driver>indi_asi_focuser</driver>'
        '</device></devGroup></driversList>')
    (tmp_path / "x_sk.xml").write_text("<not a list")                  # skeletons are skipped
    monkeypatch.setattr(indi.shutil, "which",
                        lambda exe: None if exe == "indi_not_here" else "/usr/bin/" + exe)
    assert indi.drivers_installed(str(tmp_path)) == {"Telescopes": [("ZWO AM5", "indi_lx200am5")],
                                                    "Focusers": [("ASI EAF", "indi_asi_focuser")]}


@pytest.fixture
def simulator():
    if not (shutil.which("indiserver") and shutil.which("indi_simulator_telescope")):
        pytest.skip("no indiserver / telescope simulator")
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    srv = indi.Server(["indi_simulator_telescope"], port=port).start()
    client = indi.Client(port=port).connect()
    yield client
    client.close()
    srv.stop()


def test_mount_on_the_simulator(simulator):
    m = mt.Mount(simulator).connect()
    m.set_site(40.87, -72.88, 20.0)
    m.set_time()
    m.park(False)
    time.sleep(0.5)
    m.goto(310.357, 45.28)
    assert m.wait_slew(120)
    st = m.status()
    assert st["connected"] and st["tracking"] and not st["slewing"]
    assert abs(st["dec"] - 45.28) < 0.1
    m.sync(310.4, 45.3)
    time.sleep(0.5)
    ra, dec = m.position()
    assert ra == pytest.approx(310.4, abs=0.01) and dec == pytest.approx(45.3, abs=0.01)
    m.goto(300.0, 40.0, track=False)                   # pre-point: tracking off once there
    assert m.wait_slew(120)
    assert simulator.wait(lambda: not m.tracking(), 10)


def test_mount_dock_goto_sync_center(monkeypatch, tmp_path):
    """The camera window's Mount dock on the simulator: connect (our own indiserver), goto,
    Solve & sync and the Center loop, with the stack and plate solve faked."""
    if not (shutil.which("indiserver") and shutil.which("indi_simulator_telescope")):
        pytest.skip("no indiserver / telescope simulator")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
        from pystarlight.capture.telescope_gui import MountDock
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from types import SimpleNamespace
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]

    solves = []                                  # the centres the fake plate solves return

    class Main(QtWidgets.QMainWindow):
        pass
    main = Main()
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):   # not the user's settings
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(gps=None, stacker=SimpleNamespace(state="idle", path=None, error=""))
    main.target_radec, main.solution, main.solve_count = None, None, 0
    main.solve_thread = main.solved_path = None
    main.solve_log = ""

    def on_stack_button():
        main.c.stacker.state, main.c.stacker.path = "done", f"stack{main.solve_count}.fits"

    def start_solve(path):
        ra, dec = solves.pop(0)
        main.solution, main.solved_path = {"center": {"ra": ra, "dec": dec}}, path
        main.solve_count += 1
    main.on_stack_button, main.start_solve = on_stack_button, start_solve

    dock = MountDock(main)
    dock.timer.stop()                             # polled by hand below
    dock.mount_combo.setCurrentIndex(dock.mount_combo.findData("indi_simulator_telescope"))
    dock.focus_combo.setCurrentIndex(0)
    dock.server_check.setChecked(True)
    dock.host_edit.setText(f"localhost:{port}")

    def run_until(cond, timeout=60.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            dock.poll()
            if cond():
                return True
            time.sleep(0.1)
        return False

    try:
        dock.on_connect()
        assert run_until(lambda: dock.mount is not None and not dock.busy), dock.error
        dock.mount.park(False)
        assert dock.rate_combo.count() == 4 and dock.rate_combo.itemText(0) == "Guide"   # the driver's rates
        dock.rate_combo.setCurrentIndex(1)
        dock.on_rate(1)
        assert dock.client.wait(lambda: dock.mount.rates()[1] == "2x", 5)
        dock.jog_buttons["N"].setDown(True)          # held: moving north ...
        dock.on_jog("N", True)
        assert dock.client.wait(lambda: dock.client.switch(dock.mount.device, "TELESCOPE_MOTION_NS")
                                ["MOTION_NORTH"], 5)
        dock.jog_buttons["N"].setDown(False)         # ... released without the signal: the poll stops it
        dock.poll()
        assert dock.client.wait(lambda: not any(dock.client.switch(dock.mount.device,
                                                                    "TELESCOPE_MOTION_NS").values()), 5)
        answers = []                                  # Park / Home ask first; No: nothing sent
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            lambda *a, **k: answers.append(a[1]) or QtWidgets.QMessageBox.No)
        dock.on_park()
        dock.on_home()
        time.sleep(0.5)
        assert answers == ["Park", "Home"] and not dock.mount.parked()
        assert dock.jog_frame.toolTip() in ("tracking", "not tracking")       # the jog area's tint
        assert dock.tracking_shown is dock.mount.tracking()
        dock.set_target(310.357, 45.28)
        assert mt.parse_radec(dock.target_edit.text()) == pytest.approx((310.357, 45.28), abs=2e-4)
        dock.on_goto()
        assert run_until(lambda: dock.mount.tracking() and not dock.mount.slewing())
        solves.append((310.5, 45.4))              # Solve & sync: synced to the solved centre
        dock.on_solve_sync()
        assert run_until(lambda: dock.job is None and "synced" in dock.error)
        assert run_until(lambda: abs(dock.mount.position()[0] - 310.5) < 0.01)
        solves.extend([(310.0, 45.0), (310.357, 45.28)])   # Center: off once, then on target
        dock.on_center()
        assert run_until(lambda: dock.job is None, 120), dock.error
        assert "centred" in dock.error and "2 round" in dock.error
    finally:
        dock.shutdown()
        main.close()


def test_camera_window_docks_hidden_toggled_remembered(monkeypatch, tmp_path):
    """The Mount dock starts hidden, View menu / Shift+M toggles it, its state is kept."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture import gui
        from pystarlight.capture.qt import QtCore, QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from types import SimpleNamespace
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])   # noqa: F841
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):   # not the user's settings
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))

    def window():
        w = QtWidgets.QMainWindow()
        w.settings = QtCore.QSettings("PyStarLight", "camera")
        w.c = SimpleNamespace(gps=None)
        w.setCentralWidget(QtWidgets.QLabel("image"))
        for name in ("make_telescope_docks", "show_dock"):
            setattr(w, name, getattr(gui.MainWindow, name).__get__(w))
        w.make_telescope_docks()
        w.mount_dock.timer.stop()
        return w

    w = window()
    assert w.mount_dock.isHidden()
    act = w.mount_dock.toggleViewAction()
    assert act.shortcut().toString() == "Shift+M" and act in w.menuBar().actions()[0].menu().actions()
    w.show()
    act.trigger()
    assert w.mount_dock.isVisible()
    w.settings.setValue("dock_state", w.saveState())
    w.settings.sync()
    w.mount_dock.shutdown()
    w.close()
    w2 = window()
    w2.show()
    assert w2.mount_dock.isVisible()                  # restored as it was
    w2.mount_dock.shutdown()
    w2.close()


def test_home_uses_the_drivers_property():
    """TELESCOPE_HOME's GO (INDI 2) first, else a driver's own GO_HOME (its GO, or its first)."""
    from types import SimpleNamespace
    sent = []
    props = {}
    client = SimpleNamespace(get=lambda dev, name: props.get(name),
                             set_switch=lambda dev, name, values: sent.append((name, values)))
    m = mt.Mount(client, device="AM5")
    assert not m.can_home()
    with pytest.raises(RuntimeError):
        m.home()
    props["GO_HOME"] = SimpleNamespace(name="GO_HOME", values={"HOME_GO": False}, state="Idle")
    m.home()
    assert sent[-1] == ("GO_HOME", {"HOME_GO": True}) and not m.homing()
    props["TELESCOPE_HOME"] = SimpleNamespace(name="TELESCOPE_HOME", state="Busy",
                                              values={"FIND": False, "SET": False, "GO": False})
    m.home()
    assert sent[-1] == ("TELESCOPE_HOME", {"GO": True}) and m.homing()


def test_set_link_usb_and_wifi():
    """USB: the system port named ZWO; Wi-Fi: the mount's address (host[:port])."""
    from types import SimpleNamespace
    props = {"CONNECTION_MODE": {"CONNECTION_SERIAL": False, "CONNECTION_TCP": True},
             "DEVICE_PORT": {"PORT": "/dev/ttyUSB0"}, "SYSTEM_PORTS": {"Other": False, "ZWO_Device_1": False},
             "DEVICE_ADDRESS": {"ADDRESS": "192.168.4.1", "PORT": "4030"}}
    sent = []

    def set_any(dev, name, values):
        sent.append((name, values))
        if name == "SYSTEM_PORTS":
            props["DEVICE_PORT"] = {"PORT": "/dev/serial/by-id/usb-" + next(iter(values)) + "-if00"}
        elif name in ("DEVICE_ADDRESS", "DEVICE_PORT"):
            props[name] = dict(values)
    client = SimpleNamespace(wait_property=lambda dev, name, t=0: name in props, wait=lambda f, t=0: f(),
                             switch=lambda dev, n: props.get(n), text=lambda dev, n: props.get(n),
                             set_switch=set_any, set_text=set_any)
    m = mt.Mount(client, device="AM5")
    assert m.set_link("usb") == "/dev/serial/by-id/usb-ZWO_Device_1-if00"
    assert ("CONNECTION_MODE", {"CONNECTION_SERIAL": True}) in sent
    assert m.set_link("usb", "/dev/ttyACM0") == "/dev/ttyACM0"
    assert m.set_link("wifi") == "192.168.4.1:4030"                       # the AM5's own hotspot
    assert m.set_link("wifi", "192.168.1.50") == "192.168.1.50:4030"
    assert sent[-1] == ("DEVICE_ADDRESS", {"ADDRESS": "192.168.1.50", "PORT": "4030"})


def test_jog_moves_the_simulator(simulator):
    m = mt.Mount(simulator).connect()
    m.park(False)
    time.sleep(0.5)
    rates, _ = m.rates()
    m.set_rate(rates[-1][0])                      # fastest
    dec0 = m.position()[1]
    m.jog("S", True)
    time.sleep(2.0)
    m.jog("S", False)
    time.sleep(0.5)
    dec1 = m.position()[1]
    assert dec1 < dec0 - 0.1
    time.sleep(1.0)
    assert abs(m.position()[1] - dec1) < 0.05     # stopped
