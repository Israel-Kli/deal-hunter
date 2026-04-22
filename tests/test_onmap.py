"""Golden-fixture tests for the OnMap adapter."""

from __future__ import annotations

import json
from pathlib import Path

from deal_hunter.adapters.onmap import OnMapAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "onmap_feed_telaviv.json"

# Wide filters to let most fixture items through; age filter effectively disabled.
SEARCH = {
    "rooms_min": 1.0,
    "rooms_max": 10.0,
    "price_min": 100_000,
    "price_max": 100_000_000,
    "min_sqm": 20,
    "max_listing_age_days": 100_000,
    "exclude_ground_floor": False,
}


def _parsed_listings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    adapter = OnMapAdapter(city_slugs=["tel-aviv-yafo"], search=SEARCH)
    listings = []
    for raw in data["data"]:
        listing, _reason = adapter._parse(raw, "tel-aviv-yafo")
        if listing is not None:
            listings.append(listing)
    return listings


def test_onmap_parses_fixture_items():
    listings = _parsed_listings()
    assert len(listings) >= 30, f"expected >=30 parsed listings, got {len(listings)}"


def test_onmap_first_listing_core_fields():
    listings = _parsed_listings()
    first = listings[0]
    assert first.source == "onmap"
    assert first.source_id, "source_id must be non-empty"
    assert first.price > 0
    assert first.url.startswith("https://www.onmap.co.il/")
    assert first.city, "city must be populated (Hebrew name)"
    # Fixture is TLV; city should be the Hebrew form
    assert "תל אביב" in first.city
    # price_per_sqm derived when sqm known
    if first.sqm:
        assert first.price_per_sqm is not None and first.price_per_sqm > 0


def test_onmap_geo_and_images_present():
    listings = _parsed_listings()
    geo = [l for l in listings if l.lat is not None and l.lon is not None]
    assert len(geo) >= 30, "most OnMap items should carry lat/lon"
    # Coordinates should be inside Tel Aviv bounding box
    for l in geo[:5]:
        assert 31.9 < l.lat < 32.3
        assert 34.6 < l.lon < 34.9

    with_images = [l for l in listings if l.images]
    assert with_images, "at least one listing should have images"
    assert with_images[0].images[0].startswith("https://res.cloudinary.com/")


def test_onmap_rooms_and_floor_mapping():
    listings = _parsed_listings()
    # At least some items should expose rooms and floor
    assert any(l.rooms is not None for l in listings)
    assert any(l.floor is not None for l in listings)


def test_onmap_price_filter_drops_out_of_range():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tight_search = dict(SEARCH, price_min=1, price_max=2_000_000)
    adapter = OnMapAdapter(city_slugs=["tel-aviv-yafo"], search=tight_search)
    kept = [
        listing
        for raw in data["data"]
        if (listing := adapter._parse(raw, "tel-aviv-yafo")[0]) is not None
    ]
    for l in kept:
        assert 1 <= l.price <= 2_000_000


def test_onmap_build_feed_url_shape():
    adapter = OnMapAdapter(city_slugs=["tel-aviv-yafo"], search=SEARCH)
    url = adapter._build_feed_url("tel-aviv-yafo", 48)
    assert url.startswith("https://phoenix.onmap.co.il/v1/properties/mixed_search?")
    assert "city=tel-aviv-yafo" in url
    assert "option=buy" in url
    assert "%24skip=48" in url  # '$skip' is url-encoded
    assert "%24limit=24" in url
