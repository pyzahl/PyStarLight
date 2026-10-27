"""Background threads between camera and GUI.

  - Grabber:    pops buffers from the stream and keeps only the newest one, undecoded
                (older ones are recycled straight back to the camera); stamps and hands
                every frame to the recorder while recording.
  - Controller: applies exposure/gain/trigger changes to the camera in the background, so
                the GUI never blocks on slow register access or stream restarts.
"""
import collections
import threading
import time

from .cameras import try_call


class Grabber(threading.Thread):
    """Pops frames from the stream and holds on to only the newest *undecoded* buffer.

    No pixel work happens here: at 160 fps, decoding every frame in this thread would
    hog Python's GIL and starve the GUI. The main thread decodes only what it displays.
    """

    def __init__(self, stream, success, recorder=None):
        super().__init__(daemon=True)
        self.stream = stream
        self.recorder = recorder    # gets a raw copy of every frame while recording
        self.stacker = None         # LiveStacker: gets a raw copy of a frame when it is free
        # Count frames missing from gaps in the frame timing too: the Pi's CSI-2 receiver
        # drops frames without a sequence gap when no buffer is free. frame_period_ns():
        # expected spacing when free-running (None: don't check, e.g. triggered).
        self.frame_period_ns = None
        # PulseStamper while GPS-triggered (None: free run etc.); the recorder then starts
        # and stops by pulse time
        self.stamper = None
        # exposure_start(frame_ns, pulse_ns or None) -> row-0 exposure start (UTC ns), the
        # time stamp written to the recording
        self.exposure_start = lambda frame_ns, pulse_ns: frame_ns
        # frame_info() -> dict of per-frame values for the recording (exposure, GPS status)
        self.frame_info = dict
        self.exp_ns = None          # exposure start of the newest frame
        self.pulse_ns = None        # its trigger pulse (GPS)
        self.success = success      # buffer status of a complete frame for this backend
        self.lock = threading.Lock()
        self.latest = None          # (buffer, frame_id, stamp_ns) not yet taken by the GUI
        self.dropped = 0            # frame-ID gaps (frames lost before reaching us)
        self.missed = 0             # GPS pulses without a frame
        self.bad = {}               # non-SUCCESS buffer statuses -> count
        self.cam_fps = 0.0
        self.intervals = collections.deque(maxlen=200)  # frame-start spacing (ns), recent frames
        self.running = True
        self.last_ok = time.monotonic()
        self.paused = False         # pause(): the stream is about to be replaced
        self.parked = False         # ... and this thread has let go of it
        self.generation = 0         # +1 per new stream (frame IDs and timing restart)

    def pause(self):
        """Stop using the stream (before it is replaced); returns once this thread is idle."""
        self.paused = True
        t = time.monotonic() + 2.0
        while not self.parked and time.monotonic() < t:
            time.sleep(0.005)

    def resume(self, stream):
        self.stream = stream
        self.generation += 1
        self.paused = False

    def run(self):
        last_id = None
        last_ns, last_starts = None, None
        last_stamper, last_pulse = None, None
        last_gen = self.generation
        t0, n = time.monotonic(), 0
        while self.running:
            if self.paused:
                self.parked = True
                time.sleep(0.01)
                continue
            self.parked = False
            if self.generation != last_gen:     # new stream: nothing to compare with
                last_gen = self.generation
                last_id = last_ns = last_starts = last_stamper = last_pulse = None
                self.intervals.clear()
            buf = self.stream.timeout_pop_buffer(200_000)  # short timeout -> quick shutdown
            dt = time.monotonic() - t0
            if dt >= 1.0:
                self.cam_fps, t0, n = n / dt, time.monotonic(), 0
            if buf is None:
                continue
            status = buf.get_status()
            if status != self.success:
                name = getattr(status, "value_nick", str(status))
                self.bad[name] = self.bad.get(name, 0) + 1
                if self.bad[name] in (1, 10, 100) or self.bad[name] % 1000 == 0:
                    print(f"Frame not complete: status '{name}' x{self.bad[name]}")
                self.stream.push_buffer(buf)
                continue
            self.last_ok = time.monotonic()
            fid = buf.get_frame_id()
            if last_id is not None and fid > last_id + 1:
                self.dropped += fid - last_id - 1
            last_id = fid
            frame_ns = buf.get_system_timestamp()
            starts = getattr(self.stream, "starts", 0)
            if self.frame_period_ns is not None:
                if last_ns is not None and starts == last_starts:   # not across a restart
                    period = self.frame_period_ns()
                    dt = frame_ns - last_ns
                    if dt > 1.5 * period:
                        self.dropped += round(dt / period) - 1
            if last_ns is not None and starts == last_starts:
                self.intervals.append(frame_ns - last_ns)
            elif starts != last_starts:
                self.intervals.clear()          # restarted: old spacing no longer applies
            last_ns, last_starts = frame_ns, starts
            stamper = self.stamper
            pulse = None
            if stamper is not None:
                pulse = stamper(frame_ns)
                if stamper is last_stamper and last_pulse is not None:
                    gap = pulse - last_pulse
                    if gap > (stamper.stride + 0.5) * stamper.period_ns:
                        self.missed += round(gap / stamper.period_ns) - stamper.stride
                last_stamper, last_pulse = stamper, pulse
            exp_ns = self.exposure_start(frame_ns, pulse)
            self.exp_ns, self.pulse_ns = exp_ns, pulse
            sel_ns = exp_ns if pulse is None else pulse     # what start/stop times refer to
            if self.recorder is not None:
                if self.recorder.wants(sel_ns):
                    self.recorder.put(buf.get_data(), fid, sel_ns, exp_ns, self.frame_info())
            stacker = self.stacker
            if stacker is not None and stacker.wants():
                stacker.put(buf.get_data(), exp_ns, self.frame_info())
            with self.lock:
                old, self.latest = self.latest, (buf, fid, exp_ns)
            if old is not None:                 # GUI didn't want it in time -> recycle
                self.stream.push_buffer(old[0])
            n += 1

    def take(self):
        """Hand the newest buffer to the caller, who must push it back to the stream."""
        with self.lock:
            item, self.latest = self.latest, None
        return item


class Controller(threading.Thread):
    """Applies camera settings off the GUI thread; only the newest request per key is sent."""

    def __init__(self, camera, apply_trigger, reconfigure):
        super().__init__(daemon=True)
        self.camera = camera
        self.apply_trigger = apply_trigger  # mode -> configures the camera (not acquiring)
        # reconfigure(region=(x, y, w, h) or None, pixel_format=name or None): rebuilds the stream
        self.reconfigure = reconfigure
        self.reconfigs = 0                  # +1 per rebuilt stream (the GUI follows)
        self.cond = threading.Condition()
        self.pending = {}
        self.running = True
        self.restarts = 0
        self.busy = False
        # cached values actually applied by the camera (read back after each change)
        self.exposure_us = camera.get_exposure_time()
        self.gain_db = camera.get_gain()

    def request(self, key, value):
        with self.cond:
            if key == "reconfigure" and key in self.pending:    # region and format both
                value = {**self.pending[key], **value}
            self.pending[key] = value
            self.cond.notify()

    def run(self):
        while self.running:
            with self.cond:
                while not self.pending and self.running:
                    self.cond.wait(0.5)
                todo, self.pending = self.pending, {}
                self.busy = bool(todo)
            try:
                if "reconfigure" in todo:
                    self.reconfigure(**todo["reconfigure"])
                    self.reconfigs += 1
                if "trigger" in todo:
                    print(f"Trigger mode: {todo['trigger']}")
                    self.camera.stop_acquisition()
                    self.apply_trigger(todo["trigger"])
                    self.camera.start_acquisition()
                if "delay" in todo:
                    # the camera ignores the delay register while acquiring
                    self.camera.stop_acquisition()
                    us = todo["delay"]
                    try_call(lambda us=us: self.camera.set_float("TriggerDelay", us))
                    self.camera.start_acquisition()
                if "exposure" in todo:
                    self.camera.set_exposure_time(todo["exposure"])
                    self.exposure_us = self.camera.get_exposure_time()
                if "gain" in todo:
                    self.camera.set_gain(todo["gain"])
                    self.gain_db = self.camera.get_gain()
                if "restart" in todo:
                    print("Restarting acquisition...")
                    self.camera.stop_acquisition()
                    self.camera.start_acquisition()
                    self.restarts += 1
            except Exception as e:
                print(f"Camera setting failed: {e}")
            self.busy = False

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify()
