"""Tests for Hebrew text normalization."""

from __future__ import annotations

import pytest

from deal_hunter.normalize.hebrew import (
    canonicalize_address,
    extract_street_number,
    normalize_hebrew,
    rooms_bucket,
    sqm_bucket,
    strip_niqqud,
)


# ── strip_niqqud ──────────────────────────────────────────────────────


def test_strip_niqqud_removes_vowel_points():
    # "בֵּית" with niqqud → "בית"
    assert strip_niqqud("בֵּית") == "בית"


def test_strip_niqqud_preserves_plain_hebrew():
    assert strip_niqqud("רחוב הרצל") == "רחוב הרצל"


def test_strip_niqqud_handles_mixed():
    assert strip_niqqud("דִּירָה 12") == "דירה 12"


# ── normalize_hebrew ──────────────────────────────────────────────────


def test_normalize_expands_abbreviations():
    assert normalize_hebrew("רח' הרצל") == "רחוב הרצל"


def test_normalize_collapses_whitespace():
    assert normalize_hebrew("רחוב   הרצל   12") == "רחוב הרצל 12"


def test_normalize_handles_apostrophe_variants():
    # Various apostrophe/quote forms in "ממ"ד"
    assert normalize_hebrew('ממ"ד') == normalize_hebrew("ממ״ד")


def test_normalize_empty_string():
    assert normalize_hebrew("") == ""
    assert normalize_hebrew(None) == ""


# ── canonicalize_address ──────────────────────────────────────────────


def test_canonicalize_drops_street_prefix():
    # "רחוב" is a stopword, should be removed
    assert "רחוב" not in canonicalize_address("רחוב הרצל")


def test_canonicalize_converges_variants():
    """Different renderings of same address should converge."""
    a = canonicalize_address("רח' הרצל 12")
    b = canonicalize_address("רחוב הרצל  12")
    c = canonicalize_address("הרצל 12")
    assert a == b == c, f"Expected convergence: {a!r} vs {b!r} vs {c!r}"


def test_canonicalize_preserves_numbers():
    result = canonicalize_address("הרצל 12")
    assert "12" in result


def test_canonicalize_drops_listing_type_words():
    result = canonicalize_address("דירות למכירה הרצל 12")
    assert "דירות" not in result
    assert "למכירה" not in result


# ── extract_street_number ─────────────────────────────────────────────


def test_extract_simple():
    street, number = extract_street_number("הרצל 12")
    assert street == "הרצל"
    assert number == "12"


def test_extract_with_prefix():
    street, number = extract_street_number("רחוב הרצל 24")
    assert street == "רחוב הרצל"
    assert number == "24"


def test_extract_with_letter():
    street, number = extract_street_number("ביאליק 24א")
    assert street == "ביאליק"
    assert number == "24א"


def test_extract_no_number():
    street, number = extract_street_number("רחוב בלי מספר")
    assert "רחוב בלי מספר" in street
    assert number == ""


def test_extract_empty():
    street, number = extract_street_number("")
    assert street == ""
    assert number == ""


# ── buckets ───────────────────────────────────────────────────────────


def test_sqm_bucket():
    assert sqm_bucket(73, 10) == "70"
    assert sqm_bucket(70, 10) == "70"
    assert sqm_bucket(69, 10) == "60"
    assert sqm_bucket(None, 10) == ""


def test_rooms_bucket():
    assert rooms_bucket(4.3) == "4.5"
    assert rooms_bucket(4.0) == "4.0"
    assert rooms_bucket(3.7) == "3.5"
    assert rooms_bucket(None) == ""
