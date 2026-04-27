"""Tests that user override columns survive scraper upserts."""

from __future__ import annotations

from datetime import datetime

from deal_hunter.models import Listing
from deal_hunter.repo.listings_repo import ListingsRepo


def _listing(**kw) -> Listing:
    base = dict(
        source="yad2",
        source_id="ov1",
        url="https://example.com/ov1",
        price=1_000_000,
        description="original",
        sqm=100,
        sqm_build=80,
        units_count=2,
        lot_sqm=200,
        garden_sqm=50,
        score=5.0,
        first_seen_at=datetime(2026, 1, 1, 12, 0, 0),
        last_seen_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    base.update(kw)
    return Listing(**base)


def test_upsert_preserves_override_sqm(tmp_path):
    db = tmp_path / "t.db"
    with ListingsRepo(db) as repo:
        repo.upsert(_listing())
        repo.update_user_fields("yad2", "ov1", sqm_user=150, sqm_build_user=130)
        row = repo.get("yad2", "ov1")
        assert row["sqm_user"] == 150
        assert row["sqm_build_user"] == 130

        repo.upsert(_listing(sqm=200, sqm_build=180))
        row = repo.get("yad2", "ov1")
        assert row["sqm_user"] == 150
        assert row["sqm_build_user"] == 130


def test_upsert_preserves_override_units_lot_garden(tmp_path):
    db = tmp_path / "t.db"
    with ListingsRepo(db) as repo:
        repo.upsert(_listing())
        repo.update_user_fields(
            "yad2", "ov1",
            units_count_user=4,
            lot_sqm_user=350,
            garden_sqm_user=90,
        )
        repo.upsert(_listing(units_count=2, lot_sqm=200, garden_sqm=50))
        row = repo.get("yad2", "ov1")
        assert row["units_count_user"] == 4
        assert row["lot_sqm_user"] == 350
        assert row["garden_sqm_user"] == 90


def test_update_user_fields_clears_override_with_null(tmp_path):
    db = tmp_path / "t.db"
    with ListingsRepo(db) as repo:
        repo.upsert(_listing())
        repo.update_user_fields("yad2", "ov1", sqm_user=150)
        repo.update_user_fields("yad2", "ov1", sqm_user=None)
        row = repo.get("yad2", "ov1")
        assert row["sqm_user"] is None


def test_update_user_fields_only_changes_specified_overrides(tmp_path):
    db = tmp_path / "t.db"
    with ListingsRepo(db) as repo:
        repo.upsert(_listing())
        repo.update_user_fields("yad2", "ov1", sqm_user=150)
        repo.update_user_fields("yad2", "ov1", user_notes="hello")
        row = repo.get("yad2", "ov1")
        assert row["sqm_user"] == 150
        assert row["user_notes"] == "hello"


def test_all_for_dashboard_includes_eff_keys(tmp_path):
    db = tmp_path / "t.db"
    with ListingsRepo(db) as repo:
        repo.upsert(_listing())
        repo.update_user_fields("yad2", "ov1", sqm_user=150)
        rows = repo.all_for_dashboard()
        assert len(rows) == 1
        r = rows[0]
        assert r["sqm_eff"] == 150
        assert r["sqm_build_eff"] == 80
        assert r["units_count_eff"] == 2
        assert r["lot_sqm_eff"] == 200
        assert r["garden_sqm_eff"] == 50
        assert r["price_per_sqm_eff"] is not None


def test_new_columns_migrated(tmp_path):
    db = tmp_path / "t.db"
    repo = ListingsRepo(db)
    cur = repo.conn.execute("PRAGMA table_info(listings)")
    cols = {row["name"] for row in cur.fetchall()}
    for col in ("units_count", "lot_sqm", "garden_sqm",
                "sqm_user", "sqm_build_user", "units_count_user",
                "lot_sqm_user", "garden_sqm_user"):
        assert col in cols, f"column {col} missing"
    repo.close()
