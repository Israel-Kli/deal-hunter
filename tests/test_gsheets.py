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


def test_no_features_column():
    assert "features" not in gs.SHEET_COLUMNS
    assert "Features" not in gs.SHEET_HEADERS


def test_no_age_column():
    assert "building_age" not in gs.SHEET_COLUMNS
    assert "Age" not in gs.SHEET_HEADERS


def test_sale_type_column_position():
    """Sale Type must sit immediately after Source."""
    assert "sale_type" in gs.SHEET_COLUMNS
    src_idx = gs.SHEET_COLUMNS.index("source")
    sale_idx = gs.SHEET_COLUMNS.index("sale_type")
    assert sale_idx == src_idx + 1


def test_sale_type_value_agent_vs_direct():
    rows, _ = gs._build_rows(
        [_listing(is_agent=True), _listing(source_id="x2", is_agent=False)],
        {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    sale_idx = gs.SHEET_COLUMNS.index("sale_type")
    values = {r[sale_idx] for r in rows}
    assert values == {"Agent", "Direct"}


def test_user_edit_preserved_via_shadow():
    """If user edits a cell, next cycle keeps their value."""
    # Simulate cycle 1: write the listing and capture its shadow.
    rows1, _ = gs._build_rows(
        [_listing(price=2_000_000)], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    shadow_str = rows1[0][gs.SHEET_COLUMNS.index("sync_shadow")]
    # Simulate user editing the price cell in the sheet from 2,000,000 to 1,800,000.
    existing = _row_to_strings(rows1[0])
    existing["price"] = "1800000"  # what the sheet now shows
    existing["sync_shadow"] = shadow_str
    existing_map = {("yad2", "abc123"): existing}

    # Cycle 2: DB still has price = 2,000,000.
    rows2, _ = gs._build_rows(
        [_listing(price=2_000_000)], existing_map, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    price_idx = gs.SHEET_COLUMNS.index("price")
    assert rows2[0][price_idx] == 1_800_000  # user edit preserved


def test_price_per_sqm_is_a_formula():
    rows, _ = gs._build_rows(
        [_listing()], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    ppsqm_cell = rows[0][gs.SHEET_COLUMNS.index("price_per_sqm_eff")]
    assert isinstance(ppsqm_cell, str) and ppsqm_cell.startswith("=IFERROR(")
    # The formula must reference the same row in price (col P) and sqm (col K).
    assert "ROUND(" in ppsqm_cell


def test_comments_column_exists_phone_does_not():
    assert "comments" in gs.SHEET_COLUMNS
    assert "Comments" in gs.SHEET_HEADERS
    assert "phone" not in gs.SHEET_COLUMNS
    assert "Phone" not in gs.SHEET_HEADERS


def test_comments_positioned_after_first_listed():
    fl_idx = gs.SHEET_COLUMNS.index("first_listed_date")
    cm_idx = gs.SHEET_COLUMNS.index("comments")
    assert cm_idx == fl_idx + 1


def test_comments_default_empty_and_preserved():
    """Comments start empty; user fills it; next cycle keeps the value."""
    rows1, _ = gs._build_rows(
        [_listing()], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    comments_idx = gs.SHEET_COLUMNS.index("comments")
    assert rows1[0][comments_idx] == ""

    existing = _row_to_strings(rows1[0])
    existing["comments"] = "Saw it Tuesday, great location"
    existing["sync_shadow"] = rows1[0][gs.SHEET_COLUMNS.index("sync_shadow")]
    existing_map = {("yad2", "abc123"): existing}

    rows2, _ = gs._build_rows(
        [_listing()], existing_map, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    assert rows2[0][comments_idx] == "Saw it Tuesday, great location"


def test_address_merges_street_and_house():
    """`Address` = street + house_number; if street already ends with house, keep."""
    assert gs._merge_address("הדקל", "43") == "הדקל 43"
    assert gs._merge_address("הדקל 43", "43") == "הדקל 43"
    assert gs._merge_address("Main St", "") == "Main St"
    assert gs._merge_address("", "12") == "12"
    assert gs._merge_address("", "") == ""


def test_no_street_or_house_columns():
    assert "street" not in gs.SHEET_COLUMNS
    assert "house_number" not in gs.SHEET_COLUMNS
    assert "address" in gs.SHEET_COLUMNS


def test_address_in_row():
    rows, _ = gs._build_rows(
        [_listing(street="הדקל", house_number="43")], {},
        cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    addr = rows[0][gs.SHEET_COLUMNS.index("address")]
    assert addr == "הדקל 43"


def test_description_column_populated():
    rows, _ = gs._build_rows(
        [_listing(description="Spacious house with garden")], {},
        cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    desc = rows[0][gs.SHEET_COLUMNS.index("description")]
    assert desc == "Spacious house with garden"


def test_per_source_scan_active_when_seen_with_latest_scan():
    """Listing observed in the most recent scan of its source → active,
    regardless of the wall-clock cutoff."""
    scan_ts = FIXED_NOW - timedelta(minutes=5)
    last_seen_iso = scan_ts.isoformat()  # observed right in that scan
    rows, disappeared = gs._build_rows(
        [_listing(last_seen_at=last_seen_iso)], {},
        cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
        source_latest_scans={"yad2": scan_ts.isoformat()},
    )
    assert disappeared == []
    assert rows[0][gs.SHEET_COLUMNS.index("disappeared_on")] == ""


def test_per_source_scan_disappeared_when_recent_scan_missed_listing():
    """Source ran recently but didn't re-observe this listing → disappeared."""
    scan_ts = FIXED_NOW - timedelta(minutes=2)
    stale_seen = (FIXED_NOW - timedelta(days=5)).isoformat()
    rows, disappeared = gs._build_rows(
        [_listing(last_seen_at=stale_seen)], {},
        cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
        source_latest_scans={"yad2": scan_ts.isoformat()},
    )
    assert disappeared == [0]


def test_no_user_edit_uses_fresh_db_value():
    """If user did not touch a cell, next cycle uses the DB value."""
    rows1, _ = gs._build_rows(
        [_listing(price=2_000_000)], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    existing = _row_to_strings(rows1[0])
    existing["sync_shadow"] = rows1[0][gs.SHEET_COLUMNS.index("sync_shadow")]
    existing_map = {("yad2", "abc123"): existing}

    # Cycle 2: DB now has price = 1,900,000 (a real price drop), user didn't edit.
    rows2, _ = gs._build_rows(
        [_listing(price=1_900_000)], existing_map, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    price_idx = gs.SHEET_COLUMNS.index("price")
    assert rows2[0][price_idx] == 1_900_000  # fresh DB value


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


def test_source_cell_is_hyperlink_to_listing_url():
    rows, _ = gs._build_rows(
        [_listing()], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    src_cell = rows[0][gs.SHEET_COLUMNS.index("source")]
    assert src_cell.startswith("=HYPERLINK(")
    assert "https://www.yad2.co.il/item/abc123" in src_cell
    assert '"yad2"' in src_cell


def test_no_link_column():
    assert "url" not in gs.SHEET_COLUMNS
    assert "Link" not in gs.SHEET_HEADERS


def test_no_fair_price_column():
    assert "fair_price_estimate" not in gs.SHEET_COLUMNS
    assert "Fair Price" not in gs.SHEET_HEADERS


def test_sqm_family_columns_present():
    for col in ("sqm_eff", "sqm_build_eff", "lot_sqm_eff", "garden_sqm_eff"):
        assert col in gs.SHEET_COLUMNS
    # Order: SQM → Built → Lot → Garden (right after SQM, before Floor)
    idx = [gs.SHEET_COLUMNS.index(c) for c in ("sqm_eff", "sqm_build_eff", "lot_sqm_eff", "garden_sqm_eff", "floor")]
    assert idx == sorted(idx), f"expected sqm family in order, got indexes {idx}"


def test_sqm_family_values_in_row():
    l = _listing(sqm=120, sqm_build=140, lot_sqm=300, garden_sqm=50)
    rows, _ = gs._build_rows(
        [l], {}, cutoff_minutes=120, audit_max=20,
        today=FIXED_TODAY, now=FIXED_NOW,
    )
    row = rows[0]
    assert row[gs.SHEET_COLUMNS.index("sqm_eff")] == 120
    assert row[gs.SHEET_COLUMNS.index("sqm_build_eff")] == 140
    assert row[gs.SHEET_COLUMNS.index("lot_sqm_eff")] == 300
    assert row[gs.SHEET_COLUMNS.index("garden_sqm_eff")] == 50


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


_HYPERLINK_DISPLAY_RE = __import__("re").compile(
    r'^=HYPERLINK\("[^"]*",\s*"([^"]*)"\)$'
)


def _display(v):
    """Mimic gspread's get_all_values: return cell display text, not formulas."""
    s = "" if v is None else str(v)
    m = _HYPERLINK_DISPLAY_RE.match(s)
    return m.group(1) if m else s


def _row_to_strings(row):
    """Convert a typed row list back to the dict shape `_read_existing` returns."""
    out = {}
    for i, col in enumerate(gs.SHEET_COLUMNS):
        v = row[i] if i < len(row) else ""
        out[col] = _display(v)
    return out


def _make_existing_row(listing_dict, *, disappeared_on="", last_changed="", change_log=""):
    """Build a sheet-row dict (strings) from a listing dict, plus audit columns.
    Includes a sync_shadow that mirrors the editable columns — simulates a row
    that was previously written by the sync."""
    data = gs._build_data_dict(listing_dict)
    shadow = {col: data.get(col) for col in gs.EDITABLE_COLUMNS}
    out: dict[str, str] = {}
    for col in gs.SHEET_COLUMNS:
        if col == "disappeared_on":
            out[col] = disappeared_on
        elif col == "last_changed":
            out[col] = last_changed
        elif col == "change_log":
            out[col] = change_log
        elif col == "sync_shadow":
            out[col] = json.dumps(shadow, ensure_ascii=False)
        else:
            v = gs._coerce_cell(data.get(col))
            out[col] = "" if v == "" else str(v)
    return out
