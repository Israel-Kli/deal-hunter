"""Golden-fixture tests for the Simplestate adapter."""

from __future__ import annotations

import json
from pathlib import Path

from deal_hunter.adapters.simplestate import SimplestateAdapter, _extract_built_sqm, _extract_lot_sqm, _extract_garden_sqm

FIXTURE = Path(__file__).parent / "fixtures" / "simplestate_feed_877.json"

SEARCH = {
    "rooms_min": 1.0,
    "rooms_max": 20.0,
    "price_min": 100_000,
    "price_max": 100_000_000,
    "min_sqm": 20,
    "max_listing_age_days": 100_000,
    "exclude_ground_floor": False,
}


def _parsed_listings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items = data.get("data") or []
    adapter = SimplestateAdapter(business_ids=[877], search=SEARCH, request_delay_sec=0)
    listings = []
    for raw in items:
        if isinstance(raw, dict):
            listing, _reason = adapter._parse(raw, 877)
            if listing is not None:
                listings.append(listing)
    return listings


def test_simplestate_parses_fixture_items():
    listings = _parsed_listings()
    assert len(listings) >= 15, f"expected >=15, got {len(listings)}"


def test_simplestate_first_listing_core_fields():
    listings = _parsed_listings()
    first = listings[0]
    assert first.source == "simplestate"
    assert first.source_id, "source_id must be non-empty"
    assert first.price > 0
    assert first.city
    assert first.url.startswith("https://www.simplestate.me/business-view/")
    assert first.description, "description must be populated"


def test_simplestate_all_have_city():
    listings = _parsed_listings()
    for listing in listings:
        assert listing.city, f"{listing.source_id} missing city"


def test_simplestate_most_have_rooms():
    listings = _parsed_listings()
    with_rooms = [l for l in listings if l.rooms is not None]
    assert len(with_rooms) > len(listings) * 0.7, f"only {len(with_rooms)} of {len(listings)} have rooms"


def test_simplestate_private_house_types():
    listings = _parsed_listings()
    house_types = {"דו משפחתי", "בית פרטי", "וילה", "קוטג'"}
    house_listings = [l for l in listings if l.listing_type in house_types]
    # At least some should be houses
    assert len(house_listings) >= 1, f"no house-type listings found in {len(listings)}"


def test_simplestate_tight_filter_price():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items = data.get("data") or []
    adapter = SimplestateAdapter(
        business_ids=[877],
        search={**SEARCH, "price_max": 50_000},
        request_delay_sec=0,
    )
    parsed = 0
    for raw in items:
        if isinstance(raw, dict):
            listing, _reason = adapter._parse(raw, 877)
            if listing is not None:
                parsed += 1
    assert parsed == 0, f"all should be filtered, got {parsed}"


def test_extract_built_sqm():
    assert _extract_built_sqm('דירה שטח 90 מ"ר') == 90
    assert _extract_built_sqm("שטח בנוי 138 מ\"ר מפואר") == 138
    assert _extract_built_sqm("שטח בנוי של 193 מ״ר") == 193
    assert _extract_built_sqm("180 מ״ר בנוי על מגרש 445") == 180
    assert _extract_built_sqm("כ־ 30 מ״ר בנוי") == 30
    assert _extract_built_sqm("בנוי 55 מ\"ר") == 55
    assert _extract_built_sqm("בנוי של 120 מ\"ר") == 120
    assert _extract_built_sqm("אין שטח") is None
    assert _extract_built_sqm("מגרש של 607 מ״ר") is None
    assert _extract_built_sqm("גינה 80 מ\"ר") is None


def test_extract_lot_sqm():
    assert _extract_lot_sqm("מגרש 500 מ\"ר") == 500
    assert _extract_lot_sqm("אין מגרש") is None


def test_extract_garden_sqm():
    assert _extract_garden_sqm("גינה ענקית של 120 מ\"ר") == 120
    assert _extract_garden_sqm("חצר 80 מ\"ר מטופחת") == 80
    assert _extract_garden_sqm("אין גינה") is None


def test_extract_built_not_captured_by_other():
    """Verify built sqm regex doesn't capture lot or garden numbers."""
    assert _extract_built_sqm("מגרש 500 מ\"ר") is None
    assert _extract_built_sqm("גינה 120 מ\"ר") is None
    assert _extract_built_sqm("חצר 80 מ\"ר") is None


def test_detail_enrichment_reads_structured_fields():
    """fetch_detail should read size/field_size/garden_size from API response."""
    from unittest.mock import patch

    from deal_hunter.adapters.simplestate import SimplestateAdapter
    from deal_hunter.models import Listing

    listing = Listing(
        source="simplestate",
        source_id="123",
        url="https://www.simplestate.me/business-view/877/real_estate/property/123",
        city="תל אביב",
        price=2_000_000,
        description="תיאור מקורי",
        is_agent=True,
        source_payload={"_business_id": 877, "_property_id": 123},
    )

    detail_response = {
        "body": {
            "data": {
                "size": 120,
                "field_size": 500,
                "garden_size": 80,
                "description": "תיאור מפורט",
                "parking_spaces": 1,
                "floor": 2,
                "elevator": True,
                "ac": True,
                "balcony": True,
                "renovated": True,
            }
        }
    }

    adapter = SimplestateAdapter(business_ids=[877], search=SEARCH, request_delay_sec=0)

    with patch("deal_hunter.adapters.simplestate.fetch", return_value=detail_response):
        result = adapter.fetch_detail(listing)

    assert result.sqm == 120
    assert result.lot_sqm == 500
    assert result.garden_sqm == 80
    assert result.price_per_sqm == round(2_000_000 / 120)
    assert result.floor == 2
    assert result.elevator is True
    assert result.ac is True
    assert result.balcony is True
    assert result.renovated is True
    assert result.parking is True
    assert result.description == "תיאור מקורי"  # Keeps original if already set
