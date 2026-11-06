"""The `pystarlight` front door: subcommands, aliases, a bare file goes to analyze."""

import pytest

from pystarlight import cli


@pytest.fixture
def ran(monkeypatch):
    calls = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def main(self, argv):
            calls.append((self.name, list(argv)))
            return 0
    import importlib
    monkeypatch.setattr(importlib, "import_module", lambda mod: Fake(mod))
    return calls


@pytest.mark.parametrize("argv, module, rest", [
    (["analyze", "m.ravf"], "pystarlight.viewer", ["m.ravf"]),
    (["analyse", "m.ravf"], "pystarlight.viewer", ["m.ravf"]),
    (["view"], "pystarlight.viewer", []),
    (["player", "m.ser", "--focal-mm", "375"], "pystarlight.capture.player", ["m.ser", "--focal-mm", "375"]),
    (["review", "m.ser"], "pystarlight.capture.player", ["m.ser"]),
    (["camera", "--list"], "pystarlight.capture.app", ["--list"]),
    (["config", "path"], "pystarlight.config", ["path"]),
    ([], "pystarlight.viewer", []),                                   # a file dialog
    (["--config", "c.toml"], "pystarlight.viewer", ["--config", "c.toml"]),
])
def test_commands(ran, argv, module, rest):
    assert cli.main(argv) == 0 and ran == [(module, rest)]


def test_a_file_goes_to_analyze(ran, tmp_path):
    night = tmp_path / "night1"                                       # a FITS folder without a dot
    night.mkdir()
    assert cli.main([str(night)]) == 0 and ran == [("pystarlight.viewer", [str(night)])]
    assert cli.main(["x.ravf"]) == 0 and ran[-1] == ("pystarlight.viewer", ["x.ravf"])


def test_help_version_and_unknown(capsys, ran):
    assert cli.main(["--help"]) == 0 and "analyze" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0 and "PyStarLight" in capsys.readouterr().out
    assert cli.main(["frobnicate"]) == 2 and "unknown command" in capsys.readouterr().err
    assert ran == []
