#!/bin/sh
# Install or update PyStarLight (Linux, macOS): git pull, system check, uv sync, check.
#   ./install.sh                    update, with the extras chosen before (first time: all camera)
#   ./install.sh all camera aravis  choose the extras (remembered)
#   ./install.sh --help             all options (scripts/install.py)
cd "$(dirname "$0")" || exit 1
if ! command -v uv >/dev/null 2>&1; then          # installed but not yet on PATH (new terminal)
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin" /opt/homebrew/bin /usr/local/bin; do
        if [ -x "$d/uv" ]; then PATH="$d:$PATH"; export PATH; break; fi
    done
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install it, open a new terminal and run ./install.sh again:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
unset VIRTUAL_ENV
# --no-project: runs without touching .venv (which may be missing or broken)
exec uv run --no-project scripts/install.py "$@"
