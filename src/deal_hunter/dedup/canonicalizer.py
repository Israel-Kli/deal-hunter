"""Cross-source listing dedup via canonical address keys.

Strategy
--------
1. **Exact key** — ``sha1(normalize_street + house_number + rooms_bucket + sqm_bucket)``
   Fast, deterministic. Works when two sources report the same address
   with minor cosmetic differences (e.g. "רח' הרצל 12" vs "רחוב הרצל 12").

2. **Fuzzy fallback** — when the exact key finds no match, we compare
   against all existing canonical groups in the same city using
   ``rapidfuzz`` Levenshtein ratio on the normalized address.
   If the best match exceeds the threshold (default 0.85), we merge
   into that group; otherwise we create a new canonical_id.

Canonical IDs
-------------
Format: ``CAN-<sha1-prefix-12>`` (e.g. ``CAN-a1b2c3d4e5f6``).
Assigned per-scan-cycle to freshly upserted rows that lack a canonical_id.

Price-spread signal
-------------------
Once multiple source-listings share the same canonical_id, the dashboard
can show MAX(price) - MIN(price) across sources — a listing appearing
cheaper on ad.co.il than Yad2 for the same unit is an actionable signal.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from deal_hunter.models import Listing
from deal_hunter.normalize.hebrew import (
    canonicalize_address,
    extract_street_number,
    rooms_bucket,
    sqm_bucket,
)

log = logging.getLogger(__name__)

# Minimum fuzzy ratio to consider two addresses the same listing.
# 0.85 means ~85% character-level similarity on the normalized address.
FUZZY_THRESHOLD = 0.85


@dataclass
class CanonicalGroup:
    """A cluster of listings that represent the same physical unit."""

    canonical_id: str
    city: str
    street_normalized: str
    house_number: str
    rooms_b: str
    sqm_b: str
    members: list[Listing] = field(default_factory=list)

    @property
    def price_spread(self) -> int:
        """MAX(price) - MIN(price) across members."""
        prices = [m.price for m in self.members]
        return max(prices) - min(prices) if prices else 0

    @property
    def cheapest(self) -> Listing | None:
        return min(self.members, key=lambda m: m.price) if self.members else None

    @property
    def most_expensive(self) -> Listing | None:
        return max(self.members, key=lambda m: m.price) if self.members else None


def make_canonical_id(raw: str) -> str:
    """Deterministic ID from a string. Returns 'CAN-<12 hex chars>'."""
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"CAN-{h}"


def exact_key(listing: Listing) -> str:
    """Build the deterministic exact-match key.

    Uses normalized street + house number + rooms bucket + sqm bucket.
    City is NOT included — same building could span city boundaries in
    rare cases (e.g. Givatayim / Ramat Gan border). If needed, prepend
    city later.
    """
    street, number = extract_street_number(listing.street or listing.address)
    street_norm = canonicalize_address(street)
    raw = "|".join([
        street_norm,
        number or listing.house_number or "",
        rooms_bucket(listing.rooms),
        sqm_bucket(listing.sqm),
    ])
    return make_canonical_id(raw)


def fuzzy_match(
    listing: Listing,
    existing_groups: dict[str, CanonicalGroup],
    *,
    threshold: float = FUZZY_THRESHOLD,
) -> str | None:
    """Find the best fuzzy match for a listing among existing canonical groups.

    Returns the canonical_id of the best match if ratio >= threshold, else None.
    """
    candidate_addr = canonicalize_address(
        f"{listing.city} {listing.street} {listing.house_number}"
    )
    if not candidate_addr.strip():
        return None

    best_id: str | None = None
    best_score = 0.0

    # Only compare within the same city to avoid false positives
    city_norm = canonicalize_address(listing.city)
    for gid, group in existing_groups.items():
        if canonicalize_address(group.city) != city_norm:
            continue
        group_addr = canonicalize_address(
            f"{group.city} {group.street_normalized} {group.house_number}"
        )
        ratio = fuzz.ratio(candidate_addr, group_addr) / 100.0
        if ratio > best_score:
            best_score = ratio
            best_id = gid

    if best_score >= threshold and best_id:
        return best_id
    return None


def _group_exact_key(group: CanonicalGroup) -> str:
    """Compute the exact key for a CanonicalGroup from its stored fields."""
    return make_canonical_id(
        "|".join([group.street_normalized, group.house_number, group.rooms_b, group.sqm_b])
    )


def dedup_batch(
    listings: list[Listing],
    existing_groups: dict[str, CanonicalGroup] | None = None,
) -> dict[str, CanonicalGroup]:
    """Assign canonical_ids to a batch of listings.

    Args:
        listings: freshly fetched listings (may have canonical_id=None).
        existing_groups: previously known canonical groups from the DB.
            Mutated in-place with new members appended.

    Returns the updated groups dict.
    """
    groups: dict[str, CanonicalGroup] = existing_groups or {}

    # Build an index: computed exact key → group canonical_id
    ek_to_gid: dict[str, str] = {}
    for gid, group in groups.items():
        ek_to_gid[_group_exact_key(group)] = gid

    for listing in listings:
        if listing.canonical_id and listing.canonical_id in groups:
            groups[listing.canonical_id].members.append(listing)
            continue

        # 1. Try exact key against existing groups (both direct and via computed index)
        ek = exact_key(listing)
        if ek in groups:
            listing.canonical_id = ek
            groups[ek].members.append(listing)
            continue
        if ek in ek_to_gid:
            gid = ek_to_gid[ek]
            listing.canonical_id = gid
            groups[gid].members.append(listing)
            continue

        # 2. Try fuzzy match
        fuzzy_id = fuzzy_match(listing, groups)
        if fuzzy_id:
            listing.canonical_id = fuzzy_id
            groups[fuzzy_id].members.append(listing)
            log.debug(
                "fuzzy match: %s %s → %s",
                listing.source, listing.source_id, fuzzy_id,
            )
            continue

        # 3. Create new group
        street, number = extract_street_number(listing.street or listing.address)
        cid = ek  # exact key becomes the new group id
        listing.canonical_id = cid
        groups[cid] = CanonicalGroup(
            canonical_id=cid,
            city=listing.city,
            street_normalized=canonicalize_address(street),
            house_number=number or listing.house_number or "",
            rooms_b=rooms_bucket(listing.rooms),
            sqm_b=sqm_bucket(listing.sqm),
            members=[listing],
        )
        ek_to_gid[ek] = cid

    return groups


def load_existing_groups(conn) -> dict[str, CanonicalGroup]:
    """Load canonical groups from the SQLite DB.

    Groups listings by canonical_id (only rows where canonical_id IS NOT NULL).
    Returns a dict[canonical_id -> CanonicalGroup].
    """
    rows = conn.execute(
        "SELECT source, source_id, url, city, neighborhood, street, "
        "house_number, address, rooms, sqm, floor, price, price_before, "
        "price_per_sqm, listing_type, is_agent, parking, elevator, balcony, "
        "ac, mamad, renovated, description, images_json, tags_json, lat, lon, "
        "publish_date, first_seen_at, last_seen_at, canonical_id, "
        "fair_price_estimate, fair_price_low, fair_price_high, score, "
        "score_reasons, source_payload "
        "FROM listings WHERE canonical_id IS NOT NULL"
    ).fetchall()

    import json
    from deal_hunter.models import Listing

    groups: dict[str, CanonicalGroup] = {}
    for r in rows:
        listing = Listing(
            source=r["source"],
            source_id=r["source_id"],
            url=r["url"],
            city=r["city"],
            neighborhood=r["neighborhood"],
            street=r["street"],
            house_number=r["house_number"],
            address=r["address"],
            rooms=r["rooms"],
            sqm=r["sqm"],
            floor=r["floor"],
            price=r["price"],
            price_before=r["price_before"],
            price_per_sqm=r["price_per_sqm"],
            listing_type=r["listing_type"],
            is_agent=bool(r["is_agent"]),
            parking=bool(r["parking"]),
            elevator=bool(r["elevator"]),
            balcony=bool(r["balcony"]),
            ac=bool(r["ac"]),
            mamad=bool(r["mamad"]),
            renovated=bool(r["renovated"]),
            description=r["description"],
            images=json.loads(r["images_json"] or "[]"),
            tags=json.loads(r["tags_json"] or "[]"),
            lat=r["lat"],
            lon=r["lon"],
            publish_date=r["publish_date"],
            canonical_id=r["canonical_id"],
            fair_price_estimate=r["fair_price_estimate"],
            fair_price_low=r["fair_price_low"],
            fair_price_high=r["fair_price_high"],
            score=r["score"],
            score_reasons=json.loads(r["score_reasons"] or "{}"),
            source_payload=json.loads(r["source_payload"] or "{}"),
        )
        cid = listing.canonical_id
        if cid not in groups:
            street, number = extract_street_number(listing.street or listing.address)
            groups[cid] = CanonicalGroup(
                canonical_id=cid,
                city=listing.city,
                street_normalized=canonicalize_address(street),
                house_number=number or listing.house_number or "",
                rooms_b=rooms_bucket(listing.rooms),
                sqm_b=sqm_bucket(listing.sqm),
                members=[],
            )
        groups[cid].members.append(listing)

    return groups
