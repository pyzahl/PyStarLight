"""The player's data, without Qt: every frame's time stamp and status (load_timing) and the
target's light curve from an analysis next to the recording (load_analysis). Also used by the
movie writer (pystarlight.tangra.write_movie: target mark and light-curve panel)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .ravf_writer import FRAME_HEAD, ravf_to_unix

TICKS_UNIX_EPOCH = 621355968000000000      # .NET ticks at 1970-01-01T00:00:00Z (SER)


# ---------------------------------------------------------------------------
# Time stamps and per-frame status of a whole file
# ---------------------------------------------------------------------------
def frame_time_s(date, time_str):
    """Frame date/time strings -> Unix seconds (seconds of the day if there is no date)."""
    from ..timestamps.timing import parse_time
    sec = parse_time(time_str)
    if not np.isfinite(sec) or not date:
        return sec
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp() + sec


def ravf_headers(path, reader):
    """Per-frame RAVF header fields (all frames, without the image data)."""
    out = []
    idx = reader._r.index
    with open(path, "rb") as f:
        for i in range(idx.count()):
            f.seek(idx.item(i)[0])
            (_m, _hl, _dl, _type, start, exposure, sats, almanac, almanac_off, fix,
             seq) = FRAME_HEAD.unpack(f.read(FRAME_HEAD.size))
            out.append({"t": ravf_to_unix(start) / 1e9, "exposure_ms": exposure / 1e6,
                        "satellites": sats, "fix": fix, "almanac": almanac,
                        "almanac_offset": almanac_off, "sequence": seq})
    return out


def load_timing(path, progress=lambda done, total: None):
    """(times [s], per-frame info dicts) of every frame. RAVF and SER from their index /
    trailer (fast); other formats by reading each frame."""
    from ..movie import RavfReader, SerReader, open_movie
    with open_movie(path) as mv:
        n = len(mv)
        if isinstance(mv, RavfReader):
            info = ravf_headers(path, mv)
            return np.array([d["t"] for d in info]), info
        if isinstance(mv, SerReader) and len(mv.ticks) >= n:
            t = (np.asarray(mv.ticks[:n], dtype=np.int64) - TICKS_UNIX_EPOCH) / 1e7
            return t, [{} for _ in range(n)]
        times, info = np.full(n, np.nan), []
        for i in range(n):
            fr = mv.read(i)
            times[i] = frame_time_s(fr.date, fr.time)
            info.append(dict(fr.status or {}))
            if i % 50 == 0:
                progress(i, n)
        return times, info


def load_analysis(path, times=None):
    """The target's light curve from an analysis next to the recording (pystarlight analyze):
    <movie>.pystarlight.csv (every aperture's signal by frame) and, if there, <movie>.event.json
    (target, the references it used, normalise window, step-fit levels and D/R times). The flux
    is the target / the smoothed reference ensemble, as the analysis' step fit has it. times:
    the frames' UTC (s), to place the levels and D/R. None when there is no analysis."""
    import csv
    p = Path(path)
    stem = p.with_name(p.stem)
    table = Path(f"{stem}.pystarlight.csv")
    if not table.is_file():
        return None
    try:
        with open(table) as f:
            lines = f.read().splitlines()
        roles = {}
        for ln in lines:
            if ln.startswith("# roles:"):
                roles = dict(kv.strip().split("=", 1) for kv in ln[len("# roles:"):].split(",") if "=" in kv)
        rows = list(csv.DictReader(ln for ln in lines if not ln.startswith("#")))
        ev = {}
        evp = Path(f"{stem}.event.json")
        if evp.is_file():
            with open(evp) as f:
                ev = json.load(f)
        target = ((ev.get("target") or {}).get("aperture")
                  or next((n for n, r in roles.items() if r == "target"), None))
        refs = ([r["aperture"] for r in ev.get("references") or [] if r.get("aperture")]
                or [n for n, r in roles.items() if r == "reference"])
        window = int((ev.get("photometry") or {}).get("normalize_window") or 31)
        if not target or not rows or f"signal-{target}" not in rows[0]:
            return None
        frames = np.array([float(r["FrameNum"]) for r in rows])
        xy = None                                   # the target's tracked centre per frame
        if f"xc-{target}" in rows[0] and f"yc-{target}" in rows[0]:
            xy = (np.array([float(r[f"xc-{target}"]) for r in rows]),
                  np.array([float(r[f"yc-{target}"]) for r in rows]))
        sig = {n: np.array([float(r[f"signal-{n}"]) for r in rows]) for n in [target, *refs]
               if f"signal-{n}" in rows[0]}
        refs = [r for r in refs if r in sig]
        if refs:
            from ..normalize import ensemble
            ens, _, _ = ensemble(sig, refs, None, window=window)
            flux = sig[target] / ens
            what = f"target {target} / {len(refs)} reference{'s' if len(refs) > 1 else ''}"
        else:
            flux = sig[target] / np.nanmedian(sig[target])
            what = f"target {target} (no references: / its median)"
    except (OSError, ValueError, KeyError) as e:
        print(f"analysis not shown: {e}")
        return None

    def at(utc):                                    # UTC text -> fractional frame index
        if times is None or not len(times) or not utc:
            return None
        try:
            t = datetime.fromisoformat(str(utc).rstrip("Z")).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
        ok = np.isfinite(times)
        return float(np.interp(t, times[ok], np.flatnonzero(ok))) if ok.sum() > 1 else None
    # shown relative to the median (the unocculted star ~ 1): the analysis' flux is target /
    # the summed reference ensemble, often far from 1 (a target fainter than its references)
    ok = np.isfinite(flux)
    scale = float(np.median(flux[ok])) if ok.any() else 1.0
    scale = scale if scale > 0 else 1.0
    flux = flux / scale
    fit = ev.get("step_fit") or {}
    levels = [(at(lv.get("start_utc")), at(lv.get("end_utc")),
               lv["flux"] / scale if lv.get("flux") is not None else None)
              for lv in fit.get("levels") or []]
    marks = [(at(tr.get("utc")), tr.get("kind", ""), str(tr.get("utc") or "")[11:23])
             for tr in fit.get("transitions") or []]
    name = str((ev.get("target") or {}).get("name") or "")
    return {"frames": frames, "flux": flux, "what": what, "target": f"target {target}", "source": table.name,
            "xy": xy, "label": name or f"target {target}",
            "levels": [lv for lv in levels if None not in lv[:2] and lv[2] is not None],
            "marks": [m for m in marks if m[0] is not None]}
