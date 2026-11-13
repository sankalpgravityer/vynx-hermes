"""A child's size makes a kids product (13 Nov 2026, VIN-000421 / VIN-000313) — twin of
vnyx-api src/utils/kids-size.ts. No network, no database."""
import pytest

from app import readiness as r


@pytest.mark.parametrize("size,age", [
    ("10 (140 cm)", 10), ("10Y", 10), ("14Yrs / 164cm", 14), ("8-9 years", 9),
    ("14 ANS", 14), ("age 6", 6), ("92 cm", 2), ("140cm", 10)])
def test_child_sizes(size, age):
    assert r.kids_age_from_size(size) == age


@pytest.mark.parametrize("size", ["M", "XL (158-170 cm)", "176 cm", "W30 L32", "29", "EU 38", "One Size", "", None])
def test_adult_sizes(size):
    assert r.kids_age_from_size(size) is None


def test_is_kids_reads_the_size_but_not_on_shoes():
    assert r.is_kids("Women", "Bottom", None, size="10 (140 cm)", category="Trousers")
    assert not r.is_kids("Women", "Bottom", None, size="M", category="Trousers")
    assert not r.is_kids("Women", None, None, size="10Y", category="Shoes")
    assert not r.is_kids("Women", "Bottom", None)            # no size: as before


def test_is_kids_reads_every_size_field():
    # VIN-000421: column "M", label "10 (140 cm)".
    assert r.is_kids("Women", "Bottom", None, size=("M", None, "10 (140 cm)"), category="Shorts")
