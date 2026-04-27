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
    assert _extract_built_sqm("דירה שטח 90 מ\"ר") == 90
    assert _extract_built_sqm("שטח בנוי 138 מ\"ר מפואר") == 138
    assert _extract_built_sqm("אין שטח") is None


def test_extract_lot_sqm():
    assert _extract_lot_sqm("מגרש 500 מ\"ר") == 500
    assert _extract_lot_sqm("אין מגרש") is None


def test_extract_garden_sqm():
    assert _extract_garden_sqm("גינה ענקית של 120 מ\"ר") == 120
    assert _extract_garden_sqm("חצר 80 מ\"ר מטופחת") == 80
    assert _extract_garden_sqm("אין גינה") is None
