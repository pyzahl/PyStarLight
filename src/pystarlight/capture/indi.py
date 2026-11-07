"""A small INDI client (the XML protocol over TCP, port 7624) and the INDI server it may start.

NEW CODE, standard library only (like the rest of capture/: runs with the Pi's system Python, no
PyIndi to build). INDI: https://www.indilib.org/develop/developer-manual/106-client-development.html

    client = Client().connect()                 # reads the server's properties in a thread
    client.wait_device("Telescope Simulator")
    client.set_switch(dev, "CONNECTION", {"CONNECT": True})
    client.number(dev, "EQUATORIAL_EOD_COORD")  # {'RA': 5.6, 'DEC': 22.0}

The stream is a sequence of elements without a document root: def*Vector (a property is
defined, with its elements), set*Vector (new values, state), delProperty, message. Each
property keeps its state (Idle / Ok / Busy / Alert) and values; callbacks run in the reader
thread (keep them short; the GUI polls instead). BLOBs are switched off (enableBLOB Never).

Server: Server(drivers).start() runs `indiserver -p PORT drivers...` (ASTRID does the same) and
stop() ends it. drivers_installed() lists the drivers INDI's XML files name, by group
("Telescopes", "Focusers", ...).
"""
import glob
import os
import shutil
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

PORT = 7624
DRIVER_DIR = "/usr/share/indi"
STATES = ("Idle", "Ok", "Busy", "Alert")
TELESCOPE, FOCUSER = 1 << 0, 1 << 3      # INDI's DRIVER_INTERFACE bits


def number(text):
    """An INDI number: decimal, or sexagesimal 'd:m:s' / 'd m s' (the sign on the whole)."""
    s = (text or "").strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.replace(":", " ").split()
    if not parts:
        return float("nan")
    neg = parts[0].startswith("-")
    v = 0.0
    for i, p in enumerate(parts[:3]):
        v += abs(float(p)) / 60 ** i
    return -v if neg else v


class Property:
    """One property vector: kind (Number / Switch / Text / Light / BLOB), state, values."""

    def __init__(self, device, name, kind, attrs):
        self.device, self.name, self.kind = device, name, kind
        self.label = attrs.get("label", name)
        self.group = attrs.get("group", "")
        self.perm = attrs.get("perm", "ro")
        self.rule = attrs.get("rule", "")
        self.state = attrs.get("state", "Idle")
        self.values = {}                # element name -> float (Number), bool (Switch), str
        self.labels = {}
        self.updated = time.monotonic()

    def parse(self, elem, define):
        if "state" in elem.attrib:
            self.state = elem.attrib["state"]
        for child in elem:
            name = child.attrib.get("name")
            if name is None:
                continue
            if define:
                self.labels[name] = child.attrib.get("label", name)
            text = (child.text or "").strip()
            if self.kind == "Number":
                self.values[name] = number(text)
            elif self.kind == "Switch":
                self.values[name] = text == "On"
            elif self.kind != "BLOB":
                self.values[name] = text
        self.updated = time.monotonic()

    def __repr__(self):
        return f"<{self.kind} {self.device}.{self.name} {self.state} {self.values}>"


class Client:
    """Connection to an INDI server; properties[device][name] -> Property."""

    def __init__(self, host="localhost", port=PORT):
        self.host, self.port = host, port
        self.sock = None
        self.properties = {}
        self.messages = []              # (device, text), the newest last (at most 200 kept)
        self.lock = threading.RLock()             # wait() predicates read under it
        self.changed = threading.Condition(self.lock)
        self.callbacks = []             # f(event, device, name): 'def', 'set', 'del', 'message'
        self.reader = None
        self.connected = False
        self.error = None

    # ---- connection ----
    def connect(self, timeout=5.0):
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(None)
        self.connected, self.error = True, None
        self.reader = threading.Thread(target=self._read, name="indi-client", daemon=True)
        self.reader.start()
        self.send('<getProperties version="1.7"/>')
        return self

    def close(self):
        self.connected = False
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=2.0)

    def send(self, xml):
        if self.sock is None:
            raise ConnectionError("not connected to an INDI server")
        self.sock.sendall(xml.encode("utf-8") + b"\n")

    def _read(self):
        parser = ET.XMLPullParser(events=("start", "end"))
        parser.feed("<indi>")
        depth, root = 0, None
        try:
            while self.connected:
                data = self.sock.recv(65536)
                if not data:
                    break
                parser.feed(data)
                for event, elem in parser.read_events():
                    if event == "start":
                        if root is None:
                            root = elem
                        depth += 1
                        continue
                    depth -= 1
                    if depth == 1:          # a complete top-level element
                        self._handle(elem)
                        root.remove(elem)
        except (OSError, ET.ParseError) as e:
            self.error = str(e)
        finally:
            self.connected = False
            with self.changed:
                self.changed.notify_all()

    def _handle(self, elem):
        tag = elem.tag
        device, name = elem.attrib.get("device", ""), elem.attrib.get("name", "")
        event = None
        with self.changed:
            if tag.startswith("def") and tag.endswith("Vector"):
                kind = tag[3:-6]
                prop = Property(device, name, kind, elem.attrib)
                prop.parse(elem, define=True)
                self.properties.setdefault(device, {})[name] = prop
                event = "def"
                if kind == "BLOB":
                    self._send_later(f"<enableBLOB device={quoteattr(device)}>Never</enableBLOB>")
            elif tag.startswith("set") and tag.endswith("Vector"):
                prop = self.properties.get(device, {}).get(name)
                if prop is not None:
                    prop.parse(elem, define=False)
                    event = "set"
            elif tag == "delProperty":
                if name:
                    self.properties.get(device, {}).pop(name, None)
                else:
                    self.properties.pop(device, None)
                event = "del"
            if elem.attrib.get("message"):
                self.messages.append((device, elem.attrib["message"]))
                del self.messages[:-200]
                event = event or "message"
            self.changed.notify_all()
        if event:
            for cb in list(self.callbacks):
                try:
                    cb(event, device, name)
                except Exception as e:      # a callback must not stop the reader
                    print(f"INDI callback failed: {e!r}")

    def _send_later(self, xml):
        threading.Thread(target=lambda: self._try_send(xml), daemon=True).start()

    def _try_send(self, xml):
        try:
            self.send(xml)
        except OSError:
            pass

    # ---- reading ----
    def devices(self):
        with self.lock:
            return list(self.properties)

    def get(self, device, name):
        with self.lock:
            return self.properties.get(device, {}).get(name)

    def values(self, device, name):
        prop = self.get(device, name)
        return dict(prop.values) if prop is not None else None

    number = text = switch = values

    def state(self, device, name):
        prop = self.get(device, name)
        return prop.state if prop is not None else None

    def wait(self, predicate, timeout=10.0):
        """Wait until predicate() is true (checked whenever something arrives); True if it was."""
        end = time.monotonic() + timeout
        with self.changed:
            while True:
                try:
                    if predicate():
                        return True
                except Exception:
                    pass
                left = end - time.monotonic()
                if left <= 0 or not self.connected:
                    return False
                self.changed.wait(min(left, 0.5))

    def wait_property(self, device, name, timeout=10.0):
        return self.wait(lambda: name in self.properties.get(device, {}), timeout)

    def interfaces(self, device):
        """The driver's interface bits (DRIVER_INFO.DRIVER_INTERFACE: TELESCOPE, FOCUSER, ...)."""
        prop = self.properties.get(device, {}).get("DRIVER_INFO")
        info = prop.values if prop is not None else {}
        try:
            return int(float(info.get("DRIVER_INTERFACE", "0")))
        except ValueError:
            return 0

    def find_device(self, interface, timeout=10.0):
        """The first device whose driver offers `interface` (TELESCOPE, FOCUSER: INDI's bits;
        known before the device is connected, unlike most of its properties)."""
        found = []

        def look():
            for dev in self.properties:
                if self.interfaces(dev) & interface:
                    found.append(dev)
                    return True
            return False
        self.wait(look, timeout)
        return found[0] if found else None

    # ---- writing ----
    def _new(self, kind, device, name, values, fmt):
        one = "".join(f"<one{kind} name={quoteattr(k)}>{escape(fmt(v))}</one{kind}>"
                      for k, v in values.items())
        self.send(f"<new{kind}Vector device={quoteattr(device)} name={quoteattr(name)}>"
                  f"{one}</new{kind}Vector>")

    def set_number(self, device, name, values):
        self._new("Number", device, name, values, lambda v: repr(float(v)))

    def set_switch(self, device, name, values):
        """values: {element: True/False}; for a one-of-many switch give just the one to turn on."""
        self._new("Switch", device, name, values, lambda v: "On" if v else "Off")

    def set_text(self, device, name, values):
        self._new("Text", device, name, values, str)

    def connect_device(self, device, timeout=20.0):
        """Switch the driver's CONNECTION on and wait for it (True: connected)."""
        if not self.wait_property(device, "CONNECTION", timeout):
            return False
        if (self.switch(device, "CONNECTION") or {}).get("CONNECT"):
            return True
        self.set_switch(device, "CONNECTION", {"CONNECT": True})
        return self.wait(lambda: (self.switch(device, "CONNECTION") or {}).get("CONNECT")
                         and self.state(device, "CONNECTION") != "Busy", timeout)


# ---------------------------------------------------------------------------
# The server and the installed drivers
# ---------------------------------------------------------------------------
def drivers_installed(directory=DRIVER_DIR):
    """{group: [(label, executable), ...]} from INDI's driver lists (drivers.xml and the 3rd-party
    driver packages' XML files); only drivers whose executable is installed."""
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.xml"))):
        if path.endswith("_sk.xml"):
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for group in root.iter("devGroup"):
            for dev in group.iter("device"):
                drv = dev.find("driver")
                if drv is None or not (drv.text or "").strip():
                    continue
                exe = drv.text.strip()
                if shutil.which(exe) is None:
                    continue
                entry = (dev.attrib.get("label", exe), exe)
                lst = out.setdefault(group.attrib.get("group", ""), [])
                if entry not in lst:
                    lst.append(entry)
    return out


class Server:
    """`indiserver` with the given drivers, started by us (and stopped again)."""

    def __init__(self, drivers, port=PORT):
        self.drivers = [d for d in drivers if d]
        self.port = port
        self.proc = None

    def start(self, wait_s=5.0):
        exe = shutil.which("indiserver")
        if exe is None:
            raise RuntimeError("indiserver not found (Debian / Raspberry Pi OS: sudo apt install indi-bin)")
        # INDI 1.9 also listens on a local socket of a fixed name (-u, default /tmp/indiserver): a
        # second server on the machine, even on another port, needs its own or it ends at once
        local = [] if self.port == PORT else ["-u", f"/tmp/indiserver-{self.port}"]
        self.proc = subprocess.Popen([exe, "-p", str(self.port), "-m", "100", *local, *self.drivers],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace").strip()
                raise RuntimeError(f"indiserver ended: {err[-300:]}")
            try:
                socket.create_connection(("localhost", self.port), timeout=0.5).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"indiserver not answering on port {self.port}")

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
