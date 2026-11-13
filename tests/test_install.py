"""scripts/install.py (installer, stdlib only) and pystarlight doctor."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from pystarlight import cli, doctor

ROOT = Path(__file__).resolve().parents[1]

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@pytest.fixture(scope="module")
def inst():
    spec = importlib.util.spec_from_file_location("pystarlight_installer", ROOT / "scripts" / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pyproject_extras():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]


def test_installer_reads_extras_like_tomllib(inst):
    assert inst.known_extras() == pyproject_extras()


def test_doctor_knows_what_all_contains():
    all_ = pyproject_extras()["all"]
    assert all_ == [f"starlight-photometry[{','.join(doctor.EXTRAS_IN['all'])}]"]


def test_expand_and_resolve(inst):
    table = inst.known_extras()
    assert inst.expand(["all", "camera"], table) == {"all", "adv", "ravf", "catalog", "plot", "camera"}
    assert inst.resolve_extras([], {}, table) == inst.DEFAULT_EXTRAS
    assert inst.resolve_extras([], {"extras": ["camera"]}, table) == ["camera"]
    assert inst.resolve_extras(["all,camera", "camera"], {}, table) == ["all", "camera"]
    assert inst.resolve_extras(["none"], {"extras": ["all"]}, table) == []
    with pytest.raises(SystemExit):
        inst.resolve_extras(["cmaera"], {}, table)


def test_state_round_trip(inst, tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "STATE", tmp_path / "state.json")
    assert inst.load_state() == {}
    inst.save_state({"extras": ["all"], "managed_python": True})
    assert inst.load_state() == {"extras": ["all"], "managed_python": True}


def test_missing_aravis_build_packages_reported_before_sync(inst, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(inst, "target_python", lambda *a, **k: "")       # uv would download one
    monkeypatch.setattr(inst, "aravis_typelib", lambda: True)
    monkeypatch.setattr(inst, "package_manager", lambda: "apt")
    monkeypatch.setattr(inst.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(cmd, capture=False, **kw):                                   # pkg-config: nothing there
        return subprocess.CompletedProcess(cmd, 1 if cmd[0] == "pkg-config" else 0, stdout="")
    monkeypatch.setattr(inst, "run", run)
    problems = inst.check_system({"aravis", "camera"}, "uv", {"extras": ["camera", "aravis"]})
    assert len(problems) == 1
    assert problems[0][1] == "sudo apt install libgirepository1.0-dev libcairo2-dev"


def test_python_without_tk_switches_to_uv_python(inst, monkeypatch):
    monkeypatch.setattr(inst, "target_python", lambda *a, **k: "/usr/bin/python3")
    monkeypatch.setattr(inst, "base_of", lambda py: py)
    monkeypatch.setattr(inst, "is_managed", lambda py, uv: False)
    monkeypatch.setattr(inst, "python_has", lambda py, what: what != "tk")
    monkeypatch.setattr(inst, "venv_python", lambda: Path("/nonexistent/python"))
    state = {"extras": ["all"]}
    assert inst.check_system({"plot"}, "uv", state) == []
    assert state["managed_python"] is True and state["rebuild"] is False


def test_doctor_requires_only_the_chosen_extras(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [
        doctor.Check("base", "", lambda: doctor.Result("ok", "1.0")),
        doctor.Check("gi", "aravis", lambda: doctor.Result("missing", "No module named 'gi'", "apt ...")),
        doctor.Check("solver", None, lambda: doctor.Result("missing", "none")),
        doctor.Check("broken", None, lambda: 1 / 0),
    ])
    lines = []
    assert doctor.run_checks({"camera"}, out=lines.append) == 0
    assert "not installed (extra 'aravis')" in lines[1] and "check failed" in lines[-1]
    lines.clear()
    assert doctor.run_checks({"aravis"}, out=lines.append) == 1
    assert lines[1].split()[0] == "MISSING" and "fix: apt ..." in lines[2]
    assert doctor.chosen_extras("all") == ({"all", "adv", "ravf", "catalog", "plot"}, "--extras")
    assert doctor.chosen_extras("none") == (set(), "--extras")


def test_doctor_runs_as_a_file():
    r = subprocess.run([sys.executable, str(ROOT / "src" / "pystarlight" / "doctor.py"), "--extras", "none"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "numpy" in r.stdout


def test_cli_has_doctor():
    assert cli.TOOLS["doctor"][1] == "pystarlight.doctor"
    assert cli.ALIASES["check"] == "doctor"
