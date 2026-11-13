"""`pystarlight`: one command for all of PyStarLight's programs (also `python -m pystarlight`).

    pystarlight analyze FILE    analyse a recording: photometry, event timing, report (also: analyse)
    pystarlight player FILE     play a recording with its GPS / time stamps, frame status, stack & solve
    pystarlight camera          record on the Raspberry Pi 5 (GPS-timed frames)
    pystarlight config ...      the private config: init, path, show, sites
    pystarlight doctor          check the installation: what is missing and how to fix it
    pystarlight FILE            = pystarlight analyze FILE
    pystarlight                 = pystarlight analyze (a file dialog)

The older names stay as commands of their own: pystarlight-view (= analyze), pystarlight-player
(pystarlight-review before), pystarlight-camera, pystarlight-config.
"""

from __future__ import annotations

import os
import sys

TOOLS = {
    "analyze": ("analyse a recording: photometry, event timing (D/R), astrometry, event.json",
                "pystarlight.viewer"),
    "player": ("play a recording: GPS / time stamps, dropped frames, stack & plate solve",
               "pystarlight.capture.player"),
    "camera": ("record on the Raspberry Pi 5: live view, GPS-timed RAVF / SER", "pystarlight.capture.app"),
    "config": ("the private config: init, path, show, sites", "pystarlight.config"),
    "doctor": ("check the installation: what is missing and how to fix it", "pystarlight.doctor"),
}
ALIASES = {"analyse": "analyze", "view": "analyze", "viewer": "analyze", "review": "player",
           "play": "player", "capture": "camera", "check": "doctor"}


def usage() -> str:
    from . import __version__
    lines = [f"PyStarLight {__version__}: occultation light curves, from the camera to the report.",
             "", "usage: pystarlight <command> [options]     (pystarlight <command> --help for more)", ""]
    lines += [f"  {name:9s} {text}" for name, (text, _) in TOOLS.items()]
    lines += ["", "  pystarlight FILE   = analyze FILE;  pystarlight   = analyze with a file dialog",
              "  analyse = analyze, review = player"]
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    first = argv[0] if argv else ""
    if first in ("-h", "--help", "help"):
        print(usage())
        return 0
    if first in ("-V", "--version", "version"):
        from . import __version__
        print(f"PyStarLight {__version__}")
        return 0
    name = ALIASES.get(first, first)
    if name in TOOLS:
        argv = argv[1:]
    else:                               # a file, an option of analyze, or nothing: analyze
        if first and not first.startswith("-") and not os.path.exists(first) and first.isidentifier():
            print(f"pystarlight: unknown command '{first}'\n\n{usage()}", file=sys.stderr)
            return 2
        name = "analyze"
    import importlib
    try:
        module = importlib.import_module(TOOLS[name][1])
    except ImportError as e:            # no Qt for the player / camera: say what to install
        if "PySide6 or PyQt5" not in str(e):
            raise
        print(f"pystarlight {name}: {e}", file=sys.stderr)
        return 1
    return module.main(argv) or 0


if __name__ == "__main__":
    sys.exit(main())
