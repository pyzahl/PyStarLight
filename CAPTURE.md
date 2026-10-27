# Capture on the Raspberry Pi 5 with GPS-timed frames

`src/pystarlight/camera.py` starts a live viewer and recorder (RAVF or SER) for the Raspberry Pi 5. It drives a
VEYE MV-MIPI camera on a CSI port, or any Aravis (GenICam USB3/GigE) camera. A u-blox GPS can
trigger the camera, so that each frame belongs to a known UTC instant. These notes describe the
hardware, how to run the program, how it works, and the timing measurements behind its timestamps.

The code is the `src/pystarlight/capture/` package (`uv run pystarlight-camera`);
`src/pystarlight/camera.py` launches it with the system Python.

| file (`capture/`) | what |
|---|---|
| `app.py` | command line, wiring of camera, threads, GPS and GUI, shutdown |
| `gui.py` | PyQt5 GUI: image view, control panel, status |
| `cameras.py` | camera backends behind one interface (`AravisCamera`, VEYE) and `CameraSession`: pixel format, region, trigger, stream, reconfigure, exposure-start time stamps |
| `veye_mvcam.py` | VEYE MV-MIPI camera through the `veye_mvcam` kernel driver: I2C registers, media pipeline, V4L2 buffers; the same interface as an Aravis camera |
| `decode.py` | pixel formats, unpacking of packed 10/12-bit buffers |
| `timing.py` | GPS pulse stamping (`PulseStamper`), VEYE timing constants, exposure-start formula |
| `acquisition.py` | Grabber and Controller threads |
| `recorder.py` | Recorder thread; `repair` of interrupted recordings |
| `stacker.py` | live stacking aligned on the stars, FITS writer |
| `qt.py` | Qt binding: PySide6 (venv) or PyQt5 (system Python) |
| `review.py` | review player `pystarlight-review` (needs the full package) |
| `ravf_writer.py` | RAVF writer (as ASTRID writes), frame scan, repair |
| `ser_writer.py` | SER v3 writer, one UTC timestamp per frame, `.times` sidecar, repair |
| `gps_ublox.py` | u-blox GPS: NMEA status, timepulse rate (UBX-CFG-TP5), optional QMC5883L/HMC5883L compass |

## Hardware (the owner's rig)

- Raspberry Pi 5, Raspberry Pi OS (Debian 12), kernel 6.12, `dtoverlay=veye_mvcam` in
  `/boot/firmware/config.txt`.
- VEYE **MV-MIPI-IMX178M** on cam0: 3088 × 2064, 12-bit mono, rolling shutter.
- u-blox **NEO-M8N** GPS module (with compass), NMEA on the GPIO UART **/dev/ttyAMA0, 9600 baud**
  (`dtparam=uart0=on`).
- The GPS **timepulse (PPS) is wired to the camera's trigger input**. It is not wired to a Pi GPIO,
  so the Pi's own clock is plain NTP (systemd-timesyncd, about ±1 ms). The timestamps in GPS
  mode don't depend on it (see below).
- The module's compass is not connected to the Pi: nothing answers on I2C buses 0 or 1. The code
  shows a magnetic heading if a QMC5883L (0x0d) or HMC5883L (0x1e) appears on bus 1 (GPIO2/3).
  The heading is uncalibrated: no hard-iron or soft-iron correction, no tilt compensation.

## Running it

The app runs in the repo's uv venv, like the rest of PyStarLight:

```bash
sudo apt install libgirepository1.0-dev libcairo2-dev gir1.2-aravis-0.8 v4l-utils   # once
uv sync --extra all --extra camera --extra aravis                                    # once
uv run pystarlight-camera                                          # free run, full frame
uv run pystarlight-camera --trigger gps --gps-rate 10 --exposure 20
uv run pystarlight-camera --trigger gps --gps-rate 30 --roi 904,672,1280,720 --exposure 5
uv run pystarlight-camera --trigger gps --record-start 23:40:00 --record-duration 60
uv run pystarlight-camera --exposure 800 --gain 24 --focal-mm 24       # sky: stack + plate solve (s)
uv run pystarlight-camera --list                                   # cameras found
uv run pystarlight-camera --record-format ser                      # SER instead of RAVF
uv run pystarlight-camera --repair ~/data/*.ravf                   # finish crashed recordings
```

- Extras: `camera` = PySide6 (Qt GUI) + pyserial (GPS) + astroquery (star names); `aravis` =
  PyGObject for Aravis (GenICam USB3/GigE) cameras, built against the system's GObject
  introspection (hence the apt dev packages; Aravis itself is the system library). VEYE cameras
  need only `camera`.
- Version pins, both because Raspberry Pi OS bookworm is older than the newest wheels:
  - PySide6 < 6.8.1 on aarch64: newer aarch64 wheels need glibc 2.39, bookworm has 2.36.
    PyQt5 has no aarch64 wheels at all.
  - PyGObject < 3.51: newer needs girepository-2.0 (GLib 2.80), bookworm has 2.74.
- The GUI works with PySide6 or PyQt5 (`capture/qt.py`; `PYSTARLIGHT_QT=pyqt5` forces PyQt5).
  The venv's opencv-python (pulled in by `ravf`) points Qt at its own bundled Qt5 plugins, which
  breaks PySide6; `fix_cv2_qt_env()` undoes that before the QApplication is made.
- Without uv, the Pi's system Python still works (no plate solve: that needs the full package):
  `sudo apt install python3-gi gir1.2-aravis-0.8 python3-pyqt5 python3-opencv python3-serial
  python3-numpy`, then `python3 src/pystarlight/camera.py ...` (same options). Run as a file,
  the launcher imports `capture` as a top-level package from its own folder, skipping the rest of
  PyStarLight (scikit-image etc.).

- `--exposure` is in **milliseconds**.
- `--pps [RATE]` is an old alias for `--trigger gps --gps-rate RATE`.
- `--gps-port none` runs without a GPS. The trigger pulses must then arrive at the selected rate on
  their own.
- See `--help` for the rest: gain, gamma, ROI, pixel format, trigger edge and delay, observer and
  telescope for the file header.

## The GUI

| panel | contents |
|---|---|
| image | fit to the window; **mouse wheel** zooms around the cursor (up to 64×, pixels as blocks past 1:1), **left drag** pans, **double-click** fits again |
| **Camera** | exposure (log slider + ms box), gain, display gamma, resolution, peak value; measured frame number, rate, interval (mean ± jitter, min..max over ~200 frames), exposure read back from the camera and, in free run, its frame period; **Depth** 12 / 10 / 8 bit |
| **Sensor region (ROI)** | x / y / w / h boxes, **Select** (then drag a rectangle on the image; shown in yellow until applied), **Apply**, **Full sensor**; current region |
| **Trigger** | **GPS trigger** checkbox (off = free run, or the `--trigger hw/sw` mode), **GPS frame rate** 30 / 20 / 15 / 10 / 8 / 6 / 5 / 4 / 3 / 2 / 1 / 0.5 fps, **Trigger delay** (ms), **y-trigger row** (VEYE, GPS mode; see below), warnings (exposure longer than the pulse period, rate too high for the camera, pulses skipped because of the delay, no fix), missed pulses |
| **GPS** | UTC, fix, satellites (used / tracked / in view), DOP, position, altitude, timepulse rate, exposure start of the newest frame, its trigger pulse, pulse → frame delay, rolling-shutter line time, heading if a compass is found |
| **Recording** | folder, format (RAVF / SER), observer, telescope, start **Now** or **At UTC** (with "next min"), stop after N s and/or N frames; Record button (orange = armed / Cancel, red = recording / Stop), frame count, elapsed time, countdown, last file saved |
| status bar | exposure, gain, camera and display fps, lost frames, bad buffers, restarts, frame ID, trigger count / lost, sensor position and raw value under the mouse |

Keys: `e/E` exposure, `g/G` gain, `d/D` gamma, `r` record / stop, `t` software trigger (in `sw`
mode), `s` stack, `c` or `Esc` continue the live view after a stack, `q` quit.

- The panel sits beside the image with a draggable divider. The **GPS** section folds away (click
  its title), leaving one line: fix, satellites, UTC. The divider position and the fold are
  remembered in `~/.config/PyStarLight/camera.conf`.
- The trigger, ROI and depth controls are locked while a recording is armed or running.
- Applying a ROI or a new depth stops the camera, rebuilds the stream (media pipeline, buffers)
  and restarts it, in about a second. If the camera rejects the change, the previous settings
  are restored.
- Regions are rounded to the VEYE steps (x, w: 8; y, h: 4). A selection smaller than the minimum
  grows around its centre.
- Switching between GPS and free run restarts the camera's acquisition, and so does changing the
  trigger delay: the camera ignores the delay register while it streams.
- **y-trigger** (or `--y-trigger ROW`): the app sets the trigger delay so that the timepulse, the
  moment the GPS LED switches on, falls on that image row, marked with a dashed orange line. It
  keeps the delay there while exposure, region, depth or rate change (not while recording). Use
  it to check the timing: put the LED on the line and the line should cut the LED in half.
  - The edge is exposure / line time rows wide (2 ms: about 140 rows), so use a short exposure for
    a sharp line.
  - The delay is nearly a full pulse period (about 970 ms at 1 Hz, full frame). The camera is
    still busy at the next pulse, so it takes every 2nd pulse and runs at half the rate. The
    trigger box says so, and those skipped pulses don't count as missed.
  - Measured (2 ms, LED at rows 151–190): target 140 → edge at row 142.8, 170 → 176.7, i.e.
    within 0.1 ms.
- On exit, the GPS goes back to its default 1 Hz PPS.

## How it works

**Threads.**
- **Grabber** pops frames, stamps them, and passes every wanted frame to the recorder. It keeps
  only the newest frame for display, undecoded.
- **Controller** applies exposure, gain, trigger and trigger-delay changes off the GUI thread. Register writes and
  stream restarts are slow.
- **Recorder** decodes frames and writes the RAVF or SER file.
- **Gps** reads the serial port.
- The Qt main thread renders: debayer, then downscale to the view size, then the gamma lookup table.

**GPS timepulse.**
- `Gps.set_timepulse(rate)` sends UBX-CFG-TP5. The pulse is set to come **only while the GPS has a
  fix** (unlocked pulse length 0), aligned to the UTC second, rising edge first.
- Whole-Hz rates are set as a frequency, so 30 Hz means pulses at exactly k/30 s. Other rates are
  set as a period in µs: 0.5 Hz means even seconds.
- Pulse length is a quarter of the period, at most 100 ms.
- The setting is not saved in the GPS: a power cycle brings back 1 Hz.
- Two serial-port quirks:
  - The **first UBX message after opening the port goes unanswered**.
  - Replies queue behind the NMEA output, arriving about 0.7 s later at 9600 baud.

  So `_send_wait` resends up to 3 times, with a 2 s timeout each.

**Which pulse triggered a frame** (`PulseStamper`).
- A frame's CSI-2 frame-start time comes a steady delay after its pulse. On the VEYE camera that
  delay is about the camera's frame period + 3 ms, plus any trigger delay.
- Frame time minus that delay, rounded to the pulse grid (k/rate seconds from the Unix epoch, which
  matches the UTC grid), gives the pulse. This only needs the Pi clock to be within about half a
  pulse period: ±16 ms even at 30 fps, where NTP is about 1 ms.
- The predicted delay is refreshed whenever the exposure changes, then follows the measured delay.
- The stamper uses the rate the **GPS confirmed** (ACK), not the one selected in the menu, so a
  rejected or still-pending change can't mis-stamp frames.
- Missed pulses (gaps in the pulse sequence) are counted, except for the pulses the camera skips on
  purpose: a trigger that comes while the camera is still busy (its delay + about two frame
  periods + 3 ms after the last trigger) is lost, so with a long delay or a high rate it takes
  every n-th pulse (`pulses_per_frame`).

**Recording start and stop.**
- In GPS mode, start and stop times refer to the **trigger pulse**. A recording armed for 23:40:00
  begins with the frame of exactly that pulse. "Stop after 10 s" at 2 fps gives exactly 20 frames.
- In free run they refer to the exposure start.

## Stack & plate solve

For finding out where the camera points: the **Stack & plate solve** panel (key `s`) averages a
number of frames, aligned on the stars, shows the stack instead of the live frames, saves it as
FITS and plate-solves it on nova.astrometry.net with PyStarLight's solver
(`pystarlight.wcs.solve_stack`, which uses `solve_astrometry_net`, `orientation` and `identify`).

- Result in the panel (and `<stack>.solve.json`, `<stack>.wcs.fit` next to the FITS): RA/Dec of
  the field centre, field size, plate scale, north direction, mirroring, altitude/azimuth of the
  centre (needs a GPS fix or a default site in the config), and the brightest stars with RA/Dec
  and names (astrometry.net annotations, then SIMBAD), circled on the stack.
- **Focal length** (`--focal-mm`) gives the plate-scale hint with the sensor's pixel size (IMX178:
  2.4 µm, `--pixel-um` otherwise): 24 mm -> 20.6"/px, a 17.6° x 11.8° field. 0 = blind solve.
- **Solver**: a local astrometry.net when installed (offline, see "Offline plate solving"), else
  nova.astrometry.net with `[astrometry] api_key` from `pystarlight.toml`
  (`[astrometry] solver = "auto" | "local" | "nova"`).
- **Alignment** (`capture/stacker.py`): without tracking the sky drifts ~15"/s x cos(dec), i.e.
  ~0.5 px/s at 24 mm (measured: 6 px in a 12.8 s stack). Each frame is aligned to the first by
  phase correlation of "star maps" (2x downsampled, median-filtered against hot pixels, minus a
  blurred background against sky glow and tree silhouettes, clipped above the noise, Hann
  window). Tested to ~0.3 px on synthetic fields with a static dark "tree" and hot pixels.
  Frames whose correlation is too weak keep the last shift (counted as "not aligned").
- The stacker takes the next frame whenever it is free, so on a fast stream it stacks every k-th
  frame. The stack FITS header has DATE-OBS (first frame), EXPTIME (sum), NCOMBINE, DRIFTX/Y,
  XPIXSZ, FOCALLEN, GAINDB, ROI offset and, with a GPS fix, SITELAT/SITELONG/SITEELEV.
- Standalone: `uv run python -m pystarlight.wcs.solve_stack STACK.fits [--stars 5] [--scale S]`.
- ROI and depth are locked while a stack runs.
- While the stack is shown, a banner at the bottom of the image says so ("STACK 12/16",
  "... - solved"). **Continue live view** (button, key `c` or `Esc`) goes back to the live frames;
  a running stack goes on in the background, and **Show stack** brings the stack and its marked
  stars back. The stack is auto-stretched (background black, brightest 0.05 % white); live
  frames are shown as they are, with the display gamma only.
- First sky test (2026-10-27, 24 mm lens, trees in the lower half): 4 x 0.8 s at 24 dB, full
  frame, solved offline in 7.8 s: centre RA 22h34m27s Dec +46°12' (Lacerta), 17.34° x 11.64°,
  20.37"/px (nominal 20.6"), north 102° from up; the five brightest stars named by SIMBAD
  (α Lac, 1 Lac, 5 Lac, HD 211073, ο And; V 3.6-4.5).

## Review player

`uv run pystarlight-review FILE` (`capture/review.py`), or **Review last clip** in the camera
app's Recording box (starts it as its own process, on the file just recorded; with the system
Python it uses the repo's `.venv`). Reads RAVF, SER, ADV/AAV, AVI and FITS folders
(`open_movie`, also unfinished recordings).

- **Frame**: number, UTC, step to the previous frame against the typical step, the problem at
  this frame, and for RAVF the frame header: sequence number, exposure, GPS satellites, fix,
  leap-second status. Other formats show their per-frame status (ADV) if any.
- **File**: size, depth, frames, duration, typical step / fps, problem count, the whole header.
- **Step plot** below the image: the step of every frame over the file (min/max per pixel
  column), red ticks at problems, yellow line at the current frame; click or drag to jump.
- **Problems**: `check_timing` on the time stamps (dropped, duplicated, backwards, irregular,
  missing) plus RAVF sequence gaps (dropped by the camera link or the recorder queue).
  `n` / `b` jump to the next / previous one.
- **Stack & plate solve** of a frame range (`[` / `]` set from / to at the current frame, `s`
  starts): `LiveStacker` (aligned on the stars) -> `<movie>_f<a>-<b>_..._stack<n>.fits` next to
  the movie -> `solve_fits` (local solver or nova) -> centre, field, alt/az, named stars circled on
  the stack. Focal length and pixel size come from `--focal-mm` / `--pixel-um`, the camera model
  in the file, or the config's default site. **Save as the movie's plate solution** writes
  `<movie>.wcs.fit`, `.wcs.json` (stack_start = the range start) and `.stars.json` as
  pystarlight-view caches them, so the viewer uses that solution.
- Keys: Space play/pause, Left/Right, PgUp/PgDn (10), Home/End, `n`/`b`, `[`/`]`, `s`, `c` or
  `Esc` back to the frames, `q`.
- Times are kept as float64 Unix seconds (0.24 µs resolution), enough for review; the files keep
  their ns stamps.
- Tested 2026-10-27 on a 20-frame RAVF clip made from the 24 mm sky stack with one dropped and
  one late frame: both found (frame 8: time gap + sequence +2; frame 14: +200 ms), frames 0-15
  stacked (drift -7.0, +2.3 px; built in -6.75, +2.25) and solved offline as before; the saved
  solution loads through `solve_movie` / `field_stars`.

## Offline plate solving (local astrometry.net)

Installed on the Pi (2026-10-27): `astrometry.net` 0.93 (`solve-field`) and the 2MASS index
files 4202-4219 (`sudo apt install astrometry-data-2mass-02 ... -07 astrometry-data-2mass-08-19`,
9.7 GB in `/usr/share/astrometry`, downloaded at install time). The index files must cover quads
of about 10-100 % of the field width; 4202-4219 = quads 4'-2000', i.e. fields from ~12' to many
degrees. For fields under ~10' add `astrometry-data-2mass-01` (9 GB) and `-00` (14 GB).
Tycho-2 (`astrometry-data-tycho2-*`) starts at 22' quads and is too sparse for <= 1° fields.

`solve_astrometry_net` (viewer key `p`, `solve_movie`, the stack solve) uses it automatically
(`[astrometry] solver = "auto"`): `solve_local` runs `solve-field` with the same hints (scale
+-`scale_err_pct`, centre, downsampling) and gives the same WCS file. There is no job id, so no
astrometry.net annotations: star names come from SIMBAD (network) only. The SIMBAD search radius
is 10", or 2 px on coarse scales (`identify.simbad_radius`).

Synthetic tests (Gaia DR3 G < 15 near Deneb, the owner's plate scales, 2026-10-27):

| setup | field | with scale hint | blind | centre error |
|---|---|---|---|---|
| IMX178 @ 343 mm, 1.44"/px, 3088 x 2064 | 74' x 50' | 6.3 s | 59 s | 0.12" |
| ASTRID IMX296 @ 343 mm, 2.07"/px | 50' x 38' | 6.2 s | 120 s | 0.25" |
| ROI 1280 x 720 @ 1.44"/px | 31' x 17' | 8.0 s | 32 s | 0.04" |

So give the focal length (scale hint): 5-20x faster.

## Recording formats

**RAVF** (default, `--record-format ravf`) is the Raw Astro Video Format that ASTRID writes, read
by PyMovie and PyStarLight. `ravf_writer.py` writes the same bytes as the `ravf` package (which
isn't needed on the Pi).
- Every frame has its own 101-byte header: a magic number, its exposure start and duration (ns
  since 2010-01-01 UTC), GPS satellites and fix status, and a sequence number counted from the
  camera's frame ID (gaps = dropped frames).
- Pixels are 16-bit with their raw values (12 bit stays 0..4095); the user entry
  `IMAGE-BIT-DEPTH` gives the depth. 8-bit data is stored as 16-bit too, because the `ravf`
  reader rejects its own 8-bit format (and debayers its "unpacked 12-bit" one unconditionally).
- Header metadata: camera model, vendor and serial, Pi model, gain, exposure, GPS position,
  observer, telescope, `FRAME-TIMING-ACCURACY` (0.1 ms: GPS on the calibrated IMX178 at 12 bit,
  5 ms: GPS otherwise, 2 ms: Pi clock), and user entries `TIMESTAMP-SOURCE`, `ROW-TIME-NS`
  (rolling-shutter line time), `ROI-X`/`ROI-Y`, `TRIGGER`, `TRIGGER-DELAY-NS`.
- GPS fields per frame: only set for GPS-stamped frames (fix status 3, almanac 0 while the GPS
  has a fix); free-run frames carry fix 0, because their time comes from the Pi clock.

**SER** (`--record-format ser`) keeps its timestamps in a trailer after the last frame.

**Crash safety.** Both writers update the header's frame count and `fsync` to disk every
second.
- An interrupted RAVF file has no index (`OFFSET-INDEX` 0). Every frame carries its own magic and
  time stamp, so the index can be rebuilt by walking the frames.
- An interrupted SER file would lose all its timestamps, so while recording they also go to a
  sidecar `<file>.ser.times` (the same int64 ticks), deleted when the file is closed.
- PyStarLight's reader opens both kinds of interrupted file as they are, read-only: RAVF by
  scanning the frames, SER with the sidecar's timestamps. Both warn.
- `--repair FILE ...` (or `python3 -m pystarlight.capture.recorder FILE ...`) finishes them in
  place: it drops a partial last frame, appends the RAVF index or SER trailer, fixes the header
  and deletes the sidecar. PyMovie and other readers can then open them. Complete files are left
  alone.
- Tested 2026-10-26 by `kill -9` during 10 fps recordings: 80 RAVF and 77 SER frames, all
  readable before and after repair, with timestamps.

## Timestamps: what the recordings hold

Each frame's timestamp is the **exposure start of row 0**. The IMX178 has a rolling shutter, so **row y
starts y × 14.33 µs later**: 29.6 ms down to the last of 2064 rows. For an object at row y, add
that offset.

| mode | row-0 exposure start | clock |
|---|---|---|
| GPS trigger | pulse + 0.9967 × trigger delay + (frame period − exposure) + 0.33 ms | GPS only |
| free run | frame_ns − exposure − 3.33 ms | Pi (NTP) |

- `frame_ns` is the CSI-2 frame-start time that the rp1-cfe driver records in CLOCK_MONOTONIC,
  converted to CLOCK_REALTIME.
- "Frame period" is the camera's current frame period (`veye_mvcam.Camera.frame_period_ns()`). It
  follows the exposure, with a minimum of 30.12 ms at full frame and about 10.85 ms at 1280 × 720.
- In trigger mode, a VEYE camera holds off the exposure so that it **ends** about one frame period
  after the trigger. Short exposures therefore start well after the pulse: about 22.5 ms after it
  at 8 ms exposure, full frame. Stamping a GPS frame at the pulse itself would be up to about 30 ms
  early.
- The trigger delay is set in camera µs, but the camera's delay clock runs 0.33 % fast: the real
  delay is 0.9967 × the setting (`VEYE_DELAY_SCALE`). That's 3 ms at a 970 ms delay.
- The constants are `VEYE_FREE_LEAD_NS`, `VEYE_TRIGGER_OFFSET_NS`, `VEYE_LINE_NS` and
  `VEYE_DELAY_SCALE` in
  `capture/timing.py`, measured on this IMX178. For other cameras (Aravis, other VEYE models) nothing is
  measured, and the code falls back to frame start − exposure, or the pulse + delay.

### How the timing was measured (2026-10-25)

The GPS module's PPS LED was placed in the camera's view, defocused into a round blob covering rows
587–1356. At 1 Hz the LED is lit from 0 to 100 ms after each pulse.

- **Free run, row offsets and line time.** Free run at 33 fps for 16 s. Frames that catch the LED
  switching on or off mid-exposure tell when each 8-row band of the blob started exposing: band
  start = 100 ms − (lit fraction × exposure) at the off edge, and similarly at the on edge. A
  straight-line fit of band start against row number gives:
  - 8 ms exposure: row 0 at −3.39 ms, line 14.34 µs, residual 0.03 ms (674 samples)
  - 20 ms exposure: row 0 at −3.27 ms, line 14.32 µs, residual 0.10 ms (1690 samples)

  These rely on the Pi clock, so they carry its NTP error (about ±1 ms).
- **GPS trigger, no Pi clock.** GPS trigger at 1 Hz, with the trigger delay swept in 1 ms steps so
  that the LED's off edge falls inside the blob rows' exposure. The same band analysis gives row-0
  start = pulse + delay + K:
  - 8 ms exposure: K = 22.48 ms = frame period − exposure + 0.36 ms
  - 20 ms exposure: K = 10.43 ms = frame period − exposure + 0.31 ms

  The leftover slope against row number (−0.6 µs/row) suggests the line time may be slightly
  shorter in trigger mode. That is about ±0.3 ms across the blob; not resolved.
- **Check with the finished code.** In a recording at 1 Hz and 8 ms exposure, every GPS frame is
  stamped hh:mm:ss.022450 with the LED fully lit, as predicted. In free run, the LED brightness
  predicted from the stamps and the line time matched the measured brightness in 100 frames to
  0.2% on average and 6% at worst (about 0.5 ms of the 8 ms exposure).

### Trigger-delay clock (2026-10-26)

The LED was refocused to a small blob at rows 151–190, columns 1508–1554. GPS trigger at 1 Hz, full
frame, 12 bit, 2 ms exposure. The delay was swept in 0.5 ms steps, with the LED's mean brightness
recorded per step (dark 14, lit 59). Delays at which the LED is half lit:

| edge | register | predicted (real time) | real / register |
|---|---|---|---|
| off (100 ms after the pulse) | 68.25 ms | 68.12 ms | 0.998 (± 0.15 %) |
| on, next pulse | 970.94 ms | 968.10 ms | 0.9971 |
| on, pulse after next | 1974.70 ms | 1968.10 ms | 0.9967 |

- The error grows with the delay: +3 ms at one period, +6.6 ms at two. So it's a clock rate, not a
  fixed offset. 0.34 % would match a 74.25 MHz clock counted as 74.0 MHz. That's a guess, not
  checked.
- The register takes values up to 10 s, but **only while the camera isn't streaming**. Writes
  during acquisition are ignored (it reads back the old value). It also **keeps its value after the
  app exits**, so `apply_trigger` always writes it (0 unless `--trigger-delay`). Otherwise a delay
  left over from the last session would shift every exposure while the stamps assumed none.
- With a ~970 ms delay at 1 Hz, frames come every 2 s: every other pulse is lost while the camera
  is busy, as `pulses_per_frame` predicts.

Other measurements:
- Pulse → frame start = frame period + 3.0 ms at exposures from 1 to 200 ms (1 Hz). For example:
  33.1 ms at exposures of 1–20 ms (full frame), 53 ms at 50 ms, 203 ms at 200 ms.
- The trigger-delay register (0x1000) is in **µs**. An earlier test seemed to show otherwise
  because of this start delay.
- Trigger counter register 0x0418: low 16 bits = triggers received, high 16 bits = triggers lost.
  It counts **only while streaming**.
- "High-speed trigger" mode (register 0x0404 = 2) gave no higher trigger rate than mode 1.

## Limits

**Highest usable trigger rate** ≈ 1 / (2 × frame period + 3 ms + exposure margin). The camera drops
every other pulse once that's exceeded; watch `lost` in the status bar.

| region | frame period (min) | max GPS rate (measured) |
|---|---|---|
| 3088 × 2064 (full) | 30.12 ms | 10 fps works; 20 → 10 fps and 30 → 15 fps (every 2nd pulse lost) |
| 1280 × 720 | 10.85 ms | 30 fps works with 0 lost (5 ms exposure) |

- **Pixel depth.** Lower depth reads out faster, measured in free run at 1 ms exposure:

  | depth (VEYE format) | 3088 × 2064 | 1280 × 720 |
  |---|---|---|
  | 12 bit (Y12) | 33.2 fps (30.12 ms) | 92.2 fps |
  | 10 bit (Y10) | 39.2 fps (25.54 ms) | 108.7 fps |
  | 8 bit (GREY) | 40.1 fps (24.91 ms) | 111.5 fps |

  The sensor's line time sets the speed, not the amount of data. So 10 bit gets nearly all of
  the gain while keeping 4× finer steps than 8 bit. Packed and unpacked variants (Y10P / Y10) run
  at the same speed. The GPS trigger limit improves in the same proportion (estimated, full
  frame: about 15.8 / 18.5 / 18.9 fps).
- **Timing is calibrated for 12 bit only.** For 10 and 8 bit, the line time is estimated from the
  minimum frame period (12.13 and 11.80 µs; frame period = rows × line + about 0.5 ms, which fits
  the 12-bit numbers at full frame and at 1280 × 720). The row-0 offsets are assumed unchanged.
  The GUI marks these as estimates. Rerun the LED measurement before relying on 10- or 8-bit
  timestamps to better than a few ms.
- **No hardware binning or subsampling.** The camera reports `ReadMode_Cap` (0x2C) = 1, normal
  readout only, and `ReadOut_Mode` (0x838, newer VEYE register map: 0 normal, 1 binning, 2
  subsampling) reads 0xFFFFFFFF, i.e. not present in this firmware (Device_Version 0x01360201).
  Binning in software would gain neither speed nor signal-to-noise over summing pixels in an
  aperture afterwards. It would only shrink the files. The trigger-capability and trigger-cycle
  registers (0x30, 0x46C, 0x470) are missing in this firmware too.
- **Smallest region (VEYE IMX178, Y12): height 320, any width.** Lower ones fail when the stream
  starts (EPIPE from the Pi's CSI-2 pipeline): height 316 fails and 320 works. A 488 × 320 region
  runs at 195 fps. Width used to be limited too (368 failed, 376 worked), but the sensor now
  always reads full rows (next point), so 8 px wide works.
- **The camera ignores the high bits of the ROI x offset** (firmware 0x01360201, measured
  2026-10-25). The x offset register (0x820) reads back what was written, but the image starts at
  x mod 256: 400 → 144, 800 → 32, 1600 → 64, 2400 → 96. The y offset works, and the write order
  makes no difference. So `veye_mvcam` sets the sensor to full-width rows (y and height as
  asked) and crops x in software (`Stream(crop_x, crop_w)`). That costs no speed: the frame rate
  depends only on the row count (3088 × 480 and 640 × 480 both reach 163 fps). Check with frames,
  not register read-back, before going back to a hardware x offset after a firmware update.
- **Exposure must be shorter than the pulse period.** At 1.6 s exposure and a 1 Hz PPS, most pulses
  are lost.
- **No GPS fix means no pulses**, and so no frames in GPS mode. The GUI shows this in red.
- Free-run timestamps are only as good as NTP (about 1 ms). For GPS-grade timing, use GPS mode,
  or wire the PPS to a Pi GPIO as well (`pps-gpio` overlay + chrony).

## Possible next steps

- Calibrate the row-0 constants for other ROIs and pixel formats. They were measured at full frame,
  Y12; the formulas use the current frame period, so they should carry over, but that is unchecked.
- The rolling-shutter line time and the timing method are in the RAVF header (`ROW-TIME-NS`,
  `TIMESTAMP-SOURCE`). SER has no field for them.
- Wire the compass and calibrate it, if a heading is useful.
- A `pystarlight-camera` entry point would need a `camera` extra (PyQt5, pyserial); gi/Aravis
  remain system-only.
- The older copy in `raspberrypi_v4l2/rpi5_scripts/` doesn't have the row-0 timing corrections or
  the trigger-delay work. It does have the ROI x-crop fix in its `veye_mvcam.py`.
- An "LED off" option for the y-trigger: the off edge needs only a short delay, so no pulses are
  skipped. It needs the pulse length (`min(100 ms, period / 4)`, as `set_timepulse` sets it; 100
  ms after a GPS power cycle).
