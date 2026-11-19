"""A focuser through INDI (capture/indi.py): position, steps in and out, go to, abort, temperature.

NEW CODE, standard library only. INDI focusers come in three kinds: absolute (ABS_FOCUS_POSITION:
ZWO EAF, most stepper focusers), relative only (REL_FOCUS_POSITION: steps, no position read back)
and timer (FOCUS_TIMER: DC motors, run for ms). An absolute focuser is moved by absolute targets
(also the steps: position + n, kept within 0 .. FOCUS_MAX); a relative one by FOCUS_MOTION, then
the steps (the driver takes the direction set when the steps arrive); its position is counted
here from the steps sent ("moved" since connect).
"""
from .indi import FOCUSER, TELESCOPE

ABS, REL, TIMER = "ABS_FOCUS_POSITION", "REL_FOCUS_POSITION", "FOCUS_TIMER"


def find(client, timeout=5.0):
    """The focuser among the INDI devices: one with the FOCUSER interface, preferably not also a
    mount (some mount drivers drive a focuser too); None."""
    if client.find_device(FOCUSER, timeout) is None:
        return None
    devices = [d for d in client.devices() if client.interfaces(d) & FOCUSER]
    return next((d for d in devices if not client.interfaces(d) & TELESCOPE), devices[0])


class Focuser:
    def __init__(self, client, device=None, timeout=10.0):
        self.client = client
        self.device = device or find(client, timeout)
        if self.device is None:
            raise RuntimeError("no focuser among the INDI devices")
        self.moved = 0                  # steps sent since connect (relative focusers)

    def connect(self, timeout=20.0):
        if not self.client.connect_device(self.device, timeout):
            raise RuntimeError(f"{self.device}: could not connect")
        c = self.client
        c.wait(lambda: any(c.get(self.device, n) is not None for n in (ABS, REL, TIMER)), timeout)
        return self

    def _has(self, name):
        return self.client.get(self.device, name) is not None

    def kind(self):
        """'absolute', 'relative', 'timer' or None (what the driver offers)."""
        return ("absolute" if self._has(ABS) else "relative" if self._has(REL) else
                "timer" if self._has(TIMER) else None)

    def position(self):
        v = self.client.number(self.device, ABS)
        return v.get("FOCUS_ABSOLUTE_POSITION") if v else None

    def maximum(self):
        v = self.client.number(self.device, "FOCUS_MAX")
        return v.get("FOCUS_MAX_VALUE") if v else None

    def temperature(self):
        v = self.client.number(self.device, "FOCUS_TEMPERATURE")
        t = v.get("TEMPERATURE") if v else None
        return t if t is not None and t == t and t > -100 else None     # EAF without probe: -273

    def moving(self):
        return any(self.client.state(self.device, n) == "Busy" for n in (ABS, REL, TIMER))

    def alert(self):
        return any(self.client.state(self.device, n) == "Alert" for n in (ABS, REL, TIMER))

    def _direction(self, outward):
        if self._has("FOCUS_MOTION"):
            self.client.set_switch(self.device, "FOCUS_MOTION",
                                   {"FOCUS_OUTWARD" if outward else "FOCUS_INWARD": True})

    def move_to(self, position):
        """Go to an absolute position (absolute focusers; clipped to 0 .. FOCUS_MAX)."""
        if not self._has(ABS):
            raise RuntimeError(f"{self.device} has no absolute position")
        top = self.maximum()
        position = max(0, round(position))
        if top:
            position = min(position, int(top))
        self.client.set_number(self.device, ABS, {"FOCUS_ABSOLUTE_POSITION": position})
        return position

    def move_by(self, steps):
        """Out (steps > 0) or in (< 0) by that many steps; a timer focuser: that many ms."""
        steps = int(round(steps))
        if not steps:
            return
        if self._has(ABS):
            pos = self.position()
            if pos is not None:
                self.move_to(pos + steps)
                return
        if self._has(REL):
            self._direction(steps > 0)
            self.client.set_number(self.device, REL, {"FOCUS_RELATIVE_POSITION": abs(steps)})
            self.moved += steps
        elif self._has(TIMER):
            self._direction(steps > 0)
            self.client.set_number(self.device, TIMER, {"FOCUS_TIMER_VALUE": abs(steps)})
        else:
            raise RuntimeError(f"{self.device}: no way to move it (no position, steps or timer)")

    def abort(self):
        if self._has("FOCUS_ABORT_MOTION"):
            self.client.set_switch(self.device, "FOCUS_ABORT_MOTION", {"ABORT": True})

    def status(self):
        """Everything the GUI shows, in one dict."""
        return {"device": self.device, "kind": self.kind(), "position": self.position(),
                "max": self.maximum(), "moved": self.moved, "moving": self.moving(),
                "alert": self.alert(), "temperature": self.temperature()}
