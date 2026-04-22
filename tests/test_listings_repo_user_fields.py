"""User-owned listing fields survive scraper upserts."""

from __future__ import annotations

from datetime import datetime

from deal_hunter.models import Listing
from deal_hunter.repo.listings_repo import ListingsRepo


def _minimal_listing(**kwargs) -> Listing:
    base = dict(
        source="yad2",
        source_id="u1",
        url="https://example.com/u1",
        price=1_000_000,
        description="first desc",
        score=5.0,
        first_seen_at=datetime(2026, 1, 1, 12, 0, 0),
        last_seen_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    base.update(kwargs)
    return Listing(**base)


def test_upsert_preserves_user_fields_after_update_user_fields(tmp_path):
    db_path = tmp_path / "t.db"
    with ListingsRepo(db_path) as repo:
        repo.upsert(_minimal_listing())
        assert repo.update_user_fields("yad2", "u1", is_favorite=True, user_notes="keep me")

        row1 = repo.get("yad2", "u1")
        assert row1 is not None
        assert row1["is_favorite"] == 1
        assert row1["user_notes"] == "keep me"

        repo.upsert(
            _minimal_listing(
                description="scraped changed",
                score=9.0,
                price=2_000_000,
            )
        )

        row2 = repo.get("yad2", "u1")
        assert row2 is not None
        assert row2["description"] == "scraped changed"
        assert row2["score"] == 9.0
        assert row2["price"] == 2_000_000
        assert row2["is_favorite"] == 1
        assert row2["user_notes"] == "keep me"
