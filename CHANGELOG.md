# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Windows setup offers ASTAP:** when no ASTAP with a star database is found, it asks to download
  and install ASTAP + D80 (official downloads linked from hnsky.org; their installers ask for
  administrator rights); Start menu item *Install ASTAP (plate solver)* (`setup.ps1 -Astap`); the
  closing message says whether offline solving is ready (`-NoAstap` for tests, used by the release
  workflow).

## [1.1.0] - 2026-11-27

### Added
- **Docs: "What if a Starlink satellite occults a star?"** (`docs/starlink_occultation.md`, figure and
  script `docs/starlink_occultation.py`, linked from the README intro; a slide in the method slides): the
  diffraction model at 550 km. 0.4 ms (body) to ~4 ms (with the panels), almost total for every star (even a
  45 mas supergiant is a point source there), full Fresnel fringes, and only a 0.01–0.14 mag dip in a 30 fps
  video.
- **Windows setup: one file, double-click** (`PyStarLight-Setup.cmd`, attached to every release;
  `windows/setup.ps1` behind a small cmd header, built by `scripts/build_windows_setup.py`).
  - Per user, no administrator rights, no Python, no git: a private uv, uv's Python (with Tk),
    `uv tool install` of the release wheel with the release's `constraints.txt` (the tested
    versions).
  - Start menu folder: Analyze, Player, Camera, Settings, Check installation, Logs, Update,
    Uninstall. Desktop icons: Analyze, Player, Camera.
  - "Open with" for recordings: `.ravf` / `.ser` / `.adv` / `.aav` open in the player, and the
    player and the analyzer are offered for `.avi`.
  - At the end, `pystarlight doctor`.
- Windowed launchers (`[project.gui-scripts]`, `pystarlight.winlaunch`): no console window, output
  in a log file, a message box when a program cannot start; Settings opens the private config
  (created from the template). Extra `desktop` (all + Qt + pyserial).
- Release workflow on a `vX.Y.Z` tag:
  1. build the wheel, the sdist, the setup and the constraints;
  2. run the setup on a real Windows runner (install, check, shortcuts, file types, uninstall);
  3. create the GitHub release with all files.

  PyPI only when the repository variable `PUBLISH_PYPI` is `true`.

- **ASTAP as a plate solver** (`wcs/solve.py`: `solve_astap`, `astap_available`, `solver_order`):
  offline on Windows, Linux (incl. the Raspberry Pi) and macOS, with one of ASTAP's star databases.
  `[astrometry] solver = "auto"` now tries the installed offline solvers: ASTAP first when the
  position is roughly known, astrometry.net first for a blind solve, the other one when the first
  fails; nova only when neither is installed. New settings `astap`, `astap_db`, `astap_database`.
  `pystarlight doctor` reports both solvers.
  - ASTAP needs the field size: from the plate scale, else field heights 0.1 to 5 deg are tried in
    turn (a scan solution whose own field height is more than 25 % off the one tried is rejected).
    `auto` puts ASTAP first when the position is known; without it astrometry.net goes first, and
    ASTAP follows only with a plate scale. With neither, ASTAP is never run (`solve_astap` refuses):
    its blind scan gave false solutions on two of the owner's recordings.
  - Benchmark on 11 of the owner's recordings (ASTRID RAVF, ADV 816x620 / 408x310, offline, D80
    vs 2MASS 4202-4219), with a position hint: ASTAP solved 7/8 (median 2.2 s), astrometry.net 5/8
    (3.8 s); ASTAP solved two that astrometry.net could not. Where both solved they agree to a
    median of 1.6" (max 7.8"). Blind: astrometry.net 9/11 (median 8 s); ASTAP 4/11, two of them
    false.
  - Tested on Windows Server 2025 (ASTAP + D20 / D80): the Fringilla ADV (16' x 12' field, no
    scale in the file) solved offline in 0.9 s; the same solution as astrometry.net's blind solve
    (15 s) to under 0.5".
- **Diffraction-aware event fit** (`diffraction.py`, docs/diffraction.md): Fresnel diffraction at the
  shadow edges (1-D bar model, exact in Fresnel integrals; several strips add in the field),
  finite limb-darkened star, **double stars** (each component its own D and R, the primary's
  share of the light), the camera band (one wavelength, 600 nm, or an optional QE table and star
  temperature; wavelengths summed incoherently), the exposure. Least-squares fit of the D / R
  times started from the step fit; **Monte-Carlo errors** (the model plus AR(1) noise like the
  residuals, refitted; sigma = the larger of covariance and MC); per component the D-R pairing,
  duration and chord, flagged **ambiguous** when the light share is close to 0.5 (both pairings
  given). A 2-D sinc engine (E = W S W) ported from Bob Anderson's notebooks with his
  permission (Cubillos & Jimenez 2022) for any silhouette.
- Analyzer: button **diffr: off / auto / on** (top row; default `[diffraction] mode`, off). After
  the step fit the diffraction fit runs in the background ("auto": when the step fit shows
  double-star steps or the Fresnel scale plus the star takes >= 10 % of a frame); distance and
  shadow speed from JPL Horizons for the recording's asteroid (`wcs.asteroid.shadow_geometry`)
  or the config. Shows the differences: `<movie>-diffraction` figure (both models, a close-up of
  every D / R, a table of step vs diffraction times, sigma from fit and MC, components and
  chords), `event.json` `diffraction_fit`, a status line.
- Method slides: "Event timing, parts 3 and 4" (diffraction, the fit, double stars, sinc method)
  with references.
- Player 1.1.0: a recording **dragged from a file manager** (Thunar, Nautilus, Dolphin ...) onto
  the window opens (a FITS frame: its folder); a file that does not open leaves the one shown
  as it was (also from the dialog: the new file is opened before the old one is closed).
- Camera 1.5.0: **DSLR window** (menu DSLR, Shift+C; `capture/dslr.py`, `capture/dslr_gui.py`,
  extra `dslr`: python-gphoto2 + rawpy, config `[dslr]`): a DSLR or mirrorless camera as a second,
  photo-only camera while the video camera keeps streaming, recording and tracking. ISO,
  aperture, image format and where the files go from the camera's own lists; the exposure set
  to the nearest shutter speed, or Bulb timed by the computer beyond the longest; **Photo** keeps
  the camera's files (ARW / CR2 / NEF, JPEG) and writes a FITS: the RAW's Bayer mosaic as uint16
  (BAYERPAT, ROWORDER TOP-DOWN, black / white level, white balance, ISO, shutter) or a JPEG's RGB
  planes, with the time the release was sent (GPS UTC when anchored), the site and the target.
  Colour live view in the window; the last photo shown debayered with the camera's white
  balance, dim red in night mode. Seen working with a Sony A7R II (connect, settings, live view);
  photos tested against a simulated camera. `pystarlight doctor` checks the extra.
- Camera 1.5.0: **photo programs** (DSLR window, Program tab; `capture/schedule.py`,
  `capture/schedule_gui.py`), derived from the owner's SoFiMagic: its eclipse XML programs read
  and written (contacts C1 .. C4, C0, phases with interval (N) or back-to-back (-1) shooting,
  exposure lists with drive S / C / M / L / B, bursts and brackets done here), our TOML for any
  reference times (event time, NOW) and `every`; the editor ported from the Sony app's settings
  screens; timeline with warnings; test run; runs on the GPS UTC clock with the exposure set
  before the slot, catch-up / skipping, no exposure into the next phase, failed photos logged
  and passed, a CSV photo log; files with FITS (in the background), files only, or left on the
  card. Start: as programmed, or shifted so the program or a reference (C1, C2, ...) comes in
  15 s. Dry run without a camera, fast-forward or on time, as a Markdown list + CSV (GUI, and
  `python -m pystarlight.capture.schedule`). The tab is laid out as the Sony app's screen. DSLR files are named as the recordings: `<model>_<asteroid>_<star>_<UTC>Z_photo.*`.
- Camera: DSLR photos **plate-solved and annotated as the video camera's stack** (`capture/photo_solve.py`;
  the marker code moved out of gui.py into `capture/marks.py`, shared): the WCS (RA / Dec <-> pixel),
  centre, scale, named stars and the target's pixel into the photo's FITS (its mosaic unchanged),
  `.wcs.fit`, `.solve.json` and an annotated colour PNG beside it; the markers on the DSLR window's
  photo; `python -m pystarlight.capture.photo_solve` for photos afterwards. `solve_stack` takes a
  timeout for the whole-sky solve (a DSLR photo without a scale hint: `[dslr] solve_timeout_s`).
- Camera: `photo_solve` takes camera files (ARW, CR2, NEF, DNG, JPG; FITS to `-o DIR`), `--focal`
  / `--camera` for the scale hint; the annotated PNG stretched for the sky (asinh from the sky
  level) with the camera window's tick markers. `capture/photo_stack.py`: colour stacks of DSLR
  photos aligned by their plate solutions (homography; drift and field rotation), weighted
  mean with leave-one-out kappa-sigma clipping, the stack solved and annotated. Tried on the
  owner's 2017 A7R II + K150 (3000 mm) photos of M31.
- Camera: gui.py split: `imageview.py` (ImageView, RecordOverlay), `widgets.py` (night theme,
  CollapsibleBox, LevelsBox, zscale), `station_dialog.py`; `gui` still offers the names.
  `stacker.write_fits` writes uint16 (BZERO) and planes (n, h, w) too.
- Camera 1.5.0: **Polar alignment** window (menu INDI Telescope Controls, Shift+P;
  `capture/polar.py`, `capture/polar_gui.py`, config `[polar_align]`). Stack + plate solve, sync,
  turn the RA axis only (a goto at the mount's own Dec), solve again: the RA axis from two points
  and the turn the mount reports, or from the plane through three points (the two-point answer as
  a check); refraction put in. Any field with stars works, Polaris (σ Oct) is optional, so a
  limited horizon is fine: the allowed range is the RA step, the direction (auto: the side that
  stays highest), a minimum altitude along the whole way, and no meridian crossing (a German
  mount would flip). Then a star near the centre (or one clicked) is followed live while the
  Alt / Az knobs turn: the window shows what is left ("Alt: raise 12.3′", "Az: move east
  4.1′", the error left, ✓ aligned under 2′), the image an arrow from the star along the Az leg,
  then the Alt leg, to a ring, and the pole and the RA axis when they are near the field.
  **Refresh** plate-solves again (knobs at rest) and re-bases the model. Not yet on the sky.
- Camera 1.4.0: the Focuser is a **window of its own** (menu INDI Telescope Controls, Shift+F;
  closed when done, place and size remembered; `capture/focuser_gui.py`) instead of a panel, and
  has a **focus curve** (`capture/focus.py`): a star (Centre star: the brightest unsaturated one
  near the centre; or Pick star and click one) measured on the live frames, its half-flux
  diameter (HFD, still meaningful when the star is a ring far from focus; FWHM shown too)
  against the focuser position, each position once the focuser has stood still for 1 s; HFD²
  fitted as a parabola, **Go to best** goes to its lowest point. Only the star's own connected
  light counts (a neighbour's wing stays out); SATURATED warns. Closing the window stops it.
  The plot is dim red in night mode.
- Camera: the mount connects before the focuser (a focuser driver that cannot connect no longer
  holds it up); the Focuser window says why a focuser is missing, and names `indi_asi_focuser`
  when a ZWO EAF is on USB with another driver picked. A focuser's state shows Alert only from
  the property it is moved by (the EAF keeps an unused one in Alert). The Focuser window shows
  the driver's latest message for the focuser, and after a USB drop-out (ZWO's "Error: 4") says
  to Disconnect / Connect; CAPTURE.md: power the EAF from 12 V or a powered hub (the Pi 5 cuts
  all USB ports on over-current).
- Installer: `--launcher` writes menu entries and desktop icons for Camera (in a terminal, the
  window waits for Enter at the end), Player and Analyze (Linux; start scripts in ~/.local/bin,
  remembered and refreshed on each update; `--no-launcher` stops that); `--camera-args` the
  Camera's options (remembered).
- CAPTURE.md: the IMX265M's GPS timing checked against ASTRID #0182 with hand flashes on a
  white ceiling (2026-11-18): run 1, 7 edges, −0.1 ms; run 2, 56 flashes at 200 fps, +0.2 … +0.5
  ms (ASTRID saturated, fitted to the IMX265's light curve); run 3, 60 flashes, +0.24 ± 0.38 ms.
  Together +0.25 ± 0.12 ms (the IMX265 probably a quarter ms late). No frame or ms offset.
- Wiring diagram (`docs/wiring/capture_wiring.svg`): the field network switch (pin 37 GPIO26 to
  pin 39 GND) and its LED (pin 38 GPIO20, 330 ohm, pin 34 GND), with the far end of the header.
- CAPTURE.md: the Pi header pins in use, one table (GPS, compass, hotspot switch GPIO26 / pin 37
  and its LED GPIO20 / pin 38, at the far end of the header, clear of the connector on pins 1-10).
- `scripts/netswitch.py`: home WiFi or the Pi's own hotspot by a toggle switch on GPIO26 / pin 37
  (LED on GPIO20 / pin 38), or by hand; installs itself as a systemd service. `hotspot` refuses at
  once when a system `dnsmasq` service blocks the hotspot's own (it would fail after 30 s with
  "Address already in use") and names the fix. `cert`: one VNC certificate naming the host, the
  hotspot address and the Pi's addresses, so viewers connect without a name warning on either
  network. `home` brings up the known home WiFi by name (in range; highest priority, then the one
  used last): `nmcli device connect wlan0` started the hotspot again a second after taking it down.
  `pin` reads the switch through `pinctrl` while the service holds the line; the service logs each
  switch at once (`journalctl -u pystarlight-netswitch`). CAPTURE.md "Remote screen in the field": VNC
  (wayvnc, Remmina), autologin under SDDM, a fixed screen size without a display.
- Camera: Focuser window: a number typed into Go to stays until it is used (the half-second
  refresh put the focuser's position back between pressing and releasing Go to); Enter goes
  there too.
- Camera 1.3.0: **Focuser panel** (menu INDI Telescope Controls, Shift+F; `capture/focuser.py`) on the Mount
  panel's INDI connection, the focuser driver from INDI settings (e.g. `indi_asi_focuser` for a
  ZWO EAF, package `indi-asi`): position (and temperature), steps in / out (the step picked, or
  ten of them; remembered per focuser), go to a position, abort, and a focus mark to return to.
  Relative-only focusers show the steps moved since connect, timer focusers take ms. A focuser
  alone, without a mount driver, connects too.
- Camera: **star lock** (menu INDI Telescope Controls → Star lock panel, Shift+L; `capture/guider.py`,
  `capture/lock_gui.py`, config `[lock]`). Locks on 2-3 reference stars (never the target),
  corrects the mount with INDI guide pulses through the Mount panel after a short calibration,
  and plots the target's flux live (target / lock stars). Corrections only while every check
  passes (SNR, flux, width, no jumps, the stars moving together); clouds or a lost star: hold, no
  pulses; held for 30 s: lost, and found again only on the same star pattern. Guiding stops by
  itself if the corrections make it worse. **Lock** is a check box, on and off by hand,
  independent of recording; one CSV row per window next to the recordings. `Mount.pulse` / `can_guide` / `guide_rate`.
- Camera: **Night mode (red)** (View menu, F9, remembered): the GUI dark with dim red text and
  lines (a Qt style sheet), the image and its annotations unchanged; **Full screen** (F11) hides
  the window manager's title bar. The buttons on the image are outlined and see-through; the
  Mount panel's jog area is dark green / dark red with a border at night.
- Camera 1.2.0: **Photo** (Stack & plate solve box): one frame at its own exposure (remembered;
  the live exposure comes back after it), saved as `<name>_<UTC>Z_photo.fits` next to the
  recordings with the exposure start (DATE-OBS), exposure, camera and site, and once the field is
  plate-solved the WCS moved onto the photo (region and tracked drift), the centre (RA / DEC,
  OBJCTRA / OBJCTDEC), the target (OBJECT, TRGRA / TRGDEC) and the named stars (STARn: name ra
  dec x y). Not while recording; in GPS mode only up to the longest exposure the rate allows
  (`capture/photo.py`). Also a **PHOTO** button next to REC on the image.
- Camera: a **Record / Stop** button and the UTC time (HH:MM:SS, GPS clock once anchored) on the
  image, top right, so they never scroll away with the side panel; the Recording box's button
  stays (same action, same colours).
- Camera: **Crosshair** check box in the Display box (across the whole image through its centre,
  the middle open; remembered), and once plate-solved **N / E arrows** on the image (lower
  right), from the solution's WCS as in the analysis viewer.
- Camera (VEYE): free run after GPS mode could get no frames at all until a power cut (a
  firmware hang, IMX265M). Switching to free run now waits for the sensor to go idle, and a
  repeated free-run stall reboots the camera over I2C (its own reboot command) and sets it up
  again: format, region, trigger, exposure, gain.
- Camera, Trigger panel: **Exposure: longest for the rate** (on by default): setting the GPS
  rate (or switching to GPS, or a new region) sets the longest exposure with which the camera
  still takes every pulse (`timing.max_exposure_us`: the VEYE busy model, IMX178 two frame
  periods, Sony global shutter one, 3 % / 1 ms margin; other cameras: before the next pulse).
  Changing the exposure by hand unticks it.
- Camera: a goto / Center while the star lock runs no longer loses the stars: the Mount panel
  tells the camera window the slew and the field's expected shift (mount positions before /
  after through the plate solution's WCS); the lock waits during the slew, finds its pattern
  nearest that shift and re-locks there (holds the new position), the star markers move by it.
- Mount panel: the target field takes a star's name as well as RA Dec: a star named on the image
  by the last plate solve (e.g. `UCAC4 471-000881`), or the stack panel's target; no network
  needed. Before, a name gave "could not convert string to float" and Goto / Center did nothing.
- Camera: a status-bar indicator while a plate solve runs ("plate solving 37 s", amber). The
  local solve near the expected position gets 120 s (was 30 s: a Pi busy with the camera needed
  64 s and was stopped just as it solved, then fell back to the slow whole-sky search).
- Offline in the field: SIMBAD and the VO catalogues (GAVO / ESA) are checked first (a TCP
  connection within 2 s, kept for a minute); offline they are skipped at once instead of each
  lookup waiting 20 s (the camera's solve "did not complete" for minutes after solving).
  Cancelling a local plate solve (or its timeout) now also ends the astrometry-engine it started
  (it kept a core busy until its CPU limit; three at once loaded the Pi to 7.8).
- Mount panel: Solve & sync / Center waited for ever ("sync: stack") when the camera's automatic
  solve had solved the new stack first; it now takes that solution. A stack or solve step that
  takes longer than 10 min ends the job. The star lock says why it cannot guide (the Mount
  panel busy, not connected, or no guide pulses). `Mount.guide_rate` reads the AM5's single
  `GUIDE_RATE` element.
- Star lock: hot pixels (an IMX265 has hundreds, often several per box) are filtered out (3 x 3
  median) when finding and measuring the lock stars; the stars' width is taken from the bright
  candidates only. Before, clusters of hot pixels set the "typical width", the real stars were
  refused as too narrow, and hot-pixel clusters were locked on (hold at once).
- Camera plate solve: the hint is where the mount points when the Mount panel is connected (it
  was the field remembered from the last night, so Solve & sync searched the wrong part of the
  sky until the CPU limit). Hot pixels are removed from the stack before solving (with the mount
  tracking they stack in; 624 on an IMX265 stack). A local solve near the hint gives up after
  30 s and then solves the whole sky (a stale hint: 65 s instead of 187 s; a good one: 2 s).
- CAPTURE.md: the Pi 5's compute reserve at 50 fps (recording + the lock's work: no frame lost,
  5 % of a core).

### Changed
- Camera: the menu **Mount** is now **INDI Telescope Controls** (Mount, Focuser and Star lock panels, INDI
  settings); the settings window is titled "INDI settings".

### Fixed
- **Windows setup without a release that has the Windows files** (v1.0.0 predates them): it takes the
  newest release that carries a wheel, else installs the current source from GitHub (with a note)
  instead of failing; an explicit `-Version` older than the setup says so plainly.
- **SIMBAD at CDS down (every request 404, Nov 2026):** look-ups now fall back to SIMBAD's CfA mirror
  (`simbad.harvard.edu`); the server that answered is asked first from then on (`identify._simbad_ask`).
- **Star box without a local catalogue** (Windows): UCAC4 / TYC / Gaia DR3 names are resolved at the
  VO services (GAVO / ESA, as the camera's target) before SIMBAD, which lists few UCAC4 stars.
- **Analyzer status lines stay one line:** a multi-line message (astroquery's SIMBAD error) grew upwards
  over the sliders; lines are joined, very long text is shortened, and the full text goes to the
  console (on Windows the launcher's log).
- The player and the camera now start on Windows (tested on Windows Server 2025):
  - the ZWO module called `os.uname()` at import; it looks for `ASICamera2.dll` there now, and its
    messages name the Windows DLL;
  - the GPS, v4l2 and VEYE modules imported the Unix-only `fcntl`;
  - the v4l2 / VEYE modules asserted Linux kernel struct sizes at import (a C `long` is 4 bytes on
    Windows).

  `config init` writes the template as UTF-8. Ending a local solver's process used Unix-only
  calls (`SIGKILL`, process groups) and failed on Windows even after a finished solve.
- Windows setup: running PyStarLight programs are found before an update or uninstall (Windows
  locks their files). The user is asked to close them (or types STOP); `-Force` ends them. A
  native program's error output no longer aborts the script (PowerShell 5.1), and leftovers are
  reported.

## [1.0.0] - 2026-11-15

### Added
- Camera and player: own version numbers (`capture/version.py`, 1.0.0 each) in the window title
  and `--version`; RAVF `RECORDER-SOFTWARE-VERSION` is the camera's, PyStarLight's added in
  brackets. The analyze window title shows PyStarLight's version.
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
