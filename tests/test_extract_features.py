"""Tests for description-based feature extraction."""

from __future__ import annotations

from deal_hunter.models import Listing
from deal_hunter.normalize.extract_features import extract_features


def _listing(desc: str = "", **kw) -> Listing:
    base = dict(
        source="yad2",
        source_id="ext1",
        url="https://example.com/ext1",
        price=1_000_000,
    )
    base.update(kw)
    base["description"] = desc
    return Listing(**base)


def test_units_count_extracted():
    l = _listing("דירת 5 חדרים, 3 יחידות דיור")
    extract_features(l)
    assert l.units_count == 3


def test_units_count_singular():
    l = _listing("יחידת דיור אחת")
    extract_features(l)
    assert l.units_count == 1


def test_garden_sqm_extracted():
    l = _listing("גינה 80 מ״ר, חניה")
    extract_features(l)
    assert l.garden_sqm == 80


def test_garden_with_chet_variant():
    l = _listing("גינה 120 מ״ר")
    extract_features(l)
    assert l.garden_sqm == 120


def test_yard_extracted():
    l = _listing("חצר 50 מ\"ר")
    extract_features(l)
    assert l.garden_sqm == 50


def test_no_false_positive():
    l = _listing("דירה נחמדה עם מטבח גדול")
    extract_features(l)
    assert l.units_count is None
    assert l.garden_sqm is None


def test_tags_also_searched():
    l = Listing(
        source="yad2",
        source_id="ext_tags",
        url="https://example.com/tags",
        price=1_000_000,
        description="",
        tags=["3 יחידות דיור", "גינה 200 מ״ר"],
    )
    extract_features(l)
    assert l.units_count == 3
    assert l.garden_sqm == 200
