"""scripts/netswitch.py (the Pi's home WiFi / hotspot switch): the nmcli calls, without nmcli."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ns():
    spec = importlib.util.spec_from_file_location("netswitch", ROOT / "scripts" / "netswitch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeNM:
    def __init__(self, active, dnsmasq="inactive"):
        self.active, self.calls, self.dnsmasq = list(active), [], dnsmasq

    def __call__(self, cmd, capture_output=True, text=True):
        if cmd[0] == "systemctl":                      # the dnsmasq check
            return SimpleNamespace(returncode=3, stdout=self.dnsmasq + "\n", stderr="")
        args = cmd[1:]
        self.calls.append(args)
        if args[:4] == ["-t", "-f", "NAME", "connection"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(self.active) + "\n", stderr="")
        if args[:2] == ["connection", "up"]:
            self.active.append(args[2])
        elif args[:2] == ["connection", "down"]:
            self.active.remove(args[2])
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_switching_brings_the_hotspot_up_and_down(ns):
    nm = FakeNM(["31A138_5G", "lo"])
    ns.set_mode("hotspot", run=nm, log=lambda m: None)
    assert ["connection", "up", ns.PROFILE] in nm.calls and ns.PROFILE in nm.active
    n = len(nm.calls)
    ns.set_mode("hotspot", run=nm, log=lambda m: None)          # already up: nothing more
    assert [c for c in nm.calls[n:] if c[0] != "-t"] == []
    ns.set_mode("home", run=nm, log=lambda m: None)
    assert ["connection", "down", ns.PROFILE] in nm.calls and ["device", "connect", "wlan0"] in nm.calls


def test_setup_profile_never_comes_up_by_itself(ns):
    args = ns.setup_args("PyStarLight", "secret123", band="a")
    i = args.index("autoconnect")
    assert args[i + 1] == "no" and "ap" in args and "shared" in args
    assert args[args.index("802-11-wireless.band") + 1] == "a"
    with pytest.raises(SystemExit):
        ns.setup("PyStarLight", "short", run=FakeNM([]))


def test_unit_runs_this_script(ns):
    text = ns.unit("/home/percy/PyStarLight/scripts/netswitch.py", "/usr/bin/python3")
    assert "ExecStart=/usr/bin/python3 /home/percy/PyStarLight/scripts/netswitch.py watch" in text


def test_a_system_dnsmasq_is_named_not_waited_for(ns):
    nm = FakeNM(["31A138_5G"], dnsmasq="active")
    with pytest.raises(RuntimeError, match="systemctl disable --now dnsmasq"):
        ns.set_mode("hotspot", run=nm, log=lambda m: None)
    assert ["connection", "up", ns.PROFILE] not in nm.calls        # home WiFi kept
