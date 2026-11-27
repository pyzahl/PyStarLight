#!/usr/bin/env python3
"""Build the Windows setup files of a release into a folder (default dist/):

    PyStarLight-Setup.cmd    one file to double-click: a cmd header + windows/setup.ps1 (CRLF)
    constraints.txt          the package versions of uv.lock for the "desktop" extra (the setup
                             installs exactly these, as tested)

    python scripts/build_windows_setup.py [--dist dist] [--no-constraints]

The release workflow (.github/workflows/release.yml) runs it next to `uv build` and attaches all
files to the GitHub release. Standard library only (+ uv on PATH for the constraints).
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "windows" / "setup.ps1"
MARKER = "#PS-BEGIN"

# cmd stops at `exit /b`, so it never reads the PowerShell part; PowerShell gets the text after the
# marker (the marker is built from two halves in the command, so the command line does not match)
HEADER = r"""@echo off
rem PyStarLight setup for Windows: double-click to install or update PyStarLight (for this user,
rem no administrator rights needed). Uninstall: Start menu -> PyStarLight -> Uninstall PyStarLight,
rem or run this file with -Uninstall. Other options: -Version v1.1.0, -NoDesktop.
setlocal
set "PSL_SELF=%~f0"
set "PSL_SETUP=%TEMP%\PyStarLight-setup-%RANDOM%.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t = [IO.File]::ReadAllText($env:PSL_SELF); $i = $t.IndexOf('#PS' + '-BEGIN'); [IO.File]::WriteAllText($env:PSL_SETUP, $t.Substring($i), (New-Object Text.UTF8Encoding $true))"
if errorlevel 1 (
  echo Could not unpack the setup script.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PSL_SETUP%" -Pause %*
set "RC=%ERRORLEVEL%"
del "%PSL_SETUP%" >nul 2>nul
exit /b %RC%
"""  # noqa: E501 (a cmd line cannot be wrapped)


def crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def build_cmd(out_dir: Path) -> Path:
    ps1 = PS1.read_text(encoding="utf-8")
    if any(ord(c) > 127 for c in ps1):
        raise SystemExit("windows/setup.ps1 must be ASCII (Windows PowerShell 5.1 reads it as ANSI)")
    if MARKER in ps1:
        raise SystemExit(f"windows/setup.ps1 must not contain {MARKER}")
    out = out_dir / "PyStarLight-Setup.cmd"
    out.write_bytes(crlf(HEADER + MARKER + "\n" + ps1).encode("ascii"))
    return out


def build_constraints(out_dir: Path) -> Path:
    out = out_dir / "constraints.txt"
    subprocess.run(["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", "--no-default-groups",
                    "--extra", "desktop", "--format", "requirements-txt", "-o", str(out)],
                   cwd=ROOT, check=True)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dist", default=str(ROOT / "dist"))
    p.add_argument("--no-constraints", action="store_true")
    a = p.parse_args(argv)
    out_dir = Path(a.dist)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(build_cmd(out_dir))
    if not a.no_constraints:
        print(build_constraints(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
