"""winlaunch: the windowed launchers' log + message box, the settings file, the .ico."""

import struct
from pathlib import Path

import pytest

from pystarlight import winlaunch


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(winlaunch, 'log_dir', lambda: tmp_path / 'logs')
    boxes = []
    monkeypatch.setattr(winlaunch, 'message_box', lambda title, text: boxes.append((title, text)))
    return tmp_path / 'logs', boxes


def test_icon_has_four_png_images():
    data = winlaunch.ico_bytes(Path(winlaunch.__file__).with_name('pystarlight_logo.png'))
    reserved, kind, count = struct.unpack('<HHH', data[:6])
    assert (reserved, kind, count) == (0, 1, 4)
    sizes = []
    for k in range(count):
        w, h, _, _, planes, bpp, size, offset = struct.unpack('<BBBBHHII', data[6 + 16 * k:22 + 16 * k])
        assert planes == 1 and bpp == 32
        assert data[offset:offset + 8] == b'\x89PNG\r\n\x1a\n'          # PNG-compressed entry
        assert offset + size <= len(data)
        sizes.append(w or 256)
    assert sizes == [256, 48, 32, 16]


def test_run_logs_output_and_boxes_errors(logs, monkeypatch):
    log_dir, boxes = logs
    import pystarlight.cli as cli

    def fine(argv):
        print('hello from', argv)
        return 0
    monkeypatch.setattr(cli, 'main', fine)
    assert winlaunch.run('analyze', ['x.ravf']) == 0
    text = (log_dir / 'analyze.log').read_text(encoding='utf-8')
    assert "PyStarLight" in text and "['analyze', 'x.ravf']" in text
    assert boxes == []

    def broken(argv):
        raise RuntimeError('no Tk here')
    monkeypatch.setattr(cli, 'main', broken)
    assert winlaunch.run('player', []) == 1
    text = (log_dir / 'player.log').read_text(encoding='utf-8')
    assert 'RuntimeError: no Tk here' in text
    assert len(boxes) == 1 and 'no Tk here' in boxes[0][1] and 'player.log' in boxes[0][1]


def test_run_keeps_the_log_short(logs, monkeypatch):
    log_dir, _ = logs
    log_dir.mkdir(parents=True)
    (log_dir / 'camera.log').write_bytes(b'old line\n' * (winlaunch.LOG_KEEP // 9 + 10))
    import pystarlight.cli as cli
    monkeypatch.setattr(cli, 'main', lambda argv: 0)
    winlaunch.run('camera', [])
    assert (log_dir / 'camera.log').stat().st_size < winlaunch.LOG_KEEP


def test_settings_creates_the_config_and_opens_it(tmp_path, monkeypatch):
    from pystarlight import config
    monkeypatch.delenv('PYSTARLIGHT_CONFIG', raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'project_root', lambda: None)
    target = tmp_path / 'appdata' / 'pystarlight' / 'config.toml'
    monkeypatch.setattr(config, 'user_config_path', lambda: target)
    opened = []
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda args, **kw: opened.append(args))
    assert winlaunch.settings() == 0
    assert target.is_file() and '[diffraction]' in target.read_text(encoding='utf-8')
    assert opened and str(target) in opened[0]
