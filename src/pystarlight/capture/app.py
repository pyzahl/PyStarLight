"""Live viewer and recorder for Aravis (GenICam: FLIR/Teledyne Grasshopper, Basler etc.)
cameras and VEYE MV-MIPI cameras on the Raspberry Pi 5 CSI ports, with GPS-timed triggering
(u-blox timepulse -> camera trigger input). Recordings are RAVF (default) or SER files with a
UTC time stamp per frame.

Modules:
  cameras.py      backends (Aravis, VEYE via veye_mvcam.py) and CameraSession
  decode.py       pixel formats, unpacking
  timing.py       GPS pulse stamping, exposure-start calibration
  acquisition.py  Grabber and Controller threads
  recorder.py     Recorder thread, repair of interrupted files
  ravf_writer.py, ser_writer.py   file formats
  gps_ublox.py    u-blox GPS (status, timepulse rate), compass
  gui.py          Qt GUI
  app.py          this: command line, wiring, shutdown

Threads:
  - Grabber:    pops buffers, stamps them, hands every frame to the recorder while recording
  - Controller: applies camera settings in the background
  - Recorder:   writes the recording; GPS: reads the GPS serial port
  - Main:       Qt GUI only
"""
import argparse
import os
import signal
import socket

from . import gps_ublox, ravf_writer
from .acquisition import Controller, Grabber
from .cameras import CameraSession, find_cameras, open_camera, pixel_um, select_camera
from .decode import format_bits
from .recorder import RECORD_FORMATS, Recorder
from .recorder import main as repair_main
from .stacker import LiveStacker
from .timing import GPS_RATES, VEYE_CALIBRATED_BITS, veye_line_ns

parser = argparse.ArgumentParser(description="Aravis / VEYE MIPI live view and recorder")
parser.add_argument("--camera", default=None,
                    help="which camera to open: any part of its ID, vendor, model or serial "
                         "(e.g. Basler, a2A1920, Grasshopper, 2316xxxx, VEYE, IMX178). "
                         "Default: first found")
parser.add_argument("--list", action="store_true", help="list connected cameras and exit")
parser.add_argument("--repair", nargs="+", metavar="FILE",
                    help="finish recordings interrupted by a crash (.ravf / .ser) and exit")
parser.add_argument("--bits", choices=["12", "10", "8"], default="12",
                    help="pixel depth (default 12); 10 and 8 bit read out faster (IMX178 full "
                         "frame: 33.2 / 39.2 / 40.1 fps); also selectable in the GUI")
parser.add_argument("--format", default=None,
                    help="explicit GenICam PixelFormat (e.g. Mono12p, Mono16, Mono8), or for VEYE "
                         "cameras GREY, Y10, Y12 (unpacked by the Pi) or Y10P, Y12P (packed); "
                         "overrides --bits")
parser.add_argument("--exposure", type=float, default=None,
                    help="initial exposure time in ms (manual; auto-exposure is turned off)")
parser.add_argument("--gain", type=float, default=None,
                    help="initial gain in dB (manual; auto-gain is turned off)")
parser.add_argument("--max-exposure", type=float, default=2000.0,
                    help="upper end of the exposure slider in ms (default 2000)")
parser.add_argument("--scale", type=float, default=0.5,
                    help="initial window size as a fraction of the image (default 0.5); "
                         "the view follows the window, data is always full resolution")
parser.add_argument("--gamma", type=float, default=1.0,
                    help="initial display gamma (>1 brightens faint detail; display only)")
parser.add_argument("--fps", type=float, default=None,
                    help="fixed camera frame rate (default: free-running, as fast as exposure allows); "
                         "exposure is then limited to 1/fps")
parser.add_argument("--usb-mode", choices=["sync", "async"], default=None,
                    help="Aravis USB3 transfer mode (default: Aravis' own default)")
parser.add_argument("--bandwidth", type=float, default=None,
                    help="limit camera USB throughput in MB/s (e.g. 200)")
parser.add_argument("--buffers", type=int, default=16,
                    help="number of stream buffers (default 16)")
parser.add_argument("--trigger", choices=["off", "gps", "hw", "sw"], default="off",
                    help="start mode: off: free-running (default); gps: GPS timepulse on the "
                         "trigger input, frames stamped with the pulse time; hw: one frame per "
                         "edge on the trigger input (Line0); sw: one frame per 't' key press. "
                         "The GUI's GPS checkbox switches between gps and the others")
parser.add_argument("--gps-rate", choices=GPS_RATES, default="1",
                    help="GPS trigger frame rate in fps (default 1)")
parser.add_argument("--pps", dest="gps_rate_alias", choices=GPS_RATES, nargs="?", const="1",
                    default=None, help=argparse.SUPPRESS)   # = --trigger gps --gps-rate RATE
parser.add_argument("--gps-port", default="/dev/ttyAMA0",
                    help="u-blox GPS serial port (default /dev/ttyAMA0; 'none': no GPS - the "
                         "trigger pulses then must come at the selected rate by themselves)")
parser.add_argument("--gps-baud", type=int, default=9600, help="GPS baud rate (default 9600)")
parser.add_argument("--trigger-edge", choices=["rising", "falling"], default="rising",
                    help="active edge of the hardware trigger (default rising)")
parser.add_argument("--trigger-delay", type=float, default=None,
                    help="delay from trigger edge to exposure start, in the camera's units (us)")
parser.add_argument("--y-trigger", type=int, default=None, metavar="ROW",
                    help="VEYE, GPS trigger: set the trigger delay so the timepulse (the GPS LED "
                         "switching on) falls on this image row; follows exposure and ROI changes")
parser.add_argument("--roi", default=None,
                    help="sensor region x,y,w,h (default: full sensor); smaller = faster "
                         "(and needed for GPS rates above ~10 fps on a full IMX178)")
parser.add_argument("--record-dir", default=os.path.expanduser("~/data"),
                    help="where recordings go (default ~/data)")
parser.add_argument("--record-format", choices=RECORD_FORMATS, default="ravf",
                    help="ravf (default; as ASTRID writes, survives a crash) or ser")
parser.add_argument("--record-start", default=None,
                    help="arm recording to start at this UTC time: HH:MM[:SS] (next occurrence) "
                         "or YYYY-MM-DDTHH:MM:SS")
parser.add_argument("--record-duration", type=float, default=None,
                    help="stop recording after this many seconds")
parser.add_argument("--record-frames", type=int, default=None,
                    help="stop recording after this many frames")
parser.add_argument("--stack-frames", type=int, default=16,
                    help="frames per live stack (default 16; GUI 'Stack & plate solve', key s)")
parser.add_argument("--focal-mm", type=float, default=0.0,
                    help="focal length of the lens / telescope in mm, for the plate-scale hint "
                         "of the solve (default 0: blind solve)")
parser.add_argument("--pixel-um", type=float, default=None,
                    help="pixel pitch in um (default: from the sensor, e.g. IMX178 2.4)")
parser.add_argument("--observer", default="", help="observer name for the file header")
parser.add_argument("--telescope", default="", help="telescope for the file header")


def hardware_name():
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\0\n ")
    except OSError:
        return socket.gethostname()


def gps_frame_info(gps):
    """Per-frame GPS fields of a GPS-stamped frame (RAVF: satellites, fix, almanac)."""
    if gps is None or not gps.has_fix():
        return {"satellites": gps.sats_used if gps else 0, "fix": ravf_writer.FIX_NONE,
                "almanac": 1}
    # NMEA has no leap-second status; a valid RMC fix means the receiver's UTC is in use
    return {"satellites": gps.sats_used, "fix": ravf_writer.FIX_TIME_POSITION, "almanac": 0}


def main(argv=None):
    args = parser.parse_args(argv)
    if args.repair:
        repair_main(args.repair)
        return
    if args.gps_rate_alias:
        args.trigger, args.gps_rate = "gps", args.gps_rate_alias

    cams = find_cameras()
    for i, c in enumerate(cams):
        print(f"[{i}] {c['vendor']} {c['model']}  serial {c['serial']}  id '{c['id']}'")
    if args.list:
        return
    cam_info = select_camera(cams, args.camera)

    session = gps = None
    try:
        camera = open_camera(cam_info)
        print(f"Connected to: {camera.get_model_name()} ({camera.get_vendor_name()})")
        session = s = CameraSession(camera, args.buffers, args.trigger_edge, args.trigger_delay)

        # ---- GPS ----
        if args.gps_port.lower() != "none":
            try:
                gps = gps_ublox.Gps(args.gps_port, args.gps_baud)
                print(f"GPS on {args.gps_port}")
            except Exception as e:
                print(f"No GPS ({e})")
        s.gps = gps
        s.compass = gps_ublox.Compass.find()
        if s.compass is not None:
            print(f"Compass: {s.compass.kind}")

        # ---- camera settings, trigger, stream ----
        roi = tuple(int(v) for v in args.roi.split(",")) if args.roi else None
        s.configure(args.format, args.bits, roi, args.fps, args.exposure, args.gain,
                    args.max_exposure)
        s.apply_trigger("free" if args.trigger == "off" else args.trigger)
        if camera.kind == "aravis":
            camera.set_usb(args.usb_mode, args.bandwidth)   # before the stream is created

        s.controller = Controller(camera, s.apply_trigger, s.reconfigure)
        s.controller.start()

        def header_info():
            """Metadata for a new recording (taken when it starts)."""
            gps_stamped = s.grabber.stamper is not None
            bits = format_bits(s.pixel_format)
            info = {
                "instrument": camera.get_model_name(),
                "vendor": camera.get_vendor_name(),
                "serial": cam_info.get("serial", ""),
                "sensor": camera.get_model_name().split("-")[-1] if s.is_veye else "",
                "pixel_format": s.pixel_format,
                "hardware": hardware_name(),
                "gain_db": s.controller.gain_db,
                "exposure_ns": s.controller.exposure_us * 1000,
                "roi_x": s.region[0], "roi_y": s.region[1],
                "trigger": f"GPS timepulse {s.grabber.stamper.rate} Hz" if gps_stamped
                           else "free run / external",
                "trigger_delay_ns": s.trigger_delay_ns() if gps_stamped else 0,
                "timestamp_source": "GPS trigger pulse" if gps_stamped else "Pi clock (NTP)",
                # measured on the IMX178 at 12 bit to ~0.1 ms; NTP ~1 ms; else unknown
                "timing_accuracy_ns": (100_000 if gps_stamped and s.is_veye
                                       and bits in VEYE_CALIBRATED_BITS else
                                       5_000_000 if gps_stamped else 2_000_000),
                "row_time_ns": veye_line_ns(bits) if s.is_veye else 0,
            }
            if gps is not None and gps.lat is not None:
                info.update(latitude=gps.lat, longitude=gps.lon, altitude=gps.alt or 0.0)
            return info

        s.recorder = Recorder(
            s.decode_raw, lambda: (s.width, s.height, s.decode.depth),     # follow a new ROI
            args.record_dir, name=camera.get_model_name().replace(" ", "_"),
            fmt=args.record_format, header_info=header_info)
        s.recorder.text.update(observer=args.observer, telescope=args.telescope)
        s.grabber = Grabber(None, camera.success, s.recorder)
        s.grabber.exposure_start = s.exposure_start

        def frame_info():
            info = {"exposure_ns": int(s.controller.exposure_us * 1000)}
            if s.grabber.stamper is not None:
                info.update(gps_frame_info(gps))
            return info

        s.grabber.frame_info = frame_info

        s.pixel_um = args.pixel_um or pixel_um(camera.get_model_name())
        s.focal_mm = args.focal_mm              # the GUI changes it

        def stack_info():
            """FITS header values for a new stack (taken when it starts)."""
            info = {"instrument": f"{camera.get_vendor_name()} {camera.get_model_name()}",
                    "depth": s.decode.depth, "gain_db": s.controller.gain_db,
                    "roi_x": s.region[0], "roi_y": s.region[1],
                    "pixel_um": s.pixel_um, "focal_mm": s.focal_mm}
            if gps is not None and gps.has_fix() and gps.lat is not None:
                info.update(latitude=gps.lat, longitude=gps.lon, altitude=gps.alt or 0.0)
            return info

        s.stacker = LiveStacker(s.decode_raw, args.record_dir,
                                camera.get_model_name().replace(" ", "_"), stack_info)
        s.grabber.stacker = s.stacker

        # the GUI's imports (Qt: ~1 s) before streaming, so they don't starve the grabber
        from .gui import MainWindow
        from .qt import API, QtCore, QtWidgets, exec_app, fix_cv2_qt_env
        fix_cv2_qt_env()
        s.grabber.stream = s.create_stream()
        s.grabber.start()                   # before the frames come, as the buffers are few
        s.start_acquisition()
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        print(f"Qt: {API}")
        # Ctrl+C in the terminal; posted, so that it also works before exec_() runs
        signal.signal(signal.SIGINT, lambda *_: QtCore.QTimer.singleShot(0, app.quit))
        win = MainWindow(s, args)
        if args.record_start:
            win.on_record_button()          # armed from the command line
        win.show()
        print("Streaming. Keys: e/E exposure, g/G gain, d/D gamma, r record, "
              + ("t trigger, " if args.trigger == "sw" else "") + "q quit.")
        exec_app(app)

    finally:
        print("\nShutting down stream safely...")
        if session is not None:
            if session.grabber is not None and session.grabber.is_alive():
                session.grabber.running = False
                session.grabber.join(timeout=2)
            if session.recorder is not None:
                session.recorder.close()    # finishes the file (RAVF index, SER trailer)
            if session.stacker is not None:
                session.stacker.close()
            if session.controller is not None and session.controller.is_alive():
                session.controller.stop()
                session.controller.join(timeout=2)
            session.stop()
        if gps is not None:
            gps.close()                     # back to the GPS' default 1 Hz PPS


if __name__ == "__main__":
    main()
