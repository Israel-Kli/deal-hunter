"""Nadlanh adapter — WordPress Elementor + ele-custom-skin (nadlanh.co.il).

Feed page `/למכירה-באריאל/?type=בית-פרטי` with AJAX pagination by ECS plugin.
Cards are `article.elementor-post.ecs-post-loop` using icon-based field selectors
(blueprint icon → area, plan icon → rooms, etc.). Detail pages provide full description
and amenities.

NOTE: nadlanh.co.il returns 403 to curl_cffi (Cloudflare anti-bot). We use stdlib
urllib with a plain browser User-Agent which passes through.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from io import BytesIO
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Tag

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.models import Listing

log = logging.getLogger(__name__)

WEB_BASE = "https://nadlanh.co.il"

NADLANH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8",
}


def _fetch_urllib(url: str, timeout: int = 30) -> str | None:
    """GET with stdlib urllib (avoids curl_cffi TLS fingerprint that triggers Cloudflare 403)."""
    req = Request(url, headers=NADLANH_HEADERS)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("nadlanh fetch %s: %s", url, e)
        return None


class NadlanhAdapter:
    source = "nadlanh"

    def __init__(
        self,
        search: dict[str, Any],
        *,
        property_types: list[str] | None = None,
        max_pages: int = 3,
        request_delay_sec: float = 2.0,
    ):
        self.search = search
        self.property_types = property_types or ["בית פרטי"]
        self.max_pages = max_pages
        self.request_delay = request_delay_sec

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        for ptype in self.property_types:
            yield from self._iter_type(ptype)

    def fetch_detail(self, listing: Listing) -> Listing:
        html = _fetch_urllib(listing.url)
        if not html:
            return listing
        soup = BeautifulSoup(html, "html.parser")

        # ── Description ──
        desc_selectors = [
            "div[data-id^='9ce8f'] .elementor-widget-container",
            "[data-id='9ce8f11'] .elementor-widget-container",
            ".elementor-widget-text-editor .elementor-widget-container",
        ]
        for sel in desc_selectors:
            for el in soup.select(sel):
                txt = el.get_text(strip=True)
                if txt and len(txt) > 20 and "403" not in txt:
                    listing.description = txt
                    break
            if listing.description:
                break

        # ── Amenities from check-detail section ──
        amenity_section = soup.select_one("section[data-id='73430878']")
        if amenity_section:
            amenity_texts = []
            for h5 in amenity_section.select("h5.elementor-heading-title"):
                amenity_texts.append(h5.get_text(strip=True))
            ft = " ".join(amenity_texts)
            listing.parking = listing.parking or "חניה" in ft or "חנייה" in ft
            listing.ac = listing.ac or "מיזוג" in ft or "מזגן" in ft or "ממוזג" in ft
            listing.elevator = listing.elevator or "מעלית" in ft
            listing.balcony = listing.balcony or "מרפסת" in ft
            listing.mamad = listing.mamad or "ממד" in ft or "ממ" in ft and "ד" in ft
            listing.renovated = listing.renovated or "משופץ" in ft or "שיפוץ" in ft
            for t in amenity_texts:
                if t and t not in listing.tags:
                    listing.tags.append(t)

        # ── Gallery images ──
        gallery_items = soup.select("ul.easy-image-gallery li a[data-fancybox]")
        for a in gallery_items:
            href = a.get("href", "")
            if href and href not in listing.images:
                listing.images.append(href)

        # Additional images from swiper/slider
        swiper_imgs = soup.select(".swiper-slide img[src]")
        for img in swiper_imgs:
            src = img.get("src", "")
            if src and not src.endswith(".svg") and src not in listing.images:
                listing.images.append(src)

        # ── Lot sqm from description ──
        if listing.description:
            listing.lot_sqm = listing.lot_sqm or _extract_lot_sqm(listing.description)
            listing.garden_sqm = listing.garden_sqm or _extract_garden_sqm(listing.description)

        time.sleep(self.request_delay)
        return listing

    def _iter_type(self, ptype: str) -> Iterable[Listing]:
        filter_stats: dict[str, int] = {}
        for page in range(1, self.max_pages + 1):
            params = {"type": ptype}
            if page > 1:
                params["paged"] = str(page)
            qs = urlencode(params)
            url = f"{WEB_BASE}/%D7%9C%D7%9E%D7%9B%D7%99%D7%A8%D7%94-%D7%91%D7%90%D7%A8%D7%99%D7%90%D7%9C/?{qs}"
            html = _fetch_urllib(url)
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")

            # Check for ECS posts container
            containers = soup.select("div.ecs-posts.elementor-posts-container")
            cards: list[Tag] = []
            seen_ids: set[int] = set()
            for container in containers:
                for article in container.select("article.elementor-post.ecs-post-loop[class*='post-']"):
                    post_id = _extract_post_id(article)
                    if post_id and post_id not in seen_ids:
                        seen_ids.add(post_id)
                        cards.append(article)

            if not cards:
                # Fallback: all articles on page
                for article in soup.select("article.elementor-post.ecs-post-loop[class*='post-']"):
                    post_id = _extract_post_id(article)
                    if post_id and post_id not in seen_ids:
                        seen_ids.add(post_id)
                        cards.append(article)

            if not cards:
                break

            log.info("Nadlanh page %d (%s): %d items", page, ptype, len(cards))
            for article in cards:
                listing, reason = self._parse_card(article)
                if reason:
                    filter_stats[reason] = filter_stats.get(reason, 0) + 1
                if listing:
                    yield listing

            if page > 1:
                time.sleep(self.request_delay)

        if filter_stats:
            total = sum(filter_stats.values())
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("Nadlanh filter stats (%s, %d filtered): %s", ptype, total, summary)

    def _parse_card(self, article: Tag) -> tuple[Listing | None, str | None]:
        post_id = _extract_post_id(article)
        if not post_id:
            log.debug("Nadlanh filtered: reason=no_post_id article_classes=%s", article.get("class", []))
            return None, "no_post_id"

        # ---- Price ----
        price_el = _find_element_by_text(article, ["h3", "h5"], re.compile(r"(מחיר|שיווק).*?([\d,]+)"))
        raw_price = price_el.get_text(strip=True) if price_el else ""
        if not raw_price:
            log.debug("Nadlanh filtered: reason=no_price post_id=%s", post_id)
            return None, "no_price"
        try:
            price = int(re.sub(r"[^\d]", "", raw_price))
        except ValueError:
            log.debug("Nadlanh filtered: reason=bad_price post_id=%s raw=%s", post_id, raw_price)
            return None, "bad_price"

        s = self.search
        if s.get("price_min") and price < s["price_min"]:
            log.debug("Nadlanh filtered: reason=price_out_of_range post_id=%s price=%d min=%s", post_id, price, s["price_min"])
            return None, "price_out_of_range"
        if s.get("price_max") and price > s["price_max"]:
            log.debug("Nadlanh filtered: reason=price_out_of_range post_id=%s price=%d max=%s", post_id, price, s["price_max"])
            return None, "price_out_of_range"

        # ---- Rooms (plan icon) ----
        rooms_el = _find_icon_element(article, "032-plan.png")
        rooms_f: float | None = None
        if rooms_el:
            rooms_f = _first_float(rooms_el.get_text(strip=True))

        if s.get("rooms_min") and rooms_f and rooms_f < s["rooms_min"]:
            log.debug("Nadlanh filtered: reason=rooms_out_of_range post_id=%s rooms=%s min=%s", post_id, rooms_f, s["rooms_min"])
            return None, "rooms_out_of_range"
        if s.get("rooms_max") and rooms_f and rooms_f > s["rooms_max"]:
            log.debug("Nadlanh filtered: reason=rooms_out_of_range post_id=%s rooms=%s max=%s", post_id, rooms_f, s["rooms_max"])
            return None, "rooms_out_of_range"

        # ---- Built sqm (blueprint icon) ----
        area_el = _find_icon_element(article, "043-blueprint.png")
        sqm_i: int | None = None
        if area_el:
            sqm_i = _first_int(area_el.get_text(strip=True))

        if s.get("min_sqm") and sqm_i and sqm_i < s["min_sqm"]:
            log.debug("Nadlanh filtered: reason=sqm_too_small post_id=%s sqm=%s min=%s", post_id, sqm_i, s["min_sqm"])
            return None, "sqm_too_small"

        # ---- Floor (elevator icon) ----
        floor_el = _find_icon_element(article, "022-elevator.png")
        floor_i: int | None = None
        if floor_el:
            floor_txt = floor_el.get_text(strip=True)
            if "קרקע" in floor_txt:
                floor_i = 0
            else:
                floor_i = _first_int(floor_txt)

        # ---- Address (map-marker icon) ----
        addr_el = _find_icon_element(article, "fa-map-marker-alt", tag="i")
        address = ""
        if addr_el:
            parent = addr_el.parent
            if parent:
                address = parent.get_text(strip=True).lstrip("").strip()

        # ---- Detail URL ----
        url = ""
        permalink = article.select_one("a[href*='nadlanh.co.il/20']")
        if permalink:
            href = permalink.get("href", "")
            url = href if href.startswith("http") else urljoin(WEB_BASE, href)

        # ---- Image ----
        images: list[str] = []
        img_el = article.select_one("img.wp-post-image, img[src*='wp-content/uploads']")
        if img_el:
            src = img_el.get("src", "")
            if src:
                images.append(src)

        # ---- Tags (categories) ----
        tags: list[str] = []
        cat_els = article.select("div[data-id='50749ea'] h3 a, div[data-id='bbbb907'] h3 a, div[data-id='c648453'] h3 a, div[data-id='7d7d96b'] h3 a")
        for a in cat_els:
            txt = a.get_text(strip=True)
            if txt:
                tags.append(txt)

        # ---- Address parts ----
        street, house_number, neighborhood, city = _parse_address(address)

        log.debug("Nadlanh parsed: post_id=%s price=%d rooms=%s sqm=%s city=%s", post_id, price, rooms_f, sqm_i, city)

        return Listing(
            source="nadlanh",
            source_id=str(post_id),
            url=url,
            city=city,
            neighborhood=neighborhood,
            street=street,
            house_number=house_number,
            address=address,
            rooms=rooms_f,
            sqm=sqm_i,
            floor=floor_i,
            price=price,
            price_per_sqm=round(price / sqm_i) if sqm_i else None,
            listing_type="",
            is_agent=True,
            images=images,
            tags=tags,
            source_payload={"_post_id": post_id},
        ), None


# ── helpers ──────────────────────────────────────────────────────────────────


def _extract_post_id(article: Tag) -> int | None:
    classes = article.get("class", [])
    for cls in classes:
        if cls.startswith("post-"):
            try:
                return int(cls.split("-")[1])
            except (ValueError, IndexError):
                pass
    el_id = article.get("id", "")
    if el_id.startswith("post-"):
        try:
            return int(el_id.split("-")[1])
        except (ValueError, IndexError):
            pass
    return None


def _find_icon_element(parent: Tag, img_pattern: str, *, tag: str = "img") -> Tag | None:
    """Find a heading element whose child img src contains `img_pattern`."""
    if tag == "img":
        img = parent.find("img", src=re.compile(img_pattern))
    else:
        img = parent.find(tag, class_=re.compile(img_pattern))
    if img:
        h = img.find_parent(["h3", "h4", "h5"])
        if h:
            return h
    return None


def _find_element_by_text(parent: Tag, tags: list[str], pattern: re.Pattern) -> Tag | None:
    for t in tags:
        for el in parent.find_all(t):
            txt = el.get_text(strip=True)
            if pattern.search(txt):
                return el
    return None


def _first_int(s: str) -> int | None:
    m = re.search(r"(\d+)", s.replace(",", ""))
    return int(m.group(1)) if m else None


def _first_float(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _parse_address(raw: str) -> tuple[str, str, str, str]:
    street = ""
    house_number = ""
    neighborhood = ""
    city = "אריאל"

    if not raw:
        return street, house_number, neighborhood, city

    raw = raw.strip().rstrip(",").rstrip(".").strip()
    # Normalize HTML entities in street names
    raw = raw.replace("&#8221;", '"').replace("&#8220;", '"').replace("&#8243;", '"')
    raw = raw.replace("&#34;", '"').replace("&quot;", '"')

    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 1:
        street_part = parts[0]
        m = re.match(r"(.+?)\s+(\d+[א-ת]?)$", street_part)
        if m:
            street = m.group(1).strip()
            house_number = m.group(2)
        else:
            street = street_part
    if len(parts) >= 2:
        city = parts[-1].strip()
    if len(parts) >= 3:
        neighborhood = parts[-2].strip()

    return street, house_number, neighborhood, city


_LOT_RE = re.compile(
    r"(?:מגרש|קרקע|המגרש)[^.\n]{0,80}?(\d{3,5})\s*(?:מ[״\"']?ר|מטר)",
)
_GARDEN_RE = re.compile(
    r"(?:גינ[הת]|חצר)[^.\n]{0,40}?(\d{2,4})\s*מ[״\"']?ר",
)


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
