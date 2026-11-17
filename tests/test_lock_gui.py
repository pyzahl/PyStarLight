"""The camera window's Star lock dock (capture/lock_gui.py) on the synthetic sky of
test_guider: the Lock check box (target from the marker; independent of recording), the boxes on
the image, the flux plot, the log, guiding through a fake Mount panel (calibrates first),
release; Lock unticks itself without stars."""

import time
from types import SimpleNamespace

import numpy as np
import pytest

from pystarlight.capture import guider as gd
from test_guider import TARGET, Sky


@pytest.fixture
def qt(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):   # not the user's settings
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    return app, QtCore, QtWidgets


def window(qt, tmp_path, sky):
    app, QtCore, QtWidgets = qt
    from pystarlight.capture.lock_gui import LockDock
    fast = gd.Settings(interval_s=0.5, settle_s=0.2, cal_px=4.0, lost_after_s=5.0)
    locker = gd.LockThread(gd.StarLock(fast, log=lambda m: None), decode=lambda raw: raw)
    main = QtWidgets.QMainWindow()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    main.c = SimpleNamespace(locker=locker, region=(0, 0, 320, 240),
                             decode=SimpleNamespace(max_value=65535),
                             stacker=SimpleNamespace(out_dir=str(tmp_path), name="cam"),
                             recorder=SimpleNamespace(state="idle"))
    main.marks = [{"kind": "target", "x": TARGET[0], "y": TARGET[1]}]
    main.marks_shift = (0.0, 0.0)
    main.marks_check, main.stack_show = QtWidgets.QCheckBox(), QtWidgets.QCheckBox()
    main.marks_check.setChecked(True)
    main.view = SimpleNamespace(lock_boxes=[], update=lambda: None)
    mount = SimpleNamespace(can_guide=lambda: True, slewing=lambda: False, pulse=sky.pulse)
    main.mount_dock = SimpleNamespace(mount=mount, client=SimpleNamespace(connected=True), job=None)
    dock = LockDock(main)
    dock.timer.stop()                                   # polled by hand
    main.show()                                         # (the plot redraws only when shown)
    dock.show()
    return app, main, dock


def feed(app, dock, sky, seconds, until=None):
    """Frames as they come from a 50 fps camera (real time: the lock's windows are timed)."""
    end = time.monotonic() + seconds
    last = time.monotonic()
    while time.monotonic() < end:
        now = time.monotonic()
        if dock.thread.wants():
            dock.thread.put(sky.frame(now - last), int(time.time() * 1e9))
            last = now
        app.processEvents()
        dock.poll()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


def test_lock_dock_locks_plots_logs_guides_and_releases(qt, tmp_path):
    sky = Sky(drift=(0.4, 0.0))
    app, main, dock = window(qt, tmp_path, sky)
    try:
        lock = dock.lock
        main.c.recorder.state = "armed"                  # recording does not lock: only the box
        feed(app, dock, sky, 0.3)
        assert lock.state == gd.IDLE and not dock.lock_check.isChecked()
        dock.lock_check.setChecked(True)
        assert feed(app, dock, sky, 5, lambda: lock.state == gd.LOCKED and len(lock.history) > 20)
        main.c.recorder.state = "idle"                   # recording ends: the lock stays
        feed(app, dock, sky, 0.5)
        assert lock.state == gd.LOCKED and dock.lock_check.isChecked()
        assert lock.target == pytest.approx(TARGET[:2])                    # from the marker
        assert all(np.hypot(r["x"] - TARGET[0], r["y"] - TARGET[1]) > 30 for r in lock.ref)
        kinds = [b[4].name() for b in main.view.lock_boxes]
        assert len(kinds) == len(lock.ref) + 1 and "#ff4081" in kinds      # stars + the target
        assert dock.plot.data.shape[1] == 2 and "target" in dock.plot.label
        assert "locked" in dock.state_label.text()
        dock.guide_check.setChecked(True)                # calibrates first, then guides
        assert lock.state == gd.CALIBRATING or feed(app, dock, sky, 2, lambda: lock.state == gd.CALIBRATING)
        assert feed(app, dock, sky, 60, lambda: lock.cal is not None), lock.note
        assert dock.guide_check.isChecked() and lock.guide
        feed(app, dock, sky, 12)
        assert lock.state == gd.LOCKED and np.hypot(*sky.err) < 2.0 and lock.pulses > 2
        dock.lock_check.setChecked(False)                # release
        assert feed(app, dock, sky, 2, lambda: lock.state == gd.IDLE)
        assert not dock.guide_check.isChecked() and main.view.lock_boxes == []
        log = list(tmp_path.glob("cam_*Z_lock.csv"))
        assert len(log) == 1
        rows = log[0].read_text().splitlines()
        assert rows[1].startswith("unix_time,state") and len(rows) > 10
        assert any(r.split(",")[-2] not in ("0", "") for r in rows[2:])     # pulses logged
    finally:
        dock.shutdown()
        dock.thread.close()
        main.close()


def test_guiding_needs_a_mount_that_takes_pulses(qt, tmp_path):
    sky = Sky()
    app, main, dock = window(qt, tmp_path, sky)
    try:
        main.mount_dock.mount = None                     # Mount panel not connected
        dock.lock_check.setChecked(True)
        assert feed(app, dock, sky, 3, lambda: dock.lock.state == gd.LOCKED)
        dock.guide_check.setChecked(True)
        assert not dock.guide_check.isChecked() and "Mount panel" in dock.lock.note
        assert not dock.cal_button.isEnabled()
        main.c.region = (8, 0, 320, 240)                 # a new camera region: released
        assert feed(app, dock, sky, 2, lambda: dock.lock.state == gd.IDLE)
        assert "region" in dock.lock.note and not dock.lock_check.isChecked()
        sky.transparency = 0.0                           # no stars: Lock unticks itself
        dock.lock_check.setChecked(True)
        assert feed(app, dock, sky, 3, lambda: not dock.lock_check.isChecked())
        assert dock.lock.state == gd.IDLE and "no lock star" in dock.lock.message
        assert len(list(tmp_path.glob("cam_*Z_lock.csv"))) == 1          # no log for a failed lock
    finally:
        dock.shutdown()
        dock.thread.close()
        main.close()

