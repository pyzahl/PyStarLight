# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
