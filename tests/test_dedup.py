"""Tests for cross-source listing dedup / canonicalizer."""

from __future__ import annotations

import pytest

from deal_hunter.dedup.canonicalizer import (
    CanonicalGroup,
    dedup_batch,
    exact_key,
    fuzzy_match,
    load_existing_groups,
    make_canonical_id,
)
from deal_hunter.models import Listing


def _listing(**kwargs) -> Listing:
    defaults = dict(
        source="yad2",
        source_id="12345",
        url="https://example.com/12345",
        price=5_000_000,
    )
    defaults.update(kwargs)
    return Listing(**defaults)


# ── make_canonical_id ────────────────────────────────────────────────


def test_make_canonical_id_deterministic():
    a = make_canonical_id("hello")
    b = make_canonical_id("hello")
    assert a == b
    assert a.startswith("CAN-")
    assert len(a) == 16  # "CAN-" + 12 hex chars


def test_make_canonical_id_different_inputs():
    assert make_canonical_id("a") != make_canonical_id("b")


# ── exact_key ────────────────────────────────────────────────────────


def test_exact_key_same_address_converges():
    """Two listings with cosmetically different addresses should get same key."""
    l1 = _listing(
        source="yad2", source_id="y1",
        street="רח' הרצל 12", rooms=4.0, sqm=100,
    )
    l2 = _listing(
        source="ad", source_id="a1",
        street="רחוב הרצל  12", rooms=4.0, sqm=100,
    )
    assert exact_key(l1) == exact_key(l2)


def test_exact_key_different_rooms_diverges():
    l1 = _listing(street="הרצל 12", rooms=4.0, sqm=100)
    l2 = _listing(street="הרצל 12", rooms=3.0, sqm=100)
    assert exact_key(l1) != exact_key(l2)


def test_exact_key_different_sqm_bucket_diverges():
    l1 = _listing(street="הרצל 12", rooms=4.0, sqm=100)
    l2 = _listing(street="הרצל 12", rooms=4.0, sqm=105)
    assert exact_key(l1) == exact_key(l2)  # same 10-bucket


def test_exact_key_different_sqm_bucket_crosses_boundary():
    l1 = _listing(street="הרצל 12", rooms=4.0, sqm=109)
    l2 = _listing(street="הרצל 12", rooms=4.0, sqm=110)
    assert exact_key(l1) != exact_key(l2)  # 100 vs 110


# ── fuzzy_match ──────────────────────────────────────────────────────


def test_fuzzy_match_finds_close_address():
    """Typo in street name should still match via fuzzy."""
    groups = {
        "CAN-001": CanonicalGroup(
            canonical_id="CAN-001",
            city="תל אביב",
            street_normalized="הרצל",
            house_number="12",
            rooms_b="4.0",
            sqm_b="100",
        ),
    }
    listing = _listing(
        city="תל אביב",
        street="הרצ'ל 12",  # typo: apostrophe inserted
        rooms=4.0, sqm=100,
    )
    result = fuzzy_match(listing, groups)
    assert result == "CAN-001"


def test_fuzzy_match_different_city_no_match():
    groups = {
        "CAN-001": CanonicalGroup(
            canonical_id="CAN-001",
            city="חיפה",
            street_normalized="הרצל",
            house_number="12",
            rooms_b="4.0",
            sqm_b="100",
        ),
    }
    listing = _listing(
        city="תל אביב",
        street="הרצל 12",
        rooms=4.0, sqm=100,
    )
    assert fuzzy_match(listing, groups) is None


def test_fuzzy_match_too_different_no_match():
    groups = {
        "CAN-001": CanonicalGroup(
            canonical_id="CAN-001",
            city="תל אביב",
            street_normalized="רוטשילד",
            house_number="5",
            rooms_b="3.0",
            sqm_b="80",
        ),
    }
    listing = _listing(
        city="תל אביב",
        street="הרצל 12",
        rooms=4.0, sqm=100,
    )
    assert fuzzy_match(listing, groups) is None


# ── dedup_batch ──────────────────────────────────────────────────────


def test_dedup_batch_exact_match_across_sources():
    """Two listings from different sources, same address → same canonical_id."""
    l1 = _listing(source="yad2", source_id="y1", street="הרצל 12", rooms=4.0, sqm=100)
    l2 = _listing(source="ad", source_id="a1", street="רחוב הרצל 12", rooms=4.0, sqm=100)
    groups = dedup_batch([l1, l2])
    assert l1.canonical_id == l2.canonical_id
    assert len(groups[l1.canonical_id].members) == 2


def test_dedup_batch_different_addresses_different_ids():
    l1 = _listing(source="yad2", source_id="y1", street="הרצל 12", rooms=4.0, sqm=100)
    l2 = _listing(source="yad2", source_id="y2", street="רוטשילד 5", rooms=3.0, sqm=80)
    groups = dedup_batch([l1, l2])
    assert l1.canonical_id != l2.canonical_id


def test_dedup_batch_merges_into_existing_group():
    """A new listing should merge into an existing group via exact key."""
    existing_group = CanonicalGroup(
        canonical_id="CAN-existing",
        city="תל אביב",
        street_normalized="הרצל",
        house_number="12",
        rooms_b="4.0",
        sqm_b="100",
        members=[_listing(source="yad2", source_id="old", street="הרצל 12", rooms=4.0, sqm=100)],
    )
    groups = {"CAN-existing": existing_group}
    new_listing = _listing(source="ad", source_id="new1", street="הרצל 12", rooms=4.0, sqm=100)
    groups = dedup_batch([new_listing], groups)
    assert new_listing.canonical_id == "CAN-existing"
    assert len(groups["CAN-existing"].members) == 2


def test_dedup_batch_fuzzy_fallback():
    """When exact key misses, fuzzy should find the right group."""
    existing_group = CanonicalGroup(
        canonical_id="CAN-fuzzy",
        city="תל אביב",
        street_normalized="הרצל",
        house_number="12",
        rooms_b="4.0",
        sqm_b="100",
        members=[_listing(source="yad2", source_id="old", street="הרצל 12", rooms=4.0, sqm=100)],
    )
    groups = {"CAN-fuzzy": existing_group}
    # Slightly different street spelling (apostrophe inserted)
    new_listing = _listing(
        source="ad", source_id="new2",
        city="תל אביב",
        street="הרצ'ל 12", rooms=4.0, sqm=100,
    )
    groups = dedup_batch([new_listing], groups)
    assert new_listing.canonical_id == "CAN-fuzzy"


# ── CanonicalGroup properties ────────────────────────────────────────


def test_canonical_group_price_spread():
    members = [
        _listing(source="yad2", source_id="y1", price=5_000_000),
        _listing(source="ad", source_id="a1", price=4_500_000),
    ]
    g = CanonicalGroup(
        canonical_id="CAN-test",
        city="תל אביב",
        street_normalized="הרצל",
        house_number="12",
        rooms_b="4.0",
        sqm_b="100",
        members=members,
    )
    assert g.price_spread == 500_000
    assert g.cheapest.price == 4_500_000
    assert g.most_expensive.price == 5_000_000


def test_canonical_group_empty_members():
    g = CanonicalGroup(
        canonical_id="CAN-empty",
        city="תל אביב",
        street_normalized="",
        house_number="",
        rooms_b="",
        sqm_b="",
    )
    assert g.price_spread == 0
    assert g.cheapest is None
    assert g.most_expensive is None


# ── load_existing_groups ─────────────────────────────────────────────


def test_load_existing_groups_from_db(tmp_path):
    """Integration test: create a SQLite DB, insert listings, load groups."""
    from deal_hunter.repo.listings_repo import ListingsRepo

    db_path = tmp_path / "test.db"
    with ListingsRepo(db_path) as repo:
        # Insert two listings with the same canonical_id
        for src, sid in [("yad2", "y1"), ("ad", "a1")]:
            repo.conn.execute(
                "INSERT INTO listings (source, source_id, url, city, street, "
                "price, canonical_id, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    src, sid, f"https://example.com/{sid}",
                    "תל אביב", "הרצל 12", 5_000_000,
                    "CAN-shared",
                    "2026-01-01T00:00:00", "2026-01-01T00:00:00",
                ),
            )
        repo.conn.commit()

    # Use ListingsRepo to get a connection with row_factory
    with ListingsRepo(db_path) as repo:
        groups = load_existing_groups(repo.conn)
    assert "CAN-shared" in groups
    assert len(groups["CAN-shared"].members) == 2
    sources = {m.source for m in groups["CAN-shared"].members}
    assert sources == {"yad2", "ad"}
