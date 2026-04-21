"""Fair-price estimator.

Given a Listing, queries the `comps` table for comparable closed deals
(same city, same neighborhood OR nearby-city, ±20% sqm, matching rooms,
last N months) and returns (estimate, low, high) as ₪ totals.

Falls back to None when there are not enough comps (< MIN_COMPS).
Callers should populate listing.fair_price_* and pass them to the scorer.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MIN_COMPS = 3          # minimum sample size to trust an estimate
WINDOW_MONTHS = 18     # look back this many months


def _months_ago(months: int) -> str:
    cutoff = datetime.utcnow() - timedelta(days=months * 30)
    return cutoff.strftime("%Y-%m")


def estimate(
    conn: sqlite3.Connection,
    *,
    city: str,
    neighborhood: str,
    rooms: float | None,
    sqm: int | None,
    window_months: int = WINDOW_MONTHS,
    min_comps: int = MIN_COMPS,
) -> tuple[int, int, int] | None:
    """Return (estimate, low, high) in ₪, or None if not enough data.

    Strategy:
      1. Pull comps: same city, same neighborhood (if known), rooms ±0.5,
         sqm ±20%, deal_date >= window cutoff.
      2. If fewer than min_comps after neighborhood filter, widen to city-only.
      3. Compute median ₪/sqm; low = 25th percentile × sqm; high = 75th × sqm.
    """
    if not sqm or sqm <= 0:
        return None

    cutoff = _months_ago(window_months)

    rows = _query_comps(conn, city=city, neighborhood=neighborhood,
                        rooms=rooms, sqm=sqm, cutoff=cutoff, strict_nbhd=True)
    if len(rows) < min_comps:
        rows = _query_comps(conn, city=city, neighborhood=neighborhood,
                            rooms=rooms, sqm=sqm, cutoff=cutoff, strict_nbhd=False)
    if len(rows) < min_comps:
        log.debug("fair_price: only %d comps for %s/%s — skipping", len(rows), city, neighborhood)
        return None

    ppsqm_list = sorted(
        r["price"] / r["sqm"]
        for r in rows
        if r["sqm"] and r["sqm"] > 0
    )
    if len(ppsqm_list) < min_comps:
        return None

    n = len(ppsqm_list)
    p25 = ppsqm_list[max(0, n // 4)]
    p50 = ppsqm_list[n // 2]
    p75 = ppsqm_list[min(n - 1, (3 * n) // 4)]

    estimate_val = int(p50 * sqm)
    low_val = int(p25 * sqm)
    high_val = int(p75 * sqm)

    log.debug(
        "fair_price: %d comps, ppsqm p25=%.0f p50=%.0f p75=%.0f → est=%d",
        n, p25, p50, p75, estimate_val,
    )
    return estimate_val, low_val, high_val


def _query_comps(
    conn: sqlite3.Connection,
    *,
    city: str,
    neighborhood: str,
    rooms: float | None,
    sqm: int,
    cutoff: str,
    strict_nbhd: bool,
) -> list[Any]:
    sqm_lo = sqm * 0.80
    sqm_hi = sqm * 1.20

    params: list[Any] = [city, cutoff, sqm_lo, sqm_hi]
    nbhd_clause = ""
    if strict_nbhd and neighborhood:
        nbhd_clause = "AND neighborhood = ?"
        params.append(neighborhood)

    rooms_clause = ""
    if rooms is not None:
        rooms_clause = "AND (rooms IS NULL OR ABS(rooms - ?) <= 0.5)"
        params.append(rooms)

    sql = f"""
        SELECT price, sqm
        FROM comps
        WHERE city = ?
          AND deal_date >= ?
          AND sqm BETWEEN ? AND ?
          {nbhd_clause}
          {rooms_clause}
        ORDER BY deal_date DESC
        LIMIT 50
    """
    rows = conn.execute(sql, params).fetchall()
    return [{"price": r[0], "sqm": r[1]} for r in rows]


def enrich_listing_fair_price(listing: Any, conn: sqlite3.Connection) -> None:
    """Compute and assign fair_price_* fields on listing in-place."""
    result = estimate(
        conn,
        city=listing.city,
        neighborhood=listing.neighborhood,
        rooms=listing.rooms,
        sqm=listing.sqm,
    )
    if result:
        listing.fair_price_estimate, listing.fair_price_low, listing.fair_price_high = result
