"""Yad2 for-sale adapter. Talks to Yad2's internal Next.js data API.

Ported from Eilons/realestate-opportunity-finder (MIT/personal-use upstream).
Restructured to fit the ScraperAdapter protocol and the canonical Listing model.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Comp, Listing

log = logging.getLogger(__name__)

BASE = "https://www.yad2.co.il"
SALE_PAGE = f"{BASE}/realestate/forsale"
ITEM_URL = f"{BASE}/item/{{}}"

AGENCY_KEYWORDS = [
    "תיווך", 'נדל"ן', "מתווך", "סוכנ", "נכסים",
    "אנגלו סכסון", "רימקס", "RE/MAX", "קולדוול", "century", "סנצ'ורי",
]


class Yad2Adapter:
    source = "yad2"

    def __init__(self, cities: list[dict[str, Any]], search: dict[str, Any], *, max_pages: int = 10, request_delay_sec: float = 3.0):
        self.cities = cities
        self.search = search
        self.max_pages = max_pages
        self.request_delay = request_delay_sec
        self._build_id: str | None = None

    # ---- public ScraperAdapter surface ---------------------------------

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        bid = self._get_build_id()
        if not bid:
            return
        for city in self.cities:
            yield from self._iter_city(bid, city)

    def fetch_detail(self, listing: Listing) -> Listing:
        bid = self._get_build_id()
        if not bid:
            return listing
        slug = listing.source_payload.get("_slug", self.cities[0].get("slug", "tel-aviv-area"))
        self._enrich(listing, bid, slug)
        return listing

    def fetch_detail_with_comps(self, listing: Listing) -> tuple[Listing, list[Comp]]:
        """Enrich listing AND extract comps from the same HTML fetch. Returns (listing, comps)."""
        from deal_hunter.comps.yad2_deals import extract_comps_from_html

        bid = self._get_build_id()
        if not bid:
            return listing, []
        slug = listing.source_payload.get("_slug", self.cities[0].get("slug", "tel-aviv-area"))
        html = self._enrich(listing, bid, slug)
        comps: list[Comp] = []
        if html:
            comps = extract_comps_from_html(
                html, source_city=listing.city, source_neighborhood=listing.neighborhood
            )
        return listing, comps

    # ---- internals ------------------------------------------------------

    def _get_build_id(self) -> str | None:
        if self._build_id:
            return self._build_id
        html = fetch(SALE_PAGE, as_json=False)
        if not html:
            log.error("Cannot fetch Yad2 sale page")
            return None
        for pattern in (
            r"/_next/data/([a-zA-Z0-9_-]+)/",
            r'"buildId"\s*:\s*"([^"]+)"',
            r"/_next/static/([a-zA-Z0-9_-]{10,})/",
        ):
            m = re.search(pattern, html)
            if m:
                self._build_id = m.group(1)
                log.info("Yad2 build id: %s", self._build_id)
                return self._build_id
        log.error("Yad2 build id not found")
        return None

    def _build_feed_url(self, build_id: str, city: dict[str, Any], page: int = 1) -> str:
        s = self.search
        params = [
            ("minRooms", str(s["rooms_min"])),
            ("maxRooms", str(s["rooms_max"])),
            ("minPrice", str(s["price_min"])),
            ("maxPrice", str(s["price_max"])),
            ("multiCity", city["city_code"]),
        ]
        if s.get("min_sqm"):
            params.append(("squareMeterMin", str(s["min_sqm"])))
        if page > 1:
            params.append(("page", str(page)))
        qs = "&".join(f"{k}={v}" for k, v in params)
        slug = city.get("slug", "israel")
        return f"{BASE}/realestate/_next/data/{build_id}/forsale/{slug}.json?{qs}"

    def _iter_city(self, build_id: str, city: dict[str, Any]) -> Iterable[Listing]:
        for page in range(1, self.max_pages + 1):
            url = self._build_feed_url(build_id, city, page)
            data = fetch(url)
            if not data:
                break
            items = _extract_items(data)
            if not items:
                break
            log.info("Yad2 %s page %d: %d items", city["name"], page, len(items))
            for raw in items:
                listing = self._parse(raw, city)
                if listing:
                    yield listing
            if not _has_next_page(data):
                break
            time.sleep(self.request_delay + random.uniform(0.5, 1.5))

    def _parse(self, item: dict[str, Any], city: dict[str, Any]) -> Listing | None:
        s = self.search
        token = item.get("token")
        if not token:
            return None

        details = item.get("additionalDetails", {}) or {}
        rooms = details.get("roomsCount")
        if rooms is not None and not (s["rooms_min"] <= rooms <= s["rooms_max"]):
            return None

        # Property type filter (house-only: בית פרטי/קוטג', דו משפחתי)
        allowed_types = s.get("property_types")
        if allowed_types:
            prop = (details.get("property") or {})
            prop_text = prop.get("text", "")
            if prop_text not in allowed_types:
                return None

        addr = item.get("address", {}) or {}
        house = addr.get("house", {}) or {}
        floor = house.get("floor")
        if s.get("exclude_ground_floor") and floor == 0:
            return None

        price = item.get("price")
        if not price or price < s["price_min"] or price > s["price_max"]:
            return None

        street = (addr.get("street", {}) or {}).get("text", "") or ""
        neighborhood = (addr.get("neighborhood", {}) or {}).get("text", "") or ""
        city_name = (addr.get("city", {}) or {}).get("text", "") or city.get("name", "")
        house_num = str(house.get("number", "") or "")
        address_str = ", ".join(filter(None, [f"{street} {house_num}".strip(), neighborhood, city_name]))

        sqm_advertised = details.get("squareMeter")
        sqm_build = (item.get("metaData", {}) or {}).get("squareMeterBuild")
        size = sqm_build or sqm_advertised
        if s.get("min_sqm") and size and size < s["min_sqm"]:
            return None

        meta = item.get("metaData", {}) or {}
        images = list(meta.get("images", []) or [])
        cover = meta.get("coverImage", "")
        if cover and cover not in images:
            images.insert(0, cover)

        publish_date = _publish_date_from_images(images)
        max_age = s.get("max_listing_age_days", 30)
        if publish_date and publish_date < datetime.now() - timedelta(days=max_age):
            return None

        tags_raw = item.get("tags", []) or []
        tag_names = " ".join(t.get("name", "") for t in tags_raw if isinstance(t, dict))
        tl = tag_names.lower()

        is_agent = _detect_agent(item, tag_names)

        price_per_sqm = round(price / size) if price and size and size > 0 else None
        coords = addr.get("coords", {}) or {}

        return Listing(
            source="yad2",
            source_id=token,
            url=ITEM_URL.format(token),
            city=city_name,
            neighborhood=neighborhood,
            street=f"{street} {house_num}".strip(),
            house_number=house_num,
            address=address_str,
            rooms=rooms,
            sqm=sqm_advertised,
            sqm_build=sqm_build,
            floor=floor,
            price=price,
            price_before=item.get("priceBeforeTag"),
            price_per_sqm=price_per_sqm,
            listing_type=(details.get("property", {}) or {}).get("text", ""),
            is_agent=is_agent,
            parking="חנייה" in tl or "חניה" in tl,
            elevator="מעלית" in tl or bool(details.get("elevator") or details.get("hasElevator")),
            balcony="מרפסת" in tl or bool(details.get("balcony") or details.get("hasBalcony")),
            ac="מיזוג" in tl or "מזגן" in tl,
            mamad='ממ"ד' in tl or "ממד" in tl,
            renovated="משופצת" in tl or "שיפוץ" in tl,
            images=images,
            tags=[t.get("name", "") for t in tags_raw if isinstance(t, dict)],
            lat=coords.get("lat"),
            lon=coords.get("lon"),
            publish_date=publish_date.strftime("%Y-%m-%d") if publish_date else "",
            source_payload={"_slug": city.get("slug", "")},
        )

    def _enrich(self, listing: Listing, build_id: str, slug: str) -> str | None:
        """Enrich listing from JSON or HTML. Returns the HTML page (if fetched) for reuse."""
        token = listing.source_id
        time.sleep(1.0 + random.uniform(0.3, 1.0))
        for url in (
            f"{BASE}/realestate/_next/data/{build_id}/item/{slug}/{token}.json",
            f"{BASE}/realestate/_next/data/{build_id}/item/{token}.json",
        ):
            data = fetch(url)
            if data:
                item = _extract_item_from_json(data)
                if item:
                    _apply_json_enrichment(listing, item)
                    # Still return None — caller must do an HTML fetch for comps
                    return None
        html_url = f"{BASE}/realestate/item/{slug}/{token}"
        html = fetch(html_url, as_json=False)
        if html:
            m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if m:
                listing.description = m.group(1).strip()
            hl = html.lower()
            if not listing.elevator and "מעלית" in hl:
                listing.elevator = True
            if not listing.parking and ("חנייה" in hl or "חניה" in hl):
                listing.parking = True
            if not listing.mamad and ('ממ"ד' in hl or "ממד" in hl):
                listing.mamad = True
        return html


def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for q in data.get("pageProps", {}).get("dehydratedState", {}).get("queries", []) or []:
        sd = q.get("state", {}).get("data", {})
        if not isinstance(sd, dict):
            continue
        for key in ("private", "agency", "platinum", "items", "feed_items"):
            lst = sd.get(key)
            if isinstance(lst, list):
                for it in lst:
                    if isinstance(it, dict) and it.get("token"):
                        it["_feed_section"] = key
                        items.append(it)
    seen: set[str] = set()
    out = []
    for it in items:
        t = it.get("token")
        if t and t not in seen:
            seen.add(t)
            out.append(it)
    return out


def _has_next_page(data: dict[str, Any]) -> bool:
    for q in data.get("pageProps", {}).get("dehydratedState", {}).get("queries", []) or []:
        sd = q.get("state", {}).get("data", {})
        if isinstance(sd, list):
            continue
        if not isinstance(sd, dict):
            continue
        p = sd.get("pagination", {}) or {}
        if p and p.get("currentPage", 1) < p.get("totalPages", 1):
            return True
    return False


def _publish_date_from_images(images: list[str]) -> datetime | None:
    for img in images[:3]:
        m = re.search(r"/Pic/(\d{4})(\d{2})/(\d{2})/", img)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def _detect_agent(item: dict[str, Any], tag_names: str) -> bool:
    feed = item.get("_feed_section", "")
    if feed in ("agency", "platinum"):
        return True
    ad_type = (item.get("adType") or item.get("ad_type") or "").lower()
    if ad_type in ("agency", "business", "broker", "agent", "commercial"):
        return True
    if item.get("agency") or item.get("broker") or item.get("agencyName"):
        return True
    contact = item.get("contact", {}) or {}
    if isinstance(contact, dict):
        ct = (contact.get("type") or contact.get("contactType") or "").lower()
        if ct in ("agency", "broker", "agent", "business"):
            return True
        cname = contact.get("name") or contact.get("contactName") or ""
        if any(kw in cname for kw in AGENCY_KEYWORDS):
            return True
    seller = (item.get("metaData", {}) or {}).get("sellerName", "") or ""
    if any(kw in seller for kw in AGENCY_KEYWORDS):
        return True
    return any(kw in tag_names for kw in ["תיווך", "מתווך", "סוכן"])


def _extract_item_from_json(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    props = data.get("pageProps", data)
    item = props.get("itemData")
    if isinstance(item, dict):
        return item
    for q in props.get("dehydratedState", {}).get("queries", []) or []:
        sd = q.get("state", {}).get("data", {})
        if isinstance(sd, dict) and (sd.get("token") or sd.get("description")):
            return sd
    if data.get("token") or data.get("description"):
        return data
    return None


def _apply_json_enrichment(listing: Listing, item: dict[str, Any]) -> None:
    desc = ""
    for key in ("description", "text", "info"):
        v = item.get(key)
        if isinstance(v, str) and len(v) > 10:
            desc = v
            break
    if not desc:
        meta = item.get("metaData", {}) or {}
        desc = meta.get("description", "") or meta.get("text", "")
    if desc:
        listing.description = desc.strip()

    in_prop = item.get("inProperty", {})
    if isinstance(in_prop, dict):
        if in_prop.get("includeElevator"):     listing.elevator = True
        if in_prop.get("includeParking"):      listing.parking = True
        if in_prop.get("includeSecurityRoom"): listing.mamad = True
        if in_prop.get("includeBalcony"):      listing.balcony = True
        if in_prop.get("includeAirconditioner"): listing.ac = True

    blob = json.dumps(item, ensure_ascii=False).lower()
    if not listing.elevator and "מעלית" in blob:  listing.elevator = True
    if not listing.parking and ("חנייה" in blob or "חניה" in blob): listing.parking = True
    if not listing.mamad and ('ממ"ד' in blob or "ממד" in blob): listing.mamad = True
    if not listing.balcony and "מרפסת" in blob: listing.balcony = True
    if not listing.ac and ("מיזוג" in blob or "מזגן" in blob): listing.ac = True
    if not listing.renovated and ("משופצ" in blob or "שיפוץ" in blob): listing.renovated = True
