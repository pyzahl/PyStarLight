"""Minimal matplotlib viewer: step through a movie, see the timestamp, place apertures, run.

    pystarlight-view OTEVideo/run_1/run_1.ravf

Mouse   left click     add aperture (snaps to the star's brightest pixels)
        right click    remove the nearest aperture
Keys    left / right   previous / next frame      page up / down  -/+ 25 frames
        home / end     first / last frame
        t r c k        give the aperture under the mouse a role (defaults; set in the config):
                       t target, r reference, c check star, k sky (star-free)
        Y R G W        PyMovie colours (shift; used when analysis.mode = "pymovie")
        n              rename nearest aperture (in the terminal)
        1 / 2          show the aperture under the mouse in zoom view 1 / 2 (right side)
        a              run the analysis from this frame to the end
        s / l          save / load apertures (<movie>.apertures.json)
        p              plate-solve again (otherwise automatic on opening, cached as <movie>.wcs.fit)
        i              identify the stars in the apertures (astrometry.net names, SIMBAD); also automatic
Toolbar zoom/pan works as usual.

Results are written next to the movie: <movie>.csv (PyMovie layout, opens in PyOTE),
<movie>.pystarlight.csv (all columns) and <movie>-summary.pdf/png.
Settings come from the config file (pystarlight-config init / show).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import PowerNorm
from matplotlib.patches import Rectangle
from matplotlib.widgets import Button, CheckButtons, RangeSlider, Slider

from . import config as config_mod
from . import plot
from .calibrate import Calibration
from .movie import open_movie
from .photometry import PhotometryConfig, brightest_pixel, circular_mask
from .pipeline import analyze
from .tracking import Aperture

AP_FIELDS = ('name', 'x0', 'y0', 'size', 'color', 'thresh', 'default_mask_radius', 'role')
PYMOVIE_KEYS = {'Y': 'yellow', 'R': 'red', 'G': 'green', 'W': 'white'}
NAV_KEYS = ('left', 'right', 'pageup', 'pagedown', 'home', 'end', 'n', 'a', 's', 'l', 'p', 'i', '1', '2')


def free_matplotlib_keys(keys):
    """Remove our keys from matplotlib's built-in key bindings (e.g. 'k' = log x axis,
    's' = save dialog, 'left' = back), so they reach the viewer. 'f' (full screen),
    'o' (zoom) and 'q' (quit) keep their matplotlib meaning unless the config uses them."""
    for name in [k for k in plt.rcParams if k.startswith('keymap.')]:
        plt.rcParams[name] = [k for k in plt.rcParams[name] if k not in keys]


def save_apertures(path, apertures, wcs=None, stars=None):
    """JSON list of apertures; with a plate solution also each centre's RA/Dec (deg, J2000),
    and with `stars` ({aperture name: FieldStar}) the catalogue identification."""
    from dataclasses import asdict
    out = []
    for a in apertures:
        d = {k: getattr(a, k) for k in AP_FIELDS}
        if wcs is not None:
            ra, dec = (float(v) for v in wcs.all_pix2world(*a.center, 0))
            d.update(ra=round(ra, 6), dec=round(dec, 6))
        if stars and a.name in stars:
            d['star'] = asdict(stars[a.name])
        out.append(d)
    with open(path, 'w') as f:
        json.dump(out, f, indent=1)


def load_apertures(path) -> list[Aperture]:
    with open(path) as f:
        return [Aperture(**{k: v for k, v in d.items() if k in AP_FIELDS}) for d in json.load(f)]


def load_aperture_stars(path) -> dict:
    """{aperture name: FieldStar} saved by save_apertures(..., stars=...)."""
    from .wcs.identify import FieldStar
    with open(path) as f:
        return {d['name']: FieldStar(**d['star']) for d in json.load(f) if d.get('star')}


STAR_APERTURE_ROLES = ('target', 'reference', 'check')
LIVE_ZOOM_INTERVAL = 0.5        # s between zoom-view updates while analysing


def readings_of(value) -> dict:
    """One aperture's measurement as a dict, from either analysis mode's progress values."""
    from .globaltrack import SKY_COLUMNS, STAR_COLUMNS
    if isinstance(value, dict):
        return value
    if isinstance(value, tuple):
        cols = STAR_COLUMNS if len(value) == len(STAR_COLUMNS) else SKY_COLUMNS
        return dict(zip(cols, value))
    return {k: getattr(value, k) for k in ('signal', 'bkavg', 'bkstd', 'mskpx', 'xc', 'yc')}


def readout_text(r: dict, frame=None) -> str:
    """Two short lines for a zoom view: frame and signal; fit (global) or background (PyMovie)."""
    def ok(v):
        return v is not None and np.isfinite(v)
    head = f'fr {frame:.0f}  ' if ok(frame) else ''
    if 'signal' not in r:                                   # sky aperture
        return head + f'sky {r.get("bkavg", np.nan):.0f} ± {r.get("bkstd", np.nan):.1f}'
    line1 = head + f'S {r["signal"]:,.0f}'
    if 'fwhm' in r:                                         # global mode: peak fit
        if ok(r.get('fwhm')):
            line2 = f'fit FWHM {r["fwhm"]:.1f}px  SNR {r["peak_snr"]:.0f}'
            if r.get('dimmed'):
                line2 += '  faded'
            elif not r.get('used'):
                line2 += '  not tracking'
        else:
            line2 = 'fit: no star (faded / lost)'
    else:                                                   # PyMovie mode
        line2 = f'bkg {r["bkavg"]:.0f} ± {r["bkstd"]:.1f}  px {r["mskpx"]:.0f}'
    return line1 + '\n' + line2


def run_analysis(movie_path, apertures, start, stop, cfg, calibration=None, progress=None):
    """Analysis as configured: 'global' when roles are set and mode is global, else PyMovie."""
    a = cfg['analysis']
    with open_movie(str(movie_path)) as mv:
        if a['mode'] == 'global' and any(ap.role in ('target', 'reference', 'check') for ap in apertures):
            from .globaltrack import analyze_global
            return analyze_global(mv, apertures, start, stop, radius=a['radius'] or None,
                                  background=a['background'], search=a['search'],
                                  rotation=a['rotation'], min_snr=a['min_snr'],
                                  dim_fraction=a['dim_fraction'], target_drift=a['target_drift'],
                                  calibration=calibration, progress=progress)
        return analyze(mv, apertures, start, stop, cfg=PhotometryConfig(), calibration=calibration,
                       progress=progress)


class Viewer:
    def __init__(self, path: str, cfg: Optional[dict] = None, calibration: Optional[Calibration] = None):
        self.cfg = cfg or config_mod.load()
        self.roles = self.cfg['apertures']['roles']
        self.role_keys = {r['key']: name for name, r in self.roles.items()}
        free_matplotlib_keys(set(self.role_keys) | set(PYMOVIE_KEYS) | set(NAV_KEYS))
        self.path = Path(path)
        self.movie = open_movie(str(path))
        self.n = len(self.movie)
        self.size = int(self.cfg['apertures']['size'])
        self.calibration = calibration
        self.apertures: list[Aperture] = []
        self.index = 0
        self._job: Optional[threading.Thread] = None
        self._progress = (0, 0)
        self._live = None             # (frame, {name: values}) latest result while analysing
        self._live_shown = 0.0
        self._live_frame = None
        self._lc = None               # last analysis result
        self._result = None
        self._error = None

        self.fig = plt.figure(figsize=(12, 7.5))
        self.fig.canvas.manager.set_window_title(f'PyStarLight - {self.path.name}')
        self.ax = self.fig.add_axes([0.04, 0.24, 0.80, 0.70])     # right margin: key cheat sheet
        self.info = self.fig.text(0.04, 0.955, '', family='monospace', fontsize=10)
        self.status = self.fig.text(0.04, 0.012, '', fontsize=9, color='0.3')   # own row below the buttons

        frame = self.movie.read(0)
        self.jump = self.frames_per(10.0)
        # display: levels (auto = zscale, or manual), optional auto per frame, display gamma
        dc = self.cfg.get('display', {})
        self.gamma = float(dc.get('gamma', 1.0))
        self.levels_auto = not isinstance(dc.get('levels', 'auto'), list)
        self.per_frame = bool(dc.get('per_frame', False))
        self.lo, self.hi = (plot.zscale(frame.image) if self.levels_auto
                            else (float(dc['levels'][0]), float(dc['levels'][1])))
        self.im = self.ax.imshow(frame.image, cmap='gray', interpolation='nearest',
                                 norm=PowerNorm(self.gamma, self.lo, self.hi, clip=True))
        self.patches = []

        vmin, vmax = self._useful_range(frame.image, self.lo, self.hi)
        self._setting_levels = False
        self.levels = RangeSlider(self.fig.add_axes([0.12, 0.185, 0.36, 0.022]), 'levels',
                                  vmin, vmax, valinit=(self.lo, self.hi), valfmt='%.0f')
        self.levels.on_changed(self._on_levels)
        self.gamma_slider = Slider(self.fig.add_axes([0.12, 0.152, 0.36, 0.022]), 'gamma', 0.2, 3.0,
                                   valinit=self.gamma, valfmt='%.2f')
        self.gamma_slider.on_changed(self._on_gamma)
        self._auto_button = Button(self.fig.add_axes([0.61, 0.172, 0.05, 0.036]), 'Auto')
        self._auto_button.on_clicked(lambda e: self.auto_levels())
        self._per_frame_box = CheckButtons(self.fig.add_axes([0.665, 0.165, 0.09, 0.05]), ['per frame'],
                                           [self.per_frame])
        self._per_frame_box.on_clicked(self._on_per_frame)

        self.slider = Slider(self.fig.add_axes([0.2, 0.115, 0.6, 0.025]), 'frame', 0, self.n - 1,
                             valinit=0, valstep=1, valfmt='%d')
        self.slider.on_changed(lambda v: self.show(int(v)))
        buttons = [('|<', lambda e: self.go(0)),
                   ('-10 s', lambda e: self.go(self.index - self.jump)),
                   ('<', lambda e: self.go(self.index - 1)),
                   ('>', lambda e: self.go(self.index + 1)),
                   ('+10 s', lambda e: self.go(self.index + self.jump)),
                   ('>|', lambda e: self.go(self.n - 1)),
                   ('Run', lambda e: self.run())]
        self._buttons = []
        for i, (label, cb) in enumerate(buttons):
            b = Button(self.fig.add_axes([0.14 + i * 0.075, 0.055, 0.065, 0.035]), label)
            b.on_clicked(cb)
            self._buttons.append(b)
        self.fig.text(0.855, 0.94, self.cheat_sheet(), family='monospace', fontsize=8, va='top',
                      color='0.25', linespacing=1.35)

        # two zoomed aperture views (keys 1 / 2 over an aperture), ~4-5x the main image
        self.zoom_names: list[Optional[str]] = [None, None]
        self.zoom_axes, self.zoom_ims, self.zoom_marks = [], [], []
        for k, y in enumerate((0.30, 0.025)):
            zax = self.fig.add_axes([0.855, y, 0.13, 0.20])
            zax.set_xticks([])
            zax.set_yticks([])
            zax.set_title(f'{k + 1}: (press {k + 1} over an aperture)', fontsize=8, loc='left')
            self.zoom_axes.append(zax)
            self.zoom_ims.append(zax.imshow(np.zeros((2, 2)), cmap='gray', interpolation='nearest'))
            self.zoom_marks.append([])
        self._track = None            # tracked centres from the last run: {name: (frames, xc, yc)}

        self.fig.canvas.mpl_connect('button_press_event', self.on_click)
        self.fig.canvas.mpl_connect('key_press_event', self.on_key)
        self.timer = self.fig.canvas.new_timer(interval=250)
        self.timer.add_callback(self.poll)

        self.ap_stars = {}            # aperture name -> FieldStar (catalogue identification)
        if self.aperture_file.exists():
            self.apertures = load_apertures(self.aperture_file)
            self.ap_stars = load_aperture_stars(self.aperture_file)
        self._ident_job = None
        self._ident_msg = ''
        self.show(0)
        keys = ' '.join(f'{r["key"]}:{name}' for name, r in self.roles.items())
        self.say(f'click: add aperture, right click: remove, roles {keys}, a: run, s/l: save/load')

        # plate solution: RA/Dec under the cursor and field orientation
        self.wcs = self.orient = None
        self.stars = []               # brightest stars, identified (wcs.field_stars)
        self._star_marks = []
        self.ax.format_coord = self.format_coord
        self._solve_job = None
        self._solve_msg = ''
        acfg = self.cfg['astrometry']
        if acfg.get('auto_solve') and acfg.get('api_key'):
            self.start_solve()

    # ---------------------------------------------------------------- display

    @property
    def aperture_file(self) -> Path:
        return self.path.with_suffix('.apertures.json')

    def say(self, msg):
        self.status.set_text(msg)
        self.fig.canvas.draw_idle()

    def frames_per(self, seconds: float) -> int:
        """Frames in `seconds`, from the timestamps (else the file's frame rate, else 6 fps,
        typical of the 4-10 fps occultation cameras)."""
        from .timestamps import parse_time
        k = min(10, self.n - 1)
        if k > 0:
            t0, t1 = parse_time(self.movie.read(0).time), parse_time(self.movie.read(k).time)
            if np.isfinite(t0) and np.isfinite(t1) and t1 > t0:
                return max(1, round(seconds * k / (t1 - t0)))
        fps = getattr(self.movie, 'fps', None) or 6.0
        return max(1, round(seconds * fps))

    def cheat_sheet(self) -> str:
        roles = '\n'.join(f"  {r['key']}  {name}" for name, r in self.roles.items())
        return ('KEYS\n'
                'click/r-click add/remove\n'
                'over an aperture:\n'
                f'{roles}\n'
                '  n rename  1 2 zoom view\n'
                '<- ->  1 frame  PgUp/Dn 25\n'
                f'buttons 10 s ({self.jump} fr)\n'
                'a run  s/l save/load\n'
                'p plate-solve again\n'
                'i identify (SIMBAD)\n'
                'f full screen  o zoom\n'
                'levels/gamma: sliders\n'
                'Y R G W PyMovie colours')

    def go(self, i):
        self.slider.set_val(int(np.clip(i, 0, self.n - 1)))   # calls show()

    # ---------------------------------------------------------------- display levels / gamma

    @staticmethod
    def _useful_range(image, lo, hi):
        """Levels slider range: the useful part of the histogram, not up to saturated stars."""
        p_lo, p_hi = (float(v) for v in np.percentile(image[::4, ::4], (0.1, 99.9)))
        span = max(hi - lo, 1.0)
        return min(p_lo, lo), max(p_hi, hi + 4 * span)

    def _norm(self, lo, hi):
        return PowerNorm(self.gamma, lo, max(hi, lo + 1e-6), clip=True)

    def set_levels(self, lo, hi, manual=True):
        """Black / white point of the main image. manual=False: set by auto levels."""
        self.lo, self.hi = float(lo), float(hi)
        if manual:
            self.levels_auto = False
        self._setting_levels = True
        try:
            if hasattr(self, 'levels'):
                ax_min, ax_max = self.levels.valmin, self.levels.valmax
                if self.lo < ax_min or self.hi > ax_max:      # grow the slider range if needed
                    self.levels.valmin, self.levels.valmax = min(ax_min, self.lo), max(ax_max, self.hi)
                    self.levels.ax.set_xlim(self.levels.valmin, self.levels.valmax)
                self.levels.set_val((self.lo, self.hi))
        finally:
            self._setting_levels = False
        self.im.set_norm(self._norm(self.lo, self.hi))
        self.fig.canvas.draw_idle()

    def auto_levels(self):
        """zscale levels of the current frame; back to automatic mode."""
        self.levels_auto = True
        lo, hi = plot.zscale(self.image)
        if hasattr(self, 'levels'):                          # also reset a grown slider range
            self.levels.valmin, self.levels.valmax = self._useful_range(self.image, lo, hi)
            self.levels.ax.set_xlim(self.levels.valmin, self.levels.valmax)
        self.set_levels(lo, hi, manual=False)

    def _on_levels(self, val):
        if not self._setting_levels:                          # user dragged: manual levels
            self.set_levels(*val, manual=True)

    def _on_gamma(self, g):
        self.gamma = float(g)
        self.im.set_norm(self._norm(self.lo, self.hi))
        self.update_zooms()

    def _on_per_frame(self, _label):
        self.per_frame = not self.per_frame
        if self.per_frame:
            self.auto_levels()

    def show(self, i):
        self.index = i
        fr = self.movie.read(i)
        self.image = fr.image
        self.im.set_data(fr.image)
        if self.per_frame and self.levels_auto and hasattr(self, 'levels'):
            self.set_levels(*plot.zscale(fr.image), manual=False)
        t = f'{fr.date or ""} {fr.time or "(no timestamp)"}'.strip()
        self.info.set_text(f'frame {i:6d} / {self.n - 1}    UTC {t}')
        self.draw_apertures()
        self.update_zooms()

    def centre_of(self, a) -> tuple[float, float]:
        """Aperture centre in the current frame: tracked position after a run, else placed."""
        if self._track and a.name in self._track:
            frames, xc, yc = self._track[a.name]
            j = int(np.clip(np.searchsorted(frames, self.index), 0, len(frames) - 1))
            if frames[j] == self.index and np.isfinite(xc[j]):
                return float(xc[j]), float(yc[j])
        return a.x0 + a.size / 2 - 0.5, a.y0 + a.size / 2 - 0.5

    def _lc_readings(self, frame) -> dict:
        """{name: readings} of the last analysis at `frame` (empty if not analysed)."""
        lc = self._lc
        if lc is None:
            return {}
        out = {}
        for n in lc.names:
            fr = lc[n]['frame']
            j = int(np.searchsorted(fr, frame))
            if j < len(fr) and fr[j] == frame:
                out[n] = {c: float(v[j]) for c, v in lc[n].items()}
        return out

    def update_zooms(self, image=None, readings=None, frame=None):
        """Redraw the zoom views. image/readings/frame: a frame being analysed (live view);
        default: the displayed frame and, after a run, its stored readings."""
        if not hasattr(self, 'zoom_axes'):
            return
        live = image is not None
        image = self.image if image is None else image
        frame = self.index if frame is None else frame
        readings = self._lc_readings(frame) if readings is None else readings
        radius = None
        if self._lc is not None and 'radius' in self._lc.frame_info and len(self._lc.frame_info['radius']):
            radius = float(self._lc.frame_info['radius'][0])
        h, w = image.shape
        by_name = {a.name: a for a in self.apertures}
        for k, zax in enumerate(self.zoom_axes):
            for m in self.zoom_marks[k]:
                m.remove()
            self.zoom_marks[k] = []
            a = by_name.get(self.zoom_names[k])
            if a is None:                       # unassigned, or the aperture was removed
                self.zoom_names[k] = None
                self.zoom_ims[k].set_data(np.zeros((2, 2)))
                zax.set_title(f'{k + 1}: (press {k + 1} over an aperture)', fontsize=8, loc='left',
                              color='0.3')
                continue
            r = readings.get(a.name, {})
            if np.isfinite(r.get('xc', np.nan)) and np.isfinite(r.get('yc', np.nan)):
                cx, cy = r['xc'], r['yc']
            else:
                cx, cy = self.centre_of(a)
            half = a.size // 2
            x0 = int(np.clip(round(cx) - half, 0, w - a.size))
            y0 = int(np.clip(round(cy) - half, 0, h - a.size))
            cut = image[y0:y0 + a.size, x0:x0 + a.size]
            lo, hi = plot.zscale(cut)
            hi = max(hi, float(np.percentile(cut, 99.5)))      # keep the star's core visible
            im = self.zoom_ims[k]
            im.set_data(cut)
            im.set_norm(PowerNorm(self.gamma, lo, max(hi, lo + 1e-6), clip=True))
            im.set_extent((x0 - 0.5, x0 + a.size - 0.5, y0 + a.size - 0.5, y0 - 0.5))
            zax.set_xlim(x0 - 0.5, x0 + a.size - 0.5)
            zax.set_ylim(y0 + a.size - 0.5, y0 - 0.5)
            col = self.color_of(a)
            self.zoom_marks[k] += [zax.axhline(cy, color=col, lw=0.5, alpha=0.7),
                                   zax.axvline(cx, color=col, lw=0.5, alpha=0.7)]
            if radius and a.role != 'sky':
                from matplotlib.patches import Circle
                self.zoom_marks[k].append(zax.add_patch(Circle((cx, cy), radius, fill=False, ec=col,
                                                               lw=0.6, alpha=0.8)))
            if np.isfinite(r.get('x_fit', np.nan)):             # where the peak fit found the star
                self.zoom_marks[k] += zax.plot([r['x_fit']], [r['y_fit']], '+', color='cyan', ms=7, mew=1)
            if r:
                self.zoom_marks[k].append(zax.text(
                    0.02, 0.02, readout_text(r, frame), transform=zax.transAxes, fontsize=7,
                    color='white', va='bottom', family='monospace',
                    bbox=dict(boxstyle='square,pad=0.2', fc='black', ec='none', alpha=0.6)))
            if live:
                self.zoom_marks[k].append(zax.text(0.98, 0.98, 'live', transform=zax.transAxes, fontsize=7,
                                                   color='orange', ha='right', va='top'))
            if self.wcs is None:
                title = f'{k + 1}: {a.name}  ({cx:.1f}, {cy:.1f})'
            else:       # name (+ star id) / RA Dec; the pixel position is in the cursor readout
                st = self.ap_stars.get(a.name) or next(
                    (st for st in self.stars if np.hypot(st.x - cx, st.y - cy) < max(5, 2 * st.fwhm)), None)
                title = f'{k + 1}: {a.name}'
                if st is not None:
                    title += (f'  V={st.vmag:.2f}' if st.vmag is not None else '') + f'\n{st.name or "?"}'
                # the solution belongs to the solved (first) frames: for an identified star show its
                # fixed position; otherwise the aperture centre is only right before the field drifts
                title += '\n' + (st.radec if st is not None and np.isfinite(st.ra) else self.sky_of(cx, cy))
            zax.set_title(title, fontsize=7.5, loc='left', color=col)
        self.fig.canvas.draw_idle()

    def color_of(self, a) -> str:
        if a.role in self.roles:
            c = self.roles[a.role]['color']
            return plot.APERTURE_COLORS.get(c, c)
        return plot.APERTURE_COLORS.get(a.color, a.color)

    def draw_apertures(self):
        for p in self.patches:
            p.remove()
        self.patches = []
        for a in self.apertures:
            col = self.color_of(a)
            self.patches.append(self.ax.add_patch(
                Rectangle((a.x0 - 0.5, a.y0 - 0.5), a.size, a.size, fill=False, ec=col, lw=1)))
            st = getattr(self, 'ap_stars', {}).get(a.name)
            label = a.name + (f'  {st.label()}' if st is not None and st.name else '')
            self.patches.append(self.ax.text(a.x0 + a.size + 2, a.y0, label, color=col,
                                             fontsize=8, va='top'))
        self.fig.canvas.draw_idle()

    # ---------------------------------------------------------------- plate solution

    def start_solve(self, force: bool = False):
        from .wcs.solve import solve_movie

        def work():
            try:
                self.wcs, self.orient = solve_movie(str(self.path), self.cfg, log=self._solve_log,
                                                    force=force)
                from .wcs.solve import field_stars
                try:
                    self.stars = field_stars(str(self.path), self.wcs, self.cfg, n=3,
                                             log=self._solve_log, force=force)
                except Exception as e:      # names are a bonus
                    self._solve_msg = f'star identification failed: {e}'
            except Exception as e:
                self._solve_msg = f'plate solve failed: {e}'
        self._solve_job = threading.Thread(target=work, daemon=True)
        self._solve_job.start()
        self._solve_timer = self.fig.canvas.new_timer(interval=500)
        self._solve_timer.add_callback(self._poll_solve)
        self._solve_timer.start()

    def _solve_log(self, msg):
        self._solve_msg = msg

    def _poll_solve(self):
        if self._solve_job is not None and self._solve_job.is_alive():
            self.say(f'plate solving: {self._solve_msg}')
            return
        self._solve_timer.stop()
        self._solve_job = None
        if self.orient is None:
            self.say(self._solve_msg)
            return
        self.draw_compass()
        self.draw_stars()
        names = '; '.join(s.label() for s in self.stars)
        self.say('plate solved: ' + self.orient.describe() + (f'   |   brightest: {names}' if names else ''))
        self.update_zooms()
        if self._unidentified():
            self.start_identify()

    # ---------------------------------------------------------------- catalogue identification

    def _star_apertures(self):
        return [a for a in self.apertures
                if a.role in STAR_APERTURE_ROLES or (not a.role and a.color != 'white')]

    def _unidentified(self):
        if self.wcs is None:
            return []
        return [a for a in self._star_apertures() if a.name not in self.ap_stars]

    def identify_apertures(self, apertures=None, frame=None, log=None):
        """Name the stars in the apertures (blocking; called from worker threads).

        The plate solution belongs to the stacked first frames, so the drift between that stack
        and the frame the apertures sit on is measured first (phase correlation)."""
        from skimage.registration import phase_cross_correlation

        from .wcs.identify import identify_positions
        from .wcs.solve import solution_path, stack_frames
        log = log or self._ident_log
        apertures = apertures if apertures is not None else self._star_apertures()
        frame = self.index if frame is None else frame
        if self.wcs is None or not apertures:
            return {}
        meta_p = solution_path(self.path).with_suffix('.json')
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
        with open_movie(str(self.path)) as mv:
            stack = stack_frames(mv, int(meta.get('stack_start', 0)),
                                 int(self.cfg['astrometry']['stack_frames']))
            cur = mv.read(frame).image.astype(np.float32)
        (dy, dx), _, _ = phase_cross_correlation(stack, cur, upsample_factor=10, normalization=None)
        log(f'field shift to the solved frames: dx={dx:+.1f} dy={dy:+.1f} px')
        pos = {}
        for a in apertures:
            cx, cy = self.centre_of(a) if frame == self.index else (a.center[0], a.center[1])
            pos[a.name] = (cx + dx, cy + dy)
        found = identify_positions(stack, self.wcs, pos, job_id=meta.get('job_id'), log=log)
        self.ap_stars.update(found)
        return found

    def start_identify(self):
        if self._ident_job is not None or self.wcs is None:
            return

        def work():
            try:
                found = self.identify_apertures()
                self._ident_msg = '; '.join(f'{n}: {st.label()}' for n, st in found.items()) or 'no apertures'
            except Exception as e:
                self._ident_msg = f'identification failed: {e}'
        self._ident_job = threading.Thread(target=work, daemon=True)
        self._ident_job.start()
        self._ident_timer = self.fig.canvas.new_timer(interval=500)
        self._ident_timer.add_callback(self._poll_identify)
        self._ident_timer.start()

    def _ident_log(self, msg):
        self._ident_msg = msg

    def _poll_identify(self):
        if self._ident_job is not None and self._ident_job.is_alive():
            self.say(f'identifying: {self._ident_msg}')
            return
        self._ident_timer.stop()
        self._ident_job = None
        if self.ap_stars:
            save_apertures(self.aperture_file, self.apertures, self.wcs, self.ap_stars)
        self.draw_apertures()
        self.draw_stars()
        self.update_zooms()
        self.say(f'identified: {self._ident_msg}')

    def all_stars(self):
        """Brightest stars plus the identified aperture stars (no duplicates), for the exports."""
        out = list(self.stars)
        for st in self.ap_stars.values():
            if not any(np.hypot(o.x - st.x, o.y - st.y) < 3 for o in out):
                out.append(st)
        return out

    def format_coord(self, x, y):
        s = f'x={x:.1f} y={y:.1f}'
        if self.wcs is not None:
            from astropy.coordinates import SkyCoord
            ra, dec = (float(v) for v in self.wcs.all_pix2world(x, y, 0))
            c = SkyCoord(ra, dec, unit='deg')
            s += '   ' + c.to_string('hmsdms', precision=1, sep=':')
        return s

    def draw_stars(self):
        """Label the identified brightest stars on the image."""
        for m in self._star_marks:
            m.remove()
        self._star_marks = []
        named = [self.ap_stars[a.name] for a in self.apertures if a.name in self.ap_stars]
        for k, st in enumerate(self.stars, 1):
            if any(np.hypot(n.x - st.x, n.y - st.y) < 3 for n in named):
                continue                # the aperture label already names it
            self._star_marks.append(self.ax.annotate(
                f'{k}: {st.label()}', (st.x, st.y), xytext=(12, 12), textcoords='offset points',
                color='orange', fontsize=8, arrowprops=dict(arrowstyle='-', color='orange', lw=0.6)))
        self.fig.canvas.draw_idle()

    def sky_of(self, x, y) -> str:
        if self.wcs is None:
            return ''
        from astropy.coordinates import SkyCoord
        ra, dec = (float(v) for v in self.wcs.all_pix2world(x, y, 0))
        return SkyCoord(ra, dec, unit='deg').to_string('hmsdms', precision=1, sep=':')

    def draw_compass(self):
        """N and E arrows in the upper-left corner, from the plate solution."""
        if self.wcs is None:
            return
        h, w = self.image.shape
        x0, y0 = 0.08 * w, 0.12 * h
        ra, dec = (float(v) for v in self.wcs.all_pix2world(x0, y0, 0))
        L = 0.06 * w
        for lab, (r, d) in (('N', (ra, dec + 1 / 60)), ('E', (ra + 1 / 60 / np.cos(np.radians(dec)), dec))):
            xe, ye = (float(v) for v in self.wcs.all_world2pix(r, d, 0))
            v = np.array([xe - x0, ye - y0])
            v = v / np.hypot(*v) * L
            self.ax.annotate('', (x0 + v[0], y0 + v[1]), (x0, y0),
                             arrowprops=dict(arrowstyle='->', color='orange', lw=1.2))
            self.ax.text(x0 + 1.25 * v[0], y0 + 1.25 * v[1], lab, color='orange', ha='center', va='center')
        self.fig.canvas.draw_idle()

    # ---------------------------------------------------------------- editing

    def nearest(self, x, y) -> Optional[Aperture]:
        if not self.apertures:
            return None
        return min(self.apertures, key=lambda a: np.hypot(a.center[0] - x, a.center[1] - y))

    def _free_name(self, base: str, numbered: bool = True) -> str:
        names = {a.name for a in self.apertures}
        if not numbered and base not in names:
            return base
        k = 1
        while f'{base}{k}' in names:
            k += 1
        return f'{base}{k}'

    def add(self, x, y):
        """New aperture centred on the star's brightest pixels near the click."""
        c = self.size // 2
        h, w = self.image.shape
        x0 = int(np.clip(round(x) - c, 0, w - self.size))
        y0 = int(np.clip(round(y) - c, 0, h - self.size))
        mask, _ = circular_mask(self.size, 8)
        try:
            cx, cy = brightest_pixel(mask * self.image[y0:y0 + self.size, x0:x0 + self.size], 5)
            x0 = int(np.clip(round(x0 + cx) - c, 0, w - self.size))
            y0 = int(np.clip(round(y0 + cy) - c, 0, h - self.size))
        except ValueError:
            pass
        pm = self.cfg['pymovie']
        self.apertures.append(Aperture(self._free_name('ap'), x0, y0, self.size,
                                       thresh=pm['threshold'], default_mask_radius=pm['default_mask_radius']))
        self.draw_apertures()

    def place_sky(self, x, y):
        """Sky apertures go exactly where clicked (no snapping onto stars)."""
        c = self.size // 2
        h, w = self.image.shape
        x0 = int(np.clip(round(x) - c, 0, w - self.size))
        y0 = int(np.clip(round(y) - c, 0, h - self.size))
        return x0, y0

    def set_role(self, x, y, role):
        a = self.nearest(x, y)
        if a is None:
            return
        r = self.roles[role]
        holders = [b for b in self.apertures if b.role == role and b is not a]
        if r['max'] and len(holders) >= r['max']:
            if r['max'] == 1:                       # move the single role (e.g. target) here
                for b in holders:
                    b.role = ''
                    b.name = self._free_name('ap')
            else:
                self.say(f'at most {r["max"]} {role} apertures')
                return
        a.role = role
        a.name = self._free_name(r['name'], numbered=r['max'] != 1)
        if role == 'sky':
            a.x0, a.y0 = self.place_sky(x, y)
        self.draw_apertures()

    def set_color(self, x, y, color):
        a = self.nearest(x, y)
        if a is None:
            return
        if color == 'yellow' and a.color != 'yellow':
            if sum(b.color == 'yellow' for b in self.apertures) >= 2:
                self.say('at most two yellow (tracking) apertures')
                return
        a.color = color
        a.primary_yellow = False
        self.draw_apertures()

    def on_click(self, ev):
        toolbar = getattr(self.fig.canvas, 'toolbar', None)
        if ev.inaxes is not self.ax or (toolbar is not None and toolbar.mode):
            return   # ignore clicks while zooming / panning
        if ev.button == 1:
            self.add(ev.xdata, ev.ydata)
        elif ev.button == 3:
            a = self.nearest(ev.xdata, ev.ydata)
            if a:
                self.apertures.remove(a)
                self.ap_stars.pop(a.name, None)
                self.draw_apertures()
                self.update_zooms()

    def on_key(self, ev):
        k = ev.key
        steps = {'right': 1, 'left': -1, 'pagedown': 25, 'pageup': -25}
        if k in steps:
            self.go(self.index + steps[k])
        elif k == 'home':
            self.go(0)
        elif k == 'end':
            self.go(self.n - 1)
        elif k in self.role_keys and ev.inaxes is self.ax:
            self.set_role(ev.xdata, ev.ydata, self.role_keys[k])
        elif k in PYMOVIE_KEYS and ev.inaxes is self.ax:
            self.set_color(ev.xdata, ev.ydata, PYMOVIE_KEYS[k])
        elif k == 'n' and ev.inaxes is self.ax:
            a = self.nearest(ev.xdata, ev.ydata)
            if a:
                old = a.name
                a.name = input(f'new name for {a.name}: ').strip() or a.name
                self.zoom_names = [a.name if z == old else z for z in self.zoom_names]
                if old in self.ap_stars:
                    self.ap_stars[a.name] = self.ap_stars.pop(old)
                self.draw_apertures()
                self.update_zooms()
        elif k == 's':
            save_apertures(self.aperture_file, self.apertures, self.wcs, self.ap_stars)
            self.say(f'saved {self.aperture_file.name}')
        elif k == 'l' and self.aperture_file.exists():
            self.apertures = load_apertures(self.aperture_file)
            self.ap_stars = load_aperture_stars(self.aperture_file)
            self.draw_apertures()
        elif k == 'a':
            self.run()
        elif k in ('1', '2') and ev.inaxes is self.ax:
            a = self.nearest(ev.xdata, ev.ydata)
            if a:
                self.zoom_names[int(k) - 1] = a.name
                self.update_zooms()
        elif k == 'i':
            missing = [a for a in self._star_apertures() if a.name not in self.ap_stars]
            if not missing:             # everything known: look all of them up again
                self.ap_stars.clear()
            self.start_identify()
        elif k == 'p' and self._solve_job is None:
            self.start_solve(force=True)

    # ---------------------------------------------------------------- analysis

    def run(self):
        if self._job is not None or not self.apertures:
            return
        aps = [Aperture(**{k: getattr(a, k) for k in AP_FIELDS}) for a in self.apertures]
        start = self.index
        missing = self._unidentified()
        self._result = self._error = None

        def work():
            try:
                if missing:
                    try:
                        self.identify_apertures(missing, frame=start,
                                                log=lambda m: setattr(self, '_progress', (0, self.n - 1)))
                    except Exception as e:   # names are a bonus
                        print(f'identification failed: {e}')
                save_apertures(self.aperture_file, self.apertures, self.wcs, self.ap_stars)
                if self.calibration is None:          # dark/flat clips from the config (cached)
                    from .calibrate import from_config
                    self.calibration = from_config(self.cfg, log=lambda m: setattr(self, '_cal_msg', m))
                self._result = run_analysis(self.path, aps, start, self.n - 1, self.cfg, self.calibration,
                                            progress=self._on_progress)
            except Exception as e:   # shown in the status line; the GUI keeps running
                self._error = e
        self._live = None
        self._live_frame = None
        self._job = threading.Thread(target=work, daemon=True)
        self._job.start()
        self.timer.start()

    def _on_progress(self, frame, values):
        """Called by the worker for every frame: only remember the latest (cheap)."""
        self._progress = (frame, self.n - 1)
        self._live = (frame, values)

    def poll(self):
        i, last = self._progress
        if self._job is not None and self._job.is_alive():
            cal = getattr(self, '_cal_msg', '')
            self.say(f'analysing ... frame {i} / {last}' if i else (cal or 'starting ...'))
            live = self._live
            now = time.monotonic()
            if (live is not None and now - self._live_shown >= LIVE_ZOOM_INTERVAL and any(self.zoom_names)
                    and live[0] != self._live_frame):
                self._live_shown = now
                fr, values = live
                self._live_frame = fr
                self.update_zooms(image=self.movie.read(fr).image,
                                  readings={n: readings_of(v) for n, v in values.items()}, frame=fr)
            return
        self.timer.stop()
        self._job = None
        lc = self._result
        if lc is None:
            self.say(f'analysis failed: {self._error!r}')
            return
        csv = self.path.with_suffix('.csv')
        stars = self.all_stars()
        lc.to_pymovie_csv(str(csv), at_frame=self.index, wcs=self.wcs, stars=stars)  # for PyOTE
        lc.to_csv(str(self.path.with_suffix('.pystarlight.csv')), wcs=self.wcs, stars=stars)
        self._track = {n: (lc[n]['frame'], lc[n]['xc'], lc[n]['yc']) for n in lc.names
                       if 'xc' in lc[n]}
        self._lc = lc
        self.update_zooms()
        target, refs = self._target_and_refs()
        pc = self.cfg['plot']
        with plot.style(**{'savefig.dpi': pc['dpi']}):
            fig = plot.summary(lc, self.image, self.apertures, target, refs,
                               window=self.cfg['analysis']['normalize_window'],
                               bin_size=pc['bin_size'], title=self.path.stem)
            plot.save(fig, str(self.path.with_name(self.path.stem + '-summary')), pc['formats'])
        fig.show()
        self.say(f'wrote {csv.name} and {self.path.stem}-summary ({", ".join(pc["formats"])})')
        if self.cfg.get('output', {}).get('stack_image'):
            self.start_stack(lc)

    # ---------------------------------------------------------------- stacked field image

    def start_stack(self, lc):
        """Registered stack of the analysed frames, annotated, in the background."""
        from .stacking import attach_wcs, save_field_stack, stack_field
        from .wcs.solve import solution_path, stack_frames
        oc = self.cfg['output']
        self._stack_msg = 'stacking ...'
        self._stack_done = None
        apertures = list(self.apertures)
        stars, ap_stars = self.all_stars(), dict(self.ap_stars)

        def work():
            try:
                with open_movie(str(self.path)) as mv:
                    st = stack_field(mv, lc, max_frames=int(oc.get('stack_max_frames') or 0),
                                     calibration=self.calibration,
                                     progress=lambda k, n: setattr(self, '_stack_msg', f'stacking {k}/{n}'))
                    if self.wcs is not None:
                        meta_p = solution_path(self.path).with_suffix('.json')
                        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
                        solved = stack_frames(mv, int(meta.get('stack_start', 0)),
                                              int(self.cfg['astrometry']['stack_frames']))
                        attach_wcs(st, self.wcs, solved)
                stem = str(self.path.with_name(self.path.stem + '-stack'))
                self._stack_done = save_field_stack(st, stem, apertures, lc, stars=stars, ap_stars=ap_stars,
                                                    title=self.path.stem,
                                                    formats=tuple(oc.get('stack_formats', ('svg', 'png'))))
            except Exception as e:
                self._stack_msg = f'stacking failed: {e!r}'
        self._stack_job = threading.Thread(target=work, daemon=True)
        self._stack_job.start()
        self._stack_timer = self.fig.canvas.new_timer(interval=500)
        self._stack_timer.add_callback(self._poll_stack)
        self._stack_timer.start()

    def _poll_stack(self):
        if self._stack_job.is_alive():
            self.say(self._stack_msg)
            return
        self._stack_timer.stop()
        if self._stack_done:
            names = sorted({Path(p).name for p in self._stack_done})
            self.say('stacked field: ' + ', '.join(names))
        else:
            self.say(self._stack_msg)

    def _target_and_refs(self):
        norm = {n for n, r in self.roles.items() if r['normalize']}
        target = next((a.name for a in self.apertures if a.role == 'target'), None)
        refs = [a.name for a in self.apertures if a.role in norm]
        if target is None:          # PyMovie colours: first red/green is the target, yellows are refs
            reds = [a.name for a in self.apertures if a.color in ('red', 'green')]
            target = reds[0] if reds else self.apertures[0].name
            refs = refs or [a.name for a in self.apertures if a.color == 'yellow']
        return target, refs


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog='pystarlight-view', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('movie', help='movie file or FITS folder')
    p.add_argument('--config', help='config file (default: see pystarlight-config path)')
    p.add_argument('--calibration', help='.npz from Calibration.save() (default: [calibration] in config)')
    a = p.parse_args(argv)
    cfg = config_mod.load(a.config)
    # --calibration overrides the config; otherwise [calibration] clips are stacked on the first run
    Viewer(a.movie, cfg, Calibration.load(a.calibration) if a.calibration else None)
    plt.show()


if __name__ == '__main__':
    sys.exit(main())
