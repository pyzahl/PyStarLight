"""Frame timestamps: VTI OCR for analog video, and timing checks.

Timestamps that come with the file (SER, ADV/AAV, RAVF, FITS, SharpCap AVI) are read by
the movie readers into ``Frame.date`` / ``Frame.time``. Analog video with a VTI needs
``VtiReader``.
"""

from .timing import TimingReport, check_timing, field_times, parse_time, parse_times, unwrap_midnight
from .vti import PROFILES, PYMOVIE_VTI_MENU, FieldReading, VtiReader, VtiReading

__all__ = [
    "PROFILES",
    "PYMOVIE_VTI_MENU",
    "FieldReading",
    "TimingReport",
    "VtiReader",
    "VtiReading",
    "check_timing",
    "field_times",
    "parse_time",
    "parse_times",
    "unwrap_midnight",
]
