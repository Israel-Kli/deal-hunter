"""Reariel adapter — Webflow CMS site (reariel.co.il).

Single feed page (`/apartments-for-sale`) with ~110 listings, all server-rendered.
Cards use `.collection-item-3.w-dyn-item` with a hidden `.filter-data` block for
structured price + type + tags. Detail pages provide lot size via free-text description.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Listing
from deal_hunter.normalize.israeli_cities import hebrew_allowed_city_keys, hebrew_city_match_key

log = logging.getLogger(__name__)

WEB_BASE = "https://www.reariel.co.il"
FEED_URL = f"{WEB_BASE}/apartments-for-sale"

REARIEL_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}


class RearielAdapter:
    source = "reariel"
    enrich_always = True  # card blurbs are truncated; detail page has full description

    def __init__(
        self,
        search: dict[str, Any],
        *,
        allowed_cities: list[str] | None = None,
        request_delay_sec: float = 1.5,
    ):
        self.search = search
        self.request_delay = request_delay_sec
        if allowed_cities:
            self._allowed_city_keys = hebrew_allowed_city_keys(list(allowed_cities))
        else:
            self._allowed_city_keys = None

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        html = fetch(FEED_URL, as_json=False, headers=REARIEL_HEADERS)
        if not isinstance(html, str) or not html:
            log.warning("Reariel: empty/bad feed response")
            return
        soup = BeautifulSoup(html, "html.parser")
        catalog = soup.select_one("#Catalog .collection-list-wrapper-3")
        if not catalog:
            catalog = soup.select_one("#Catalog")
        if not catalog:
            log.warning("Reariel: #Catalog not found in feed page")
            return
        cards = catalog.select(".collection-item-3.w-dyn-item")
        if not cards:
            cards = catalog.select("div[role='listitem'].collection-item-3")

        filter_stats: dict[str, int] = {}
        for card in cards:
            listing, reason = self._parse_card(card)
            if reason:
                filter_stats[reason] = filter_stats.get(reason, 0) + 1
            if listing:
                yield listing

        if filter_stats:
            total = sum(filter_stats.values())
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("Reariel filter stats (%d filtered / %d cards): %s", total, len(cards), summary)

    def fetch_detail(self, listing: Listing) -> Listing:
        url = listing.url
        html = fetch(url, as_json=False, headers=REARIEL_HEADERS)
        if not isinstance(html, str) or not html:
            return listing
        soup = BeautifulSoup(html, "html.parser")

        # Description (free text with lot/land mentions)
        rich = soup.select_one(".rich-text-block-2")
        if rich:
            desc = rich.get_text(separator="\n", strip=True)
            if desc:
                listing.description = desc
                listing.lot_sqm = _extract_lot_sqm(desc)
                listing.garden_sqm = listing.garden_sqm or _extract_garden_sqm(desc)

        # Tags from detail page
        tags_wrapper = soup.select_one(".tags-wrapper")
        if tags_wrapper:
            visible_tags = [
                t.get_text(strip=True)
                for t in tags_wrapper.select(".greentags")
                if "w-condition-invisible" not in (t.get("class") or [])
            ]
            for tag in visible_tags:
                if tag not in listing.tags:
                    listing.tags.append(tag)

        # Amenities from tags
        tl = " ".join(listing.tags)
        listing.parking = listing.parking or "חנייה" in tl or "חניה" in tl
        listing.elevator = listing.elevator or "מעלית" in tl
        listing.balcony = listing.balcony or "מרפסת" in tl
        listing.ac = listing.ac or "מיזוג" in tl or "מזגן" in tl
        listing.mamad = listing.mamad or "ממ" in tl and "ד" in tl
        listing.renovated = listing.renovated or "משופץ" in tl or "שיפוץ" in tl

        # Floor from detail (if available)
        floor_el = soup.select_one(".floor-str") or soup.select_one(".floor-number")
        if floor_el:
            txt = floor_el.get_text(strip=True)
            listing.floor = _first_int(txt)

        # Additional images from gallery
        gallery_imgs = soup.select(".collection-small-imegs-item-5 a[style*='background-image']")
        for a in gallery_imgs:
            style = a.get("style", "")
            m = re.search(r"url\(\"?([^\"')]+)\"?\)", style)
            if m:
                img_url = m.group(1)
                if img_url not in listing.images:
                    listing.images.append(img_url)

        # Garden sqm from description
        if listing.garden_sqm is None and listing.description:
            listing.garden_sqm = _extract_garden_sqm(listing.description)

        return listing

    def _parse_card(self, card: Tag) -> tuple[Listing | None, str | None]:
        # ---- hidden filter-data block (most reliable structured data) ----
        filter_data = card.select_one(".filter-data")
        if not filter_data:
            return None, "no_filter_data"

        raw_price = _text(filter_data.select_one(".div-price div"))
        if not raw_price:
            return None, "no_price"
        try:
            price = int(re.sub(r"[^\d]", "", raw_price))
        except ValueError:
            log.debug("Reariel filtered: reason=bad_price raw=%s", raw_price)
            return None, "bad_price"

        s = self.search
        if s.get("price_min") and price < s["price_min"]:
            log.debug("Reariel filtered: reason=price_out_of_range price=%d min=%s", price, s["price_min"])
            return None, "price_out_of_range"
        if s.get("price_max") and price > s["price_max"]:
            log.debug("Reariel filtered: reason=price_out_of_range price=%d max=%s", price, s["price_max"])
            return None, "price_out_of_range"

        # Property type
        asset_type = _text(filter_data.select_one(".asset-type .apartment-type")) or ""

        # Tags from filter-data
        tags: list[str] = []
        for div in filter_data.select(".div-tag div:not(.w-condition-invisible):not(.apartment-type)"):
            txt = div.get_text(strip=True)
            if txt:
                tags.append(txt)

        # ---- visible card data ----
        href = ""
        link_el = card.select_one("a.hot-appartment-cards[href]")
        if link_el:
            href = link_el.get("href", "")
        url = urljoin(WEB_BASE, href) if href else ""

        # Source ID from URL slug
        m = re.search(r"/for-sale/([^/?]+)", href)
        source_id = m.group(1) if m else ""

        # Image
        images: list[str] = []
        photo_div = card.select_one(".apartment-card-photo")
        if photo_div:
            style = photo_div.get("style", "")
            m = re.search(r"url\(\"?([^\"')]+)\"?\)", style)
            if m:
                images.append(m.group(1))

        # Address
        address = _text(card.select_one("h4.address")) or ""

        # Rooms / sqm from cards-white-part — walk paired (value, label) by DOM order
        rooms_f: float | None = None
        sqm_i: int | None = None
        info_nums = card.select(".details-wrapper .info-numbers-2")
        details_labels = card.select(".details-wrapper .apartment-details2, .details-wrapper .apartment-details-2")
        for i, num_el in enumerate(info_nums):
            label_text = _text(details_labels[i]) if i < len(details_labels) else ""
            if "חדר" in label_text:
                if rooms_f is None:
                    rooms_f = _first_float(num_el.get_text(strip=True))
            elif _is_sqm_label(label_text):
                if sqm_i is None:
                    sqm_i = _first_int(num_el.get_text(strip=True))
            else:
                # Fallback: assume first numeric is rooms, second is sqm
                if i == 0 and rooms_f is None:
                    rooms_f = _first_float(num_el.get_text(strip=True))
                elif i == 1 and sqm_i is None:
                    sqm_i = _first_int(num_el.get_text(strip=True))

        if s.get("rooms_min") and rooms_f and rooms_f < s["rooms_min"]:
            log.debug("Reariel filtered: reason=rooms_out_of_range rooms=%s min=%s", rooms_f, s["rooms_min"])
            return None, "rooms_out_of_range"
        if s.get("rooms_max") and rooms_f and rooms_f > s["rooms_max"]:
            log.debug("Reariel filtered: reason=rooms_out_of_range rooms=%s max=%s", rooms_f, s["rooms_max"])
            return None, "rooms_out_of_range"
        if s.get("min_sqm") and sqm_i and sqm_i < s["min_sqm"]:
            log.debug("Reariel filtered: reason=sqm_too_small sqm=%s min=%s", sqm_i, s["min_sqm"])
            return None, "sqm_too_small"

        # Description blurb
        description = _text(card.select_one("p.apartment-text")) or ""

        # Lot sqm from description (regex)
        lot_sqm = _extract_lot_sqm(description)
        garden_sqm = _extract_garden_sqm(description)

        # Parse address parts
        street, house_number, neighborhood, city = _parse_address(address)

        # City filter
        if self._allowed_city_keys is not None and hebrew_city_match_key(city) not in self._allowed_city_keys:
            log.debug("Reariel filtered: reason=city_not_allowed source_id=%s city=%s", source_id, city)
            return None, "city_not_allowed"

        # Amenities from tags
        tl = " ".join(tags)
        parking = "חנייה" in tl or "חניה" in tl
        balcony = "מרפסת" in tl
        mamad = "ממ" in tl and "ד" in tl
        renovated = "משופץ" in tl

        log.debug("Reariel parsed: source_id=%s price=%d rooms=%s sqm=%s city=%s type=%s", source_id, price, rooms_f, sqm_i, city, asset_type)

        return Listing(
            source="reariel",
            source_id=source_id,
            url=url,
            city=city,
            neighborhood=neighborhood,
            street=street,
            house_number=house_number,
            address=address,
            rooms=rooms_f,
            sqm=sqm_i,
            sqm_build=sqm_i,
            price=price,
            price_per_sqm=round(price / sqm_i) if sqm_i else None,
            listing_type=asset_type,
            is_agent=True,
            parking=parking,
            balcony=balcony,
            mamad=mamad,
            renovated=renovated,
            description=description,
            images=images,
            tags=tags,
            lot_sqm=lot_sqm,
            garden_sqm=garden_sqm,
            source_payload={"_asset_type": asset_type},
        ), None


# ── helpers ──────────────────────────────────────────────────────────────────


def _text(el: Tag | None) -> str:
    return el.get_text(strip=True) if el else ""


def _first_int(s: str) -> int | None:
    m = re.search(r"(\d+)", s.replace(",", ""))
    return int(m.group(1)) if m else None


def _first_float(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


_SQM_LABEL_INDICATORS = ("מ״ר", 'מ"ר', "מטר", "מ'", "מ׳ר", "שטח")


def _is_sqm_label(text: str) -> bool:
    """Check whether a detail label refers to square-meter area."""
    if not text:
        return False
    return any(ind in text for ind in _SQM_LABEL_INDICATORS)


def _parse_address(raw: str) -> tuple[str, str, str, str]:
    """Parse 'דרך הציונות 4, אריאל' into (street, house#, neighborhood, city)."""
    street = ""
    house_number = ""
    neighborhood = ""
    city = "אריאל"

    if not raw:
        return street, house_number, neighborhood, city

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
    r"(?:מגרש|קרקע|המגרש|פינת[יי])"
    r"(?:"
    r"[^.\n]{0,80}?(\d{3,5})\s*(?:מ[״\"'׳]?ר|מטר)"
    r"|"
    r"\s*מ[״\"'׳]?ר\s*(\d{3,5})"
    r"|"
    r"[^.\n]{0,40}?(\d{3,5})(?:[\s,.]|$)"  # "מגרש 600" without unit
    r")",
)
_LOT_DUNAM_RE = re.compile(
    r"(?:דונם|חצי\s+דונם)[^.]{0,40}?(\d{3,5})\s*(?:מ[״\"'׳]?ר|מטר)",
)
_GARDEN_RE = re.compile(
    r"(?:גינ[הת]|חצר)[^.\n]{0,40}?(\d{2,4})\s*מ[״\"'׳]?ר",
)


def _extract_lot_sqm(text: str) -> int | None:
    m = _LOT_RE.search(text)
    if m:
        val = m.group(1) or m.group(2) or m.group(3)
        if val:
            return int(val)
    m = _LOT_DUNAM_RE.search(text)
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
