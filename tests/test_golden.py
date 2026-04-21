"""Golden-fixture tests for M1 behaviour.

Three tests that lock the core pipeline before any new sources are added:
  1. Yad2 feed JSON → expected list[Listing]
  2. Scoring: known fair_price delta → expected score bucket
  3. Repo upsert: same source_id at two prices → price_history has 2 rows
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from deal_hunter.adapters.yad2 import Yad2Adapter, _extract_items
from deal_hunter.comps.yad2_deals import extract_comps_from_html
from deal_hunter.models import Listing
from deal_hunter.repo.listings_repo import ListingsRepo
from deal_hunter.scoring.heuristic import score_listing

FIXTURE = Path(__file__).parent / "fixtures" / "yad2_feed_telaviv.json"

CITY = {"name": "תל אביב יפו", "city_code": "5000", "slug": "tel-aviv-area"}
SEARCH = {
    "rooms_min": 2.5,
    "rooms_max": 5,
    "price_min": 1_800_000,
    "price_max": 10_000_000,
    "min_sqm": 55,
    "max_listing_age_days": 90,
}

# Minimal HTML with both ₪ character and &#x20AA; entity forms to guard price parsing
_DEALS_HTML = """
<section data-testid="deals-history">
<table><tbody>
<tr>
  <td>הרצל 12</td><td>דירה</td><td>03/2024</td><td>3</td>
  <td>80</td><td>2</td><td>1995</td><td>2,800,000 &#x20AA;</td>
</tr>
<tr>
  <td>ביאליק 5</td><td>דירה</td><td>01/2024</td><td>3.5</td>
  <td>90</td><td>3</td><td>2002</td><td>3,100,000 ₪</td>
</tr>
<tr>
  <td></td><td>N/A</td><td>bad</td><td></td><td></td><td></td>
</tr>
</tbody></table>
</section>
"""


# ── 1. Feed parsing ──────────────────────────────────────────────────────────


def test_yad2_feed_parse_returns_listings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    adapter = Yad2Adapter(cities=[CITY], search=SEARCH)
    items = _extract_items(data)
    assert items, "fixture must contain at least one raw item"

    listings = [adapter._parse(raw, CITY) for raw in items]
    parsed = [l for l in listings if l is not None]
    assert parsed, "at least one raw item must survive the filter"

    first = parsed[0]
    assert first.source == "yad2"
    assert first.source_id, "source_id must be non-empty"
    assert first.price > 0, "price must be positive"
    assert first.url.startswith("https://"), "url must be https"
    assert first.city, "city must be populated"
    assert first.price_per_sqm is None or first.price_per_sqm > 0


# ── 1b. Comps HTML parser ────────────────────────────────────────────────────


def test_extract_comps_from_html_parses_table():
    comps = extract_comps_from_html(_DEALS_HTML, source_city="תל אביב יפו", source_neighborhood="מרכז")
    assert len(comps) == 2, f"expected 2 valid comps, got {len(comps)}"

    # First comp: HTML-entity price
    assert comps[0].price == 2_800_000, f"entity price mismatch: {comps[0].price}"
    assert comps[0].sqm == 80
    assert comps[0].rooms == 3.0
    assert comps[0].year_built == 1995
    assert comps[0].deal_date == "03/2024"
    assert comps[0].city == "תל אביב יפו"
    assert comps[0].source == "yad2_deals"

    # Second comp: direct ₪ character
    assert comps[1].price == 3_100_000, f"direct price mismatch: {comps[1].price}"
    assert comps[1].rooms == 3.5


def test_extract_comps_empty_on_no_section():
    assert extract_comps_from_html("") == []
    assert extract_comps_from_html("<html><body>no deals here</body></html>") == []


# ── 2. Scoring ───────────────────────────────────────────────────────────────


def _make_listing(**overrides) -> Listing:
    base = dict(
        source="yad2",
        source_id="test-001",
        url="https://www.yad2.co.il/item/test-001",
        city="תל אביב יפו",
        neighborhood="מונטיפיורי",
        price=3_000_000,
        sqm=80,
        rooms=3.0,
        price_per_sqm=37_500,
    )
    base.update(overrides)
    return Listing(**base)


def test_score_fair_price_below_estimate():
    """Listing priced well below fair_price_estimate → score above midpoint."""
    listing = _make_listing(
        price=2_800_000,
        sqm=80,
        price_per_sqm=35_000,
        # fair_price says 40,000 ₪/sqm → 3,200,000 total
        fair_price_estimate=3_200_000,
        fair_price_low=3_000_000,
        fair_price_high=3_500_000,
    )
    score, reasons = score_listing(listing)
    assert reasons["market_band_source"] == "comps", "should use comps-based band"
    assert score > 5.0, f"expected score > 5, got {score}"
    assert "price_vs_market" in reasons


def test_score_market_refs_fallback_when_no_fair_price():
    """When fair_price_estimate is None, scorer falls back to MARKET_REFS."""
    listing = _make_listing(
        price=3_000_000,
        sqm=80,
        price_per_sqm=37_500,
        fair_price_estimate=None,
    )
    score, reasons = score_listing(listing)
    assert reasons["market_band_source"] == "market_refs"
    assert isinstance(score, float)
    assert 1.0 <= score <= 10.0


def test_score_clamped_to_1_10():
    """Score must always be within [1, 10]."""
    listing = _make_listing(
        price=100_000,
        sqm=200,
        price_per_sqm=500,  # absurdly cheap
        parking=True, elevator=True, balcony=True, mamad=True, renovated=True,
    )
    score, _ = score_listing(listing)
    assert 1.0 <= score <= 10.0


# ── 3. Repo upsert ───────────────────────────────────────────────────────────


def test_repo_upsert_price_history():
    """Insert same source_id twice at different prices → price_history has 2 rows."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        with ListingsRepo(db_path) as repo:
            listing = _make_listing(price=2_000_000)

            is_new, prev = repo.upsert(listing)
            assert is_new is True
            assert prev is None

            # Update price
            listing.price = 1_900_000
            listing.price_per_sqm = 23_750
            is_new2, prev2 = repo.upsert(listing)
            assert is_new2 is False
            assert prev2 == 2_000_000, "should return previous price on update"

            rows = repo.conn.execute(
                "SELECT price FROM price_history WHERE source=? AND source_id=? ORDER BY ts",
                ("yad2", "test-001"),
            ).fetchall()
            prices = [r[0] for r in rows]
            assert len(prices) == 2, f"expected 2 price_history rows, got {prices}"
            assert prices[0] == 2_000_000
            assert prices[1] == 1_900_000


def test_repo_upsert_same_price_no_duplicate_history():
    """Re-upsert at the same price does NOT duplicate price_history rows."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        with ListingsRepo(db_path) as repo:
            listing = _make_listing(price=3_000_000)
            repo.upsert(listing)
            is_new, prev = repo.upsert(listing)
            assert is_new is False
            assert prev is None  # price unchanged
            rows = repo.conn.execute(
                "SELECT COUNT(*) FROM price_history WHERE source=? AND source_id=?",
                ("yad2", "test-001"),
            ).fetchone()
            assert rows[0] >= 1

