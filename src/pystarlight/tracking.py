"""Aperture tracking from frame to frame.

Extracted from PyMovie 4.2.8 (main.py: initializeTracking, processYellowApertures,
centerAllApertures, centerAperture, trackCentroid, positionApertureAtCentroid,
tracking-path methods, calcTheta; aperture.py MeasurementAperture). No GUI state.

Modes, chosen by how many apertures are 'yellow':
  0 yellow  every non-white aperture re-centres on its own star.
  1 yellow  the yellow aperture follows its star; red/green apertures move by the
            same shift (good for faint targets in wind-shake).
  2 yellow  as 1, plus field rotation: other apertures keep their start-of-run
            distance and angle to yellow #1, rotated by the change in the
            yellow #1 -> yellow #2 angle.
A TrackPath (two points, linear in frame number) can drive yellow #1 instead of
its star, for a target that drifts steadily or is invisible.

White apertures never move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .photometry import Measurement, PhotometryConfig, circular_mask, measure_aperture


@dataclass
class Aperture:
    name: str
    x0: int                   # top-left corner, image coordinates
    y0: int
    size: int = 51            # PyMovie 'roi size' (square)
    color: str = 'red'        # red | green | yellow | white
    thresh: float = 99999     # mask threshold above background (99999 = always default mask)
    default_mask_radius: float = 3.2
    method: str = 'aperture'  # 'aperture' | 'tme'
    primary_yellow: bool = False
    role: str = ''            # 'target' | 'reference' | 'check' | 'sky' (global mode; '' = PyMovie colours)
    default_mask: Optional[np.ndarray] = None

    # tracking state
    xc: Optional[float] = None
    yc: Optional[float] = None
    dx: Optional[float] = None
    dy: Optional[float] = None
    theta: Optional[float] = None

    # background smoother (used for the TME hunt-bias value)
    smoothed_background: float = 0.0
    background_count: int = 0

    def __post_init__(self):
        if self.default_mask is None:
            self.default_mask, _ = circular_mask(self.size, self.default_mask_radius)
        if self.name.startswith('TME'):
            self.method = 'tme'

    @classmethod
    def centered_at(cls, name: str, x: int, y: int, size: int = 51, **kw) -> 'Aperture':
        c = int(size / 2)
        return cls(name, x - c, y - c, size, **kw)

    @property
    def center(self) -> tuple[int, int]:
        return self.x0 + int(self.size / 2), self.y0 + int(self.size / 2)

    def clamp(self, width: int, height: int):
        self.x0 = int(min(max(self.x0, 0), width - self.size))
        self.y0 = int(min(max(self.y0, 0), height - self.size))

    def update_background(self, sample: float, smoothing_count: int):
        """Exponential smoother with fast warm-up (MeasurementAperture.addData)."""
        self.background_count += 1
        if smoothing_count:
            tau = min(smoothing_count, self.background_count)
            b1 = np.exp(-1 / tau)
        else:
            b1 = 0.0
        if self.smoothed_background == 0:
            self.smoothed_background = sample
        else:
            self.smoothed_background = (1 - b1) * sample + b1 * self.smoothed_background


@dataclass
class TrackPath:
    """Yellow #1 centre = a * frame + b, from two (x, y, frame) points."""
    early: tuple[int, int, int]
    late: tuple[int, int, int]

    def center(self, frame: int) -> tuple[int, int]:
        (xe, ye, fe), (xl, yl, fl) = self.early, self.late
        if fe == fl:
            raise ValueError('track path points need different frame numbers')
        xa = (xe - xl) / (fe - fl)
        ya = (ye - yl) / (fe - fl)
        return int(round(xa * frame + xe - xa * fe)), int(round(ya * frame + ye - ya * fe))


def angle(dx: float, dy: float) -> float:
    """Angle of (dx, dy) in 0..2pi (PyMovie calcTheta)."""
    return math.atan2(dy, dx) % (2 * math.pi)


class Tracker:
    """Moves the apertures for each new frame and measures them.

    Usage:
        tracker = Tracker(apertures, image_shape)
        tracker.initialize(first_image)
        for frame in frames:
            results = tracker.step(frame.image, frame.index)   # {name: Measurement}
    """

    def __init__(self, apertures: list[Aperture], image_shape: tuple[int, int],
                 cfg: PhotometryConfig = PhotometryConfig(),
                 track_path: Optional[TrackPath] = None,
                 use_yellow_mask: bool = False,
                 defect_frame: Optional[np.ndarray] = None,
                 fields: bool = False,
                 calibration=None):
        self.apertures = apertures
        self.height, self.width = image_shape
        self.cfg = cfg
        self.track_path = track_path
        self.use_yellow_mask = use_yellow_mask
        self.defect_frame = defect_frame
        self.fields = fields
        self.calibration = calibration
        self.yellow_mask: Optional[np.ndarray] = None
        self.yellow_xy = (0.0, 0.0)
        self.delta_theta = 0.0
        yellows = [a for a in apertures if a.color == 'yellow']
        if len(yellows) > 2:
            raise ValueError('at most two yellow apertures')
        if yellows and not any(a.primary_yellow for a in yellows):
            yellows[0].primary_yellow = True
        self.y1 = next((a for a in yellows if a.primary_yellow), None)
        self.y2 = next((a for a in yellows if not a.primary_yellow), None)
        for a in apertures:
            a.clamp(self.width, self.height)

    # ---------------------------------------------------------------- helpers

    def measure(self, image, ap: Aperture, use_mask: bool = True) -> Measurement:
        return measure_aperture(
            image, ap.x0, ap.y0, ap.size, color=ap.color, thresh=ap.thresh, name=ap.name,
            default_mask=ap.default_mask, default_mask_radius=ap.default_mask_radius,
            method=ap.method, smoothed_background=ap.smoothed_background,
            yellow_mask=self.yellow_mask if (use_mask and self.use_yellow_mask) else None,
            defect_frame=self.defect_frame, fields=self.fields, cfg=self.cfg,
            calibration=self.calibration)

    def _jog(self, ap: Aperture, dx: int, dy: int):
        """Move the aperture by (-dx, -dy) (PyMovie jogAperture sign convention)."""
        ap.x0 -= dx
        ap.y0 -= dy
        ap.clamp(self.width, self.height)
        ap.xc, ap.yc = ap.center

    def _place_at(self, ap: Aperture, xc: float, yc: float):
        c = int(ap.size / 2)
        ap.x0 = int(round(xc - c))
        ap.y0 = int(round(yc - c))
        ap.clamp(self.width, self.height)

    def _save_yellow_mask(self, image):
        if self.use_yellow_mask and self.y1 is not None:
            # PyMovie keeps the blob mask, which is empty if no blob was found.
            self.yellow_mask = self.measure(image, self.y1, use_mask=False).blob_mask

    # ---------------------------------------------------------------- run

    def initialize(self, image: np.ndarray):
        """Record start-of-run geometry (needed for 2-yellow rotation tracking)."""
        self._save_yellow_mask(image)
        if self.y1 is None or self.y2 is None:
            return
        m = self.measure(image, self.y1)
        self.y1.xc, self.y1.yc = m.xc, m.yc
        self.y1.dx = self.y1.dy = 0
        self.y1.theta = 0.0
        self.yellow_xy = (m.xc, m.yc)

        m = self.measure(image, self.y2)
        self.y2.xc, self.y2.yc = m.xc, m.yc
        self.y2.dx, self.y2.dy = m.xc - self.yellow_xy[0], m.yc - self.yellow_xy[1]
        self.y2.theta = angle(self.y2.dx, self.y2.dy)
        self.delta_theta = 0.0

        for ap in self.apertures:
            if ap.color != 'yellow':
                m = self.measure(image, ap)
                ap.xc, ap.yc = m.xc, m.yc
                ap.dx, ap.dy = m.xc - self.yellow_xy[0], m.yc - self.yellow_xy[1]
                ap.theta = None

    def step(self, image: np.ndarray, frame: int) -> dict[str, Measurement]:
        """Re-position all apertures for this image, then measure them all."""
        if self.y1 is not None:
            self._track_yellow(image, frame)
        else:
            for ap in self.apertures:
                if ap.color != 'white':
                    m = self.measure(image, ap)
                    ap.xc, ap.yc = m.xc, m.yc
                    if m.xc_roi is not None:
                        c = int(ap.size / 2)
                        self._jog(ap, c - int(round(m.xc_roi)), c - int(round(m.yc_roi)))

        results = {}
        for ap in self.apertures:
            m = self.measure(image, ap)
            ap.update_background(m.background_sample, self.cfg.smoothing_count)
            results[ap.name] = m
        return results

    def _track_yellow(self, image, frame):
        y1, y2 = self.y1, self.y2
        c = int(y1.size / 2)

        # Where are the yellow stars in their current apertures?
        t1 = self.measure(image, y1).track_xy
        t2 = self.measure(image, y2).track_xy if y2 is not None else None

        # Yellow #1: follow the star (or the track path).
        # If the star is lost, the aperture stays put (PyMovie re-uses the position from
        # the previous frame's post-move measurement, which is close to the centre).
        if self.track_path is not None:
            cx, cy = y1.center
            px, py = self.track_path.center(frame)
            delta = (cx - px, cy - py)
        elif t1 is not None:
            delta = (c - t1[0], c - t1[1])
        else:
            delta = (0, 0)
        self._jog(y1, *delta)

        if y2 is not None:
            self.yellow_xy = (y1.xc, y1.yc)
            self._save_yellow_mask(image)

            c2 = int(y2.size / 2)
            self._jog(y2, *((c2 - t2[0], c2 - t2[1]) if t2 is not None else (0, 0)))
            m = self.measure(image, y2)
            y2.xc, y2.yc = m.xc, m.yc
            self.delta_theta = angle(m.xc - self.yellow_xy[0], m.yc - self.yellow_xy[1]) - y2.theta

            cos_t, sin_t = math.cos(self.delta_theta), math.sin(self.delta_theta)
            for ap in self.apertures:
                if ap.color in ('yellow', 'white'):
                    continue
                ap.xc = ap.dx * cos_t - ap.dy * sin_t + self.yellow_xy[0]
                ap.yc = ap.dx * sin_t + ap.dy * cos_t + self.yellow_xy[1]
                self._place_at(ap, ap.xc, ap.yc)
            return

        # One yellow: shift red/green apertures by the same amount.
        for ap in self.apertures:
            if ap.color in ('red', 'green'):
                self._jog(ap, *delta)
        self._save_yellow_mask(image)
