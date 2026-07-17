"""Yad2 `dates` block extraction + propagation to Listing fields."""

from __future__ import annotations

from deal_hunter.adapters.yad2 import (
    Yad2Adapter,
    _apply_gw_item,
    _apply_json_enrichment,
    _extract_dates,
    _gw_item_description,
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


# ---- gw /realestate-item/{token} enrichment (the live map-feed path) ----------


def _gw_detail(**overrides):
    """A gw item `data` object, shaped like a real /realestate-item response."""
    base = {
        "token": "abc123",
        "adType": "private",
        "price": 2_500_000,
        "furnitureInfo": "",
        "additionalDetails": {
            "roomsCount": 5,
            "squareMeter": 120,
            "squareMeterBuild": 130,
            "parkingSpacesCount": 1,
            "balconiesCount": 1,
            "propertyCondition": {"id": 6, "text": "חדש (גרו בנכס)"},
        },
        "inProperty": {
            "includeElevator": True,
            "includeParking": True,
            "includeSecurityRoom": True,
            "includeBalcony": True,
            "includeAirconditioner": True,
            "isRenovated": True,
        },
        "metaData": {"description": "דירה מהממת, מרווחת ומאווררת. נוף פתוח לצד צפון מזרח."},
        "dates": {
            "createdAt": "2026-06-10T15:10:37",
            "updatedAt": "2026-06-11T10:52:56",
            "endsAt": "2026-07-20T00:00:00",
            "rebouncedAt": "2026-06-10T15:11:00",
        },
    }
    base.update(overrides)
    return base


def test_gw_item_description_uses_furniture_info_when_metadata_is_seo():
    # Some ads: metaData.description is a short auto-generated SEO string and the
    # real body lives in furnitureInfo. Longest-wins picks the real body.
    item = {
        "furnitureInfo": "למכירה דירת 4 חדרים בשכונת בית בפארק היפהפייה, נכס חדש, קומה 5 מתוך 9.",
        "metaData": {"description": "מכירה, דירה, קומה 5, אור יהודה"},
    }
    assert _gw_item_description(item) == item["furnitureInfo"]


def test_gw_item_description_prefers_metadata_over_short_furniture_note():
    # The common shape: metaData.description is the body, furnitureInfo a short note.
    item = {
        "furnitureInfo": "ניתן לרכוש עם הריהוט ומוצרי חשמל",
        "metaData": {"description": "דירה יפה ומשופצת, נוף פתוח, 4 חדרים, ממד, לא בשבת!"},
    }
    assert _gw_item_description(item) == item["metaData"]["description"]


def test_gw_item_description_empty_when_no_candidates():
    assert _gw_item_description({}) == ""
    assert _gw_item_description({"metaData": {}}) == ""


def test_apply_gw_item_fills_dates_description_and_amenities():
    adapter = Yad2Adapter(cities=[CITY], search=SEARCH)
    # Marker baseline carries no dates and no description (map feed omits them).
    listing, _ = adapter._parse(_item(dates={}), CITY)
    assert listing.created_at == ""
    assert listing.description == ""

    _apply_gw_item(listing, _gw_detail())

    assert listing.created_at == "2026-06-10"
    assert listing.updated_at == "2026-06-11"
    assert listing.ends_at == "2026-07-20"
    assert listing.rebounced_at == "2026-06-10"
    assert listing.publish_date == "2026-06-10"
    assert listing.first_listed_date == "2026-06-10"
    assert "דירה מהממת" in listing.description
    assert listing.elevator and listing.parking and listing.balcony
    assert listing.ac and listing.mamad and listing.renovated
    # sqm_build filled from detail (marker had none) → price_per_sqm recomputed.
    assert listing.sqm_build == 130
    assert listing.price_per_sqm == round(2_500_000 / 130)


def test_apply_gw_item_does_not_clear_marker_amenities_on_missing_flags():
    adapter = Yad2Adapter(cities=[CITY], search=SEARCH)
    listing, _ = adapter._parse(_item(dates={}), CITY)
    listing.parking = True  # e.g. set from a marker tag
    _apply_gw_item(listing, _gw_detail(inProperty={}, additionalDetails={}))
    assert listing.parking is True  # never flipped back off
