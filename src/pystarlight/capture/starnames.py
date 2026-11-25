"""Proper star names as targets, offline: 'Polaris', 'Atlas', "Herschel's Garnet Star", also
SIMBAD's main designations of those stars ('alf UMi', 'alpha UMi', '27 Tau', 'zet UMa').

NEW CODE. The table (star_names.json, made by scripts/make_star_names.py from SIMBAD) holds
ICRS J2000 positions with proper motions; position() moves them to the epoch. The catalogue
name (UCAC4, else TYC, else Gaia DR3) is found by wcs/catnames.resolve_target at that position.
Stdlib only (the camera runs with the Pi's system Python).
"""
import json
import math
import os
import re
from datetime import datetime, timezone

GREEK = {"alpha": "alf", "beta": "bet", "gamma": "gam", "delta": "del", "epsilon": "eps",
         "zeta": "zet", "eta": "eta", "theta": "tet", "iota": "iot", "kappa": "kap",
         "lambda": "lam", "mu": "mu", "nu": "nu", "xi": "ksi", "omicron": "omi", "pi": "pi",
         "rho": "rho", "sigma": "sig", "tau": "tau", "upsilon": "ups", "phi": "phi", "chi": "chi",
         "psi": "psi", "omega": "ome"}
# a designation with several names in SIMBAD: the IAU (WGSN) one, else the shortest
PREFERRED = {"alf UMi": "Polaris", "bet Cen": "Hadar", "eta UMa": "Alkaid", "bet01 Cyg": "Albireo",
             "alf And": "Alpheratz", "lam Ori": "Meissa", "ksi Per": "Menkib", "q Tau": "Taygeta",
             "alf Cen A": "Rigil Kentaurus", "16 Tau": "Celaeno", "bet Per": "Algol", "alf Gem": "Castor",
             "omi Cet": "Mira", "alf CMi": "Procyon", "alf CMa": "Sirius", "bet01 Cap": "Dabih",
             "ksi Pup": "Azmidiske", "mu. Cep": "Herschel's Garnet Star"}
_table = None


def key(text):
    """'Alpha  UMi' -> 'alfumi', "Barnard's Star" -> 'barnardsstar', 'mu. Cep' -> 'mucep'."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return "".join(GREEK.get(w, w) for w in words)


def table():
    """{key: (name, entry)} for the names and the main designations (names win)."""
    global _table
    if _table is None:
        path = os.path.join(os.path.dirname(__file__), "star_names.json")
        try:
            with open(path, encoding="utf-8") as f:
                stars = json.load(f)["stars"]
        except (OSError, ValueError, KeyError):
            stars = {}
        t = {}
        for name, e in sorted(stars.items(), key=lambda ne: (PREFERRED.get(ne[1][5]) != ne[0], len(ne[0]))):
            t.setdefault(key(e[5]), (name, e))
        for name, e in stars.items():
            t[key(name)] = (name, e)
        _table = t
    return _table


def lookup(text):
    """{'name', 'designation', 'ra', 'dec' (J2000), 'pmra', 'pmdec' (mas/yr), 'mag'} of a named
    star, None if the text is no name in the table."""
    k = key(text)
    hit = table().get(k) if k else None
    if hit is None:
        return None
    name, (ra, dec, pmra, pmdec, v, main_id) = hit
    return {"name": name, "designation": main_id, "ra": ra, "dec": dec, "pmra": pmra,
            "pmdec": pmdec, "mag": v}


def decimal_year(when=None):
    when = when or datetime.now(timezone.utc)
    start = datetime(when.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(when.year + 1, 1, 1, tzinfo=timezone.utc)
    return when.year + (when - start).total_seconds() / (end - start).total_seconds()


def position(star, epoch=None):
    """(ra, dec) deg of a lookup() result at a decimal year (default now): J2000 + proper motion
    (pmra includes cos dec, as SIMBAD's). Linear: < 0.1" in 30 years but for the fastest stars."""
    years = (epoch if epoch is not None else decimal_year()) - 2000.0
    dec = star["dec"] + star["pmdec"] * years / 3.6e6
    cosd = math.cos(math.radians(star["dec"]))
    ra = star["ra"] + (star["pmra"] * years / 3.6e6 / cosd if cosd > 1e-6 else 0.0)
    return ra % 360.0, max(-90.0, min(90.0, dec))
