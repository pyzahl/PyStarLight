"""Photo programs: timed schedules of DSLR photos (an eclipse's contacts, an occultation's event
time, a time-lapse from now), planned and run on the GPS UTC clock (the DSLR window's Program tab,
schedule_gui.py; the camera: dslr.py).

Derived from the owner's SoFiMagic (github.com/pyzahl/SoFiMagic, gMagicPy: the same XML programs
for gphoto2), generalised. Standard library only (tomllib, xml.etree).

A program has
- reference times: named UTC instants. An eclipse's contacts C1 .. C4 and C0 = (C2 + C3) / 2,
  an event time T0, ... ; NOW is the moment Run is pressed.
- phases, in shooting order: from `start` to `end` (a reference +/- seconds, e.g. "C2 - 6"),
  with the exposure block shot
    shots = N > 0   N times, spread evenly from start to end (one: at start); with `every`:
                    from start every `every` seconds (N times, or until end)
    shots = -1      back to back from start until end (SoFiMagic's -1)
- an exposure block: a list of exposures, each a shutter speed ('1/4000', '2', '30s'; bulb
  beyond the camera's longest), ISO, f-number (0 / none: as the lens is), and a drive mode:
  S single, C / M / L a burst of `burst` seconds (photos as fast as the camera takes them),
  B a bracket (SoFiMagic's codes: 10 = 1 EV x 3, 11 = 1 EV x 5, ...) by the shutter speed.

Formats:
- SoFiMagic XML (the Sony app's SOFIPRG.XML; with or without its xmlns): contacts as HH:MM:SS
  on a date given here, UTC or local time (the app used the camera's local clock).
- TOML (our own; see EXAMPLE_TOML): [times], [[phase]] with exposures as text,
  "1/100 ISO800", "2 ISO1600 f/4", "1/4000 ISO100 C4" (burst 4 s), "1/500 ISO400 B10" (bracket).

Running (Runner): phase by phase; before a slot the first exposure is set on the camera (ISO,
shutter, aperture take a moment over USB), then it waits for the slot and releases. A slot is
skipped (counted) when the next one is already due: so a late start (a restart during an
eclipse) catches up, and an overrunning block never piles up. Every photo goes into a CSV log:
planned and actual release (UTC), phase, block, exposure, the camera's file names. A failed
photo is logged and the program goes on.
"""
from __future__ import annotations

import csv
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Optional

NS = 1_000_000_000
# SoFiMagic's bracket codes (BURST_DURATION_LIST with drive mode B): code -> (EV step, photos)
BRACKETS = {3: (0.3, 3), 4: (0.3, 5), 5: (0.5, 3), 6: (0.5, 5), 7: (0.7, 3), 8: (0.7, 5), 9: (0.7, 9),
            10: (1.0, 3), 11: (1.0, 5), 12: (1.0, 9), 20: (2.0, 3), 21: (2.0, 5), 22: (2.0, 9),
            30: (3.0, 3), 31: (3.0, 5), 32: (3.0, 9)}
MODES = {"S": "single", "C": "burst (high)", "M": "burst (medium)", "L": "burst (low)", "B": "bracket"}
OVERHEAD_S = {"fits": 2.0, "files": 1.5, "card": 0.4}     # per photo beyond the shutter: a guess
                                                          # until photos measure it (Runner.overhead)
EXAMPLE_TOML = '''# PyStarLight photo program (capture/schedule.py). Times are UTC.
name = "Occultation field"
date = "2026-11-22"                # for times given as HH:MM:SS (default: today)
files = "files"                    # fits: download + FITS; files: the camera's files only;
                                   # card: they stay on the camera's card

[times]
T0 = "03:14:05"                    # HH:MM:SS[.fff] on `date`, or "2026-11-22T03:14:05Z"
# NOW is the moment Run is pressed

[[phase]]
name = "before"
start = "T0 - 300"
end = "T0 - 30"
shots = 10                         # blocks spread evenly from start to end (-1: back to back)
exposures = ["2 ISO1600", "10 ISO1600"]

[[phase]]
name = "after"
start = "T0 + 30"
every = 60                         # a block every 60 s ...
shots = 5                          # ... 5 times
exposures = ["2 ISO1600"]
'''


# the SoFiMagic app's lists (CameraUtilISOs / CameraUtilShutterSpeed): the editor's choices
# while no camera is connected
STD_ISOS = [50, 64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000,
            2500, 3200, 4000, 5000, 6400, 8000, 10000, 12800, 16000, 20000, 25600, 32000, 40000,
            51200, 64000, 80000, 102400]
STD_APERTURES = [1, 1.1, 1.3, 1.4, 1.6, 1.8, 2, 2.5, 2.8, 3.2, 3.5, 4, 4.5, 5, 5.6, 6.3, 7.1, 8, 9, 10,
                 11, 13, 14, 16, 18, 20, 22, 28, 32]
STD_SHUTTERS = ["1/8000", "1/6400", "1/5000", "1/4000", "1/3200", "1/2500", "1/2000", "1/1600",
                "1/1250", "1/1000", "1/800", "1/640", "1/500", "1/400", "1/320", "1/250", "1/200",
                "1/160", "1/125", "1/100", "1/80", "1/60", "1/50", "1/40", "1/30", "1/25", "1/20",
                "1/15", "1/13", "1/10", "1/8", "1/6", "1/5", "1/4", "1/3", "0.4", "1/2", "0.6", "0.8",
                "1", "1.3", "1.6", "2", "2.5", "3.2", "4", "5", "6", "8", "10", "13", "15", "20", "25",
                "30"]
# the SoFiMagic app's default program (its README): New eclipse program starts from it
DEFAULT_SOFIMAGIC = """<?xml version="1.0" encoding="UTF-8"?><sofimagic xmlns="com.github.pyzahl">
  <VALID_SOFI_PROGRAM>
    <STATUS>VALID</STATUS>
    <LOGGING_LEVEL>5</LOGGING_LEVEL>
  </VALID_SOFI_PROGRAM>
  <ECLIPSE_REF_CONTACT_TIMES>
    <C1>11:00:00</C1>
    <C2>12:10:00</C2>
    <C3>12:14:00</C3>
    <C4>13:24:00</C4>
  </ECLIPSE_REF_CONTACT_TIMES>
  <PHASE>
    <NAME>Partial1</NAME>
    <REF_CONTACT_START>1</REF_CONTACT_START>
    <START>-30</START>
    <REF_CONTACT_END>2</REF_CONTACT_END>
    <END>-10</END>
    <NUMBER_SHOTS>100</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>S,C,S,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>0,4,0,</BURST_DURATION_LIST>
    <ISO_LIST>400,400,320,</ISO_LIST>
    <F_LIST>3.2,0.0,0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/3200,1/2000,1/4000,</SHUTTER_SPEED_LIST>
  </PHASE>
  <PHASE>
    <NAME>Contact2</NAME>
    <REF_CONTACT_START>2</REF_CONTACT_START>
    <START>-6</START>
    <REF_CONTACT_END>2</REF_CONTACT_END>
    <END>6</END>
    <NUMBER_SHOTS>-1</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>C,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>12,</BURST_DURATION_LIST>
    <ISO_LIST>100,</ISO_LIST>
    <F_LIST>0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/4000,</SHUTTER_SPEED_LIST>
  </PHASE>
  <PHASE>
    <NAME>TotalityA</NAME>
    <REF_CONTACT_START>2</REF_CONTACT_START>
    <START>6</START>
    <REF_CONTACT_END>0</REF_CONTACT_END>
    <END>-30</END>
    <NUMBER_SHOTS>-1</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>S,S,S,S,S,S,S,S,S,S,S,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>0,0,0,0,0,0,0,0,0,0,0,</BURST_DURATION_LIST>
    <ISO_LIST>50,100,400,800,800,800,800,800,800,800,800,</ISO_LIST>
    <F_LIST>0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/4000,1/2000,1/1000,1/1000,1/500,1/250,1/100,1/50,1/20,1/4,1/1,</SHUTTER_SPEED_LIST>
  </PHASE>
  <PHASE>
    <NAME>MaxTotality</NAME>
    <REF_CONTACT_START>0</REF_CONTACT_START>
    <START>-30</START>
    <REF_CONTACT_END>0</REF_CONTACT_END>
    <END>30</END>
    <NUMBER_SHOTS>-1</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>S,S,S,S,S,S,S,S,S,S,S,S,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>0,0,0,0,0,0,0,0,0,0,0,0,</BURST_DURATION_LIST>
    <ISO_LIST>100,400,800,800,800,800,800,800,800,800,800,800,</ISO_LIST>
    <F_LIST>0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/1000,1/1000,1/1000,1/500,1/1000,1/500,1/1000,1/500,1/100,1/20,1/1,2/1,</SHUTTER_SPEED_LIST>
  </PHASE>
  <PHASE>
    <NAME>TotalityB</NAME>
    <REF_CONTACT_START>0</REF_CONTACT_START>
    <START>30</START>
    <REF_CONTACT_END>3</REF_CONTACT_END>
    <END>-6</END>
    <NUMBER_SHOTS>-1</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>S,S,S,S,S,S,S,S,S,S,S,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>0,0,0,0,0,0,0,0,0,0,0,</BURST_DURATION_LIST>
    <ISO_LIST>50,100,400,800,800,800,800,800,800,800,800,</ISO_LIST>
    <F_LIST>0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/4000,1/2000,1/1000,1/1000,1/500,1/250,1/100,1/50,1/20,1/4,1/1,</SHUTTER_SPEED_LIST>
  </PHASE>
  <PHASE>
    <NAME>Contact3</NAME>
    <REF_CONTACT_START>3</REF_CONTACT_START>
    <START>-6</START>
    <REF_CONTACT_END>3</REF_CONTACT_END>
    <END>6</END>
    <NUMBER_SHOTS>-1</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>C,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>12,</BURST_DURATION_LIST>
    <ISO_LIST>100,</ISO_LIST>
    <F_LIST>0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/4000,</SHUTTER_SPEED_LIST>
  </PHASE>
  <PHASE>
    <NAME>Partial2</NAME>
    <REF_CONTACT_START>3</REF_CONTACT_START>
    <START>10</START>
    <REF_CONTACT_END>4</REF_CONTACT_END>
    <END>30</END>
    <NUMBER_SHOTS>100</NUMBER_SHOTS>
    <CAMERA_FLAG_LIST>S,S,S,</CAMERA_FLAG_LIST>
    <BURST_DURATION_LIST>0,0,0,</BURST_DURATION_LIST>
    <ISO_LIST>400,400,320,</ISO_LIST>
    <F_LIST>0.0,0.0,0.0,</F_LIST>
    <SHUTTER_SPEED_LIST>1/3200,1/2000,1/3200,</SHUTTER_SPEED_LIST>
  </PHASE>
</sofimagic>
"""


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------
@dataclass
class Exposure:
    shutter: Optional[float] = None       # s; None: as the camera is set
    iso: Optional[int] = None
    fnumber: Optional[float] = None       # None: as the lens is
    mode: str = "S"                       # S / C / M / L / B
    burst: float = 0.0                    # C/M/L: seconds of the burst; B: the bracket code

    def label(self):
        parts = [shutter_text(self.shutter) if self.shutter else "(shutter as set)"]
        if self.iso:
            parts.append(f"ISO{self.iso}")
        if self.fnumber:
            parts.append(f"f/{self.fnumber:g}")
        if self.mode in "CML" and self.mode != "S":
            parts.append(f"{self.mode}{self.burst:g}")
        elif self.mode == "B":
            parts.append(f"B{int(self.burst)}")
        return " ".join(parts)

    def bracket(self):
        """The shutter speeds of a bracket (B), around this one; [shutter] otherwise."""
        if self.mode != "B" or not self.shutter:
            return [self.shutter]
        ev, n = BRACKETS.get(int(self.burst), (1.0, 3))
        half = n // 2
        return [self.shutter * 2.0 ** (ev * k) for k in range(-half, half + 1)]

    def photos(self, overhead_s):
        """(photos, seconds) this exposure takes (a burst: as many as fit)."""
        each = (self.shutter or 0.0) + overhead_s
        if self.mode in ("C", "M", "L") and self.burst > 0:
            return max(1, int(self.burst / max(each, 0.05))), max(self.burst, each)
        shutters = self.bracket()
        return len(shutters), sum((s or 0.0) + overhead_s for s in shutters)


@dataclass
class Phase:
    name: str
    start: tuple                          # (reference, offset s)
    end: Optional[tuple]                  # (reference, offset s) or None (with shots + every)
    shots: int = 1                        # N > 0, or -1: back to back until end
    every: Optional[float] = None         # s between blocks (else spread evenly)
    exposures: list = field(default_factory=list)


@dataclass
class Program:
    name: str
    times: dict                           # reference -> UTC ns
    phases: list
    files: str = "fits"                   # fits / files / card
    source: str = ""
    notes: list = field(default_factory=list)   # what loading it found odd

    def resolve(self, ref):
        """UTC ns of (reference, offset)."""
        name, offset = ref
        if name not in self.times:
            raise ValueError(f"no reference time {name!r} (have {', '.join(self.times) or 'none'})")
        return self.times[name] + int(round(offset * NS))

    def window(self, phase):
        """(start_ns, end_ns or None) of a phase."""
        start = self.resolve(phase.start)
        if phase.end is not None:
            return start, self.resolve(phase.end)
        if phase.every and phase.shots > 0:
            return start, start + int(round(phase.every * (phase.shots - 1) * NS))
        return start, start if phase.shots == 1 else None

    def slots(self, phase):
        """Block start times (UTC ns) of a phase; None for a back-to-back phase (-1)."""
        start, end = self.window(phase)
        if phase.shots == -1:
            return None
        if phase.every:
            n = phase.shots if phase.shots > 0 else int((end - start) / (phase.every * NS)) + 1
            out = [start + int(round(i * phase.every * NS)) for i in range(n)]
            return [t for t in out if end is None or t <= end + NS // 1000]
        if phase.shots <= 1 or end is None:
            return [start]
        step = (end - start) / (phase.shots - 1)
        return [start + int(round(i * step)) for i in range(phase.shots)]

    def at(self, now_ns):
        """The program with NOW at now_ns (the moment Run is pressed)."""
        return replace(self, times={**self.times, "NOW": int(now_ns)})

    def start_now(self, now_ns, ref=None, lead_s=15.0):
        """(program, shift ns): moved so that `ref` (a reference time; None: the first phase's
        start) comes lead_s after now_ns. Phases before it are then over and skipped."""
        at = self.first_start() if ref is None else self.resolve((ref, 0.0))
        shift = int(now_ns + lead_s * NS) - at
        return self.shifted(shift), shift

    def shifted(self, delta_ns):
        """The program with every reference time moved by delta_ns (a test run)."""
        return replace(self, times={k: v + delta_ns for k, v in self.times.items()})

    def first_start(self):
        return min((self.window(p)[0] for p in self.phases), default=None)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def shutter_seconds(text):
    """'1/4000' -> 0.00025, '2' / '2s' / '2"' -> 2.0, '1/1' -> 1.0; ValueError otherwise."""
    t = str(text).strip().lower().rstrip("s\"″ ").replace(",", ".")
    if "/" in t:
        a, b = t.split("/", 1)
        v = float(a) / float(b)
    else:
        v = float(t)
    if not v > 0:
        raise ValueError(f"not a shutter speed: {text!r}")
    return v


def shutter_text(s, unit="s"):
    """'1/4000', '1/3', '0.4s', '2s', '2.5s' (unit='' without the s)."""
    if s is None:
        return "-"
    if s < 1 and (s < 0.3 or abs(1 / s - round(1 / s)) < 1e-3):
        return f"1/{round(1 / s):d}"
    return f"{s:g}{unit}"


def parse_exposure(text):
    """'1/100 ISO800 f/4 C4' -> Exposure (any order; ISO and f optional; mode S default)."""
    e = Exposure()
    for tok in str(text).replace(",", " ").split():
        t = tok.strip()
        u = t.upper()
        if u.startswith("ISO"):
            e.iso = int(u[3:].lstrip("=:"))
        elif u.startswith("F/") or (u.startswith("F") and re.fullmatch(r"F\d+(\.\d+)?", u)):
            e.fnumber = float(u.lstrip("F/")) or None
        elif re.fullmatch(r"[SCMLB]\d*(\.\d+)?", u):
            e.mode = u[0]
            e.burst = float(u[1:]) if len(u) > 1 else (10.0 if u[0] == "B" else 0.0)
        else:
            e.shutter = shutter_seconds(t)
    return e


def parse_ref(text, names=None):
    """'C2 - 6' / 'T0+300' / 'NOW' / 'C1' -> (name, offset s)."""
    m = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:([+-])\s*([0-9.]+)\s*s?)?\s*", str(text))
    if not m:
        raise ValueError(f"not a time: {text!r} (a reference +/- seconds, e.g. 'C2 - 6')")
    name, sign, num = m.groups()
    name = name.upper() if name.upper() in ("NOW",) else name
    return name, (float(num) * (-1 if sign == "-" else 1) if num else 0.0)


def parse_time(text, day, local=False):
    """UTC ns of 'HH:MM:SS[.fff]' on `day` (a date; local: the system's time zone), or of an ISO
    date-time ('2026-11-22T03:14:05Z', '... +02:00')."""
    s = str(text).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?", s):
        parts = s.split(":")
        h, m = int(parts[0]), int(parts[1])
        sec = float(parts[2]) if len(parts) > 2 else 0.0
        t = datetime(day.year, day.month, day.day, tzinfo=None if local else timezone.utc)
        t = (t.astimezone() if local else t) + timedelta(hours=h, minutes=m, seconds=sec)
        return int(round(t.timestamp() * NS))
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.astimezone() if local else t.replace(tzinfo=timezone.utc)
    return int(round(t.timestamp() * NS))


def utc_text(ns, ms=True):
    t = datetime.fromtimestamp(ns / NS, timezone.utc)
    return t.strftime("%H:%M:%S.%f")[:12] if ms else t.strftime("%H:%M:%S")


def duration_text(seconds):
    s = abs(seconds)
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    out = (f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}") + f".{int((s % 1) * 10)}"
    return ("-" if seconds < 0 else "") + out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def today():
    return datetime.now(timezone.utc).date()


def load(path, day=None, local=False):
    """A program from a SoFiMagic XML or our TOML file (by its content)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    prog = (load_sofimagic(text, day, local) if text.lstrip().startswith("<") else
            load_toml(text, day, local))
    prog.source = path
    return prog


def load_sofimagic(text, day=None, local=False):
    """A SoFiMagic program (SOFIPRG.XML of the Sony app / gMagicPy). Contacts on `day` (default
    today), UTC unless local."""
    day = day or today()
    text = re.sub(r'\sxmlns="[^"]*"', "", text, count=1)          # the app's namespace
    root = ET.fromstring(text)
    ct = root.find("ECLIPSE_REF_CONTACT_TIMES")
    if ct is None:
        raise ValueError("not a SoFiMagic program: no ECLIPSE_REF_CONTACT_TIMES")
    times = {f"C{i}": parse_time(ct.findtext(f"C{i}"), day, local) for i in (1, 2, 3, 4)}
    times["C0"] = (times["C2"] + times["C3"]) // 2                 # maximum: SoFiMagic's C0
    phases, notes = [], []
    for p in root.findall("PHASE"):
        name = (p.findtext("NAME") or "").strip()
        shots = int(float(p.findtext("NUMBER_SHOTS") or 0))
        if name.upper() == "END" or shots == 0:
            break
        lists = [[v.strip() for v in (p.findtext(tag) or "").split(",")]
                 for tag in ("CAMERA_FLAG_LIST", "BURST_DURATION_LIST", "ISO_LIST", "F_LIST",
                             "SHUTTER_SPEED_LIST")]
        exposures = []
        for k, iso in enumerate(lists[2]):
            if not iso or int(float(iso)) <= 0:                      # ISO 0 ends the list
                break
            cf, bt, _, f, ss = (_item(lists, i, k) for i in range(5))
            if not ss:      # gMagicPy shot a left-over default speed here: say so, keep the camera's
                notes.append(f"{name}: exposure {k + 1} has no shutter speed (the camera's is kept)")
            mode = (cf or "S").upper()[:1]
            if mode not in MODES:
                notes.append(f"{name}: drive mode {cf!r} unknown, single shot")
                mode = "S"
            exposures.append(Exposure(shutter_seconds(ss) if ss else None, int(float(iso)),
                                      float(f or 0) or None, mode, float(bt or 0)))
        phases.append(Phase(name, (f"C{int(p.findtext('REF_CONTACT_START'))}", float(p.findtext("START"))),
                            (f"C{int(p.findtext('REF_CONTACT_END'))}", float(p.findtext("END"))),
                            shots, None, exposures))
    status = (root.findtext("VALID_SOFI_PROGRAM/STATUS") or "VALID").strip()
    if status.upper() != "VALID":
        raise ValueError(f"the program says STATUS {status}")
    return Program("SoFiMagic eclipse", times, phases, "card", notes=notes)


def _item(lists, i, k):
    """Entry k of SoFiMagic list i ('' past its end: a shorter list)."""
    return lists[i][k] if k < len(lists[i]) else ""


def load_toml(text, day=None, local=False):
    import tomllib
    d = tomllib.loads(text)
    day = Date.fromisoformat(d["date"]) if d.get("date") and day is None else (day or today())
    local = bool(d.get("local", local))
    times = {k: parse_time(v, day, local) for k, v in (d.get("times") or {}).items()}
    phases = []
    for p in d.get("phase", []):
        ex = [parse_exposure(e) if isinstance(e, str) else
              Exposure(shutter_seconds(e["shutter"]) if e.get("shutter") else None, e.get("iso"),
                       e.get("f") or None, str(e.get("mode", "S")).upper()[:1], float(e.get("burst", 0)))
              for e in p.get("exposures", [])]
        phases.append(Phase(p.get("name", f"phase {len(phases) + 1}"), parse_ref(p["start"]),
                            parse_ref(p["end"]) if p.get("end") else None, int(p.get("shots", 1)),
                            float(p["every"]) if p.get("every") else None, ex))
    files = d.get("files", "fits")
    if files not in OVERHEAD_S:
        raise ValueError(f"files = {files!r}: fits, files or card")
    return Program(d.get("name", "photo program"), times, phases, files)


UNTIL_STOPPED_S = 24 * 3600.0             # a sequence without a count: a day at most


def sequence(count, every_s, exposures, start_in_s=0.0, files="files", name="sequence"):
    """A plain sequence from NOW (+ start_in_s): `count` blocks, one every `every_s` s (0: back
    to back); count 0: until stopped (a day at most)."""
    start = ("NOW", float(start_in_s))
    if count > 0 and every_s:
        ph = Phase(name, start, None, int(count), float(every_s), list(exposures))
    elif count > 0:                         # back to back, count times: every = 0 s
        ph = Phase(name, start, None, int(count), 1e-6, list(exposures))
    else:
        end = ("NOW", float(start_in_s) + UNTIL_STOPPED_S)
        ph = Phase(name, start, end, 0 if every_s else -1, float(every_s) if every_s else None,
                   list(exposures))
    return Program(name, {"NOW": 0}, [ph], files)


def xml_shutter(s):
    """A shutter speed as SoFiMagic writes it: always n/d ('1/4000', '2/1'; gMagicPy reads only
    that form)."""
    if s >= 0.3 or abs(1 / s - round(1 / s)) > 1e-6:
        from fractions import Fraction
        fr = Fraction(s).limit_denominator(10000)
        return f"{fr.numerator}/{fr.denominator}"
    return f"1/{round(1 / s)}"


def to_sofimagic(prog, local=False):
    """SoFiMagic XML of a program whose times are the contacts C1 .. C4 (C0 their middle): for
    the Sony app or gMagicPy. Contacts as HH:MM:SS, UTC (local: the system's time zone, as the
    camera's clock); offsets in whole seconds (gMagicPy reads integers). ValueError when the
    program does not fit (other references, no shutter speed)."""
    from xml.sax.saxutils import escape
    if not all(f"C{i}" in prog.times for i in (1, 2, 3, 4)):
        raise ValueError("SoFiMagic programs are made of the contacts C1 .. C4: this one is not")

    def hms(ns):
        t = datetime.fromtimestamp(ns / NS, timezone.utc)
        return (t.astimezone() if local else t).strftime("%H:%M:%S")

    def contact(ref):
        if ref is None or not re.fullmatch(r"C[0-4]", ref[0]):
            raise ValueError(f"reference {ref and ref[0]!r}: SoFiMagic knows C0 .. C4 only")
        return ref[0][1], str(int(round(ref[1])))
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<sofimagic>", "  <VALID_SOFI_PROGRAM>",
           "    <STATUS>VALID</STATUS>", "    <LOGGING_LEVEL>5</LOGGING_LEVEL>",
           f"    <COMMENT>{escape(prog.name)} (written by PyStarLight)</COMMENT>",
           "  </VALID_SOFI_PROGRAM>", "  <ECLIPSE_REF_CONTACT_TIMES>"]
    out += [f"    <C{i}>{hms(prog.times[f'C{i}'])}</C{i}>" for i in (1, 2, 3, 4)]
    out.append("  </ECLIPSE_REF_CONTACT_TIMES>")
    for p in prog.phases:
        if p.every:
            raise ValueError(f"{p.name}: 'every' has no SoFiMagic form (use shots)")
        if any(e.shutter is None or not e.iso for e in p.exposures):
            raise ValueError(f"{p.name}: every exposure needs a shutter speed and an ISO in SoFiMagic "
                             "(ISO 0 ends its list)")
        (rs, s), (re_, e) = contact(p.start), contact(p.end)
        ex = p.exposures

        def row(values):
            return ",".join(values) + ","
        out += ["  <PHASE>", f"    <NAME>{escape(p.name)}</NAME>",
                f"    <REF_CONTACT_START>{rs}</REF_CONTACT_START>", f"    <START>{s}</START>",
                f"    <REF_CONTACT_END>{re_}</REF_CONTACT_END>", f"    <END>{e}</END>",
                f"    <NUMBER_SHOTS>{p.shots}</NUMBER_SHOTS>",
                f"    <CAMERA_FLAG_LIST>{row(x.mode for x in ex)}</CAMERA_FLAG_LIST>",
                f"    <BURST_DURATION_LIST>{row(str(int(x.burst)) for x in ex)}</BURST_DURATION_LIST>",
                f"    <ISO_LIST>{row(str(x.iso) for x in ex)}</ISO_LIST>",
                f"    <F_LIST>{row(str(float(x.fnumber or 0)) for x in ex)}</F_LIST>",
                f"    <SHUTTER_SPEED_LIST>{row(xml_shutter(x.shutter) for x in ex)}</SHUTTER_SPEED_LIST>",
                "  </PHASE>"]
    out.append("</sofimagic>")
    return "\n".join(out) + "\n"


def to_toml(prog):
    """Our TOML text of a program (times as ISO UTC)."""
    def iso(ns):
        return datetime.fromtimestamp(ns / NS, timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z")

    def ref(r):
        return f"{r[0]} {'-' if r[1] < 0 else '+'} {abs(r[1]):g}" if r[1] else r[0]
    lines = [f'name = "{prog.name}"', f'files = "{prog.files}"', "", "[times]"]
    lines += [f'{k} = "{iso(v)}"' for k, v in prog.times.items() if k != "NOW"]
    for p in prog.phases:
        lines += ["", "[[phase]]", f'name = "{p.name}"', f'start = "{ref(p.start)}"']
        if p.end is not None:
            lines.append(f'end = "{ref(p.end)}"')
        lines.append(f"shots = {p.shots}")
        if p.every:
            lines.append(f"every = {p.every:g}")
        lines.append("exposures = [" + ", ".join(f'"{e.label()}"' for e in p.exposures) + "]")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------
@dataclass
class Row:
    phase: Phase
    start_ns: int
    end_ns: Optional[int]
    blocks: Optional[int]          # None: back to back (as many as fit)
    photos: int
    block_s: float
    warnings: list


def timeline(prog, now_ns=None, overhead_s=None):
    """[Row] of the phases and the program's warnings: a block longer than the time between
    blocks, phases out of order or overlapping, phases over, a reference missing."""
    overhead = OVERHEAD_S[prog.files] if overhead_s is None else overhead_s
    rows, prev_end = [], None
    for p in prog.phases:
        warn = []
        try:
            start, end = prog.window(p)
            slots = prog.slots(p)
        except ValueError as e:
            rows.append(Row(p, 0, None, 0, 0, 0.0, [str(e)]))
            continue
        per = [e.photos(overhead) for e in p.exposures]
        block_photos, block_s = sum(n for n, _ in per), sum(s for _, s in per)
        if not p.exposures:
            warn.append("no exposures")
        if slots is None:
            blocks = int((end - start) / NS / block_s) if block_s > 0 and end else 0
            photos = blocks * block_photos
        else:
            blocks, photos = len(slots), len(slots) * block_photos
            if len(slots) > 1:
                gap = min(b - a for a, b in zip(slots, slots[1:])) / NS
                if block_s > gap:
                    warn.append(f"a block takes ~{block_s:.1f} s, blocks are {gap:.1f} s apart: "
                                "some are skipped")
        if end is not None and end < start:
            warn.append("ends before it starts")
        if prev_end is not None and start < prev_end:
            warn.append("starts before the phase before ends (they run one after the other)")
        if now_ns is not None and end is not None and end < now_ns:
            warn.append("over")
        prev_end = end if end is not None else start
        rows.append(Row(p, start, end, None if slots is None else blocks, photos, block_s, warn))
    return rows


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
class Runner:
    """Runs a program on a camera. camera: an object with
        prepare(exposure, shutter)  ISO / aperture / shutter set (before the slot)
        release(shutter, files)     the photo: returns ([file names], release UTC ns)
    clock() -> UTC ns, sleep(s). status (for the GUI): state ('waiting', 'shooting', 'done',
    'stopped', 'failed'), phase, next_ns (the next slot), done (photos), skipped (blocks), errors,
    last; log_path. stop() ends it between photos (an exposure under way finishes)."""

    def __init__(self, prog, camera, clock, sleep=time.sleep, log_dir=".", on_photo=None, name="",
                 stamp_ns=None):
        self.prog, self.camera, self.clock, self.sleep = prog, camera, clock, sleep
        self.on_photo = on_photo               # (files, info) -> None: e.g. FITS in the background
        self.stopping = threading.Event()
        self.state, self.phase, self.next_ns, self.last = "waiting", "", None, ""
        self.done = self.skipped = self.cut = 0     # photos; blocks skipped; blocks cut short
        self.records = []                           # every photo as a dict (the dry run's list)
        self.cuts = {}                              # phase -> blocks cut short (the next phase)
        self.max_photos = None                      # stop after this many (a dry run of an endless one)
        self.limit = None                           # the next phase's start (UTC ns)
        self.errors = []
        self.overheads = []                    # measured: release time minus the shutter
        # <name>_<UTC>Z_<program>_photolog.csv: name as the photos' (<camera>_<asteroid>_<star>)
        stamp = datetime.fromtimestamp((stamp_ns or clock()) / NS, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = re.sub(r"[^A-Za-z0-9.+-]+", "-", prog.name).strip("-") or "program"
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, "_".join(x for x in (name, stamp, tag, "photolog") if x)
                                     + ".csv")

    def stop(self):
        self.stopping.set()

    def overhead(self):
        return (sorted(self.overheads)[len(self.overheads) // 2] if self.overheads
                else OVERHEAD_S[self.prog.files])

    def wait_until(self, t_ns):
        """Sleep until t_ns (the last 50 ms in short steps); False when stopped."""
        while not self.stopping.is_set():
            left = (t_ns - self.clock()) / NS
            if left <= 0:
                return True
            self.sleep(min(left - 0.05, 0.5) if left > 0.1 else min(left, 0.005))
        return False

    def run(self):
        with open(self.log_path, "w", newline="") as f:
            self.log = csv.writer(f)
            # shutter_s .. mode: as programmed; camera_settings: as the camera had them at the release
            self.log.writerow(["planned_utc", "release_utc", "late_s", "phase", "block", "exposure",
                               "photo", "shutter_s", "iso", "fnumber", "mode", "camera_settings", "files",
                               "error"])
            try:
                phases = self.prog.phases
                for i, p in enumerate(phases):
                    if self.stopping.is_set():
                        break
                    # no exposure may run into the next phase (gMagicPy finished a whole list
                    # first: a long block could eat a short contact phase after it)
                    self.limit = self.prog.window(phases[i + 1])[0] if i + 1 < len(phases) else None
                    self.run_phase(p)
                    f.flush()
                self.state = "stopped" if self.stopping.is_set() else "done"
            except Exception as e:
                self.state, self.last = "failed", str(e)
                self.errors.append(str(e))
            self.next_ns = None
        return self.state

    def run_phase(self, p):
        start, end = self.prog.window(p)
        slots = self.prog.slots(p)
        if end is not None and self.clock() > end:
            return                                          # over
        self.phase = p.name
        if slots is None:                                   # back to back until end
            self.next_ns = start
            if not self.wait_until(start):
                return
            block = 0
            while self.clock() < end and not self.stopping.is_set():
                before = self.done + len(self.errors)
                self.run_block(p, block, None, end, prepared=False)
                if self.done + len(self.errors) == before:      # nothing fits before the next
                    return                                      # phase (or no exposures): done
                block += 1
            return
        for i, t in enumerate(slots):
            later = slots[i + 1] if i + 1 < len(slots) else (end if end is not None else t) + NS // 2
            if self.clock() >= later:                       # the next one is due already
                self.skipped += 1
                continue
            if p.exposures:                                 # set up before the slot
                e = p.exposures[0]
                self._prepare(e, e.bracket()[0])
            self.next_ns = t
            self.state = "waiting"
            if not self.wait_until(t):
                return
            self.run_block(p, i, t, None, prepared=True)

    def would_overrun(self, e):
        """An exposure started now would end after the next phase's start."""
        if self.limit is None:
            return False
        return self.clock() + int(((e.shutter or 0.0) + self.overhead()) * NS) > self.limit

    def _prepare(self, e, shutter):
        try:
            self.camera.prepare(e, shutter)
        except Exception as ex:                             # e.g. no aperture without a lens
            self.errors.append(f"settings {e.label()}: {ex}")

    def run_block(self, p, block, planned, end, prepared):
        """The exposures of a block; prepared: the first one is set on the camera already."""
        self.state = "shooting"
        for k, e in enumerate(p.exposures):
            if self.stopping.is_set() or (end is not None and self.clock() >= end):
                return
            if self.would_overrun(e):
                self.cut += 1
                self.cuts[p.name] = self.cuts.get(p.name, 0) + 1
                return
            if e.mode in ("C", "M", "L") and e.burst > 0:
                until = self.clock() + int(e.burst * NS)
                n = 0
                while self.clock() < until and not self.stopping.is_set() and not self.would_overrun(e):
                    self.shoot(p, block, k, n, e, e.shutter, planned if n == 0 and k == 0 else None,
                               set_up=not (prepared and k == 0 and n == 0))
                    n += 1
            else:
                for n, s in enumerate(e.bracket()):
                    if n and self.would_overrun(replace(e, shutter=s)):
                        self.cut += 1
                        self.cuts[p.name] = self.cuts.get(p.name, 0) + 1
                        return
                    self.shoot(p, block, k, n, e, s, planned if n == 0 and k == 0 else None,
                               set_up=not (prepared and k == 0 and n == 0))

    def shoot(self, p, block, k, n, e, shutter, planned, set_up=True):
        err, files, rel, actual = "", [], self.clock(), None
        try:
            if set_up:
                self.camera.prepare(e, shutter)
            got = self.camera.release(shutter, self.prog.files)
            files, rel = got[0], got[1]
            actual = got[2] if len(got) > 2 else None       # what the camera had at the release
            took = (self.clock() - rel) / NS - (shutter or 0.0)
            self.overheads = (self.overheads + [max(0.0, took)])[-20:]
            self.done += 1
            if self.on_photo is not None and files:
                self.on_photo(files, {"phase": p.name, "block": block, "exposure": e, "shutter": shutter,
                                      "release_ns": rel})
        except Exception as ex:
            err = str(ex)
            self.errors.append(f"{p.name} #{block}: {err}")
        self.last = f"{p.name} #{block} {e.label()}" + (f": {err}" if err else "")
        late = (rel - planned) / NS if planned is not None else ""
        self.records.append({"planned": planned, "release": rel, "late": late, "phase": p.name,
                             "block": block, "exposure": k, "photo": n, "shutter": shutter, "iso": e.iso,
                             "fnumber": e.fnumber, "mode": e.mode, "files": files, "error": err,
                             "camera": camera_text(actual)})
        if self.max_photos is not None and len(self.records) >= self.max_photos:
            self.stop()
        self.log.writerow([utc_text(planned) if planned else "", utc_text(rel),
                           f"{late:.3f}" if late != "" else "", p.name, block, k, n,
                           f"{shutter:.6g}" if shutter else "", e.iso or "", e.fnumber or "", e.mode,
                           camera_text(actual), " ".join(os.path.basename(x) for x in files), err])


def camera_text(settings):
    """'0.8 ISO800 f/0': the camera's own shutter / ISO / aperture read at the release ('' for none)."""
    if not settings:
        return ""
    parts = [str(settings.get("shutter") or ""), f"ISO{settings['iso']}" if settings.get("iso") else "",
             str(settings.get("aperture") or "")]
    return " ".join(p for p in parts if p)


def estimate_end(prog):
    """UTC ns the program's last phase ends (None if open)."""
    ends = [prog.window(p)[1] for p in prog.phases]
    return None if not ends or any(e is None for e in ends) else max(ends)


def seconds_to(ns, now_ns):
    return (ns - now_ns) / NS if ns is not None else math.nan


# ---------------------------------------------------------------------------
# Dry run: the program without a camera, as a list
# ---------------------------------------------------------------------------
DRY_MAX_PHOTOS = 20000          # an endless program stops here in a dry run


class VirtualClock:
    """A clock that a sleep moves forward (fast-forward)."""

    def __init__(self, t_ns):
        self.t = int(t_ns)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += int(max(s, 0.0005) * NS)


class SimCamera:
    """No camera: a photo takes its shutter time plus overhead_s (download, settings), slept on
    the runner's clock (the virtual one: instant; the real one: on time)."""

    def __init__(self, clock, sleep, overhead_s):
        self.clock, self.sleep, self.overhead_s = clock, sleep, overhead_s
        self.notes = []

    def prepare(self, exposure, shutter):
        pass

    def release(self, shutter, files):
        rel = self.clock()
        self.sleep((shutter or 0.0) + self.overhead_s)
        return [], rel


def dry_run(prog, now_ns, fast=True, shift_to_now=False, lead_s=5.0, overhead_s=None, out_dir=".",
            name="", real_sleep=time.sleep, real_clock=None, ref=None):
    """The program run without a camera. fast: on a virtual clock from just before its first
    phase (instant); else on the real clock (real_clock, real_sleep: on time). shift_to_now: the
    whole program moved so that `ref` (None: its first phase) starts lead_s after now_ns (an
    on-time test any day). Returns the Runner (records, skipped, cut; its CSV log in out_dir)
    and the shift (ns)."""
    prog = prog.at(now_ns)
    shift = 0
    if shift_to_now:
        prog, shift = prog.start_now(now_ns, ref, lead_s)
    overhead = OVERHEAD_S[prog.files] if overhead_s is None else overhead_s
    if fast:
        clock = VirtualClock(min(now_ns, prog.first_start() - NS) if ref is None or not shift_to_now
                             else now_ns)
        sleep = clock.sleep
    else:
        clock, sleep = real_clock or time.time_ns, real_sleep
    runner = Runner(prog, SimCamera(clock, sleep, overhead), clock, sleep=sleep, log_dir=out_dir,
                    name="_".join(x for x in (name, "dryrun") if x), stamp_ns=now_ns)
    runner.max_photos = DRY_MAX_PHOTOS
    runner.dry = {"fast": fast, "shift_ns": shift, "overhead_s": overhead, "program": prog}
    return runner, shift


def dry_run_markdown(runner, local=False):
    """The dry run as Markdown: the program, its reference times, every photo, a summary."""
    d = runner.dry
    prog, shift = d["program"], d["shift_ns"]

    def t(ns):
        if ns is None or ns == "":
            return ""
        tt = datetime.fromtimestamp(ns / NS, timezone.utc)
        tt = tt.astimezone() if local else tt
        return tt.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
    base = "local time" if local else "UTC"
    lines = [f"# Dry run: {prog.name}", "",
             f"- mode: {'fast-forward (virtual clock)' if d['fast'] else 'on time (real clock)'}",
             f"- times: {base}" + (f"; the program shifted by {shift / NS:+.1f} s to start now "
                                   "(column 'programmed': the times as programmed)" if shift else ""),
             f"- files: {prog.files}; per photo beyond the shutter: {d['overhead_s']:g} s (a guess "
             "for this camera and file mode)",
             f"- result: {runner.state}, {len(runner.records)} photos, {runner.skipped} blocks skipped, "
             f"{runner.cut} blocks cut at the next phase"
             + (f" (stopped after {DRY_MAX_PHOTOS} photos)" if len(runner.records) >= DRY_MAX_PHOTOS
                else ""),
             f"- source: {prog.source or '(edited)'}", "", "## Reference times", "",
             "| reference | time |", "|---|---|"]
    lines += [f"| {k} | {t(v - shift)} |" for k, v in prog.times.items() if k != "NOW"]
    if "NOW" in prog.times:
        lines.append(f"| NOW | {t(prog.times['NOW'] - shift)} |")
    lines += ["", "## Photos", "",
              "| # | phase | block | exp | planned | release | late s | "
              + ("programmed | " if shift else "") + "shutter | ISO | f | drive | note |",
              "|---:|---|---:|---:|---|---|---:|" + ("---|" if shift else "") + "---|---:|---:|---|---|"]
    for i, r in enumerate(runner.records, 1):
        late = f"{r['late']:.2f}" if r["late"] != "" else ""
        shot = r["release"] - shift if shift else None
        lines.append(f"| {i} | {r['phase']} | {r['block']} | {r['exposure'] + 1}"
                     + (f".{r['photo'] + 1}" if r["photo"] else "")
                     + f" | {t(r['planned'])} | {t(r['release'])} | "
                     f"{late} | " + (f"{t(shot)} | " if shift else "")
                     + f"{shutter_text(r['shutter']) if r['shutter'] else '-'} | {r['iso'] or ''} | "
                     f"{format(r['fnumber'], 'g') if r['fnumber'] else ''} | {r['mode']} | {r['error']} |")
    lines += ["", "## Per phase", "",
              "(cut: a set of exposures stopped so as not to run into the next phase)", "",
              "| phase | photos | first | last | late max s | cut |", "|---|---:|---|---|---:|---:|"]
    for p in prog.phases:
        rs = [r for r in runner.records if r["phase"] == p.name]
        if not rs:
            lines.append(f"| {p.name} | 0 | | | | {runner.cuts.get(p.name, '')} |")
            continue
        lates = [r["late"] for r in rs if r["late"] != ""]
        worst = f"{max(lates):.2f}" if lates else ""
        lines.append(f"| {p.name} | {len(rs)} | {t(rs[0]['release'])} | {t(rs[-1]['release'])} | {worst} | "
                     f"{runner.cuts.get(p.name, '')} |")
    if runner.errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in runner.errors]
    return "\n".join(lines) + "\n"


def write_dry_run(runner, local=False):
    """The Markdown beside the run's CSV log (..._dryrun_photolog.csv -> ..._dryrun.md)."""
    path = runner.log_path.replace("_photolog.csv", ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(dry_run_markdown(runner, local))
    return path


def main(argv=None):
    """python -m pystarlight.capture.schedule PROGRAM [--date D] [--local] [--on-time] ..."""
    import argparse
    ap = argparse.ArgumentParser(description="Dry run of a photo program (SoFiMagic XML or TOML): "
                                 "every photo with its time, as Markdown (and CSV).")
    ap.add_argument("program")
    ap.add_argument("--date", help="date of HH:MM:SS times (default: today, or the TOML's)")
    ap.add_argument("--local", action="store_true", help="the program's times are local time, not UTC")
    ap.add_argument("--on-time", action="store_true",
                    help="run in real time, shifted to start in --lead s (default: fast-forward at the "
                         "programmed times)")
    ap.add_argument("--start-now", nargs="?", const="", metavar="REF",
                    help="shift the program to start --lead s from now; with REF (e.g. C1, C2, T0) that "
                         "reference comes then instead (phases before it are skipped)")
    ap.add_argument("--lead", type=float, default=5.0, help="seconds from now to the start (shifted)")
    ap.add_argument("--overhead", type=float, help="seconds per photo beyond the shutter")
    ap.add_argument("-o", "--out-dir", default=".", help="folder for the .md and .csv")
    a = ap.parse_args(argv)
    prog = load(a.program, day=Date.fromisoformat(a.date) if a.date else None, local=a.local)
    for n in prog.notes:
        print(f"note: {n}")
    shift = a.on_time or a.start_now is not None
    runner, _ = dry_run(prog, time.time_ns(), fast=not a.on_time, shift_to_now=shift, lead_s=a.lead,
                        overhead_s=a.overhead, out_dir=a.out_dir, ref=a.start_now or None)
    if a.on_time:
        print(f"running on time from {utc_text(runner.prog.first_start())} UTC; Ctrl+C stops")
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.stop()
    path = write_dry_run(runner, a.local)
    print(f"{len(runner.records)} photos, {runner.skipped} skipped, {runner.cut} cut: {path}")


if __name__ == "__main__":
    main()
