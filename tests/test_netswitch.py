"""scripts/netswitch.py (the Pi's home WiFi / hotspot switch): the nmcli calls, without nmcli."""

import importlib.util
import shutil
import subprocess
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
    PROFILES = {"31A138_5G": ("yes", 0, 1795057086, "31A138_5G"),          # used last
                "31A138_2GEX1": ("yes", 0, 1795050000, "31A138_2GEX1"),
                "Cafe\\:Net": ("yes", 20, 0, "Cafe:Net"), "PiHotspot": ("no", 0, 1795057048, "PyStarLight")}

    def __init__(self, active, dnsmasq="inactive", seen=("31A138_2GEX1", "31A138_5G", "Neighbour")):
        self.active, self.calls, self.dnsmasq, self.seen = list(active), [], dnsmasq, seen

    def __call__(self, cmd, capture_output=True, text=True):
        if cmd[0] == "systemctl":                      # the dnsmasq check
            return SimpleNamespace(returncode=3, stdout=self.dnsmasq + "\n", stderr="")
        args = cmd[1:]
        self.calls.append(args)
        out = ""
        if args[:4] == ["-t", "-f", "NAME", "connection"]:
            out = "\n".join(self.active)
        elif args[:3] == ["-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,TIMESTAMP"]:
            out = "\n".join(f"{n}:802-11-wireless:{a}:{p}:{t}" for n, (a, p, t, _) in self.PROFILES.items())
            out += "\nWired connection 1:802-3-ethernet:yes:-999:0"
        elif args[:2] == ["-g", "802-11-wireless.ssid"]:
            out = self.PROFILES[args[-1].replace(":", "\\:")][3]
        elif args[:4] == ["-t", "-f", "SSID", "device"]:
            out = "\n".join(self.seen)
        if out:
            return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
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
    ups = [c for c in nm.calls if c[:2] == ["connection", "up"]]
    assert ["connection", "down", ns.PROFILE] in nm.calls
    assert ups[-1] == ["connection", "up", "31A138_5G"]        # by name: never the hotspot again
    assert not any(c[:2] == ["device", "connect"] for c in nm.calls)


def test_home_takes_the_best_known_network_in_range(ns):
    nm = FakeNM([], seen=("31A138_2GEX1",))
    assert ns.home_networks(run=nm)[0] == ("Cafe:Net", "Cafe:Net")       # escaped ':' kept
    assert "PiHotspot" not in [n for n, _ in ns.home_networks(run=nm)]
    nm.active = [ns.PROFILE]
    ns.set_mode("home", run=nm, log=lambda m: None)
    assert [c for c in nm.calls if c[:2] == ["connection", "up"]] == [["connection", "up", "31A138_2GEX1"]]
    nm = FakeNM([ns.PROFILE], seen=())                                   # nothing known in range
    logs = []
    ns.set_mode("home", run=nm, log=logs.append)
    assert not any(c[:2] == ["connection", "up"] for c in nm.calls) and "keeps looking" in logs[-1]


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


def test_cert_names_every_way_in(ns, tmp_path):
    names = ns.cert_names("raspberrypi", ["192.168.0.178", "fe80::1", "10.42.0.1", "172.17.0.1"])
    assert names == ["DNS:raspberrypi", "DNS:raspberrypi.local", "DNS:localhost", "IP:10.42.0.1",
                     "IP:192.168.0.178", "IP:172.17.0.1"]
    cmd = ns.cert_command("raspberrypi", names, str(tmp_path / "k.pem"), str(tmp_path / "c.pem"))
    if shutil.which("openssl") is None:
        pytest.skip("no openssl")
    subprocess.run(cmd, check=True, capture_output=True)
    text = subprocess.run(["openssl", "x509", "-in", str(tmp_path / "c.pem"), "-noout", "-text"],
                          capture_output=True, text=True).stdout
    assert "IP Address:10.42.0.1" in text and "IP Address:192.168.0.178" in text
    assert "DNS:raspberrypi.local" in text and "CA:TRUE" in text
    assert "secp384r1" in text or "P-384" in text
