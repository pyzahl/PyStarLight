# Diffraction-aware light curves (`pystarlight.diffraction`)

Fitting occultation light curves with a model that includes:

- Fresnel diffraction at the shadow edges;
- the star's finite, limb-darkened disk;
- **double stars**;
- the camera's spectral bandpass;
- the exposure time.

It is an option of the analyzer: `steps.py` (sharp steps) stays the default, and the diffraction fit
runs after it with `[diffraction] mode = "auto"` or `"on"` (viewer button **diffr: off / auto / on**).

## When it matters

The Fresnel scale is f = sqrt(λZ/2). It is about 0.3 km for a main-belt asteroid at 2.3 AU and about
1.4 km at 40 AU. A star of 0.2 mas projects to about 0.34 km at 2.3 AU. At typical shadow speeds of
5–15 km/s these lengths last 0.02–0.07 s, a sizeable fraction of a 0.1 s video frame. A sharp-step fit
therefore biases sub-frame D/R times, and it cannot interpret the stepped D/R of a double star.

## Model

- **Bar model (1-D, exact):** every occultation is an opaque strip between its D and R edges,
  crossed at the perpendicular shadow speed `v_perp`. The field is written in Fresnel integrals.
  Several strips (a satellite, a second body) add in the field (Babinet). A long chord gives the
  knife edge: 0.25 at the geometric edge, first fringe 1.37.
- **Bandpass:** the intensities of the wavelengths are summed, **incoherently**. Light of different
  wavelengths does not interfere.
- **Star:** a disk with linear limb darkening, 1 − u(1 − μ). Its brightness integrated along the
  edge is used exactly.
- **Double star:** two components, each with its own D and R times, added in intensity with the
  primary's share of the light `f`. Each component has its own times because the separation
  projected on the edge normal differs between D and R. The fit gives the times and `f`. The
  separation and position angle follow from several chords (or a known edge geometry).
- **Exposure:** a box over the frame time. Times are mid-exposure (as `plot.seconds` in the viewer).
- **Sinc engine (2-D):** the field behind any silhouette, E = W S W, with W the symmetric Toeplitz
  matrix of Fresnel-integral weights. It handles irregular shapes and cross-checks the bar model
  (a long strip agrees to 0.5 %). Two limits: a sharp silhouette needs fine sampling (about 3–5 %
  error near the edges at dx = f/10, 0.5 % at f/30), and fringes are only reproduced within about
  λZ/(2·dx) of an edge (the band limit).

## In the analyzer

The config section `[diffraction]` sets the default; the button **diffr:** in the analyzer window (right
column, under *Fit frame*) cycles off → auto → on. With a result on screen, the new mode applies at
once.

- `mode = "off"` (default): the step fit only.
- `mode = "on"`: after every result plot, the diffraction fit of the step fit's main event (first D to
  last R) runs in the background, on the same flux with the frame interval as exposure.
- `mode = "auto"`: the same, but only when it matters. It runs when the step fit shows double-star
  steps (two D and two R), or when the Fresnel scale plus the star diameter, divided by the shadow
  speed, take at least 10 % of a frame. Otherwise the status line says why it was skipped.
- Older configs: `enabled = true` counts as `"on"`.
- The asteroid's distance and the shadow speed come from JPL Horizons, for the asteroid the
  recording names (network, extra `catalog`). The speed is the distance times the topocentric
  motion. `distance_au` and `shadow_speed_km_s` override them. The edges are taken perpendicular to
  the path.
- Further settings: `star_diameter_mas`, `limb_darkening`, `fit_star_diameter`, `double_star`
  (`"auto"` = when the step fit shows two D and two R steps), `wavelength_nm`, the optional
  `qe_table` and `star_teff`, and `mc_trials` (default 100; 0 = covariance errors only).
- Results:
  - `<movie>-diffraction.pdf/png`: the event with both models, a close-up of every D and R (data,
    sharp step, diffraction model, both times with 1 sigma), and a table of the step time, the
    diffraction time, their difference in ms, and the model settings;
  - `event.json` → `diffraction_fit`: UTC times with sigma, the step fit's times and the
    differences, levels, the double star's light share, settings and geometry;
  - a status line: `diffraction fit: D +13.0 ms, R -1.8 ms vs the step fit (...)`.

## Use in Python

```python
from pystarlight import diffraction as dif

band = dif.Band.monochromatic(600)              # default: one effective wavelength
# band = dif.Band.from_table('imx296_qe.txt', teff=5800)   # QE curve (nm, QE), optional star colour

fit = dif.fit_event(t, flux, distance_au=2.33, v_perp=5.1, exposure=0.1, band=band,
                    double=False, diameter_km=dif.star_diameter_km(0.2, 2.33))
print(fit.report())                              # D, R (or D1 R1 D2 R2) +- 1 sigma, levels, f, chi2
```

Free parameters:

- always: baseline, bottom, and the D and R times (per component for a double star);
- double star: the primary's share of the light `f`;
- optional: the star diameter (`fit_diameter=True`) and the perpendicular shadow speed
  (`fit_speed=True`).

Start values come from `steps.fit_steps`.

### Errors: Monte Carlo

`monte_carlo(fit, trials)` estimates how much the times would change with different noise:

1. Take the fitted model.
2. Simulate noise like the residuals: their rms and their frame-to-frame correlation (AR(1)
   coefficient φ; scintillation and video noise are often correlated).
3. Refit, `trials` times, each starting from the best fit.
4. The MC σ of each time is the 16–84 % half-width of the refitted values. This is robust, and it
   counts a step landing a frame off.

The quoted σ is the larger of the covariance σ (Jacobian, scaled by max(1, χ²/ν)) and the MC σ, as in
the step fit. The figure and `event.json` show both parts. 100 trials of a double-star fit take about
15 s, in the background.

### Double stars: components and the pairing ambiguity

`fit.components()` gives each component's D–R pairing, duration and chord (duration × shadow speed).
With nearly equal components (light share within max(3σ, 0.05) of 0.5) one light curve cannot tell
which R belongs to which D: swapping the R times gives the same curve. The fit then flags the pairing
as **ambiguous** and lists the other pairing's durations and chords. The asteroid's size, other
chords or a catalogue separation decide.

Example: (709) Fringilla, 2025-01-16, an equal-light double. The chords are 87 / 89 km, or 138 /
38 km with the other pairing. Fringilla is about 100 km across, so the first pairing is the physical
one: the two stars are about 3.6 s = 49 km apart along the track, about 32 mas at 2.12 au.

## Camera QE curves

They are optional. Without a table, a single effective wavelength is used (600 nm suits unfiltered
monochrome CMOS sensors). A table is a text file of `wavelength_nm  QE` rows (any QE scale; commas or
blanks; `#` comments). Curves are often only published as plots in datasheets. Digitise such a plot
(for example with WebPlotDigitizer) into a table. With `teff` the weights also follow the star's
photon spectrum (black body).

## Credits

The sinc engine is ported from Bob Anderson's (IOTA) notebooks, with his permission:

- [Sinc-based-fresnel-diffraction](https://github.com/bob-anderson-ok/Sinc-based-fresnel-diffraction)
  (the core of Occult4's Fresnel modelling and of GoPyOTE);
- the double-star approach follows his
  [IOTAdiffraction](https://github.com/bob-anderson-ok/IOTAdiffraction).

The method: M. Cubillos and E. Jimenez, *Diffraction integral computation using sinc approximation*,
Applied Numerical Mathematics 178 (2022), doi:10.1016/j.apnum.2022.02.011.

## Not yet

- Reading IOTAdiffraction / GoPyOTE JSON5 parameter files.
- The 2-D sinc fit for small bodies.
