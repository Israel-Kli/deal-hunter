"""Yad2 `dates` block extraction + propagation to Listing fields."""

from __future__ import annotations

from deal_hunter.adapters.yad2 import (
    Yad2Adapter,
    _apply_json_enrichment,
    _extract_dates,
)

CITY = {
    "name": "Or Yehuda",
    "city_code": "2400",
    "slug": "or-yehuda",
    "hebrew_name": "אור יהודה",
    "yad2_region": "center-and-sharon",
}
SEARCH = {
    "rooms_min": 2.0,
    "rooms_max": 10,
    "price_min": 1_000_000,
    "price_max": 10_000_000,
    "min_sqm": 0,
    "max_listing_age_days": 365 * 10,
}


def _item(**overrides):
    base = {
        "token": "abc123",
        "additionalDetails": {"roomsCount": 5, "squareMeter": 120},
        "address": {
            "house": {"floor": 2, "number": 7},
            "street": {"text": "הרצל"},
            "neighborhood": {"text": ""},
            "city": {"text": "אור יהודה"},
            "coords": {"lat": 32.02549, "lon": 34.863672},
        },
        "price": 2_500_000,
        "metaData": {"images": [], "coverImage": ""},
        "tags": [],
        "dates": {
            "createdAt": "2026-06-10T15:10:37",
            "updatedAt": "2026-06-11T10:52:56",
            "endsAt": "2026-07-20T00:00:00",
            "rebouncedAt": "2026-06-10T15:11:00",
        },
    }
    base.update(overrides)
    return base


def test_extract_dates_maps_camel_to_snake_and_slices():
    out = _extract_dates(_item())
    assert out == {
        "created_at": "2026-06-10",
        "updated_at": "2026-06-11",
        "ends_at": "2026-07-20",
        "rebounced_at": "2026-06-10",
    }


def test_extract_dates_missing_block_returns_empty():
    assert _extract_dates({}) == {}
    assert _extract_dates({"dates": None}) == {}
    assert _extract_dates({"dates": "string-not-dict"}) == {}


def test_extract_dates_partial_block_keeps_what_exists():
    out = _extract_dates({"dates": {"createdAt": "2026-06-10T15:10:37"}})
    assert out == {"created_at": "2026-06-10"}


def test_extract_dates_skips_non_string_values():
    out = _extract_dates({"dates": {"createdAt": 123, "updatedAt": "2026-06-11T10:52:56"}})
    assert out == {"updated_at": "2026-06-11"}


def test_parse_uses_created_at_for_publish_date():
    adapter = Yad2Adapter(cities=[CITY], search=SEARCH)
    listing, reason = adapter._parse(_item(), CITY)
    assert reason is None
    assert listing is not None
    assert listing.publish_date == "2026-06-10"
    assert listing.first_listed_date == "2026-06-10"
    assert listing.created_at == "2026-06-10"
    assert listing.updated_at == "2026-06-11"
    assert listing.ends_at == "2026-07-20"
    assert listing.rebounced_at == "2026-06-10"


def test_parse_falls_back_to_image_heuristic_when_no_dates():
    item = _item(dates={})
    item["metaData"]["images"] = ["https://img.yad2.co.il/Pic/2026/04/03/foo.jpeg"]
    adapter = Yad2Adapter(cities=[CITY], search=SEARCH)
    listing, reason = adapter._parse(item, CITY)
    assert reason is None
    assert listing is not None
    assert listing.publish_date == "2026-04-03"
    assert listing.first_listed_date == "2026-04-03"
    assert listing.created_at == ""
    assert listing.updated_at == ""
    assert listing.ends_at == ""
    assert listing.rebounced_at == ""


def test_apply_json_enrichment_overrides_dates():
    adapter = Yad2Adapter(cities=[CITY], search=SEARCH)
    list_item = _item()
    list_item["dates"] = {"createdAt": "2026-05-01T00:00:00"}
    listing, _ = adapter._parse(list_item, CITY)
    assert listing.created_at == "2026-05-01"
    detail_item = {
        "description": "מודעה משופרת עם כל הפרטים",
        "dates": {
            "createdAt": "2026-06-10T15:10:37",
            "updatedAt": "2026-06-11T10:52:56",
            "endsAt": "2026-07-20T00:00:00",
            "rebouncedAt": "2026-06-10T15:11:00",
        },
    }
    _apply_json_enrichment(listing, detail_item)
    assert listing.created_at == "2026-06-10"
    assert listing.updated_at == "2026-06-11"
    assert listing.ends_at == "2026-07-20"
    assert listing.rebounced_at == "2026-06-10"
    assert listing.publish_date == "2026-06-10"
    assert listing.first_listed_date == "2026-06-10"
