"""Publication-quality figures with matplotlib.

    from pystarlight import plot
    with plot.style():
        fig = plot.summary(lc, image, apertures, target='target', references=['ref1', 'ref2'])
        plot.save(fig, 'occultation')            # writes occultation.pdf and occultation.png

Needs matplotlib (pip install "starlight-photometry[plot]").
"""

from __future__ import annotations

import contextlib
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
        ax.set_xlabel(f'Time from {date} {int(hh):02d}:{int(mm):02d}:{ss:06.3f} UTC (s)'.replace('  ', ' '))
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
    ax.set_xlabel('x (px)')
    ax.set_ylabel('y (px)')
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
    ax.set_ylabel('Drift (px)')
    return ax


def timing(ax, lc):
    """Time step between rows, in ms: dropped frames and timing jitter stand out."""
    t = seconds(lc)
    ax.plot(lc.columns[lc.names[0]]['frame'][1:], np.diff(t) * 1000, '.', ms=2, color='k')
    ax.set_xlabel('Frame')
    ax.set_ylabel('Time step (ms)')
    return ax


# --------------------------------------------------------------------------- figures

def _legend(ax, **kw):
    """Top left, in an opaque box over the data: an occultation sits mid-run, not at the start."""
    return ax.legend(loc='upper left', frameon=True, framealpha=1.0, facecolor='white',
                     edgecolor='0.6', fancybox=False, **kw)


def summary(lc, image: np.ndarray, apertures: Sequence, target: str,
            references: Sequence[str] = (), window: int = 101, bin_size: int = 1,
            title: Optional[str] = None, figsize=(7.0, 6.5)):
    """Three panels: field with apertures, normalised target light curve, tracking drift."""
    fig = plt.figure(figsize=figsize, constrained_layout=True)
    gs = fig.add_gridspec(3, 2, height_ratios=[1.3, 1, 0.6], width_ratios=[1, 1.3])
    ax_img = fig.add_subplot(gs[0, 0])
    frame(ax_img, image, apertures)
    ax_raw = fig.add_subplot(gs[0, 1])
    x = _time_axis(ax_raw, lc, seconds(lc))
    ax_raw.plot(x, lc[target]['signal'], lw=1.2, color=TARGET_COLOR, label=target, zorder=3)
    for k, n in enumerate(references):          # target first in the legend, drawn on top
        ax_raw.plot(x, lc[n]['signal'], lw=0.6, color=OTHER_COLORS[k % len(OTHER_COLORS)], label=n,
                    zorder=2)
    ax_raw.set_ylabel('Signal (ADU)')
    _legend(ax_raw)
    ax_lc = fig.add_subplot(gs[1, :])
    lightcurve(ax_lc, lc, target, references, window=window, bin_size=bin_size, color=TARGET_COLOR)
    _legend(ax_lc)
    ax_tr = fig.add_subplot(gs[2, :], sharex=ax_lc)
    tracking(ax_tr, lc, [target], colors=[TARGET_COLOR])
    _legend(ax_tr, ncol=2)
    if title:
        fig.suptitle(title)
    return fig
