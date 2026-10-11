"""Read VTI (video time inserter) timestamps burned into analog video frames.

One ``VtiReader`` per video. It holds what PyMovie keeps in its GUI: the character
boxes of both fields, the ten trained digit templates, the formatter for the VTI model,
and the Kiwi left/right box bookkeeping.

    vti = VtiReader('iota3-full', width=720)        # or VtiReader.from_pymovie_folder(folder)
    vti.train(image, 'upper', box_index=3, digit=7)  # until vti.missing_digits() == []
    r = vti.read(image)                              # VtiReading for both fields
    r.upper_timestamp, r.upper_time, r.lower_timestamp, r.lower_time

Extracted from PyMovie 4.2.8 (main.py: vtiSelected, extractTimestamps, startTimestampReading,
setTimestampFormatter, processOcrTemplate, load/pickle OCR boxes and model digits).
Files written by ``save()`` use PyMovie's names and pickle layout, so profiles trained
in either program work in the other.
"""

from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from . import ocr

# PyMovie VTI menu entry -> (profile name, formatter code)
PYMOVIE_VTI_MENU = {
    'IOTA VTI 3: one line (with F)': 'iota3-full',
    'IOTA VTI 3: two line (with F)': 'iota3-safe',
    'IOTA VTI 2: one line (with P)': 'iota2-full',
    'IOTA VTI 2: two line (with P)': 'iota2-safe',
    'BoxSprite: one-line': 'boxsprite',
    'Kiwi NTSC (left)': 'kiwi-left',
    'Kiwi NTSC (right)': 'kiwi-right',
    'Kiwi PAL (left)': 'kiwi-PAL-left',
    'Kiwi PAL (right)': 'kiwi-PAL-right',
    'GHS VTI': 'GHS',
}


@dataclass(frozen=True)
class VtiProfile:
    code: str                       # PyMovie formatter code (formatter.txt)
    formatter: Callable
    boxes_640: Optional[Callable]   # -> (upper boxes, lower boxes)
    boxes_720: Callable
    right_boxes_640: Optional[Callable] = None   # Kiwi: boxes shifted 11 px right
    right_boxes_720: Optional[Callable] = None
    kiwi: bool = False              # Kiwi character handling (blank digits, 6/8/9 checks)
    slant: bool = False             # Kiwi NTSC slanted characters
    start_right: bool = False


PROFILES = {
    'iota3-full': VtiProfile('iota', ocr.format_iota_timestamp,
                             ocr.setup_for_iota_640_full_screen_mode3,
                             ocr.setup_for_iota_720_full_screen_mode3),
    'iota3-safe': VtiProfile('iota', ocr.format_iota_timestamp,
                             ocr.setup_for_iota_640_safe_mode3, ocr.setup_for_iota_720_safe_mode3),
    'iota2-full': VtiProfile('iota', ocr.format_iota_timestamp,
                             ocr.setup_for_iota_640_full_screen_mode2,
                             ocr.setup_for_iota_720_full_screen_mode2),
    'iota2-safe': VtiProfile('iota', ocr.format_iota_timestamp,
                             ocr.setup_for_iota_640_safe_mode2, ocr.setup_for_iota_720_safe_mode2),
    'boxsprite': VtiProfile('boxsprite', ocr.format_boxsprite3_timestamp,
                            ocr.setup_for_boxsprite3_640, ocr.setup_for_boxsprite3_720),
    'kiwi-left': VtiProfile('kiwi-left', ocr.format_kiwi_timestamp,
                            ocr.setup_for_kiwi_vti_640_left, ocr.setup_for_kiwi_vti_720_left,
                            ocr.setup_for_kiwi_vti_640_right, ocr.setup_for_kiwi_vti_720_right,
                            kiwi=True, slant=True),
    'kiwi-right': VtiProfile('kiwi-right', ocr.format_kiwi_timestamp,
                             ocr.setup_for_kiwi_vti_640_left, ocr.setup_for_kiwi_vti_720_left,
                             ocr.setup_for_kiwi_vti_640_right, ocr.setup_for_kiwi_vti_720_right,
                             kiwi=True, slant=True, start_right=True),
    'kiwi-PAL-left': VtiProfile('kiwi-PAL-left', ocr.format_kiwi_timestamp,
                                None, ocr.setup_for_kiwi_PAL_720_left,
                                None, ocr.setup_for_kiwi_PAL_720_right, kiwi=True),
    'kiwi-PAL-right': VtiProfile('kiwi-PAL-right', ocr.format_kiwi_timestamp,
                                 None, ocr.setup_for_kiwi_PAL_720_left,
                                 None, ocr.setup_for_kiwi_PAL_720_right, kiwi=True, start_right=True),
    'GHS': VtiProfile('GHS', ocr.format_ghs_timestamp, None, ocr.setup_for_GHS_generic),
}

# formatter.txt code -> profile. 'iota' files do not say which IOTA layout; the saved boxes do.
CODE_TO_PROFILE = {'iota': 'iota3-full', 'boxsprite': 'boxsprite', 'kiwi-left': 'kiwi-left',
                   'kiwi-right': 'kiwi-right', 'kiwi-PAL-left': 'kiwi-PAL-left',
                   'kiwi-PAL-right': 'kiwi-PAL-right', 'GHS': 'GHS'}


@dataclass
class FieldReading:
    timestamp: str        # '[hh:mm:ss.ffff]'; '[00:00:00.0000]' when unreadable
    time: float           # seconds since midnight; negative when unreadable
    digits: str           # raw OCR string, blanks as ' '
    scores: str           # per-box correlation scores (0-99) and their sum
    cum_score: int        # sum of correlation scores * 100

    @property
    def ok(self) -> bool:
        return self.time >= 0


@dataclass
class VtiReading:
    upper: FieldReading
    lower: FieldReading
    top_field_first: Optional[bool]   # None until both fields have been read once

    @property
    def first(self) -> FieldReading:
        return self.lower if self.top_field_first is False else self.upper

    @property
    def second(self) -> FieldReading:
        return self.upper if self.top_field_first is False else self.lower


class VtiReader:
    def __init__(self, profile: str, width: int = 720, digits: Optional[list] = None,
                 upper_boxes: Optional[list] = None, lower_boxes: Optional[list] = None,
                 upper_boxes_right: Optional[list] = None, lower_boxes_right: Optional[list] = None):
        if profile not in PROFILES:
            raise ValueError(f'unknown VTI profile {profile!r}; one of {sorted(PROFILES)}')
        self.profile_name = profile
        self.profile = p = PROFILES[profile]
        use_640 = width == 640 and p.boxes_640 is not None
        up, lo = (p.boxes_640 if use_640 else p.boxes_720)()
        self.upper_boxes_left = list(upper_boxes or up)
        self.lower_boxes_left = list(lower_boxes or lo)
        right = p.right_boxes_640 if use_640 and p.right_boxes_640 else p.right_boxes_720
        if right is not None:
            ru, rl = right()
        else:
            ru, rl = [], []
        self.upper_boxes_right = list(upper_boxes_right or ru)
        self.lower_boxes_right = list(lower_boxes_right or rl)
        self.digits: list = list(digits) if digits is not None else [None] * 10
        self.reset()

    def reset(self):
        """Forget per-run state (Kiwi counters, box choice, detected field order)."""
        self.upper_left_count = self.upper_right_count = 0
        self.lower_left_count = self.lower_right_count = 0
        pos = 'right' if self.profile.start_right else 'left'
        self.upper_pos = self.lower_pos = pos
        self.top_field_first: Optional[bool] = None

    # ------------------------------------------------------------ training

    def missing_digits(self) -> list[int]:
        return [i for i, d in enumerate(self.digits) if d is None]

    @property
    def ready(self) -> bool:
        return not self.missing_digits()

    def boxes(self, field: str) -> list:
        if field == 'upper':
            return self.upper_boxes_left if self.upper_pos == 'left' else self.upper_boxes_right
        return self.lower_boxes_left if self.lower_pos == 'left' else self.lower_boxes_right

    def box_image(self, image: np.ndarray, field: str, index: int) -> np.ndarray:
        """Pixels of one character box, as used for matching."""
        fld = image[0::2, :] if field == 'upper' else image[1::2, :]
        return ocr.timestamp_box_image(fld, self.boxes(field)[index],
                                       kiwi=self.profile.kiwi, slant=self.profile.slant)

    def train(self, image: np.ndarray, field: str, box_index: int, digit: int):
        """Use the character in this box as the template for `digit`.
        Pick a frame >= 1: frame 0 is often damaged during recording."""
        self.digits[digit] = self.box_image(image, field, box_index)

    def confusion_matrix(self) -> np.ndarray:
        """10x10 template-vs-template scores. Off-diagonal values near 1 mean two
        templates are too alike to tell apart."""
        if not self.ready:
            raise ValueError(f'missing digits {self.missing_digits()}')
        return np.array([ocr.cv2_score(self.digits[i].copy(), self.digits)[2] for i in range(10)])

    # ------------------------------------------------------------ reading

    def _read_field(self, fld, boxes, use_left):
        p = self.profile
        ts, t, raw, scores, cum, left_used = ocr.extract_timestamp(
            fld, boxes, self.digits, p.formatter, 0, kiwi=p.kiwi, slant=p.slant, t2fromleft=use_left)
        return FieldReading(ts, t, raw, scores, cum), left_used

    def read(self, image: np.ndarray) -> VtiReading:
        """OCR both fields of one interlaced frame (full-height image, before any
        correction that changes pixel values)."""
        if not self.ready:
            raise RuntimeError(f'VTI digit templates missing: {self.missing_digits()}')
        upper_f, lower_f = image[0::2, :], image[1::2, :]

        if self.profile.kiwi:
            # Kiwi draws the time at one of two horizontal positions; read both, keep the
            # better match. After a few frames the field's t2 side is fixed by majority.
            use_left = (self.upper_left_count > self.upper_right_count
                        if self.upper_left_count + self.upper_right_count > 3 else None)
            reg, reg_l = self._read_field(upper_f, self.upper_boxes_left, use_left)
            alt, alt_l = self._read_field(upper_f, self.upper_boxes_right, use_left)
            if reg.cum_score > alt.cum_score:
                self.upper_pos, upper, upper_left_used = 'left', reg, reg_l
            else:
                self.upper_pos, upper, upper_left_used = 'right', alt, alt_l

            use_left = (self.lower_left_count > self.lower_right_count
                        if self.lower_left_count + self.lower_right_count > 3 else None)
            reg, reg_l = self._read_field(lower_f, self.lower_boxes_left, use_left)
            alt, alt_l = self._read_field(lower_f, self.lower_boxes_right, use_left)
            if reg.cum_score > alt.cum_score:
                self.lower_pos, lower, lower_left_used = 'left', reg, reg_l
            else:
                self.lower_pos, lower, lower_left_used = 'right', alt, alt_l
        else:
            upper, upper_left_used = self._read_field(upper_f, self.upper_boxes_left, None)
            lower, lower_left_used = self._read_field(lower_f, self.lower_boxes_left, None)

        if upper_left_used:
            self.upper_left_count += 1
        else:
            self.upper_right_count += 1
        if lower_left_used:
            self.lower_left_count += 1
        else:
            self.lower_right_count += 1

        # Field order is decided once, from the first frame where both fields read
        if self.top_field_first is None and upper.ok and lower.ok:
            self.top_field_first = not lower.time < upper.time

        return VtiReading(upper, lower, self.top_field_first)

    # ------------------------------------------------------------ persistence (PyMovie format)

    def save(self, folder: str):
        """Write formatter.txt, custom-boxes-*.p and custom-digits.p like PyMovie."""
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'formatter.txt'), 'w') as f:
            f.write(self.profile.code)
        for name, obj in (('custom-boxes-upper.p', self.upper_boxes_left),
                          ('custom-boxes-lower.p', self.lower_boxes_left),
                          ('custom-boxes-upper-right.p', self.upper_boxes_right),
                          ('custom-boxes-lower-right.p', self.lower_boxes_right),
                          ('custom-digits.p', self.digits)):
            with open(os.path.join(folder, name), 'wb') as f:
                pickle.dump(obj, f)

    @classmethod
    def from_pymovie_folder(cls, folder: str, width: int = 720,
                            fallback_dir: Optional[str] = None) -> VtiReader:
        """Load a profile saved by PyMovie (its AVI-WCS folder, else ``fallback_dir``,
        which PyMovie keeps as a copy in the user's home folder).

        Only load pickle files you created yourself: unpickling runs code."""
        def find(name):
            for d in (folder, fallback_dir):
                if d and os.path.exists(os.path.join(d, name)):
                    return os.path.join(d, name)
            return None

        def load(name, default=None):
            p = find(name)
            if p is None:
                return default
            with open(p, 'rb') as f:
                return pickle.load(f)

        fpath = find('formatter.txt')
        if fpath is None:
            raise FileNotFoundError(f'no formatter.txt in {folder}')
        with open(fpath) as f:
            code = f.readline().strip()
        if code == 'SharpCap8':
            raise ValueError("SharpCap 8-bit AVI: use open_movie(path, sharpcap_timestamps=True)")
        if code not in CODE_TO_PROFILE:
            raise ValueError(f'unknown formatter code {code!r}')
        return cls(CODE_TO_PROFILE[code], width=width,
                   digits=load('custom-digits.p'),
                   upper_boxes=load('custom-boxes-upper.p'),
                   lower_boxes=load('custom-boxes-lower.p'),
                   upper_boxes_right=load('custom-boxes-upper-right.p'),
                   lower_boxes_right=load('custom-boxes-lower-right.p'))
