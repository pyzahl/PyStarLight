"""Pixel formats and decoding of raw camera buffers to numpy images."""
import cv2
import numpy as np

# pixel formats per bit depth, in order of preference ({b}: Mono / Bayer base name); packed
# 10-bit GenICam formats are left out (the decoder doesn't unpack them)
FORMATS_BY_BITS = {12: ["{b}12p", "{b}12Packed", "{b}16", "Y12", "Y12P"],
                   10: ["{b}10", "Y10", "Y10P"],
                   8: ["{b}8", "GREY"]}


def format_bits(pixel_format):
    """Bit depth of a pixel format name (12 / 10 / 8; 16 counts as 12, the cameras' ADC)."""
    for bits in (12, 10, 8):
        if any(pixel_format == f.format(b=b) for f in FORMATS_BY_BITS[bits]
               for b in ("Mono", "BayerRG", "BayerBG", "BayerGR", "BayerGB")):
            return bits
    return 16 if "16" in pixel_format else 8


def format_for_bits(bits, available, current):
    """The preferred pixel format of a bit depth that the camera offers (None: none)."""
    base = "Bayer" + current[5:7] if current.startswith("Bayer") else "Mono"
    return next((f.format(b=base) for f in FORMATS_BY_BITS[bits]
                 if f.format(b=base) in available), None)


def unpack_12bit(raw, w, h, lsb_first):
    """Unpack 12-bit packed data (2 pixels in 3 bytes) to a new uint16 (h, w) array.

    lsb_first=True  -> GenICam 'Mono12p' / 'Bayer*12p' layout
    lsb_first=False -> legacy 'Mono12Packed' / 'Bayer*12Packed' layout
    """
    b = np.frombuffer(raw, np.uint8, count=w * h * 3 // 2).reshape(-1, 3)
    out = np.empty((h, w), np.uint16)
    o = out.reshape(-1, 2)
    b0 = b[:, 0]
    b1 = b[:, 1].astype(np.uint16)
    b2 = b[:, 2]
    if lsb_first:
        np.left_shift(b1 & 0x0F, 8, out=o[:, 0])
        o[:, 0] |= b0
        np.left_shift(b2, 4, out=o[:, 1], dtype=np.uint16)
        o[:, 1] |= b1 >> 4
    else:
        np.left_shift(b0, 4, out=o[:, 0], dtype=np.uint16)
        o[:, 0] |= b1 & 0x0F
        np.left_shift(b2, 4, out=o[:, 1], dtype=np.uint16)
        o[:, 1] |= b1 >> 4
    return out


def unpack_mipi(raw, w, h, bits):
    """Unpack MIPI CSI-2 packed RAW10 / RAW12 (V4L2 'Y10P' / 'Y12P') to a uint16 (h, w) array.

    Each group starts with the high 8 bits of every pixel, followed by one byte holding
    the low bits of all of them (pixel 0 in the least significant bits).
    """
    n = 4 if bits == 10 else 2              # pixels per group
    b = np.frombuffer(raw, np.uint8, count=w * h * bits // 8).reshape(-1, n + 1)
    low = b[:, n].astype(np.uint16)
    lbits = bits - 8
    out = np.empty((h, w), np.uint16)
    o = out.reshape(-1, n)
    for i in range(n):
        np.left_shift(b[:, i], lbits, out=o[:, i], dtype=np.uint16)
        o[:, i] |= (low >> (lbits * i)) & ((1 << lbits) - 1)
    return out


# GenICam Bayer names -> OpenCV codes (OpenCV's naming is shifted by one pixel)
BAYER_CODES = {
    "BayerRG": cv2.COLOR_BayerBG2BGR,
    "BayerBG": cv2.COLOR_BayerRG2BGR,
    "BayerGR": cv2.COLOR_BayerGB2BGR,
    "BayerGB": cv2.COLOR_BayerGR2BGR,
}


class Decoder:
    def __init__(self, pixel_format):
        pf = pixel_format
        self.pixel_format = pf
        self.mipi_bits = {"Y10P": 10, "Y12P": 12}.get(pf)     # V4L2 MIPI-packed (VEYE)
        self.msb_shift = {"Y10": 6, "Y12": 4}.get(pf)         # VEYE, unpacked by the Pi
        self.is_packed12 = self.mipi_bits is None and (
            "12p" in pf.lower() or "12packed" in pf.lower())
        self.lsb_first = pf.endswith("12p")
        if self.is_packed12 or "12" in pf:
            self.max_value = 4095
        elif "10" in pf:
            self.max_value = 1023
        elif "16" in pf:
            self.max_value = 65535
        else:
            self.max_value = 255
        self.depth = int(self.max_value).bit_length()           # significant bits per pixel
        self.dtype = np.uint8 if self.max_value == 255 else np.uint16
        self.bayer_code = BAYER_CODES.get(pf[:7]) if pf.startswith("Bayer") else None

    def __call__(self, buf):
        # PyGObject returns a copy, so the Aravis buffer can be recycled
        return self.raw(buf.get_data(), buf.get_image_width(), buf.get_image_height())

    def raw(self, raw, w, h):
        if self.msb_shift:
            return np.frombuffer(raw, np.uint16, count=w * h).reshape(h, w) >> self.msb_shift
        if self.mipi_bits:
            img = unpack_mipi(raw, w, h, self.mipi_bits)
        elif self.is_packed12:
            img = unpack_12bit(raw, w, h, self.lsb_first)
        else:
            img = np.frombuffer(raw, dtype=self.dtype, count=w * h).reshape(h, w)
        return img
