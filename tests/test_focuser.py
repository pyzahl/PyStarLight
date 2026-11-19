"""Focuser through INDI (capture/focuser.py) and the camera window's Focuser dock: absolute and
relative moves against a fake client, the dock against INDI's focuser simulator (skipped
without indiserver)."""

import shutil
import socket
import time

import pytest

from pystarlight.capture import focuser as fc
from pystarlight.capture import indi


class FakeClient:
    """Properties as {name: (state, values)}; what is sent is recorded in order."""

    def __init__(self, props):
        self.props, self.sent = props, []

    def get(self, device, name):
        return self.props.get(name)

    def number(self, device, name):
        p = self.props.get(name)
        return dict(p[1]) if p else None

    switch = number

    def state(self, device, name):
        p = self.props.get(name)
        return p[0] if p else None

    def set_number(self, device, name, values):
        self.sent.append((name, values))

    set_switch = set_number


def test_absolute_moves_are_clipped_targets():
    c = FakeClient({fc.ABS: ("Ok", {"FOCUS_ABSOLUTE_POSITION": 900.0}),
                    "FOCUS_MAX": ("Ok", {"FOCUS_MAX_VALUE": 1000.0}),
                    "FOCUS_TEMPERATURE": ("Ok", {"TEMPERATURE": -273.0})})
    f = fc.Focuser(c, "EAF")
    assert f.kind() == "absolute" and f.temperature() is None          # no probe
    f.move_by(250)
    f.move_by(-2000)
    assert c.sent == [(fc.ABS, {"FOCUS_ABSOLUTE_POSITION": 1000}), (fc.ABS, {"FOCUS_ABSOLUTE_POSITION": 0})]
    c.props[fc.ABS] = ("Busy", {"FOCUS_ABSOLUTE_POSITION": 950.0})
    assert f.moving() and f.status()["position"] == 950.0


def test_relative_moves_set_the_direction_first():
    c = FakeClient({fc.REL: ("Ok", {"FOCUS_RELATIVE_POSITION": 0.0}),
                    "FOCUS_MOTION": ("Ok", {"FOCUS_INWARD": True, "FOCUS_OUTWARD": False})})
    f = fc.Focuser(c, "DSD")
    f.move_by(40)
    f.move_by(-15)
    assert c.sent == [("FOCUS_MOTION", {"FOCUS_OUTWARD": True}), (fc.REL, {"FOCUS_RELATIVE_POSITION": 40}),
                      ("FOCUS_MOTION", {"FOCUS_INWARD": True}), (fc.REL, {"FOCUS_RELATIVE_POSITION": 15})]
    assert f.kind() == "relative" and f.moved == 25 and f.position() is None
    with pytest.raises(RuntimeError):
        f.move_to(100)


def test_focuser_dock_on_the_simulator(monkeypatch, tmp_path):
    """Focuser alone (no mount driver): Connect from the Focuser dock, steps out / in, go to, mark
    and back to the mark."""
    if not (shutil.which("indiserver") and shutil.which("indi_simulator_focus")):
        pytest.skip("no indiserver / focuser simulator")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
        from pystarlight.capture.telescope_gui import FocuserDock, MountDock
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from types import SimpleNamespace
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):   # not the user's settings
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    main = QtWidgets.QMainWindow()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(gps=None)
    main.mount_dock = dock = MountDock(main)
    fdock = FocuserDock(main)
    for d in (dock, fdock):
        d.timer.stop()                              # polled by hand below
    dock.mount_combo.setCurrentIndex(0)
    dock.focus_combo.setCurrentIndex(dock.focus_combo.findData("indi_simulator_focus"))
    dock.server_check.setChecked(True)
    dock.host_edit.setText(f"localhost:{port}")

    def run_until(cond, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            dock.poll()
            fdock.poll()
            if cond():
                return True
            time.sleep(0.1)
        return False

    def pos():
        f = dock.focuser
        return f.position() if f is not None and not f.moving() else None

    try:
        fdock.connect_button.click()
        assert run_until(lambda: dock.focuser is not None and pos() is not None), dock.error
        assert dock.mount is None and dock.state_label.text() == "connected, no mount"
        start = pos()
        assert fdock.pos_label.text() == str(int(start)) and fdock.goto_button.isEnabled()
        fdock.step_combo.setCurrentText("50")
        fdock.move_buttons[2].click()                       # Out ▶
        assert run_until(lambda: pos() == start + 50)
        fdock.move_buttons[0].click()                       # ◀◀ In: ten steps
        assert run_until(lambda: pos() == start - 450)
        fdock.mark_button.click()
        fdock.poll()
        assert fdock.mark_label.text() == f"mark: {int(start - 450)}"
        fdock.goto_spin.setValue(int(start + 2000))
        fdock.goto_button.click()
        assert run_until(lambda: pos() == start + 2000)
        fdock.back_button.click()
        assert run_until(lambda: pos() == start - 450)
        assert main.settings.value("focuser/Focuser Simulator/step") == "50"
        fdock.connect_button.click()                        # Disconnect
        assert run_until(lambda: dock.client is None)
        assert fdock.state_label.text() == "not connected" and not fdock.move_buttons[0].isEnabled()
    finally:
        fdock.shutdown()
        dock.shutdown()
        main.close()


def test_find_prefers_a_focuser_that_is_not_a_mount():
    class C:
        def find_device(self, interface, timeout):
            return "Mount+Focus"

        def devices(self):
            return ["Mount+Focus", "EAF"]

        def interfaces(self, d):
            return indi.TELESCOPE | indi.FOCUSER if d == "Mount+Focus" else indi.FOCUSER
    assert fc.find(C()) == "EAF"
