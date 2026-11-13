# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `./install.sh` / `install.cmd` (`scripts/install.py`): install and update in one command on
  Linux, macOS and Windows. It runs git pull, checks the system before building (Tk, the
  Aravis build packages), runs `uv sync --locked` with the extras remembered in
  `.pystarlight-install.json`, builds Adv2 on Apple Silicon and finally runs the check.
- `pystarlight doctor` (also `python src/pystarlight/doctor.py`): checks the installation and
  prints a fix for every missing part.

### Fixed
- Auto ap / `A` after a run crashed and left no references: placing the target redrew the old
  result plot before the references were there. A run without references now says so instead
  of failing in the normalisation.
- Global mode: the seeing (optimal extraction's profile FWHM, the auto radius) is the stars' FWHM
  median weighted by SNR^2 (`seeing_fwhm`), no longer the plain median: three faint references
  pulled it 0.1-0.2 px narrower and changed a marginal event's result.
- ADV / AAV: D and R were **half an exposure early** (0.5 s on a 1 s DVTI recording). The reader
  takes Adv2's start-of-exposure time (as PyMovie; the file stores the middle) but the analysis
  took it as mid-exposure and never shifted it. Now `start`, shifted by half the frame's
  exposure (`EXPOSURE-S` from the frame). The CSV files keep the times as before.
- Viewer froze while a star was entered: the lookup (SIMBAD can take 20 s to give up) ran in the
  GUI thread, also when just clicking elsewhere, and every key redrew the whole window (~0.2 s).
  Lookups and Auto now run in the background, the text boxes redraw when idle.
- Two apertures on one star (e.g. **Set target** onto a star that had a reference): the reference
  put the target into its own normalisation and hid the drop. Set target, `t` and Run now remove
  the second aperture and say so (`drop_doubles`).
- Star field: hyphenated catalogue names (`UCAC4-593-004965`, `TYC-...`); a catalogue name that
  is not in the field says so at once instead of asking SIMBAD.

### Added
- Camera: the app runs on GPS UTC once the GPS clock is anchored (start now, set start time,
  stop, Favorites), not on the Pi clock, and sets the Pi's system clock from it (no NTP in the
  field): right after anchoring, then every minute when off by more than 20 ms, not while
  recording. A root helper adds the offset and sets the RTC (`settime_helper.py`, installed as
  `/usr/local/sbin/pystarlight-settime`, CAPTURE.md "System clock from the GPS"). GPS box:
  **Pi clock** line. Config `[camera] set_system_clock`, `system_clock_tolerance_ms`.
- Viewer: **Delete** or **Backspace** (the Mac's delete key) removes the aperture under the mouse,
  as the right click does (on macOS the right click does not always arrive).
- Global mode: `photometry = "auto"` (new default; viewer button **phot: auto / circle /
  optimal** in place of the Optimal checkbox): both measured in one pass, kept is the one whose
  references scatter less against each other (`reference_scatter`; the target never counts).
  Chosen method and both scatters in the status line and `event.json` `photometry.choice`.
- Analysis: **automatic apertures**. `pystarlight analyze --auto [--target STAR] [--batch] FILE`
  plate-solves, puts the target aperture on the named star (`--target`, the star field, or the
  recording's new `TARGET-STAR` / `TARGET-RA` / `TARGET-DEC`), picks references and runs;
  `--batch` without a window, exit code 1 when nothing was found. In the viewer: type the target
  in the star field and click **Auto ap** (or key `A`) to place them.
  References (`autopick.py`, config `[auto]`): not saturated, bright enough, star-shaped, alone in
  their box, inside the frame over the whole drift; the brightest first, then spread over the field.
- Viewer star field: an **asteroid** as the target (`(476) Hedwig`, `476 Hedwig`, `2003 SB60`):
  its path during the recording from JPL Horizons (`wcs/asteroid.py`, topocentric), the target is
  the catalogue star on it. Auto also reads the star from OBJNAME
  (`(476) Hedwig occ. UCAC4 442-124351`) or the asteroid in it.
- Camera: the target star goes into the recording as `TARGET-STAR` (catalogue name once looked
  up), with `TARGET-RA` / `TARGET-DEC` (RAVF header, SER `.meta.json`).
- Camera: **Mount** panel (INDI; menu Mount / Shift+M, a dock that can float or hide; drivers and
  connection in Mount → INDI settings…; the jog area green / red for tracking): drivers from
  the installed ones (ZWO AM5 `indi_lx200am5`, ASI EAF `indi_asi_focuser`, simulators), our own
  `indiserver` or one at host:port (config `[indi]`), USB or Wi-Fi with the port found by itself
  (the AM5 driver's default is Wi-Fi), site and time from the GPS, position in
  J2000 and alt / az, goto (with or without tracking), sync, tracking, abort, park, home, jog N/S/W/E at the driver's speeds (AM5 0.25x .. 1440x), **Solve &
  sync** and **Center** (goto, solve, sync, repeat to 1′). `capture/indi.py` (an INDI XML client,
  standard library), `capture/mount.py` (IAU 1976 precession J2000 <-> JNow, sidereal time),
  `capture/telescope_gui.py`. Tested on INDI's telescope simulator.
- Camera GUI: exposure and gain are remembered per camera model and restored at start (the
  camera itself may hold another program's values); `--exposure` / `--gain` override.
- ZWO ASI: a reader thread of its own and a ~512 MB frame ring (1024 x 768 at 47.6 fps: 0 frames
  lost in 60 s, 8 before); `--asi-bandwidth PCT` and `--asi-high-speed on/off` (the camera keeps
  another program's settings; the start-up line and the recording's comment say what is used).
- RAVF / SER writers drop the pages they have synced from the page cache (`posix_fadvise`), so a
  fast recording doesn't fill the memory and stall the camera's USB transfers.
- Camera GUI: exposures up to 60 s (`--max-exposure`, default now 60000 ms, was 2000). The
  slider spans up to 2 s; typing more than 2 s (or the `E` key past it) switches it to
  0.5 .. 60 s, typing less than 1 s back. Dragging never switches it.
- Camera: ZWO ASI cameras (`capture/asi_cam.py`) through ZWO's libASICamera2 (ctypes; found via
  `$ZWO_ASI_LIB`, the linker path, `/usr/local/lib`, `/usr/lib/<arch>-linux-gnu`): RAW8 / RAW16,
  region, exposure, gain in 0.1 dB; free run, frames stamped with the Pi clock on arrival minus
  the exposure, SDK-dropped frames as gaps. Tested with an ASI178MM (USB3, SDK 1.27).
- Recordings say where their time stamps sit in the exposure: header `TIMESTAMP-REFERENCE`
  (`start` / `middle` / `end` / `unknown`; PyStarLight's camera: start), and the pixel pitch as
  ASTRID does (`INSTRUMENT-SENSOR-PIXEL-SIZE-X/Y`, also read for the plate-scale hint). Both in
  the station digest (older recordings stay valid).
- Install notes: the player needs Qt (PySide6 or PyQt5). The install step is now
  `uv sync --extra all --extra camera` (PySide6 wheels on Linux, macOS, Windows), with notes per
  OS and a check line. Without Qt, `pystarlight player` / `camera` now say what to install
  instead of stopping with `No module named 'PyQt5'`.
- Player: the target's light curve, the step-fit levels and D / R from the analysis
  (`<movie>.pystarlight.csv`, `.event.json`) as a second track below the frame timing (relative
  to its median, scaled on the target alone), and an open crosshair on the target on every
  frame (its tracked centre and catalogue name; **Target** box to hide it); the
  timing track's labels read max / min / typical step, and it has a tooltip.
- Saturated stars (`saturation`): an aperture over a saturated star is labelled SAT on the frame
  and warned about when placed; after Run, references and check stars saturated in more than
  1 % of the frames are left out of normalisation, magnitude zero point and histograms (not
  repaired), a saturated target is flagged; notes in both CSVs and `event.json`
  (`photometry.saturation`). The ADV reader exposes `max_pixel_value`.
- One command, `pystarlight` (also `python -m pystarlight`): `analyze` (= `analyse`, `view`),
  `player` (= `review`), `camera`, `config`; `pystarlight FILE` analyses, plain `pystarlight`
  opens the file dialog. The hyphenated commands stay.
- Viewer: a clear message at start when no window can open (Python without Tk: what to install
  on Debian / Fedora / Arch / Homebrew, or uv's own Python; or no display), instead of
  matplotlib's silent fallback.
- README: install per OS (Linux with `python3-tk`, macOS, Windows), uv, a check step, the
  extras explained; installs from GitHub (not on PyPI yet).
- Station data kept as recorded: `STATION-DIGEST` (SHA-256 of the station, site, timing and
  camera entries) in the RAVF header / SER sidecar, finished recordings and sidecars made
  read-only, `recording.station.integrity` (`ok` / `changed` / `none`) in event.json.
- Camera: with a GPS receiver, Record arms only with a fix (waits for one); the header's
  position is the fix at the first frame (or the last fix, at most 10 min old, with
  `GPS-FIX-AGE-S`). `Gps.last_fix`.
- Camera: any Linux video device (`/dev/videoN`, `capture/v4l2_cam.py`): USB grabbers for
  analog cameras, UVC webcams. Luminance of GREY / Y16 / YUYV / UYVY / MJPG, frames unscaled
  (fields woven), region cut in software, `--video-std`, `--video-input`, `--video-size`, the
  device's exposure / gain controls where it has them. No trigger: driver time stamps (Pi
  clock), marked as such in the header; the true time from an embedded VTI time code or GPS
  LED flashes in the image.
- Station data for the occultation report (PySyzygos `docs/observation_interface.md`): config
  `[observer]` gains observer2, more_observers, station, telescope_type, aperture_mm,
  focal_length_mm, focal_reducer, mount, observing_method, camera_delay_s(+_applied); the
  capture writes them into every recording (`capture/station_header.py`), with the GPS site only
  with a fix (text copies at 7 decimals, altitude MSL + geoid separation from GGA field 11, fix,
  satellites, HDOP, fix time; else the config's site marked `SITE-SOURCE = "config"`, or none).
  SER recordings carry the same keys in `<file>.ser.meta.json`, read by `SerReader`.
- `station.station_of`: RAVF / ADV / SER station data in one layout, `event.json`
  `recording.station`; event.json also gets the time base per event, the searched interval and
  the light-curve file names.
- `report_push`: viewer key `e` / **Send** (and `auto_send`) hands event.json to PySyzygos'
  observation GUI, found through the connection file it writes at its start
  (`observation_api.json`: url, key, inbox; no configuration); not running: copied into its
  inbox. `[pysyzygos_observations]` url / token for a PySyzygos on another computer.
- Docs: CAPTURE.md "PySyzygos favorites server link" (server mode, token, config, a check
  from the Pi, what the Favorites button fills in, error messages); README "Event timing: the
  automatic step fit" (what is fitted, change-point search, sub-frame timing and its sigma,
  magnitudes and limits, D/R pairing, validation, limits of the model).
- Gaia DR3 astrometric reduction in the manner of PRAIA (`wcs.reduce`, `python -m
  pystarlight.wcs.reduce MOVIE`): measured stars of the solved stack paired with the local
  catalogue (proper motions to the date), tangent-plane polynomial (4 constants to 3rd order),
  outliers dropped one by one; RA/Dec with errors anywhere, and each aperture's offset from its
  Gaia star. The viewer runs it with the identification when the local catalogue is there, shows
  the O-C and the target's offset, flags apertures more than 3 sigma off, and saves
  `<movie>.astrometry.json`. `localcat.lookup` also returns the star's RA/Dec. The viewer
  reduces the frame shown (`i` on any frame; WCS moved by the drift, `reduce.shifted_wcs`) and
  measures all matched stars and apertures on the Gaia G scale (`reduce.measure_mags`): target
  magnitude with error and its difference from G in sigma of the field's scatter. Then on every
  frame shown (`reduce.remeasure`: drift by phase correlation, tracked centres after a run, new
  zero point per frame) the aperture labels show the measured `m=` (target: error, difference
  from G, sigma), so a fading target is followed.
- Second result plot `<movie>-summary-mag` (same layout) in magnitudes: on the catalogue G (or V)
  scale of the identified references, per-frame zero point from their summed, smoothed signal
  (`normalize.magnitudes`, `plot.summary(mags=...)`, `plot.magcurve`); without catalogue
  magnitudes Δmag to the references. Updated live like the first.
- Step fit of the target for occultation times (`steps.fit_steps`): piecewise-constant model of
  the flux relative to the reference ensemble (`normalize.ensemble`), change points by circular
  binary segmentation with a BIC-like penalty (short events too), levels that differ by less than
  5 sigma merged, each D/R time to a fraction of a frame (partial exposures) with a 1 sigma error
  (chi^2 + 1 interval or Monte Carlo, the larger; calibrated on simulated events). Drawn on the
  magnitude summary with level bands, time error bars and transition close-ups (`plot.stepmodel`,
  `plot.stepzoom`); the times in UTC in the viewer's status line. No false events in noise.
- Camera: **Favorites** button and **Asteroid** field (`capture/favorites.py`, `--pysyzygos URL`,
  `[pysyzygos_server]`): tonight's events (next 12 h) from a PySyzygos server's favorites API,
  a list when there are several; fills asteroid, target star, its Gaia DR3 id to verify, and the
  recording window (event - 40 s to + 40 s; `past_hours` lists recent events too, for tests).
  The asteroid goes into the file name before the star
  and into OBJNAME.
- Viewer star field: a target star by Gaia DR3 id, UCAC4, TYC, HIP name, RA/Dec or SIMBAD name
  (`wcs.identify.find_star`, `localcat.parse_star_id`, `LocalGaia.find`: the solved field searched
  in the local catalogue, offline) is marked on the image with open ticks and its name, following
  the drift; **Set target** moves or creates the target aperture there and names it after the star.
  Saved in `<movie>.target.json` and in the event file (`marked_star`).
- `<movie>.event.json` with every result (`steps.event_report`, `movie.object_name`): object,
  target star, references, check stars, fitted levels in flux and magnitude, D/R times in UTC with
  errors, nested D/R pairs and durations, recording metadata. The magnitude summary lists each
  level's magnitude +- error in a box next to the D/R times.
- Third result plot `<movie>-mag-hist`: magnitude histograms (probability density, common bins)
  of the target in each fitted level and, below, of the check stars (else the two references
  nearest its magnitude), with fitted
  Gaussian envelopes (`plot.mag_histograms`, `plot.maghist`, `plot.gaussian_envelope`).
- Global mode: Naylor optimal extraction (`analyze_global(photometry="optimal")`,
  `[analysis] photometry`, `optimal_radius_fwhm`, `seeing_frames`; `globaltrack.optimal_stats`,
  `pixel_gaussian`): pixels weighted by a circular Gaussian of the frame's seeing (median FWHM
  of the reference stars, running median) at the tracked position, the same profile for all
  stars; defect pixels get no weight. Per row `frame_info['psf_fwhm']`. Opt-in: on the owner's
  recordings the 1.5 x FWHM circle stayed better (scintillation / correlated video noise).
- Capture: per-sensor timing profiles for VEYE cameras (`timing.SensorTiming`): the measured
  IMX178 model, and an assumed (not yet measured) Sony Pregius global-shutter model for the
  IMX265M / IMX264M / IMX296M / IMX287M (exposure starts at the trigger, all rows at once,
  readout overlapping the next exposure). GUI, y-trigger, missed-pulse model and RAVF header
  follow the profile.
- Capture: GPS-mode frame stamps use GPS UTC only (`GpsClock`): the Pi's monotonic clock is
  anchored on the 1 Hz timepulse with UBX NAV-TIMEUTC (leap seconds known) and kept by the
  trigger pulses, with drift tracking; no NTP. Re-anchors on every rate change and before every
  GPS recording (Record waits until the stamps are exact; a set start time anchors 20 s ahead).
- Capture: GPS rate typed in the GUI or `--gps-rate` as fps or period (`1.5`, `250ms`, `2s`), 1.5 fps
  in the list; rates that aren't whole Hz go to the GPS as a µs period with the pulse phase from
  UBX TIM-TP. Rate changes stamp on both pulse grids until the GPS confirms.
- Capture: `--gps-pulse-ms` (timepulse length, for LED timing checks).
- Capture: the y-trigger line can be dragged; dotted lines mark where the exposure-smeared LED edge
  begins and ends.
- Capture: live stacking aligned on the stars (key `s`, `--stack-frames`) and plate solve of the
  stack on nova.astrometry.net (`--focal-mm` for the scale hint): RA/Dec of the field centre,
  field size, orientation, alt/az, the brightest stars with names, circled on the stack.
  Standalone: `python -m pystarlight.wcs.solve_stack STACK.fits`.
- Review player `pystarlight-review` (and "Review last clip" in the camera app): frames with UTC,
  step and per-frame RAVF status, timing problems (dropped / irregular / sequence gaps) with a
  clickable step plot, stack & plate solve of a frame range, optionally saved as the movie's
  plate solution for pystarlight-view.
- Capture runs in the uv venv: `uv run pystarlight-camera`, extras `camera` (PySide6, pyserial)
  and `aravis` (PyGObject); the GUI supports PySide6 and PyQt5.
- `solve_astrometry_net(downsample=...)`.
- Offline plate solving with a local astrometry.net (`solve-field` + index files): used
  automatically when installed (`[astrometry] solver = "auto" | "local" | "nova"`), for the
  viewer, `solve_movie` and the stack solve (`wcs.solve.solve_local`).
- Capture: RAVF recording (the format ASTRID writes), now the default (`--record-format`, GUI
  "Format"); header with camera, Pi, GPS position, timing accuracy and source, rolling-shutter
  line time, ROI and trigger; per-frame exposure, GPS fix and sequence.
- Capture: recordings survive a crash or power loss. Both formats sync to disk every second;
  SER timestamps also go to a `.times` sidecar until the file is closed. `--repair FILE ...`
  finishes interrupted files in place. The RAVF and SER readers open unfinished recordings as
  they are (frame scan / sidecar), with a warning.
- Alternative peak fit for global mode (`analysis.savgol_fit`, viewer checkbox "SG fit"):
  2-D Savitzky-Golay smoothing, then an axis-aligned 2-D Gaussian fit (`fit_peak_savgol`);
  photometry circles sit on each star's fitted centre where the fit is good, else on the
  global solution. The standard fit stays the default.
- Viewer button "Fit frame" (below "Center all"): peak fit and circle photometry of every star
  aperture on the displayed frame, without moving apertures; shown in the zoom views and status.

### Changed
- Camera, Favorites: the recording window is event -30 s to +30 s by default (was 40 s), set in
  the new config section `[camera]` (`record_before_s`, `record_after_s`).
- Optimal extraction is the viewer's default photometry (`[analysis] photometry = "optimal"`,
  checkbox ticked); untick it for the circular aperture. `analyze_global()` keeps "aperture".
- Analysis: a click makes the first aperture the target and every later one a reference
  (`ref1`, `ref2`, ...; PyMovie colours red / yellow to match), instead of role-less red
  apertures that all looked like targets. `t` on the target no longer renames it `target1`.
- The review player is now the player: `pystarlight player` / `pystarlight-player`
  (`capture/player.py`, `PlayerWindow`); the camera's button reads "Play last clip".
  `pystarlight-review` still works. Without a file it opens a file dialog (the last folder used;
  a FITS frame stands for its folder).
- The recording's own optics give the plate-scale hint (`solve.recorded_scale`: effective focal
  length and the sensor's pixel pitch); `[fallback_equipment]` (new) only for videos without
  them, with a WARNING. `[sites.<id>]` tables are gone (PySyzygos' `sites.py` stays, for the
  station dialog's first-time defaults); "recorded elsewhere" and the stack's alt/az use the
  `[observer]` fallback position. The live
  solve's hint defaults to the station's focal length. `recording.station.warnings` lists the
  essentials a recording lacks (observer, telescope, aperture, focal length, position, time
  source), shown by the viewer after every Run.
- Station dialog: the first time (no station data in `[observer]`), its empty fields are filled
  from the PySyzygos default site; `[observer]` alone is used for recordings.
- Station optics in millimetres end to end: config `aperture_mm` / `focal_length_mm`, header
  `APERTURE-MM` / `FOCAL-LENGTH-MM`, event.json `equipment.aperture_mm` / `focal_length_mm`
  (the cm keys and entries of earlier versions are read and converted x 10). Plausibility
  check (aperture 10 mm .. 1 m, focal length 50 mm .. 20 m, f/1 .. f/40) before recording.
- Camera: the Observer / Telescope fields in the Recording panel are replaced by a **Station &
  equipment** dialog (one button) for all `[observer]` keys, saved into the private config
  (`localconfig.update_section`: only those lines change). Recording or arming needs the
  required station data; with any missing, the dialog opens first.
- Capture: no GPS position in the header without a fix (was the last position, or 0).
- Result plots: the title names the candidate star (the identified star in the target
  aperture); axis units written as "in": `G in mag`, `Signal in ADU`, `Drift in px`,
  `x in px`, `Time ... UTC in s` (also the stack figure); the magnitude axis no longer says
  "(from N references)".
- Viewer: the checkbox "SG fit" is now "Optimal" (photometry for the next run and Fit frame);
  Fit frame and auto-centring use the standard peak fit. The `savgol_*` config keys are gone
  (old private configs keep working); the SG functions stay in `globaltrack`.
- Capture: VEYE GPS-trigger timing offset 0.33 → 0.51 ms (re-measured with a 1 ms LED flash, see
  CAPTURE.md); GPS stamps recorded before are about 0.18 ms early. The timepulse is set on the
  UTC grid explicitly; non-whole-Hz rates are no longer aligned by the GPS (TIM-TP instead).
- Capture code split into the `pystarlight.capture` package (GUI, camera backends + session,
  decoding, timing, threads, recorder, file writers); `camera.py` is now a launcher with the same
  command line.
- Automatic photometry radius is `[analysis] radius_fwhm` x median FWHM (default 1.5 as
  before), so it follows the seeing; ~0.85 gives the best SNR for faint, sky-limited targets.
  Minimum radius 1.5 px (was 2.0).
- The viewer no longer submits a plate solve when a movie is opened: it loads a cached solution,
  and only key `p` uploads to astrometry.net.
- Plate solve: burnt-in timestamp rows are blanked before the upload; a recording's own RA/Dec
  (ADV metadata) is sent as the field-centre hint, and the site's plate-scale hint is dropped
  when the recording was made elsewhere (it made such solves fail).
- Plate-solve hints are configurable in `[astrometry]`: `scale_hint` (false = blind solve),
  `scale_arcsec` (override the plate scale), `center_hint` (send the recording's RA/Dec).
- Viewer: plate solve, identification and stacking report on their own line below the status,
  so they no longer hide "wrote ... -summary" and other messages.

### Fixed
- Viewer without Tk: the hint names the Python the environment runs on. A uv environment built
  on the system's Python (uv takes it when it finds one) gets the two fixes that apply: Tk for that
  Python, or the environment rebuilt on uv's own (`rm -rf .venv && uv sync --python-preference
  only-managed --extra all`); the old hint suggested uv as if it were not in use.
- SER recordings of a colour camera say their Bayer pattern (ColorID 8-11: RGGB, GRBG, GBRG,
  BGGR), also after a repair; they were always marked mono. RAVF had it (`COLOR-TYPE`).
- Event timing: the step fit and all figures now use mid-exposure times. They took the time
  stamps as mid-exposure, but RAVF (ASTRID, PyStarLight) stamps the exposure start: D / R times
  came out half an exposure early (0.25 s at 0.5 s exposures). Shift from the recording's
  `TIMESTAMP-REFERENCE` or its format (`station.time_reference`), in `event.json`
  `step_fit.time_base`; the CSV files keep the recorded stamps.
- Capture (VEYE): the frame period used for GPS stamps and the y-trigger could keep another bit
  depth's value after a depth change (stale cached register, and a frame-rate register faster
  than the sensor can read out); 12-bit stamps on a small region were up to ~1.2 ms early. The
  register is re-read after each new stream and the IMX178 12-bit period is floored at its
  readout limit.
- Capture GUI: '&' in the "Stack & plate solve" title showed as an underline marker.
- Star names on coarse plate scales: the SIMBAD search radius is now 2 px when that is more
  than 10" (wide-field stacks named only 2 of 5 bright stars).
- Capture: Ctrl+C during start-up (before the window was up) was ignored and left the app
  running.
- Global mode failed with KeyError for apertures without a role (since the SG-fit option).

## [0.1.0] - 2026-10-11

### Added
- Movie readers for AVI/MOV, SER, ADV/AAV, RAVF and FITS folders (`open_movie`).
- Frame corrections: gamma, CMOS defect pixels, hot pixels, line-noise filter, dark/flat.
- Robust background, star masks, brightest-pixel centroid, tight-mask extraction (TME).
- Aperture tracking with 0, 1 or 2 yellow apertures (field rotation) and track paths.
- `analyze()` pipeline with progress and cancel hooks, CSV output.
- VTI timestamp OCR (IOTA 2/3, BoxSprite, Kiwi NTSC/PAL, GHS) with PyMovie-compatible
  profile files, per-field times and automatic field-order detection.
- Timing checks: dropped and duplicated frames, unreadable times, midnight rollover.
- Target location: nova.astrometry.net plate solving, two-reference-star solution
  (with a `mirrored` option PyMovie lacks), UCAC4 lookup.
- Dark/flat calibration (PyMovie formulas) applied to aperture boxes only; defect maps.
- Publication figures with matplotlib (`plot`): field with apertures, light curve, drift, timing.
- `pystarlight-view`: minimal matplotlib viewer to step frames, see UTC, place apertures and run.
- Global mode (`analyze_global`): aperture roles (target, reference, check, sky); all stars
  track together via 2-D Gaussian peak fits and one robust shift + rotation; target excluded
  from tracking while occulted or fading, its offset interpolated across the event; sub-pixel
  circular apertures; local or sky background.
- Config file from a packaged template (`pystarlight-config init/show/path`): observer, site,
  astrometry.net key, paths, aperture roles, analysis and plot settings.
- Automatic plate solve when a movie is opened in the viewer (median stack, plate-scale hint
  from the site's optics, private upload, cached); RA/Dec readout, N/E compass, orientation
  (scale, north angle, mirrored). Sites compatible with PyOccult's (now PySyzygos') `sites.py`.
- Fixed (inherited from PyMovie): the image uploaded to astrometry.net was written as int16,
  wrapping 16-bit pixels above 32767 to negative values.
- PyMovie-layout CSV export for PyOTE (`LightCurves.to_pymovie_csv`; the viewer writes it as
  `<movie>.csv`), identical to PyMovie's own file on a real recording and read by PyOTE's reader.
- Viewer: reference key `r`, zoom views (keys 1/2), ±10 s buttons, key list, own keys freed from
  matplotlib's bindings (`f` stays full screen).
- Bright-star identification after the plate solve (astrometry.net annotations, else SIMBAD:
  name, HD/HIP/TYC/Gaia DR3 IDs, V), labelled in the viewer and cached; pixel -> RA/Dec mapping
  and aperture RA/Dec in both CSV headers and in apertures.json.
- Catalogue identification of every target/reference/check aperture (key `i`, also automatic),
  drift-corrected to the solved frames; saved in apertures.json and written to the CSVs.
- Zoom views update live while analysing (throttled, from the worker's latest frame) and show
  signal and peak-fit readings (FWHM, SNR, fitted centre, photometry circle).
- Dark/flat clips in the config (`[calibration]`), averaged into cached masters.
- Private config can live in the project root (`pystarlight.toml`, git-ignored; `init --here`);
  `PYSTARLIGHT_CONFIG=none` ignores all config files.
- Stacked field image after each run (registered with the tracking solution, hot pixels and
  cosmic rays removed), as FITS with WCS and annotated SVG/PNG/PDF.
- Viewer display controls: manual levels (range slider), Auto, per-frame auto levels, display
  gamma (main image and zoom views); defaults in `[display]`. Calibration on/off switch
  `use_calibration`; relative config paths resolve against the config file's folder.
- ADV reader works with numpy 2 (Adv2 1.2.0 patched at run time); frames without a GPS
  timestamp no longer get Adv2's fake 2010-01-01 date.
- Reference-star normalisation, relative flux, delta magnitude, SNR.
- Parity tests against PyMovie 4.2.8, including a real ASTRID RAVF recording
  (all 36,096 CSV values and all timestamps identical).
