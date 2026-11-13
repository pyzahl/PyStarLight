"""`pystarlight doctor`: is everything there that the programs need, and if not, how to fix it.

    pystarlight doctor                       check; required = the extras chosen at install
    pystarlight doctor --extras all,camera   check against these extras
    python src/pystarlight/doctor.py ...     the same without importing the package first (the
                                             installer does this, so a broken install still reports)

Required checks fail (exit code 1) only for the extras chosen: the installer's
.pystarlight-install.json, else --extras, else nothing beyond the base packages. Standard library
only at the top: every check imports what it tests inside a try.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Callable, NamedTuple, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__" and sys.path and os.path.abspath(sys.path[0]) == _HERE:
    del sys.path[0]          # run as a file: this folder's modules must not shadow others (wcs, ...)

ROOT = Path(__file__).resolve().parents[2]
CHECKOUT = (ROOT / "pyproject.toml").is_file()
WINDOWS = sys.platform == "win32"
INSTALLER = "install.cmd" if WINDOWS else "./install.sh"
# extras pulling in others (pyproject.toml's 'all'; tests/test_doctor.py keeps it in step)
EXTRAS_IN = {"all": ["adv", "ravf", "catalog", "plot"]}
SETTIME_HELPER = "/usr/local/sbin/pystarlight-settime"


class Result(NamedTuple):
    status: str              # ok, missing, warn, info
    detail: str = ""
    fix: str = ""


class Check(NamedTuple):
    name: str
    needs: str               # extra that requires it ('' = base, None = never required)
    run: Callable[[], Result]


def sync_fix(extra: str) -> str:
    if CHECKOUT:
        return f"{INSTALLER} <your extras> {extra}   (or: uv sync --extra ... --extra {extra})"
    return f'pip install "starlight-photometry[{extra}]"'


def version_of(module) -> str:
    return str(getattr(module, "__version__", "") or getattr(module, "VERSION", "") or "")


def importable(name: str, extra: str, label: Optional[str] = None) -> Result:
    try:
        mod = importlib.import_module(name)
    except Exception as e:  # noqa: BLE001 (any import failure is the finding)
        if extra:
            fix = sync_fix(extra)
        else:
            fix = (f"{INSTALLER} --rebuild" if CHECKOUT
                   else "pip install --force-reinstall starlight-photometry")
        return Result("missing", f"{label or name}: {type(e).__name__}: {e}", fix)
    return Result("ok", version_of(mod) or "installed")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_python() -> Result:
    v = platform.python_version()
    where = sys.prefix if sys.prefix != sys.base_prefix else sys.executable
    detail = f"{v} ({where})"
    try:
        wanted = (ROOT / ".python-version").read_text().split()[0] if CHECKOUT else ""
    except (OSError, IndexError):
        wanted = ""
    if wanted and not v.startswith(wanted):
        return Result("warn", f"{detail}; the repo asks for {wanted}", f"{INSTALLER} --rebuild")
    return Result("ok", detail)


def check_environment() -> Result:
    other = os.environ.get("VIRTUAL_ENV")
    if other and Path(other).resolve() != Path(sys.prefix).resolve():
        return Result("warn", f"VIRTUAL_ENV={other} is set but not this environment (uv ignores it, warning)",
                      "unset VIRTUAL_ENV  (and remove it from your shell profile)")
    return Result("ok", "")


def check_tk() -> Result:
    try:
        import tkinter
    except Exception as e:  # noqa: BLE001
        base = getattr(sys, "_base_executable", "") or sys.executable
        uv_own = "/uv/python/" in base.replace("\\", "/")
        fixes = []
        if CHECKOUT and not uv_own:
            fixes.append(f"{INSTALLER} --managed-python   (uv's own Python has Tk)")
        fixes.append({"darwin": "brew install python-tk", "win32": 're-run the python.org installer, '
                      'tick "tcl/tk and IDLE"'}.get(sys.platform, "sudo apt install python3-tk "
                                                     "(Fedora: python3-tkinter, Arch: tk)"))
        return Result("missing", f"no tkinter in {base}: {e}", "  or  ".join(fixes))
    return Result("ok", f"Tk {tkinter.TkVersion}")


def check_qt() -> Result:
    try:
        from pystarlight.capture import qt
    except Exception as e:  # noqa: BLE001
        return Result("missing", str(e).splitlines()[0], sync_fix("camera"))
    return Result("ok", qt.API)


def check_adv2() -> Result:
    try:
        import Adv2  # noqa: F401
    except ImportError as e:
        return Result("missing", str(e), sync_fix("adv"))
    try:
        import Adv2.Adv2File  # noqa: F401
    except OSError as e:
        fix = "sh scripts/build_advcore.sh" if CHECKOUT else "build ADVCore (scripts/build_advcore.sh)"
        return Result("missing", f"Adv2's native library does not load: {e}", fix)
    except Exception as e:  # noqa: BLE001
        return Result("missing", f"{type(e).__name__}: {e}", sync_fix("adv"))
    return Result("ok", "Adv2 (with its native library)")


def check_aravis() -> Result:
    if not sys.platform.startswith("linux"):
        return Result("info", "Linux only")
    try:
        import gi
    except ImportError as e:
        return Result("missing", f"PyGObject: {e}",
                      "sudo apt install libgirepository1.0-dev libcairo2-dev gir1.2-aravis-0.8, then "
                      + sync_fix("aravis"))
    try:
        gi.require_version("Aravis", "0.8")
        from gi.repository import Aravis
    except (ValueError, ImportError) as e:
        return Result("missing", f"Aravis 0.8 typelib: {e}", "sudo apt install gir1.2-aravis-0.8")
    try:
        v = f"{Aravis.MAJOR_VERSION}.{Aravis.MINOR_VERSION}.{Aravis.MICRO_VERSION}"
    except AttributeError:
        v = "0.8"
    return Result("ok", f"PyGObject {gi.__version__}, Aravis {v}")


def check_zwo() -> Result:
    try:
        from pystarlight.capture import asi_cam
        lib = asi_cam.library()
    except Exception as e:  # noqa: BLE001
        return Result("info", f"not checked: {e}")
    if lib is None:
        return Result("info", "ZWO's libASICamera2 not installed (only for ZWO ASI cameras; CAPTURE.md)")
    return Result("ok", f"libASICamera2 {asi_cam.sdk_version(lib)}")


def check_solver() -> Result:
    if not shutil.which("solve-field"):
        return Result("info", "no local astrometry.net (solve-field): plate solves go online "
                      "(nova.astrometry.net, needs the API key); README: astrometry")
    try:
        from pystarlight.wcs.solve import astrometry_cfg, local_index_files
        n = len(local_index_files())
    except Exception as e:  # noqa: BLE001
        return Result("info", f"solve-field found, index files not checked: {e}")
    if not n:
        return Result("warn", f"solve-field found but no index files (config {astrometry_cfg()})",
                      "install index files, CAPTURE.md 'Offline plate solving'")
    return Result("ok", f"solve-field, {n} index files")


def check_settime_helper() -> Result:
    if not sys.platform.startswith("linux"):
        return Result("info", "Linux only")
    src = Path(__file__).resolve().parent / "capture" / "settime_helper.py"
    line = f"sudo install -o root -g root -m 755 {src} {SETTIME_HELPER}"
    if not os.path.exists(SETTIME_HELPER):
        return Result("info", "not installed (only to set the Pi's clock from the GPS; CAPTURE.md)")
    try:
        same = Path(SETTIME_HELPER).read_bytes() == src.read_bytes()
    except OSError as e:
        return Result("warn", f"cannot compare: {e}")
    if not same:
        return Result("warn", f"{SETTIME_HELPER} differs from the repo's settime_helper.py", line)
    return Result("ok", "same as the repo's")


def check_config() -> Result:
    try:
        from pystarlight import config
        path = config.find_config()
    except Exception as e:  # noqa: BLE001
        return Result("info", f"not checked: {e}")
    if path is None:
        return Result("info", "no private config yet (defaults); create one: pystarlight config init")
    return Result("ok", str(path))


CHECKS = [
    Check("Python", "", check_python),
    Check("environment", None, check_environment),
    Check("numpy", "", lambda: importable("numpy", "")),
    Check("scipy", "", lambda: importable("scipy", "")),
    Check("OpenCV", "", lambda: importable("cv2", "", "opencv")),
    Check("scikit-image", "", lambda: importable("skimage", "", "scikit-image")),
    Check("astropy", "", lambda: importable("astropy", "")),
    Check("PyStarLight", "", lambda: importable("pystarlight", "")),
    Check("matplotlib", "plot", lambda: importable("matplotlib", "plot")),
    Check("Tk (viewer window)", "plot", check_tk),
    Check("RAVF reader", "ravf", lambda: importable("ravf", "ravf")),
    Check("ADV reader", "adv", check_adv2),
    Check("astroquery", "catalog", lambda: importable("astroquery", "catalog")),
    Check("Qt (player, camera)", "camera", check_qt),
    Check("pyserial (GPS)", "camera", lambda: importable("serial", "camera", "pyserial")),
    Check("Aravis cameras", "aravis", check_aravis),
    Check("ZWO ASI cameras", None, check_zwo),
    Check("local plate solver", None, check_solver),
    Check("GPS clock helper", None, check_settime_helper),
    Check("config", None, check_config),
]


def expand(extras) -> set:
    out, todo = set(), list(extras)
    while todo:
        e = todo.pop()
        if e not in out:
            out.add(e)
            todo += EXTRAS_IN.get(e, [])
    return out


def chosen_extras(arg: Optional[str]) -> tuple[Optional[set], str]:
    """The extras to hold the install to, and where they came from (None: unknown)."""
    if arg is not None:
        return expand([] if arg == "none" else [e for e in arg.split(",") if e]), "--extras"
    try:
        state = json.loads((ROOT / ".pystarlight-install.json").read_text(encoding="utf-8"))
        return expand(state.get("extras", [])), INSTALLER
    except (OSError, ValueError):
        return None, ""


def run_checks(extras: Optional[set], out=print) -> int:
    """Print one line per check (and the fix under it); 1 when something required is missing."""
    failed = 0
    for check in CHECKS:
        try:
            r = check.run()
        except Exception as e:  # noqa: BLE001 (a crashing check must not hide the others)
            r = Result("warn", f"check failed: {type(e).__name__}: {e}")
        if check.name == "environment" and r.status == "ok":
            continue
        required = check.needs == "" or (check.needs is not None and check.needs in (extras or ()))
        status = r.status
        if status == "missing" and not required:
            status = "absent" if check.needs is not None else "info"
        failed |= status == "missing"
        tag = {"ok": "ok", "missing": "MISSING", "warn": "WARN", "info": "-", "absent": "-"}[status]
        detail = r.detail if status != "absent" else f"not installed (extra '{check.needs}')"
        out(f"  {tag:8s} {check.name:22s} {detail}")
        if r.fix and status in ("missing", "warn"):
            out(f"  {'':8s} {'':22s} fix: {r.fix}")
    return 1 if failed else 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="pystarlight doctor", description=__doc__.split("\n\n")[0])
    p.add_argument("--extras", help="comma-separated extras that must work, e.g. all,camera,aravis "
                   "('none': base only); default: the installer's choice")
    a = p.parse_args(argv)
    extras, source = chosen_extras(a.extras)
    shown = ", ".join(sorted(extras)) if extras else "none"
    print(f"PyStarLight installation check ({platform.system()} {platform.machine()})")
    print(f"  extras required: {shown}" + (f" (from {source})" if source else
                                            " (unknown: only the base packages are required)"))
    code = run_checks(extras)
    print("\nAll required parts are there." if code == 0
          else "\nSomething required is missing: see 'fix:' above.")
    return code


if __name__ == "__main__":
    sys.exit(main())
