"""Photo programs (capture/schedule.py): the owner's SoFiMagic XML (the Sony app's file with its
namespace, gMagicPy's Nikon D7000 program), our TOML, slots and the timeline, both writers, and
the Runner on a virtual clock with a fake camera: slots on time, set up before the slot, catch-up,
skipping, no exposure into the next phase, bursts, brackets, errors that do not stop it, the log."""

import csv
from datetime import date
from pathlib import Path

import pytest

from pystarlight.capture import schedule as sc

DATA = Path(__file__).parent / "data"
DAY = date(2026, 8, 12)
NS = sc.NS


def test_sofimagic_nikon_program():
    prog = sc.load(str(DATA / "sofimagic_nikon_d7000.xml"), day=DAY)
    t = prog.times
    assert sc.utc_text(t["C1"]) == "11:14:24.000" and sc.utc_text(t["C3"]) == "12:36:10.000"
    assert t["C0"] == (t["C2"] + t["C3"]) // 2
    names = [p.name for p in prog.phases]
    assert names[:3] == ["Partial1", "Contact2Pre", "Contact2"] and len(names) == 9
    p1 = prog.phases[0]
    assert p1.shots == 154 and p1.start == ("C1", -60.0) and p1.end == ("C2", -60.0)
    assert [e.label() for e in p1.exposures] == ["1/1600 ISO100 f/4", "1/1600 ISO100 f/8", "1/640 ISO100 f/4"]
    slots = prog.slots(p1)
    assert len(slots) == 154 and slots[0] == prog.resolve(("C1", -60))
    assert slots[-1] == prog.resolve(("C2", -60))
    c2 = prog.phases[2]
    assert prog.slots(c2) is None                        # -1: back to back
    rows = sc.timeline(prog)
    assert rows[0].blocks == 154 and rows[0].photos == 154 * 3


def test_sofimagic_sony_app_file_with_namespace_and_local_time():
    utc = sc.load(str(DATA / "sofimagic_sony_app.xml"), day=DAY)
    local = sc.load(str(DATA / "sofimagic_sony_app.xml"), day=DAY, local=True)
    assert len(utc.phases) == 9 and utc.files == "card"
    from datetime import datetime
    offset = datetime(2026, 8, 12, 12).astimezone().utcoffset().total_seconds()
    assert (utc.times["C2"] - local.times["C2"]) / NS == pytest.approx(offset)


def test_writers_round_trip(tmp_path):
    prog = sc.load(str(DATA / "sofimagic_nikon_d7000.xml"), day=DAY)
    xml = sc.to_sofimagic(prog)
    back = sc.load_sofimagic(xml, DAY)
    assert back.times == prog.times
    assert [(p.name, p.start, p.end, p.shots, [e.label() for e in p.exposures]) for p in back.phases] == \
           [(p.name, p.start, p.end, p.shots, [e.label() for e in p.exposures]) for p in prog.phases]
    assert "<SHUTTER_SPEED_LIST>1/1600," in xml
    toml = sc.to_toml(prog)
    back = sc.load_toml(toml)
    assert back.times["C2"] == prog.times["C2"]
    assert [e.label() for e in back.phases[5].exposures] == [e.label() for e in prog.phases[5].exposures]
    assert sc.xml_shutter(2.0) == "2/1" and sc.xml_shutter(1 / 3) == "1/3" and sc.xml_shutter(2.5) == "5/2"


def test_exposure_text_and_toml_example():
    e = sc.parse_exposure("1/4000 ISO100 C4")
    assert e.shutter == pytest.approx(1 / 4000) and e.iso == 100 and e.mode == "C" and e.burst == 4
    b = sc.parse_exposure("1/500 ISO400 B11")
    assert [round(1 / s) for s in b.bracket()] == [2000, 1000, 500, 250, 125]
    assert sc.parse_exposure('2" f/4').fnumber == 4 and sc.parse_ref("T0 - 300") == ("T0", -300.0)
    prog = sc.load_toml(sc.EXAMPLE_TOML)
    before, after = prog.phases
    assert len(prog.slots(before)) == 10 and prog.files == "files"
    slots = prog.slots(after)
    assert len(slots) == 5 and (slots[1] - slots[0]) / NS == pytest.approx(60)


class Clock:
    def __init__(self, t0):
        self.t = int(t0)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += int(max(s, 0.001) * NS)


class FakeCam:
    """Takes shutter + overhead seconds of the virtual clock per photo; prepare 0.2 s."""

    def __init__(self, clock, overhead=0.5, fail_on=()):
        self.clock, self.overhead, self.fail_on = clock, overhead, set(fail_on)
        self.shots, self.prepared = [], []

    def prepare(self, e, shutter):
        self.prepared.append((self.clock(), shutter, e.iso))
        self.clock.t += int(0.2 * NS)

    def release(self, shutter, files):
        rel = self.clock()
        self.clock.t += int(((shutter or 0) + self.overhead) * NS)
        self.shots.append((rel, shutter))
        if len(self.shots) in self.fail_on:
            raise RuntimeError("camera busy")
        return [f"IMG_{len(self.shots):04d}.ARW"], rel


def program(text, t0):
    prog = sc.load_toml(text)
    return prog.at(t0)


def run(prog, clock, cam, tmp_path):
    r = sc.Runner(prog, cam, clock, sleep=clock.sleep, log_dir=str(tmp_path))
    assert r.run() == "done", r.errors
    return r


def test_runner_slots_on_time_and_set_up_before(tmp_path):
    t0 = 1_786_000_000 * NS
    clock = Clock(t0)
    prog = program('''
[[phase]]
name = "lapse"
start = "NOW + 10"
every = 5
shots = 4
exposures = ["1/100 ISO800", "1 ISO1600"]
''', t0)
    cam = FakeCam(clock)
    r = run(prog, clock, cam, tmp_path)
    firsts = cam.shots[::2]
    for i, (rel, _) in enumerate(firsts):
        assert abs(rel - (t0 + (10 + 5 * i) * NS)) < 0.01 * NS          # released on the slot
    assert r.done == 8 and r.skipped == 0
    prep_first = [t for t, s, iso in cam.prepared if iso == 800]
    assert all(p < t0 + (10 + 5 * i) * NS for i, p in enumerate(prep_first))   # set up before
    rows = list(csv.reader(open(r.log_path)))
    assert rows[0][0] == "planned_utc" and len(rows) == 9 and float(rows[1][2]) == pytest.approx(0, abs=0.01)


def test_runner_skips_and_catches_up(tmp_path):
    t0 = 1_786_000_000 * NS
    clock = Clock(t0)
    # blocks every 2 s but each takes 3.7 s: every other slot is skipped; started 5 s late
    prog = program('''
[[phase]]
name = "fast"
start = "NOW - 5"
every = 2
shots = 10
exposures = ["3 ISO100"]
''', t0)
    cam = FakeCam(clock)
    r = run(prog, clock, cam, tmp_path)
    assert r.skipped >= 5 and r.done + r.skipped == 10
    assert cam.shots[0][0] - t0 < 0.5 * NS                              # the late start: at once


def test_runner_never_runs_into_the_next_phase(tmp_path):
    t0 = 1_786_000_000 * NS
    clock = Clock(t0)
    # a back-to-back list of 6 x 1 s (+0.5 s) right up to a short contact phase
    prog = program('''
[times]
C3 = "2026-08-12T12:00:00Z"
[[phase]]
name = "TotalityB"
start = "NOW"
end = "NOW + 7"
shots = -1
exposures = ["1 ISO800", "1 ISO800", "1 ISO800", "1 ISO800", "1 ISO800", "1 ISO800"]
[[phase]]
name = "Contact3"
start = "NOW + 8"
end = "NOW + 12"
shots = -1
exposures = ["1/4000 ISO100 C4"]
''', t0)
    cam = FakeCam(clock)
    r = run(prog, clock, cam, tmp_path)
    contact = [rel for rel, s in cam.shots if s == pytest.approx(1 / 4000)]
    assert contact and contact[0] - (t0 + 8 * NS) < 0.3 * NS             # on time at its start
    before = [rel for rel, s in cam.shots if s == 1]
    assert max(before) + int(1.5 * NS) <= t0 + 8 * NS and r.cut >= 1
    assert len(contact) >= 6                                              # the burst fills 4 s


def test_runner_keeps_going_after_a_failed_photo(tmp_path):
    t0 = 1_786_000_000 * NS
    clock = Clock(t0)
    prog = program('''
[[phase]]
name = "p"
start = "NOW + 1"
every = 3
shots = 3
exposures = ["1/100 ISO100"]
''', t0)
    cam = FakeCam(clock, fail_on={2})
    r = run(prog, clock, cam, tmp_path)
    assert r.done == 2 and len(r.errors) == 1 and "camera busy" in r.errors[0]
    rows = list(csv.reader(open(r.log_path)))
    assert rows[2][-1] == "camera busy"


def test_sequence_builder_and_shift():
    seq = sc.sequence(5, 30, [sc.parse_exposure("2 ISO1600")], start_in_s=10)
    prog = seq.at(1000 * NS)
    slots = prog.slots(prog.phases[0])
    assert len(slots) == 5 and slots[0] == 1010 * NS and slots[-1] == 1130 * NS
    endless = sc.sequence(0, 0, [sc.parse_exposure("1 ISO100")]).at(0)
    assert endless.slots(endless.phases[0]) is None and endless.window(endless.phases[0])[1] == 86400 * NS
    eclipse = sc.load(str(DATA / "sofimagic_nikon_d7000.xml"), day=DAY)
    test = eclipse.shifted(-eclipse.first_start() + 15 * NS)
    assert test.first_start() == 15 * NS


def test_dry_run_fast_forward_and_start_now(tmp_path):
    prog = sc.load(str(DATA / "sofimagic_nikon_d7000.xml"), day=DAY)
    now = 1_800_000_000 * NS                                        # long after the eclipse
    runner, shift = sc.dry_run(prog, now, fast=True, out_dir=str(tmp_path), name="Sony_Alpha-A7r_II")
    assert shift == 0 and runner.run() == "done"
    assert len(runner.records) == 1305 and runner.records[0]["release"] == prog.resolve(("C1", -60))
    interval = [r for r in runner.records if r["planned"]]
    assert all(abs(r["late"]) < 0.001 for r in interval)            # every slot released on time
    assert sum(runner.cuts.values()) == runner.cut and "MaxTotality" in runner.cuts
    path = sc.write_dry_run(runner)
    text = Path(path).read_text()
    assert Path(path).name.startswith("Sony_Alpha-A7r_II_dryrun_") and path.endswith("_SoFiMagic-eclipse.md")
    assert "| 1 | Partial1 | 0 | 1 | 2026-08-12 11:13:24.000 |" in text and "## Per phase" in text
    assert "| 1/1600 | 100 | 4 | S |" in text

    # C2 now: the phases before it are over, Contact2 is under way
    runner, shift = sc.dry_run(prog, now, fast=True, shift_to_now=True, ref="C2", lead_s=5.0,
                               out_dir=str(tmp_path))
    runner.run()
    phases = {r["phase"] for r in runner.records}
    assert "Partial1" not in phases and "Contact2" in phases and "Partial2" in phases
    assert runner.prog.times["C2"] == now + 5 * NS and shift == now + 5 * NS - prog.times["C2"]
    assert "programmed |" in sc.dry_run_markdown(runner)


def test_dry_run_on_time_with_a_real_clock(tmp_path):
    import time as _time
    prog = sc.sequence(3, 0.25, [sc.parse_exposure("1/100 ISO100")], start_in_s=0.1)
    now = _time.time_ns()
    runner, _ = sc.dry_run(prog, now, fast=False, overhead_s=0.05, out_dir=str(tmp_path))
    t0 = _time.monotonic()
    runner.run()
    assert len(runner.records) == 3 and 0.5 < _time.monotonic() - t0 < 2.0
    assert all(abs(r["late"]) < 0.05 for r in runner.records)
