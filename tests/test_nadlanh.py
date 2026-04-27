"""Golden-fixture tests for the Nadlanh adapter."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from deal_hunter.adapters.nadlanh import NadlanhAdapter, _parse_address, _extract_post_id

FIXTURE = Path(__file__).parent / "fixtures" / "nadlanh_feed.html"

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
    adapter = NadlanhAdapter(search=SEARCH, request_delay_sec=0)

    # Grab from all ECS containers, de-dup by post_id
    seen_ids: set[int] = set()
    listings = []
    for article in soup.select("article.elementor-post.ecs-post-loop[class*='post-']"):
        post_id = _extract_post_id(article)
        if post_id and post_id not in seen_ids:
            seen_ids.add(post_id)
            listing, _reason = adapter._parse_card(article)
            if listing is not None:
                listings.append(listing)
    return listings


def test_nadlanh_parses_fixture_items():
    listings = _parsed_listings()
    assert len(listings) >= 20, f"expected >=20, got {len(listings)}"


def test_nadlanh_first_listing_core_fields():
    listings = _parsed_listings()
    first = listings[0]
    assert first.source == "nadlanh"
    assert first.source_id, "source_id must be non-empty"
    assert first.price > 0
    assert first.url.startswith("https://nadlanh.co.il/")
    assert first.city, "city must be populated"


def test_nadlanh_most_have_sqm():
    listings = _parsed_listings()
    with_sqm = [l for l in listings if l.sqm is not None]
    # Some listings (land plots, etc.) don't have sqm icons — at least 8 should
    assert len(with_sqm) >= 8, f"only {len(with_sqm)} of {len(listings)} have sqm"


def test_nadlanh_most_have_rooms():
    listings = _parsed_listings()
    with_rooms = [l for l in listings if l.rooms is not None]
    # Some listings don't have room icons — at least 8 should
    assert len(with_rooms) >= 8, f"only {len(with_rooms)} of {len(listings)} have rooms"


def test_nadlanh_tight_filter_price():
    adapter = NadlanhAdapter(
        search={**SEARCH, "price_max": 50_000},
        request_delay_sec=0,
    )
    html = FIXTURE.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    parsed = 0
    for article in soup.select("article.elementor-post.ecs-post-loop[class*='post-']"):
        listing, _reason = adapter._parse_card(article)
        if listing is not None:
            parsed += 1
    assert parsed == 0, f"all should be filtered, got {parsed}"


def test_parse_address():
    assert _parse_address("בזלת 31, אריאל") == ("בזלת", "31", "", "אריאל")
    assert _parse_address("ששת הימים 24, אריאל") == ("ששת הימים", "24", "", "אריאל")
    assert _parse_address("עיר היונה 14, אריאל") == ("עיר היונה", "14", "", "אריאל")


def test_extract_post_id():
    from bs4 import BeautifulSoup
    html = '<article id="post-20649" class="elementor-post elementor-grid-item ecs-post-loop post-20649 post type-post">'
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article")
    assert _extract_post_id(article) == 20649
