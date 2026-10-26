# Capture on the Raspberry Pi 5 with GPS-timed frames

`src/pystarlight/camera.py` is a live viewer and SER recorder for the Raspberry Pi 5. It drives a
VEYE MV-MIPI camera on a CSI port, or any Aravis (GenICam USB3/GigE) camera. A u-blox GPS can
trigger the camera, so that each frame belongs to a known UTC instant. These notes describe the
hardware, how to run the program, how it works, and the timing measurements behind its timestamps.

| file | what |
|---|---|
| `camera.py` | PyQt5 GUI, threads, trigger modes, time stamping |
| `veye_mvcam.py` | VEYE MV-MIPI camera through the `veye_mvcam` kernel driver: I2C registers, media pipeline, V4L2 buffers; the same interface as an Aravis camera |
| `gps_ublox.py` | u-blox GPS: NMEA status, timepulse rate (UBX-CFG-TP5), optional QMC5883L/HMC5883L compass |
| `ser_recorder.py` | SER v3 writer thread, one UTC timestamp per frame |

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

The program uses system packages that the PyStarLight venv can't provide: `gi` and Aravis are
only available for the system Python 3.11. Install them with:

```bash
sudo apt install python3-gi gir1.2-aravis-0.8 python3-pyqt5 python3-opencv python3-serial \
                 python3-numpy v4l-utils
```

Run it as a file, not as `python -m pystarlight.camera`. Importing the package pulls in
scikit-image, astropy and so on, which the system Python doesn't have. When run as a file, it
imports its helper modules from the same folder.

```bash
python3 src/pystarlight/camera.py                                    # free run, full frame
python3 src/pystarlight/camera.py --trigger gps --gps-rate 10 --exposure 20
python3 src/pystarlight/camera.py --trigger gps --gps-rate 30 --roi 904,672,1280,720 --exposure 5
python3 src/pystarlight/camera.py --trigger gps --record-start 23:40:00 --record-duration 60
python3 src/pystarlight/camera.py --list                             # cameras found
```

- `--exposure` is in **milliseconds**.
- `--pps [RATE]` is an old alias for `--trigger gps --gps-rate RATE`.
- `--gps-port none` runs without a GPS. The trigger pulses must then arrive at the selected rate on
  their own.
- See `--help` for the rest: gain, gamma, ROI, pixel format, trigger edge and delay, observer and
  telescope for the SER header.

## The GUI

| panel | contents |
|---|---|
| image | fit to the window; **mouse wheel** zooms around the cursor (up to 64×, pixels as blocks past 1:1), **left drag** pans, **double-click** fits again |
| **Camera** | exposure (log slider + ms box), gain, display gamma, resolution, peak value; measured frame number, rate, interval (mean ± jitter, min..max over ~200 frames), exposure read back from the camera and, in free run, its frame period; **Depth** 12 / 10 / 8 bit |
| **Sensor region (ROI)** | x / y / w / h boxes, **Select** (then drag a rectangle on the image; shown in yellow until applied), **Apply**, **Full sensor**; current region |
| **Trigger** | **GPS trigger** checkbox (off = free run, or the `--trigger hw/sw` mode), **GPS frame rate** 30 / 20 / 15 / 10 / 8 / 6 / 5 / 4 / 3 / 2 / 1 / 0.5 fps, **Trigger delay** (ms), **y-trigger row** (VEYE, GPS mode; see below), warnings (exposure longer than the pulse period, rate too high for the camera, pulses skipped because of the delay, no fix), missed pulses |
| **GPS** | UTC, fix, satellites (used / tracked / in view), DOP, position, altitude, timepulse rate, exposure start of the newest frame, its trigger pulse, pulse → frame delay, rolling-shutter line time, heading if a compass is found |
| **Recording** | folder, observer, telescope, start **Now** or **At UTC** (with "next min"), stop after N s and/or N frames; Record button (orange = armed / Cancel, red = recording / Stop), frame count, elapsed time, countdown, last file saved |
| status bar | exposure, gain, camera and display fps, lost frames, bad buffers, restarts, frame ID, trigger count / lost, sensor position and raw value under the mouse |

Keys: `e/E` exposure, `g/G` gain, `d/D` gamma, `r` record / stop, `t` software trigger (in `sw`
mode), `q` quit.

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
- **Recorder** decodes frames and writes the SER file.
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

## Timestamps: what the SER file holds

Each SER timestamp is the **exposure start of row 0**. The IMX178 has a rolling shutter, so **row y
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
  `camera.py`, measured on this IMX178. For other cameras (Aravis, other VEYE models) nothing is
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
- Write the rolling-shutter line time and the timing method into the SER file. SER has no field for
  it; a sidecar JSON next to the file would work.
- Wire the compass and calibrate it, if a heading is useful.
- A `pystarlight-camera` entry point would need a `camera` extra (PyQt5, pyserial); gi/Aravis
  remain system-only.
- The older copy in `raspberrypi_v4l2/rpi5_scripts/` doesn't have the row-0 timing corrections or
  the trigger-delay work. It does have the ROI x-crop fix in its `veye_mvcam.py`.
- An "LED off" option for the y-trigger: the off edge needs only a short delay, so no pulses are
  skipped. It needs the pulse length (`min(100 ms, period / 4)`, as `set_timepulse` sets it; 100
  ms after a GPS power cycle).
