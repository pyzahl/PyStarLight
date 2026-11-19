"""Focuser through INDI (capture/focuser.py), the focus helper (capture/focus.py) and the
camera's Focuser window: moves against a fake client, HFD / curve fit / star following on
synthetic frames, the window against INDI's focuser simulator (skipped without indiserver)."""

import shutil
import socket
import time

import numpy as np
import pytest

from pystarlight.capture import focus, indi
from pystarlight.capture import focuser as fc


class FakeClient:
    """Properties as {name: (state, values)}; what is sent is recorded in order."""

    def __init__(self, props):
        self.props, self.sent, self.messages = props, [], []

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


def test_focuser_window_on_the_simulator(monkeypatch, tmp_path):
    """Focuser alone (no mount driver): Connect from the Focuser window, steps out / in, go to,
    mark and back to the mark; a (fake) focus star whose HFD follows the position: the curve's
    best position is found and Go to best goes there."""
    if not (shutil.which("indiserver") and shutil.which("indi_simulator_focus")):
        pytest.skip("no indiserver / focuser simulator")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture import focuser_gui
        from pystarlight.capture.qt import QtCore, QtWidgets
        from pystarlight.capture.telescope_gui import MountDock
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from types import SimpleNamespace
    monkeypatch.setattr(focuser_gui, "SETTLE_S", 0.3)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):   # not the user's settings
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))

    class Meter:                                    # FocusMeter's face: a star measuring
        state, note, star = "idle", "", (100.0, 80.0)
        samples = []

        def ask(self, *req):
            self.state = "idle" if req[0] == "stop" else "measuring"

        def box(self):
            return (70, 50, 130, 110) if self.state == "measuring" else None
    meter = Meter()
    main = QtWidgets.QMainWindow()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(gps=None, focus=meter, decode=SimpleNamespace(max_value=4095))
    main.view = SimpleNamespace(focus_boxes=[], pick_callback=None, update=lambda: None,
                                setCursor=lambda c: None)
    main.mount_dock = dock = MountDock(main)
    win = focuser_gui.FocuserWindow(main)
    for d in (dock, win):
        d.timer.stop()                              # polled by hand below
    dock.mount_combo.setCurrentIndex(0)
    dock.focus_combo.setCurrentIndex(dock.focus_combo.findData("indi_simulator_focus"))
    dock.server_check.setChecked(True)
    dock.host_edit.setText(f"localhost:{port}")
    win.show()

    def run_until(cond, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            f = dock.focuser
            if meter.state == "measuring" and f is not None and f.position() is not None:
                p = f.position()                     # the star's size at this position
                meter.samples.append((time.monotonic(), float(np.hypot(3.0, (p - best) / 200.0)), 3.0, 900.0))
            dock.poll()
            win.poll()
            if cond():
                return True
            time.sleep(0.05)
        return False

    def pos():
        f = dock.focuser
        return f.position() if f is not None and not f.moving() else None

    try:
        win.connect_button.click()
        assert run_until(lambda: dock.focuser is not None and pos() is not None), dock.error
        assert dock.mount is None and dock.state_label.text() == "connected, no mount"
        start = pos()
        best = start + 700
        assert win.pos_label.text() == str(int(start)) and win.goto_button.isEnabled()
        win.step_combo.setCurrentText("50")
        win.move_buttons[2].click()                         # out ▶
        assert run_until(lambda: pos() == start + 50)
        win.move_buttons[0].click()                         # ◀◀: ten steps in
        assert run_until(lambda: pos() == start - 450)
        win.mark_button.click()
        win.poll()
        assert win.mark_label.text() == f"{int(start - 450)}"
        win.goto_spin.setValue(int(start + 2000))
        win.goto_button.click()
        assert run_until(lambda: pos() == start + 2000)
        win.back_button.click()
        assert run_until(lambda: pos() == start - 450)
        assert main.settings.value("focuser/Focuser Simulator/step") == "50"

        win.center_button.click()                           # the focus curve
        assert run_until(lambda: main.view.focus_boxes and "HFD" in win.star_label.text())
        for target in (start - 500, start + 300, start + 1100, start + 1900):
            win.on_goto(target)
            assert run_until(lambda t=target: pos() == t)
            assert run_until(lambda t=target: len(win.curve.get(t, [])) >= 5)
        assert win.fit is not None and abs(win.fit[0] - best) < 30 and win.best_button.isEnabled()
        win.best_button.click()
        assert run_until(lambda: pos() is not None and abs(pos() - best) < 30)
        win.close()                                         # closed: measuring stops
        assert meter.state == "idle" and main.view.focus_boxes == []
        win.connect_button.click()                          # Disconnect
        assert run_until(lambda: dock.client is None)
        assert win.state_label.text() == "not connected" and not win.move_buttons[0].isEnabled()
    finally:
        win.shutdown()
        dock.shutdown()
        main.close()


def star(shape, x, y, sigma, amp, ring=0.0):
    yy, xx = np.indices(shape)
    r = np.hypot(xx - x, yy - y)
    return amp * np.exp(-((r - ring) ** 2) / (2 * sigma ** 2))


def test_hfd_of_a_gaussian_and_a_ring():
    rng = np.random.default_rng(0)
    img = rng.normal(100, 3, (200, 200)) + star((200, 200), 100.3, 99.6, 2.0, 2000)
    m = focus.hfd(img, 100, 100)
    assert m["x"] == pytest.approx(100.3, abs=0.1) and m["y"] == pytest.approx(99.6, abs=0.1)
    assert m["hfd"] == pytest.approx(2.0 * 2.0 * np.sqrt(np.pi / 2), rel=0.1)   # 2 x mean radius
    ring = rng.normal(100, 3, (200, 200)) + star((200, 200), 100, 100, 1.5, 800, ring=8.0)
    assert focus.hfd(ring, 100, 100)["hfd"] == pytest.approx(16.0, rel=0.15)     # a donut: ~ its size


def test_fit_curve_finds_the_vertex():
    pts = [(p, float(np.hypot(2.5, (p - 1234) / 150))) for p in (600, 900, 1200, 1500, 1800)]
    best, low, curve = focus.fit_curve(pts)
    assert best == pytest.approx(1234, abs=5) and low == pytest.approx(2.5, abs=0.3)
    assert curve(1800) == pytest.approx(pts[-1][1], rel=0.1)
    assert focus.fit_curve(pts[:2]) is None                         # too few positions
    assert focus.fit_curve([q for q in pts if q[0] < 1200]) is None # one side only


def test_meter_picks_the_unsaturated_centre_star_and_follows_it():
    shape = (300, 400)
    rng = np.random.default_rng(1)

    def frame(dx=0.0):
        img = rng.normal(100, 3, shape)
        img += star(shape, 210 + dx, 140, 2.0, 2500)               # near the centre
        img += star(shape, 180 + dx, 160, 2.0, 60000)              # brighter, saturated
        img += star(shape, 60 + dx, 60, 2.0, 9000)                 # bright, off centre
        return np.minimum(img, 4095)
    m = focus.FocusMeter(lambda raw: raw, full_scale=4095)
    try:
        m.ask("center")
        m.process(frame(), 0.0)
        assert m.state == "measuring" and m.star[0] == pytest.approx(210, abs=0.5)
        for i in range(1, 6):
            m.process(frame(i * 0.8), float(i))                   # drifts: followed
        assert m.star[0] == pytest.approx(214, abs=0.5) and len(m.samples) == 6     # with the first
        assert m.samples[-1][1] == pytest.approx(5.0, rel=0.15) and m.samples[-1][3] < 4095   # own peak
        m.ask("at", 61, 59)                                        # a point picked: nearest star
        m.process(frame(), 7.0)
        assert m.star == pytest.approx((60, 60), abs=0.5) and len(m.samples) == 1   # afresh
        for i in range(focus.LOST_FRAMES):
            m.process(rng.normal(100, 3, shape), 8.0 + i)          # clouds
        assert m.state == "lost"
        m.ask("stop")
        assert m.state == "idle" and m.box() is None
    finally:
        m.close()


def test_find_prefers_a_focuser_that_is_not_a_mount():
    class C:
        def find_device(self, interface, timeout):
            return "Mount+Focus"

        def devices(self):
            return ["Mount+Focus", "EAF"]

        def interfaces(self, d):
            return indi.TELESCOPE | indi.FOCUSER if d == "Mount+Focus" else indi.FOCUSER
    assert fc.find(C()) == "EAF"


def test_usb_hint_names_the_driver(tmp_path):
    dev = tmp_path / "3-1"
    dev.mkdir()
    (dev / "idVendor").write_text("03c3\n")
    (dev / "idProduct").write_text("1f10\n")
    (tmp_path / "usb3").mkdir()                     # a hub: no ids
    assert fc.usb_focusers(str(tmp_path)) == [("ZWO EAF", "indi_asi_focuser")]
    assert "indi_asi_focuser" in fc.usb_hint("indi_ieaf_focus", str(tmp_path))
    assert fc.usb_hint("indi_asi_focuser", str(tmp_path)) == ""
    assert fc.usb_hint("", str(tmp_path / "none")) == ""


def test_driver_message_and_lost_hint():
    c = FakeClient({fc.ABS: ("Alert", {"FOCUS_ABSOLUTE_POSITION": 13259.0})})
    c.messages = [("ZWO AM5", "Error reading RA/DEC."),
                  ("ASI EAF", "[ERROR] Failed to read position. Error: 4"),
                  ("ZWO AM5", "Error reading RA/DEC.")]
    st = fc.Focuser(c, "ASI EAF").status()
    assert st["alert"] and st["message"] == "[ERROR] Failed to read position. Error: 4"
    assert "Disconnect, then Connect" in fc.lost_hint(st["message"])
    assert fc.lost_hint("[INFO] ASI EAF parameters updated") == ""
