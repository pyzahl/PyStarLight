# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
  (scale, north angle, mirrored). Sites compatible with PyOccult's `sites.py`.
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
- Reference-star normalisation, relative flux, delta magnitude, SNR.
- Parity tests against PyMovie 4.2.8, including a real ASTRID RAVF recording
  (all 36,096 CSV values and all timestamps identical).
