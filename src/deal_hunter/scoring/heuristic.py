from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from deal_hunter.effective import (
    effective_garden_sqm,
    effective_price_per_sqm,
    effective_units,
)
from deal_hunter.models import Listing
from deal_hunter.scoring.description_signals import (
    combined_search_text,
    garden_bonus_scoring,
    multi_unit_bonus_and_matches,
)

log = logging.getLogger(__name__)

# Or Chana school in Or Yehuda — used as a positive proximity signal.
# Change these constants (or lift to ScoringCfg) if the focus school moves.
OR_CHANA_LAT = 32.025982
OR_CHANA_LON = 34.864421


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _school_distance_bonus(listing: Listing) -> tuple[float, dict[str, Any]]:
    if listing.lat is None or listing.lon is None:
        return 0.0, {}
    km = _haversine_km(listing.lat, listing.lon, OR_CHANA_LAT, OR_CHANA_LON)
    if km < 0.5:
        bonus = 1.5
    elif km < 1.0:
        bonus = 1.0
    elif km < 1.5:
        bonus = 0.6
    elif km < 2.5:
        bonus = 0.2
    elif km < 4.0:
        bonus = 0.0
    else:
        bonus = -0.5
    return bonus, {
        "school_distance_km": round(km, 2),
        "school_distance_bonus": round(bonus, 2),
    }


def _physical_features_bonus(listing: Listing) -> tuple[float, dict[str, Any]]:
    """Bonuses for amenities that matter for apartments + houses alike."""
    bonus = 0.0
    detail: dict[str, Any] = {}
    if listing.parking:
        bonus += 0.3
        detail["parking"] = True
    if listing.balcony:
        bonus += 0.2
        detail["balcony"] = True
    if listing.renovated:
        bonus += 0.4
        detail["renovated"] = True
    if bonus:
        detail["features_bonus"] = round(bonus, 2)
    return bonus, detail

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
    ("אור יהודה", "",         28_000, 38_000),
]
FALLBACK_BAND = (25_000, 40_000)


def market_band(city: str, neighborhood: str) -> tuple[int, int]:
    for city_sub, nbhd_sub, lo, hi in MARKET_REFS:
        if city_sub in city and (not nbhd_sub or nbhd_sub in neighborhood):
            return lo, hi
    return FALLBACK_BAND


def _price_vs_market_delta(ppsqm: float, lo: float, hi: float) -> tuple[float, str]:
    if ppsqm <= lo:
        span = max(lo, 1.0)
        t = min(ppsqm, lo) / span
        return round(3.0 - t * 1.0, 2), "below band"
    if ppsqm <= hi:
        span = max(hi - lo, 1.0)
        t = (ppsqm - lo) / span
        return round(2.0 - t * 2.0, 2), "in band"
    s = max(hi * 0.2, 1.0)
    t = min(ppsqm - hi, hi * 0.2) / s
    return round(0.0 - t * 2.0, 2), "above band"


def score_listing(listing: Listing) -> tuple[float, dict[str, Any]]:
    score = 5.0
    reasons: dict[str, Any] = {}

    price = listing.price
    ppsqm = float(effective_price_per_sqm(listing) or 0)
    city = listing.city
    neighborhood = listing.neighborhood

    e_units = effective_units(listing)
    e_garden = effective_garden_sqm(listing)

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
        reasons["price_per_sqm_used"] = int(ppsqm)

    text = combined_search_text(listing)
    unit_bon, unit_matches, unit_src = multi_unit_bonus_and_matches(text, e_units)
    if unit_bon > 0:
        score += unit_bon
        reasons["description_unit_hit"] = True
        reasons["matched_unit_phrases"] = unit_matches
        reasons["description_unit_adjustment"] = round(unit_bon, 2)
        reasons["description_unit_source"] = unit_src
        if e_units is not None:
            reasons["units_count_used"] = e_units
    else:
        reasons["description_unit_hit"] = False

    out_bonus, out_detail = garden_bonus_scoring(
        listing, text, e_garden
    )
    if out_bonus > 0:
        score += out_bonus
        reasons.update(out_detail)
    if e_garden is not None:
        reasons["garden_sqm_used"] = e_garden

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

    school_bonus, school_detail = _school_distance_bonus(listing)
    if school_detail:
        score += school_bonus
        reasons.update(school_detail)

    feat_bonus, feat_detail = _physical_features_bonus(listing)
    if feat_bonus:
        score += feat_bonus
        reasons.update(feat_detail)

    if listing.price_before and listing.price_before > price:
        drop = (listing.price_before - price) / listing.price_before * 100
        reasons["price_drop_pct"] = round(drop, 2)
        drop_bonus = min(1.0, max(0.0, drop / 5.0))
        reasons["price_drop_bonus"] = round(drop_bonus, 2)
        score += drop_bonus

    if listing.year_built:
        reasons["year_built"] = listing.year_built
        current_yr = datetime.utcnow().year
        age = current_yr - listing.year_built
        reasons["building_age"] = age
        if age <= 5:
            adj = 0.3
            reasons["building_age_label"] = "new_construction"
        elif age <= 15:
            adj = 0.1
            reasons["building_age_label"] = "modern"
        elif age <= 35:
            adj = 0.0
            reasons["building_age_label"] = "standard"
        elif age <= 50:
            adj = -0.2
            reasons["building_age_label"] = "older"
        else:
            adj = -0.4
            reasons["building_age_label"] = "very_old"
        reasons["building_age_adjustment"] = round(adj, 2)
        score += adj

    if price and price > 5_000_000:
        score -= 0.3
        reasons["risk_liquidity"] = True

    final = max(1.0, min(10.0, round(score, 1)))
    reasons["final"] = final
    log.debug(
        "score %s/%s (%s, %s): %.1f %s seller=%s",
        listing.source, listing.source_id, city, neighborhood,
        final, reasons.get("price_vs_market", "?"),
        reasons.get("seller_channel", "?"),
    )
    return final, reasons
