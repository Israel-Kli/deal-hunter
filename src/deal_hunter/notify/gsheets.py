"""Google Sheets sync — keeps a single tab in sync with current listings.

One row per (source, source_id). On each cycle:
- Existing rows are looked up by identity, diffed, and updated in place.
- New listings are appended.
- Listings whose `last_seen_at` is older than the cutoff get a `Disappeared On`
  stamp and a grey background.
- A JSON `Change Log` column accumulates the last N change entries per row.

Failures here never crash a scan cycle.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from deal_hunter.effective import (
    effective_garden_sqm,
    effective_lot_sqm,
    effective_price_per_sqm,
    effective_rooms,
    effective_sqm,
    effective_sqm_build,
    effective_units,
)

log = logging.getLogger(__name__)


# Column order (also the order of cells in each row).
SHEET_COLUMNS = [
    "score",
    "source",
    "city",
    "neighborhood",
    "street",
    "house_number",
    "rooms_eff",
    "sqm_eff",
    "sqm_build_eff",
    "lot_sqm_eff",
    "garden_sqm_eff",
    "floor",
    "price",
    "price_per_sqm_eff",
    "price_before",
    "features",
    "listing_type",
    "building_age",
    "units_count_eff",
    "first_listed_date",
    "last_seen_at",
    "why_score",
    "disappeared_on",
    "source_id",
    "last_changed",
    "change_log",
]

SHEET_HEADERS = [
    "Score",
    "Source",
    "City",
    "Neighborhood",
    "Street",
    "House #",
    "Rooms",
    "SQM",
    "Built m²",
    "Lot m²",
    "Garden m²",
    "Floor",
    "Price",
    "₪/m²",
    "Prev Price",
    "Features",
    "Type",
    "Age",
    "Units",
    "First Listed",
    "Last Seen",
    "Why Score",
    "Disappeared On",
    "Source ID",
    "Last Changed",
    "Change Log",
]

SHEET_LEGEND = [
    "Heuristic 1–10 investment score",
    "Scraper (yad2, onmap, ad, …); click to open listing",
    "City",
    "Neighborhood",
    "Street",
    "House number",
    "Effective room count (user override or extracted)",
    "Effective interior floor area, m²",
    "Effective built area, m² (מ\"ר בנוי) — includes walls / balconies / service areas",
    "Effective plot / lot size, m² (מגרש)",
    "Effective garden area, m² (גינה)",
    "Floor number",
    "Asking price, ₪",
    "Effective price per m² — gradient green→red (lower is better)",
    "Previous asking price, ₪ (if dropped)",
    "Compact features: P=parking, B=balcony, R=renovated, A=AC, M=mamad, E=elevator",
    "Private vs agent",
    "Years since year_built",
    "יחידות דיור count if extracted",
    "Earliest publish/first-seen date",
    "Most recent crawl that observed the listing",
    "Concise summary derived from score_reasons",
    "Date the listing stopped appearing in any source; row turns grey when set",
    "Per-source listing token; combined with Source forms the unique row key",
    "Most recent date any data field on this row differed from the previous cycle",
    "JSON log of the last N change entries; each entry has ts + changes/disappeared/reappeared",
]

SHEET_COL_WIDTHS = [
    55,   # score
    90,   # source (hyperlink)
    100,  # city
    120,  # neighborhood
    140,  # street
    55,   # house #
    55,   # rooms
    55,   # sqm
    65,   # built m²
    65,   # lot m²
    65,   # garden m²
    55,   # floor
    100,  # price
    80,   # ₪/m²
    100,  # prev price
    100,  # features
    70,   # listing_type
    50,   # age
    50,   # units
    100,  # first listed
    100,  # last seen
    220,  # why score
    100,  # disappeared on
    140,  # source id
    100,  # last changed
    280,  # change log
]

# Columns formatted as numbers with thousands separators.
PRICE_COLUMNS = ("price", "price_per_sqm_eff", "price_before")

# Columns whose values participate in the Change Log diff. Identity,
# rolling dates, and audit columns are excluded.
DATA_COLUMNS_FOR_DIFF = [
    "score",
    "city",
    "neighborhood",
    "street",
    "house_number",
    "rooms_eff",
    "sqm_eff",
    "sqm_build_eff",
    "lot_sqm_eff",
    "garden_sqm_eff",
    "floor",
    "price",
    "price_per_sqm_eff",
    "price_before",
    "features",
    "listing_type",
    "building_age",
    "units_count_eff",
]

LEGEND_ROWS = 1  # only row 1 (headers). Legend is attached as cell notes.


# ---- helpers ----------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _features_summary(d: dict[str, Any]) -> str:
    parts = []
    for label, key in (
        ("P", "parking"),
        ("B", "balcony"),
        ("R", "renovated"),
        ("A", "ac"),
        ("M", "mamad"),
        ("E", "elevator"),
    ):
        if d.get(key):
            parts.append(label)
    return " ".join(parts)


def _score_reasons_summary(d: dict[str, Any]) -> str:
    reasons = d.get("score_reasons") or {}
    if not isinstance(reasons, dict):
        return ""
    parts: list[str] = []
    for k, v in reasons.items():
        if isinstance(v, (int, float)):
            sign = "+" if v > 0 else ""
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            parts.append(f"{k}:{sign}{s}")
        else:
            parts.append(f"{k}:{v}")
    return ", ".join(parts[:8])


def _building_age(d: dict[str, Any]) -> int | None:
    yb = d.get("year_built")
    if isinstance(yb, (int, float)) and yb > 0:
        return datetime.now(timezone.utc).year - int(yb)
    return None


def _coerce_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, float):
        return round(v, 2)
    return v


def _to_cell_str(v: Any) -> str:
    c = _coerce_cell(v)
    if c == "":
        return ""
    return str(c)


def _build_data_dict(listing: dict[str, Any]) -> dict[str, Any]:
    """Return per-column raw value map for one listing (pre-coercion)."""
    return {
        "score": listing.get("score"),
        "source": listing.get("source", ""),
        "city": listing.get("city", ""),
        "neighborhood": listing.get("neighborhood", ""),
        "street": listing.get("street", ""),
        "house_number": listing.get("house_number", ""),
        "rooms_eff": effective_rooms(listing),
        "sqm_eff": effective_sqm(listing),
        "sqm_build_eff": effective_sqm_build(listing),
        "lot_sqm_eff": effective_lot_sqm(listing),
        "garden_sqm_eff": effective_garden_sqm(listing),
        "floor": listing.get("floor"),
        "price": listing.get("price"),
        "price_per_sqm_eff": effective_price_per_sqm(listing),
        "price_before": listing.get("price_before"),
        "features": _features_summary(listing),
        "listing_type": listing.get("listing_type", ""),
        "building_age": _building_age(listing),
        "units_count_eff": effective_units(listing),
        "first_listed_date": (listing.get("first_listed_date") or "")[:10],
        "last_seen_at": (str(listing.get("last_seen_at") or ""))[:10],
        "why_score": _score_reasons_summary(listing),
        "url": listing.get("url", ""),
        "source_id": listing.get("source_id", ""),
    }


def _row_from_data(
    data: dict[str, Any],
    *,
    disappeared_on: str = "",
    last_changed: str = "",
    change_log: str = "",
) -> list[Any]:
    out: list[Any] = []
    for col in SHEET_COLUMNS:
        if col == "source":
            src = data.get("source", "")
            url = data.get("url", "")
            if src and url:
                safe_url = url.replace('"', '%22')
                safe_src = str(src).replace('"', '\\"')
                out.append(f'=HYPERLINK("{safe_url}", "{safe_src}")')
            else:
                out.append(src)
        elif col == "disappeared_on":
            out.append(disappeared_on)
        elif col == "last_changed":
            out.append(last_changed)
        elif col == "change_log":
            out.append(change_log)
        else:
            out.append(_coerce_cell(data.get(col)))
    return out


def _diff(prior: dict[str, str], fresh: dict[str, Any]) -> dict[str, list]:
    """Return {col: [old, new]} for columns in DATA_COLUMNS_FOR_DIFF whose
    string-form differs between prior (read from sheet) and fresh (typed)."""
    changes: dict[str, list] = {}
    for col in DATA_COLUMNS_FOR_DIFF:
        new_str = _to_cell_str(fresh.get(col)).strip()
        old_str = (prior.get(col) or "").strip()
        if old_str == new_str:
            continue
        old_val: Any = old_str if old_str else None
        if isinstance(old_val, str):
            try:
                if "." in old_val:
                    old_val = float(old_val)
                else:
                    old_val = int(old_val)
            except ValueError:
                pass
        new_val = fresh.get(col)
        if isinstance(new_val, float):
            new_val = round(new_val, 2)
        changes[col] = [old_val, new_val]
    return changes


def _load_log(s: str | None) -> list[dict]:
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _trim_log(entries: list, cap: int) -> list:
    if cap > 0:
        return entries[:cap]
    return entries


def _date_desc_key(yyyy_mm_dd: str) -> str:
    """Invert digit chars so ascending sort yields descending dates."""
    if not yyyy_mm_dd:
        return "ZZZZ"
    return "".join(
        chr(ord("9") - (ord(c) - ord("0"))) if c.isdigit() else c
        for c in yyyy_mm_dd
    )


def _index_to_letter(idx: int) -> str:
    s = ""
    n = idx
    while True:
        s = chr(n % 26 + ord("A")) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _read_existing(ws) -> dict[tuple[str, str], dict[str, str]]:
    """Read all data rows and key by (source, source_id)."""
    try:
        values = ws.get_all_values()
    except Exception as e:
        log.warning("gsheets: get_all_values failed: %s", e)
        return {}
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in values[LEGEND_ROWS:]:
        if not row or all(not c for c in row):
            continue
        d: dict[str, str] = {}
        for i, col in enumerate(SHEET_COLUMNS):
            d[col] = row[i] if i < len(row) else ""
        key = (d.get("source", ""), d.get("source_id", ""))
        if key == ("", ""):
            continue
        out[key] = d
    return out


# ---- core build (pure, testable) -------------------------------------------


def _build_rows(
    listings: list[dict],
    existing: dict[tuple[str, str], dict[str, str]],
    *,
    cutoff_minutes: int,
    audit_max: int,
    today: str | None = None,
    now: datetime | None = None,
) -> tuple[list[list[Any]], list[int]]:
    """Build sheet rows + indexes of disappeared rows. Pure function."""
    today = today or _today()
    now = now or _now_utc()
    cutoff_ts = now.timestamp() - cutoff_minutes * 60

    records: list[tuple[tuple, dict[str, Any], str, str, str]] = []

    for listing in listings:
        source = listing.get("source", "")
        source_id = listing.get("source_id", "")
        if not source or not source_id:
            continue
        key = (source, source_id)
        prior = existing.get(key, {})

        last_seen = _parse_iso_dt(listing.get("last_seen_at"))
        is_active = last_seen is not None and last_seen.timestamp() >= cutoff_ts

        data = _build_data_dict(listing)
        diff = _diff(prior, data)

        log_entries = _load_log(prior.get("change_log"))

        prior_disappeared = (prior.get("disappeared_on") or "").strip()
        disappeared_on = prior_disappeared

        if is_active and prior_disappeared:
            log_entries.insert(0, {"ts": today, "reappeared": True})
            disappeared_on = ""
        elif not is_active and not prior_disappeared:
            log_entries.insert(0, {"ts": today, "disappeared": True})
            disappeared_on = today

        if diff:
            log_entries.insert(0, {"ts": today, "changes": diff})

        log_entries = _trim_log(log_entries, audit_max)

        last_changed = (prior.get("last_changed") or "").strip()
        if diff:
            last_changed = today
        elif not last_changed and not prior:
            last_changed = today

        change_log_str = (
            json.dumps(log_entries, ensure_ascii=False) if log_entries else ""
        )

        if not disappeared_on:
            fl = data.get("first_listed_date") or ""
            score_val = data.get("score") or 0
            try:
                score_f = float(score_val)
            except (TypeError, ValueError):
                score_f = 0.0
            sort_key = (0, _date_desc_key(fl), -score_f)
        else:
            sort_key = (1, _date_desc_key(disappeared_on), 0.0)

        records.append((sort_key, data, disappeared_on, last_changed, change_log_str))

    records.sort(key=lambda x: x[0])

    rows_out: list[list[Any]] = []
    disappeared_rows: list[int] = []
    for idx, (_sk, data, disappeared_on, last_changed, change_log_str) in enumerate(records):
        row = _row_from_data(
            data,
            disappeared_on=disappeared_on,
            last_changed=last_changed,
            change_log=change_log_str,
        )
        rows_out.append(row)
        if disappeared_on:
            disappeared_rows.append(idx)

    return rows_out, disappeared_rows


# ---- sync entry point ------------------------------------------------------


def sync(
    listings: list[dict],
    *,
    sheet_id: str,
    credentials_path: str,
    tab_name: str = "Deal Hunter-2026",
    disappeared_cutoff_minutes: int = 120,
    audit_max_entries: int = 20,
) -> bool:
    """Sync the listings snapshot into the given tab. Returns True on success."""
    if not sheet_id:
        log.warning("gsheets: sheet_id not set — skipping")
        return False
    if not os.path.isfile(credentials_path):
        log.warning("gsheets: credentials file not found at %s — skipping", credentials_path)
        return False

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        log.error("gsheets: missing dependency %s — pip install gspread google-auth", e)
        return False

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = Credentials.from_service_account_file(credentials_path, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_key(sheet_id)
    except Exception as e:
        log.error("gsheets: auth/open failed: %s", e)
        return False

    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        try:
            ws = sh.add_worksheet(
                title=tab_name,
                rows=max(len(listings) + LEGEND_ROWS + 50, 200),
                cols=len(SHEET_COLUMNS),
            )
            log.info("gsheets: created tab %r", tab_name)
        except Exception as e:
            log.error("gsheets: failed to create worksheet %r: %s", tab_name, e)
            return False

    existing = _read_existing(ws)
    rows_out, disappeared_rows = _build_rows(
        listings,
        existing,
        cutoff_minutes=disappeared_cutoff_minutes,
        audit_max=audit_max_entries,
    )

    return _write_block(ws, sh, rows_out, disappeared_rows)


def _write_block(ws, sh, rows_out: list[list[Any]], disappeared_rows: list[int]) -> bool:
    n_cols = len(SHEET_COLUMNS)
    last_col_letter = _index_to_letter(n_cols - 1)
    sheet_meta_id = ws.id
    data_start = LEGEND_ROWS + 1  # row 2 (1-indexed)
    data_end = LEGEND_ROWS + len(rows_out)  # last data row (1-indexed)

    # Resize if needed
    try:
        needed_rows = LEGEND_ROWS + len(rows_out) + 50
        new_rows = max(ws.row_count, needed_rows)
        new_cols = max(ws.col_count, n_cols)
        if new_rows != ws.row_count or new_cols != ws.col_count:
            ws.resize(rows=new_rows, cols=new_cols)
    except Exception as e:
        log.debug("gsheets: resize skipped: %s", e)

    # Header row only
    try:
        ws.update(
            range_name=f"A1:{last_col_letter}1",
            values=[SHEET_HEADERS],
            value_input_option="USER_ENTERED",
        )
    except Exception as e:
        log.warning("gsheets: failed to write header: %s", e)

    if rows_out:
        try:
            ws.update(
                range_name=f"A{data_start}:{last_col_letter}{data_end}",
                values=rows_out,
                value_input_option="USER_ENTERED",
            )
        except Exception as e:
            log.error("gsheets: failed to write data rows: %s", e)
            return False

    # Clear any stale rows below our new data range
    try:
        max_existing = ws.row_count
        first_empty = data_end + 1
        if max_existing >= first_empty:
            tail_rows = max_existing - first_empty + 1
            if tail_rows > 0:
                empty = [[""] * n_cols for _ in range(tail_rows)]
                ws.update(
                    range_name=f"A{first_empty}:{last_col_letter}{first_empty + tail_rows - 1}",
                    values=empty,
                    value_input_option="USER_ENTERED",
                )
    except Exception as e:
        log.debug("gsheets: tail clear skipped: %s", e)

    # Best-effort delete existing conditional format rules so we don't stack.
    for _ in range(50):
        try:
            sh.batch_update({
                "requests": [
                    {"deleteConditionalFormatRule": {"sheetId": sheet_meta_id, "index": 0}}
                ]
            })
        except Exception:
            break

    # Build the single big formatting batch.
    requests: list[dict] = []

    # Bold header row
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_meta_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }
    })

    # Freeze + header notes (legend on hover)
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_meta_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })
    for col_idx, legend_text in enumerate(SHEET_LEGEND):
        requests.append({
            "updateCells": {
                "range": {"sheetId": sheet_meta_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                "rows": [{"values": [{"note": legend_text}]}],
                "fields": "note",
            }
        })

    # Number formatting on price columns
    if rows_out:
        for col_name in PRICE_COLUMNS:
            col_idx = SHEET_COLUMNS.index(col_name)
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_meta_id,
                              "startRowIndex": LEGEND_ROWS, "endRowIndex": data_end,
                              "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            })

    # Reset all data rows to white background first
    if rows_out:
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_meta_id,
                          "startRowIndex": LEGEND_ROWS, "endRowIndex": data_end,
                          "startColumnIndex": 0, "endColumnIndex": n_cols},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
        # Grey out disappeared rows
        for ridx in disappeared_rows:
            abs_row = LEGEND_ROWS + ridx
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_meta_id,
                              "startRowIndex": abs_row, "endRowIndex": abs_row + 1,
                              "startColumnIndex": 0, "endColumnIndex": n_cols},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    # Gradient conditional formatting on ₪/m² (lower = green, higher = red)
    if rows_out:
        ppsqm_idx = SHEET_COLUMNS.index("price_per_sqm_eff")
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_meta_id,
                        "startRowIndex": LEGEND_ROWS, "endRowIndex": data_end,
                        "startColumnIndex": ppsqm_idx, "endColumnIndex": ppsqm_idx + 1,
                    }],
                    "gradientRule": {
                        "minpoint": {"color": {"red": 0.34, "green": 0.78, "blue": 0.4},
                                     "type": "MIN"},
                        "midpoint": {"color": {"red": 1.0, "green": 0.92, "blue": 0.4},
                                     "type": "PERCENTILE", "value": "50"},
                        "maxpoint": {"color": {"red": 0.94, "green": 0.5, "blue": 0.5},
                                     "type": "MAX"},
                    },
                },
                "index": 0,
            }
        })

    # Column widths
    for col_idx, px in enumerate(SHEET_COL_WIDTHS):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_meta_id, "dimension": "COLUMNS",
                          "startIndex": col_idx, "endIndex": col_idx + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        })

    # Basic filter on header + data range (replaces any existing filter)
    requests.append({
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet_meta_id,
                    "startRowIndex": 0,
                    "endRowIndex": max(data_end, 1),
                    "startColumnIndex": 0,
                    "endColumnIndex": n_cols,
                }
            }
        }
    })

    try:
        sh.batch_update({"requests": requests})
    except Exception as e:
        log.warning("gsheets: formatting batch failed: %s", e)

    log.info(
        "gsheets sync: wrote %d rows (%d disappeared)",
        len(rows_out),
        len(disappeared_rows),
    )
    return True
