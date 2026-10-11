"""PyStarLight: movie readers, star tracking and aperture photometry for occultation light curves.

The readers, tracking and photometry are derived from PyMovie by Bob Anderson (MIT licence,
see LICENSES/MIT-PyMovie.txt).
"""

from . import calibrate, normalize, preprocess, timestamps, wcs
from .calibrate import Calibration
from .movie import Frame, open_movie, split_fields
from .photometry import Measurement, PhotometryConfig, measure_aperture, robust_mean_std
from .pipeline import LightCurves, analyze
from .tracking import Aperture, Tracker, TrackPath

__version__ = "0.1.0"

__all__ = [
    "Aperture",
    "Calibration",
    "Frame",
    "LightCurves",
    "Measurement",
    "PhotometryConfig",
    "TrackPath",
    "Tracker",
    "analyze",
    "calibrate",
    "measure_aperture",
    "normalize",
    "open_movie",
    "preprocess",
    "robust_mean_std",
    "split_fields",
    "timestamps",
    "wcs",
]
