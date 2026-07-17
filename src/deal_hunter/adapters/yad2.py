"""Yad2 for-sale adapter. Talks to Yad2's internal Next.js data API.

Ported from Eilons/realestate-opportunity-finder (MIT/personal-use upstream).
Restructured to fit the ScraperAdapter protocol and the canonical Listing model.
"""

from __future__ import annotations

import html as _html
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
from deal_hunter.normalize.year_built import extract_year_built

log = logging.getLogger(__name__)

BASE = "https://www.yad2.co.il"
SALE_PAGE = f"{BASE}/realestate/forsale"
ITEM_URL = f"{BASE}/item/{{}}"

# Modern Yad2 API gateway. Serves clean JSON and — unlike www.yad2.co.il — is not
# Radware-blocked from datacenter IPs, so it needs no build-id scrape and no cookies.
GW_MAP = "https://gw.yad2.co.il/realestate-feed/forsale/map"
# Per-token detail. The map feed's markers omit dates + free-text description;
# this endpoint carries item.dates (created/updated/ends/rebounced), the Hebrew
# ad body, and structured inProperty amenity flags. Also gw = not Radware-blocked.
GW_ITEM = "https://gw.yad2.co.il/realestate-item/{}"
GW_HEADERS = {"Origin": BASE, "Referer": f"{BASE}/"}


def _num(v: Any) -> str:
    """Render a numeric filter value without a spurious trailing .0 (gw wants 5, not 5.0)."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _split_bbox(lat_min: float, lon_min: float, lat_max: float, lon_max: float, n: int):
    """Yield an n×n grid of (lat_min, lon_min, lat_max, lon_max) sub-boxes.

    The gw map endpoint caps at ~200 markers per call and clusters dense areas, so
    we tile the city box and de-dupe by token to enumerate every matching listing.
    """
    dlat = (lat_max - lat_min) / n
    dlon = (lon_max - lon_min) / n
    for i in range(n):
        for j in range(n):
            yield (
                lat_min + i * dlat, lon_min + j * dlon,
                lat_min + (i + 1) * dlat, lon_min + (j + 1) * dlon,
            )

AGENCY_KEYWORDS = [
    "תיווך", 'נדל"ן', "מתווך", "סוכנ", "נכסים",
    "אנגלו סכסון", "רימקס", "RE/MAX", "קולדוול", "century", "סנצ'ורי",
]


class Yad2Adapter:
    source = "yad2"
    enrich_always = True  # map markers omit dates + description; fetch_detail fills them from the gw item endpoint

    def __init__(self, cities: list[dict[str, Any]], search: dict[str, Any], *, max_pages: int = 10, request_delay_sec: float = 3.0):
        self.cities = cities
        self.search = search
        self.max_pages = max_pages
        self.request_delay = request_delay_sec
        self._build_id: str | None = None

    # ---- public ScraperAdapter surface ---------------------------------

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        for city in self.cities:
            yield from self._iter_city_gw(city)

    def _filter_qs(self) -> str:
        s = self.search
        parts = [
            f"minRooms={_num(s['rooms_min'])}",
            f"maxRooms={_num(s['rooms_max'])}",
            f"minPrice={_num(s['price_min'])}",
            f"maxPrice={_num(s['price_max'])}",
        ]
        if s.get("min_sqm"):
            parts.append(f"squareMeterMin={_num(s['min_sqm'])}")
        return "&".join(parts)

    def _iter_city_gw(self, city: dict[str, Any]) -> Iterable[Listing]:
        region_id = city.get("yad2_region_id")
        bbox = city.get("yad2_bbox") or ""
        if not region_id or not bbox:
            log.warning(
                "Yad2 %s: missing yad2_region_id/yad2_bbox — skipping (gw feed needs them)",
                city.get("name"),
            )
            return
        try:
            lat_min, lon_min, lat_max, lon_max = (float(x) for x in bbox.split(","))
        except ValueError:
            log.error(
                "Yad2 %s: bad yad2_bbox %r (want lat_min,lon_min,lat_max,lon_max)",
                city.get("name"), bbox,
            )
            return
        tiles = max(1, int(city.get("yad2_tiles", 3) or 3))
        qs = self._filter_qs()
        seen: set[str] = set()
        filter_stats: dict[str, int] = {}
        emitted = 0
        clusters_left = 0
        for (la0, lo0, la1, lo1) in _split_bbox(lat_min, lon_min, lat_max, lon_max, tiles):
            box = f"{la0:.5f},{lo0:.5f},{la1:.5f},{lo1:.5f}"
            url = f"{GW_MAP}?region={region_id}&bBox={box}&zoom=18&{qs}"
            data = fetch(url, headers=GW_HEADERS)
            if not isinstance(data, dict):
                continue
            block = data.get("data", {}) or {}
            markers = block.get("markers", []) or []
            clusters_left += len(block.get("clusters", []) or [])
            for raw in markers:
                if not isinstance(raw, dict):
                    continue
                tok = raw.get("token")
                if not tok or tok in seen:
                    continue
                seen.add(tok)
                listing, reason = self._parse(raw, city)
                if reason:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1
                if listing:
                    emitted += 1
                    yield listing
            time.sleep(self.request_delay + random.uniform(0.3, 0.8))
        log.info(
            "Yad2 %s (gw): tiles=%d unique_markers=%d emitted=%d clusters=%d",
            city.get("name"), tiles * tiles, len(seen), emitted, clusters_left,
        )
        if clusters_left:
            # At zoom 18 the residual clusters are typically pins in neighbouring
            # cities (dropped by the _parse city filter), not missed target-city
            # listings. Kept at debug to avoid crying wolf every cycle.
            log.debug(
                "Yad2 %s (gw): %d cluster(s) left unexpanded (usually neighbouring-city pins)",
                city.get("name"), clusters_left,
            )
        if filter_stats:
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("Yad2 %s (gw) filter stats: %s", city.get("name"), summary)

    def fetch_detail(self, listing: Listing) -> Listing:
        """Enrich from the gw item endpoint. The map markers this adapter feeds on
        omit dates + description, so fill them here (plus structured amenity flags).
        gw is not Radware-blocked, so — unlike www item pages — this works from a
        datacenter IP with no cookies. Mutates and returns `listing`."""
        token = listing.source_id
        if not token:
            return listing
        time.sleep(random.uniform(0.4, 0.9))  # be polite; runs once per surviving listing
        data = fetch(GW_ITEM.format(token), headers=GW_HEADERS)
        item = data.get("data") if isinstance(data, dict) else None
        if isinstance(item, dict):
            _apply_gw_item(listing, item)
            log.debug(
                "Yad2 enrich gw-item ok: token=%s desc_len=%d created=%s",
                token, len(listing.description or ""), listing.created_at or "-",
            )
        else:
            log.debug("Yad2 enrich gw-item: no data for token=%s", token)
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
        ]
        if s.get("min_sqm"):
            params.append(("squareMeterMin", str(s["min_sqm"])))
        area = city.get("yad2_area")
        city_code = city.get("city_code")
        if area and city_code:
            params.append(("area", str(area)))
            params.append(("city", str(city_code)))
        if page > 1:
            params.append(("page", str(page)))
        qs = "&".join(f"{k}={v}" for k, v in params)
        slug = city.get("yad2_region") or city.get("slug", "israel")
        return f"{BASE}/realestate/_next/data/{build_id}/forsale/{slug}.json?{qs}"

    def _iter_city(self, build_id: str, city: dict[str, Any]) -> Iterable[Listing]:
        filter_stats: dict[str, int] = {}
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
                listing, reason = self._parse(raw, city)
                if reason:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1
                if listing:
                    yield listing
            if not _has_next_page(data):
                break
            time.sleep(self.request_delay + random.uniform(0.5, 1.5))
        if filter_stats:
            total_filtered = sum(filter_stats.values())
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("Yad2 %s filter stats (%d filtered): %s", city["name"], total_filtered, summary)

    def _parse(self, item: dict[str, Any], city: dict[str, Any]) -> tuple[Listing | None, str | None]:
        s = self.search
        token = item.get("token")
        if not token:
            return None, "no_token"

        details = item.get("additionalDetails", {}) or {}
        rooms = details.get("roomsCount")
        if rooms is not None and not (s["rooms_min"] <= rooms <= s["rooms_max"]):
            log.debug("Yad2 filtered: reason=rooms_out_of_range token=%s rooms=%s expected=[%s, %s]", token, rooms, s["rooms_min"], s["rooms_max"])
            return None, "rooms_out_of_range"

        # Property type filter (house-only: בית פרטי/קוטג', דו משפחתי)
        allowed_types = s.get("property_types")
        if allowed_types:
            prop = (details.get("property") or {})
            prop_text = prop.get("text", "")
            if prop_text not in allowed_types:
                log.debug("Yad2 filtered: reason=property_type_filtered token=%s prop_text=%s allowed=%s", token, prop_text, allowed_types)
                return None, "property_type_filtered"

        addr = item.get("address", {}) or {}
        house = addr.get("house", {}) or {}
        floor = house.get("floor")
        if s.get("exclude_ground_floor") and floor == 0:
            log.debug("Yad2 filtered: reason=ground_floor token=%s", token)
            return None, "ground_floor"

        price = item.get("price")
        if not price or price < s["price_min"] or price > s["price_max"]:
            log.debug("Yad2 filtered: reason=price_out_of_range token=%s price=%s expected=[%s, %s]", token, price, s["price_min"], s["price_max"])
            return None, "price_out_of_range"

        street = (addr.get("street", {}) or {}).get("text", "") or ""
        neighborhood = (addr.get("neighborhood", {}) or {}).get("text", "") or ""
        city_name = (addr.get("city", {}) or {}).get("text", "") or ""
        expected_city = city.get("hebrew_name", city.get("name", ""))
        if expected_city and city_name != expected_city:
            log.debug("Yad2 filtered: reason=city_mismatch token=%s city=%s expected=%s", token, city_name, expected_city)
            return None, "city_mismatch"
        house_num = str(house.get("number", "") or "")
        address_str = ", ".join(filter(None, [f"{street} {house_num}".strip(), neighborhood, city_name]))

        sqm_advertised = details.get("squareMeter")
        sqm_build = (item.get("metaData", {}) or {}).get("squareMeterBuild")
        size = sqm_build or sqm_advertised
        if s.get("min_sqm") and size and size < s["min_sqm"]:
            log.debug("Yad2 filtered: reason=sqm_too_small token=%s sqm=%s min_sqm=%s", token, size, s["min_sqm"])
            return None, "sqm_too_small"

        meta = item.get("metaData", {}) or {}
        images = list(meta.get("images", []) or [])
        cover = meta.get("coverImage", "")
        if cover and cover not in images:
            images.insert(0, cover)

        dates_block = _extract_dates(item)
        created_at_str = dates_block.get("created_at", "")
        publish_date: datetime | None = None
        if created_at_str:
            try:
                publish_date = datetime.fromisoformat(created_at_str)
            except ValueError:
                publish_date = None
        if publish_date is None:
            publish_date = _publish_date_from_images(images)
        max_age = s.get("max_listing_age_days", 30)
        if publish_date and publish_date < datetime.now() - timedelta(days=max_age):
            log.debug("Yad2 filtered: reason=too_old token=%s publish_date=%s max_age=%d", token, publish_date.strftime("%Y-%m-%d") if publish_date else "?", max_age)
            return None, "too_old"

        tags_raw = item.get("tags", []) or []
        tag_names = " ".join(t.get("name", "") for t in tags_raw if isinstance(t, dict))
        tl = tag_names.lower()

        is_agent = _detect_agent(item, tag_names)

        price_per_sqm = round(price / size) if price and size and size > 0 else None
        coords = addr.get("coords", {}) or {}

        log.debug("Yad2 parsed: token=%s price=%d rooms=%s sqm=%s city=%s neighborhood=%s", token, price, rooms, size, city_name, neighborhood)
        pub_s = created_at_str or (publish_date.strftime("%Y-%m-%d") if publish_date else "")
        desc = item.get("description") or item.get("text") or item.get("info") or ""
        if not desc:
            desc = meta.get("description", "") or meta.get("text", "") or ""
        desc = _html.unescape(desc.strip()) if desc else ""
        year_built = extract_year_built(desc)
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
            description=desc,
            images=images,
            tags=[t.get("name", "") for t in tags_raw if isinstance(t, dict)],
            lat=coords.get("lat"),
            lon=coords.get("lon"),
            publish_date=pub_s,
            first_listed_date=pub_s,
            created_at=dates_block.get("created_at", ""),
            updated_at=dates_block.get("updated_at", ""),
            ends_at=dates_block.get("ends_at", ""),
            rebounced_at=dates_block.get("rebounced_at", ""),
            source_payload={"_slug": city.get("yad2_region") or city.get("slug", "")},
            year_built=year_built,
        ), None

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
                    log.debug("Yad2 enrich JSON ok: token=%s desc_len=%d", token, len(listing.description or ""))
                    # Still return None — caller must do an HTML fetch for comps
                    return None
                else:
                    log.debug("Yad2 enrich: no item in JSON for token=%s", token)
        html_url = f"{BASE}/realestate/item/{slug}/{token}"
        html = fetch(html_url, as_json=False)
        if html:
            _enrich_from_html(listing, html)
            log.debug("Yad2 enrich HTML ok: token=%s html_len=%d", token, len(html))
        else:
            log.debug("Yad2 enrich HTML failed: token=%s", token)
        # Extract year_built from description after enrichment
        if listing.year_built is None:
            listing.year_built = extract_year_built(listing.description)
        return html


def _extract_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    sections_found: list[str] = []
    for q in data.get("pageProps", {}).get("dehydratedState", {}).get("queries", []) or []:
        sd = q.get("state", {}).get("data", {})
        if not isinstance(sd, dict):
            continue
        for key in ("private", "agency", "platinum", "items", "feed_items"):
            lst = sd.get(key)
            if isinstance(lst, list):
                sections_found.append(key)
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
    if sections_found:
        log.debug("Yad2 _extract_items: sections=%s raw=%d unique=%d", sections_found, len(items), len(out))
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


_DATES_KEY_MAP = {
    "createdAt": "created_at",
    "updatedAt": "updated_at",
    "endsAt": "ends_at",
    "rebouncedAt": "rebounced_at",
}


def _extract_dates(item: dict[str, Any]) -> dict[str, str]:
    """Pull `item.dates` (createdAt/updatedAt/endsAt/rebouncedAt), slice each
    ISO timestamp to YYYY-MM-DD. Returns dict keyed by the model field names."""
    dates = item.get("dates")
    if not isinstance(dates, dict):
        return {}
    out: dict[str, str] = {}
    for src_key, dst_key in _DATES_KEY_MAP.items():
        val = dates.get(src_key)
        if isinstance(val, str) and len(val) >= 10:
            out[dst_key] = val[:10]
    return out


def _publish_date_from_images(images: list[str]) -> datetime | None:
    """Best-effort date from Yad2 CDN image paths (layout varies by CDN version)."""
    patterns = (
        r"/Pic/(\d{4})(\d{2})/(\d{2})/",  # /Pic/202604/03/…
        r"/Pic/(\d{4})/(\d{2})/(\d{2})/",  # /Pic/2026/04/03/…
    )
    for img in images[:5]:
        if not isinstance(img, str):
            continue
        for pat in patterns:
            m = re.search(pat, img)
            if m:
                try:
                    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    break
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


def _gw_item_description(item: dict[str, Any]) -> str:
    """Pick the real free-text ad body from a gw item.

    Usually it's `metaData.description`. But for some ads that field holds a short
    auto-generated SEO string (e.g. "מכירה, דירה, קומה 5, אור יהודה") and the real
    body sits in `furnitureInfo`. The real body is invariably the longest of the
    candidates, so take the longest non-empty one.
    """
    meta = item.get("metaData", {}) or {}
    candidates = (
        item.get("furnitureInfo"),
        meta.get("description"),
        item.get("description"),
    )
    best = max(
        (c.strip() for c in candidates if isinstance(c, str)),
        key=len,
        default="",
    )
    return _html.unescape(best) if best else ""


def _apply_gw_item(listing: Listing, item: dict[str, Any]) -> None:
    """Enrich a Listing in place from a gw /realestate-item/{token} `data` object.

    Fills the fields the map-markers feed can't: dates, the Hebrew description, and
    structured amenity flags (more reliable than tag/keyword guessing). Amenity
    flags are only ever flipped on — never cleared — so a marker-derived True wins.
    """
    desc = _gw_item_description(item)
    if desc:
        listing.description = desc

    dates_block = _extract_dates(item)
    if dates_block:
        listing.created_at = dates_block.get("created_at", listing.created_at)
        listing.updated_at = dates_block.get("updated_at", listing.updated_at)
        listing.ends_at = dates_block.get("ends_at", listing.ends_at)
        listing.rebounced_at = dates_block.get("rebounced_at", listing.rebounced_at)
        if listing.created_at:
            listing.publish_date = listing.created_at
            listing.first_listed_date = listing.created_at

    in_prop = item.get("inProperty", {})
    if isinstance(in_prop, dict):
        if in_prop.get("includeElevator"):       listing.elevator = True
        if in_prop.get("includeParking"):        listing.parking = True
        if in_prop.get("includeSecurityRoom"):   listing.mamad = True
        if in_prop.get("includeBalcony"):        listing.balcony = True
        if in_prop.get("includeAirconditioner"): listing.ac = True
        if in_prop.get("isRenovated"):           listing.renovated = True

    ad = item.get("additionalDetails", {}) or {}
    if (ad.get("parkingSpacesCount") or 0) > 0:
        listing.parking = True
    if (ad.get("balconiesCount") or 0) > 0:
        listing.balcony = True
    if not listing.sqm_build and ad.get("squareMeterBuild"):
        listing.sqm_build = ad.get("squareMeterBuild")
        size = listing.sqm_build or listing.sqm
        if listing.price and size:
            listing.price_per_sqm = round(listing.price / size)

    if listing.year_built is None:
        listing.year_built = extract_year_built(listing.description)


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
        listing.description = _html.unescape(desc.strip())

    dates_block = _extract_dates(item)
    if dates_block:
        listing.created_at = dates_block.get("created_at", listing.created_at)
        listing.updated_at = dates_block.get("updated_at", listing.updated_at)
        listing.ends_at = dates_block.get("ends_at", listing.ends_at)
        listing.rebounced_at = dates_block.get("rebounced_at", listing.rebounced_at)
        if listing.created_at:
            listing.publish_date = listing.created_at
            listing.first_listed_date = listing.created_at

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


def _enrich_from_html(listing: Listing, html: str) -> None:
    desc = ""
    for pattern in (
        # name before content
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
        # content before name
        r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']description["\']',
        # og:description property before content
        r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        # og:description content before property
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:description["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            desc = _html.unescape(raw)
            break

    # Parse __NEXT_DATA__ for structured enrichment (inProperty) + description fallback
    nd_m = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>\s*(.*?)\s*</script>', html, re.DOTALL)
    if nd_m:
        try:
            nd = json.loads(nd_m.group(1))
            for q in nd.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", []) or []:
                sd = q.get("state", {}).get("data", {})
                if isinstance(sd, dict) and sd.get("token") == listing.source_id:
                    in_prop = sd.get("inProperty", {})
                    if isinstance(in_prop, dict):
                        if in_prop.get("includeElevator"):     listing.elevator = True
                        if in_prop.get("includeParking"):      listing.parking = True
                        if in_prop.get("includeSecurityRoom"): listing.mamad = True
                        if in_prop.get("includeBalcony"):      listing.balcony = True
                        if in_prop.get("includeAirconditioner"): listing.ac = True
                    if not desc:
                        meta = sd.get("metaData", {}) or {}
                        md = meta.get("description", "") or meta.get("text", "")
                        if md:
                            desc = _html.unescape(md.strip())
                    break
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if desc:
        listing.description = desc

    hl = html.lower()
    if not listing.elevator and "מעלית" in hl:
        listing.elevator = True
    if not listing.parking and ("חנייה" in hl or "חניה" in hl):
        listing.parking = True
    if not listing.mamad and ('ממ"ד' in hl or "ממד" in hl):
        listing.mamad = True
    if not listing.balcony and "מרפסת" in hl:
        listing.balcony = True
    if not listing.ac and ("מיזוג" in hl or "מזגן" in hl):
        listing.ac = True
    if not listing.renovated and ("משופצ" in hl or "שיפוץ" in hl):
        listing.renovated = True
