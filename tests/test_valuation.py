"""Tests for fair-price valuation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from deal_hunter.models import Listing
from deal_hunter.valuation.fair_price import enrich_listing_fair_price, estimate


def _seed_comps(conn, city="תל אביב", neighborhood="נוה שאנן", count=10, base_price_per_sqm=40_000):
    """Insert synthetic comps into the DB for testing."""
    now = datetime.utcnow()
    for i in range(count):
        deal_date = (now - timedelta(days=i * 30)).strftime("%Y-%m-%d")
        price = int(base_price_per_sqm * (100 + i * 2))  # slight variation
        sqm = 100
        conn.execute(
            "INSERT OR REPLACE INTO comps "
            "(provider, address_hash, deal_date, price, sqm, rooms, city, neighborhood, "
            "street, house_number, year_built, raw, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test", f"hash_{i}", deal_date, price, sqm, 4.0,
                city, neighborhood, "הרצל", "12", 2010, "{}",
                now.isoformat(),
            ),
        )
    conn.commit()


def test_estimate_returns_none_when_no_comps(tmp_path):
    from deal_hunter.repo.listings_repo import ListingsRepo
    db = tmp_path / "test.db"
    with ListingsRepo(db) as repo:
        result = estimate(repo.conn, city="תל אביב", neighborhood="נוה שאנן", rooms=4.0, sqm=100)
    assert result is None


def test_estimate_returns_tuple_when_enough_comps(tmp_path):
    from deal_hunter.repo.listings_repo import ListingsRepo
    db = tmp_path / "test.db"
    with ListingsRepo(db) as repo:
        _seed_comps(repo.conn, count=10, base_price_per_sqm=40_000)
        result = estimate(repo.conn, city="תל אביב", neighborhood="נוה שאנן", rooms=4.0, sqm=100)
    assert result is not None
    est, low, high = result
    assert est > 0
    assert low <= est <= high
    # With base 40k ₪/sqm and 100 sqm, estimate should be around 4M
    assert 3_000_000 <= est <= 5_000_000


def test_estimate_widens_to_city_when_neighborhood_too_few(tmp_path):
    from deal_hunter.repo.listings_repo import ListingsRepo
    db = tmp_path / "test.db"
    with ListingsRepo(db) as repo:
        # Only 2 comps in the exact neighborhood — should widen to city
        _seed_comps(repo.conn, city="תל אביב", neighborhood="נוה שאנן", count=2)
        # Add more comps in the same city but different neighborhood
        _seed_comps(repo.conn, city="תל אביב", neighborhood="פלורנטין", count=8, base_price_per_sqm=38_000)
        result = estimate(repo.conn, city="תל אביב", neighborhood="נוה שאנן", rooms=4.0, sqm=100)
    assert result is not None


def test_enrich_listing_fair_price_assigns_fields(tmp_path):
    from deal_hunter.repo.listings_repo import ListingsRepo
    db = tmp_path / "test.db"
    with ListingsRepo(db) as repo:
        _seed_comps(repo.conn, count=10, base_price_per_sqm=40_000)
        listing = Listing(
            source="yad2", source_id="test1", url="",
            city="תל אביב", neighborhood="נוה שאנן",
            rooms=4.0, sqm=100, price=3_500_000,
        )
        enrich_listing_fair_price(listing, repo.conn)
    assert listing.fair_price_estimate is not None
    assert listing.fair_price_low is not None
    assert listing.fair_price_high is not None


def test_enrich_listing_fair_price_none_when_insufficient_comps(tmp_path):
    from deal_hunter.repo.listings_repo import ListingsRepo
    db = tmp_path / "test.db"
    with ListingsRepo(db) as repo:
        # Only 1 comp — below MIN_COMPS=3
        _seed_comps(repo.conn, count=1)
        listing = Listing(
            source="yad2", source_id="test2", url="",
            city="תל אביב", neighborhood="נוה שאנן",
            rooms=4.0, sqm=100, price=3_500_000,
        )
        enrich_listing_fair_price(listing, repo.conn)
    assert listing.fair_price_estimate is None
