"""Golden-fixture tests for the ad.co.il adapter."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from deal_hunter.adapters.ad import (
    AdAdapter,
    _apply_detail_enrichment,
    _parse_price,
    _parse_total_pages,
    _split_city_title,
    _trailing_number,
)
from deal_hunter.models import Listing

FIXTURES = Path(__file__).parent / "fixtures"
FEED_HTML = FIXTURES / "ad_feed_p1.html"
DETAIL_HTML = FIXTURES / "ad_detail_16192054.html"

SEARCH = {
    "rooms_min": 1.0,
    "rooms_max": 10.0,
    "price_min": 100_000,
    "price_max": 100_000_000,
    "min_sqm": 20,
    "max_listing_age_days": 100_000,
    "exclude_ground_floor": False,
}


def _adapter() -> AdAdapter:
    return AdAdapter(city_paths=["/nadlansale"], search=SEARCH)


def _parsed_cards() -> list[Listing]:
    html = FEED_HTML.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    ad = _adapter()
    out: list[Listing] = []
    seen: set[str] = set()
    for card in soup.select("div.card.overflow-hidden"):
        l = ad._parse_card(card)
        if l is None or l.source_id in seen:
            continue
        seen.add(l.source_id)
        out.append(l)
    return out


# ── feed parsing ───────────────────────────────────────────────────────────


def test_parses_many_cards_from_feed():
    listings = _parsed_cards()
    # ad.co.il shows ~50 cards/page; fixture had 51 anchors → 51 unique ids.
    assert len(listings) >= 40, f"expected >=40 listings, got {len(listings)}"


def test_first_card_core_fields():
    listings = _parsed_cards()
    first = listings[0]
    assert first.source == "ad"
    assert first.source_id.isdigit() and len(first.source_id) >= 6
    assert first.price > 0
    assert first.url.startswith("https://www.ad.co.il/ad/")
    assert first.city, "city must be non-empty"


def test_price_and_sqm_rooms_extracted():
    listings = _parsed_cards()
    with_price = [l for l in listings if l.price > 0]
    assert len(with_price) == len(listings), "all cards should have a price"
    with_sqm = [l for l in listings if l.sqm]
    assert len(with_sqm) >= len(listings) * 0.8, "most cards should have sqm"
    with_rooms = [l for l in listings if l.rooms is not None]
    assert len(with_rooms) >= len(listings) * 0.8, "most cards should have rooms"
    # price_per_sqm derived when sqm known
    for l in with_sqm[:5]:
        assert l.price_per_sqm is not None and l.price_per_sqm > 0


def test_promoted_tag_detected():
    listings = _parsed_cards()
    promoted = [l for l in listings if "promoted" in l.tags]
    # Feed fixture contained the 'מקודם' badge on the top sponsored cards.
    assert promoted, "expected at least one sponsored listing in fixture"


def test_images_absolutized():
    listings = _parsed_cards()
    with_images = [l for l in listings if l.images]
    assert with_images, "expected at least one listing with an image"
    for l in with_images[:5]:
        assert l.images[0].startswith("https://"), l.images[0]


def test_known_listing_in_fixture():
    """Spot-check: /ad/16192054 is present with its expected core fields."""
    listings = {l.source_id: l for l in _parsed_cards()}
    target = listings.get("16192054")
    assert target is not None
    assert target.price == 6_000_000
    assert target.sqm == 135
    assert target.rooms == 5.0
    assert "בת ים" in target.city
    assert "promoted" in target.tags  # this fixture row is sponsored


def test_filters_applied_in_feed_iteration():
    """Respect price/rooms filters by routing through the public filter path."""
    tight = dict(SEARCH, price_min=1, price_max=2_000_000)
    ad = AdAdapter(city_paths=["/nadlansale"], search=tight)
    html = FEED_HTML.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    kept: list[Listing] = []
    for card in soup.select("div.card.overflow-hidden"):
        l = ad._parse_card(card)
        if l is None or ad._passes_filters(l) is not None:
            continue
        kept.append(l)
    assert kept, "filter should keep at least a few cards"
    for l in kept:
        assert 1 <= l.price <= 2_000_000


# ── detail enrichment ─────────────────────────────────────────────────────


def test_detail_enrichment_fills_floor_and_amenities():
    html = DETAIL_HTML.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    listing = Listing(
        source="ad",
        source_id="16192054",
        url="https://www.ad.co.il/ad/16192054",
        price=6_000_000,
    )
    _apply_detail_enrichment(listing, soup)
    assert listing.first_listed_date == "2024-07-02"
    assert listing.floor == 19
    # Fixture's amenity panel: elevator, parking, balcony, mamad, ac, renovated = True
    assert listing.elevator is True
    assert listing.parking is True
    assert listing.balcony is True
    assert listing.mamad is True
    assert listing.ac is True
    assert listing.renovated is True
    assert listing.description and "בת ים" in listing.description


# ── pure helpers ──────────────────────────────────────────────────────────


def test_parse_price_handles_common_shapes():
    assert _parse_price("6,000,000 ₪") == 6_000_000
    assert _parse_price("1,670K ₪") == 1_670_000
    assert _parse_price("2.5M ₪") == 2_500_000
    assert _parse_price("") is None
    assert _parse_price("מחיר לא צוין") is None


def test_split_city_title_handles_multiword_cities():
    assert _split_city_title("תל אביב יפו נוה שאנן") == ("תל אביב יפו", "נוה שאנן")
    assert _split_city_title("בת ים אזור התעשייה") == ("בת ים", "אזור התעשייה")
    assert _split_city_title("רמת גן") == ("רמת גן", "")
    assert _split_city_title("חיפה הדר") == ("חיפה", "הדר")
    assert _split_city_title("") == ("", "")


def test_trailing_number_extracts_house_number():
    assert _trailing_number("הקוממיות  16") == "16"
    assert _trailing_number("ביאליק 24") == "24"
    assert _trailing_number("רחוב בלי מספר") == ""


def test_parse_total_pages_from_fixture():
    soup = BeautifulSoup(FEED_HTML.read_text(encoding="utf-8"), "html.parser")
    n = _parse_total_pages(soup)
    assert n is not None and n >= 1


# ── URL shape ─────────────────────────────────────────────────────────────


def test_build_feed_url_pagination():
    ad = _adapter()
    assert ad._build_feed_url("/nadlansale", 1) == "https://www.ad.co.il/nadlansale"
    assert ad._build_feed_url("/nadlansale", 2) == "https://www.ad.co.il/nadlansale?pageindex=2"
    # Preserve existing query string
    assert ad._build_feed_url("/nadlansale?sp276=17414", 3) == (
        "https://www.ad.co.il/nadlansale?sp276=17414&pageindex=3"
    )
