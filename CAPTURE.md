# Capture on the Raspberry Pi 5 with GPS-timed frames

`src/pystarlight/camera.py` starts a live viewer and recorder (RAVF or SER) for the Raspberry Pi 5. It drives a
VEYE MV-MIPI camera on a CSI port, any Aravis (GenICam USB3/GigE) camera, a [ZWO ASI](#zwo-asi-cameras)
camera, or any Linux video device (`/dev/videoN`: a USB grabber for an analog camera, a UVC webcam);
the last two free-run with the computer's time stamps ([limited timing](#video-devices-devvideon)). A u-blox GPS can
trigger the camera, so that each frame belongs to a known UTC instant. These notes describe the
hardware, how to run the program, how it works, and the timing measurements behind its timestamps.

The code is the `src/pystarlight/capture/` package (`uv run pystarlight-camera`);
`src/pystarlight/camera.py` launches it with the system Python.

| file (`capture/`) | what |
|---|---|
| `app.py` | command line, wiring of camera, threads, GPS and GUI, shutdown |
| `gui.py` | PyQt5 GUI: image view, control panel, status |
| `cameras.py` | camera backends behind one interface (`AravisCamera`, VEYE) and `CameraSession`: pixel format, region, trigger, stream, reconfigure, exposure-start time stamps |
| `asi_cam.py` | ZWO ASI cameras through ZWO's libASICamera2 (ctypes): RAW8 / RAW16, the camera's region, exposure and gain (0.1 dB); free run, frames stamped on arrival; no trigger |
| `v4l2_cam.py` | any Linux video device (`/dev/videoN`): V4L2 mmap capture, luminance of GREY / Y16 / YUYV / UYVY / MJPG, analog standard and input, the device's own exposure / gain controls; no trigger |
| `veye_mvcam.py` | VEYE MV-MIPI camera through the `veye_mvcam` kernel driver: I2C registers, media pipeline, V4L2 buffers; the same interface as an Aravis camera |
| `decode.py` | pixel formats, unpacking of packed 10/12-bit buffers |
| `timing.py` | GPS pulse stamping (`PulseStamper`), VEYE timing constants, exposure-start formula |
| `acquisition.py` | Grabber and Controller threads |
| `recorder.py` | Recorder thread; `repair` of interrupted recordings |
| `stacker.py` | live stacking aligned on the stars, FITS writer |
| `qt.py` | Qt binding: PySide6 (venv) or PyQt5 (system Python) |
| `player.py` | the player `pystarlight player` (GPS / time stamps, frame status, stack & solve; needs the full package) |
| `ravf_writer.py` | RAVF writer (as ASTRID writes), frame scan, repair |
| `ser_writer.py` | SER v3 writer, one UTC timestamp per frame, `.times` sidecar, repair |
| `station_header.py` | station, equipment (config `[observer]`) and GPS site for each new recording's header |
| `localconfig.py` | the private config without the rest of the package (system Python) |
| `favorites.py` | PySyzygos favorites API client: tonight's events -> asteroid, target star, recording window (standard library only) |
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
uv run pystarlight-camera --trigger gps --gps-rate 1.5             # or a period: --gps-rate 250ms
uv run pystarlight-camera --trigger gps --gps-pulse-ms 1           # 1 ms LED flash (timing checks)
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
- `--gps-rate` takes fps (`10`, `1.5`, `1.5fps`, `12Hz`) or the pulse period (`250ms`, `2s`), from
  10 ms to 60 s. Rates that aren't whole fps become a period in whole µs (1.5 fps = 666 667 µs).
- `--gps-pulse-ms` sets the timepulse length (default: a quarter of the period, at most 100 ms).
  The trigger uses only the rising edge, so this only matters for a GPS LED in view: a 1 ms flash
  shows as a narrow band, which makes timing checks easy (see the 2026-10-28 measurement).
- `--pps [RATE]` is an old alias for `--trigger gps --gps-rate RATE`.
- `--gps-port none` runs without a GPS. The trigger pulses must then arrive at the selected rate on
  their own.
- See `--help` for the rest: gain, gamma, ROI, pixel format, trigger edge and delay, observer and
  telescope for the file header.

## The GUI

| panel | contents |
|---|---|
| image | fit to the window; **mouse wheel** zooms around the cursor (up to 64×, pixels as blocks past 1:1), **left drag** pans, **double-click** fits again |
| **Camera** | exposure and gain as last used with this camera model (kept in the GUI's settings; `--exposure` / `--gain` override), exposure (log slider + ms box; the slider spans up to 2 s, typing more than 2 s in the box switches it to 0.5 .. 60 s, typing less than 1 s back; `--max-exposure` caps it, default 60 s or the camera's limit), gain, resolution, peak value; measured frame number, rate, interval (mean ± jitter, min..max over ~200 frames), exposure read back from the camera and, in free run, its frame period; **Depth** 12 / 10 / 8 bit |
| **Display** | as in the player and `pystarlight analyze`: **Black** / **White** point (slider + box, raw values), **Auto** (zscale of the shown image, key `a`), **Full** (0 to the largest raw value: the start setting, and after a depth change unless on Auto), **per frame** (auto levels recomputed on every frame), display **gamma** (keys `d/D`). Display only; the recording is untouched |
| **Sensor region (ROI)** | x / y / w / h boxes, **Select** (then drag a rectangle on the image; shown in yellow until applied), **Apply**, **Full sensor**; current region |
| **Trigger** | **GPS trigger** checkbox (off = free run, or the `--trigger hw/sw` mode), **GPS frame rate**: pick 30 / 20 / 15 / 10 / 8 / 6 / 5 / 4 / 3 / 2 / 1.5 / 1 / 0.5 fps or type fps or a period (`250 ms`) and press Enter, **Trigger delay** (ms), **y-trigger row** (VEYE, GPS mode; see below), warnings (exposure longer than the pulse period, rate too high for the camera, pulses skipped because of the delay, no fix, anchoring to GPS UTC, frames or TIM-TP pulses off the grid), missed pulses |
| **GPS** | UTC, fix, satellites (used / tracked / in view), DOP, position, altitude, timepulse rate, **time base** (GPS UTC since when, the Pi clock's rate error in ppm, the GPS message burst's lag after the second; or what anchoring waits for), exposure start of the newest frame (marked GPS UTC or Pi clock), its trigger pulse, pulse → frame delay, rolling-shutter line time, heading if a compass is found |
| **Recording** | folder, format (RAVF / SER), **Station & equipment …** (dialog, below; its summary line turns red while required data are missing), **Asteroid** with **Favorites** (tonight's events from PySyzygos, see below), start **Now** or **At UTC** (with "next min"), stop after N s and/or N frames; Record button (orange = anchoring / armed / Cancel, red = recording / Stop), frame count, elapsed time, countdown, last file saved |
| status bar | exposure, gain, camera and display fps, lost frames, bad buffers, restarts, frame ID, trigger count / lost, sensor position and raw value under the mouse |

Keys: `e/E` exposure, `g/G` gain, `d/D` gamma, `a` auto levels, `r` record / stop, `t` software trigger (in `sw`
mode), `s` stack, `c` or `Esc` continue the live view after a stack, `q` quit. Keys go to a text
field while it has the focus; entering a rate hands the focus back to the image, and clicking the
image does too.

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
  moment the GPS LED switches on, falls in the **middle of the exposure** of that image row, marked
  with a dashed orange line. **Drag the line** in the image (the cursor turns into ↕ near it) or
  type the row. It keeps the delay there while exposure, region, depth or rate change (not while
  recording). Use it to check the timing: put the LED on the line and the line should cut the LED
  in half.
  - Image row 0 is the **top** of the image (the first row read out and exposed). Row y starts
    exposing y × 14.33 µs after it.
  - The LED's on edge is smeared over exposure / line time rows (2 ms: about 140 rows), between two
    **dotted lines** half an exposure above and below the dashed one. The **first lit row is the
    upper dotted line** (its exposure ends on the pulse). With a long exposure that is far above
    the dashed line: 26 ms puts it 912 rows higher, and a line at the bottom of the frame can't
    bring it below about the middle. Use a short exposure (1–2 ms) for a sharp edge on the line.
  - The delay is up to a full pulse period of the **selected** rate (also while the GPS runs at
    1 Hz for anchoring). Near the camera's limit the camera is still busy at the next pulse, so it
    takes every 2nd pulse and runs at half the rate. The trigger box says so, and those skipped
    pulses don't count as missed. At full frame, 15 fps works only with a delay under ~3.5 ms,
    i.e. not with most y-trigger rows; use 10 fps or a smaller region.
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
- The Qt main thread renders: debayer, then downscale to the view size, then the lookup table (levels + gamma).

**GPS timepulse.**
- `Gps.set_timepulse(rate)` sends UBX-CFG-TP5. The pulse is set to come **only while the GPS has a
  fix** (unlocked pulse length 0), on the **UTC time grid** (`gridUtcGnss`, flags bits 7–10, = 0;
  1 would be GPS time, 18 s off), rising edge first.
- **Whole-Hz rates** are set as a frequency and aligned to the UTC second (`alignToTow`), so 30 Hz
  means pulses at exactly k/30 s.
- **Other rates** (1.5, 0.5 fps, `333ms` …) are set as a period in whole µs. The GPS aligns only
  periods that divide a second, so their phase is taken from **UBX TIM-TP**, which gives the exact
  GPS UTC of the next pulse: the grid is that pulse plus whole periods. Each later TIM-TP is
  checked against it (off by more than 10 µs: counted, shown in red).
- Pulse length is a quarter of the period, at most 100 ms, or `--gps-pulse-ms`.
- The setting is not saved in the GPS: a power cycle brings back 1 Hz. The app restores 1 Hz on
  exit.
- The first rate setting also switches on UBX NAV-TIMEUTC and TIM-TP (once per second each) and
  switches off the NMEA GLL and VTG sentences (unused), so the 9600 baud line doesn't fill up.
- Two serial-port quirks:
  - The **first UBX message after opening the port goes unanswered**.
  - Replies queue behind the NMEA output, arriving about 0.7 s later at 9600 baud.

  So `_send_wait` resends up to 3 times, with a 2 s timeout each.

**Which pulse triggered a frame** (`PulseStamper`).
- A frame's CSI-2 frame-start time comes a steady delay after its pulse. On the VEYE camera that
  delay is about the camera's frame period + 3 ms, plus any trigger delay.
- Frame time minus that delay, rounded to the pulse grid, gives the pulse. That only needs the
  frame time to within half a pulse period (±16 ms at 30 fps). The frame time comes from the
  **GPS clock** below, so no NTP is involved. Without a GPS (`--gps-port none`) it comes from the
  Pi clock instead and the predicted delay follows the measured one.
- The stamp is the pulse's exact grid value, so the frame-time jitter drops out.
- During a **rate change** the GPS switches whenever it gets the command, not on a second. From
  sending the command until a pulse after the GPS's ACK, a frame goes to the nearest pulse of the
  old or the new grid (both UTC-aligned); then the new one only. A new rate that needs TIM-TP
  stamps nothing until TIM-TP has named its grid (about a second).
- Missed pulses (gaps in the pulse sequence) are counted, except for the pulses the camera skips on
  purpose: a trigger that comes while the camera is still busy (its delay + about two frame
  periods + 3 ms after the last trigger) is lost, so with a long delay or a high rate it takes
  every n-th pulse (`pulses_per_frame`).

**GPS UTC without NTP** (`GpsClock`).
- The clock is the Pi's monotonic clock plus an offset that the GPS sets and the trigger pulses
  keep. The Pi's NTP clock is not used for GPS-mode stamps.
- **Anchoring** needs 1 Hz pulses for a few seconds. Every second the GPS sends a burst of messages
  about the second that has just begun; the burst's start (first byte after a quiet line) is taken
  from the monotonic clock, and its NAV-TIMEUTC names the UTC second, used only when the GPS says
  UTC is valid (leap seconds known). The pulse of that second is the last trigger pulse less than
  0.5 s before the burst. Three such seconds agreeing within 20 ms give the offset. (Serial timing
  is far too loose to pick one of 30 pulses a second, but fine for one per second.) They need not
  be consecutive: with a long trigger delay the camera takes every n-th pulse, and a frame taken
  for the wrong second would disagree by a whole second.
- From then on every stamped frame measures the offset again; a line fitted over the last minutes
  follows the drift of the Pi's crystal (shown in ppm), also across gaps. The bursts keep being
  checked against the clock: three in a row off by a whole second mark the clock as suspect (red).
- **Re-anchoring** happens on every rate change and before every GPS-mode recording: the GPS goes
  to 1 Hz, the clock anchors (about 4 s), then the GPS goes to the selected rate. **Record** first
  re-anchors, then arms the recorder once the stamps are exact at the selected rate; a set start
  time begins anchoring 20 s before. It also re-anchors when the GPS trigger is turned on after 10
  minutes without stamped frames or after a warning. Until anchored, frames carry the Pi clock and
  the GUI says so.
- The RAVF header says `TIMESTAMP-SOURCE = GPS trigger pulse, GPS UTC`; the per-frame almanac
  flag comes from NAV-TIMEUTC's "UTC valid".
- **Leap seconds.** Only GPS UTC that the GPS marks valid is used (NAV-TIMEUTC `validUTC`,
  TIM-TP "UTC available"): leap seconds known. After a cold start that can take up to 12.5 min
  (the almanac); until then the clock does not anchor, the GPS box says "UTC not valid yet",
  and a GPS-mode recording waits.
- **The app runs on GPS UTC.** Once anchored, everything the app times itself (start now, a set
  start time, stop, the waits shown, Favorites' "tonight") uses the GPS clock, not the Pi clock;
  before that, and in free run after 10 minutes without stamped frames, the Pi clock.

**System clock from the GPS.** In the field there is no network, so no NTP: without a battery on
the Pi 5's J5 the RTC loses the time at power-off and `fake-hwclock` restores the time of the last
shutdown, hours or days behind. The app sets the system clock from the anchored GPS clock: right
after anchoring, then every minute, when it is off by more than
`[camera] system_clock_tolerance_ms` (default 20 ms; `set_system_clock = false` turns it off).
Not while recording (later). The GPS box's **Pi clock** line shows the result.
- Accuracy: that of the anchoring, i.e. the pulse-to-frame lag of the sensor's timing profile
  (IMX178 measured to ~0.1 ms; the IMX265's profile is assumed, so possibly a few ms) and the
  frame-start jitter (~10 us). Plenty for file times, the mount and other programs; the frame
  stamps never depend on the system clock.
- Setting the clock needs root: a small helper that *adds an offset* to the clock (so the delay
  of sudo does not matter) and copies the time to the RTC. Install it once, root-owned (never
  allow a file in the clone through sudo: anyone who can edit it could run anything as root):
  ```bash
  sudo install -o root -g root -m 755 src/pystarlight/capture/settime_helper.py /usr/local/sbin/pystarlight-settime
  echo "$USER ALL=(root) NOPASSWD: /usr/local/sbin/pystarlight-settime" | sudo tee /etc/sudoers.d/pystarlight-settime
  sudo chmod 440 /etc/sudoers.d/pystarlight-settime && sudo visudo -c
  ```
  Re-run the `install` line when `settime_helper.py` changes. Without it the GUI shows how far
  off the Pi clock is and why it was not set.
- With a network, `systemd-timesyncd` keeps the clock within a few ms anyway, so the app finds
  nothing to set; the two do not fight.
- Check the helper: `sudo -n /usr/local/sbin/pystarlight-settime 0` prints "system clock stepped
  by +0.000000 s; RTC set".
- **Test it offline** (GPS fix, timepulse wired to the camera's trigger input): Wi-Fi off, then
  put the clock off on purpose, NTP first (else it corrects it when the network is back):
  ```bash
  sudo timedatectl set-ntp false
  sudo date -s "1 hour ago"
  uv run pystarlight camera --gps-pulse-ms 1     # GPS mode
  ```
  The GPS box's **Time base** anchors ("GPS UTC since ..."), then within a minute **Pi clock**
  says "set from GPS UTC (stepped +1 h 0 min)", as does the terminal; `timedatectl` shows the
  right system and RTC time. Afterwards `sudo timedatectl set-ntp true` and Wi-Fi on.

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
  centre (needs a GPS fix, else the station's fallback position, with a warning), and the brightest stars with RA/Dec
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
  stars back. The stack and the live frames share the **Display** levels; in
  auto mode the stack gets its own zscale levels while it is shown (and the frame's again on the
  way back).
- First sky test (2026-10-27, 24 mm lens, trees in the lower half): 4 x 0.8 s at 24 dB, full
  frame, solved offline in 7.8 s: centre RA 22h34m27s Dec +46°12' (Lacerta), 17.34° x 11.64°,
  20.37"/px (nominal 20.6"), north 102° from up; the five brightest stars named by SIMBAD
  (α Lac, 1 Lac, 5 Lac, HD 211073, ο And; V 3.6-4.5).
- **Target and markers**: after a solve the view marks the target (pink) and the `mark N
  brightest` stars (blue, default 3) with four ticks around each star (the star itself stays
  free) and a label: the catalogue name an occultation prediction uses, UCAC4, else TYC, else
  Gaia DR3, and the magnitude (`pystarlight.wcs.catnames`). **Target** takes `Gaia DR3 <id>`,
  `UCAC4 zzz-nnnnnn` or RA Dec (`21:24:59.4 +36:40:45` or degrees); it is remembered. Names come
  from the local Gaia catalogue when there is one, else online (UCAC4 and Gaia DR3 at GAVO,
  Gaia DR3 also at ESA; CDS is not needed). On the live frames the markers follow the field's
  drift: each new frame the bright marked stars are re-centred within ±40 px and their median
  shift moves all markers (the target is usually too faint to steer by). Key `m` hides them.
- **Verify**: a second ID of the target (e.g. the prediction's UCAC4 or TYC while Target holds the
  Gaia DR3 id; any of DR3 / UCAC4 / TYC in either field). Both are looked up and cross-identified
  (all their UCAC4 / TYC / DR3 IDs listed in the panel); within 1.5" and without a conflicting
  ID they are the SAME STAR: one marker, the label gets `✓ <verify id>`. Otherwise the panel says
  DIFFERENT STARS with the separation and the verify ID gets its own (yellow) marker. The panel
  also says whether a star is detected at the target in the solved stack (sigma, offset from
  the catalogue position). Tycho-2 is looked up at ESA (`public.tycho2`, retried when ESA says
  "overloaded"), else VizieR. Target and Verify are remembered between runs.
- **Faster solves**: the focal length is remembered between runs (`--focal-mm` wins), so the
  scale hint is not lost on a restart (without it solve-field searches all scales: ~95 s instead
  of ~6 s on a 1.24"/px stack). The solve also searches first within 3° of the last solution's
  centre (remembered across runs), else of the target, and the whole sky only if that fails:
  3.7 s with scale and position on the Pi 5. The SIMBAD checkbox is remembered too.
- **Observer** and **Telescope** (Recording panel) are remembered between runs too; the command
  line (`--observer`, `--telescope`) wins when given. They go into the RAVF/SER header.
- **File names** carry the target before the time:
  `MV-MIPI-IMX178M_UCAC4-634-113397_20261102T001500Z.ravf` (the catalogue name once the target was
  looked up, else the Target text as typed; spaces and other characters become `-`). No target:
  `<camera>_<time>Z` as before. The target name also goes into the RAVF header as OBJNAME, and
  always as `TARGET-STAR`, with `TARGET-RA` / `TARGET-DEC` (degrees, ICRS at the date) once it was
  looked up: `pystarlight analyze --auto` puts the target aperture there by itself.
  First use (2026-11-01, 400 mm f/6, 1.24"/px): Gaia DR3 1963716673315870848 -> UCAC4
  634-113397, G 11.2; brightest UCAC4 637-109640, 638-109181, 636-111806.
  With an **Asteroid** the name carries it before the star,
  `MV-MIPI-IMX178M_158772-2003-SB60_UCAC4-521-006590_20261017T043548Z.ravf`, and OBJNAME is the
  asteroid (the star is in the file name and the viewer's identification).
- **Favorites** (Recording panel): tonight's events from a PySyzygos server fill **Asteroid**,
  **Target**, **Verify** and the recording window (event - 30 s to event + 30 s by default,
  `[camera] record_before_s` / `record_after_s` in the config). Setting up the
  link: [PySyzygos favorites server link](#pysyzygos-favorites-server-link) below.

## PySyzygos favorites server link

[PySyzygos](https://github.com/pyzahl/PySyzygos) (the occultation predictions and favorites list)
serves its favorites read-only over HTTP (its `SERVER_MODE.md`, section 6). With the link set up,
the **Favorites** button in the Recording panel fills in the next event, so nothing has to be
typed at the telescope.

### 1. Start PySyzygos as a server (on the computer that has it)

```bash
pysyzygos gui --set-password                 # once: login for the browser GUI
pysyzygos gui --host 0.0.0.0 --port 8080     # server mode, reachable from the Pi
```

To keep it running, use the systemd unit in PySyzygos' `deploy/` folder (`SERVER_MODE.md` section 3).
If PySyzygos runs on the Pi itself in local mode (`http://127.0.0.1:8080`), the API needs no
token: set only the URL and skip step 2.

### 2. Give PyStarLight its own token

```bash
pysyzygos gui --new-token PyStarLight        # shown once: copy it now
pysyzygos gui --list-tokens
pysyzygos gui --revoke-token PyStarLight     # e.g. if the Pi is lost
```

A token can only read `/api/v1/...`: it cannot open the GUI, start runs or change favorites.
A new or revoked token takes effect at once, without restarting the server.

### 3. Put URL and token into the private config

In `pystarlight.toml` (the repo root, mode 600, git-ignored; the defaults are in
`config_template.toml`):

```toml
[pysyzygos_server]
url = "http://phenom.local:8080/"   # the PySyzygos computer: name or address, port
token = "..."                       # from step 2; empty for local mode on 127.0.0.1
past_hours = 0                      # also list events that many hours back (for tests)
```

Instead of the config, `--pysyzygos URL` on the command line and the environment variables
`$PYSYZYGOS_URL` / `$PYSYZYGOS_TOKEN` work too. The argument comes first, then the
environment, then the config. Keep the token out of shell history and scripts that are shared.

Check the link from the Pi (without printing the token):

```bash
python3 -c "import sys; sys.path.insert(0, 'src/pystarlight'); from capture import favorites as f; \
u, t = f.server_settings(); print(u, 'token set' if t else 'no token'); \
[print(f.describe(e)) for e in f.fetch_favorites(u, t, past=f.past_hours())]"
```

This runs with the Pi's system Python too: `favorites.py` uses the standard library only, and
reads `pystarlight.toml` directly when the package's config module cannot be imported.

### 4. Use it

**Favorites** asks for the events from now (minus `past_hours`) to now + 12 h:

- one event is taken at once. With several, a list shows each one (time, asteroid, star, G,
  magnitude drop, duration, altitude, chance, site) to pick from;
- the chosen event fills **Asteroid**, **Target** (the star's UCAC4 / TYC / DR3 name, looked up as
  usual) and **Verify** (its Gaia DR3 id). It sets start **At UTC** = event - 30 s (whole
  seconds) and **Stop after** 60 s, so the recording ends at event + 30 s. The window is the
  config's `[camera] record_before_s` / `record_after_s` (30 s each by default);
- already inside that window: the recording starts now and stops at event + 30 s. An event that
  is over (only listed with `past_hours`) records the same length from now, marked "(over: test recording
  from now)";
- the line below the button shows the event time and the star's altitude and azimuth, for
  pointing;
- the file name carries asteroid and star, e.g.
  `MV-MIPI-IMX178M_3200-Phaethon-1983-TB_UCAC4-634-113397_20261102T010830Z.ravf`, and OBJNAME is the
  asteroid. The viewer takes the object name for its results (`<movie>.event.json`) from there.

Messages under the button: `favorites: PySyzygos refused the token (401)` (wrong or revoked
token: make a new one), `[Errno 111] Connection refused` / timeout (server not running, wrong
host or port, or the Pi is not on that network), `no favorites in the time window`.

## Player

A video player that knows about time: next to the frames it shows each frame's UTC, its GPS
status and the step to the previous frame, and marks dropped, duplicated and irregular frames.

`uv run pystarlight player FILE` (`capture/player.py`; also `pystarlight-player`, and
`pystarlight-review` as before; without FILE it opens a file dialog in the last folder used, a
FITS frame standing for its folder), or **Play last clip** in the camera
app's Recording box (starts it as its own process, on the file just recorded; with the system
Python it uses the repo's `.venv`). Reads RAVF, SER, ADV/AAV, AVI and FITS folders
(`open_movie`, also unfinished recordings).

- **Frame**: number, UTC, step to the previous frame against the typical step, the problem at
  this frame, and for RAVF the frame header: sequence number, exposure, GPS satellites, fix,
  leap-second status. Other formats show their per-frame status (ADV) if any.
- **File**: size, depth, frames, duration, typical step / fps, problem count, the whole header.
- **Timing track** below the time slider (light blue): for every frame, the time step from
  the frame before it in ms, over the whole file from left to right (same span as the slider).
  Where the file has more frames than the plot has pixels, each pixel column is a bar from its
  frames' smallest to largest step, so a single odd frame still shows. The labels give the
  scale (**max** at the top, **min** at the bottom); the grey dashed line is the **typical**
  step (e.g. 66.67 ms at 15 fps), labelled on the right. Red ticks at the bottom: problem
  frames; yellow line: the frame shown; click or drag to jump. How to read it: a flat line on
  the dashed one = steady timing (GPS-triggered); a spike up to about twice the step = a
  dropped frame; spikes towards 0 = bunched / duplicated stamps (MJPG from a USB grabber); a
  fuzzy band = jittery stamps (Pi clock / NTP). The plot's tooltip says the same.
- **Light-curve track** below it, when the recording has been analysed (`pystarlight analyze`
  writes `<movie>.pystarlight.csv` and `<movie>.event.json` next to it): the target's flux
  (orange: target / the smoothed reference ensemble, as the analysis' step fit has it,
  with the references it used, so saturated ones are left out here too), the step fit's
  levels (green) and the D / R times (magenta, labelled with their UTC). Shown relative to
  its median (the unocculted target at 1; the analysis' own flux is target / the summed
  references, often far from 1) and scaled on the target alone: its own range, widened only
  to take in the fitted levels. So a timing problem can be seen next to the event: is a dropped
  frame close to a D or R? Analyse again and reopen the file to see a new result.
- **Target crosshair**: with an analysis, an open crosshair (four ticks, the star left free)
  marks the target on every frame at its tracked centre, labelled with its catalogue name from
  `event.json` (else the aperture's name); frames the analysis did not cover, and the stack,
  get none. The **Target** box in the play bar (shown only when the analysis has positions)
  switches it off and on; the choice is remembered.
- **Problems**: `check_timing` on the time stamps (dropped, duplicated, backwards, irregular,
  missing) plus RAVF sequence gaps (dropped by the camera link or the recorder queue).
  `n` / `b` jump to the next / previous one.
- **Stack & plate solve** of a frame range (`[` / `]` set from / to at the current frame, `s`
  starts): `LiveStacker` (aligned on the stars) -> `<movie>_f<a>-<b>_..._stack<n>.fits` next to
  the movie -> `solve_fits` (local solver or nova) -> centre, field, alt/az, named stars circled on
  the stack. Focal length and pixel size come from `--focal-mm` / `--pixel-um`, the recording's
  own (effective focal length, its camera's sensor), or `[fallback_equipment]` (with a warning). **Save as the movie's plate solution** writes
  `<movie>.wcs.fit`, `.wcs.json` (stack_start = the range start) and `.stars.json` as
  `pystarlight analyze` caches them, so the analysis uses that solution.
- Keys: Space play/pause, Left/Right, PgUp/PgDn (10), Home/End, `n`/`b`, `[`/`]`, `a` auto levels, `d/D` gamma, `s`, `c` or
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

Install (Debian / Raspberry Pi OS). Pick the index sets whose quads are about 1/4 to 1x the
field width (the field: pixels x plate scale):

```bash
sudo apt-get install astrometry.net                                    # solve-field
sudo apt-get install astrometry-data-2mass-08-19                       # quads 30'-2000'
sudo apt-get install astrometry-data-2mass-04 astrometry-data-2mass-05 \
                     astrometry-data-2mass-06 astrometry-data-2mass-07   # quads 8'-30'
sudo apt-get install astrometry-data-2mass-02 astrometry-data-2mass-03   # quads 4'-8'
```

| package | quads | for fields of about |
|---|---|---|
| `astrometry-data-2mass-00` / `-01` | 2'-2.8' / 2.8'-4' | 2'-16' (14 / 9 GB) |
| `astrometry-data-2mass-02` / `-03` | 4'-5.6' / 5.6'-8' | 4'-30' |
| `astrometry-data-2mass-04` / `-05` | 8'-11' / 11'-16' | 8'-1° (`-05`: 0.64 GB) |
| `astrometry-data-2mass-06` / `-07` | 16'-22' / 22'-30' | 16'-2° |
| `astrometry-data-2mass-08-19` | 30'-2000' | 30' and larger |

The packages download the index files at install time into `/usr/share/astrometry`
(`add_path` in `/etc/astrometry.cfg`); nothing else to configure. Examples: ASTRID on the APO107
(2.07"/px, 50' x 38') solves with `-05` ... `-08-19`; a C14 with a 0.4x reducer (f ~1564 mm,
~1.2"/px, 816 x 620 px = 16' x 12') needs `-02` ... `-05`: with only `-08-19` installed
`solve-field` fails ("Did not solve"), as every quad is larger than the field. Check what is
installed with `uv run python -c "from pystarlight.wcs.solve import local_index_files as f; print(f())"`.

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
  (rolling-shutter line time), `ROI-X`/`ROI-Y`, `TRIGGER`, `TRIGGER-DELAY-NS` (already in the
  time stamps: a GPS-triggered frame is stamped at pulse + delay).
- The target star, when one was entered: `TARGET-STAR` (catalogue name once looked up, else as
  typed), `TARGET-RA` / `TARGET-DEC` (degrees, once looked up). The analysis's automatic
  apertures (`--auto`, key `A`) start from them.
- Station and site (see [Station data in the recording](#station-data-in-the-recording)):
  `OBSERVER-2`, `OBSERVERS-MORE`, `STATION`, `TELESCOPE-TYPE`, `APERTURE-MM`, `FOCAL-LENGTH-MM`,
  `FOCAL-REDUCER`, `MOUNT`, `OBSERVING-METHOD`, `CAMERA-DELAY-S` / `CAMERA-DELAY-APPLIED`,
  `SITE-SOURCE`, `LATITUDE-TEXT` / `LONGITUDE-TEXT` / `ALTITUDE-TEXT`, `ALTITUDE-REF`,
  `GEOID-SEPARATION-M`, `DATUM`, `GPS-FIX`, `GPS-SATELLITES`, `GPS-HDOP`, `GPS-POSITION-UTC`.
- GPS fields per frame: only set for GPS-stamped frames (fix status 3, almanac 0 while the GPS
  has a fix); free-run frames carry fix 0, because their time comes from the Pi clock.

**SER** (`--record-format ser`) keeps its timestamps in a trailer after the last frame. Its
header has room for observer, instrument and telescope only (40 characters each); everything
else the RAVF header holds goes, under the same names, into `<file>.ser.meta.json` next to it
(kept with the file; the viewer reads it into the movie's metadata).

### Station data in the recording

What describes the observation (where, with what, by whom) is written into the recording when
it starts, so that it reaches the occultation report unchanged (PySyzygos'
`docs/observation_interface.md`; the viewer passes it on in `<movie>.event.json`):

- **Station and equipment** come from `[observer]` in the private config: `name`, `observer2`,
  `more_observers`, `station`, `telescope`, `telescope_type` (the IOTA-ES form's Refractor,
  Newtonian, SCT, ...), `aperture_mm`, `focal_length_mm` (effective, with reducer / Barlow;
  millimetres everywhere: config, header, event.json; the `aperture_cm` / `focal_length_cm`
  of earlier versions and `APERTURE-CM` / `FOCAL-LENGTH-CM` of older recordings are read and
  converted, and the dialog's Save replaces the old keys),
  `focal_reducer`, `mount`, `observing_method` (form code, `a` = video), `camera_delay_s` with
  `camera_delay_applied`. Empty or 0 values are left out. `--observer` / `--telescope` on the
  command line override name and telescope.
- **`[observer]` is the only source** for what a recording says about the station. From then
  on the recording itself carries it: the viewer's plate-scale hint comes from its effective
  focal length and sensor (the live solve's from the station's focal length unless
  `--focal-mm`). `[fallback_equipment]` (focal length, pixel size) is only for odd or
  incomplete recordings (no optics in the file), with a WARNING each time, and not for one made
  more than 20 km from the station (someone else's optics); `event.json` lists what a recording lacks
  in `recording.station.warnings` ("no focal length in the recording", ...), and the viewer
  shows them after every Run.
  Only the first time, when `[observer]` holds no station data yet, the dialog fills its empty
  fields from the PySyzygos default site (name -> station, lat / lon / ele -> fallback site,
  aperture_cm -> mm, focal_mm, camera) and says so; Save makes them `[observer]`'s own.
- **Edited in the GUI:** the button **Station & equipment …** in the Recording panel opens a
  dialog with all these fields (and the fallback site); **Save** writes them into `[observer]`
  of the private config file in use (only those lines change; comments, other tables and keys
  stay as they are; a new file is made at `~/.config/pystarlight/config.toml`, mode 600).
  Observer and telescope typed into earlier versions' sidebar fields are offered there once.
- **Complete before recording.** Required (marked * in the dialog): observer, station,
  telescope, telescope type, aperture, focal length, mount, observing method, and without a
  GPS receiver the fallback latitude / longitude. Record (button, `r`, or `--record-start`)
  with anything missing opens the dialog first and does not arm until it is complete.
- **Site from the GPS, only with a fix, taken when the recording starts.** With a GPS
  receiver, Record arms only once it has a fix (the button shows "Waiting for a GPS fix",
  Cancel stops waiting; `--record-start` at program start waits the same way). The header then
  gets the fix at the first frame: `LATITUDE` / `LONGITUDE` plus 7-decimal text copies (RAVF
  keeps the numbers as float32, about 0.5 m), `ALTITUDE` above mean sea level only with a 3D
  fix (`ALTITUDE-REF = "MSL"`, `GEOID-SEPARATION-M` from GGA field 11: the ellipsoid height is
  the sum), `DATUM` blank (WGS84), and the fix it came from (`GPS-FIX`, `GPS-SATELLITES`,
  `GPS-HDOP`, `GPS-POSITION-UTC`); `SITE-SOURCE = "GPS"`. Lost just at that moment: the
  receiver's last fixed position (at most 10 min old), still `"GPS"`, with `GPS-FIX-AGE-S`.
  Without a GPS receiver: the config's `[observer]` latitude / longitude / altitude, marked
  `SITE-SOURCE = "config"` (required then); none at all: `SITE-SOURCE = "none"`, NaN in the
  RAVF's required position entries. Earlier versions wrote the last position even without a
  fix.
- **Kept as recorded, on the way to the report.** The header (RAVF) or the sidecar (SER)
  carries `STATION-DIGEST`, a SHA-256 over the station, site, timing and camera entries; a
  finished recording and its `.ser.meta.json` are made read-only (`chmod u+w` undoes it; a copy
  with `cp`, `rsync` or a file manager keeps the mode). Not a signature, a guard against
  accidents: the viewer checks the digest and writes the result into `event.json`
  (`recording.station.integrity`: `ok`, `changed`, or `none` for another program's file or a
  SER file whose sidecar was left behind; it prints a warning on `changed`). Nothing in
  PyStarLight writes into a recording after it is finished, except `--repair` of an
  interrupted one. **Copy a SER file together with its `.ser.meta.json`**: the SER header
  itself holds only observer, instrument and telescope.

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

## Video devices (/dev/videoN)

Any V4L2 capture device shows up in the camera list (`--list`) next to the Aravis and VEYE
cameras, e.g. a USB grabber (em28xx, stk1160, usbtv, MacroSilicon MS2109) with an analog
video camera (Watec, PC164C, ...), or a UVC webcam. Pick it with `--camera v4l2`, the
device (`--camera /dev/video2`) or its name. Nodes of the Pi's own camera pipeline
(rp1-cfe, used by VEYE / libcamera), codecs and metadata-only nodes are left out.

```bash
uv run pystarlight-camera --camera v4l2 --video-std PAL --video-input 0 --video-size 720x576
```

- **Frames as they come:** an uncompressed format by default (a device left in MJPG by another
  program, e.g. VLC, is switched to YUYV / GREY; `--format MJPG` or the GUI list to keep it), full size and unscaled, an interlaced frame with both fields
  woven, only the luminance (YUYV / UYVY: the Y bytes; GREY, Y16 as sent; MJPG decoded to
  gray, compressed: worse for photometry and for reading a time code, so take YUYV when the
  device offers it). The GUI's Depth list offers the device's formats instead. Region: cut
  out of the full frame in software. `--video-std` (PAL / NTSC / SECAM) and `--video-input`
  (composite / S-video) set the grabber; `--video-size WxH` the frame size.
- **Exposure and gain** are the device's V4L2 controls where it has them (webcams; gain in
  device units, not dB). An analog grabber has none: the camera sets them itself, the GUI
  greys them out, and the recording says exposure 0 (unknown).
- **No trigger:** GPS / hardware / software trigger are off (`--trigger` falls back to free
  run). Each frame keeps the driver's time stamp (V4L2 buffer time, usually the start or end
  of the frame's transfer over USB; turned into UTC by the Pi clock, NTP). That is not the
  exposure: it lags by the camera's integration, the analog line, the grabber and USB
  buffering, tens of ms and not constant (MJPG on an MS2109: frames bunched, 0 ms apart). A
  device without an exposure control keeps that time as it is; one with a control gets it
  minus the exposure. The header says so: `TIMESTAMP-SOURCE` ("V4L2 driver time ..., Pi
  clock (NTP); not the exposure ..."), `TIMESTAMP-ROW` (transfer start / end), and
  `FRAME-TIMING-ACCURACY` one frame period. PySyzygos reads the source as NTP.
- **The true time comes from the image:** record the camera's own time code with it, e.g. an
  IOTA-VTI / KIWI / GPSBOXSPRITE overlay, read later by the library's VTI OCR
  (`timestamps.VtiReader`, `pipeline.analyze(..., vti=...)`; any recording format, as the
  frames are kept unscaled), or GPS-driven LED flashes in the field (the u-blox timepulse on
  an LED), whose frames mark known UTC seconds.
- File names use the device's name made safe (`USB-Video-USB-Video_<UTC>.ravf`); a device
  another program holds (VLC, a browser) is reported as busy.

Tested with a MacroSilicon MS2109 (uvcvideo): YUYV 1920x1080 at 5 fps, 1280x720 at 10 fps,
intervals 200.0 / 100.0 ms, driver stamp at the transfer start.

## ZWO ASI cameras

ZWO ASI cameras (USB2 / USB3) show up in the camera list when ZWO's library is installed;
`--camera ASI` or the model (`--camera ASI178`) picks one. The backend (`capture/asi_cam.py`)
calls the library through ctypes, so it runs with the Pi's system Python too.

- **The library** (closed source, free): `libASICamera2.so` from ZWO's developer SDK
  (zwoastro.com, Software, "For Developers", Linux, `lib/<arch>/`), or INDI's `libasi` package
  (puts it in `/usr/lib/<arch>-linux-gnu`). Found via `$ZWO_ASI_LIB`, the linker's path,
  `/usr/local/lib`, `/usr/lib` or `/usr/lib/<arch>-linux-gnu`.
- **Access without root:** ZWO's udev rule (`lib/asi.rules` in the SDK) into `/etc/udev/rules.d/`.
- **USB3 buffer memory:** Linux gives USB 16 MB by default, too little for full frames of the
  bigger sensors; ZWO recommends 200 MB:
  `echo 200 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb` (lost at reboot; for good:
  `usbcore.usbfs_memory_mb=200` on the kernel command line, `/boot/firmware/cmdline.txt` on the Pi).
- **Formats:** RAW8 and RAW16 as Mono8 / Mono16 (colour cameras: Bayer..8 / ..16). The GUI's
  12 bit is RAW16. RAW16 holds the ADC's bits left-aligned in 16 (the ASI178's 14 bit read
  0 .. 65528, in steps of 4), like ASTRID. Region: the camera's own (width in steps of 8,
  height of 2). Exposure in µs from the camera's limits; gain in ZWO's 0.1 dB steps, shown as dB.
- **No trigger, no hard timing:** the camera free-runs as fast as exposure and USB allow (no
  frame-rate setting; the trigger modes of the few trigger cameras are not used). The SDK gives
  no time stamp: each frame is stamped with the Pi clock (NTP) when it has arrived, minus the
  exposure. Readout and transfer time are not known, some ms to a frame, so the header says
  `TIMESTAMP-SOURCE` "Pi clock (NTP) when the frame had arrived over USB, minus the exposure"
  and `FRAME-TIMING-ACCURACY` 20 ms. As for video devices, true time needs GPS LED flashes in
  the field. Frames the SDK dropped (`ASIGetDroppedFrames`) leave a gap in the frame numbers.
- **A reader thread of its own** takes the frames from the SDK into a ring of ~512 MB (at most
  256 frames), so the SDK's few internal buffers never wait for live view or recorder.
- **USB bandwidth and high-speed mode** stay as the camera has them (it keeps another program's
  settings, e.g. FireCapture's); `--asi-bandwidth PCT` (40 .. 100) and `--asi-high-speed on/off`
  set them. The start-up line `ASI: ...` and the recording's comment say what was used.
- The comment line carries the SDK version, USB2 / USB3, ADC depth, pixel size, USB bandwidth,
  high-speed mode and sensor temperature; the pixel size goes into
  `INSTRUMENT-SENSOR-PIXEL-SIZE-X/Y` (plate-scale hint).

Tested (2026-11-07) with an ASI178MM on USB3, PC (x86-64, 8 cores, 32 GB), SDK 1.27 (INDI's
libasi), recording to an NVMe SSD (1.8 GB/s), 1 ms exposure, `usbfs_memory_mb` 200:

| region, depth, bandwidth | fps | lost |
|---|---|---|
| 1024 × 768, 16 bit (RAVF / SER), 60 % | 47.6 | **0** in 2855 frames (60 s); 8 before the reader thread |
| full frame, 8 bit, 40 %, reading only (no GUI, no disk) | 23.6 | 0 in 30 s |
| full frame, 8 bit, 40 %, written directly (no GUI) | 23.6 | 0 in 30 s |
| full frame, 8 bit, 40 %, camera GUI recording | 22.6 .. 23.5 | 0 .. 36 in 30 s, run to run |
| full frame, 8 bit, 60 % / 100 % | 31 / 14 | ~130 / ~1290 in 30 s |
| full frame, 8 bit, 60 %, to a SATA QLC SSD (164 MB/s) | 26 | ~590 in 60 s, 162 of them by the recorder |

ASI432MM (IMX432, global shutter, 1608 × 1104, 9 µm, 12 bit left-aligned in 16), same PC, 80 %:

| region, depth | fps | lost |
|---|---|---|
| full frame / 8 bit / 800 × 500 16 bit, reading only (5 s) | 84 / 115 / 238 | 0 |
| 800 × 500, 16 bit, GUI recording 60 s (≈ 190 MB/s) | 247 | 51 in 14 821 (single frames) |
| full frame, 16 bit, GUI recording 30 s (≈ 300 MB/s) | 83.9 | ~103, 88 of them by the recorder (its pipeline's limit) |

The losses at full frame are stalls of 0.25 .. 1 s inside the SDK (`ASIGetVideoData` waiting,
every Python thread idle; FireCapture drops there too). A gap-free stream: a region (or 8 bit and
40 % bandwidth at full frame), a disk well above the stream's rate, and a check of the
recording's frame numbers / intervals afterwards (player).

**Page cache:** a recording at ~200 MB/s fills Linux's page cache; once free memory is used up
(~15 s on this PC) the kernel's reclaim stalled the camera's USB transfers. The RAVF / SER
writers therefore drop the pages they have synced (`posix_fadvise` DONTNEED, once a second):
lost frames at full frame, 40 %, from 89 to 36 in 30 s.

## Mount and focuser (INDI)

The camera window's **Mount** panel (menu **Mount**, Mount panel, or Shift+M; a dock: beside the
side panel, or pulled out as a window of its own, closed when not needed; Qt remembers the
arrangement) drives a mount through [INDI](https://www.indilib.org). The panel holds only what is
used at the telescope (Connect, position, target, goto / sync, jog, park / home); the drivers and
the connection are in **Mount → INDI settings…**, a window of their own. It follows ASTRID's approach: pick the drivers,
and the program runs `indiserver` with them.

- **Drivers** (INDI settings): the lists show the installed INDI drivers (from INDI's XML files in
  `/usr/share/indi`), e.g. **ZWO AM5** (`indi_lx200am5`) and **ASI EAF** (`indi_asi_focuser`,
  package `indi-asi`); `indi_simulator_telescope` for trying it out. Debian / Raspberry Pi OS:
  `sudo apt install indi-bin indi-asi`. The choice is saved in the config's `[indi]`
  (`mount_driver`, `focuser_driver`, `start_server`, `host`, `port`).
- **USB or Wi-Fi** (INDI settings): the **Connection** list (config `mount_connection`: `usb`, `wifi` or empty for the
  driver's default). **The AM5 driver defaults to Wi-Fi** (192.168.4.1), so USB has to be chosen;
  the port is then the system port named ZWO (here `/dev/serial/by-id/usb-ZWO_Systems_ZWO_Device_
  123456-if00`, i.e. `/dev/ttyACM0`), or the one typed (`mount_port`). The user needs to be in the
  `dialout` group. Wi-Fi: the driver's address is the AM5's own hotspot (`192.168.4.1:4030`;
  join its network); an AM5 on your own network: type its `host[:port]` in the same field. Tested (2026-11-07): an AM5 over USB, INDI 1.9.9: connects, reads position,
  tracking and park state; its home is the driver's `GO_HOME` (element `GO`). Its clock read
  2000-01-01 before Connect set the time.
- A second `indiserver` on the machine (e.g. the tests' simulator next to the AM5's) gets its own
  local socket (`-u /tmp/indiserver-<port>`; INDI 1.9 ends a second one at once otherwise).
- **Connect** starts `indiserver` with the drivers (stopped again on Disconnect and on exit), or,
  with "Start the INDI server here" off, connects to one at host:port (another computer, KStars /
  Ekos). The mount gets the site (GPS fix, else `[observer]`) and the time (UTC).
- **Status:** where the mount points (RA / Dec J2000, altitude / azimuth at the site), slewing /
  tracking / parked, the driver's last message; one line in the status bar too.
- **Target:** typed (`20:41:25.9 +45:16:49`, `20h41m25.9s +45d16m49s` or degrees, J2000), or
  **From target** (the Recording panel's looked-up target) / **From last solve**.
- **Goto** (then tracking), **Goto, no tracking** (slews with tracking on and switches it off once
  there, as ASTRID does: some mounts ignore a slew without tracking), **Sync to target**,
  **Abort** (INDI's `TELESCOPE_ABORT_MOTION`: stops any goto / slew / homing, and ends a running
  Solve & sync or Center), **Tracking**, **Park / Unpark** and **Home**, both asking first (Unpark does not), **Home** (INDI 2's `TELESCOPE_HOME`, or the driver's
  own `GO_HOME` as the AM5's in INDI 1.9; greyed out when the driver has neither).
- **Jog:** square N / S / W / E buttons around the speed list move the mount while held; the
  jog area's background shows tracking (light green) or not (light red), plain when not connected (INDI's `TELESCOPE_MOTION_NS` / `_WE`), at the
  **Speed** picked from the driver's rates (AM5: 0.25x .. 1440x sidereal; remembered per mount).
  Release stops it, also when the mouse slides off the button; a lost release is caught by the
  next status poll (twice a second); Abort and Disconnect stop it too.
- **Solve & sync:** a new stack (as `s`), plate-solved, and the mount synced to its centre.
  **Center:** goto the target, solve & sync, and goto again until within 1′ (at most 4 rounds).
- Positions are J2000 throughout; the mount gets the current epoch (`EQUATORIAL_EOD_COORD`, IAU
  1976 precession, within 0.1″ of astropy; nutation / aberration, under 1′, go with the sync).
  A driver with `EQUATORIAL_COORD` gets J2000 as it is.

The INDI client (`capture/indi.py`) and the mount (`capture/mount.py`) use only the standard
library (the Pi's system Python). Tested against INDI 1.9.9's telescope simulator (goto, sync,
tracking off after a slew, the Center loop); not yet on the AM5. Next: pre-pointing a favorite
(goto ahead with tracking, or the fixed-alt/az point for a mount that does not track) and EAF
autofocus from the stars' FWHM.

## Timestamps: what the recordings hold

**Which instant of the exposure** a recording's time stamps mark is in its header,
`TIMESTAMP-REFERENCE`: `start` for every PyStarLight recording (each backend works the
exposure start out of the pulse, the frame start or the arrival), `unknown` for a video
device without an exposure control. The analysis moves the times to mid-exposure from it
(README, "Event timing"); recordings without the entry follow their format (RAVF start, ADV
middle). DVTI cameras, for example, stamp the middle of the exposure.

Each frame's timestamp is the **exposure start of row 0**. The IMX178 has a rolling shutter, so **row y
starts y × 14.33 µs later**: 29.6 ms down to the last of 2064 rows. For an object at row y, add
that offset.

| mode | row-0 exposure start | clock |
|---|---|---|
| GPS trigger | pulse + 0.9967 × trigger delay + (frame period − exposure) + 0.51 ms | GPS only |
| free run | frame_ns − exposure − 3.33 ms | Pi (NTP) |

- `frame_ns` is the CSI-2 frame-start time that the rp1-cfe driver records in CLOCK_MONOTONIC,
  converted to CLOCK_REALTIME.
- "Frame period" is the camera's current frame period (`veye_mvcam.Camera.frame_period_ns()`). It
  follows the exposure, with a minimum of 30.12 ms at full frame and about 10.85 ms at 1280 × 720.
- In trigger mode, a VEYE camera holds off the exposure so that it **ends** about one frame period
  after the trigger. Short exposures therefore start well after the pulse: about 22.6 ms after it
  at 8 ms exposure, full frame. Stamping a GPS frame at the pulse itself would be up to about 30 ms
  early.
- The trigger delay is set in camera µs, but the camera's delay clock runs 0.33 % fast: the real
  delay is 0.9967 × the setting (`VEYE_DELAY_SCALE`). That's 3 ms at a 970 ms delay.
- The 0.51 ms GPS-trigger offset was re-measured on 2026-10-28 (below). It was 0.33 ms before,
  so **GPS stamps recorded before 2026-10-28 are about 0.18 ms early**.
- The constants are `VEYE_FREE_LEAD_NS`, `VEYE_TRIGGER_OFFSET_NS`, `VEYE_LINE_NS` and
  `VEYE_DELAY_SCALE` in
  `capture/timing.py`, measured on this IMX178. For other cameras (Aravis, other VEYE models) nothing is
  measured, and the code falls back to frame start − exposure, or the pulse + delay.

### Trigger offset re-measured (2026-10-28)

Setup: GPS LED on the timepulse in the camera's view, defocused, VEYE IMX178 at 12 bit, GPS trigger,
y-trigger to place the edges. A long exposure smears the LED's on edge over many rows, so the
cleanest test uses a **1 ms LED flash** (`--gps-pulse-ms 1`) at 30 fps with 2 ms exposure in a small
region at the top left (832 × 444): the LED then shows as a band about 210 rows tall with both
edges inside the region.

- The band's **first** row is the row whose exposure **ends** on the pulse; its **last** row is the
  row whose exposure **starts** 1 ms after the pulse.
- Measured: first row ~20 (predicted with the old 0.33 ms: 30.2), last row ~227 (predicted 239.6).
  So exposure start is 0.18 ms and exposure end 0.14 ms later than the old offset gave; the real
  exposure matches the setting to about 0.05 ms. Offset now 0.51 ms.
- The exposure-end edge alone, in five more recordings:

  | region, rate | exposure | trigger delay | first lit row | predicted (old) | difference |
  |---|---|---|---|---|---|
  | full, 10 fps | 26.1 ms | 65.1 ms | 299 | 308 | −9 rows |
  | full, 10 fps | 26.1 ms | 61.0 ms | 585 | 594 | −9 |
  | full, 10 fps | 26.1 ms | 54.6 ms | 1033 | 1042 | −9 |
  | full, 10 fps | 21.3 ms | 68.7 ms | 49 | 58 | −9 |
  | full, 10 fps | 1.5 ms | 47.1 ms | 1555 | 1565 | −10 |
  | 568 × 320, 30 fps | 1 ms | 27.0 ms | ~53 | 65 | −12 |
  | 568 × 320, 30 fps | 2 ms | 27.5 ms | ~20 | 30 | −10 |

  (9 rows = 0.13 ms.) The same offset at all rows, exposures and delays confirms the line time
  (14.33 µs), the top-to-bottom readout and the delay-clock scale; it is a constant offset.
- With the LED saturated, its edge is sharp to about a row even at long exposures: a few µs of
  light already show.
- The free-run lead (3.33 ms) was not re-checked.

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

### Other VEYE models: IMX265M (global shutter)

The timing model is chosen by sensor (`timing.sensor_timing`, `CameraSession.timing`):

| profile | models | status |
|---|---|---|
| `IMX178` | MV-MIPI-IMX178M | measured (12 bit; 10 / 8 bit estimated) |
| `SONY_GLOBAL` | IMX265M, IMX264M, IMX296M, IMX287M (Sony Pregius global shutter) | **assumed, not measured** |
| IMX178 model, uncalibrated | any other VEYE MV model | guess |

What the Sony global-shutter profile assumes until it is measured:
- All rows expose together: no row offset (`ROW-TIME-NS = 0`, `TIMESTAMP-ROW` says "global
  shutter"), and the GUI's dotted y-trigger lines and row label change accordingly. The
  y-trigger then simply puts the pulse in the middle of the exposure.
- GPS trigger: the exposure **starts** at the trigger + delay (offset 0), unlike the IMX178, which
  holds the exposure until about a frame period after the trigger. Frame start about 1 ms after
  the exposure ends. The delay register is taken as exact (the IMX178's 0.9967 is not assumed).
- Free run: the frame starts when the exposure ends (lead 0).
- Trigger rate: VEYE's manual says Sony global-shutter sensors overlap the readout of one frame
  with the next exposure in trigger mode, reaching "almost the same external trigger frame rate as
  in video streaming mode" (IMX265M: 55 fps at 2064 × 1544). So the camera is taken to be busy for
  one frame period + 0.5 ms after a trigger (IMX178: two frame periods + 3 ms). That also means
  much less dead time between exposures: the exposure can fill most of the pulse period.
- Recordings say `FRAME-TIMING-ACCURACY` 5 ms (uncalibrated) instead of 0.1 ms.
- Not modelled either: the smallest region (the IMX178's 320 rows is assumed) and the x-offset
  firmware bug (x is cropped in software anyway, which works on any model).

**Measuring it** (the same LED setup, `--gps-pulse-ms 1`): with a global shutter the LED doesn't
form a band; it is lit in a frame when the 1 ms flash overlaps that frame's exposure, and its
brightness is the overlap. Sweep the trigger delay (y-trigger off) in 0.1 ms steps at a short
exposure (e.g. 2 ms) and note where the LED starts and stops being lit: the flash starts at the
pulse, so the delays give the exposure start and end relative to the pulse. Then set the profile's
`trigger_offset_ns` (and `lag_extra_ns`, from the "Pulse → frame" readout) in `timing.py`, add the
depth to `calibrated_bits`, and check the delay scale with a long delay (one pulse period later).
To measure the dead time, raise the exposure towards the pulse period at a rate near the maximum
and watch for frames where a flash at the end of the period goes missing.

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
- **The frame-rate register can't be trusted after a depth change** (seen 2026-10-28, 448-row
  region): it read 174 fps in 8, 10 and 12 bit, but 12 bit reads out in 6.96 ms (144 fps), so
  the camera ran at that limit while the stamps assumed 5.75 ms: about 1.2 ms (84 rows) early.
  For the IMX178 at 12 bit the frame period now has a floor at the readout limit,
  rows × 14.33 µs + 0.54 ms (30.12 ms full frame, 10.86 ms at 720 rows), and faster rates are
  never written. 8 and 10 bit have no such floor (their line times are estimates); in that
  test the register's 5.75 ms fitted 10 bit to a row, and 8 bit to 11 rows.
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
- **GPS anchoring assumes** the GPS starts each second's message burst less than 0.5 s after the
  second (watch "burst +N ms" in the time base row) and that TIM-TP reports UTC when the pulse is
  on the UTC grid (a TIM-TP more than 2 s off the clock is ignored, and rates that need it then
  stamp nothing). If the serial line is so busy that bursts run together, anchoring doesn't
  finish; it never produces wrong stamps.
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
