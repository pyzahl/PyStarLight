#!/usr/bin/env python3
"""Install or update PyStarLight in this folder: git pull, check the system, uv sync, check the result.

Started by the wrappers in the repo root (they find uv and run this with `uv run --no-project`,
so it works even when .venv is missing or broken):

    ./install.sh                      Linux, macOS
    install.cmd                       Windows (cmd or PowerShell: .\\install.cmd)

    ./install.sh all camera aravis    first time: the extras wanted (remembered)
    ./install.sh                      later: update with the remembered extras
    ./install.sh --no-pull            sync only, no git pull
    ./install.sh --managed-python     build .venv on uv's own Python (has Tk; remembered)
    ./install.sh --rebuild            delete .venv and build it again
    ./install.sh --check              only check the installation (pystarlight doctor)

Standard library only, Python 3.8+. The choices live in .pystarlight-install.json (git-ignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".pystarlight-install.json"
DEFAULT_EXTRAS = ["all", "camera"]
WINDOWS = sys.platform == "win32"
WRAPPER = "install.cmd" if WINDOWS else "./install.sh"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def head(msg: str) -> None:
    say(f"\n== {msg}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def clean_env() -> dict:
    """The environment for uv: a VIRTUAL_ENV of another environment only makes uv warn."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    return env


def run(cmd, capture=False, check=False, **kw):
    kw.setdefault("cwd", ROOT)
    kw.setdefault("env", clean_env())
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    try:
        r = subprocess.run([str(c) for c in cmd], **kw)
    except OSError as e:                       # program not found
        r = subprocess.CompletedProcess(cmd, 127, stdout=str(e))
    if check and r.returncode:
        raise SystemExit(r.returncode)
    return r


def find_uv() -> str:
    uv = os.environ.get("UV") or shutil.which("uv")   # uv run sets $UV to its own path
    if not uv:
        say("uv not found. Install it (https://docs.astral.sh/uv/), open a new terminal, run again.")
        raise SystemExit(1)
    return uv


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def known_extras(pyproject: Path = ROOT / "pyproject.toml") -> dict:
    """Extras in pyproject.toml: {name: [requirements]} (a plain parser: tomllib is 3.11+)."""
    text = pyproject.read_text(encoding="utf-8")
    m = re.search(r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[)", text, re.M | re.S)
    if not m:
        return {}
    body = re.sub(r"#[^\n\"']*$", "", m.group(1), flags=re.M)      # comments outside strings
    extras, name = {}, None
    for line in body.splitlines():
        key = re.match(r"\s*([A-Za-z0-9_.-]+)\s*=", line)
        if key:
            name = key.group(1)
            extras[name] = []
            line = line[key.end():]
        if name:
            extras[name] += re.findall(r'"([^"]*)"', line)
    return extras


def expand(extras, table: dict) -> set:
    """The extras with the ones they pull in (all = adv, ravf, catalog, plot)."""
    out, todo = set(), list(extras)
    while todo:
        e = todo.pop()
        if e in out:
            continue
        out.add(e)
        for req in table.get(e, []):
            m = re.match(r"starlight-photometry\[([^\]]*)\]", req)
            if m:
                todo += [x.strip() for x in m.group(1).split(",")]
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def package_manager() -> str:
    for pm in ("apt", "dnf", "pacman", "zypper", "brew"):
        if shutil.which(pm):
            return pm
    return ""


INSTALL_CMD = {"apt": "sudo apt install", "dnf": "sudo dnf install", "pacman": "sudo pacman -S",
               "zypper": "sudo zypper install", "brew": "brew install"}

# system packages per need and package manager
PACKAGES = {
    "girepository": {"apt": "libgirepository1.0-dev", "dnf": "gobject-introspection-devel",
                     "pacman": "gobject-introspection", "zypper": "gobject-introspection-devel"},
    "cairo": {"apt": "libcairo2-dev", "dnf": "cairo-devel", "pacman": "cairo", "zypper": "cairo-devel"},
    "aravis": {"apt": "gir1.2-aravis-0.8", "dnf": "aravis", "pacman": "aravis",
               "zypper": "typelib-1_0-Aravis-0_8"},
    "pkg-config": {"apt": "pkg-config", "dnf": "pkgconf", "pacman": "pkgconf", "zypper": "pkg-config"},
    "compiler": {"apt": "build-essential", "dnf": "gcc", "pacman": "base-devel", "zypper": "gcc"},
    "tk": {"apt": "python3-tk", "dnf": "python3-tkinter", "pacman": "tk", "zypper": "python3-tk",
           "brew": "python-tk"},
}


def install_line(needs) -> str:
    pm = package_manager()
    pkgs = [PACKAGES[n].get(pm) for n in needs]
    if pm and all(pkgs):
        return f"{INSTALL_CMD[pm]} {' '.join(dict.fromkeys(pkgs))}"
    return "install the development files of: " + ", ".join(needs)


def aravis_typelib() -> bool:
    dirs = ["/usr/lib/girepository-1.0", "/usr/lib64/girepository-1.0", "/usr/local/lib/girepository-1.0"]
    dirs += [str(p) for p in Path("/usr/lib").glob("*/girepository-1.0")]
    dirs += os.environ.get("GI_TYPELIB_PATH", "").split(os.pathsep)
    return any(Path(d, "Aravis-0.8.typelib").is_file() for d in dirs if d)


# ---------------------------------------------------------------------------
# Python that .venv is (or will be) built on
# ---------------------------------------------------------------------------
def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def wanted_python_version() -> str:
    try:
        return (ROOT / ".python-version").read_text().split()[0]
    except (OSError, IndexError):
        return ""


def venv_info() -> dict:
    """pyvenv.cfg of .venv: home, version_info (empty without a .venv)."""
    info = {}
    try:
        for line in (ROOT / ".venv" / "pyvenv.cfg").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
    except OSError:
        pass
    return info


def is_managed(path: str, uv: str) -> bool:
    """A Python that uv installed itself (under `uv python dir`)."""
    r = run([uv, "python", "dir"], capture=True)
    managed_dir = r.stdout.strip() if r.returncode == 0 else ""
    try:
        return bool(managed_dir) and Path(path).resolve().is_relative_to(Path(managed_dir).resolve())
    except AttributeError:                    # Python < 3.9
        return str(Path(path).resolve()).startswith(str(Path(managed_dir).resolve()))


def target_python(uv: str, managed: bool, use_venv: bool = True) -> str:
    """The interpreter uv sync will use: .venv's own when it stays, else what uv finds ('' when
    uv has to download one; its own builds have Tk and the C headers)."""
    info = venv_info()
    wanted = wanted_python_version()
    version = info.get("version_info", info.get("version", ""))
    if use_venv and venv_python().exists() and (not wanted or version.startswith(wanted)):
        return str(venv_python())
    cmd = [uv, "python", "find", "--no-project"]
    cmd += ["--python-preference", "only-managed"] if managed else ["--python-preference", "managed"]
    r = run(cmd + ([wanted] if wanted else []), capture=True)
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else ""


def python_has(py: str, what: str) -> bool:
    code = {"tk": "import tkinter",
            "headers": "import os, sysconfig; "
                       "raise SystemExit(not os.path.isfile(os.path.join(sysconfig.get_paths()['include'], "
                       "'Python.h')))"}[what]
    return run([py, "-c", code], capture=True).returncode == 0


def base_of(py: str) -> str:
    """The real interpreter behind a venv's python."""
    r = run([py, "-c", "import sys; print(getattr(sys, '_base_executable', '') or sys.executable)"],
            capture=True)
    return r.stdout.strip() if r.returncode == 0 else py


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def git_pull(argv) -> None:
    """Fast-forward to the remote branch. Re-runs the new installer when the pull changed it.
    A failed pull (offline, local commits) leaves the files as they were: say so, go on."""
    head("Update (git pull)")
    if not (ROOT / ".git").exists():
        say("Not a git clone: nothing to pull (a new version: download it again).")
        return
    git = shutil.which("git")
    if not git:
        say("git not found: skipping the update.")
        return
    up = run([git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], capture=True)
    if up.returncode:
        say("This branch follows no remote branch: skipping the pull.")
        return
    before = run([git, "rev-parse", "HEAD"], capture=True).stdout.strip()
    me = file_hash(Path(__file__))
    r = run([git, "pull", "--ff-only"], capture=True)
    say(r.stdout.rstrip())
    if r.returncode:
        say(f"git pull failed (offline? local commits or changes in the way?).\n"
            f"Going on with the version in this folder; '{WRAPPER} --no-pull' skips the pull.")
        return
    after = run([git, "rev-parse", "HEAD"], capture=True).stdout.strip()
    if before != after:
        log = run([git, "log", "--oneline", "--no-decorate", f"{before}..{after}"], capture=True)
        lines = log.stdout.splitlines()
        say(f"{len(lines)} new commit(s):")
        say("\n".join("  " + ln for ln in lines[:15]) + ("\n  ..." if len(lines) > 15 else ""))
    if file_hash(Path(__file__)) != me:
        say("The installer itself changed: running the new one.")
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv, "--no-pull"])
        raise SystemExit(r.returncode)


def check_system(extras: set, uv: str, state: dict) -> list:
    """Problems that would make uv sync fail or leave a program unusable: [(what, fix)].
    May switch the environment to uv's own Python (state['managed_python'])."""
    head("System check")
    problems = []
    managed_wanted = state.get("managed_python", False)
    py = target_python(uv, managed_wanted, use_venv=not state.get("rebuild"))
    if py and managed_wanted and not is_managed(base_of(py), uv):
        say(".venv runs on the system's Python, uv's own is wanted: building .venv again.")
        state["rebuild"] = True
        py = target_python(uv, True, use_venv=False)
    if py:
        base = base_of(py)
        managed = is_managed(base, uv)
        say(f"Python for .venv: {base}" + ("  (uv's own)" if managed else "  (the system's)"))
        lacks = []
        if "plot" in extras and not python_has(py, "tk"):
            lacks.append("Tk (the viewer's window)")
        if "aravis" in extras and not python_has(py, "headers"):
            lacks.append("its C headers (to build PyGObject)")
        if lacks and not managed:
            say(f"That Python lacks {' and '.join(lacks)}: switching to uv's own Python, which has both\n"
                f"(remembered; '{WRAPPER} --system-python' goes back to the system's).")
            state["managed_python"] = True
            state["rebuild"] = venv_python().exists()
        elif lacks:
            problems.append((f"Python {base} lacks {' and '.join(lacks)}", f"{WRAPPER} --rebuild"))
    else:
        say("Python for .venv: uv downloads its own")

    if "aravis" in extras:
        if not sys.platform.startswith("linux"):
            say("Note: the 'aravis' extra (GenICam cameras) is for Linux; leaving it out here.")
            extras.discard("aravis")
        else:
            needs = []
            if not (shutil.which("cc") or shutil.which("gcc")):
                needs.append("compiler")
            if not shutil.which("pkg-config"):
                needs.append("pkg-config")
            else:
                if run(["pkg-config", "--exists", "gobject-introspection-1.0"], capture=True).returncode:
                    needs.append("girepository")
                if run(["pkg-config", "--exists", "cairo"], capture=True).returncode:
                    needs.append("cairo")
            if not aravis_typelib():
                needs.append("aravis")
            if needs:
                problems.append(("Aravis cameras (extra 'aravis') need system packages",
                                 install_line(needs)))
    elif sys.platform.startswith("linux") and aravis_typelib():
        again = " ".join([WRAPPER, *state["extras"], "aravis"])
        say("Note: Aravis is installed on this system but the 'aravis' extra is not chosen;\n"
            f"GenICam cameras (Grasshopper, Basler...) need it: {again}")
    if not problems:
        say("ok")
    return problems


def sync(uv: str, extras, state: dict) -> int:
    head("Install packages (uv sync)")
    if state.pop("rebuild", False) and (ROOT / ".venv").exists():
        say("Removing .venv to build it again.")
        try:
            shutil.rmtree(ROOT / ".venv")
        except OSError as e:
            say(f"Could not remove .venv ({e}); close programs running from it and try again.")
            return 1
    cmd = [uv, "sync"]
    if run([uv, "lock", "--check"], capture=True).returncode == 0:
        cmd.append("--locked")
    else:
        say("uv.lock does not match pyproject.toml (edited here?): uv sync updates uv.lock.")
    for e in sorted(extras):
        cmd += ["--extra", e]
    if state.get("managed_python"):
        cmd += ["--python-preference", "only-managed"]
    say("$ " + " ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd)))
    r = run(cmd)
    if r.returncode:
        say("\nuv sync failed. The lines above say why; common causes:\n"
            "  - no network (packages not in uv's cache yet)\n"
            "  - a package built from source needs system packages (see the system check)\n"
            "  - an old uv: uv self update")
    return r.returncode


def fix_adv2_on_apple_silicon(extras: set) -> None:
    """Adv2's wheel has an x86_64 library only: build the native one (scripts/build_advcore.sh)."""
    if "adv" not in extras or sys.platform != "darwin" or platform.machine() != "arm64":
        return
    r = run([venv_python(), "-c", "import Adv2.Adv2File"], capture=True)
    if r.returncode == 0:
        return
    head("Adv2 native library (Apple Silicon)")
    if not (shutil.which("c++") and shutil.which("git")):
        say("Needs git and a C++ compiler: xcode-select --install, then run this again.")
        return
    run(["sh", ROOT / "scripts" / "build_advcore.sh"], env={**clean_env(), "PYTHON": str(venv_python())})


def doctor(uv: str, extras) -> int:
    head("Check (pystarlight doctor)")
    return run([uv, "run", "--no-sync", "python", ROOT / "src" / "pystarlight" / "doctor.py",
                "--extras", ",".join(sorted(extras)) or "none"]).returncode


def resolve_extras(args_extras, state: dict, table: dict) -> list:
    if args_extras:
        chosen = [] if args_extras == ["none"] else [e for x in args_extras for e in x.split(",") if e]
    else:
        chosen = state.get("extras", DEFAULT_EXTRAS)
    unknown = [e for e in chosen if e not in table]
    if unknown:
        say(f"Unknown extra(s): {', '.join(unknown)}. Known: {', '.join(table)} (or 'none').")
        raise SystemExit(2)
    return list(dict.fromkeys(chosen))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog=WRAPPER, description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("extras", nargs="*", help="extras to install, e.g. all camera aravis ('none': base only); "
                   "remembered for later runs (default: the last ones, first time: all camera)")
    p.add_argument("--no-pull", action="store_true", help="no git pull")
    p.add_argument("--managed-python", action="store_true",
                   help="build .venv on uv's own Python (remembered)")
    p.add_argument("--system-python", action="store_true", help="let uv take the system's Python again")
    p.add_argument("--rebuild", action="store_true", help="delete .venv and build it again")
    p.add_argument("--check", action="store_true", help="only check the installation")
    a = p.parse_args(argv)

    uv = find_uv()
    state = load_state()
    table = known_extras()
    extras = resolve_extras(a.extras, state, table)
    if a.check:
        return doctor(uv, expand(extras, table))
    if not a.no_pull:
        git_pull(argv)
        table = known_extras()              # the pull may have brought new extras

    state["extras"] = extras
    if a.managed_python or a.system_python:
        state["managed_python"] = a.managed_python
    if a.rebuild:
        state["rebuild"] = True
    full = expand(extras, table)
    say(f"Extras: {' '.join(extras) or 'none'}"
        + (f"  (= {' '.join(sorted(full))})" if full != set(extras) else ""))

    problems = check_system(full, uv, state)
    if problems:
        say("\nFix these first, then run this again:")
        for what, fix in problems:
            say(f"  - {what}:\n      {fix}")
        if "aravis" in full:
            rest = [e for e in extras if e != "aravis"]
            say(f"  (or without Aravis cameras: {WRAPPER} {' '.join(rest) or 'none'})")
        save_state(state)
        return 1
    extras = [e for e in extras if e in full]           # aravis dropped off Linux
    state["extras"] = extras
    save_state(state)                      # remembered even when the sync fails: rerun repeats it

    code = sync(uv, extras, state)
    save_state(state)                      # without the done 'rebuild'
    if code:
        return code
    fix_adv2_on_apple_silicon(full)
    code = doctor(uv, full)
    head("Done" if code == 0 else "Done, with problems (see above)")
    say(f"Run: uv run pystarlight      Update later: {WRAPPER}")
    return code


if __name__ == "__main__":
    sys.exit(main())
