# PyStarLight: notes for Claude

Occultation light-curve photometry as a Python library plus a minimal matplotlib viewer.
Import name `pystarlight`, PyPI distribution name `starlight-photometry` (`pystarlight` is taken on
PyPI by an unrelated project). MIT licence. Owner: Percy Zahl (GitHub pyzahl/PyStarLight).

Most processing is derived from PyMovie 4.2.8 (Bob Anderson, MIT), whose source lives at
`../PyMovie/pymovie` with its own venv (`../PyMovie/pymovie/.venv`, Python 3.10, old pinned numpy).
PyOTE source: `../py-ote`. PyOccult (sites, local Gaia extracts): `../PyOccult`.

## Working rules (from the owner)

- **Do not commit or push** unless asked. When asked, give the command; the owner runs it.
- **Never write into `OTEVideo/`, `CZ-Data/` or `DarkLight/`** (owner data, git-ignored; OTEVideo is a symlink to the owner's recordings on an external drive,
  git-ignored). Test on a scratch copy: a folder with a symlink to the `.ravf` plus copies of
  `<movie>.wcs.fit/.wcs.json/.stars.json/.apertures.json`. A real `apertures.json` was once
  overwritten by a test run; the viewer saves next to the movie on run and on `s`.
- **The private config** is `pystarlight.toml` in this repo's root (mode 600, git-ignored; moved
  there from `~/.config/pystarlight/` at the owner's request). It holds the astrometry.net API key
  and personal data. Never print the key, copy it into tracked files, docs, tests or memory.
  `pystarlight-config show` masks it. Tests must not read it: `tests/conftest.py` sets
  `PYSTARLIGHT_CONFIG=none`. When adding config keys, add them to `config_template.toml` (defaults
  merge in) and, if useful, to the private file without touching existing values.
- Uploads to nova.astrometry.net are private (`public = false`); PyMovie's client defaulted to public.
- Exploring ideas is welcome ("tinkering"); AI/ML denoising or centring is explicitly deferred.

## Layout (`src/pystarlight/`)

| module | what |
|---|---|
| `movie.py` | `open_movie()`: AVI/MOV (OpenCV), SER (numpy memmap), ADV/AAV (Adv2), RAVF (ravf), FITS folder |
| `preprocess.py` | gamma, CMOS outlaw pixels, hot pixels, line-noise filter, dark/flat (whole frame) |
| `calibrate.py` | `Calibration`: master dark/flat, gain + defect maps, applied per aperture box (`apply_roi`) |
| `photometry.py` | PyMovie-exact background (`robust_mean_std`), blob mask, centroids, TME, `measure_aperture` |
| `tracking.py` | `Aperture` dataclass (incl. `role`), PyMovie `Tracker` (0/1/2 yellow), `TrackPath` |
| `pipeline.py` | `analyze()` PyMovie mode → `LightCurves` (`to_csv`, `to_pymovie_csv`, `normalized`) |
| `globaltrack.py` | `analyze_global()`: roles, Gaussian peak fits, weighted robust rigid transform, dimming detection, target drift interpolation, sub-pixel circular apertures |
| `export.py` | PyMovie-layout CSV for PyOTE (`write_pymovie_csv`, `sky_lines`) |
| `timestamps/` | `ocr.py` (PyMovie's VTI OCR, verbatim), `vti.py` (`VtiReader`), `timing.py` (checks, new) |
| `wcs/` | `solve.py` (astrometry.net, orientation, plate scale, `solve_movie`, `field_stars`), `identify.py` (brightest stars, SIMBAD, `identify_positions`), `manual.py` (two-star, verbatim), `astrometry_client.py` (verbatim-ish) |
| `config.py` + `config_template.toml` | private config, PyOccult-compatible sites (`pystarlight-config`) |
| `plot.py` | publication figures (`style`, `summary`, panels) |
| `stacking.py` | registered field stack (`stack_field`, `attach_wcs`, `save_field_stack` → FITS + annotated SVG/PNG/PDF), `remove_hot_pixels`; `self_dark` is opt-in only (fails for small drift) |
| `viewer.py` | `pystarlight-view`: matplotlib GUI (keys, zoom views, live updates, plate solve, identification) |

`ocr.py`, `wcs/manual.py`, `wcs/astrometry_client.py` are kept close to PyMovie's files and
excluded from ruff, so upstream fixes diff cleanly. Don't reformat them.

## Commands

```bash
uv sync --extra all                                  # repo venv (.venv, Python 3.12): plot, ravf, adv, catalog
uv run --group test pytest -q                        # current libs; PyMovie parity tests skip
uvx ruff check                                       # lint (line length 110)
uv run pystarlight-view OTEVideo/<run>/<run>.ravf    # viewer (owner's data)
```

Parity tests against PyMovie's own code run in PyMovie's venv, with pytest/tomli on a helper
path (do NOT add astroquery/numpy there: they shadow PyMovie's pinned versions):

```bash
uv pip install --target <scratch>/pt pytest tomli --python ../PyMovie/pymovie/.venv/bin/python
PYTHONPATH=src:<scratch>/pt ../PyMovie/pymovie/.venv/bin/python -m pytest -q tests
```

Expect both suites green (about 420 tests in PyMovie's env, about 70 + 7 skipped in the repo venv).
GUI tests run headless (Agg): Agg timers never fire, so tests call `poll()`/`_poll_solve()` by hand.

## Design decisions worth keeping

- **Two analysis modes.** `analyze()` reproduces PyMovie bit for bit (verified on a real ASTRID
  recording: all 36,096 CSV values and timestamps identical; the PyOTE CSV export matches PyMovie's
  file line by line). PyMovie quirks are kept on purpose and marked `PyMovie quirk` in the code.
  `analyze_global()` is the new, better mode (normalised scatter of a faint star 23.5% → 6.4%).
- **Roles** (config table `[apertures.roles]`): target (one), reference (normalisation), check
  (tracked, not normalised), sky. Keys t / r / c / k. All star roles steer global tracking.
- **Occultation handling:** the target is dropped from tracking when its fit fails or it dims
  *relative to the other stars* (clouds dim all); its own offset from the global solution is
  smoothed and linearly interpolated across the event (second pass). `event=(first, last)` forces it.
- **Peak-fit robustness:** fits must match the PSF width from the first frame (rejects noise
  spikes/cosmic rays); weights are SNR² (capped); robust start from the median displacement.
- **Plate solution** belongs to the median stack of the first `stack_frames` frames. Anything
  converting pixels to RA/Dec on later frames must remove the field drift first
  (`identify_apertures` uses phase correlation). Identified stars show catalogue RA/Dec.
- **PyOTE CSV rules:** first line contains `PyMovie`; column header is the first line containing
  `FrameNum` (no comment may contain it); signal columns first, target first; aperture names
  must not contain `-` (`csv_name`); `# date at frame 0:` and `#KEY: value` metadata feed PyOTE's report form.
- **Viewer keys** are removed from matplotlib's keymaps at start (`free_matplotlib_keys`); `f`
  (full screen), `o` (zoom), `q` stay matplotlib's. PyMovie colours are shift-keys `Y R G W`.
- Mirrored images: PyMovie's two-star solver assumes non-mirrored; `solve_two_stars(mirrored=True)`.
  The plate solve reports parity (`Orientation.mirrored`).
- PyMovie bug fixed here: the astrometry.net upload was written as int16 (16-bit pixels wrapped).
- Adv2 1.2.0 calls np.reshape(newshape=...), removed in numpy 2: `movie._patch_adv2_for_numpy2`
  swaps a thin numpy wrapper into the Adv2 module only. Adv2 also never detects missing
  timestamps (reports 2010-01-01); AdvReader returns None for a zero mid-exposure time.
- Adv2 has ONE global open file per process. `AdvReader` serialises access (`_ADV_LOCK`) and
  re-opens when another reader took the slot; `close()` only closes if it holds the slot. All
  readers' `close()` must stay idempotent (the viewer closes inside `with` blocks).
- Calibration clips: `calibrate.from_config` stacks `[calibration]` dark/flat clips; masters are
  cached in `~/.cache/pystarlight/masters` keyed on path/size/mtime (never next to the clip).
- The stacked field image shows diagonal streaks on the owner's data: the sensor's fixed pattern
  smeared along the drift. Only a real dark removes it; the self-dark experiment made it worse.

## Owner's setup

ASTRID camera (IMX296, 1456×1088, 16-bit RAVF, GPS timestamps via OTEStamper), typical 4–10 fps;
APO107 with ×0.5 reducer (measured ≈ 343 mm, 2.07″/px). Site `home` from PyOccult's `sites.py`,
corrected in the private config (`aperture_cm` 10.7, `sensor_mm` 5.02 × 3.75).
Test recording `None_20261011_042339_1` (near Deneb): target TYC 3574-1718-1, refs HD 197161,
BD+44 3540, BD+44 3545.
