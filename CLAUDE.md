# PyStarLight: notes for Claude

Occultation light-curve photometry as a Python library plus a minimal matplotlib viewer.
Import name `pystarlight`, PyPI distribution name `starlight-photometry` (`pystarlight` is taken on
PyPI by an unrelated project). MIT licence. Owner: Percy Zahl (GitHub pyzahl/PyStarLight).

Most processing is derived from PyMovie 4.2.8 (Bob Anderson, MIT), whose source lives at
`../PyMovie/pymovie` with its own venv (`../PyMovie/pymovie/.venv`, Python 3.10, old pinned numpy).
PyOTE source: `../py-ote`. PySyzygos (formerly PyOccult; sites, local Gaia extracts): still in `../PyOccult`.

## Working rules (from the owner)

- **Do not commit or push** unless asked. When asked, give the command; the owner runs it.
- **Never write into `OTEVideo/`, `CZ-Data/`, `DarkLight/` or `Data/`** (owner data, git-ignored; OTEVideo is a symlink to the owner's recordings on an external drive,
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
- Plate solving runs offline when `solve-field` + index files are installed (the Pi has 2MASS
  4202-4219, fields ~12' to degrees; `[astrometry] solver`, CAPTURE.md "Offline plate solving").
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
| `globaltrack.py` | `analyze_global()`: roles, Gaussian peak fits, weighted robust rigid transform, dimming detection, target drift interpolation, sub-pixel circular apertures; `photometry="optimal"` (Naylor optimal extraction, `optimal_stats`) or `"auto"` (both measured in one pass, kept: the lower `reference_scatter`, references against each other, never the target; `lc.photometry`; config template default, viewer button "phot: auto/circle/optimal"; `analyze_global` itself defaults to "aperture"). Seeing = `seeing_fwhm` (SNR²-weighted median). Test case: CZ-Data (476) Hedwig, Carsten's PyOTE fit_metrics.txt (D 18:56:17.903, R 29.905). The SG fit (`fit_peak_savgol`, `savgol_fit`) is kept in the library but no longer offered in the viewer or config |
| `autopick.py` | automatic references: `candidates` (fit, SNR to the sky noise, true peak) on the frame shown, `pick_references` (saturated / faint / not star-shaped / at the edge incl. drift / neighbour in box / next to the target, each with `.note`; brightest first, then farthest point). Viewer `auto_apertures` (key `A`), `request_auto`, `run_batch`; `pystarlight analyze --auto [--target] [--batch]`; target from `--target`, `.target.json`, else the recording's `TARGET-STAR`/`-RA`/`-DEC` (camera header). The fit's own `snr` saturates near 20-30 for bright stars: never use it as brightness |
| `export.py` | PyMovie-layout CSV for PyOTE (`write_pymovie_csv`, `sky_lines`) |
| `tangra.py` | Tangra 3 `.lc` (`write_lc`, `read_lc`; layout from Tangra 3's LCFile.cs, github hpavlov/tangra3: raw deflate of .NET BinaryWriter values, file 4 / header 8 / timing 2 / measurement 9 / footer 14; max 4 objects, 35x35 raw pixel box per object and frame [x-major], reading - background = our signal, mid-exposure times as ticks; RAVF / FITS labelled `FITS` so Tangra looks for no video; not yet opened in a real Tangra) and the movie copy (`write_movie`: H.264 asinh stretch + hqdn3d spatial (frame only) + 2x2 bin, `target` / `curve`: the player's target cross + light-curve panel with time cursor drawn with OpenCV from `capture/playdata.load_analysis`; or FFV1 16-bit lossless; `pystarlight movie`). Viewer: config `[output] tangra_lc`, `movie`, background jobs after Run |
| `timestamps/` | `ocr.py` (PyMovie's VTI OCR, verbatim), `vti.py` (`VtiReader`), `timing.py` (checks, new) |
| `wcs/` | `solve.py` (astrometry.net, orientation, plate scale, `solve_movie`, `field_stars`), `identify.py` (brightest stars, SIMBAD, `identify_positions`), ASTAP in `solve.py` (`solve_astap`, found on PATH / install folders + star databases by file name; `solver_order`: auto = installed offline solvers, ASTAP first with a position hint, astrometry.net first blind, fallback to the other, nova only when none; never bundled, licence not stated on hnsky.org), `localcat.py` (PySyzygos' Gaia DR3, numpy only), `reduce.py` (PRAIA-style Gaia reduction: polynomial up to 3rd order, one-by-one O-C rejection, aperture offsets from their Gaia star, magnitudes on the G scale → `<movie>.astrometry.json`; run by the viewer with the identification on the frame shown), `asteroid.py` (asteroid as the target: `parse_asteroid`, JPL Horizons `path` over the recording, `occulted_star` within 15" of the path; `find_star` uses it), `catnames.py` (UCAC4 / TYC / Gaia DR3 names and target look-up, local catalogue or VO at GAVO/ESA; camera markers), `manual.py` (two-star, verbatim), `astrometry_client.py` (verbatim-ish) |
| `config.py` + `config_template.toml` | private config (`[observer]` station, `[fallback_equipment]`), PySyzygos `sites.py` read for first-time defaults (`pystarlight-config`) |
| `station.py` | `station_of(metadata, fmt)`: RAVF / ADV / SER station data (site + source, observers, equipment, timing) in one layout → `event.json` `recording.station` (PySyzygos `docs/observation_interface.md`); `time_reference` (where in the exposure the time stamps sit: header `TIMESTAMP-REFERENCE`, else RAVF start / ADV start (the file stores the middle, but Adv2's `StartOfExposureTimestampString` is read, as PyMovie; `AdvReader` puts frame 0's `EXPOSURE-S` into the metadata) / FITS DATE-OBS start; SER, AVI unknown) and `mid_exposure_shift_s`: the viewer sets `lc.time_shift_s`, `plot.seconds` adds it, so figures and the step fit are mid-exposure; CSVs keep the recorded stamps |
| `report_push.py` | hand `event.json` to PySyzygos' observation GUI: address + key from the connection file it writes (`~/.config/pysyzygos/observation_api.json`, `connection()`), config `[pysyzygos_observations]` url/token only for another computer; not running: copy into its inbox; stdlib; viewer key `e` / Send |
| `plot.py` | publication figures (`style`, `summary`, panels) |
| `saturation.py` | saturated stars: full scale of a recording (bit depth, ADV MaxPixelValue, a repeated ceiling, dtype), per-aperture fraction of frames >= 90 % (`maxpx`); the viewer leaves references / check stars saturated in > 1 % of frames out of normalisation, zero point and histograms (never repaired), labels SAT live, notes in CSV and `event.json` |
| `steps.py` | multi-step fit of the target flux: D/R times with 1 sigma (`fit_steps`), drawn on `-summary-mag` |
| `diffraction.py` | diffraction-aware fit (`fit_event`; `from_steps` + `compare` from a step fit; viewer button `diffr: off/auto/on`, default `[diffraction] mode` (auto: double-star steps or (Fresnel + star)/speed >= 10 % of a frame; old `enabled = true` = on) -> `-diffraction` figure, event.json `diffraction_fit`; `monte_carlo` (AR(1) noise, `mc_trials` 100, sigma = max(cov, MC)), `components()` (D-R pairing, chords; ambiguous near f = 0.5), geometry from Horizons `asteroid.shadow_geometry` or config): 1-D bar model (exact Fresnel), finite limb-darkened star, double star (own D/R per component + light share `f`), bandpass summed incoherently (`Band.monochromatic` 600 nm default, `Band.from_table` QE file, optional `teff`), exposure box; sinc 2-D engine ported from Bob Anderson's notebooks (his permission; Cubillos & Jimenez 2022). IOTAdiffraction sums fields over the band (small error). docs/diffraction.md |
| `stacking.py` | registered field stack (`stack_field`, `attach_wcs`, `save_field_stack` → FITS + annotated SVG/PNG/PDF), `remove_hot_pixels`; `self_dark` is opt-in only (fails for small drift) |
| `slip_repair.py` | TEMPORARY (VEYE IMX265 word slips, with `capture/slipguard.py`): `pystarlight slip-repair FILE [-o OUT] [--shift PX\|auto] [--check]` repairs a RAVF / SER copy (only pixel blocks rewritten; per-row byte phase, slip position, 8 bytes x slips counted; slips before the start: `--shift`, `auto` finds the wrap seam on a target, not on a dark sky); `<out>.slips.json`; remove with `tests/test_slip_repair.py` and its line in `cli.py` |
| `cli.py` | `pystarlight` (and `python -m pystarlight`): subcommands `analyze` (= `analyse`, `view`), `player` (= `review`), `camera`, `config`; a bare FILE or nothing goes to analyze. The hyphenated commands stay |
| `winlaunch.py` | windowed launchers (`[project.gui-scripts]` `pystarlight-{analyze,player,camera,settings}-gui`): output to `%LOCALAPPDATA%\PyStarLight\logs\<tool>.log`, message box on failure; `icon OUT.ico` (logo as PNG-compressed .ico). Windows setup: `windows/setup.ps1` (per user: private uv, `uv tool install` of the release wheel + `constraints.txt`, extra `desktop`, Start menu / desktop shortcuts, HKCU "Open with" for .ravf .ser .adv .aav .avi, doctor; -Uninstall [-Purge]; ASCII only for PowerShell 5.1), assembled into `PyStarLight-Setup.cmd` by `scripts/build_windows_setup.py`; `.github/workflows/release.yml` on tag vX.Y.Z builds, smoke-tests on windows-latest and attaches everything to the GitHub release (PyPI only with repo variable PUBLISH_PYPI). Test VM: Windows Server 2025 at 192.168.122.103 (see PySyzygos memory windows-test-vm) |
| `doctor.py` | `pystarlight doctor`: one line per part (base packages, Tk, Qt, readers, Aravis, ZWO, solver, settime helper vs repo, config) with a `fix:`; required = extras from `.pystarlight-install.json` / `--extras`; stdlib at the top, runs as a file too (the installer does: a broken package still reports). `EXTRAS_IN` mirrors pyproject's `all` (test) |
| `viewer.py` | `pystarlight analyze` (also `pystarlight-view`): matplotlib GUI (keys, zoom views, live updates, plate solve, identification). Network lookups never in the GUI thread: `_background(name, work, done)` + `finish_jobs()` (tests / `--batch`); `QuickTextBox` (redraw when idle: a full draw is ~0.2 s) |
| `capture/` | capture on the Raspberry Pi 5, `uv run pystarlight-camera` (extras `camera`, `aravis`): `app.py` command line + wiring, `gui.py` Qt GUI via `qt.py` (PySide6 in the venv, PyQt5 with system Python), `cameras.py` backends (Aravis, VEYE MV-MIPI `veye_mvcam.py` (IMX265M: camera line width 2048 of 2064, a multiple of 64 stops its word slips; `line_width`, env `PYSTARLIGHT_VEYE_WIDTH`), any `/dev/videoN` `v4l2_cam.py`: analog grabbers / UVC, luminance only, no trigger, driver time stamps; ZWO ASI `asi_cam.py`: ZWO's closed libASICamera2 via ctypes, own reader thread + ~512 MB ring, free run, stamped on arrival minus exposure, `--asi-bandwidth` / `--asi-high-speed`, tested on an ASI178MM (full-frame losses are SDK stalls, CAPTURE.md); `can_trigger`, `exposure_fixed`) + `CameraSession`, `decode.py`, `timing.py` (GPS pulse stamping, GPS UTC clock `GpsClock` (only valid UTC: leap seconds known), `utc_now_ns`: the app's own times are GPS UTC once anchored, VEYE calibration and per-sensor timing profiles: IMX178 measured, Sony global shutter e.g. IMX265 assumed), `sysclock.py` (Pi system clock stepped from the GPS clock, not while recording, through the root helper `settime_helper.py` installed as `/usr/local/sbin/pystarlight-settime` + sudoers: adds an offset, sets the RTC), `acquisition.py` (Grabber, Controller), `recorder.py` (thread, `repair`), `ravf_writer.py` (default, as ASTRID) / `ser_writer.py` (`.times` sidecar; both `drop_cache` after each 1 s sync: a full page cache stalled USB), `stacker.py` (live stack aligned on the stars -> FITS), `slipguard.py` (TEMPORARY, VEYE IMX265 word slips: the camera moves its byte stream by 8 bytes now and then; frames whose MIPI packing is off are dropped and the stream reset; `--slip-guard off|on|auto`, default off (the owner prefers post-processing); remove: the file, `tests/test_slipguard.py`, lines marked `slipguard`), `gps_ublox.py`, `favorites.py` (PySyzygos server favorites API: tonight's events fill asteroid, target, recording window; stdlib only), `station_header.py` (config `[observer]` station/equipment + GPS site only with a fix, else config site marked `SITE-SOURCE`; RAVF entries / SER `.ser.meta.json` sidecar; `FIELDS` + `missing()`: the GUI's Station & equipment dialog, required before recording; `digest`/`check`/`protect`: `STATION-DIGEST`, read-only finished files; the GUI arms only with a GPS fix, `site_meta` takes the fix at the first frame), `localconfig.py` (private config without the package; `update_section` writes one table back, comments kept), `indi.py` (INDI XML client + `Server` (indiserver) + `drivers_installed`, stdlib), `mount.py` (Mount: goto / sync / tracking / park, J2000 <-> JNow precession, sidereal time, `parse_radec`, `altaz`), `guider.py` (star lock: `StarLock` lock stars / certainty gates / hold / lost + pattern search / calibration / PI pulses via a callback, `LockThread` fed by the grabber like the stacker; numpy + OpenCV only; config `[lock]`), `lock_gui.py` (`LockDock`, menu INDI Telescope Controls / Shift+L: Lock check box (manual, independent of recording), Guide the mount through the Mount panel, `_lock.csv`, `FluxPlot`), `telescope_gui.py` (`MountDock`: the camera window's Mount dock, Solve & sync, Center loop; menu INDI Telescope Controls / Shift+M; config `[indi]`; tests against `indi_simulator_telescope`, skipped without indiserver), `focuser.py` (INDI focuser: absolute / relative / timer, `find` prefers a non-mount device; on the Mount panel's connection, the mount connected first, `usb_hint` for a ZWO EAF; a focuser alone connects without a mount), `focuser_gui.py` (`FocuserWindow`: a top-level tool window, Shift+F, focus mark per focuser in QSettings, `CurvePlot`), `focus.py` (focus curve: `hfd` (connected patch only), `pick_star`, `fit_curve` (HFD² parabola), `FocusMeter` fed by the grabber like the lock; closing the window stops it), `dslr.py` + `dslr_gui.py` (a DSLR / mirrorless camera as a second, photo-only camera beside the video camera, libgphoto2 + rawpy, extra `dslr`: settings from the camera's lists (names per maker in `NAMES`), nearest shutter speed or bulb timed here, `capture()` -> `Shot` (all files, release time), `save()` keeps the camera's files + FITS (Bayer mosaic uint16 BAYERPAT / RGB planes), `DslrThread` owns the camera (gphoto2 not thread-safe; sequences will loop capture requests on it); window Shift+C, config `[dslr]`; seen with the owner's Sony A7R II in PC Remote mode), `schedule.py` + `schedule_gui.py` (photo programs, from the owner's SoFiMagic (Sony app + gMagicPy): its XML read / written (`load_sofimagic`, `to_sofimagic`: n/d shutters, int offsets), our TOML (`EXAMPLE_TOML`), refs incl. C0 and NOW, phases shots N / -1 / every, exposures S/C/M/L/B (brackets `BRACKETS`); `timeline`; `Runner` (prepare before the slot, skip when the next slot is due, no exposure past the next phase's start (`limit`), errors logged, CSV photolog); the editor is the Program tab of the DSLR window, ported from SettingsActivity.java; files fits / files / card via `dslr.ProgramCamera` + `FitsSaver`; DSLR files and logs named `<model>_<asteroid>_<star>_<UTC>Z_...` as recordings (`MainWindow.file_tag`)), `photo_solve.py` (DSLR photo plate solve as the stack's: luminance of the full mosaic -> `solve_stack` (timeout for the blind pass), `marks.solution_marks` (catalogue names, T / V, detect check; also the camera window's `start_marks`), WCS + STARn + TRGX/Y into the FITS by `write_fits` (mosaic kept), `.wcs.fit`, `.solve.json`, `_annotated.png`; CLI; `dslr.pixel_um_of` table for the scale hint), `photo_stack.py` (colour stack of DSLR photos: each solved, `homography` through both WCS onto the first's pixels, weighted mean, leave-one-out clip from 5 frames, threshold Student-t for the m others at kappa's Gaussian tail (`clip_limits`; plain all-n sigma cannot clip 1 outlier, plain 3 sigma vs 4 others clips 10 % of the noise), stack solved via photo_solve; `--bin 2` for memory), `imageview.py` / `widgets.py` / `station_dialog.py` (split out of gui.py; gui re-exports them), `polar.py` + `polar_gui.py` (polar alignment: RA axis from 2 (mount-reported turn) or 3 (plane) plate solves, ENU frame with refraction, knob model Az about vertical / Alt about the horizontal axis across the pole; live: `Alignment` gets the knob turns from one followed star (a second `FocusMeter`, `c.polar`, `follow` = whole box), arrow Az leg then Alt leg, `rebase` on a new solve; `plan_moves`: min altitude, no meridian crossing; window Shift+P, config `[polar_align]`; overlay `ImageView.polar`), `bodies.py` (Moon and planets as targets: astrometric (light time, no aberration: astropy's get_body is apparent, 20" off a plate solution), builtin ephemeris offline (vs JPL Horizons up to ~22" for Jupiter) or DE440s when jplephem + the file are cached (never downloads), + our WGS84 topocentric shift; tests hold Horizons values; the Sun refused; camera target (`update_body` every 10 s, file names `Jupiter`, marker at the stack's time) and the Mount panel's target / From target / Center), `starnames.py` + `star_names.json` (proper star names offline: ~390 SIMBAD 'NAME' stars to V 13, also their designations 'alf UMi' / 'alpha UMi' / '27 Tau', IAU name preferred (`PREFERRED`); J2000 + proper motion; remake with `scripts/make_star_names.py` (SIMBAD CfA mirror first); `catnames.parse_target` -> ('name', ...), `resolve_target` -> the UCAC4 / TYC / Gaia star there; camera target at once from the table, UCAC4 in the background; Mount panel target), `photo.py` (single photo to FITS: header, the plate solution's WCS moved onto the photo, named stars), `version.py` (camera and player version numbers, own numbering; README overview must match, `tests/test_versions.py`), `playdata.py` (the player's Qt-free data: `load_timing`, `load_analysis`; player re-exports them), `player.py` (`pystarlight player`, formerly review: timing check, GPS status, frame status, stack & solve a frame range; the camera's "Play last clip" starts it). Plate solve of a stack: `wcs/solve_stack.py`. `capture/` itself imports nothing else from the package (only the solve does), so `python3 src/pystarlight/camera.py` still runs with the Pi's system Python. Recordings survive crashes; `--repair FILE` finishes them |

Method slides: `docs/math/` (beamer, `make` -> `pystarlight_math.pdf`, not in git): `math_slides.tex` is the
single source (overview, alignment, astrometry, G scale, Naylor, step fit; formulas as in the code: keep in step),
`preamble.tex` kept equal to PySyzygos' `docs/math/preamble.tex` (same colors and macros), `pystarlight_math.tex`
(title, references). The owner's private PySyzygos talk (`../PyOccult/slides/`) `\input`s `math_slides.tex` from here.
README's slides paragraph (after the analysis modes) links docs/math. No GitHub release yet: with the first one,
attach `pystarlight_math.pdf` as an asset and add its link there
(`https://github.com/pyzahl/PyStarLight/releases/download/<tag>/pystarlight_math.pdf`, tag in URL and link text);
with every later release update that link.

`ocr.py`, `wcs/manual.py`, `wcs/astrometry_client.py` are kept close to PyMovie's files and
excluded from ruff, so upstream fixes diff cleanly. Don't reformat them.

## Commands

```bash
uv sync --extra all                                  # repo venv (.venv, Python 3.12): plot, ravf, adv, catalog
./install.sh [extras] [--no-pull|--rebuild|--check]  # install/update (scripts/install.py, Windows install.cmd): pull, system check, sync with remembered extras, doctor
uv run --group test pytest -q                        # current libs; PyMovie parity tests skip
uvx ruff check                                       # lint (line length 110)
uv run pystarlight OTEVideo/<run>/<run>.ravf         # analyze (owner's data)
uv sync --extra all --extra camera --extra aravis     # on the Pi: capture app too
uv run pystarlight-camera                            # capture app (CAPTURE.md)
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
APO107 with ×0.5 reducer (measured ≈ 343 mm, 2.07″/px). Station data in the private config's
`[observer]` (mm: `aperture_mm` 107, `focal_length_mm` 343); old ASTRID recordings carry no
optics and use `[fallback_equipment]` (343 mm, 3.45 µm). PySyzygos site `Home-Deck` (`default_site`);
the `[sites.<id>]` tables are gone.
Test recording `None_20261011_042339_1` (near Deneb): target TYC 3574-1718-1, refs HD 197161,
BD+44 3540, BD+44 3545.
