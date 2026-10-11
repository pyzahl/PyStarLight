"""Locate the target star: nova.astrometry.net plate solving or a two-reference-star solution."""

from .solve import (
    Orientation,
    TwoStarSolution,
    field_stars,
    load_wcs,
    orientation,
    plate_scale,
    prepare_for_astrometry,
    sky_coord,
    solve_astrometry_net,
    solve_movie,
    solve_two_stars,
    stack_frames,
    target_pixel,
    ucac4_position,
)

__all__ = [
    "Orientation",
    "TwoStarSolution",
    "field_stars",
    "load_wcs",
    "orientation",
    "plate_scale",
    "prepare_for_astrometry",
    "sky_coord",
    "solve_astrometry_net",
    "solve_movie",
    "solve_two_stars",
    "stack_frames",
    "target_pixel",
    "ucac4_position",
]
