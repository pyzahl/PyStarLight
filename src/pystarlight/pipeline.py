"""Run an analysis over a frame range. No GUI: call it from a worker thread or process
and pass ``progress`` / ``cancel`` to report back and stop early."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .movie import MovieReader
from .photometry import PhotometryConfig
from .timestamps.vti import VtiReader
from .tracking import Aperture, Tracker, TrackPath

COLUMNS = ('frame', 'signal', 'appsum', 'bkavg', 'bkstd', 'mskpx', 'xc', 'yc', 'maxpx',
           'hit_defect')


@dataclass
class LightCurves:
    """Per-aperture columns as numpy arrays, plus per-row timestamps.

    time_score: VTI OCR correlation sum per row (None when the time came from the file)."""
    columns: dict[str, dict[str, np.ndarray]]
    date: list[Optional[str]] = field(default_factory=list)
    time: list[Optional[str]] = field(default_factory=list)
    time_score: list[Optional[int]] = field(default_factory=list)
    top_field_first: Optional[bool] = None
    roles: dict[str, str] = field(default_factory=dict)       # aperture name -> role
    frame_info: dict[str, np.ndarray] = field(default_factory=dict)  # per-row values, e.g. tracking
    source: str = ''                                           # movie path
    metadata: dict = field(default_factory=dict)               # the movie's metadata
    mode: str = 'pymovie'                                      # 'pymovie' | 'global'
    apertures: list = field(default_factory=list)              # Aperture objects as analysed

    def __getitem__(self, name: str) -> dict[str, np.ndarray]:
        return self.columns[name]

    @property
    def names(self) -> list[str]:
        return list(self.columns)

    def by_role(self, role: str) -> list[str]:
        return [n for n in self.names if self.roles.get(n) == role]

    def normalized(self, column: str = 'signal', window: int = 101, target: Optional[str] = None,
                   references: Optional[list[str]] = None) -> np.ndarray:
        """Target divided by the smoothed sum of the references, scaled to median 1.
        Defaults to the apertures with role 'target' and 'reference'."""
        from . import normalize as nz
        target = target or self.by_role('target')[0]
        references = references or self.by_role('reference')
        ref = nz.combine_references([self.columns[r][column] for r in references])
        y = nz.normalize(self.columns[target][column], ref, window=window)
        return nz.baseline_normalize(y, slice(None))

    def to_pymovie_csv(self, path: str, **kw):
        """CSV in PyMovie's layout, readable by PyOTE (see export.write_pymovie_csv)."""
        from .export import write_pymovie_csv
        write_pymovie_csv(self, path, **kw)

    def to_csv(self, path: str, wcs=None, stars=None):
        """All columns, PyStarLight's own layout (not for PyOTE: use to_pymovie_csv).
        wcs, stars: add the plate solution and aperture RA/Dec as header comments."""
        names = self.names
        cols = {n: [c for c in self.columns[n] if c != 'frame'] for n in names}
        extra = list(self.frame_info)
        header = (['FrameNum', 'date', 'timeInfo'] + extra
                  + [f'{c}-{n}' for n in names for c in cols[n]])
        frames = self.columns[names[0]]['frame']
        with open(path, 'w') as f:
            if self.roles:
                f.write('# roles: ' + ', '.join(f'{n}={r}' for n, r in self.roles.items()) + '\n')
            if wcs is not None:
                from .export import sky_lines
                lines, where = sky_lines(self, wcs, stars)
                for ln in lines:
                    f.write(ln + '\n')
                for n, txt in where.items():
                    f.write(f'# {n} RA,Dec (J2000): {txt}\n')
            f.write(','.join(header) + '\n')
            for i in range(len(frames)):
                row = [f'{frames[i]:g}', self.date[i] or '', f'[{self.time[i]}]' if self.time[i] else '']
                row += [f'{self.frame_info[c][i]:g}' for c in extra]
                for n in names:
                    row += [f'{self.columns[n][c][i]:g}' for c in cols[n]]
                f.write(','.join(row) + '\n')


def analyze(reader: MovieReader, apertures: list[Aperture],
            start: int = 0, stop: Optional[int] = None, *,
            cfg: PhotometryConfig = PhotometryConfig(),
            preprocess: Optional[Callable[[np.ndarray], np.ndarray]] = None,
            track_path: Optional[TrackPath] = None,
            use_yellow_mask: bool = False,
            defect_frame: Optional[np.ndarray] = None,
            fields: bool = False, top_field_first: Optional[bool] = None,
            vti: Optional[VtiReader] = None,
            calibration=None,
            progress: Optional[Callable[[int, dict], None]] = None,
            cancel: Optional[threading.Event] = None) -> LightCurves:
    """Track and measure the apertures on frames start..stop (inclusive).

    stop < start runs backwards; the result is still in ascending frame order.

    vti: read the times from a VTI overlay (analog video). The OCR runs on the frame
    before ``preprocess``. Frame mode uses the first field's time. Field mode gives
    each field row its own time.
    top_field_first: field order; None = detected by the VTI reader, else top first.
    calibration: calibrate.Calibration, applied to each aperture box only (fast).
    progress(frame_index, {name: Measurement}) is called after each frame; keep it
    cheap or throttle it (e.g. redraw a GUI at 10 Hz, not every frame)."""
    if stop is None:
        stop = len(reader) - 1
    step = 1 if stop >= start else -1

    first = reader.read(start).image
    if preprocess:
        first = preprocess(first)
    tracker = Tracker(apertures, first.shape, cfg, track_path, use_yellow_mask,
                      defect_frame, fields, calibration)
    tracker.initialize(first)

    if vti is not None:
        vti.reset()

    rows: list[tuple] = []   # (frame, date, time, score, {name: (values...)})
    tff = top_field_first
    for fr in reader.frames(start, stop, step):
        if cancel is not None and cancel.is_set():
            break
        times = [(fr.time, None), (fr.time, None)]       # (first field, second field)
        if vti is not None:
            r = vti.read(fr.image)
            if top_field_first is None:
                tff = r.top_field_first
            first, second = (r.upper, r.lower) if tff is not False else (r.lower, r.upper)
            times = [(f.timestamp.strip('[]') if f.ok else None, f.cum_score) for f in (first, second)]
        img = preprocess(fr.image) if preprocess else fr.image
        res = tracker.step(img, fr.index)
        if fields:
            first_i, second_i = (0, 1) if tff is not False else (1, 0)
            for (off, fi), (t, sc) in zip(((0.0, first_i), (0.5, second_i)), times):
                rows.append((fr.index + off, fr.date, t, sc,
                             {n: _values(m.fields[fi], m) for n, m in res.items()}))
        else:
            rows.append((fr.index, fr.date, times[0][0], times[0][1],
                         {n: _values(m, m) for n, m in res.items()}))
        if progress:
            progress(fr.index, res)

    rows.sort(key=lambda r: r[0])
    cols = {}
    for ap in apertures:
        data = np.array([(r[0],) + r[4][ap.name] for r in rows], dtype=float).reshape(-1, len(COLUMNS))
        cols[ap.name] = {c: data[:, i] for i, c in enumerate(COLUMNS)}
    return LightCurves(cols, [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
                       tff if (fields or vti is not None) else None,
                       roles={a.name: a.role for a in apertures if a.role},
                       source=str(getattr(reader, 'path', '')), metadata=dict(reader.metadata),
                       mode='pymovie', apertures=list(apertures))


def _values(m, whole):
    nan = float('nan')
    return (m.signal, m.appsum, m.bkavg, m.bkstd, m.mskpx,
            whole.xc if whole.xc is not None else nan,
            whole.yc if whole.yc is not None else nan,
            m.maxpx, whole.hit_defect)
