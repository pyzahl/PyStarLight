"""Polar alignment (capture/polar.py): frames against mount.altaz, the axis from two and three
simulated plate solves of a misaligned mount, the knob turns that remove the error, the live
model (a star followed while the knobs turn, tracking about the wrong axis), the move planner.
A fake mount turns about a known axis; a gnomonic WCS stands in for the plate solution."""

import math
import time

import numpy as np
import pytest

from pystarlight.capture import mount as mt
from pystarlight.capture import polar as pa

JD = 2461366.25            # 2026-11-21 18:00 UTC
SITE = pa.Site(lat=40.9, lon=-72.9)
ARCMIN = 1 / 60


def test_frames_agree_with_mount_altaz():
    for ra, dec in ((310.36, 45.28), (37.95, 89.26), (120.0, -10.0)):
        alt, az = pa.altaz(SITE.to_enu(ra, dec, JD))
        alt2, az2 = mt.altaz(ra, dec, SITE.lat, SITE.lon, when=_dt(JD))
        assert alt == pytest.approx(alt2, abs=1e-6)
        assert (az - az2 + 180) % 360 - 180 == pytest.approx(0, abs=1e-5)
        back = SITE.to_sky(SITE.to_enu(ra, dec, JD, seen=True), JD, seen=True)
        assert pa.sep_deg(pa.unit(*back), pa.unit(ra, dec)) < 1e-6
    assert pa.altaz(SITE.pole()) == pytest.approx((SITE.lat, 0.0))
    assert pa.refraction_deg(45.0) * 60 == pytest.approx(1.0, abs=0.05)


def _dt(jd):
    from datetime import datetime, timezone
    return datetime.fromtimestamp((jd - 2440587.5) * 86400, timezone.utc)


def test_knob_turns_move_the_axis_as_named():
    p = SITE.pole()
    up = SITE.knob_rotation(0.5, 0.0) @ p
    east = SITE.knob_rotation(0.0, 0.5) @ p
    assert pa.altaz(up)[0] == pytest.approx(SITE.lat + 0.5)
    assert pa.altaz(east)[1] == pytest.approx(0.5, abs=1e-9)
    south = pa.Site(lat=-33.9, lon=151.2)
    assert pa.altaz(south.knob_rotation(0.5, 0.0) @ south.pole())[0] == pytest.approx(33.9 + 0.5)


class FakeMount:
    """A mount whose RA axis points at `axis` (ENU), synced once: it reports hour angles as if it
    were aligned, and turns physically about its own axis. The camera sits at Dec `dec` on it
    (as the mount counts), its RA axis angle `ha` (deg, hour-angle sense)."""

    def __init__(self, site, alt_err, az_err, ha, dec, jd):
        self.site = site
        self.axis = site.knob_rotation(alt_err, az_err) @ site.pole()
        self.tilt = site.knob_rotation(alt_err, az_err)    # the aligned mount, then tilted
        self.ha, self.dec, self.jd = ha, dec, jd

    def pointing(self):
        """ENU direction of the camera's centre (the aligned mount's at (ha, dec), tilted)."""
        v = pa._hadec_to_enu(self.site.lat) @ pa.unit(-self.ha, self.dec)
        return self.tilt @ v

    def point(self, jd):
        """A plate solve at jd: J2000 of the centre (refraction put in: seen there), and the mount's
        own JNow RA (from its hour angle)."""
        dt = (jd - self.jd) * 86400.0
        self.ha += pa.SIDEREAL_DEG_PER_S * dt               # tracking
        self.jd = jd
        ra, dec = self.site.to_sky(self.pointing(), jd, seen=True)
        return pa.Point(ra, dec, jd, (self.site.lst(jd) - self.ha) % 360.0, self.dec)


@pytest.mark.parametrize("site", [SITE, pa.Site(lat=-33.9, lon=151.2)])
@pytest.mark.parametrize("n", [2, 3])
def test_axis_from_points(site, n):
    m = FakeMount(site, alt_err=0.6, az_err=-1.1, ha=-35.0, dec=30.0, jd=JD)
    points = [m.point(JD)]
    for i in range(1, n):
        m.ha += 25.0                                         # the goto west
        points.append(m.point(JD + i * 60 / 86400))
    axis, info = pa.measure_axis(site, points)
    assert pa.sep_deg(axis, m.axis) * 3600 < 1.0
    err = pa.axis_error(site, axis)
    # the turns that undo the tilt (not just its negation: the knobs' turns do not commute)
    assert pa.sep_deg(site.knob_rotation(err["alt"], err["az"]) @ axis, site.pole()) * 3600 < 0.1
    assert err["alt"] == pytest.approx(-0.6, abs=0.02) and err["az"] == pytest.approx(1.1, abs=0.02)
    assert err["alt_err"] == pytest.approx(0.6, abs=0.02)
    alt, az = pa.describe(err)
    assert alt.startswith("Alt: lower 3") and az.startswith("Az: move east 6")
    if n == 3:
        assert info["two_point_diff_arcmin"] < 0.01


def test_inconsistent_points_are_refused():
    m = FakeMount(SITE, 0.2, 0.2, ha=-30.0, dec=30.0, jd=JD)
    a = m.point(JD)
    m.ha += 10.0
    m.dec += 8.0                                             # the Dec axis moved too
    b = m.point(JD + 1e-3)
    b.mount_dec = a.mount_dec
    with pytest.raises(ValueError, match="Dec axis"):
        pa.measure_axis(SITE, [a, b])


class TanWcs:
    """A gnomonic projection at (ra0, dec0), 2"/px, north up, east left, 1456 x 1088."""

    def __init__(self, ra0, dec0, scale=2.07 / 3600, size=(1456, 1088)):
        self.c, self.scale = pa.unit(ra0, dec0), scale
        self.e = np.cross((0, 0, 1), self.c)
        self.e /= np.linalg.norm(self.e)
        self.n = np.cross(self.c, self.e)
        self.x0, self.y0 = (size[0] - 1) / 2, (size[1] - 1) / 2

    def sky2pix(self, ra, dec):
        v = pa.unit(ra, dec)
        z = v @ self.c
        return (self.x0 - math.degrees((v @ self.e) / z) / self.scale,
                self.y0 - math.degrees((v @ self.n) / z) / self.scale)

    def pix2sky(self, x, y):
        xi, eta = math.radians((self.x0 - x) * self.scale), math.radians((self.y0 - y) * self.scale)
        return pa.angles(self.c + xi * self.e + eta * self.n)


def test_live_model_follows_the_knobs_to_the_pole():
    m = FakeMount(SITE, alt_err=0.4, az_err=0.7, ha=-20.0, dec=50.0, jd=JD)
    p1 = m.point(JD)
    m.ha += 30.0
    p2 = m.point(JD + 90 / 86400)
    axis, _ = pa.measure_axis(SITE, [p1, p2])
    assert pa.sep_deg(axis, m.axis) * 3600 < 0.1
    wcs = TanWcs(p2.ra, p2.dec)
    al = pa.Alignment(SITE, axis, p2.jd, wcs.pix2sky, wcs.sky2pix)
    star = wcs.pix2sky(900.0, 400.0)
    al.pick(900.0, 400.0, p2.jd)
    assert pa.sep_deg(pa.unit(*al.star), pa.unit(*star)) * 3600 < 0.01

    # the observer turns the knobs part way; the model gets the turns from the star alone
    turned = (-0.25, -0.3)
    jd = p2.jd + 120 / 86400
    x, y = al.pixel(*star, jd, turned)
    assert al.knobs_from_star(x, y, jd) == pytest.approx(turned, abs=1e-6)
    (d_alt, d_az), now = al.remaining()
    assert d_alt == pytest.approx(al.target[0] - turned[0], abs=1e-9)
    assert d_alt == pytest.approx(-0.4 - turned[0], abs=0.03)
    assert d_az == pytest.approx(-0.7 - turned[1], abs=0.03)
    assert now["total"] == pytest.approx(pa.sep_deg(SITE.knob_rotation(*turned) @ axis, SITE.pole()))
    path = al.arrow(jd)
    assert path[0] == pytest.approx((x, y), abs=1e-6)
    # at the tip of the arrow the axis is on the pole
    tip_knobs = al.knobs_from_star(*path[2], jd)
    assert pa.sep_deg(SITE.knob_rotation(*tip_knobs) @ axis, SITE.pole()) * 60 < 0.05
    s_alt, s_az = al.sensitivity(jd)
    assert s_alt > 5 and s_az > 5                    # px per arcmin of knob: well conditioned


def test_rebase_takes_the_new_solution():
    m = FakeMount(SITE, alt_err=0.3, az_err=-0.5, ha=-15.0, dec=60.0, jd=JD)
    p1 = m.point(JD)
    m.ha += 30.0
    p2 = m.point(JD + 60 / 86400)
    axis, _ = pa.measure_axis(SITE, [p1, p2])
    wcs = TanWcs(p2.ra, p2.dec)
    al = pa.Alignment(SITE, axis, p2.jd, wcs.pix2sky, wcs.sky2pix)
    # the observer turns the knobs by (-0.2, 0.3); a minute later a new solve of the centre
    turn = SITE.knob_rotation(-0.2, 0.3)
    m.tilt = turn @ m.tilt
    m.axis = turn @ m.axis
    p3 = m.point(p2.jd + 60 / 86400)
    # the solved centre (the live model leaves refraction out: its change over a knob turn is tiny)
    knobs = al.rebase(wcs.pix2sky, wcs.sky2pix, p3.jd, (wcs.x0, wcs.y0), (p3.ra, p3.dec))
    assert knobs == pytest.approx((-0.2, 0.3), abs=2e-3)
    assert pa.sep_deg(al.axis, m.axis) * 60 < 0.1 and al.knobs == (0.0, 0.0) and al.star is None


def test_plan_moves_respects_horizon_and_meridian():
    lst = SITE.lst(JD)
    ra = (lst + 30.0) % 360.0                        # 30 deg east of the meridian, Dec 20
    sign, targets, low = pa.plan_moves(SITE, ra, 20.0, JD, 30.0, 1)
    assert sign == -1                                 # west would cross the meridian
    assert targets[0][0] == pytest.approx((ra + 30.0) % 360.0)
    with pytest.raises(ValueError, match="meridian"):
        pa.plan_moves(SITE, ra, 20.0, JD, 30.0, 1, direction="west")
    sign, _, _ = pa.plan_moves(SITE, ra, 20.0, JD, 20.0, 1, direction="west", cross_meridian=True)
    assert sign == 1
    with pytest.raises(ValueError, match="altitude"):
        pa.plan_moves(SITE, ra, 20.0, JD, 60.0, 1, direction="east", min_alt=30.0)


def test_plan_moves_prefers_turning_back():
    """Repeated measurements go back and forth: prefer is taken when it fits, else ignored."""
    ra = (SITE.lst(JD) + 150.0) % 360.0               # near the pole: both ways fit
    best, _, _ = pa.plan_moves(SITE, ra, 85.0, JD, 30.0, 1)
    for prefer in (1, -1):
        assert pa.plan_moves(SITE, ra, 85.0, JD, 30.0, 1, prefer=prefer)[0] == prefer
    assert pa.plan_moves(SITE, ra, 85.0, JD, 30.0, 1, direction="west", prefer=-1)[0] == 1
    ra = (SITE.lst(JD) + 30.0) % 360.0                # west crosses the meridian
    assert pa.plan_moves(SITE, ra, 20.0, JD, 30.0, 1, prefer=1)[0] == -1
    assert best in (1, -1)


# ---- the window, on a fake mount and a fake stack / solve ----
class IndiMount:
    """The Mount panel's mount over FakeMount: reports as an aligned mount would (offsets from a
    sync), gotos turn the axes, tracking runs with the clock."""
    device = "Sim mount"

    def __init__(self, sim):
        self.sim, self.off = sim, (0.0, 0.0)
        self.gotos, self.synced = [], []

    def tick(self):
        from pystarlight.capture.polar_gui import jd_now
        now = jd_now()
        self.sim.ha += pa.SIDEREAL_DEG_PER_S * (now - self.sim.jd) * 86400.0
        self.sim.jd = now
        return now

    def position(self):
        jd = self.tick()
        return mt.jnow_to_j2000((self.sim.site.lst(jd) - self.sim.ha + self.off[0]) % 360.0,
                                self.sim.dec + self.off[1], jd)

    def goto(self, ra, dec, track=True):
        jd = self.tick()
        r, d = mt.j2000_to_jnow(ra, dec, jd)
        self.sim.ha = self.sim.site.lst(jd) - (r - self.off[0])
        self.sim.dec = d - self.off[1]
        self.gotos.append((ra, dec))

    def sync(self, ra, dec):
        jd = self.tick()
        r, d = mt.j2000_to_jnow(ra, dec, jd)
        self.off = (r - (self.sim.site.lst(jd) - self.sim.ha), d - self.sim.dec)
        self.synced.append((ra, dec))

    def slewing(self):
        return False

    def set_tracking(self, on):
        pass


class Follower:
    def __init__(self):
        self.state, self.star, self.note, self.asked = "idle", None, "", []

    def ask(self, *request):
        self.asked.append(request)
        self.state = "idle" if request[0] == "stop" else self.state


def test_window_measures_and_guides(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    try:
        from pystarlight.capture.qt import QtCore, QtWidgets
    except ImportError as e:
        pytest.skip(f"no Qt: {e}")
    from types import SimpleNamespace

    from pystarlight.capture import polar_gui as pg
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])   # noqa: F841
    for fmt in (QtCore.QSettings.NativeFormat, QtCore.QSettings.IniFormat):
        QtCore.QSettings.setPath(fmt, QtCore.QSettings.UserScope, str(tmp_path / "settings"))
    monkeypatch.setattr(pg, "SETTLE_S", 0.0)
    monkeypatch.setattr(pg.localconfig, "config_path", lambda: None)

    jd0 = pg.jd_now()
    sim = FakeMount(SITE, alt_err=0.5, az_err=-0.8, ha=-40.0, dec=35.0, jd=jd0)
    mount = IndiMount(sim)
    solved = {}

    main = QtWidgets.QWidget()
    main.settings = QtCore.QSettings("PyStarLight", "camera")
    stacker = SimpleNamespace(state="idle", path=None, error=None, cancel=lambda: None)
    main.c = SimpleNamespace(polar=Follower(), stacker=stacker)
    main.view = SimpleNamespace(img_size=(1456, 1088), polar=None, update=lambda: None,
                                pick_callback=None, setCursor=lambda c: None)
    main.mount_dock = SimpleNamespace(mount=mount, client=SimpleNamespace(connected=True), job=None,
                                      site=lambda: (SITE.lat, SITE.lon, 0.0))
    main.lock_dock = None
    main.solve_count, main.solve_thread, main.solved_path = 0, None, None
    main.solution, main.solve_log = None, ""

    def on_stack_button():
        stacker.state, stacker.path = "done", str(tmp_path / f"stack{main.solve_count}.fits")

    def start_solve(path):
        p = sim.point(mount.tick())
        sol = {"center": {"ra": p.ra, "dec": p.dec}, "width": 1456, "height": 1088,
               "stack": path, "wcs": path}
        solved[path] = (p.jd, TanWcs(p.ra, p.dec))
        main.solution, main.solved_path = sol, path
        main.solve_count += 1
    main.on_stack_button, main.start_solve, main.continue_live = on_stack_button, start_solve, lambda: None
    monkeypatch.setattr(pg, "jd_of_stack", lambda sol: solved[sol["stack"]][0])
    monkeypatch.setattr(pg, "wcs_functions", lambda sol: (solved[sol["stack"]][1].pix2sky,
                                                         solved[sol["stack"]][1].sky2pix))

    w = pg.PolarWindow(main)
    w.timer.stop()
    w.points_combo.setCurrentIndex(w.points_combo.findData(3))
    w.step_spin.setValue(20.0)
    w.on_start()
    for _ in range(50):
        w.poll()
        time.sleep(0.01)                     # the sync runs in a thread
        if w.alignment is not None:
            break
    assert w.alignment is not None, w.error
    assert len(mount.synced) == 1 and len(mount.gotos) == 2
    # east of the meridian: turned east (west would cross it)
    assert w.info["ha_steps_deg"][0] == pytest.approx(-20.0, abs=0.2)
    assert pa.sep_deg(w.alignment.axis, sim.axis) * 60 < 0.05
    assert w.alt_label.text().startswith("Alt: lower 30")
    assert w.az_label.text().startswith("Az: move east 4")   # 0.8 deg in azimuth, ~48'
    assert main.c.polar.asked[-1] == ("center",)

    # the follower finds a star; the knobs turn half way; the window says what is left
    f = main.c.polar
    f.state, f.star = "measuring", (700.0, 500.0)
    w.poll()
    al = w.alignment
    assert al.star is not None and main.view.polar["path"][0] == pytest.approx((700.0, 500.0), abs=0.05)
    half = (al.target[0] / 2, al.target[1] / 2)
    f.star = al.pixel(*al.star, pg.jd_now(), half)
    w.poll()
    assert al.knobs == pytest.approx(half, abs=1e-3)
    assert "lower 15" in w.alt_label.text()
    f.star = al.pixel(*al.star, pg.jd_now(), al.target)
    w.poll()
    assert "aligned" in w.total_label.text()
    w.shutdown()

