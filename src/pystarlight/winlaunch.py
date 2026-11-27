"""Launchers for the desktop (Windows Start menu, desktop icons, "Open with"): the programs without a
console window, their output in a log file, and a message box when a program cannot start.

[project.gui-scripts] in pyproject.toml makes these windowed executables (pythonw: no console):

    pystarlight-analyze-gui [FILE]     the analyzer
    pystarlight-player-gui [FILE]      the player
    pystarlight-camera-gui             the capture program
    pystarlight-settings-gui           the private config in Notepad (created from the template)

Logs: %LOCALAPPDATA%\\PyStarLight\\logs\\<tool>.log (elsewhere ~/.cache/pystarlight/logs), the last
run of each tool appended, cut to its last 2 MB.

The Windows setup (windows/setup.ps1) also calls `python -m pystarlight.winlaunch icon OUT.ico` for
the shortcuts' icon (the logo as a PNG-compressed .ico: 256, 48, 32, 16 px).
"""

from __future__ import annotations

import os
import struct
import sys
import time
import traceback
from pathlib import Path

LOG_KEEP = 2_000_000           # bytes of a log kept before a run


def log_dir() -> Path:
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'PyStarLight'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'pystarlight'
    return base / 'logs'


def _open_log(name: str):
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f'{name}.log'
    if p.exists() and p.stat().st_size > LOG_KEEP:            # keep the tail only
        data = p.read_bytes()[-LOG_KEEP // 2:]
        p.write_bytes(data[data.find(b'\n') + 1:])
    return p, open(p, 'a', encoding='utf-8', buffering=1, errors='replace')


def message_box(title: str, text: str) -> None:
    """A plain error box: Windows' own (no Tk needed), else Tk, else nothing (it is in the log)."""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)        # MB_ICONERROR
            return
        except Exception:
            pass
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        root.destroy()
    except Exception:
        pass


def run(name: str, argv: list[str]) -> int:
    """`pystarlight <name> argv` with stdout / stderr into the log (a windowed program has none);
    an exception, or an exit with an error, shows a message box with the end of the log."""
    from . import __version__
    path, log = _open_log(name)
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log            # a desktop shortcut has no console to print to
    print(f'\n==== {time.strftime("%Y-%m-%d %H:%M:%S")} PyStarLight {__version__} {name} {argv}', file=log)
    rc = 1
    try:
        from . import cli
        rc = cli.main([name, *argv]) or 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if not isinstance(e.code, int) and e.code is not None:
            print(e.code, file=log)
    except BaseException:
        traceback.print_exc(file=log)
        rc = 1
    finally:
        sys.stdout, sys.stderr = saved
        log.close()
    if rc:
        tail = path.read_text(encoding='utf-8', errors='replace').splitlines()[-12:]
        message_box(f'PyStarLight {name}', f'PyStarLight {name} stopped (exit code {rc}).\n\n'
                    + '\n'.join(tail) + f'\n\nThe whole log: {path}')
    return rc


def analyze() -> int:
    return run('analyze', sys.argv[1:])


def player() -> int:
    return run('player', sys.argv[1:])


def camera() -> int:
    return run('camera', sys.argv[1:])


def settings() -> int:
    """The private config in a text editor (Notepad on Windows), created from the template first."""
    from . import config
    p = config.find_config()
    if p is None:
        p = config.init()
    try:
        if sys.platform == 'win32':
            import subprocess
            subprocess.Popen(['notepad.exe', str(p)])
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.Popen(['open', '-t', str(p)])
        else:
            import subprocess
            subprocess.Popen(['xdg-open', str(p)])
    except OSError as e:
        message_box('PyStarLight settings', f'Could not open an editor: {e}\n\nThe file: {p}')
        return 1
    return 0


def ico_bytes(png_path: Path, sizes=(256, 48, 32, 16)) -> bytes:
    """A Windows .ico with PNG-compressed images (Vista and later) of the logo at these sizes."""
    import cv2
    import numpy as np
    img = cv2.imread(str(png_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise OSError(f'cannot read {png_path}')
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = np.dstack([img, np.full(img.shape[:2], 255, img.dtype)])
    pngs = []
    for s in sizes:
        im = cv2.resize(img, (s, s), interpolation=cv2.INTER_AREA if s < img.shape[0] else cv2.INTER_CUBIC)
        ok, buf = cv2.imencode('.png', im)
        if not ok:
            raise OSError('PNG encoding failed')
        pngs.append((s, buf.tobytes()))
    head = struct.pack('<HHH', 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b'', b''
    for s, b in pngs:
        wh = 0 if s >= 256 else s
        entries += struct.pack('<BBBBHHII', wh, wh, 0, 0, 1, 32, len(b), offset + len(data))
        data += b
    return head + entries + data


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog='python -m pystarlight.winlaunch',
                                description='helpers of the desktop setup')
    sub = p.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('icon', help='write the logo as a .ico')
    i.add_argument('out')
    a = p.parse_args(argv)
    if a.cmd == 'icon':
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_bytes(ico_bytes(Path(__file__).with_name('pystarlight_logo.png')))
        print(a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
