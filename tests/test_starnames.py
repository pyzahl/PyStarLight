"""Proper star names as targets (capture/starnames.py, wcs/catnames.parse_target): offline."""
import pytest

from pystarlight.capture import starnames


def test_names_and_designations():
    p = starnames.lookup("polaris")
    assert p["name"] == "Polaris" and p["designation"] == "alf UMi"
    assert p["ra"] == pytest.approx(37.9546, abs=1e-3) and p["dec"] == pytest.approx(89.2641, abs=1e-3)
    assert starnames.lookup("Alpha UMi")["name"] == "Polaris"            # Greek spelled out
    assert starnames.lookup("alf umi")["name"] == "Polaris"
    assert starnames.lookup("Atlas")["designation"] == "27 Tau"
    assert starnames.lookup("27 Tau")["name"] == "Atlas"
    assert starnames.lookup("Aldebaran") is not None                     # SIMBAD type LP?
    assert starnames.lookup("herschels garnet star")["designation"] == "mu. Cep"
    assert starnames.lookup("UCAC4 897-000295") is None and starnames.lookup("") is None
    assert len(starnames.table()) > 600                                   # names + designations


def test_proper_motion():
    """Barnard's Star moves 10"/yr: J2000 + 26.9 years."""
    b = starnames.lookup("Barnard's Star")
    ra, dec = starnames.position(b, 2026.9)
    assert dec - b["dec"] == pytest.approx(b["pmdec"] * 26.9 / 3.6e6, rel=1e-9)
    assert abs(dec - b["dec"]) * 3600 > 250
    assert starnames.position(b, 2000.0) == pytest.approx((b["ra"], b["dec"]))


def test_catnames_parses_names():
    pytest.importorskip("astropy")
    from pystarlight.wcs.catnames import parse_target
    kind, star = parse_target("Vega")
    assert kind == "name" and star["designation"] == "alf Lyr"
    assert parse_target("UCAC4 634-113397") == ("ucac4", "634-113397")
    with pytest.raises(ValueError, match="star's name"):
        parse_target("no such star here")
