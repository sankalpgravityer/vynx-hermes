"""A two-language size label counts as its letter size (17 Oct 2026, the user)."""
import pytest

from app.llm import care_label as cl


@pytest.mark.parametrize("label,size", [("S/P", "S"), ("s / p", "S"), ("M/M", "M"), ("L/G", "L"),
                                        ("XL/TG", "XL"), ("XS/TP", "XS"), ("XXL/TTG", "XXL")])
def test_two_language_labels(label, size):
    assert cl.two_language_size(label) == size


@pytest.mark.parametrize("label", ["S/M", "M/L", "30/32", "W32/L34", "S", "36/38", "L/XL"])
def test_ranges_and_plain_sizes_are_left_alone(label):
    assert cl.two_language_size(label) == label


def test_vin_000179_reading_now_passes_the_chart():
    ladder = ["XS", "S", "M", "L", "XL"]
    assert not cl._size_ok("S/P", ladder)
    assert cl._size_ok(cl.two_language_size("S/P"), ladder)
