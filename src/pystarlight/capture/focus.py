"""Focus helper: one star's size on the live frames, against the focuser position (the Focuser
window's V-curve).

NEW CODE, numpy + OpenCV (as guider.py). The size is the half-flux diameter (HFD: twice the
flux-weighted mean distance from the centroid), which stays meaningful far from focus, where a
star becomes a ring and a width fit fails; near focus it follows the FWHM (also given, the core
width of guider.measure). The star: the brightest unsaturated one near the image centre, or the
one nearest to a point picked on the image; it is followed from frame to frame.

The curve: HFD grows linearly with the distance from focus on both sides (a hyperbola close to
it), so HFD² is a parabola in the position: a linear least-squares fit, its vertex the best
focus. fit_curve wants points on both sides of the vertex.
"""
import queue
import threading
import time

import cv2
import numpy as np

from .guider import find_stars, measure

BOX_R = 30                 # half size of the measuring box (px): room for a defocused star
LOST_FRAMES = 10           # frames without the star: lost
MIN_SNR = 5.0


def hfd(img, x, y, r=BOX_R):
    """The star in the box of half size r around (x, y): dict x, y (flux centroid), hfd (px),
    flux, peak (raw), snr; None outside the frame or with nothing above the noise. Sky from the
    box edge; only the connected patch above 3 sigma nearest the centre counts; hot pixels out
    (3 x 3 median)."""
    h, w = img.shape
    xi, yi = int(round(x)), int(round(y))
    if xi - r < 0 or yi - r < 0 or xi + r >= w or yi + r >= h:
        return None
    raw = img[yi - r:yi + r + 1, xi - r:xi + r + 1].astype(np.float32)
    s = cv2.medianBlur(raw, 3)
    edge = np.concatenate([s[0], s[-1], s[1:-1, 0], s[1:-1, -1]])
    sky = float(np.median(edge))
    noise = 1.4826 * float(np.median(np.abs(edge - sky))) or 1.0
    d = s - sky
    ax = np.arange(-r, r + 1, dtype=np.float32)
    inside = ax[None, :] ** 2 + ax[:, None] ** 2 <= r * r
    # the star's own light: the connected patch above 3 sigma nearest the box centre (a ring far
    # from focus is one patch; a neighbour's wing in the box is another and stays out)
    n, labels = cv2.connectedComponents(((d > 3 * noise) & inside).astype(np.uint8), connectivity=8)
    if n < 2:
        return None
    ys, xs = np.nonzero(labels)
    near = np.argmin((xs - r) ** 2 + (ys - r) ** 2)
    own = labels == labels[ys[near], xs[near]]
    d = np.where(own, d, 0.0)
    tot = float(d.sum())
    if tot <= 0:
        return None
    cx = float((d.sum(0) * ax).sum() / tot)
    cy = float((d.sum(1) * ax).sum() / tot)
    dist = np.hypot(ax[None, :] - cx, ax[:, None] - cy)
    return {"x": xi + cx, "y": yi + cy, "hfd": 2.0 * float((d * dist).sum() / tot), "flux": tot,
            "peak": float(raw[own].max()), "snr": float(d.max() / noise)}


def pick_star(img, full_scale, near=None, r=BOX_R):
    """(x, y) of the star to measure: the one nearest to `near` (x, y) within 2 r, else the
    brightest unsaturated star in the middle half of the frame; None."""
    h, w = img.shape
    stars = [s for s in find_stars(img, 12) if r <= s["x"] < w - r and r <= s["y"] < h - r]
    def centre(x, y):                    # the flux centroid, as hfd() measures it
        m = hfd(img, x, y, r)
        return (m["x"], m["y"]) if m is not None else None

    if near is not None:
        close = [s for s in stars if np.hypot(s["x"] - near[0], s["y"] - near[1]) < 2 * r]
        if close:
            s = min(close, key=lambda s: np.hypot(s["x"] - near[0], s["y"] - near[1]))
            return centre(s["x"], s["y"])
        return centre(float(near[0]), float(near[1]))
    limit = 0.9 * full_scale if full_scale else np.inf
    middle = [s for s in stars if abs(s["x"] - w / 2) < w / 4 and abs(s["y"] - h / 2) < h / 4
              and s["peak"] < limit and s["snr"] > 10]
    if not middle:
        return None
    s = max(middle, key=lambda s: s["flux"])
    return centre(s["x"], s["y"])


def fit_curve(points):
    """Best focus from [(position, hfd)]: HFD² = c0 + c1 p + c2 p², least squares. (best
    position, HFD there, the fitted curve as a function of the position) or None (fewer than 3
    positions, not a minimum, or the vertex outside the positions measured)."""
    pts = np.asarray(points, dtype=float)
    if len(pts) < 3 or len(np.unique(pts[:, 0])) < 3:
        return None
    p, v = pts[:, 0], pts[:, 1] ** 2
    p0, scale = p.mean(), (np.ptp(p) or 1.0)          # centred and scaled: a sane matrix
    u = (p - p0) / scale
    c2, c1, c0 = np.polyfit(u, v, 2)
    if c2 <= 0:
        return None
    best = -c1 / (2 * c2)
    if not u.min() <= best <= u.max():
        return None
    def curve(pos):
        x = (np.asarray(pos, dtype=float) - p0) / scale
        return np.sqrt(np.maximum(c0 + c1 * x + c2 * x * x, 0.0))
    return float(p0 + best * scale), float(np.sqrt(max(c0 - c1 * c1 / (4 * c2), 0.0))), curve


class FocusMeter(threading.Thread):
    """Measures the focus star on frames from the grabber (wants() / put(), as the star lock).
    ask('center') / ask('at', x, y) chooses the star on the next frame, ask('stop') ends.
    samples: [(monotonic t, hfd, fwhm, peak)] while measuring; star: (x, y) or None.
    follow: how far (px) the star may move from one frame to the next and still be followed
    (default r / 2; the Polar alignment window's follower: the whole box, the knobs move it)."""

    def __init__(self, decode, full_scale=None, r=BOX_R, follow=None):
        super().__init__(daemon=True)
        self.decode, self.full_scale, self.r = decode, full_scale, r
        self.follow = r / 2 if follow is None else follow
        self.inbox = queue.Queue(maxsize=1)
        self.request = None
        self.star = None
        self.state = "idle"             # idle / measuring / lost
        self.note = ""
        self.samples = []
        self.missed = 0
        self.running = True
        self.start()

    def wants(self):
        return (self.state != "idle" or self.request is not None) and self.inbox.empty()

    def put(self, raw, exposure_ns=None, info=None):
        try:
            self.inbox.put_nowait(raw)
        except queue.Full:
            pass

    def ask(self, *request):
        self.request = request
        if request[0] == "stop":
            self.state, self.star, self.note = "idle", None, ""

    def run(self):
        while self.running:
            try:
                raw = self.inbox.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                img = np.asarray(self.decode(raw))
                if img.ndim == 3:
                    img = img[..., 0]
                self.process(img, time.monotonic())
            except Exception as e:          # e.g. the region changed under it
                self.note = f"focus: {e}"

    def process(self, img, t):
        req = self.request
        if req is not None and req[0] in ("center", "at"):
            self.request = None
            near = (req[1], req[2]) if req[0] == "at" else None
            self.star = pick_star(img, self.full_scale, near, self.r)
            self.samples, self.missed = [], 0
            if self.star is None:
                self.state = "idle"
                self.note = ("no star there" if near else
                             "no unsaturated star near the centre: pick one on the image")
                return
            self.state, self.note = "measuring", ""
        if self.state == "idle" or self.star is None:
            return
        m = hfd(img, *self.star, self.r)
        if m is None or m["snr"] < MIN_SNR:
            self.missed += 1
            if self.missed >= LOST_FRAMES:
                self.state, self.note = "lost", "star lost (clouds, moved off?): pick it again"
            return
        self.missed = 0
        self.state, self.note = "measuring", ""
        if np.hypot(m["x"] - self.star[0], m["y"] - self.star[1]) < self.follow:
            self.star = (m["x"], m["y"])            # follow it (drift)
        core = measure(img, *self.star, min(self.r, 15))
        self.samples.append((t, m["hfd"], core["fwhm"] if core else float("nan"), m["peak"]))
        del self.samples[:-2000]

    def box(self):
        """(x0, y0, x1, y1) of the measuring box, or None."""
        if self.star is None or self.state == "idle":
            return None
        x, y = self.star
        return x - self.r, y - self.r, x + self.r, y + self.r

    def close(self):
        self.running = False
