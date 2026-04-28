"""komo.co.il for-sale adapter.

komo.co.il is a server-rendered classifieds board. Listings are rendered as
``div[class*="modaaRow"]`` tables on the ``/code/nadlan/apartments-for-sale.asp``
feed. Each card carries price, rooms, sqm, floor, and property type inline — no
detail-page enrichment required for core fields, though the detail page at
``/code/nadlan/details/?modaaNum=<id>`` adds description and more images.

Feed endpoint:
    GET https://www.komo.co.il/code/nadlan/apartments-for-sale.asp
        ?nehes=5,2
        &cityName=<url-encoded Hebrew city>
        &fromRooms=<rooms_min>
        &toPrice=<price_max>

No pagination observed — all matching listings return on a single page.
"""

from __future__ import annotations

import html
import logging
import random
import re
import time
from typing import Any, Iterable
from urllib.parse import quote_plus, urlencode

from bs4 import BeautifulSoup, Tag

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Listing
from deal_hunter.normalize.israeli_cities import hebrew_allowed_city_keys, hebrew_city_match_key

log = logging.getLogger(__name__)

WEB_BASE = "https://www.komo.co.il"
FEED_PATH = "/code/nadlan/apartments-for-sale.asp"

KOMO_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    "Referer": f"{WEB_BASE}/",
}

_MOREDETAILS_RE = re.compile(
    r'(?P<prop_type>.+?)\s+(?P<rooms>[\d.]+)\s+חדרים\s+\((?P<sqm>\d+)\s*מ[״"\']ר\)\s+קומה:\s*(?P<floor>[^\s]+)\s*(?:מתוך\s*(?P<total_floors>\d+))?',
)


class KomoAdapter:
    source = "komo"

    def __init__(
        self,
        cities: list[str],
        search: dict[str, Any],
        *,
        request_delay_sec: float = 2.0,
        allowed_cities: list[str] | None = None,
    ):
        self.cities = cities
        self.search = search
        self.request_delay = request_delay_sec
        if allowed_cities:
            self._allowed_city_keys = hebrew_allowed_city_keys(list(allowed_cities))
        else:
            self._allowed_city_keys = None

    # ---- public ScraperAdapter surface ---------------------------------

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        for city in self.cities:
            yield from self._iter_city(city)

    def fetch_detail(self, listing: Listing) -> Listing:
        """Enrich description + extra images from the detail page."""
        try:
            html = fetch(listing.url, headers=KOMO_HEADERS, as_json=False)
        except Exception as e:
            log.debug("komo detail fetch failed for %s: %s", listing.source_id, e)
            return listing
        if not isinstance(html, str):
            return listing
        soup = BeautifulSoup(html, "html.parser")
        _apply_detail_enrichment(listing, soup)
        return listing

    # ---- internals ------------------------------------------------------

    def _iter_city(self, city: str) -> Iterable[Listing]:
        url = self._build_feed_url(city)
        resp = fetch(url, headers=KOMO_HEADERS, as_json=False)
        if not isinstance(resp, str) or not resp:
            log.warning("komo %s: empty/bad response", city)
            return

        total = _parse_total(resp)
        log.info("komo %s: %d listing(s) found", city, total or 0)

        soup = BeautifulSoup(resp, "html.parser")
        cards = soup.select("[class*=modaaRow]")
        if not cards:
            return

        seen_ids: set[str] = set()
        filter_stats: dict[str, int] = {}
        for card in cards:
            table = card.find("table")
            if not isinstance(table, Tag):
                continue
            listing = self._parse_card(table)
            if listing is None:
                filter_stats["parse_failed"] = filter_stats.get("parse_failed", 0) + 1
                continue
            if listing.source_id in seen_ids:
                filter_stats["duplicate"] = filter_stats.get("duplicate", 0) + 1
                continue
            seen_ids.add(listing.source_id)
            reason = self._passes_filters(listing)
            if reason:
                filter_stats[reason] = filter_stats.get(reason, 0) + 1
                continue
            yield listing

        if filter_stats:
            total_filtered = sum(filter_stats.values())
            summary = ", ".join(
                f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1])
            )
            log.info(
                "komo %s filter stats (%d filtered): %s", city, total_filtered, summary
            )

    def _build_feed_url(self, city: str) -> str:
        params = {
            "nehes": "5,2",
            "cityName": city,
            "fromRooms": int(self.search.get("rooms_min", 0)),
            "toPrice": int(self.search.get("price_max", 99999999)),
        }
        return f"{WEB_BASE}{FEED_PATH}?{urlencode(params)}"

    def _parse_card(self, table: Tag) -> Listing | None:
        source_id: str | None = None

        # Extract ID from table id="modaaRow<id>" or onclick handler
        id_match = re.search(r"modaaRow(\d+)", table.get("id", ""))
        if id_match:
            source_id = id_match.group(1)

        onclick = table.get("onclick", "")
        num_match = re.search(r'openModaaPop\(\s*"[^"]*"\s*,\s*"(\d+)"', onclick)
        if num_match and not source_id:
            source_id = num_match.group(1)
        if not source_id:
            return None

        # --- Address (city, neighborhood, street) ---
        address_text = ""
        title_span = table.select_one("span.LinkModaaTitle")
        if isinstance(title_span, Tag):
            address_text = title_span.get_text(" ", strip=True)

        # Extract bigtitle from the dvMActions div attribute
        bigtitle = ""
        bigtitle_div = table.select_one("div[id^='dvMActions']")
        if isinstance(bigtitle_div, Tag):
            bigtitle = bigtitle_div.get("bigtitle", "")
        if isinstance(bigtitle, bytes):
            bigtitle = bigtitle.decode("utf-8", errors="replace")
        elif not isinstance(bigtitle, str):
            bigtitle = ""
        bigtitle = html.unescape(bigtitle).replace("\xa0", " ").strip()

        city, neighborhood, street = _split_address(address_text)
        house_number = _trailing_number(street)

        # --- Price ---
        price: int | None = None
        price_el = table.select_one("td.tdPrice")
        if isinstance(price_el, Tag):
            price = _parse_price_html(price_el.get_text(" ", strip=True))
        if price is None:
            log.debug("komo parse_card failed: bad_price source_id=%s", source_id)
            return None

        # --- Rooms, sqm, floor from tdMoreDetails ---
        details_el = table.select_one("td.tdMoreDetails")
        details_text = ""
        if isinstance(details_el, Tag):
            details_text = details_el.get_text(" ", strip=True)

        rooms: float | None = None
        sqm: int | None = None
        floor: int | None = None
        listing_type: str = ""

        m = _MOREDETAILS_RE.search(details_text)
        if m:
            listing_type = m.group("prop_type").strip()
            try:
                rooms = float(m.group("rooms"))
            except (ValueError, TypeError):
                pass
            try:
                sqm = int(m.group("sqm"))
            except (ValueError, TypeError):
                pass
            floor_str = m.group("floor").strip()
            if floor_str.isdigit():
                floor = int(floor_str)
            elif floor_str in ("קרקע", "ground"):
                floor = 0

        # Fallback rooms/sqm from bigtitle
        if rooms is None:
            rm_match = re.search(r"([\d.]+)\s*חדרים?", bigtitle)
            if rm_match:
                try:
                    rooms = float(rm_match.group(1))
                except (ValueError, TypeError):
                    pass

        # --- Property type from bigtitle ---
        if not listing_type:
            # Extract from bigtitle: "<type> <rooms> חדרים <address>"
            type_match = re.match(r"^[ל]?\s*(.+?)\s*\d", bigtitle)
            if type_match:
                listing_type = type_match.group(1).strip()

        # --- Images ---
        images: list[str] = []
        img = table.select_one("td.tdGallery img")
        if isinstance(img, Tag):
            src = img.get("src") or ""
            if isinstance(src, str) and src:
                images.append(_absolutize(src))

        # --- URL ---
        url = f"{WEB_BASE}/code/nadlan/details/?modaaNum={source_id}"
        detail_a = table.select_one("a[href*='details']")
        if isinstance(detail_a, Tag):
            href = detail_a.get("href", "")
            if isinstance(href, str) and href:
                url = _absolutize(href)

        # --- is_agent ---
        # Card has class "Private" when it's a private seller
        card_div = table.find_parent("div")
        is_agent = True
        if isinstance(card_div, Tag):
            classes = " ".join(card_div.get("class", []) if isinstance(card_div.get("class"), list) else [])
            is_agent = "Private" not in classes

        # --- Full address ---
        address = ", ".join(
            x for x in [street, neighborhood, city] if x
        )

        log.debug(
            "komo parsed: source_id=%s price=%d rooms=%s sqm=%s city=%s",
            source_id, price, rooms, sqm, city,
        )

        return Listing(
            source="komo",
            source_id=source_id,
            url=url,
            city=city,
            neighborhood=neighborhood,
            street=street,
            house_number=house_number,
            address=address,
            rooms=rooms,
            sqm=sqm,
            floor=floor,
            price=price,
            price_per_sqm=round(price / sqm) if sqm else None,
            listing_type=listing_type or "apartment",
            is_agent=is_agent,
            images=images,
            tags=[],
            source_payload={"_city": city, "_bigtitle": bigtitle},
        )

    def _passes_filters(self, listing: Listing) -> str | None:
        s = self.search
        sid = listing.source_id

        if self._allowed_city_keys is not None and hebrew_city_match_key(listing.city) not in self._allowed_city_keys:
            log.debug("komo filtered: reason=city_not_allowed source_id=%s city=%s", sid, listing.city)
            return "city_not_allowed"

        if listing.price < s.get("price_min", 0) or listing.price > s.get("price_max", 10**12):
            log.debug("komo filtered: reason=price_out_of_range source_id=%s price=%d", sid, listing.price)
            return "price_out_of_range"

        if listing.rooms is not None:
            if not (s.get("rooms_min", 0) <= listing.rooms <= s.get("rooms_max", 99)):
                log.debug("komo filtered: reason=rooms_out_of_range source_id=%s rooms=%s", sid, listing.rooms)
                return "rooms_out_of_range"

        if s.get("min_sqm") and listing.sqm and listing.sqm < s["min_sqm"]:
            log.debug("komo filtered: reason=sqm_too_small source_id=%s sqm=%s", sid, listing.sqm)
            return "sqm_too_small"

        if s.get("exclude_ground_floor") and listing.floor == 0:
            log.debug("komo filtered: reason=ground_floor source_id=%s", sid)
            return "ground_floor"

        return None


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse_price_html(text: str) -> int | None:
    """'2,430,000&#8362;' → 2430000 or '2,430,000 ₪' → 2430000."""
    if not text:
        return None
    t = html.unescape(text).replace("\xa0", " ").replace("₪", "").strip()
    return _parse_price(t)


def _parse_price(s: str) -> int | None:
    """'2,430,000' → 2430000."""
    m = re.search(r"([\d,]+)\s*([kKmM]?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    n = int(num)
    return n if n > 0 else None


def _split_address(address: str) -> tuple[str, str, str]:
    """Split 'אריאל, רובע ד', הנגב 74' → (city, neighborhood, street).

    Expects format: "city[, neighborhood][, street [number]]".
    """
    if not address:
        return "", "", ""
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], "", ""


def _trailing_number(street: str) -> str:
    """'הנגב 74' → '74'."""
    m = re.search(r"(\d+[A-Za-z]?)\s*$", street or "")
    return m.group(1) if m else ""


def _absolutize(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return WEB_BASE + src
    return src


def _parse_total(html: str) -> int | None:
    """Extract 'נמצאו 5 מודעות' → 5."""
    m = re.search(r"נמצאו\s+(\d+)\s+מודעות", html)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _apply_detail_enrichment(listing: Listing, soup: BeautifulSoup) -> None:
    """Enrich listing from its detail page at /code/nadlan/details/?"""
    # Description
    description_text = soup.get_text(" ", strip=True)
    if description_text and not listing.description:
        listing.description = description_text[:2000]

    # More images from detail page
    for img in soup.select("img[src*='picNum']"):
        src = img.get("src") or ""
        if isinstance(src, str) and src:
            full = _absolutize(src)
            if full not in listing.images:
                listing.images.append(full)
