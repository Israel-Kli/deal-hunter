"""Tests for scoring with structured override fields."""

from __future__ import annotations

from deal_hunter.models import Listing
from deal_hunter.scoring.heuristic import score_listing


def _listing(**kw) -> Listing:
    base = dict(
        source="yad2",
        source_id="sc1",
        url="",
        city="תל אביב",
        price=1_500_000,
        sqm=60,
        rooms=3,
        description="",
        is_agent=False,
    )
    base.update(kw)
    return Listing(**base)


def test_score_with_units_override():
    l = _listing(units_count=3, units_count_user=3)
    sc, reasons = score_listing(l)
    assert reasons["description_unit_hit"] is True
    assert reasons["units_count_used"] == 3
    assert reasons["description_unit_source"] == "structured"
    assert reasons["description_unit_adjustment"] >= 0.9


def test_score_falls_back_to_description_when_units_null():
    l = _listing(description="3 יחידות דיור")
    sc, reasons = score_listing(l)
    assert reasons["description_unit_hit"] is True
    assert reasons["description_unit_source"] == "description"


def test_score_with_lot_sqm_override():
    l = _listing(lot_sqm=250, lot_sqm_user=250)
    sc, reasons = score_listing(l)
    assert reasons.get("lot_sqm_used") == 250
    assert reasons.get("garden_bonus_source") == "structured"
    assert reasons.get("garden_bonus", 0) > 0


def test_score_with_garden_sqm_override():
    l = _listing(garden_sqm=80, garden_sqm_user=80)
    sc, reasons = score_listing(l)
    assert reasons.get("garden_sqm_used") == 80
    assert reasons.get("garden_bonus_source") == "structured"


def test_score_no_structured_falls_back_to_phrase():
    l = _listing(description="גינה גדולה")
    sc, reasons = score_listing(l)
    assert reasons.get("garden_bonus_source") == "description"


def test_score_uses_effective_price_per_sqm():
    l = _listing(price=2_000_000, sqm=100, sqm_user=80)
    sc, reasons = score_listing(l)
    assert reasons.get("price_per_sqm_used") == 25000  # 2M/80
