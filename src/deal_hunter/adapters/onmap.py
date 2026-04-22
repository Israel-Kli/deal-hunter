"""OnMap for-sale adapter. Talks to OnMap's public REST API at phoenix.onmap.co.il.

OnMap exposes an unauthenticated JSON feed (FeathersJS/Express under the hood).
No visitor token, no cookies, no bot protection observed as of 2026-04. Plain GET
with a browser-ish Origin/Referer is enough.

Feed endpoint:
    GET https://phoenix.onmap.co.il/v1/properties/mixed_search
        ?option=buy&section=residence&country=Israel
        &city=<slug>&is_mobile=false
        &$sort=-search_date&$limit=24&$skip=<offset>

Response shape: { data: [ {...item...}, ... ], meta: { hasNextPage, cityPolygon } }

Each item is already rich (address, price, rooms, sqm, floor, parking, images,
lat/lon) so `fetch_detail` is a no-op for now.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Listing
from deal_hunter.normalize.israeli_cities import hebrew_allowed_city_keys, hebrew_city_match_key

log = logging.getLogger(__name__)

FEED_URL = "https://phoenix.onmap.co.il/v1/properties/mixed_search"
WEB_BASE = "https://www.onmap.co.il"
PAGE_SIZE = 24

ONMAP_HEADERS = {
    "Accept": "application/json",
    "Origin": WEB_BASE,
    "Referer": f"{WEB_BASE}/",
}

# Hebrew agency keywords — OnMap sometimes omits agent flags; keep lightweight.
AGENCY_KEYWORDS = [
    "תיווך", 'נדל"ן', "מתווך", "סוכנ", "נכסים",
    "רימקס", "RE/MAX", "קולדוול", "century", "סנצ'ורי",
]


class OnMapAdapter:
    source = "onmap"

    def __init__(
        self,
        city_slugs: list[str],
        search: dict[str, Any],
        *,
        max_pages: int = 10,
        request_delay_sec: float = 1.5,
        allowed_cities: list[str] | None = None,
    ):
        self.city_slugs = city_slugs
        self.search = search
        self.max_pages = max_pages
        self.request_delay = request_delay_sec
        if allowed_cities:
            self._allowed_city_keys = hebrew_allowed_city_keys(list(allowed_cities))
        else:
            self._allowed_city_keys = None

    # ---- public ScraperAdapter surface ---------------------------------

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        for slug in self.city_slugs:
            yield from self._iter_city(slug)

    def fetch_detail(self, listing: Listing) -> Listing:
        # OnMap feed items are already complete; no detail page fetched today.
        return listing

    # ---- internals ------------------------------------------------------

    def _iter_city(self, city_slug: str) -> Iterable[Listing]:
        filter_stats: dict[str, int] = {}
        for page in range(self.max_pages):
            skip = page * PAGE_SIZE
            url = self._build_feed_url(city_slug, skip)
            data = fetch(url, headers=ONMAP_HEADERS)
            if not isinstance(data, dict):
                log.warning("OnMap %s page %d: empty/bad response", city_slug, page)
                break
            items = data.get("data") or []
            if not items:
                break
            log.info("OnMap %s skip=%d: %d items", city_slug, skip, len(items))
            for raw in items:
                listing, reason = self._parse(raw, city_slug)
                if reason:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1
                if listing is not None:
                    yield listing
            if not (data.get("meta") or {}).get("hasNextPage"):
                break
            time.sleep(self.request_delay + random.uniform(0.2, 0.8))
        if filter_stats:
            total_filtered = sum(filter_stats.values())
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("OnMap %s filter stats (%d filtered): %s", city_slug, total_filtered, summary)

    def _build_feed_url(self, city_slug: str, skip: int) -> str:
        params = [
            ("option", "buy"),
            ("section", "residence"),
            ("country", "Israel"),
            ("city", city_slug),
            ("is_mobile", "false"),
            ("$sort", "-search_date"),
            ("$limit", str(PAGE_SIZE)),
            ("$skip", str(skip)),
        ]
        return f"{FEED_URL}?{urlencode(params)}"

    def _parse(self, item: dict[str, Any], city_slug: str) -> tuple[Listing | None, str | None]:
        s = self.search

        # Only residential buy listings priced in ILS
        if item.get("search_option") != "buy":
            return None, "not_buy"
        if (item.get("currency") or "ILS") != "ILS":
            return None, "not_ils"

        et = item.get("entityType")
        if et is not None and et != "property":
            return None, "not_property"

        source_id = item.get("id")
        if not source_id:
            return None, "no_source_id"

        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return None, "bad_price"
        price = int(price)
        if price < s.get("price_min", 0) or price > s.get("price_max", 10**12):
            return None, "price_out_of_range"

        info = item.get("additional_info") or {}
        rooms = info.get("rooms")
        if isinstance(rooms, (int, float)):
            if not (s.get("rooms_min", 0) <= float(rooms) <= s.get("rooms_max", 99)):
                return None, "rooms_out_of_range"
            rooms_f = float(rooms)
        else:
            rooms_f = None

        area = info.get("area") or {}
        sqm = area.get("base") if isinstance(area.get("base"), (int, float)) else None
        if s.get("min_sqm") and sqm and sqm < s["min_sqm"]:
            return None, "sqm_too_small"
        sqm_i = int(sqm) if sqm else None

        floor_obj = info.get("floor") or {}
        floor_val = floor_obj.get("on_the")
        floor_i = int(floor_val) if isinstance(floor_val, (int, float)) else None
        if s.get("exclude_ground_floor") and floor_i == 0:
            return None, "ground_floor"

        addr = item.get("address") or {}
        he = addr.get("he") or {}
        city = he.get("city_name") or ""
        if self._allowed_city_keys is not None and hebrew_city_match_key(city) not in self._allowed_city_keys:
            return None, "city_not_allowed"
        neighborhood = he.get("neighborhood") or ""
        street = he.get("street_name") or ""
        house_num = he.get("house_number") or ""
        house_num_s = str(house_num) if house_num is not None else ""
        address_str = ", ".join(
            filter(None, [f"{street} {house_num_s}".strip(), neighborhood, city])
        )

        loc = addr.get("location") or {}
        lat = loc.get("lat") if isinstance(loc.get("lat"), (int, float)) else None
        lon = loc.get("lon") if isinstance(loc.get("lon"), (int, float)) else None

        created_dt = _parse_iso(item.get("created_at") or "")
        search_dt = _parse_iso(item.get("search_date") or "")
        if created_dt and search_dt:
            publish_dt = max(created_dt, search_dt)
            first_listed_dt = min(created_dt, search_dt)
        elif search_dt:
            publish_dt = search_dt
            first_listed_dt = search_dt
        elif created_dt:
            publish_dt = created_dt
            first_listed_dt = created_dt
        else:
            publish_dt = None
            first_listed_dt = None

        max_age = s.get("max_listing_age_days")
        if max_age and publish_dt:
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
            if publish_dt < cutoff:
                return None, "too_old"

        images = _extract_image_urls(item.get("images") or [])

        parking_obj = info.get("parking") or {}
        parking = bool(_to_int(parking_obj.get("aboveground")) or _to_int(parking_obj.get("underground")))

        slug = item.get("slug") or ""
        url = f"{WEB_BASE}/{slug}" if slug else f"{WEB_BASE}/property/{source_id}"

        tags_raw = addr.get("tags") or []
        tags = [str(t) for t in tags_raw if isinstance(t, (str, int, float))]

        return Listing(
            source="onmap",
            source_id=str(source_id),
            url=url,
            city=city,
            neighborhood=neighborhood,
            street=f"{street} {house_num_s}".strip(),
            house_number=house_num_s,
            address=address_str,
            rooms=rooms_f,
            sqm=sqm_i,
            floor=floor_i,
            price=price,
            price_per_sqm=round(price / sqm_i) if sqm_i else None,
            listing_type=item.get("property_type") or "",
            is_agent=False,  # OnMap feed doesn't flag this reliably; leave detection for enrichment
            parking=parking,
            elevator=False,   # not exposed in feed
            balcony=False,    # not exposed in feed
            ac=False,
            mamad=False,
            renovated=False,
            images=images,
            tags=tags,
            lat=lat,
            lon=lon,
            publish_date=publish_dt.strftime("%Y-%m-%d") if publish_dt else "",
            first_listed_date=first_listed_dt.strftime("%Y-%m-%d") if first_listed_dt else "",
            source_payload={"_city_slug": city_slug, "_slug": slug},
        ), None


# ── helpers ──────────────────────────────────────────────────────────────────


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def _parse_iso(s: str) -> datetime | None:
    if not s or not _ISO_RE.match(s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _extract_image_urls(imgs: list[Any]) -> list[str]:
    out: list[str] = []
    for img in imgs:
        if not isinstance(img, dict):
            continue
        url = img.get("gallery") or img.get("full") or img.get("thumbnail")
        if url and isinstance(url, str):
            out.append(url)
    return out
