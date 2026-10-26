"""u-blox GPS (NEO-M8N etc.) on a serial port: NMEA status for the GUI, and the timepulse
(TIMEPULSE / PPS pin) rate, which triggers the camera.

The timepulse is set with UBX-CFG-TP5: pulses only while the GPS has a fix (so every
pulse is a true UTC instant), aligned to the UTC second, rising edge first. Rates that
are whole Hz are set as a frequency (30 Hz: pulses at k/30 s exactly), others as a
period in us (0.5 Hz: every even second). Settings are not saved in the GPS (no
CFG-CFG): a power cycle brings back its default 1 Hz PPS.

Optionally reads the compass that M8N modules often carry (QMC5883L or HMC5883L on the
module's I2C wires, if they are connected to the Pi's I2C bus 1). The heading is
magnetic and uncalibrated (no hard/soft-iron correction, no tilt compensation).
"""
import fcntl
import math
import os
import struct
import threading
import time
from datetime import datetime, timezone

import serial

UBX_SYNC = b"\xb5\x62"
CFG_TP5 = (0x06, 0x31)
ACK = 0x05

# TP5 flags
TP5_ACTIVE = 0x01
TP5_LOCK_GNSS_FREQ = 0x02
TP5_LOCKED_OTHER_SET = 0x04
TP5_IS_FREQ = 0x08
TP5_IS_LENGTH = 0x10
TP5_ALIGN_TO_TOW = 0x20
TP5_POLARITY_RISING = 0x40

FIX_NAMES = {1: "no fix", 2: "2D", 3: "3D"}
GGA_QUALITY = {0: "invalid", 1: "GPS", 2: "DGPS", 4: "RTK", 5: "RTK float", 6: "dead reckoning"}


def ubx_frame(cls, mid, payload=b""):
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return UBX_SYNC + body + bytes([a, b])


def nmea_ok(line):
    """True if an NMEA sentence ($...*hh) has a valid checksum."""
    if not line.startswith("$") or len(line) < 4 or line[-3] != "*":
        return False
    c = 0
    for ch in line[1:-3]:
        c ^= ord(ch)
    try:
        return c == int(line[-2:], 16)
    except ValueError:
        return False


def nmea_deg(value, hemi):
    """'4057.31649', 'N' -> 40.955275 (None if empty)."""
    if not value:
        return None
    d = float(value)
    deg = int(d // 100) + (d % 100) / 60.0
    return -deg if hemi in ("S", "W") else deg


class Gps(threading.Thread):
    """Reads the GPS serial port in the background; status fields are plain attributes."""

    def __init__(self, port="/dev/ttyAMA0", baud=9600):
        super().__init__(daemon=True)
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.port = port
        self.lock = threading.Lock()
        self.running = True
        # status (from NMEA)
        self.utc = None             # datetime of the last RMC (time of the last fix epoch)
        self.utc_rx = 0.0           # time.monotonic() when it arrived
        self.valid = False          # RMC status A
        self.fix = 1                # GSA: 1 none, 2 2D, 3 3D
        self.quality = 0            # GGA fix quality
        self.sats_used = 0
        self.sats_view = {}         # talker (GP, GL, GA, GB) -> satellites in view
        self.snr = {}               # (talker, prn) -> SNR dB of satellites in view
        self.hdop = self.pdop = self.vdop = None
        self.lat = self.lon = self.alt = None
        self.speed_kmh = None
        self.last_rx = 0.0          # time.monotonic() of the last good sentence
        # timepulse
        self.tp_rate = None         # Hz currently set by set_timepulse() (None: untouched)
        self._tp5 = None            # last polled CFG-TP5 payload
        self._acks = {}             # (cls, id) -> True (ACK) / False (NAK)
        self._buf = b""
        self.start()

    # ---- reader thread ----
    def run(self):
        while self.running:
            try:
                data = self.ser.read(256)
            except (serial.SerialException, OSError) as e:
                print(f"GPS read failed: {e}")
                time.sleep(1)
                continue
            if data:
                self._buf += data
                self._parse()

    def _parse(self):
        buf = self._buf
        while True:
            i_n, i_u = buf.find(b"$"), buf.find(UBX_SYNC)
            if i_n < 0 and i_u < 0:
                buf = buf[-1:]          # keep a possible half sync byte
                break
            if i_u >= 0 and (i_n < 0 or i_u < i_n):
                if len(buf) < i_u + 6:
                    buf = buf[i_u:]
                    break
                n = struct.unpack("<H", buf[i_u + 4:i_u + 6])[0]
                end = i_u + 8 + n
                if n > 1024:            # not really a UBX frame
                    buf = buf[i_u + 2:]
                    continue
                if len(buf) < end:
                    buf = buf[i_u:]
                    break
                frame = buf[i_u:end]
                if ubx_frame(frame[2], frame[3], frame[6:-2]) == frame:
                    self._on_ubx(frame[2], frame[3], frame[6:-2])
                buf = buf[end:]
            else:
                j = buf.find(b"\n", i_n)
                if j < 0:
                    buf = buf[i_n:] if len(buf) - i_n < 200 else b""
                    break
                line = buf[i_n:j].decode("ascii", "replace").strip()
                buf = buf[j + 1:]
                if nmea_ok(line):
                    try:
                        self._on_nmea(line[1:-3].split(","))
                    except (ValueError, IndexError):
                        pass
        self._buf = buf

    def _on_ubx(self, cls, mid, payload):
        if cls == ACK and len(payload) >= 2:
            self._acks[(payload[0], payload[1])] = (mid == 0x01)
        elif (cls, mid) == CFG_TP5 and len(payload) >= 32:
            self._tp5 = payload

    def _on_nmea(self, f):
        kind = f[0][2:]
        talker = f[0][:2]
        now = time.monotonic()
        with self.lock:
            self.last_rx = now
            if kind == "RMC":
                self.valid = f[2] == "A"
                if f[1] and f[9]:
                    hh, mm, ss = int(f[1][:2]), int(f[1][2:4]), float(f[1][4:])
                    dd, mo, yy = int(f[9][:2]), int(f[9][2:4]), 2000 + int(f[9][4:6])
                    self.utc = datetime(yy, mo, dd, hh, mm, int(ss),
                                        int(round((ss % 1) * 1e6)), tzinfo=timezone.utc)
                    self.utc_rx = now
                self.lat, self.lon = nmea_deg(f[3], f[4]), nmea_deg(f[5], f[6])
                self.speed_kmh = float(f[7]) * 1.852 if f[7] else None
            elif kind == "GGA":
                self.quality = int(f[6] or 0)
                self.sats_used = int(f[7] or 0)
                self.hdop = float(f[8]) if f[8] else None
                self.alt = float(f[9]) if f[9] else None
            elif kind == "GSA":
                self.fix = int(f[2] or 1)
                self.pdop = float(f[15]) if f[15] else None
                self.vdop = float(f[17]) if len(f) > 17 and f[17] else None
            elif kind == "GSV":
                self.sats_view[talker] = int(f[3] or 0)
                for k in range(4, len(f) - 3, 4):
                    if f[k]:
                        snr = f[k + 3]
                        self.snr[(talker, f[k])] = int(snr) if snr else None

    # ---- status ----
    def has_fix(self):
        return self.valid and self.fix >= 2 and time.monotonic() - self.last_rx < 3.0

    def summary(self):
        """Dict of display strings."""
        with self.lock:
            alive = time.monotonic() - self.last_rx < 3.0
            fix = FIX_NAMES.get(self.fix, "?") if self.valid else "no fix"
            in_view = sum(self.sats_view.values())
            tracked = sum(1 for s in self.snr.values() if s)
            d = {
                "link": f"{self.port}" + ("" if alive else "  (no data!)"),
                "utc": self.utc.strftime("%Y-%m-%d %H:%M:%S") + " UTC" if self.utc else "-",
                "fix": f"{fix}, {GGA_QUALITY.get(self.quality, self.quality)}",
                "sats": f"{self.sats_used} used, {tracked} tracked, {in_view} in view",
                "dop": "HDOP " + (f"{self.hdop:.2f}" if self.hdop else "-")
                       + "  PDOP " + (f"{self.pdop:.2f}" if self.pdop else "-"),
                "pos": (f"{abs(self.lat):.5f}{'N' if self.lat >= 0 else 'S'} "
                        f"{abs(self.lon):.5f}{'E' if self.lon >= 0 else 'W'}"
                        if self.lat is not None and self.lon is not None else "-"),
                "alt": f"{self.alt:.1f} m" if self.alt is not None else "-",
                "pulse": (f"{self.tp_rate:g} Hz" if self.tp_rate else "default (1 Hz)")
                         + ("" if self.has_fix() else "  (off: no fix)"),
            }
        return d

    # ---- timepulse ----
    def _send_wait(self, cls, mid, payload, want, timeout=2.0, tries=3):
        """Send a UBX message; wait for the wanted reply ('ack' or 'tp5'). Replies queue
        behind the NMEA output (~0.7 s at 9600 baud), and the first message after the
        port is opened goes unanswered, so it is resent."""
        for _ in range(tries):
            if want == "ack":
                self._acks.pop((cls, mid), None)
            else:
                self._tp5 = None
            self.ser.write(ubx_frame(cls, mid, payload))
            t = time.monotonic() + timeout
            while time.monotonic() < t:
                if want == "ack" and (cls, mid) in self._acks:
                    return self._acks[(cls, mid)]
                if want == "tp5" and self._tp5 is not None:
                    return self._tp5
                time.sleep(0.02)
        return None

    def set_timepulse(self, rate_hz):
        """Timepulse TP1 at rate_hz (e.g. 30 .. 0.5), only while locked. True if ACKed."""
        cur = self._send_wait(*CFG_TP5, b"\x00", "tp5")
        if cur is None:
            raise RuntimeError("GPS did not answer the CFG-TP5 poll (not a u-blox?)")
        (tp_idx, version, res, ant_delay, rf_delay, _f, _fl, _l, _ll, user_delay,
         _flags) = struct.unpack("<BBHhhIIIIiI", cur[:32])
        is_freq = abs(rate_hz - round(rate_hz)) < 1e-9
        period_us = 1e6 / rate_hz
        value = int(round(rate_hz)) if is_freq else int(round(period_us))
        length_us = int(min(100_000, period_us / 4))    # rising edge is what triggers
        flags = (TP5_ACTIVE | TP5_LOCK_GNSS_FREQ | TP5_LOCKED_OTHER_SET | TP5_IS_LENGTH
                 | TP5_ALIGN_TO_TOW | TP5_POLARITY_RISING | (TP5_IS_FREQ if is_freq else 0))
        # unlocked (no fix): same rate but zero length = no pulses
        payload = struct.pack("<BBHhhIIIIiI", tp_idx, version, res, ant_delay, rf_delay,
                              value, value, 0, length_us, user_delay, flags) + cur[32:]
        ok = self._send_wait(*CFG_TP5, payload, "ack")
        if ok:
            self.tp_rate = rate_hz
        return bool(ok)

    def close(self, restore_pps=True):
        if restore_pps and self.tp_rate not in (None, 1):
            try:
                self.set_timepulse(1)
            except Exception as e:
                print(f"GPS: could not restore 1 Hz PPS: {e}")
        self.running = False
        self.join(timeout=1)
        self.ser.close()


class Compass:
    """QMC5883L (0x0d) or HMC5883L (0x1e) on an I2C bus; heading() in degrees (magnetic,
    uncalibrated). Construct with Compass.find() -> None if there is none."""

    I2C_SLAVE = 0x0703

    def __init__(self, bus, addr, kind):
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, self.I2C_SLAVE, addr)
        self.kind = kind
        if kind == "QMC5883L":
            self._w(0x0B, 0x01)             # set/reset period
            self._w(0x09, 0x1D)             # continuous, 200 Hz, 8 G, OSR 512
        else:
            self._w(0x00, 0x70)             # 8 averages, 15 Hz
            self._w(0x01, 0x20)             # gain 1.3 G
            self._w(0x02, 0x00)             # continuous

    def _w(self, reg, val):
        os.write(self.fd, bytes([reg, val]))

    def _r(self, reg, n):
        os.write(self.fd, bytes([reg]))
        return os.read(self.fd, n)

    @classmethod
    def find(cls, bus=1):
        for addr, kind in ((0x0D, "QMC5883L"), (0x1E, "HMC5883L")):
            try:
                fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
            except OSError:
                return None
            try:
                fcntl.ioctl(fd, cls.I2C_SLAVE, addr)
                os.read(fd, 1)
            except OSError:
                continue
            finally:
                os.close(fd)
            try:
                return cls(bus, addr, kind)
            except OSError:
                pass
        return None

    def heading(self):
        try:
            if self.kind == "QMC5883L":
                x, y, z = struct.unpack("<hhh", self._r(0x00, 6))
            else:
                x, z, y = struct.unpack(">hhh", self._r(0x03, 6))
        except OSError:
            return None
        return math.degrees(math.atan2(y, x)) % 360.0
