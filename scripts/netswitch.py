#!/usr/bin/env python3
"""Network switch for the Raspberry Pi in the field: home WiFi or the Pi's own hotspot, by a toggle
switch on a GPIO pin (no display needed), or by hand.

    switch: GPIO26 (header pin 37) to GND (pin 39)   closed: hotspot, open: home WiFi
    LED (optional): GPIO20 (pin 38) -> 330 ohm -> LED -> GND (pin 34)   on: hotspot up
    (the far end of the header: a camera / GPS connector on the first pins stays clear)

    sudo python3 scripts/netswitch.py setup --password SECRET   the hotspot profile, once
    sudo python3 scripts/netswitch.py install                    start with the Pi (systemd)
    sudo python3 scripts/netswitch.py hotspot | home             by hand (the switch wins again
                                                                 when it is flipped)
    python3 scripts/netswitch.py pin                             the switch as read now
    sudo python3 scripts/netswitch.py cert [--ip A.B.C.D]        VNC certificate for both networks
    python3 scripts/netswitch.py status

With the hotspot up, join its network (default PyStarLight) and point a VNC viewer (Remmina,
TigerVNC, bVNC) at 10.42.0.1. No switch wired: the pin reads open (pull-up), home WiFi.

wayvnc's own certificate names only the host (raspberrypi, raspberrypi.local): a viewer
connecting by address warns. `cert` makes one for every way in: the host names, the hotspot
address and the Pi's addresses now (plus --ip, e.g. a reserved home address), installs it for
wayvnc (the old one kept as .bak), restarts wayvnc, and leaves the public certificate in
~/wayvnc-cert.pem for the viewers (their "CA certificate"; copy it again after each `cert`).

NEW CODE (not part of the package). Standard library, NetworkManager's nmcli, and libgpiod's
Python module for the switch (python3-libgpiod, v1 on Raspberry Pi OS 12, v2 later; the header's
chip is found by its label: gpiozero's lgpio looked for the wrong chip on a Pi 5). The hotspot is
an ordinary NetworkManager profile (PROFILE) that never comes up by itself; leaving it,
NetworkManager joins the known WiFi.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys

PROFILE = "PiHotspot"
SERVICE = "pystarlight-netswitch"
SWITCH_PIN, LED_PIN = 26, 20
HOTSPOT_ADDRESS = "10.42.0.1"          # NetworkManager's "shared" network
DEBOUNCE_S = 0.3
HEADER_CHIPS = ("pinctrl-rp1", "pinctrl-bcm2711", "pinctrl-bcm2835")     # Pi 5, Pi 4, older


def nmcli(*args, run=subprocess.run):
    """nmcli's output; RuntimeError with its message on failure."""
    r = run(["nmcli", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"nmcli {' '.join(args)} failed")
    return r.stdout


def fields(line):
    """nmcli -t columns: split at ':' but not at an escaped one ('\\:')."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur, esc = cur + ch, False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out, cur = out + [cur], ""
        else:
            cur += ch
    return out + [cur]


def active(run=subprocess.run):
    """Names of the active connections."""
    return [line for line in nmcli("-t", "-f", "NAME", "connection", "show", "--active", run=run).splitlines()
            if line]


def setup_args(ssid, password, band="bg", ifname="wlan0"):
    """nmcli arguments that create the hotspot profile (never up by itself)."""
    return ["connection", "add", "type", "wifi", "ifname", ifname, "con-name", PROFILE, "autoconnect", "no",
            "ssid", ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
            "ipv4.method", "shared", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]


def setup(ssid, password, band="bg", run=subprocess.run):
    if len(password) < 8:
        raise SystemExit("the hotspot password needs at least 8 characters")
    names = nmcli("-t", "-f", "NAME", "connection", "show", run=run).splitlines()
    if PROFILE in names:
        nmcli("connection", "delete", PROFILE, run=run)
    nmcli(*setup_args(ssid, password, band), run=run)
    print(f"hotspot profile {PROFILE}: network {ssid}, {'5 GHz' if band == 'a' else '2.4 GHz'}")


def dns_blocker(run=subprocess.run):
    """'' or why the hotspot cannot hand out addresses: a system dnsmasq service already holds
    the DNS / DHCP ports on every address (NetworkManager's own dnsmasq then fails, 'Address
    already in use', and the hotspot is dropped after 30 s)."""
    r = run(["systemctl", "is-active", "dnsmasq"], capture_output=True, text=True)
    if r.stdout.strip() == "active":
        return ("a system dnsmasq service is running and blocks the hotspot's own (address in use): "
                "sudo systemctl disable --now dnsmasq")
    return ""


def home_networks(run=subprocess.run):
    """The saved WiFi networks to come home to: [(profile, ssid)], highest autoconnect priority
    first, then the one used last; not the hotspot, only those that may connect by themselves."""
    rows = []
    for line in nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,TIMESTAMP", "connection",
                      "show", run=run).splitlines():
        f = fields(line)
        if len(f) == 5 and f[1] == "802-11-wireless" and f[2] == "yes" and f[0] != PROFILE:
            ssid = nmcli("-g", "802-11-wireless.ssid", "connection", "show", f[0], run=run).strip()
            rows.append((-int(f[3] or 0), -int(f[4] or 0), f[0], ssid))
    return [(name, ssid) for _, _, name, ssid in sorted(rows)]


def in_range(run=subprocess.run):
    """SSIDs the WiFi sees now (a fresh scan)."""
    return {fields(line)[0] for line in nmcli("-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "yes",
                                               run=run).splitlines() if line}


def set_mode(mode, run=subprocess.run, log=print):
    """'hotspot': bring the hotspot up; 'home': take it down, NetworkManager joins the known WiFi."""
    up = PROFILE in active(run)
    if mode == "hotspot" and not up:
        blocker = dns_blocker(run)
        if blocker:
            raise RuntimeError(blocker)
        log(f"hotspot: {PROFILE} up (VNC: {HOTSPOT_ADDRESS})")
        nmcli("connection", "up", PROFILE, run=run)
    elif mode == "home" and up:
        log("home WiFi: hotspot down")
        nmcli("connection", "down", PROFILE, run=run)
        # a known network by name: 'nmcli device connect wlan0' took the hotspot again (it is
        # always "available"); none in range: NetworkManager's own autoconnect, never the hotspot
        try:
            seen = in_range(run)
        except RuntimeError:
            seen = set()
        for name, ssid in home_networks(run):
            if ssid in seen:
                try:
                    nmcli("connection", "up", name, run=run)
                    log(f"home WiFi: {name}")
                    return
                except RuntimeError as e:
                    log(f"{name}: {e}")
        log("no known WiFi in range yet; NetworkManager keeps looking")


def header_chip():
    """Path of the GPIO chip that carries the 40-pin header (by its label)."""
    import gpiod
    for path in sorted(glob.glob("/dev/gpiochip*")):
        try:
            chip = gpiod.Chip(path)
            label = chip.label() if callable(getattr(chip, "label", None)) else chip.get_info().label
            chip.close()
        except OSError:
            continue
        if label in HEADER_CHIPS:
            return path
    raise RuntimeError(f"no GPIO chip labelled {' / '.join(HEADER_CHIPS)}")


class Pins:
    """The switch (input, pull-up, both edges) and the LED (output) through libgpiod v1 or v2."""

    def __init__(self, pin, led_pin):
        import gpiod
        self.gpiod, self.pin, self.led_pin = gpiod, pin, led_pin
        path = header_chip()
        if hasattr(gpiod, "request_lines"):                     # v2
            from gpiod.line import Bias, Direction, Edge, Value
            self.active = Value.ACTIVE
            cfg = {pin: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH,
                                           bias=Bias.PULL_UP)}
            if led_pin is not None:
                cfg[led_pin] = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
            self.req = gpiod.request_lines(path, consumer="netswitch", config=cfg)
            self.v2 = True
        else:                                                   # v1 (Raspberry Pi OS 12)
            chip = gpiod.Chip(path)
            self.line = chip.get_line(pin)
            self.line.request(consumer="netswitch", type=gpiod.LINE_REQ_EV_BOTH_EDGES,
                              flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)
            self.led = None
            if led_pin is not None:
                self.led = chip.get_line(led_pin)
                self.led.request(consumer="netswitch", type=gpiod.LINE_REQ_DIR_OUT, default_val=0)
            self.v2 = False

    def closed(self):
        """The switch connects the pin to GND."""
        if self.v2:
            return self.req.get_value(self.pin) != self.active
        return self.line.get_value() == 0

    def wait(self, timeout):
        """True when an edge came within timeout s (the events are consumed)."""
        if self.v2:
            if self.req.wait_edge_events(timeout):
                self.req.read_edge_events()
                return True
            return False
        if self.line.event_wait(sec=int(timeout), nsec=int((timeout % 1) * 1e9)):
            self.line.event_read()
            return True
        return False

    def set_led(self, on):
        if self.led_pin is None:
            return
        if self.v2:
            from gpiod.line import Value
            self.req.set_value(self.led_pin, Value.ACTIVE if on else Value.INACTIVE)
        else:
            self.led.set_value(1 if on else 0)


def watch(pin=SWITCH_PIN, led_pin=LED_PIN):
    """Follow the switch: its state at start, then every flip (settled for DEBOUNCE_S); the LED
    shows the hotspot, checked every 10 s too (a hotspot started by hand)."""
    pins = Pins(pin, led_pin)

    def apply():
        mode = "hotspot" if pins.closed() else "home"
        try:
            set_mode(mode, log=lambda m: print(m, flush=True))     # straight into the service's log
        except RuntimeError as e:
            print(f"{mode}: {e}", flush=True)

    apply()
    print(f"watching GPIO{pin}: closed = hotspot, open = home WiFi", flush=True)
    while True:
        if pins.wait(10.0):
            while pins.wait(DEBOUNCE_S):            # contact bounce: until it settles
                pass
            apply()
        try:
            pins.set_led(PROFILE in active())
        except RuntimeError:
            pass


WAYVNC_DIR = "/etc/wayvnc"


def cert_names(host, addresses):
    """subjectAltName entries: the host, host.local, localhost, the hotspot address and the others."""
    ips = []
    for a in [HOTSPOT_ADDRESS, *addresses]:
        if a and ":" not in a and a not in ips:               # IPv4 only: what viewers are given
            ips.append(a)
    return [f"DNS:{host}", f"DNS:{host}.local", "DNS:localhost"] + [f"IP:{a}" for a in ips]


def cert_command(host, names, key, crt, days=3650):
    """openssl: a self-signed certificate (its own CA, as wayvnc's) on an EC P-384 key."""
    return ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:secp384r1",
            "-nodes", "-days", str(days), "-subj", f"/CN={host}", "-keyout", key, "-out", crt,
            "-addext", "subjectAltName=" + ",".join(names),
            "-addext", "basicConstraints=critical,CA:TRUE"]


def make_cert(extra_ips=()):
    import socket
    import tempfile
    host = socket.gethostname()
    here = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    names = cert_names(host, [*here, *extra_ips])
    with tempfile.TemporaryDirectory() as tmp:
        key, crt = os.path.join(tmp, "tls_key.pem"), os.path.join(tmp, "tls_cert.pem")
        subprocess.run(cert_command(host, names, key, crt), check=True, capture_output=True)
        for name, src in (("tls_key.pem", key), ("tls_cert.pem", crt)):
            dst = os.path.join(WAYVNC_DIR, name)
            if os.path.exists(dst):
                shutil.copy2(dst, dst + ".bak")
            shutil.copyfile(src, dst)
            shutil.chown(dst, "root", "vnc")
            os.chmod(dst, 0o640)
        user = os.environ.get("SUDO_USER") or "root"
        public = os.path.join(os.path.expanduser(f"~{user}"), "wayvnc-cert.pem")
        shutil.copyfile(crt, public)
        shutil.chown(public, user, user)
        os.chmod(public, 0o644)
    subprocess.run(["systemctl", "restart", "wayvnc"], check=False)
    print("VNC certificate for " + ", ".join(n.split(":", 1)[1] for n in names))
    print(f"wayvnc restarted (VNC sessions reconnect). For the viewers: {public}\n"
          f"  (Remmina / TigerVNC: 'CA certificate'; the old ones are in {WAYVNC_DIR}/*.bak)")


def unit(script, python=sys.executable):
    return f"""[Unit]
Description=PyStarLight network switch (GPIO{SWITCH_PIN}: home WiFi / hotspot)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
ExecStart={python} {script} watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install():
    path = f"/etc/systemd/system/{SERVICE}.service"
    with open(path, "w") as f:
        f.write(unit(os.path.abspath(__file__)))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", SERVICE], check=True)
    print(f"{path} written, {SERVICE} enabled and started (log: journalctl -u {SERVICE})")


def status():
    names = active()
    print("hotspot up" if PROFILE in names else "hotspot down",
          "| active:", ", ".join(n for n in names if n != "lo") or "none")
    if shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True)
        print(f"{SERVICE}: {r.stdout.strip()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="create the hotspot profile")
    s.add_argument("--ssid", default="PyStarLight")
    s.add_argument("--password", required=True)
    s.add_argument("--band", choices=("bg", "a"), default="bg", help="bg: 2.4 GHz (default), a: 5 GHz")
    w = sub.add_parser("watch", help="follow the switch (what the service runs)")
    w.add_argument("--pin", type=int, default=SWITCH_PIN)
    w.add_argument("--led", type=int, default=LED_PIN, help="-1: no LED")
    for name in ("hotspot", "home", "install", "status", "pin"):
        sub.add_parser(name)
    c = sub.add_parser("cert", help="a VNC certificate for the host names, the hotspot and the addresses")
    c.add_argument("--ip", action="append", default=[], help="another address to name (repeatable)")
    a = ap.parse_args(argv)
    if a.cmd == "setup":
        setup(a.ssid, a.password, a.band)
    elif a.cmd == "watch":
        watch(a.pin, None if a.led < 0 else a.led)
    elif a.cmd in ("hotspot", "home"):
        try:
            set_mode(a.cmd)
        except RuntimeError as e:
            raise SystemExit(f"{a.cmd}: {e}") from None
    elif a.cmd == "install":
        install()
    elif a.cmd == "cert":
        make_cert(a.ip)
    elif a.cmd == "pin":
        try:
            closed = Pins(SWITCH_PIN, None).closed()
        except OSError:                      # the service holds the line: the Pi's pinctrl reads it
            out = subprocess.run(["pinctrl", "get", str(SWITCH_PIN)], capture_output=True, text=True).stdout
            closed = "| lo" in out
        print(f"GPIO{SWITCH_PIN}: " + ("closed (hotspot)" if closed else "open (home WiFi)"))
    else:
        status()


if __name__ == "__main__":
    main()
