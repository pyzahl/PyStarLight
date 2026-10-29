"""Camera backends behind one interface, and the camera session built on them.

Backends:
  - AravisCamera: GenICam USB3 / GigE cameras (FLIR/Teledyne Grasshopper, Basler etc.) through
    Aravis; needs the system `gi` with Aravis 0.8, imported only when an Aravis camera is used.
  - veye_mvcam.Camera: VEYE MV-MIPI cameras on the Raspberry Pi 5 CSI ports.

Both offer the subset of Aravis.Camera that the capture app uses, plus:
  kind               "aravis" / "veye"
  success            buffer status of a complete frame
  create_stream(n)   a stream with n buffers queued
  close_stream(s)    release a stream before a new one is made
  set_manual()       auto exposure and auto gain off
  frame_period_ns()  the current frame period (VEYE; None: unknown)

CameraSession holds the open camera with its stream, region, pixel format and trigger
settings; the app attaches its threads and the GPS to it, and the GUI works from it.
"""
import threading

from . import veye_mvcam
from .decode import FORMATS_BY_BITS, Decoder
from .timing import IMX178, exposure_start, sensor_timing, trigger_delay_ns

# Smallest region the Pi's CSI-2 pipeline streams from a VEYE IMX178 (Y12): lower ones fail at
# STREAMON with EPIPE (measured; height 316 fails, 320 works). The sensor always reads full-width
# rows (veye_mvcam crops x in software), so any width works (8 measured; 368 failed before that).
# Other VEYE models: not measured, the same limit assumed
VEYE_MIN_ROI = (8, 320)
# pixel pitch (um) by sensor, for the plate-scale hint of a stack (--pixel-um overrides)
PIXEL_UM = {"IMX178": 2.4, "IMX296": 3.45, "IMX264": 3.45, "IMX265": 3.45, "IMX287": 6.9,
            "IMX462": 2.9, "AR0234": 3.0}


def pixel_um(model):
    """Pixel pitch of a camera from the sensor name in its model string (None: unknown)."""
    return next((um for sensor, um in PIXEL_UM.items() if sensor in model.upper()), None)


def try_call(fn):
    """Call an optional camera feature, ignoring it if the camera doesn't have it."""
    try:
        fn()
        return True
    except Exception as e:
        print(f"  (skipped: {e})")
        return False


def _aravis():
    import gi
    gi.require_version('Aravis', '0.8')
    from gi.repository import Aravis
    return Aravis


# ---------------------------------------------------------------------------
# Finding and opening cameras
# ---------------------------------------------------------------------------
def find_cameras():
    """All cameras: dicts with id, vendor, model, serial (VEYE: also backend 'veye' etc.)."""
    cams = []
    try:
        Aravis = _aravis()
    except (ImportError, ValueError) as e:
        print(f"Aravis not available ({e}); looking for VEYE cameras only")
    else:
        Aravis.update_device_list()
        for i in range(Aravis.get_n_devices()):
            cams.append({
                "id": Aravis.get_device_id(i),
                "vendor": Aravis.get_device_vendor(i) or "",
                "model": Aravis.get_device_model(i) or "",
                "serial": Aravis.get_device_serial_nbr(i) or "",
                "backend": "aravis",
            })
    return cams + veye_mvcam.find_cameras()


def select_camera(cams, key=None):
    """The camera whose id, vendor, model or serial contains `key` (None: the first)."""
    if not cams:
        raise SystemExit("No camera found.")
    if not key:
        if len(cams) > 1:
            print("Several cameras found; opening the first. Use --camera to choose.")
        return cams[0]
    key = key.lower()
    matches = [c for c in cams
               if any(key in c[f].lower() for f in ("id", "vendor", "model", "serial"))]
    if not matches:
        raise SystemExit(f"No camera matches '{key}'.")
    if len(matches) > 1:
        raise SystemExit(f"'{key}' matches {len(matches)} cameras; be more specific "
                         f"(e.g. use the serial number).")
    return matches[0]


def open_camera(info):
    return veye_mvcam.Camera(info) if info.get("backend") == "veye" else AravisCamera(info)


class AravisCamera:
    """An Aravis.Camera (all its methods pass through) with the backend extras."""

    kind = "aravis"

    def __init__(self, info):
        self._Aravis = _aravis()
        self._cam = self._Aravis.Camera.new(info["id"])
        self.success = self._Aravis.BufferStatus.SUCCESS

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._cam, name)

    def set_manual(self):
        if self._cam.is_exposure_auto_available():
            self._cam.set_exposure_time_auto(self._Aravis.Auto.OFF)
        if self._cam.is_gain_auto_available():
            self._cam.set_gain_auto(self._Aravis.Auto.OFF)

    def set_usb(self, mode=None, bandwidth_mbs=None):
        """USB3 transfer mode ('sync' / 'async') and throughput limit; before create_stream."""
        if not self._cam.is_uv_device():
            return
        if mode:
            print(f"USB mode: {mode}")
            m = self._Aravis.UvUsbMode.SYNC if mode == "sync" else self._Aravis.UvUsbMode.ASYNC
            try_call(lambda: self._cam.uv_set_usb_mode(m))
        if bandwidth_mbs:
            print(f"USB bandwidth limit: {bandwidth_mbs} MB/s")
            try_call(lambda: self._cam.uv_set_bandwidth(int(bandwidth_mbs * 1e6)))

    def create_stream(self, n_buffers):
        stream = self._cam.create_stream(None, None)
        payload = self._cam.get_payload()
        for _ in range(n_buffers):
            stream.push_buffer(self._Aravis.Buffer.new_allocate(payload))
        return stream

    def close_stream(self, stream):
        pass                                # Aravis frees it with the last reference

    def frame_period_ns(self):
        return None


# ---------------------------------------------------------------------------
# Session: one open camera with its stream and settings
# ---------------------------------------------------------------------------
class CameraSession:
    """The open camera: stream, region, pixel format, decoder, trigger settings.

    The app sets controller, grabber, recorder, stacker, gps and compass on it; the GUI reads all of
    them from here. width / height / region / pixel_format / decode follow reconfigure().
    """

    def __init__(self, camera, n_buffers=16, trigger_edge="rising", trigger_delay_us=None):
        self.camera = camera
        self.is_veye = camera.kind == "veye"
        # how the camera times its exposures (VEYE: by sensor; others: not modelled)
        self.timing = sensor_timing(camera.get_model_name()) if self.is_veye else IMX178
        self.n_buffers = n_buffers
        self.trigger_edge = trigger_edge
        self.trigger_delay_us = trigger_delay_us    # camera units (µs); the GUI changes it
        self.sensor = tuple(camera.get_sensor_size())
        self.available = list(camera.dup_available_pixel_formats_as_strings())
        self.stream = None
        self.stream_lock = threading.Lock()         # held while the stream is replaced
        self.acquiring = False
        self.controller = self.grabber = self.recorder = self.stacker = None
        self.gps = self.compass = None
        self.pixel_um = pixel_um(camera.get_model_name())   # um, for the plate-scale hint
        self.focal_mm = 0.0                                  # lens / telescope (0: unknown)
        self._refresh()

    def _refresh(self):
        self.region = tuple(self.camera.get_region())
        self.width, self.height = self.region[2:]
        self.pixel_format = self.camera.get_pixel_format_as_string()
        self.decode = Decoder(self.pixel_format)

    def describe(self):
        c = self.camera
        return f"{c.get_vendor_name()} {c.get_model_name()}"

    # ---- setup (before streaming) ----
    def configure(self, pixel_format=None, bits=12, roi=None, fps=None, exposure_ms=None,
                  gain_db=None, max_exposure_ms=2000.0):
        """Pixel format (explicit, or the preferred one of a bit depth), region (full sensor
        unless roi), no binning, manual exposure and gain, frame rate (fixed or free)."""
        camera = self.camera
        current = camera.get_pixel_format_as_string()
        print(f"Camera pixel formats: {', '.join(self.available)}")
        if pixel_format:
            wanted = [pixel_format]
        else:
            # GREY / Y10 / Y12 (P: packed) are the names a VEYE camera offers
            base = "Bayer" + current[5:7] if current.startswith("Bayer") else "Mono"
            wanted = [f.format(b=base) for f in FORMATS_BY_BITS[int(bits)]]
        chosen = next((f for f in wanted if f in self.available), None)
        if chosen is None:
            print(f"None of {wanted} supported; keeping {current}")
        elif chosen != current:
            camera.set_pixel_format_from_string(chosen)

        sensor_w, sensor_h = self.sensor
        print(f"Sensor: {sensor_w}x{sensor_h}, region before reset: {camera.get_region()}")
        if camera.is_binning_available():
            try_call(lambda: camera.set_binning(1, 1))
        region = roi or (0, 0, sensor_w, sensor_h)
        try_call(lambda: camera.set_region(*region))

        camera.set_manual()
        # FLIR cameras cap exposure by the frame-rate setting; release it so long
        # exposures work (frame rate then follows the exposure).
        if fps:
            # Fixed frame rate (Aravis handles the vendor-specific feature names)
            print(f"Setting frame rate to {fps} fps:")
            try_call(lambda: camera.set_frame_rate(fps))
        else:
            print("Disabling frame-rate limit:")
            try_call(lambda: camera.set_boolean("AcquisitionFrameRateEnable", False)) or \
                try_call(lambda: camera.set_frame_rate_enable(False))

        self.exp_min, exp_max = camera.get_exposure_time_bounds()        # microseconds
        self.exp_max = min(exp_max, max_exposure_ms * 1000.0)
        self.gain_min, self.gain_max = camera.get_gain_bounds()          # dB
        print(f"Exposure range: {self.exp_min / 1000:.3f} .. {self.exp_max / 1000:.1f} ms, "
              f"gain range: {self.gain_min:.1f} .. {self.gain_max:.1f} dB")
        if exposure_ms is not None:
            camera.set_exposure_time(min(max(exposure_ms * 1000.0, self.exp_min), self.exp_max))
        if gain_db is not None:
            camera.set_gain(min(max(gain_db, self.gain_min), self.gain_max))
        self._refresh()
        print(f"Resolution: {self.width}x{self.height}, pixel format: {self.pixel_format}")

    def apply_trigger(self, mode):
        """free / gps / hw / sw; call while not acquiring."""
        camera = self.camera
        if mode == "free":
            try_call(lambda: camera.clear_triggers())
            return
        source = "Software" if mode == "sw" else "Line0"
        print(f"Trigger: {source}" + ("" if mode == "sw" else f", {self.trigger_edge} edge"))
        camera.set_trigger(source)
        if mode != "sw":
            try_call(lambda: camera.set_string("TriggerActivation",
                                               self.trigger_edge.capitalize() + "Edge"))
        # always: the camera keeps a delay from an earlier session, which the time
        # stamps would not know about
        try_call(lambda: camera.set_float("TriggerDelay", self.trigger_delay_us or 0))

    # ---- streaming ----
    def create_stream(self):
        # VEYE: configures the CSI-2 media pipeline and maps the driver's own buffers
        self.stream = self.camera.create_stream(self.n_buffers)
        return self.stream

    def start_acquisition(self):
        self.camera.start_acquisition()
        self.acquiring = True

    def reconfigure(self, region=None, pixel_format=None):
        """New sensor region (x, y, w, h) and/or pixel format: stop, rebuild the stream,
        restart. The GUI locks this while recording, so the recorder is idle and its
        queue empty. Falls back to the old settings if the camera rejects the new ones."""
        camera, grabber = self.camera, self.grabber

        def rebuild(reg, fmt):
            if self.stream is not None:
                camera.stop_acquisition()
                camera.close_stream(self.stream)
            self.stream = grabber.stream = None
            camera.set_pixel_format_from_string(fmt)
            camera.set_region(*reg)
            self.stream = camera.create_stream(self.n_buffers)
            camera.start_acquisition()

        old = self.region, self.pixel_format
        new = region or self.region, pixel_format or self.pixel_format
        with self.stream_lock:
            grabber.pause()
            grabber.take()              # a buffer of the old stream goes with it
            try:
                rebuild(*new)
            except Exception as e:
                print(f"{new[1]} {new[0]} failed ({e}); back to {old[1]} {old[0]}")
                rebuild(*old)
            self._refresh()
            grabber.resume(self.stream)
        print(f"Region: {self.width}x{self.height} at ({self.region[0]}, {self.region[1]}), "
              f"{self.pixel_format}")

    def decode_raw(self, raw):
        """Raw frame bytes (as the grabber copied them) -> image, current region and format."""
        return self.decode.raw(raw, self.width, self.height)

    # ---- timing ----
    def trigger_delay_ns(self):
        return trigger_delay_ns(self.trigger_delay_us, self.is_veye, self.timing)

    def exposure_start(self, frame_ns, pulse_ns):
        """Row-0 exposure start (UTC ns) of a frame, see timing.exposure_start."""
        return exposure_start(frame_ns, pulse_ns, self.controller.exposure_us,
                              self.trigger_delay_us, self.is_veye,
                              self.camera.frame_period_ns() if self.is_veye else None,
                              self.timing)

    def stop(self):
        if self.acquiring:
            self.camera.stop_acquisition()
            self.acquiring = False
        try_call(lambda: self.camera.clear_triggers())  # leave the camera free-running
