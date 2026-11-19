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
    python3 scripts/netswitch.py status

With the hotspot up, join its network (default PyStarLight) and point a VNC viewer (Remmina,
TigerVNC, bVNC) at 10.42.0.1. No switch wired: the pin reads open (pull-up), home WiFi.

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


def set_mode(mode, run=subprocess.run, log=print):
    """'hotspot': bring the hotspot up; 'home': take it down, NetworkManager joins the known WiFi."""
    up = PROFILE in active(run)
    if mode == "hotspot" and not up:
        log(f"hotspot: {PROFILE} up (VNC: {HOTSPOT_ADDRESS})")
        nmcli("connection", "up", PROFILE, run=run)
    elif mode == "home" and up:
        log("home WiFi: hotspot down")
        nmcli("connection", "down", PROFILE, run=run)
        try:                                        # don't wait for the next automatic scan
            nmcli("device", "connect", "wlan0", run=run)
        except RuntimeError as e:
            log(f"no known WiFi joined yet ({e}); NetworkManager keeps trying")


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
            set_mode(mode)
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
    a = ap.parse_args(argv)
    if a.cmd == "setup":
        setup(a.ssid, a.password, a.band)
    elif a.cmd == "watch":
        watch(a.pin, None if a.led < 0 else a.led)
    elif a.cmd in ("hotspot", "home"):
        set_mode(a.cmd)
    elif a.cmd == "install":
        install()
    elif a.cmd == "pin":
        closed = Pins(SWITCH_PIN, None).closed()
        print(f"GPIO{SWITCH_PIN}: " + ("closed (hotspot)" if closed else "open (home WiFi)"))
    else:
        status()


if __name__ == "__main__":
    main()
