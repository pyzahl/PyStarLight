"""A hypothetical Starlink occultation of a star, computed with PyStarLight's diffraction model (bar model: opaque
strips, Fresnel diffraction summed over a camera band, the star's limb-darkened disk, box exposure). Writes
docs/figures/starlink_occultation.png and prints the table of docs/starlink_occultation.md.

    python docs/starlink_occultation.py          (from the repository, with PyStarLight installed: uv run ...)
"""
import math, os
import numpy as np
from pystarlight import diffraction as D

HERE = os.path.dirname(os.path.abspath(__file__))

Z_KM, V = 550.0, 7.2                        # slant range at zenith (km), shadow speed across the track (km/s)
Z_AU = Z_KM / D.AU_KM
QE = [(400, .45), (450, .65), (500, .78), (550, .80), (600, .78), (650, .70), (700, .60), (750, .50), (800, .40),
      (850, .30), (900, .20), (950, .12)]  # rough unfiltered mono CMOS (IMX-like), illustrative
bands = {"sun-like G 10 star": D.Band.from_table(QE, teff=5800), "red giant": D.Band.from_table(QE, teff=3600)}

# along-track chords through an illustrative Starlink V2-mini (m): body only, or panel + gap + body + gap + panel
GEOM = {"body only (3 m)": [(0, 3)],
        "panels + body (12 + 4 + 12 m)": [(0, 12), (13, 17), (18, 30)]}
STARS = {"faint G 10 dwarf (point source)": (0.0, "sun-like G 10 star"),
         "bright K giant, Aldebaran-like (20 mas)": (20.0, "red giant"),
         "bright M supergiant, Betelgeuse-like (45 mas)": (45.0, "red giant")}
EXPOSURES = [0.0, 1e-4, 1e-3, 10e-3, 33e-3, 100e-3]

f = D.fresnel_scale_km(D.Band.from_table(QE, teff=5800).effective_nm, Z_AU) * 1000
print(f"slant range {Z_KM:g} km, shadow speed {V:g} km/s, Fresnel scale sqrt(lambda Z/2) = {f:.2f} m "
      f"= {f / V * 1000:.0f} microseconds")
for name, (mas, b) in STARS.items():
    print(f"  {name}: projected diameter {D.star_diameter_km(mas, Z_AU) * 1e5:.2f} cm")


def curve(geom, mas, band, exp, t):
    comp = D.Component(events=[(a / 1000 / V, b / 1000 / V) for a, b in geom],
                       diameter_km=D.star_diameter_km(mas, Z_AU), limb=0.6)
    return D.transmission(t, [comp], Z_AU, V, band=band, exposure=exp)


res = {}
for g, geom in GEOM.items():
    span = geom[-1][1] / 1000 / V
    print(f"\n{g}: geometric duration {span * 1e3:.2f} ms")
    for s, (mas, b) in STARS.items():
        row = []
        for exp in EXPOSURES:
            pad = max(exp, 2e-3)
            t = np.arange(-pad, span + pad, max(2e-7, exp / 400 if exp else 2e-7))
            y = curve(geom, mas, bands[b], exp, t)
            ew = np.trapezoid(1 - y, t) * 1e3              # ms of light missing
            row.append((exp, y.min(), y.max(), ew))
            res[(g, s, exp)] = (t, y)
        print(f"  {s}")
        for exp, lo, hi, ew in row:
            mag = -2.5 * math.log10(max(lo, 1e-6))
            print(f"    exposure {exp * 1e3:7.1f} ms: min {lo:6.3f} ({mag:5.2f} mag)  max {hi:5.3f}  "
                  f"light missing {ew:5.3f} ms")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, ax = plt.subplots(2, 2, figsize=(13, 8))
for col, g in enumerate(GEOM):
    a = ax[0, col]
    for s, c in zip(STARS, ("k", "tab:orange", "tab:red")):
        t, y = res[(g, s, 0.0)]
        a.plot(t * 1e3, y, c, lw=0.8, label=s)
    a.set_title(f"{g}: instantaneous (band-averaged)")
    a.set_xlabel("time (ms)"); a.set_ylabel("relative flux"); a.legend(fontsize=8); a.grid(alpha=.3)
    a = ax[1, col]
    s = list(STARS)[0]
    for exp, c in zip(EXPOSURES[1:], ("tab:blue", "tab:green", "tab:purple", "tab:brown", "tab:gray")):
        t, y = res[(g, s, exp)]
        a.plot(t * 1e3, y, c, lw=1, label=f"exposure {exp * 1e3:g} ms")
    a.set_xlim(-40, 45); a.set_title(f"{g}: sampled by the camera (any star)")
    a.set_xlabel("time (ms)"); a.set_ylabel("relative flux"); a.legend(fontsize=8); a.grid(alpha=.3)
fig.suptitle(f"Hypothetical Starlink occultation: range {Z_KM:g} km, {V:g} km/s, Fresnel scale {f:.2f} m "
             "(PyStarLight diffraction bar model)")
fig.tight_layout()
out = os.path.join(HERE, "figures", "starlink_occultation.png")
fig.savefig(out, dpi=110)
print("\nplot:", out)
