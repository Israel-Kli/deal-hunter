"""Listings above floor 2 are filtered from the dashboard/sheets view."""

from __future__ import annotations

from datetime import datetime

from deal_hunter.models import Listing
from deal_hunter.repo.listings_repo import ListingsRepo


def _listing(**kwargs) -> Listing:
    base = dict(
        source="yad2",
        source_id="x",
        url="https://example.com",
        price=2_000_000,
        score=7.0,
        first_seen_at=datetime(2026, 1, 1, 12, 0, 0),
        last_seen_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    base.update(kwargs)
    return Listing(**base)


def test_all_for_dashboard_excludes_floor_above_2(tmp_path):
    db = tmp_path / "t.db"
    with ListingsRepo(db) as repo:
        repo.upsert(_listing(source_id="g", floor=0))
        repo.upsert(_listing(source_id="f1", floor=1))
        repo.upsert(_listing(source_id="f2", floor=2))
        repo.upsert(_listing(source_id="f5", floor=5))
        repo.upsert(_listing(source_id="nofloor", floor=None))

        ids = {row["source_id"] for row in repo.all_for_dashboard()}
        assert ids == {"g", "f1", "f2", "nofloor"}
