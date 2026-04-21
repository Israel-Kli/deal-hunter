"""Heuristic investment scorer. Ported from Eilons' auto_score().

Five weighted components: price-vs-market, yield, physical attrs, negotiation, risk.
Output: score in [1, 10] and a reasons dict explaining the contributions.

Tune market_refs below per your target neighborhoods after a soak run.
"""

from __future__ import annotations

from typing import Any

from deal_hunter.models import Listing

# Default ₪/sqm reference bands per (city_substring, neighborhood_substring).
# First match wins. Strings match Hebrew text in city/neighborhood.
MARKET_REFS: list[tuple[str, str, int, int]] = [
    # Tel Aviv premium
    ("תל אביב", "צפון הישן", 50_000, 65_000),
    ("תל אביב", "רוטשילד",    50_000, 65_000),
    ("תל אביב", "הבימה",      50_000, 65_000),
    ("תל אביב", "לב העיר",   50_000, 65_000),
    ("תל אביב", "לב תל אביב", 50_000, 65_000),
    # Tel Aviv default
    ("תל אביב", "",           38_000, 52_000),
    # Haifa
    ("חיפה",    "כרמל",       18_000, 28_000),
    ("חיפה",    "",           12_000, 20_000),
    # Ramat Gan default
    ("רמת גן", "",            28_000, 38_000),
    # Ariel
    ("אריאל",   "",           12_000, 18_000),
    # Bet Shemesh
    ("בית שמש", "",           15_000, 22_000),
]
FALLBACK_BAND = (25_000, 40_000)


def market_band(city: str, neighborhood: str) -> tuple[int, int]:
    for city_sub, nbhd_sub, lo, hi in MARKET_REFS:
        if city_sub in city and (not nbhd_sub or nbhd_sub in neighborhood):
            return lo, hi
    return FALLBACK_BAND


def score_listing(listing: Listing) -> tuple[float, dict[str, Any]]:
    """Return (score in [1,10], reasons dict)."""
    score = 5.0
    reasons: dict[str, Any] = {}

    price = listing.price
    ppsqm = listing.price_per_sqm or 0
    city = listing.city
    neighborhood = listing.neighborhood

    # 1. PRICE vs MARKET (30% → ±3)
    # Prefer fair_price_estimate from real comps; fall back to MARKET_REFS bands.
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

    if ppsqm:
        mid = (lo + hi) / 2
        if ppsqm < lo * 0.85:
            score += 3; reasons["price_vs_market"] = "exceptional (>15% below band)"
        elif ppsqm < lo:
            score += 2; reasons["price_vs_market"] = "below band"
        elif ppsqm < mid:
            score += 1; reasons["price_vs_market"] = "below midpoint"
        elif ppsqm > hi * 1.1:
            score -= 2; reasons["price_vs_market"] = "much above band"
        elif ppsqm > hi:
            score -= 1; reasons["price_vs_market"] = "above band"
        else:
            reasons["price_vs_market"] = "in band"

    # 2. RENTAL YIELD (20% → ±2)
    if price:
        mult = 0.003
        if "תל אביב" in city: mult = 0.0025
        elif "חיפה" in city: mult = 0.004
        elif "אריאל" in city: mult = 0.0035
        elif "בית שמש" in city: mult = 0.0035
        est_monthly = price * mult
        gross_yield_pct = (est_monthly * 12 / price) * 100
        reasons["est_yield_pct"] = round(gross_yield_pct, 2)
        if gross_yield_pct >= 5:   score += 2
        elif gross_yield_pct >= 3.5: score += 1
        elif gross_yield_pct < 2.5: score -= 1

    # 3. PHYSICAL (20% → ±2)
    amen = 0.0
    if listing.parking:   amen += 0.5
    if listing.elevator:  amen += 0.4
    if listing.balcony:   amen += 0.3
    if listing.mamad:     amen += 0.4
    if listing.renovated: amen += 0.6
    if listing.floor is not None and listing.floor >= 4 and listing.elevator:
        amen += 0.3
    if listing.floor == 0:
        amen -= 0.5
    amen = min(2.0, max(-1.0, amen))
    score += amen
    reasons["amenity_bonus"] = round(amen, 2)

    # 4. NEGOTIATION (15% → ±1.5)
    if not listing.is_agent:
        score += 0.5; reasons["private_seller"] = True
    if listing.price_before and listing.price_before > price:
        drop = (listing.price_before - price) / listing.price_before * 100
        reasons["price_drop_pct"] = round(drop, 2)
        score += 1.0 if drop >= 5 else 0.5

    # 5. RISK (15% → -1.5)
    is_house = any(t in listing.listing_type for t in ["בית פרטי", "קוטג'", "דו משפחתי", "בית"])
    if not is_house and listing.floor is not None and listing.floor >= 3 and not listing.elevator:
        score -= 0.5; reasons["risk_no_elevator_high_floor"] = True
    if not listing.mamad:
        score -= 0.3; reasons["risk_no_mamad"] = True
    if price and price > 5_000_000:
        score -= 0.3; reasons["risk_liquidity"] = True

    final = max(1.0, min(10.0, round(score, 1)))
    reasons["final"] = final
    return final, reasons
