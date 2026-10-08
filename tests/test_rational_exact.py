"""Strict caller time parsing and exact rendering, shared by keyframes and curves."""

from fractions import Fraction

import pytest

from fcpxml.rational import (
    format_exact_seconds,
    format_seconds,
    parse_seconds,
    parse_strict_seconds,
)


@pytest.mark.parametrize("text,expected", [
    ("0s", Fraction(0)), ("12s", Fraction(12)),
    ("1001/30000s", Fraction(1001, 30000)), ("2/4s", Fraction(1, 2)),
])
def test_strict_seconds_reads_rational_seconds(text, expected):
    assert parse_strict_seconds(text) == expected


@pytest.mark.parametrize("value", [
    "-1s", "1.5s", "1", "", " 1s", "1s ", "1s\n", "1/0s", "1/s", "s", None, 1, Fraction(1),
])
def test_strict_seconds_rejects_anything_else(value):
    with pytest.raises(ValueError):
        parse_strict_seconds(value)


def test_lenient_parse_still_reads_what_final_cut_writes():
    assert parse_seconds("1.5s") == Fraction(3, 2)
    assert parse_seconds("") == 0


@pytest.mark.parametrize("value,text", [
    (Fraction(0), "0s"), (Fraction(12), "12s"),
    (Fraction(1001, 30000), "1001/30000s"), (Fraction(1, 50), "1/50s"),
])
def test_exact_seconds_round_trip(value, text):
    assert format_exact_seconds(value) == text
    assert parse_strict_seconds(text) == value


def test_exact_format_does_not_snap_to_the_frame_grid():
    assert format_seconds(Fraction(1, 50), Fraction(1, 25)) == "1/25s"
    assert format_exact_seconds(Fraction(1, 50)) == "1/50s"
