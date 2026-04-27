"""Golden-fixture tests for the Reariel adapter."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from deal_hunter.adapters.reariel import RearielAdapter, _parse_address, _extract_lot_sqm, _extract_garden_sqm

FIXTURE = Path(__file__).parent / "fixtures" / "reariel_feed.html"

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
    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    catalog = soup.select_one("#Catalog .collection-list-wrapper-3") or soup.select_one("#Catalog")
    cards = catalog.select(".collection-item-3.w-dyn-item") if catalog else []
    adapter = RearielAdapter(search=SEARCH, request_delay_sec=0)
    listings = []
    for card in cards:
        listing, _reason = adapter._parse_card(card)
        if listing is not None:
            listings.append(listing)
    return listings


def test_reariel_parses_fixture_items():
    listings = _parsed_listings()
    assert len(listings) >= 50, f"expected >=50, got {len(listings)}"


def test_reariel_first_listing_core_fields():
    listings = _parsed_listings()
    first = listings[0]
    assert first.source == "reariel"
    assert first.source_id, "source_id must be non-empty"
    assert first.price > 0
    assert first.city
    assert "אריאל" in first.city or first.city == "נופים" or first.city == "אריאל"
    assert first.url.startswith("https://www.reariel.co.il/for-sale/")


def test_reariel_filter_data_price_and_type():
    listings = _parsed_listings()
    # Every listing should have a price and listing_type
    for listing in listings:
        assert listing.price > 0, f"zero price for {listing.source_id}"
        assert listing.listing_type, f"missing listing_type for {listing.source_id}"


def test_reariel_has_sqm_on_most():
    listings = _parsed_listings()
    with_sqm = [l for l in listings if l.sqm is not None]
    assert len(with_sqm) > len(listings) * 0.5, f"only {len(with_sqm)} of {len(listings)} have sqm"


def test_reariel_has_rooms_on_most():
    listings = _parsed_listings()
    with_rooms = [l for l in listings if l.rooms is not None]
    assert len(with_rooms) > len(listings) * 0.5, f"only {len(with_rooms)} of {len(listings)} have rooms"


def test_reariel_tight_filter_price():
    adapter = RearielAdapter(
        search={**SEARCH, "price_max": 50_000},
        request_delay_sec=0,
    )
    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    catalog = soup.select_one("#Catalog .collection-list-wrapper-3") or soup.select_one("#Catalog")
    cards = catalog.select(".collection-item-3.w-dyn-item") if catalog else []
    parsed = 0
    for card in cards:
        listing, _reason = adapter._parse_card(card)
        if listing is not None:
            parsed += 1
    assert parsed == 0, f"all should be filtered by tight price, got {parsed}"


def test_parse_address():
    assert _parse_address("דרך הציונות 4, אריאל") == ("דרך הציונות", "4", "", "אריאל")
    assert _parse_address("הירדן 1, אריאל") == ("הירדן", "1", "", "אריאל")
    assert _parse_address("האיריס 14, נופים") == ("האיריס", "14", "", "נופים")


def test_extract_lot_sqm():
    assert _extract_lot_sqm("קוטג' פינתי על מגרש ענק של 380 מ\"ר") == 380
    assert _extract_lot_sqm("המגרש משתרע על כחצי דונם (כ-500 מ\"ר)") == 500
    assert _extract_lot_sqm("בית על מגרש פינתי ללא שכנים בגודל 363 מ\"ר") == 363
    assert _extract_lot_sqm("דירה יפה") is None


def test_extract_garden_sqm():
    assert _extract_garden_sqm("גינה ענקית של 120 מ\"ר") == 120
    assert _extract_garden_sqm("חצר קטנה") is None
    assert _extract_garden_sqm("אין גינה") is None


def test_reariel_city_filter_allowed():
    """Only listings from allowed cities pass through."""
    adapter = RearielAdapter(
        search=SEARCH,
        allowed_cities=["אריאל"],
        request_delay_sec=0,
    )
    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    catalog = soup.select_one("#Catalog .collection-list-wrapper-3") or soup.select_one("#Catalog")
    cards = catalog.select(".collection-item-3.w-dyn-item") if catalog else []
    listings = []
    for card in cards:
        listing, reason = adapter._parse_card(card)
        if listing is not None:
            listings.append(listing)
        elif reason == "city_not_allowed":
            pass

    assert len(listings) >= 30, f"expected >=30 Ariel-only listings, got {len(listings)}"
    assert all(l.city == "אריאל" for l in listings), "all passed listings must be from Ariel"


def test_reariel_no_city_filter():
    """Without allowed_cities, all listings pass through (backward compat)."""
    adapter = RearielAdapter(
        search=SEARCH,
        request_delay_sec=0,
    )
    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    catalog = soup.select_one("#Catalog .collection-list-wrapper-3") or soup.select_one("#Catalog")
    cards = catalog.select(".collection-item-3.w-dyn-item") if catalog else []
    listings = []
    cities = set()
    for card in cards:
        listing, _reason = adapter._parse_card(card)
        if listing is not None:
            listings.append(listing)
            cities.add(listing.city)
    assert len(listings) >= 50, f"expected >=50 listings without filter, got {len(listings)}"
    assert len(cities) > 1, "expected multiple cities when no filter applied"
