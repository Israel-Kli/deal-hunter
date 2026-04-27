"""Simplestate adapter — Angular SSR + JSON API (simplestate.me).

Fetches property listings from the Simplestate REST API for a given business profile.
The feed endpoint returns clean JSON with type, rooms, price, address, city, street,
neighborhood, description, and parking. Built sqm and lot sqm are mined from description
text. Detail enrichment calls the per-property API endpoint.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import urlencode

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Listing

log = logging.getLogger(__name__)

WEB_BASE = "https://www.simplestate.me"
API_HOST = "https://server.simplestate.me"

SIMPLESTATE_HEADERS = {
    "Accept": "application/json",
    "Origin": WEB_BASE,
    "Referer": f"{WEB_BASE}/",
}

PROPERTY_TYPE_MAP = {
    "דירה": "דירה",
    "דירת גן": "דירת גן",
    "דו משפחתי": "דו משפחתי",
    "דופלקס": "דופלקס",
    "בית פרטי": "בית פרטי",
    "קוטג'": "קוטג'",
    "קוטג": "קוטג'",
    "וילה": "וילה",
    "פנטהאוז": "פנטהאוז",
    "מגרש": "מגרש",
}


class SimplestateAdapter:
    source = "simplestate"

    def __init__(
        self,
        business_ids: list[int],
        search: dict[str, Any],
        *,
        page_size: int = 100,
        request_delay_sec: float = 1.5,
    ):
        self.business_ids = business_ids
        self.search = search
        self.page_size = page_size
        self.request_delay = request_delay_sec

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        for biz_id in self.business_ids:
            yield from self._iter_business(biz_id)

    def fetch_detail(self, listing: Listing) -> Listing:
        source_id = listing.source_id
        biz_id = listing.source_payload.get("_business_id", "")
        prop_id = listing.source_payload.get("_property_id") or listing.source_payload.get("_id") or source_id

        url = f"{API_HOST}/api/business_view/{biz_id}/real_estate/property/{prop_id}"
        data = fetch(url, headers=SIMPLESTATE_HEADERS)
        if not isinstance(data, dict):
            return listing

        body = data.get("body") or data
        prop = (body.get("data") or {}) if isinstance(body, dict) else {}

        if isinstance(prop, dict):
            desc = prop.get("description") or ""
            if desc:
                listing.description = listing.description or desc
                listing.lot_sqm = listing.lot_sqm or _extract_lot_sqm(desc)
                listing.garden_sqm = listing.garden_sqm or _extract_garden_sqm(desc)

            if isinstance(prop.get("parking_spaces"), int):
                listing.parking = listing.parking or prop["parking_spaces"] > 0

            # Additional images from detail
            gallery = prop.get("photos") or []
            for photo in gallery:
                if isinstance(photo, dict):
                    url = photo.get("url") or photo.get("image") or photo.get("full")
                    if url and isinstance(url, str) and url not in listing.images:
                        listing.images.append(url)

            if prop.get("floor") is not None and listing.floor is None:
                listing.floor = prop["floor"]

            if prop.get("elevator") is True:
                listing.elevator = True
            if prop.get("ac") is True or prop.get("air_conditioner") is True:
                listing.ac = True
            if prop.get("mamad") is True:
                listing.mamad = True
            if prop.get("balcony") is True:
                listing.balcony = True
            if prop.get("renovated") is True:
                listing.renovated = True

        time.sleep(self.request_delay)
        return listing

    def _iter_business(self, biz_id: int) -> Iterable[Listing]:
        filter_stats: dict[str, int] = {}

        for page in range(1, 20):
            params = {
                "page[number]": page,
                "page[size]": self.page_size,
            }
            qs = urlencode(params)
            url = f"{API_HOST}/api/business_view/{biz_id}/properties-feed?{qs}"

            data = fetch(url, headers=SIMPLESTATE_HEADERS)
            if not isinstance(data, dict):
                log.warning("Simplestate biz %d: bad response", biz_id)
                break

            items = (data.get("data") or [])
            if not items and "body" in data:
                body = data["body"]
                items = (body.get("data") or []) if isinstance(body, dict) else []

            if not items:
                break

            log.info("Simplestate biz %d page %d: %d items", biz_id, page, len(items))
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                listing, reason = self._parse(raw, biz_id)
                if reason:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1
                if listing:
                    yield listing

            last_page = data.get("last_page", 1)
            if page >= last_page:
                break
            time.sleep(self.request_delay)

        if filter_stats:
            total = sum(filter_stats.values())
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("Simplestate biz %d filter stats (%d): %s", biz_id, total, summary)

    def _parse(self, item: dict[str, Any], biz_id: int) -> tuple[Listing | None, str | None]:
        s = self.search

        # Only "מכירה" (for-sale)
        deal_type = item.get("deal_type") or ""
        if deal_type != "מכירה":
            return None, "not_for_sale"

        prop_id = item.get("id")
        if not prop_id:
            return None, "no_id"

        # Price
        raw_price = item.get("price")
        if raw_price is False or raw_price is None or not isinstance(raw_price, (int, float)) or raw_price <= 0:
            return None, "bad_price"
        price = int(raw_price)

        if s.get("price_min") and price < s["price_min"]:
            return None, "price_out_of_range"
        if s.get("price_max") and price > s["price_max"]:
            return None, "price_out_of_range"

        # Rooms
        rooms = item.get("rooms")
        if isinstance(rooms, (int, float)):
            if s.get("rooms_min") and float(rooms) < s["rooms_min"]:
                return None, "rooms_out_of_range"
            if s.get("rooms_max") and float(rooms) > s["rooms_max"]:
                return None, "rooms_out_of_range"
            rooms_f = float(rooms)
        else:
            rooms_f = None

        # Type
        raw_type = item.get("type") or ""
        listing_type = PROPERTY_TYPE_MAP.get(raw_type, raw_type)

        # Address parts
        city = item.get("city") or ""
        street = item.get("street") or ""
        neighborhood = item.get("neighborhood") or ""
        full_address = item.get("full_address") or item.get("address_display") or ""
        address = ", ".join(filter(None, [full_address or f"{street}, {neighborhood}, {city}"]))

        # Description (mine for sqm / lot)
        description = item.get("description") or ""
        sqm_i = _extract_built_sqm(description)
        lot_sqm = _extract_lot_sqm(description)
        garden_sqm = _extract_garden_sqm(description)

        if s.get("min_sqm") and sqm_i and sqm_i < s["min_sqm"]:
            return None, "sqm_too_small"

        # Parking
        parking_spaces = item.get("parking_spaces")
        parking = isinstance(parking_spaces, (int, float)) and parking_spaces > 0

        # Floor
        floor_val = item.get("floor")
        floor_i = int(floor_val) if isinstance(floor_val, (int, float)) else None

        # Image
        images: list[str] = []
        preview = item.get("preview_photo") or {}
        if isinstance(preview, dict):
            img_url = preview.get("url") or preview.get("small_size_url") or ""
            if img_url:
                images.append(img_url)

        # Detail URL
        source_id = str(prop_id)
        url = f"{WEB_BASE}/business-view/{biz_id}/real_estate/property/{prop_id}"

        # Tags
        tags: list[str] = []
        if listing_type:
            tags.append(listing_type)

        return Listing(
            source="simplestate",
            source_id=source_id,
            url=url,
            city=city,
            neighborhood=neighborhood,
            street=street,
            house_number="",
            address=address,
            rooms=rooms_f,
            sqm=sqm_i,
            floor=floor_i,
            price=price,
            price_per_sqm=round(price / sqm_i) if sqm_i else None,
            listing_type=listing_type,
            is_agent=True,
            parking=parking,
            description=description,
            images=images,
            tags=tags,
            lot_sqm=lot_sqm,
            garden_sqm=garden_sqm,
            source_payload={
                "_business_id": biz_id,
                "_property_id": prop_id,
            },
        ), None


# ── helpers ──────────────────────────────────────────────────────────────────


_SQM_BUILT_RE = re.compile(
    r"(?:שטח\s*(?:בנוי|מבונה|דירה|הבית|הדירה|נטו|ברוטו)?|בנוי|(?:כ-|כ)?)\s*(\d{2,4})\s*(?:מ[״\"']?ר|מטר)",
)
_LOT_RE = re.compile(
    r"(?:מגרש|קרקע|המגרש)[^.\n]{0,80}?(\d{3,5})\s*(?:מ[״\"']?ר|מטר)",
)
_GARDEN_RE = re.compile(
    r"(?:גינ[הת]|חצר)[^.\n]{0,40}?(\d{2,4})\s*מ[״\"']?ר",
)


def _extract_built_sqm(text: str) -> int | None:
    m = _SQM_BUILT_RE.search(text)
    if m:
        val = int(m.group(1))
        if 10 <= val <= 9999:
            return val
    return None


def _extract_lot_sqm(text: str) -> int | None:
    m = _LOT_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def _extract_garden_sqm(text: str) -> int | None:
    m = _GARDEN_RE.search(text)
    if m:
        val = int(m.group(1))
        if 5 <= val <= 9999:
            return val
    return None
