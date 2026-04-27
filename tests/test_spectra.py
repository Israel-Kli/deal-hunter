"""Golden-fixture tests for the Spectra adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from deal_hunter.adapters.spectra import SpectraAdapter, _parse_address
from deal_hunter.models import Listing

FIXTURE = Path(__file__).parent / "fixtures" / "spectra_feed.html"
DETAIL_FIXTURE = Path(__file__).parent / "fixtures" / "spectra_detail.html"

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
    container = soup.select_one("#module_properties.listing-view.grid-view")
    cards = container.select("div[data-hz-id]") if container else soup.select("div[data-hz-id]")
    adapter = SpectraAdapter(search=SEARCH, request_delay_sec=0)
    listings = []
    for card in cards:
        listing, _reason = adapter._parse_card(card)
        if listing is not None:
            listings.append(listing)
    return listings


def test_spectra_parses_fixture_items():
    listings = _parsed_listings()
    assert len(listings) >= 3, f"expected >=3, got {len(listings)}"


def test_spectra_first_listing_core_fields():
    listings = _parsed_listings()
    first = listings[0]
    assert first.source == "spectra"
    assert first.source_id, "source_id (hz-id) must be non-empty"
    assert first.price > 0
    assert first.url.startswith("https://www.spectra-nadlan.co.il/")
    assert first.sqm is not None, "sqm must be populated from h-area"


def test_spectra_all_have_sqm():
    listings = _parsed_listings()
    for listing in listings:
        assert listing.sqm is not None, f"{listing.source_id} missing sqm"


def test_spectra_all_have_rooms():
    listings = _parsed_listings()
    for listing in listings:
        assert listing.rooms is not None, f"{listing.source_id} missing rooms"


def test_spectra_all_have_listing_type():
    listings = _parsed_listings()
    for listing in listings:
        assert listing.listing_type, f"{listing.source_id} missing listing_type"


def test_spectra_tight_filter_price():
    adapter = SpectraAdapter(
        search={**SEARCH, "price_max": 50_000},
        request_delay_sec=0,
    )
    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#module_properties.listing-view.grid-view")
    cards = container.select("div[data-hz-id]") if container else []
    parsed = 0
    for card in cards:
        listing, _reason = adapter._parse_card(card)
        if listing is not None:
            parsed += 1
    assert parsed == 0, f"all should be filtered by tight price, got {parsed}"


def test_parse_address():
    assert _parse_address("אריאל שנהב 7") == ("שנהב", "7", "", "אריאל")
    assert _parse_address("ברקן, הרקפת 140") == ("הרקפת", "140", "", "ברקן")
    assert _parse_address("רחוב הגליל 22, אריאל") == ("הגליל", "22", "", "אריאל")


def test_detail_enriches_lot_sqm():
    detail_html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    listing = Listing(
        source="spectra", source_id="20105", url="https://example.com/test",
        city="אריאל", price=3890000, sqm=180, is_agent=True,
    )
    adapter = SpectraAdapter(search=SEARCH, request_delay_sec=0)
    with patch("deal_hunter.adapters.spectra.fetch", return_value=detail_html):
        with patch("time.sleep"):
            enriched = adapter.fetch_detail(listing)
    assert enriched.lot_sqm == 407


def test_detail_enriches_garden_and_balcony():
    detail_html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    listing = Listing(
        source="spectra", source_id="20105", url="https://example.com/test",
        city="אריאל", price=3890000, sqm=180, is_agent=True,
    )
    adapter = SpectraAdapter(search=SEARCH, request_delay_sec=0)
    with patch("deal_hunter.adapters.spectra.fetch", return_value=detail_html):
        with patch("time.sleep"):
            enriched = adapter.fetch_detail(listing)
    assert enriched.garden_sqm == 213, f"expected 213, got {enriched.garden_sqm}"
    assert enriched.balcony is True


def test_detail_built_area_authoritative():
    detail_html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    listing = Listing(
        source="spectra", source_id="20105", url="https://example.com/test",
        city="אריאל", price=3890000, sqm=999, is_agent=True,
    )
    adapter = SpectraAdapter(search=SEARCH, request_delay_sec=0)
    with patch("deal_hunter.adapters.spectra.fetch", return_value=detail_html):
        with patch("time.sleep"):
            enriched = adapter.fetch_detail(listing)
    assert enriched.sqm == 180, "detail 'שטח בנוי' should override card-level guess"
    assert enriched.sqm_build == 180
