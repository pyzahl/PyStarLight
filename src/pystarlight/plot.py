"""Publication-quality figures with matplotlib.

    from pystarlight import plot
    with plot.style():
        fig = plot.summary(lc, image, apertures, target='target', references=['ref1', 'ref2'])
        plot.save(fig, 'occultation')            # writes occultation.pdf and occultation.png

Needs matplotlib (pip install "starlight-photometry[plot]").
"""

from __future__ import annotations

import contextlib
import warnings
from typing import Optional, Sequence

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle

from . import normalize as nz
from .timestamps import parse_times, unwrap_midnight

APERTURE_COLORS = {'red': '#d62728', 'green': '#2ca02c', 'yellow': '#e6b800', 'white': '#7f7f7f',
                   'cyan': '#17becf'}
ROLE_COLORS = {'target': 'red', 'reference': 'yellow', 'check': 'green', 'sky': 'cyan'}


TARGET_COLOR = APERTURE_COLORS['red']
# references etc. in plots: matplotlib's cycle without its red (red is the target's)
OTHER_COLORS = [c for c in plt.rcParams['axes.prop_cycle'].by_key()['color'] if c != '#d62728']


def aperture_color(ap) -> str:
    """Display colour: by role when set (global mode), else PyMovie's colour."""
    name = ROLE_COLORS.get(getattr(ap, 'role', ''), ap.color)
    return APERTURE_COLORS.get(name, name)

PUBLICATION_RC = {
    'figure.dpi': 110,
    'savefig.dpi': 300,
    'savefig.bbox': 'tight',
    'font.size': 9,
    'axes.labelsize': 9,
    'axes.titlesize': 9,
    'legend.fontsize': 8,
    'xtick.labelsize': 8,
    'ytick.labelsize': 8,
    'xtick.direction': 'in',
    'ytick.direction': 'in',
    'xtick.top': True,
    'ytick.right': True,
    'xtick.minor.visible': True,
    'ytick.minor.visible': True,
    'axes.linewidth': 0.8,
    'lines.linewidth': 0.9,
    'lines.markersize': 2.5,
    'legend.frameon': False,
    'pdf.fonttype': 42,      # embed TrueType fonts (journals ask for this)
    'ps.fonttype': 42,
}


@contextlib.contextmanager
def style(**overrides):
    """Matplotlib settings for journal figures (single column is about 3.4 in wide)."""
    with mpl.rc_context({**PUBLICATION_RC, **overrides}):
        yield


def save(fig, stem: str, formats: Sequence[str] = ('pdf', 'png')):
    for ext in formats:
        fig.savefig(f'{stem}.{ext}')


# --------------------------------------------------------------------------- helpers

def zscale(image: np.ndarray) -> tuple[float, float]:
    """IRAF zscale display limits: shows faint stars without burning out the sky."""
    from astropy.visualization import ZScaleInterval
    return ZScaleInterval().get_limits(image)


def seconds(lc) -> np.ndarray:
    """Row times of a LightCurves as seconds since midnight (UTC), rollover unwrapped."""
    return unwrap_midnight(parse_times(lc.time))


def _time_axis(ax, lc, t):
    """Times relative to the first valid one; the absolute start goes in the label."""
    t0 = np.nanmin(t)
    if np.isfinite(t0):
        hh, rem = divmod(t0 % 86400, 3600)
        mm, ss = divmod(rem, 60)
        date = next((d for d in lc.date if d), '')
        ax.set_xlabel(f'Time from {date} {int(hh):02d}:{int(mm):02d}:{ss:06.3f} UTC in s'.replace('  ', ' '))
        return t - t0
    ax.set_xlabel('Frame')
    return lc.columns[lc.names[0]]['frame']


# --------------------------------------------------------------------------- panels

def frame(ax, image: np.ndarray, apertures: Sequence = (), limits: Optional[tuple] = None,
          cmap: str = 'gray', label: bool = True, inverted: bool = False):
    """Image with aperture boxes. apertures: pystarlight Aperture objects."""
    lo, hi = limits or zscale(image)
    ax.imshow(image, cmap=cmap + ('_r' if inverted else ''), vmin=lo, vmax=hi,
              origin='upper', interpolation='nearest')
    for ap in apertures:
        col = aperture_color(ap)
        ax.add_patch(Rectangle((ap.x0 - 0.5, ap.y0 - 0.5), ap.size, ap.size, fill=False, ec=col, lw=0.8))
        if label:
            ax.annotate(ap.name, (ap.x0 + ap.size, ap.y0), color=col, fontsize=7,
                        xytext=(2, 0), textcoords='offset points', va='top')
    ax.set_xlabel('x in px')
    ax.set_ylabel('y in px')
    return ax


def lightcurve(ax, lc, name: str, references: Sequence[str] = (), window: int = 101,
               column: str = 'signal', baseline: Optional[slice] = None, bin_size: int = 1,
               color: str = 'k', label: Optional[str] = None, event: Optional[tuple] = None):
    """Light curve of one aperture against UTC time.

    references: normalise by these apertures (smoothed over `window` rows).
    baseline: rows defining 1.0 (default: whole curve median).
    bin_size: also draw block averages of this many rows.
    event: (start, end) in seconds on the plotted axis, shaded."""
    y = lc[name][column]
    if references:
        ref = nz.combine_references([lc[r][column] for r in references])
        y = nz.normalize(y, ref, window=window)
    y = nz.baseline_normalize(y, baseline if baseline is not None else slice(None))
    x = _time_axis(ax, lc, seconds(lc))
    ax.plot(x, y, '.', color=color, ms=2, alpha=0.6 if bin_size > 1 else 1.0,
            label=label or name, rasterized=len(x) > 5000)
    if bin_size > 1:
        n = len(y) // bin_size * bin_size
        xb = x[:n].reshape(-1, bin_size).mean(axis=1)
        yb = np.nanmean(y[:n].reshape(-1, bin_size), axis=1)
        ax.plot(xb, yb, '-', color=color, lw=1.0, label=f'{bin_size}-point mean')
    if event:
        ax.axvspan(*event, color='0.85', zorder=0, lw=0)
    ax.set_ylabel('Normalised flux' if references else 'Relative flux')
    return ax


def tracking(ax, lc, names: Sequence[str], colors: Optional[Sequence[str]] = None):
    """Measured centroid drift of apertures (shake / tracking diagnostics)."""
    x = _time_axis(ax, lc, seconds(lc))
    for k, n in enumerate(names):
        xc, yc = lc[n]['xc'], lc[n]['yc']
        c = colors[k] if colors else None
        ax.plot(x, xc - np.nanmedian(xc), lw=0.7, color=c, label=f'{n} x')
        ax.plot(x, yc - np.nanmedian(yc), lw=0.7, ls='--', color=c, label=f'{n} y')
    ax.set_ylabel('Drift in px')
    return ax


def timing(ax, lc):
    """Time step between rows, in ms: dropped frames and timing jitter stand out."""
    t = seconds(lc)
    ax.plot(lc.columns[lc.names[0]]['frame'][1:], np.diff(t) * 1000, '.', ms=2, color='k')
    ax.set_xlabel('Frame')
    ax.set_ylabel('Time step in ms')
    return ax


# --------------------------------------------------------------------------- figures

def _legend(ax, **kw):
    """Top left, in an opaque box over the data: an occultation sits mid-run, not at the start."""
    return ax.legend(loc='upper left', frameon=True, framealpha=1.0, facecolor='white',
                     edgecolor='0.6', fancybox=False, **kw)


def magcurve(ax, lc, mags: np.ndarray, name: str, bin_size: int = 1, color: str = 'k',
             ylabel: str = 'Magnitude in mag'):
    """Magnitude curve of one aperture against UTC time (fainter is down)."""
    x = _time_axis(ax, lc, seconds(lc))
    y = np.asarray(mags, dtype=float)
    ax.plot(x, y, '.', color=color, ms=2, alpha=0.6 if bin_size > 1 else 1.0, label=name,
            rasterized=len(x) > 5000)
    if bin_size > 1:
        n = len(y) // bin_size * bin_size
        with warnings.catch_warnings():             # all-NaN blocks (star gone)
            warnings.simplefilter('ignore', RuntimeWarning)
            yb = np.nanmean(y[:n].reshape(-1, bin_size), axis=1)
        ax.plot(x[:n].reshape(-1, bin_size).mean(axis=1), yb, '-', color=color, lw=1.0,
                label=f'{bin_size}-point mean')
    _invert(ax)
    ax.set_ylabel(ylabel)
    return ax


def hms(s: float) -> str:
    """Seconds since midnight as 'hh:mm:ss.sss'."""
    hh, rem = divmod(s % 86400, 3600)
    mm, ss = divmod(rem, 60)
    return f'{int(hh):02d}:{int(mm):02d}:{ss:06.3f}'


def stepmodel(ax, lc, fit, m0: float = 0.0, color: str = 'k'):
    """A steps.StepFit (fitted in flux on seconds(lc)) over a magnitude curve: the levels as
    m0 - 2.5 log10(flux) with their 1 sigma band; a level within 3 sigma of zero flux as the
    3 sigma limit (dotted, 'fainter than'); each transition with its 1 sigma time bar. Boxes at
    the bottom: left the levels' magnitudes +- error of the mean, right the UTC times."""
    from .steps import to_mag
    t0 = np.nanmin(seconds(lc))
    lev = fit.levels
    edges = [fit.t[0] - fit.dt / 2, *[tr.time for tr in fit.transitions], fit.t[-1] + fit.dt / 2]
    edges = np.asarray(edges) - t0

    def mag(lv):
        return to_mag(lv.value, m0) if lv.value > 3 * lv.error else to_mag(3 * lv.error, m0)
    for i, lv in enumerate(lev):
        a, b, m = edges[i], edges[i + 1], mag(lv)
        limit = lv.value <= 3 * lv.error
        ax.plot([a, b], [m, m], ':' if limit else '-', color=color, lw=1.6, zorder=4,
                label='step fit' if i == 0 else None)
        if limit:
            ax.annotate(f'fainter than {m:.2f}', ((a + b) / 2, m), xytext=(0, -3), textcoords='offset points',
                        ha='center', va='top', fontsize=7, color=color)
        else:
            hi, lo = to_mag(lv.value - lv.error, m0), to_mag(lv.value + lv.error, m0)
            ax.fill_between([a, b], lo, hi if np.isfinite(hi) else m, color=color, alpha=0.25, lw=0,
                            zorder=3, label='level 1 sigma' if i == 0 else None)
    for i, tr in enumerate(fit.transitions):
        x = tr.time - t0
        m1, m2 = mag(lev[i]), mag(lev[i + 1])
        ax.plot([x, x], [m1, m2], '-', color=color, lw=1.6, zorder=4)
        ax.errorbar(x, (m1 + m2) / 2, xerr=tr.sigma, fmt='o', ms=3, color=color, ecolor=color,
                    elinewidth=1.4, capsize=4, capthick=1.4, zorder=5)
    if len(lev) > 1:
        text = '\n'.join(_level_text(lv, m0, a, b) for lv, a, b in zip(lev, edges[:-1], edges[1:]))
        ax.text(0.01, 0.03, text,
                transform=ax.transAxes, ha='left', va='bottom', fontsize=7, zorder=6,
                bbox=dict(boxstyle='square,pad=0.3', fc='white', ec='0.6'))
    if fit.transitions:
        ax.text(0.99, 0.03, '\n'.join(_event_text(tr) for tr in fit.transitions), transform=ax.transAxes,
                ha='right', va='bottom', fontsize=7, zorder=6,
                bbox=dict(boxstyle='square,pad=0.3', fc='white', ec='0.6'))
    return ax


def _level_text(lv, m0, a, b) -> str:
    m, err, _ = lv.mag(m0)
    span = f'{max(a, 0):.1f}-{b:.1f} s'
    return f'{span}: fainter than {m:.2f}' if lv.limit else f'{span}: {m:.3f} \u00b1 {err:.3f}'


def _event_text(tr) -> str:
    return (f'{tr.kind} {hms(tr.time)} UTC \u00b1 {tr.sigma:.3f} s '
            f'({100 * tr.drop:.0f} %, {tr.significance:.0f} sigma)')


def stepzoom(ax, lc, fit, k: int, color: str = 'k', data_color: str = TARGET_COLOR, frames: int = 8):
    """Transition k of a steps.StepFit close up, in flux relative to the brighter level: the rows,
    the sharp step, and the step time with its 1 sigma bar and band."""
    tr = fit.transitions[k]
    t0 = np.nanmin(seconds(lc))
    top = max(tr.before, tr.after)
    half = max(frames * fit.dt, 4 * tr.sigma)
    near = np.abs(fit.t - tr.time) <= half
    x = fit.t[near] - t0
    ax.plot(x, fit.y[near] / top, 'o', ms=2.5, color=data_color, zorder=3)
    xm = tr.time - t0
    ax.plot([xm - half, xm, xm, xm + half], np.array([tr.before, tr.before, tr.after, tr.after]) / top,
            '-', color=color, lw=1.4, zorder=4)
    ax.axvspan(xm - tr.sigma, xm + tr.sigma, color=color, alpha=0.15, lw=0, zorder=1)
    ax.errorbar(xm, (tr.before + tr.after) / 2 / top, xerr=tr.sigma, fmt='o', ms=3, color=color,
                elinewidth=1.4, capsize=4, capthick=1.4, zorder=5)
    ax.set_xlim(xm - half, xm + half)
    ax.set_title(f'{tr.kind} {hms(tr.time)} UTC\n\u00b1 {tr.sigma:.3f} s', fontsize=8)
    ax.set_xlabel('Time in s')
    if k == 0:
        ax.set_ylabel('Relative flux')
    return ax


REGION_COLORS = ['#d62728', '#9467bd', '#8c564b', '#e377c2', '#bcbd22', '#ff7f0e']   # target levels


def gaussian_envelope(values: np.ndarray, edges: np.ndarray) -> Optional[tuple[float, float, float]]:
    """(amplitude, mean, sigma) of a Gaussian fitted to the histogram of values over edges
    (probability density), started from the median and robust scatter; None with under 5 values."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size < 5:
        return None
    counts, _ = np.histogram(v, edges, density=True)
    c = (edges[:-1] + edges[1:]) / 2
    mu = float(np.median(v))
    sd = 1.4826 * float(np.median(np.abs(v - mu))) or float(np.std(v)) or float(edges[1] - edges[0])
    p0 = (counts.max() or 1.0, mu, sd)
    try:
        from scipy.optimize import curve_fit
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            p, _ = curve_fit(lambda x, a, m, s: a * np.exp(-0.5 * ((x - m) / s) ** 2), c, counts, p0=p0,
                             maxfev=2000)
        if np.all(np.isfinite(p)) and edges[0] <= p[1] <= edges[-1]:
            return float(p[0]), float(p[1]), abs(float(p[2]))
    except (RuntimeError, ValueError):
        pass
    return p0


def maghist(ax, sets: Sequence[tuple[str, np.ndarray, str]], bins: int = 60,
            xlabel: str = 'Magnitude in mag'):
    """Histograms of several magnitude sets [(label, values, colour)] on common bins, as
    probability densities (sets of few frames, e.g. an occultation, compare in shape with long
    ones), each with its fitted Gaussian envelope; the legend gives the envelope's mean and sigma."""
    finite = [np.asarray(v, dtype=float)[np.isfinite(v)] for _, v, _ in sets]
    lo = min((np.percentile(v, 0.5) for v in finite if v.size), default=0.0)
    hi = max((np.percentile(v, 99.5) for v in finite if v.size), default=1.0)
    edges = np.linspace(lo, hi if hi > lo else lo + 1, bins + 1)
    xs = np.linspace(edges[0], edges[-1], 400)
    for (label, _, color), v in zip(sets, finite):
        if not v.size:
            continue
        ax.hist(v, edges, density=True, histtype='stepfilled', color=color, alpha=0.25, lw=0)
        ax.hist(v, edges, density=True, histtype='step', color=color, lw=0.8)
        env = gaussian_envelope(v, edges)
        if env is None:
            ax.plot([], [], color=color, label=f'{label}: {v.size} frames')
            continue
        a, m, sd = env
        ax.plot(xs, a * np.exp(-0.5 * ((xs - m) / sd) ** 2), '-', color=color, lw=1.4,
                label=f'{label}: {m:.3f} \u00b1 {sd:.3f} mag, {v.size} frames')
    ax.set_xlabel(xlabel)
    ax.set_ylabel('Probability density in 1/mag')
    ax.set_ylim(0, ax.get_ylim()[1] * 1.5)         # room for the legend above the peaks
    return ax


def mag_histograms(lc, mags: dict, target: str, references: Sequence[str] = (), fit=None,
                   mag_label: str = 'Magnitude in mag', colors: Optional[dict] = None,
                   checks: Sequence[str] = (), n_compare: int = 2, title: Optional[str] = None,
                   figsize=(7.0, 7.0), fig=None):
    """Magnitude histograms, each with its fitted Gaussian envelope, in two panels. Top: the
    target in each level of the step fit (else the whole run); a level the star has gone from
    shows only its frames that still have flux (the others have no magnitude). Below, for
    comparison: the check stars, or without any the n_compare references closest to the
    target's magnitude."""
    if fig is None:
        fig = plt.figure(figsize=figsize, constrained_layout=True)
    else:
        fig.clear()
    ax_t, ax_c = fig.subplots(2, 1)
    m = np.asarray(mags[target], dtype=float)
    t = seconds(lc)
    t = t - np.nanmin(t)
    levels = fit.levels if fit is not None and fit.levels else []
    sets = []
    if len(levels) > 1:
        for k, lv in enumerate(levels):
            name = f'{target} {t[lv.start]:.1f}-{t[lv.stop - 1]:.1f} s'
            sets.append((name, m[lv.start:lv.stop], REGION_COLORS[k % len(REGION_COLORS)]))
    else:
        sets.append((target, m, TARGET_COLOR))
    maghist(ax_t, sets, xlabel=mag_label)
    ax_t.set_title('Target', loc='left', fontsize=9)
    _legend(ax_t, fontsize=7)
    compare = [c for c in checks if c in mags]
    if compare:
        ax_c.set_title('Check stars', loc='left', fontsize=9)
    else:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            ref_level = np.nanmedian(m[levels[0].start:levels[0].stop]) if levels else np.nanmedian(m)
            med = {r: np.nanmedian(mags[r]) for r in references if r in mags}
        compare = sorted((r for r in med if np.isfinite(med[r])), key=lambda r: abs(med[r] - ref_level))
        compare = compare[:n_compare]
        ax_c.set_title('References nearest the target (no check stars)', loc='left', fontsize=9)
    if compare:
        maghist(ax_c, [(c, mags[c], (colors or {}).get(c) or OTHER_COLORS[k % len(OTHER_COLORS)])
                       for k, c in enumerate(compare)], xlabel=mag_label)
        _legend(ax_c, fontsize=7)
    else:
        ax_c.set_axis_off()
    if title:
        fig.suptitle(title)
    return fig


def _invert(ax):
    if not ax.yaxis_inverted():
        ax.invert_yaxis()


def summary(lc, image: np.ndarray, apertures: Sequence, target: str,
            references: Sequence[str] = (), window: int = 101, bin_size: int = 1,
            title: Optional[str] = None, figsize=(7.0, 6.5), colors: Optional[dict] = None, fig=None,
            mags: Optional[dict] = None, mag_label: str = 'Magnitude in mag', steps=None):
    """Three panels: field with apertures, normalised target light curve, tracking drift.
    The target is red; colors: {reference name: colour}, the others from OTHER_COLORS.
    fig: draw into this figure again (cleared) instead of a new one (the viewer's live update).
    mags ({name: magnitudes}, normalize.magnitudes): the same layout in magnitudes, signals and
    the target's curve as magnitudes (y axis `mag_label`, fainter down).
    steps: (steps.StepFit of the target's flux, m0) drawn over the target's magnitude curve
    (stepmodel), with a row of close-ups of its transitions (stepzoom)."""
    events = steps[0].transitions[:6] if steps is not None and mags is not None else []
    if fig is None:
        size = (figsize[0], figsize[1] + 1.6) if events else figsize
        fig = plt.figure(figsize=size, constrained_layout=True)
    else:
        fig.clear()
    heights = [1.3, 1, 0.6] + ([0.9] if events else [])
    gs = fig.add_gridspec(len(heights), 2, height_ratios=heights, width_ratios=[1, 1.3])
    ax_img = fig.add_subplot(gs[0, 0])
    frame(ax_img, image, apertures)
    ax_raw = fig.add_subplot(gs[0, 1])
    x = _time_axis(ax_raw, lc, seconds(lc))

    def curve(n):
        return mags[n] if mags is not None else lc[n]['signal']
    ax_raw.plot(x, curve(target), lw=1.2, color=TARGET_COLOR, label=target, zorder=3)
    for k, n in enumerate(references):          # target first in the legend, drawn on top
        c = (colors or {}).get(n) or OTHER_COLORS[k % len(OTHER_COLORS)]
        ax_raw.plot(x, curve(n), lw=0.6, color=c, label=n, zorder=2)
    if mags is not None:
        _invert(ax_raw)
    ax_raw.set_ylabel(mag_label if mags is not None else 'Signal in ADU')
    _legend(ax_raw)
    ax_lc = fig.add_subplot(gs[1, :])
    if mags is not None:
        magcurve(ax_lc, lc, mags[target], target, bin_size=bin_size, color=TARGET_COLOR, ylabel=mag_label)
        if steps is not None and steps[0].levels:
            stepmodel(ax_lc, lc, *steps)
    else:
        lightcurve(ax_lc, lc, target, references, window=window, bin_size=bin_size, color=TARGET_COLOR)
    _legend(ax_lc)
    ax_tr = fig.add_subplot(gs[2, :], sharex=ax_lc)
    tracking(ax_tr, lc, [target], colors=[TARGET_COLOR])
    _legend(ax_tr, ncol=2)
    if events:                  # close-ups of the transitions: their time bars are too short above
        sub = gs[3, :].subgridspec(1, len(events))
        for k in range(len(events)):
            stepzoom(fig.add_subplot(sub[0, k]), lc, steps[0], k)
    if title:
        fig.suptitle(title)
    return fig
