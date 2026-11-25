"""Make src/pystarlight/capture/star_names.json: proper star names (IAU WGSN and others SIMBAD
knows as 'NAME ...') with their ICRS J2000 position, proper motion and V magnitude, for target
look-ups offline (capture/starnames.py).

    uv run python scripts/make_star_names.py [--vmax 13]

Data: SIMBAD (CDS, Strasbourg; the CfA mirror is asked first). Stars only (object types with
'*', not clusters, associations or planets), V < vmax, names without digits or '*' (those are
catalogue designations, which the look-ups take as they are).
"""
import argparse
import csv
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

TAP = ("https://simbad.cfa.harvard.edu/simbad/sim-tap/sync",
       "https://simbad.cds.unistra.fr/simbad/sim-tap/sync")
QUERY = ("SELECT i.id, b.main_id, b.ra, b.dec, b.pmra, b.pmdec, b.otype, f.V "
         "FROM ident AS i JOIN basic AS b ON i.oidref = b.oid "
         "LEFT JOIN allfluxes AS f ON f.oidref = b.oid WHERE i.id LIKE 'NAME %'")
NOT_STARS = {"Cl*", "As*", "St*", "SN*", "Pl", "Pl?", "MGr", "C?*", "Cl?"}
# candidates of star types (no '*' in the code): Aldebaran is 'LP?'
STAR_CANDIDATES = {f"{c}?" for c in ("LP", "RG", "AB", "HB", "WD", "BS", "RR", "Ce", "WR", "Be", "Ae",
                                     "pA", "RB", "sg", "EB", "SB", "CV", "XB", "Ir", "Mi", "sv", "BY",
                                     "RS", "gD", "dS", "Ro", "El", "HS", "BD", "Er", "s*r", "s*y", "s*b")}
NOT_NAMES = re.compile(r"\d|\*|nebula|group|stream|cluster|association|moving", re.I)
OUT = Path(__file__).resolve().parents[1] / "src/pystarlight/capture/star_names.json"


def fetch():
    data = urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv",
                                   "QUERY": QUERY}).encode()
    err = None
    for url in TAP:
        try:
            with urllib.request.urlopen(url, data, timeout=300) as r:
                text = r.read().decode()
            if text.startswith("id,"):
                return list(csv.DictReader(io.StringIO(text)))
            err = text[:200]
        except Exception as e:
            err = e
    raise SystemExit(f"SIMBAD not reached: {err}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vmax", type=float, default=13.0)
    a = ap.parse_args()
    stars = {}
    for row in fetch():
        name = row["id"][5:].strip()
        ot = row["otype"]
        star = ("*" in ot or ot in STAR_CANDIDATES) and ot not in NOT_STARS
        if not star or NOT_NAMES.search(name) or not row["ra"]:
            continue
        v = float(row["V"]) if row["V"] else None
        if v is None and ot != "**" or v is not None and v >= a.vmax:
            continue
        main_id = re.sub(r"^(\*|NAME)\s+", "", row["main_id"]).strip()
        f = (lambda s: round(float(s), 3) if s else 0.0)
        entry = [round(float(row["ra"]), 7), round(float(row["dec"]), 7), f(row["pmra"]),
                 f(row["pmdec"]), v, re.sub(r"\s+", " ", main_id)]
        if name not in stars or (v is not None and (stars[name][4] or 99) > v):
            stars[name] = entry
    OUT.write_text(json.dumps({
        "about": "name: [ra_deg, dec_deg (ICRS J2000), pmra, pmdec (mas/yr), V, SIMBAD main id]; "
                 "SIMBAD (CDS), scripts/make_star_names.py", "stars": dict(sorted(stars.items()))},
        indent=0, ensure_ascii=False) + "\n")
    print(f"{len(stars)} names -> {OUT}")


if __name__ == "__main__":
    main()
