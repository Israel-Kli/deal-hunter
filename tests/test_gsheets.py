"""Unit tests for the Google Sheets sync builder.

Tests target the pure functions (no gspread/network calls).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deal_hunter.notify import gsheets as gs


FIXED_NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
FIXED_TODAY = FIXED_NOW.date().isoformat()


def _listing(**overrides):
    base = {
        "source": "yad2",
        "source_id": "abc123",
        "url": "https://www.yad2.co.il/item/abc123",
        "city": "אריאל",
        "neighborhood": "מרכז",
        "street": "הרצל",
        "house_number": "5",
        "rooms": 5.0,
        "sqm": 120,
        "floor": 3,
        "price": 2_000_000,
        "fair_price_estimate": 2_100_000,
        "listing_type": "private",
        "is_agent": False,
        "parking": True,
        "balcony": True,
        "renovated": False,
        "ac": False,
        "mamad": False,
        "elevator": True,
        "year_built": 2000,
        "score": 7.5,
        "score_reasons": {"price_band": 0.5, "private": 0.3},
        "first_listed_date": "2026-06-01",
        "last_seen_at": "2026-06-14T11:30:00",
    }
    base.update(overrides)
    return base


def test_features_summary():
    d = {"parking": True, "balcony": True, "renovated": False, "elevator": True}
    assert gs._features_summary(d) == "P B E"


def test_features_summary_empty():
    assert gs._features_summary({}) == ""


def test_index_to_letter():
    assert gs._index_to_letter(0) == "A"
    assert gs._index_to_letter(24) == "Y"
    assert gs._index_to_letter(25) == "Z"
    assert gs._index_to_letter(26) == "AA"


def test_build_rows_columns_match_schema():
    rows, _ = gs._build_rows(
        [_listing()], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert len(rows) == 1
    assert len(rows[0]) == len(gs.SHEET_COLUMNS)


def test_new_listing_gets_today_in_last_changed():
    rows, disappeared = gs._build_rows(
        [_listing()], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    last_changed = rows[0][gs.SHEET_COLUMNS.index("last_changed")]
    assert last_changed == FIXED_TODAY
    assert disappeared == []


def test_identity_no_duplicates_on_second_cycle():
    """Same (source, source_id) on next cycle must produce exactly one row."""
    rows1, _ = gs._build_rows(
        [_listing()], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )

    # Simulate reading back what we'd have written
    existing = {("yad2", "abc123"): _row_to_strings(rows1[0])}

    # Second cycle with the same listing (no data change)
    rows2, _ = gs._build_rows(
        [_listing()], existing, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert len(rows2) == 1


def test_unchanged_listing_preserves_last_changed_and_log():
    """A row with no data diff must preserve last_changed verbatim."""
    yesterday = (FIXED_NOW - timedelta(days=1)).date().isoformat()
    existing = {
        ("yad2", "abc123"): _make_existing_row(
            _listing(),
            last_changed=yesterday,
            change_log=json.dumps([{"ts": yesterday, "changes": {"price": [2100000, 2000000]}}]),
        )
    }
    rows, _ = gs._build_rows(
        [_listing()], existing, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    last_changed = rows[0][gs.SHEET_COLUMNS.index("last_changed")]
    assert last_changed == yesterday
    log_str = rows[0][gs.SHEET_COLUMNS.index("change_log")]
    log_entries = json.loads(log_str)
    assert len(log_entries) == 1
    assert log_entries[0]["ts"] == yesterday


def test_price_change_appends_log_entry():
    yesterday = (FIXED_NOW - timedelta(days=1)).date().isoformat()
    existing = {
        ("yad2", "abc123"): _make_existing_row(
            _listing(price=2_100_000),
            last_changed=yesterday,
            change_log="",
        )
    }
    rows, _ = gs._build_rows(
        [_listing(price=2_000_000)], existing, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    last_changed = rows[0][gs.SHEET_COLUMNS.index("last_changed")]
    assert last_changed == FIXED_TODAY
    log_str = rows[0][gs.SHEET_COLUMNS.index("change_log")]
    log_entries = json.loads(log_str)
    assert log_entries[0]["ts"] == FIXED_TODAY
    assert log_entries[0]["changes"]["price"] == [2_100_000, 2_000_000]


def test_audit_max_entries_cap():
    cap = 3
    prior_entries = [{"ts": f"2026-06-{i:02d}", "changes": {"score": [i, i + 1]}} for i in range(1, 11)]
    existing = {
        ("yad2", "abc123"): _make_existing_row(
            _listing(score=7.5),
            last_changed="2026-06-10",
            change_log=json.dumps(prior_entries),
        )
    }
    # Trigger a new diff (price change) so a new entry is prepended
    rows, _ = gs._build_rows(
        [_listing(price=1_900_000)], existing, cutoff_minutes=120, audit_max=cap,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    log_entries = json.loads(rows[0][gs.SHEET_COLUMNS.index("change_log")])
    assert len(log_entries) == cap
    assert log_entries[0]["ts"] == FIXED_TODAY


def test_disappearance_stamps_today_and_logs():
    """A listing with stale last_seen_at gets Disappeared On = today."""
    stale = (FIXED_NOW - timedelta(hours=10)).isoformat()
    rows, disappeared = gs._build_rows(
        [_listing(last_seen_at=stale)], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert disappeared == [0]
    disappeared_on = rows[0][gs.SHEET_COLUMNS.index("disappeared_on")]
    assert disappeared_on == FIXED_TODAY
    log_entries = json.loads(rows[0][gs.SHEET_COLUMNS.index("change_log")])
    assert any(e.get("disappeared") for e in log_entries)


def test_existing_disappeared_on_preserved():
    """Once stamped, Disappeared On must not be overwritten with today."""
    earlier = "2026-06-10"
    stale = (FIXED_NOW - timedelta(hours=10)).isoformat()
    existing = {
        ("yad2", "abc123"): _make_existing_row(
            _listing(last_seen_at=stale),
            disappeared_on=earlier,
        )
    }
    rows, _ = gs._build_rows(
        [_listing(last_seen_at=stale)], existing, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    disappeared_on = rows[0][gs.SHEET_COLUMNS.index("disappeared_on")]
    assert disappeared_on == earlier


def test_reappearance_clears_disappeared_and_logs():
    earlier = "2026-06-10"
    existing = {
        ("yad2", "abc123"): _make_existing_row(
            _listing(),
            disappeared_on=earlier,
        )
    }
    fresh_seen = FIXED_NOW.isoformat()
    rows, disappeared = gs._build_rows(
        [_listing(last_seen_at=fresh_seen)], existing, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert disappeared == []
    disappeared_on = rows[0][gs.SHEET_COLUMNS.index("disappeared_on")]
    assert disappeared_on == ""
    log_entries = json.loads(rows[0][gs.SHEET_COLUMNS.index("change_log")])
    assert any(e.get("reappeared") for e in log_entries)


def test_sort_active_then_disappeared():
    """Active rows first by first_listed_date desc; disappeared at bottom."""
    fresh = FIXED_NOW.isoformat()
    stale = (FIXED_NOW - timedelta(hours=10)).isoformat()
    listings = [
        _listing(source_id="old_active", first_listed_date="2026-05-01", last_seen_at=fresh),
        _listing(source_id="new_active", first_listed_date="2026-06-10", last_seen_at=fresh),
        _listing(source_id="disappeared", first_listed_date="2026-06-12", last_seen_at=stale),
    ]
    rows, disappeared = gs._build_rows(
        listings, {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    src_id_col = gs.SHEET_COLUMNS.index("source_id")
    order = [r[src_id_col] for r in rows]
    assert order == ["new_active", "old_active", "disappeared"]
    assert disappeared == [2]


def test_diff_only_data_columns():
    """last_seen_at and first_listed_date changes alone must NOT trigger a diff."""
    fresh = FIXED_NOW.isoformat()
    yesterday = (FIXED_NOW - timedelta(days=1)).date().isoformat()
    existing = {
        ("yad2", "abc123"): _make_existing_row(
            _listing(last_seen_at="2026-06-13T11:30:00"),
            last_changed=yesterday,
            change_log="",
        )
    }
    rows, _ = gs._build_rows(
        [_listing(last_seen_at=fresh)], existing, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    last_changed = rows[0][gs.SHEET_COLUMNS.index("last_changed")]
    assert last_changed == yesterday  # unchanged
    log_str = rows[0][gs.SHEET_COLUMNS.index("change_log")]
    assert log_str == ""


def test_missing_source_id_skipped():
    rows, _ = gs._build_rows(
        [_listing(source_id="")], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert rows == []


# ---- helpers --------------------------------------------------------------


def _row_to_strings(row):
    """Convert a typed row list back to the dict shape `_read_existing` would
    return — strings keyed by SHEET_COLUMNS."""
    out = {}
    for i, col in enumerate(gs.SHEET_COLUMNS):
        v = row[i] if i < len(row) else ""
        out[col] = "" if v is None else str(v)
    return out


def _make_existing_row(listing_dict, *, disappeared_on="", last_changed="", change_log=""):
    """Build a sheet-row dict (strings) from a listing dict, plus audit columns."""
    data = gs._build_data_dict(listing_dict)
    out: dict[str, str] = {}
    for col in gs.SHEET_COLUMNS:
        if col == "disappeared_on":
            out[col] = disappeared_on
        elif col == "last_changed":
            out[col] = last_changed
        elif col == "change_log":
            out[col] = change_log
        elif col == "url":
            url = data.get("url", "")
            out[col] = f'view' if url else ""  # HYPERLINK display text
        else:
            v = gs._coerce_cell(data.get(col))
            out[col] = "" if v == "" else str(v)
    return out
