"""Qt binding for the capture GUI: PySide6 (the repo venv: `uv run pystarlight-camera`; PyQt5 has
no aarch64 wheels) or PyQt5 (the Pi's system Python, apt python3-pyqt5).

$PYSTARLIGHT_QT = pyside6 / pyqt5 picks one; default: PySide6 if installed, else PyQt5.
"""
import os

_want = os.environ.get("PYSTARLIGHT_QT", "").lower()

if _want != "pyqt5":
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        Signal, Slot = QtCore.Signal, QtCore.Slot
        QShortcut = QtGui.QShortcut
        API = "PySide6"
    except ImportError:
        if _want == "pyside6":
            raise
        API = None
if _want == "pyqt5" or API is None:
    from PyQt5 import QtCore, QtGui, QtWidgets
    Signal, Slot = QtCore.pyqtSignal, QtCore.pyqtSlot
    QShortcut = QtWidgets.QShortcut
    API = "PyQt5"

__all__ = ["API", "QShortcut", "QtCore", "QtGui", "QtWidgets", "Signal", "Slot",
           "event_pos", "exec_app", "fix_cv2_qt_env"]


def event_pos(e):
    """Mouse / wheel event position in the widget (QPointF) - Qt6: position(); Qt5 mouse
    events: localPos() (QWheelEvent has position() in Qt 5.14+)."""
    return e.position() if hasattr(e, "position") else e.localPos()


def exec_app(app):
    return app.exec() if hasattr(app, "exec") else app.exec_()


def fix_cv2_qt_env():
    """opencv-python (pulled in by the ravf package) points Qt at its own bundled Qt5 plugins
    on import; Qt (PySide6 above all) then loads the wrong platform plugin. Call before the
    QApplication is made."""
    for key in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR"):
        if f"{os.sep}cv2{os.sep}" in os.environ.get(key, ""):
            del os.environ[key]
