"""PyStarLight icon (square): a python coiled under a starry sky; its body is the light curve,
a star shines in its eye and goes out when it blinks.

    uv run python tests/make_logo.py [OUT.svg] [--closed]
    rsvg-convert -w 256 src/pystarlight/pystarlight_logo.svg -o src/pystarlight/pystarlight_logo.png

Default output: src/pystarlight/pystarlight_logo.svg. --closed: still frame with the lid down.
"""
import sys
from pathlib import Path

import numpy as np

args = [a for a in sys.argv[1:] if not a.startswith("--")]
out = args[0] if args else str(Path(__file__).resolve().parents[1] / "src/pystarlight/pystarlight_logo.svg")
closed = "--closed" in sys.argv          # still frame with the lid down (preview only)


def bezier(p0, p1, p2, p3, n=160):
    t = np.linspace(0, 1, n)[:, None]
    p0, p1, p2, p3 = map(np.array, (p0, p1, p2, p3))
    return (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3


# ---- body centre line, from the neck (inside the head) to the tail tip
neck = bezier((246, 246), (120, 240), (70, 412), (160, 412))
x = np.linspace(160, 380, 600)[1:]
dip = 0.5 * (np.tanh((x - 228) / 7) - np.tanh((x - 312) / 7))
ends = np.sin(np.pi * (x - 160) / 220)                    # no wiggle where the curves join
y = 412 + 50 * dip + 2.6 * np.sin(x / 8.0) * (1 - dip) * ends
bottom = np.c_[x, y]
rise = bezier((380, 412), (446, 412), (466, 372), (458, 348))[1:]
curl = bezier((458, 348), (450, 328), (424, 322), (424, 346), 80)[1:]
c = np.vstack([neck, bottom, rise, curl])
cx, cy = c[:, 0], c[:, 1]

s = np.concatenate([[0], np.cumsum(np.hypot(np.diff(cx), np.diff(cy)))])
s /= s[-1]
hw = 1.4 + 25 * (1 - s) ** 0.75                           # thick neck, thin tail tip
dx, dy = np.gradient(cx), np.gradient(cy)
n = np.hypot(dx, dy)
nx, ny = -dy / n, dx / n
top = np.c_[cx + nx * hw, cy + ny * hw]
bot = np.c_[cx - nx * hw, cy - ny * hw]


def pts(a):
    return " ".join(f"{px:.1f},{py:.1f}" for px, py in a)


body = f"M{pts(top)} L{pts(bot[::-1])} Z"
i0, i1 = len(neck) // 3, len(c) - 60
spine = f"M{pts(c[i0:i1])}"

# ---- head, drawn in its own coordinates and placed with HEAD_T
HEAD_T = "translate(-441.4 25.8) scale(1.7)"
head = ("M382 104 C392 82 426 74 458 76 C488 78 506 92 512 108 "
        "C514 118 508 126 496 129 C472 136 440 144 410 147 C392 148 380 140 378 128 "
        "C377 118 379 110 382 104 Z")
jaw = "M506 122 C484 130 452 135 424 137"
EYE = (446, 104)
eye_open = "M418 104 C421 74 471 74 474 104 C471 132 421 132 418 104 Z"
eye_shut = "M418 104 C421 130 471 130 474 104 C471 132 421 132 418 104 Z"


def star(x0, y0, r_long, r_short, k=4, rot=0.0):
    p = []
    for i in range(2 * k):
        r = r_long if i % 2 == 0 else r_short
        a = np.pi * i / k + rot
        p.append((x0 + r * np.sin(a), y0 - r * np.cos(a)))
    return "M" + " L".join(f"{a:.1f} {b:.1f}" for a, b in p) + " Z"


big = star(*EYE, 27, 4.4)
small = star(*EYE, 12, 2.6, rot=np.pi / 4)
blink = f"""<animate attributeName="d" dur="5s" repeatCount="indefinite" calcMode="spline"
                 keyTimes="0;0.84;0.89;0.94;1" keySplines="0 0 1 1;0.5 0 1 1;0 0 0.5 1;0 0 1 1"
                 values="{eye_open};{eye_open};{eye_shut};{eye_open};{eye_open}"/>"""
dim = """<animate attributeName="opacity" dur="5s" repeatCount="indefinite"
               keyTimes="0;0.84;0.89;0.94;1" values="1;1;0;1;1"/>"""
lid = eye_shut if closed else eye_open
anim = "" if closed else blink

# ---- fit the snake (body + head, tongue included) to the tile, with a margin
hx0, hy0, hx1, hy1 = 377 * 1.7 - 441.4, 74 * 1.7 + 25.8, 527 * 1.7 - 441.4, 148 * 1.7 + 25.8
bx = np.r_[top[:, 0], bot[:, 0], hx0, hx1]
by = np.r_[top[:, 1], bot[:, 1], hy0, hy1]
MARGIN = 40
k = (512 - 2 * MARGIN) / max(bx.max() - bx.min(), by.max() - by.min())
FIT = (f"translate(256 256) scale({k:.4f}) "
       f"translate({-(bx.min() + bx.max()) / 2:.1f} {-(by.min() + by.max()) / 2:.1f})")

# ---- sky: fixed random stars, a few brighter ones with spikes
rng = np.random.default_rng(7)
sky = []
for _ in range(110):
    sx, sy = rng.uniform(20, 492, 2)
    r = rng.choice([0.6, 0.8, 1.0, 1.3, 1.7], p=[0.35, 0.3, 0.2, 0.1, 0.05])
    sky.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{r}" opacity="{rng.uniform(0.35, 0.95):.2f}"/>')
bright = "".join(f'<path d="{star(bx, by, rr, rr / 5)}"/>'
                 for bx, by, rr in ((92, 92, 9), (420, 70, 7), (176, 300, 5), (470, 470, 5)))

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <title>PyStarLight</title>
  <defs>
    <radialGradient id="night" cx="38%" cy="30%" r="80%">
      <stop offset="0" stop-color="#1c3260"/>
      <stop offset="0.55" stop-color="#0d1a36"/>
      <stop offset="1" stop-color="#050a16"/>
    </radialGradient>
    <linearGradient id="skin" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5a9bd5"/>
      <stop offset="0.55" stop-color="#3776ab"/>
      <stop offset="1" stop-color="#22568a"/>
    </linearGradient>
    <pattern id="scales" width="12" height="8" patternUnits="userSpaceOnUse">
      <path d="M0 0 A6 6 0 0 0 12 0 M-6 4 A6 6 0 0 0 6 4 M6 4 A6 6 0 0 0 18 4 M0 8 A6 6 0 0 0 12 8"
            fill="none" stroke="#173d63" stroke-width="0.9" opacity="0.6"/>
    </pattern>
    <radialGradient id="iris" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#fff6b0"/>
      <stop offset="0.4" stop-color="#ffd43b"/>
      <stop offset="0.85" stop-color="#d08a12"/>
      <stop offset="1" stop-color="#6a4506"/>
    </radialGradient>
    <radialGradient id="glow" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#ffffff"/>
      <stop offset="0.3" stop-color="#e4f1ff" stop-opacity="0.8"/>
      <stop offset="1" stop-color="#bfdcff" stop-opacity="0"/>
    </radialGradient>
    <clipPath id="tile"><rect width="512" height="512" rx="96"/></clipPath>
    <clipPath id="bodyclip"><path d="{body}"/></clipPath>
    <clipPath id="headclip"><path d="{head}"/></clipPath>
    <clipPath id="lids"><path d="{lid}">{anim}</path></clipPath>
  </defs>

  <g clip-path="url(#tile)">
    <!-- dark sky -->
    <rect width="512" height="512" fill="url(#night)"/>
    <g fill="#dfe9ff">{"".join(sky)}</g>
    <g fill="#ffffff" opacity="0.9">{bright}</g>

    <g transform="{FIT}">
    <!-- body = light curve: steady star, occultation, recovery; the tail curls up -->
    <path d="{body}" fill="url(#skin)"/>
    <rect width="512" height="512" fill="url(#scales)" clip-path="url(#bodyclip)"/>
    <path d="{spine}" fill="none" stroke="#ffd43b" stroke-width="4" stroke-linecap="round"
          stroke-linejoin="round"/>
    <path d="{body}" fill="none" stroke="#0e2a47" stroke-width="2" stroke-linejoin="round"/>

    <!-- head -->
    <g transform="{HEAD_T}">
      <path d="{head}" fill="url(#skin)"/>
      <g clip-path="url(#headclip)">
        <rect x="370" y="60" width="160" height="100" fill="url(#scales)"/>
        <path d="M398 92 Q416 82 438 80 M456 80 Q480 82 496 94" fill="none" stroke="#173d63"
              stroke-width="1.6" opacity="0.7"/>
      </g>
      <path d="{head}" fill="none" stroke="#0e2a47" stroke-width="1.6" stroke-linejoin="round"/>
      <path d="{jaw}" fill="none" stroke="#0e2a47" stroke-width="2" stroke-linecap="round"/>
      <g fill="#0e2a47">
        <circle cx="498" cy="103" r="2.4"/>
        <ellipse cx="490" cy="129" rx="2" ry="1.3"/>
        <ellipse cx="477" cy="132" rx="2" ry="1.3"/>
        <ellipse cx="464" cy="134.5" rx="2" ry="1.3"/>
      </g>
      <path d="M512 116 l14 -3 m-14 3 l13 5" fill="none" stroke="#e05555" stroke-width="1.8"
            stroke-linecap="round"/>

      <!-- the eye: a star in the slit pupil -->
      <g clip-path="url(#lids)">
        <rect x="410" y="66" width="72" height="76" fill="#1a1204"/>
        <circle cx="{EYE[0]}" cy="{EYE[1]}" r="30" fill="url(#iris)"/>
        <path d="M446 74 C453 88 453 120 446 134 C439 120 439 88 446 74 Z" fill="#05070d"/>
        <g>{'' if closed else dim}
          <circle cx="{EYE[0]}" cy="{EYE[1]}" r="24" fill="url(#glow)"/>
          <path d="{small}" fill="#ffffff" opacity="0.85"/>
          <path d="{big}" fill="#ffffff"/>
        </g>
      </g>
      <path d="{lid}" fill="none" stroke="#0e2a47" stroke-width="2.2" stroke-linejoin="round">{anim}</path>
    </g>
    </g>
  </g>
</svg>
"""
open(out, "w").write(svg)
