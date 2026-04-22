"""Heuristic investment scorer.

Components: smooth price-vs-market, description-based multi-unit signal,
garden + room-count bonus (capped), amenities (no elevator), seller channel
(private vs agent), price-drop ramp, risk (no mamad, liquidity).
Output: score in [1, 10] and a reasons dict.
"""

from __future__ import annotations

from typing import Any

from deal_hunter.models import Listing
from deal_hunter.scoring.description_signals import (
    combined_search_text,
    multi_unit_penalty_and_matches,
    outdoor_and_rooms_bonus,
)

MARKET_REFS: list[tuple[str, str, int, int]] = [
    ("תל אביב", "צפון הישן", 50_000, 65_000),
    ("תל אביב", "רוטשילד",    50_000, 65_000),
    ("תל אביב", "הבימה",      50_000, 65_000),
    ("תל אביב", "לב העיר",   50_000, 65_000),
    ("תל אביב", "לב תל אביב", 50_000, 65_000),
    ("תל אביב", "",           38_000, 52_000),
    ("חיפה",    "כרמל",       18_000, 28_000),
    ("חיפה",    "",           12_000, 20_000),
    ("רמת גן", "",            28_000, 38_000),
    ("אריאל",   "",           12_000, 18_000),
    ("בית שמש", "",           15_000, 22_000),
]
FALLBACK_BAND = (25_000, 40_000)


def market_band(city: str, neighborhood: str) -> tuple[int, int]:
    for city_sub, nbhd_sub, lo, hi in MARKET_REFS:
        if city_sub in city and (not nbhd_sub or nbhd_sub in neighborhood):
            return lo, hi
    return FALLBACK_BAND


def _price_vs_market_delta(ppsqm: float, lo: float, hi: float) -> tuple[float, str]:
    mid = (lo + hi) / 2
    if ppsqm <= lo * 0.85:
        return 3.0, "exceptional (>15% below band)"
    if ppsqm < lo:
        span = max(lo * 0.15, 1.0)
        t = (ppsqm - lo * 0.85) / span
        return 3.0 - t * 1.0, "below band"
    if ppsqm < mid:
        span = max(mid - lo, 1.0)
        t = (ppsqm - lo) / span
        return 2.0 - t * 1.0, "below midpoint"
    if ppsqm <= hi:
        span = max(hi - mid, 1.0)
        t = (ppsqm - mid) / span
        return 1.0 - t * 1.0, "in band"
    if ppsqm <= hi * 1.1:
        span = max(hi * 0.1, 1.0)
        t = (ppsqm - hi) / span
        return 0.0 - t * 1.0, "above band"
    if ppsqm <= hi * 1.2:
        span = max(hi * 0.1, 1.0)
        t = (ppsqm - hi * 1.1) / span
        return -1.0 - t * 1.0, "much above band"
    return -2.0, "much above band"


def score_listing(listing: Listing) -> tuple[float, dict[str, Any]]:
    score = 5.0
    reasons: dict[str, Any] = {}

    price = listing.price
    ppsqm = float(listing.price_per_sqm or 0)
    city = listing.city
    neighborhood = listing.neighborhood

    if listing.fair_price_estimate and listing.sqm and listing.sqm > 0:
        fair_ppsqm = listing.fair_price_estimate / listing.sqm
        lo = int(fair_ppsqm * 0.90)
        hi = int(fair_ppsqm * 1.10)
        reasons["market_band"] = [lo, hi]
        reasons["market_band_source"] = "comps"
    else:
        lo, hi = market_band(city, neighborhood)
        reasons["market_band"] = [lo, hi]
        reasons["market_band_source"] = "market_refs"

    if ppsqm > 0:
        delta, label = _price_vs_market_delta(ppsqm, float(lo), float(hi))
        score += delta
        reasons["price_vs_market"] = label
        reasons["price_vs_market_delta"] = round(delta, 2)

    text = combined_search_text(listing)
    unit_pen, unit_matches = multi_unit_penalty_and_matches(text)
    if unit_pen < 0:
        score += unit_pen
        reasons["description_unit_hit"] = True
        reasons["matched_unit_phrases"] = unit_matches
        reasons["description_unit_adjustment"] = round(unit_pen, 2)
    else:
        reasons["description_unit_hit"] = False

    out_bonus, out_detail = outdoor_and_rooms_bonus(listing, text)
    if out_bonus > 0:
        score += out_bonus
        reasons.update(out_detail)

    amen = 0.0
    if listing.parking:
        amen += 0.5
    if listing.balcony:
        amen += 0.3
    if listing.mamad:
        amen += 0.4
    if listing.renovated:
        amen += 0.6
    if listing.floor == 0:
        amen -= 0.5
    amen = min(2.0, max(-1.0, amen))
    score += amen
    reasons["amenity_bonus"] = round(amen, 2)

    if listing.is_agent:
        adj = -0.48
        reasons["seller_channel"] = "agent"
        reasons["seller_adjustment"] = adj
        score += adj
    else:
        adj = 0.72
        reasons["seller_channel"] = "private"
        reasons["seller_adjustment"] = adj
        score += adj

    if listing.price_before and listing.price_before > price:
        drop = (listing.price_before - price) / listing.price_before * 100
        reasons["price_drop_pct"] = round(drop, 2)
        drop_bonus = min(1.0, max(0.0, drop / 5.0))
        reasons["price_drop_bonus"] = round(drop_bonus, 2)
        score += drop_bonus

    is_house = any(
        t in listing.listing_type for t in ("בית פרטי", "קוטג'", "דו משפחתי", "בית")
    )
    if not is_house and not listing.mamad:
        score -= 0.3
        reasons["risk_no_mamad"] = True
    if price and price > 5_000_000:
        score -= 0.3
        reasons["risk_liquidity"] = True

    final = max(1.0, min(10.0, round(score, 1)))
    reasons["final"] = final
    return final, reasons
