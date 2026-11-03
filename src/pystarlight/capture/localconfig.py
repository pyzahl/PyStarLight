"""The private config for the capture, without needing the rest of the package.

pystarlight.config when it imports (template defaults merged in), else the toml file read
directly (the Pi's system Python): $PYSTARLIGHT_CONFIG ('none': no file), ./pystarlight.toml,
the project root's, ~/.config/pystarlight/config.toml. `update_section` writes one table back
(the camera's station dialog), keeping comments and everything else. Standard library only.
"""

import json
import os
import re
import sys


def user_config_path():
    """~/.config/pystarlight/config.toml (as pystarlight.config.user_config_path)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pystarlight", "config.toml")


def config_path():
    """The private config file in use (the lookup order of pystarlight.config.find_config), or
    None: $PYSTARLIGHT_CONFIG ('none': no file), ./pystarlight.toml, the project root's, the
    user config file."""
    env = os.environ.get("PYSTARLIGHT_CONFIG")
    if env == "none":
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(here, "..", "..", ".."))        # src/pystarlight/capture
    for p in (env, os.path.join(os.getcwd(), "pystarlight.toml"), os.path.join(root, "pystarlight.toml"),
              user_config_path()):
        if p and os.path.isfile(p):
            return p
    return None


def read():
    """The private config file as a dict, read directly ({} when there is none)."""
    p = config_path()
    if p is None:
        return {}
    try:
        import tomllib
        with open(p, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


# ---- writing one table, keeping everything else (comments, other tables, keys) as it is ----
def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        r = repr(v)
        return r if any(c in r for c in ".eEn") else r + ".0"
    return json.dumps(str(v), ensure_ascii=False)       # a TOML basic string


def _comment(line):
    """The '  # ...' after a 'key = value' line's value ('' if none)."""
    rest = line.split("=", 1)[1].lstrip()
    if rest.startswith('"'):
        i = 1
        while i < len(rest) and rest[i] != '"':
            i += 2 if rest[i] == "\\" else 1
        tail = rest[i + 1:]
    elif rest.startswith("'"):
        tail = rest[rest.find("'", 1) + 1:]
    else:
        tail = rest[len(rest.split("#", 1)[0]):] if "#" in rest else ""
    j = tail.find("#")
    return "" if j < 0 else "   " + tail[j:].rstrip("\n")


def update_section(name, values, path=None):
    """Set keys of the [name] table in the config file (path, else the one in use, else a new
    user config file, mode 600): existing lines are replaced in place (their comments kept),
    new keys are added at the end of the table. The result is checked to parse before it
    replaces the file. Returns the path written."""
    import tomllib
    path = path or config_path() or user_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    head = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{name}]"), None)
    if head is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{name}]")
        head = len(lines) - 1
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    todo = dict(values)
    for i in range(head + 1, end):
        m = re.match(r"\s*([A-Za-z0-9_-]+)\s*=", lines[i])
        if m and m.group(1) in todo:
            key = m.group(1)
            lines[i] = f"{key} = {toml_value(todo.pop(key))}" + _comment(lines[i])
    at = end
    while at > head + 1 and (not lines[at - 1].strip() or lines[at - 1].lstrip().startswith("#")):
        at -= 1                             # before the blank lines (and the next table's comments)
    lines[at:at] = [f"{k} = {toml_value(v)}" for k, v in todo.items()]
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)                     # never write a file that does not parse
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    return path


def section(name):
    """One table of the config, e.g. 'observer' ({} when missing)."""
    try:
        from pystarlight.config import load
        return load().get(name) or {}
    except Exception:                       # system Python without the package's needs
        return read().get(name) or {}


def software_version():
    """PyStarLight's version, '' when the package cannot be imported."""
    try:
        from pystarlight import __version__
        return __version__
    except Exception:
        return ""
