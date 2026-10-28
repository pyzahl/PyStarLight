# PyStarLight

Light curves from occultation videos, as a Python library: movie readers for every common
format, frame timestamps (embedded or read from a VTI overlay), star tracking through wind-shake
and field rotation, aperture photometry, and reference-star normalisation. A minimal matplotlib
viewer (`pystarlight-view`) for marking stars; results open in PyOTE.

Most of the processing is derived from [PyMovie](https://github.com/bob-anderson-ok/pymovie) 4.2.8
by Bob Anderson, and tests check that it gives the same numbers.

## Install

```bash
pip install starlight-photometry            # import name: pystarlight
pip install "starlight-photometry[all]"     # + ADV/AAV, RAVF readers, UCAC4 lookup
```

From a clone, for development:

```bash
uv sync --group dev
uv run pytest
```

## The tools

Three programs, each started with `uv run` from the clone (after `uv sync` with the extras
shown):

| program | what | start |
|---|---|---|
| `pystarlight-view` | matplotlib viewer: mark target / reference stars, plate-solve, run the photometry, export for PyOTE (see [Getting started](#getting-started)) | `uv sync --extra all`<br>`uv run pystarlight-view FILE` |
| `pystarlight-camera` | capture on the Raspberry Pi 5: live view, GPS-timed RAVF / SER recording, live stack + plate solve | `uv sync --extra all --extra camera --extra aravis`<br>`uv run pystarlight-camera` |
| `pystarlight-review` | Qt player for a recording: frames, time stamp check (dropped / irregular frames), stack & plate-solve a frame range | `uv sync --extra all --extra camera`<br>`uv run pystarlight-review FILE` |

`FILE` is any supported recording (`.ravf .ser .adv .aav .avi .mov` or a FITS folder).
The camera and the [review player](CAPTURE.md#review-player) are described in
[CAPTURE.md](CAPTURE.md), including
[offline plate solving](CAPTURE.md#offline-plate-solving-local-astrometrynet) with a local
astrometry.net. `uv run pystarlight-config` manages the private config (next section).

## Getting started

### 1. Install and configure (once)

```bash
cd PyStarLight
uv sync --extra all                      # or: pip install -e ".[all]"  (plot, RAVF, ADV, SIMBAD)
uv run pystarlight-config init --here    # private config: ./pystarlight.toml (git-ignored)
```

Edit the config: your name, email, your nova.astrometry.net API key, and your observing
site(s). Sites use the same keys as PySyzygos (formerly PyOccult): point `pysyzygos_sites` at your PySyzygos
`sites.py` (read as data, never executed) and/or add `[sites.<id>]` tables with `focal_mm`
and `sensor_mm`, from which the expected plate scale is computed. The file is only readable
by you and is never part of the repository. `pystarlight-config show` prints it with secrets
masked; `pystarlight-config sites` lists the sites; `pystarlight-config path` shows which file
is used (lookup: `$PYSTARLIGHT_CONFIG`, `./pystarlight.toml`, the project root's
`pystarlight.toml`, `~/.config/pystarlight/config.toml`). All analysis settings and the aperture
types (names, colours, keys) live there too.

**Dark / flat ("bright") clips**: set `[calibration] dark` and `flat` (and `flat_dark` if the
flat's exposure differs) to clip files. Each clip is averaged into a master frame (mean, or
median), cached in `~/.cache/pystarlight` and re-stacked only when the clip changes, then applied
to every aperture box. Record them with the same gain and exposure as the observation.

### 2. Mark the stars

```bash
uv run pystarlight-view path/to/recording.ravf
```

Press `p` to **plate-solve**: a median stack of the first frames goes to nova.astrometry.net
in the background (private upload; result cached as `<movie>.wcs.fit`, loaded automatically
when the movie is opened again; nothing is uploaded without `p`). Burnt-in timestamp rows are
blanked first; an ADV file's own RA/Dec is used as the field-centre hint, and the site's
plate-scale hint only for recordings made at the site.
With a local astrometry.net (`sudo apt-get install astrometry.net` plus the
`astrometry-data-2mass-*` index files matching your field, see
[Offline plate solving](CAPTURE.md#offline-plate-solving-local-astrometrynet)) the solve runs
offline instead.
The cursor readout then shows RA/Dec, an N/E compass is drawn, and the line below the status
gives the plate scale, the direction of north and whether the image is mirrored.

The **three brightest stars** are then found in the stack and named: astrometry.net's name
where it has one (it only knows bright-star and HD names), otherwise the nearest SIMBAD object,
with its HD/HIP/TYC/Gaia DR3 IDs and V magnitude (needs the `catalog` extra and network). They
are labelled on the image and cached as `<movie>.stars.json`. The stars in your **target,
reference and check apertures** are identified the same way (automatically after the solve and
before each run, `i` to repeat): position refined by a peak fit on the solved frames (the field
drift since then is measured and removed), then name, IDs and V. The results are saved in
`apertures.json`, shown on the aperture labels and zoom views, and written to both CSVs. The zoom views show RA/Dec and,
for those stars, the name. Both CSVs carry the pixel → RA/Dec mapping (scale, north angle,
mirroring, CRVAL/CRPIX/CD), the named stars, and every aperture's RA/Dec; `apertures.json` keeps
RA/Dec too.

Each frame's UTC timestamp is shown on top; a short key list is on the right. Left-click a star
to add an aperture (it snaps onto the star), right-click removes one. With the mouse over an
aperture, press a key to give it a role:

| key | role | what it is for |
|---|---|---|
| `t` | **target** | the star being occulted (exactly one; pressing `t` elsewhere moves it) |
| `r` | **reference** | comparison stars; the target is divided by their (smoothed) sum. Use several, not saturated, similar brightness or brighter |
| `c` | **check** | measured and tracked like a reference but not used for normalisation, to verify the references |
| `k` | **sky** | a star-free patch; the global background when `background = "sky"` |

All keys:

| key / control | action |
|---|---|
| click / right-click | add (snaps onto the star) / remove aperture |
| `t` `r` `c` `k` | role of the aperture under the mouse (see above; changeable in the config) |
| `1` / `2` | show the aperture under the mouse in zoom view 1 / 2 (right side, ~4-5×, own contrast). While analysing they update live (~2×/s) with the frame being processed; afterwards they follow the tracked position. Each shows the signal and, in global mode, the peak fit (FWHM, SNR, `+` = fitted centre, circle = photometry aperture) |
| `n` | rename the aperture under the mouse (typed in the terminal) |
| ← / → | previous / next frame |
| PgUp / PgDn | −/+ 25 frames |
| Home / End, buttons `\|<` `>\|` | first / last frame |
| buttons `-10 s` / `+10 s` | jump 10 s (frame count from the timestamps) |
| `a`, button **Run** | analyse from the current frame to the end |
| `s` / `l` | save / load apertures (`<movie>.apertures.json`) |
| `p` | plate-solve (uploads to astrometry.net) |
| `i` | identify the stars in the apertures (astrometry.net names, SIMBAD) |
| `f` | full screen (matplotlib) |
| `o` | zoom rectangle (matplotlib toolbar); click the house icon to reset |
| `Y` `R` `G` `W` (shift) | PyMovie colours yellow / red / green / white, for `mode = "pymovie"` |
| sliders **levels** / **gamma**, **Auto**, **per frame** | display only: drag the levels for manual black/white points (kept across frames); **Auto** returns to the automatic (zscale) stretch; **per frame** re-stretches every frame; gamma < 1 brightens faint stars. Defaults in `[display]` of the config |

### 3. Run

Press `a` (or **Run**) on the frame to start from. All star apertures (target, references,
checks) track together:

- each star is located by a 2-D Gaussian **peak fit**;
- one **global shift + rotation** is fitted to all of them, weighted by how well each is measured,
  with outliers rejected, so wind-shake, drift and field rotation are followed by every aperture;
- when the **target fades** during the occultation (its fit fails, or it dims relative to the
  other stars; clouds dim all stars and do not count), it is left out of the tracking. Its
  aperture keeps following the field, plus the target's own slow offset, **interpolated between
  the start and end of the event**. A known event can also be given: `event=(first, last)`;
- photometry uses a circular aperture with exact sub-pixel weights (radius 1.5 × FWHM unless set).

Written next to the movie:

| file | contents |
|---|---|
| `<movie>.csv` | light curves in **PyMovie's CSV layout: open it directly in PyOTE** |
| `<movie>.pystarlight.csv` | every column (fit positions, FWHM, peak SNR, tracking shift/rotation, ...) |
| `<movie>-summary.pdf/.png` | field with apertures, raw signals, normalised light curve vs UTC, drift |
| `<movie>.apertures.json` | the apertures, reloaded next time |
| `<movie>.wcs.fit` | the plate solution |
| `<movie>-stack.fits/.svg/.png/.pdf` | **stacked field**: all analysed frames registered with the tracked shift + rotation and averaged (`[output] stack_image`); FITS with the plate solution, figure with apertures, names, catalogue IDs, compass and 1′ scale bar. In the SVG the image and the annotation layers (`apertures`, `aperture-labels`, `stars`, `compass`) are separate groups |

### 4. Open in PyOTE

Load `<movie>.csv` in PyOTE like any PyMovie file. The target is the first light curve (PyOTE's
default); references and checks follow. The header carries the source path, the recording date
and, for RAVF/ADV, the camera metadata that PyOTE uses to fill its report form. In PyMovie mode
the file matches what PyMovie itself writes (checked line by line on a real recording); in
global mode `nmaskpx` is the effective, fractional pixel count of the circular aperture.

On a 94 s ASTRID test recording this mode lowered the normalised scatter of a faint star from
23.5 % (PyMovie-style apertures) to 6.4 %.

### Same thing from Python

```python
from pystarlight import Aperture, open_movie
from pystarlight.globaltrack import analyze_global

aps = [Aperture.centered_at('target', 946, 701, role='target'),
       Aperture.centered_at('ref1', 145, 907, role='reference'),
       Aperture.centered_at('ref2', 749, 598, role='reference'),
       Aperture.centered_at('sky', 600, 850, role='sky')]
with open_movie('recording.ravf') as mv:
    lc = analyze_global(mv, aps)
flux = lc.normalized()                   # target / smoothed sum of references, median 1
lc.to_pymovie_csv('recording.csv')       # for PyOTE
lc.to_csv('recording.pystarlight.csv')   # all columns
```

`analyze()` (no roles, PyMovie colours) reproduces PyMovie 4.2.8 exactly; use it to compare.

## Example

```python
from pystarlight import open_movie, analyze, Aperture
from pystarlight import normalize as nz
from pystarlight.timestamps import check_timing

aps = [
    Aperture.centered_at('target', 160, 120),                 # red: measured, follows yellow
    Aperture.centered_at('ref1',    80,  60, color='yellow'), # tracking star
    Aperture.centered_at('ref2',   250, 180),
]
with open_movie('Pelican_Nebula.ravf') as mv:   # .avi .mov .ser .adv .aav .ravf or a FITS folder
    lc = analyze(mv, aps)

print(check_timing(lc.time).summary())
flux = nz.normalize(lc['target']['signal'], lc['ref1']['signal'] + lc['ref2']['signal'], window=101)
lc.to_csv('out.csv')
```

### Analog video with a VTI

```python
from pystarlight.timestamps import VtiReader

vti = VtiReader('iota3-full', width=720)        # profiles: pystarlight.timestamps.PROFILES
with open_movie('occ.avi') as mv:
    img = mv.read(1).image                      # frame 0 is often damaged
    for box, digit in [(0, 0), (1, 5)]:         # ... until vti.missing_digits() == []
        vti.train(img, 'upper', box, digit)
    lc = analyze(mv, aps, vti=vti, fields=True) # one row per field, each with its own time
vti.save('occ-vti')                             # PyMovie-compatible profile files
```

`VtiReader.from_pymovie_folder(folder)` loads a profile already trained in PyMovie.

### Dark / flat calibration

```python
from pystarlight import Calibration

cal = Calibration.from_videos(dark='dark.ravf', flat='flat.ravf')   # either may be omitted
cal.save('cal.npz')                                     # pystarlight-view --calibration cal.npz
lc = analyze(mv, aps, calibration=cal)                  # applied to the aperture boxes only
```

### Figures

```python
from pystarlight import plot
with plot.style():                                      # journal-style rcParams, 300 dpi
    fig = plot.summary(lc, image, aps, target='target', references=['ref1', 'ref2'])
    plot.save(fig, 'event')                             # event.pdf, event.png
```
`plot.frame`, `plot.lightcurve`, `plot.tracking`, `plot.timing` draw single panels on your own axes.

### Locate the target

```python
from pystarlight import wcs

# from two reference stars with known positions
sol = wcs.solve_two_stars(('18h12m10s +22d10m03s', 120, 80),
                          ('18h11m52s +22d02m41s', 600, 400),
                          '18h12m01s +22d07m00s', mirrored=False)

# or plate-solve with nova.astrometry.net (needs an API key)
img = wcs.prepare_for_astrometry(frame.image, redact_bottom=20)
fits_path = wcs.solve_astrometry_net(img, api_key, workdir='wcs')
x, y = wcs.target_pixel(wcs.load_wcs(fits_path), '18h12m01s +22d07m00s')
```

## Package layout

| module | contents |
|---|---|
| `movie` | `open_movie()`: AVI/MOV, SER, ADV/AAV, RAVF, FITS folder → `Frame(index, image, date, time, status)` |
| `preprocess` | gamma, CMOS defect pixels, hot pixels, line-noise filter, dark/flat |
| `photometry` | robust background, star mask, brightest-pixel centroid, default mask, TME, `measure_aperture()` |
| `tracking` | `Aperture`, `Tracker` (0/1/2 yellow apertures, field rotation), `TrackPath` |
| `pipeline` | `analyze()` (PyMovie-exact) → `LightCurves`, with progress/cancel hooks for a worker thread |
| `globaltrack` | `analyze_global()`: roles, peak fits, global shift + rotation, sub-pixel apertures, occultation-aware target tracking |
| `export` | PyMovie-layout CSV for PyOTE |
| `config` | private config from the template, PySyzygos-compatible sites (`pystarlight-config`) |
| `timestamps` | `VtiReader` (IOTA 2/3, BoxSprite, Kiwi NTSC/PAL, GHS), `check_timing()`, midnight rollover |
| `wcs` | astrometry.net client, two-star solution, WCS → pixel, UCAC4 lookup |
| `calibrate` | master dark/flat, gain and defect maps, per-aperture correction |
| `normalize` | reference-star normalisation, relative flux, Δmag, SNR |
| `plot` | publication figures (matplotlib) |
| `viewer` | `pystarlight-view`: step frames, timestamps, place apertures, run |

## Things to know

- **Same numbers as PyMovie.** `tests/test_parity.py` and the parity test in
  `tests/test_timestamps.py` run the original PyMovie code on the same data and compare.
  They run when `pymovie` can be imported (PyMovie's own environment) and are skipped otherwise.
- **PyMovie quirks are kept** so results match; each is marked `PyMovie quirk` in the code.
  The largest: `bkstd` comes out too high when a star is inside the aperture.
- **Mirrored images.** PyMovie's two-star solution assumes a non-mirrored image. For an image
  seen through an odd number of mirrors (e.g. a star diagonal) pass `mirrored=True`, or the
  target lands at its mirror position, typically tens of pixels away. Check with a third star.
- **New code** (not in PyMovie): `normalize`, `timestamps.timing`, `plot`, `viewer`, the `mirrored`
  option, and per-aperture calibration.
- **Not included:** NRE/Naylor extraction, GCE, finder/stacking, `.pymovie` record files.
- `ocr.py`, `wcs/manual.py` and `wcs/astrometry_client.py` are kept as close to PyMovie's
  files as possible (and excluded from linting) so upstream fixes are easy to carry over.
- The RAVF reader is tested on real ASTRID recordings, the ADV reader on a real DVTI+CAM
  recording. ADV needs the `adv` extra; `Adv2` 1.2.0 is patched at run time for numpy 2.

## Licence

MIT (see `LICENSE`): free to use, modify and redistribute, also in commercial or closed
software, as long as the copyright notices are kept. Most of the processing is derived from
PyMovie by Bob Anderson, also MIT; its notice is in `LICENSES/MIT-PyMovie.txt` and must be kept
too. `wcs/astrometry_client.py` follows the client published by astrometry.net (BSD licence).
