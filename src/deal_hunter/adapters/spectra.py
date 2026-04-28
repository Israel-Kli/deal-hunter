"""Spectra adapter — WordPress Houzez theme (spectra-nadlan.co.il).

Feed page `/קטלוג-הנכסים/בתים-למכירה-באריאל/` with Houzez v2 item cards
(`div[data-hz-id]`). Cards expose `data-images` JSON and `ul.item-amenities`
structured rows. Detail pages provide lot sqm via `#property-detail-wrap`
labeled rows + JSON-LD.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Listing

log = logging.getLogger(__name__)

WEB_BASE = "https://www.spectra-nadlan.co.il"
FEED_URL = f"{WEB_BASE}/%D7%A7%D7%98%D7%9C%D7%95%D7%92-%D7%94%D7%A0%D7%9B%D7%A1%D7%99%D7%9D/%D7%91%D7%AA%D7%99%D7%9D-%D7%9C%D7%9E%D7%9B%D7%99%D7%A8%D7%94-%D7%91%D7%90%D7%A8%D7%99%D7%90%D7%9C/"

SPECTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}


class SpectraAdapter:
    source = "spectra"
    enrich_always = True  # detail pages carry description, lot_sqm, garden_sqm, amenity text

    def __init__(
        self,
        search: dict[str, Any],
        *,
        request_delay_sec: float = 1.5,
    ):
        self.search = search
        self.request_delay = request_delay_sec

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        html = fetch(FEED_URL, as_json=False, headers=SPECTRA_HEADERS)
        if not isinstance(html, str) or not html:
            log.warning("Spectra: empty/bad feed response")
            return
        soup = BeautifulSoup(html, "html.parser")

        # Try module_properties listing-view container first
        container = soup.select_one("#module_properties.listing-view.grid-view")
        if container:
            cards = container.select("div[data-hz-id]")
        else:
            cards = soup.select("div[data-hz-id]")

        if not cards:
            log.warning("Spectra: no cards found (div[data-hz-id])")
            return

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
            log.info("Spectra filter stats (%d filtered / %d cards): %s", total, len(cards), summary)

    def fetch_detail(self, listing: Listing) -> Listing:
        html = fetch(listing.url, as_json=False, headers=SPECTRA_HEADERS)
        if not isinstance(html, str) or not html:
            return listing
        soup = BeautifulSoup(html, "html.parser")

        # ── JSON-LD structured data ──
        jsonld = _extract_jsonld(soup)
        if jsonld:
            if jsonld.get("description"):
                listing.description = jsonld["description"]
            if isinstance(jsonld.get("offers"), dict):
                offer = jsonld["offers"]
                if offer.get("price"):
                    listing.price = listing.price or int(float(offer["price"]))
            if isinstance(jsonld.get("floorSize"), dict):
                fs = jsonld["floorSize"]
                if fs.get("value"):
                    listing.sqm = int(fs["value"])
            if jsonld.get("numberOfRooms"):
                listing.rooms = listing.rooms or float(jsonld["numberOfRooms"])
            if jsonld.get("numberOfBedrooms"):
                listing.rooms = listing.rooms or float(jsonld["numberOfBedrooms"])
            if isinstance(jsonld.get("image"), list):
                for img in jsonld["image"]:
                    if isinstance(img, str) and img not in listing.images:
                        listing.images.append(img)
            elif isinstance(jsonld.get("image"), str):
                if jsonld["image"] not in listing.images:
                    listing.images.append(jsonld["image"])

        # ── Property detail wrap (labeled rows) ──
        detail_wrap = soup.select_one("#property-detail-wrap")
        if detail_wrap:
            rows = detail_wrap.select("li strong")
            for strong in rows:
                label = strong.get_text(strip=True)
                value_el = strong.find_next_sibling("span")
                if value_el is None:
                    parent = strong.parent
                    value_el = parent.find("span") if parent else None
                value = value_el.get_text(strip=True) if value_el else ""
                if "מגרש" in label:
                    listing.lot_sqm = listing.lot_sqm or _first_int(value)
                elif "שטח בנוי" in label:
                    listing.sqm = _first_int(value) or listing.sqm
                    listing.sqm_build = _first_int(value) or listing.sqm_build
                elif "חצר" in label or "מרפס" in label:
                    garden_val = _first_int(value)
                    if garden_val is not None and garden_val > 0:
                        listing.garden_sqm = garden_val
                    listing.balcony = True
                elif "יחידות" in label:
                    units_val = _first_int(value)
                    if units_val is not None and units_val > 0:
                        listing.units_count = units_val

        # ── Amenities from features wrap ──
        features = soup.select_one("#property-features-wrap")
        if features:
            feature_links = features.select(".block-content-wrap ul li a")
            ft = " ".join(a.get_text(strip=True) for a in feature_links)
            listing.parking = listing.parking or "חניה" in ft or "חנייה" in ft
            listing.ac = listing.ac or "מיזוג" in ft or "מזגן" in ft
            listing.elevator = listing.elevator or "מעלית" in ft
            listing.balcony = listing.balcony or "מרפסת" in ft
            listing.mamad = listing.mamad or "ממד" in ft or "ממ״ד" in ft or "ממ\"ד" in ft
            listing.renovated = listing.renovated or "משופץ" in ft or "שיפוץ" in ft
            for a in feature_links:
                txt = a.get_text(strip=True)
                if txt and txt not in listing.tags:
                    listing.tags.append(txt)

        # ── Gallery images ──
        gallery_links = soup.select(".hs-gallery-v4-grid a[data-src]")
        for a in gallery_links:
            src = a.get("data-src", "")
            if src and src not in listing.images:
                listing.images.append(src)

        # ── Description text (if not already from JSON-LD) ──
        if not listing.description:
            for sel in (
                "#property-description-wrap .block-content-wrap p",
                "#property-description-wrap p",
                "#property-description-wrap .block-content-wrap",
                "#property-description-wrap",
                ".property-description p",
                ".block-content-wrap p",
            ):
                desc_el = soup.select_one(sel)
                if desc_el:
                    listing.description = desc_el.get_text(strip=True)
                    break

        # Garden sqm from description
        if listing.garden_sqm is None and listing.description:
            listing.garden_sqm = _extract_garden_sqm(listing.description)

        time.sleep(self.request_delay)
        return listing

    def _parse_card(self, card: Tag) -> tuple[Listing | None, str | None]:
        hz_id = card.get("data-hz-id", "")
        if not hz_id:
            return None, "no_hz_id"

        # ---- Price ----
        price_el = card.select_one("ul.item-price-wrap li.item-price span.price")
        raw_price = price_el.get_text(strip=True) if price_el else ""
        if not raw_price:
            log.debug("Spectra filtered: reason=no_price hz_id=%s", hz_id)
            return None, "no_price"
        try:
            price = int(re.sub(r"[^\d]", "", raw_price))
        except ValueError:
            log.debug("Spectra filtered: reason=bad_price hz_id=%s raw=%s", hz_id, raw_price)
            return None, "bad_price"

        s = self.search
        if s.get("price_min") and price < s["price_min"]:
            log.debug("Spectra filtered: reason=price_out_of_range hz_id=%s price=%d min=%s", hz_id, price, s["price_min"])
            return None, "price_out_of_range"
        if s.get("price_max") and price > s["price_max"]:
            log.debug("Spectra filtered: reason=price_out_of_range hz_id=%s price=%d max=%s", hz_id, price, s["price_max"])
            return None, "price_out_of_range"

        # ---- Type ----
        type_el = card.select_one("ul.item-amenities li.h-type span")
        listing_type = ""
        if type_el:
            listing_type = " ".join(
                t.strip() for t in type_el.contents
                if isinstance(t, str) and t.strip()
            )

        # ---- Rooms ----
        rooms_el = card.select_one("ul.item-amenities li.h-rooms span")
        rooms_f: float | None = None
        if rooms_el:
            rooms_f = _first_float(rooms_el.get_text()) if rooms_el.get_text() else None

        if s.get("rooms_min") and rooms_f and rooms_f < s["rooms_min"]:
            log.debug("Spectra filtered: reason=rooms_out_of_range hz_id=%s rooms=%s min=%s", hz_id, rooms_f, s["rooms_min"])
            return None, "rooms_out_of_range"
        if s.get("rooms_max") and rooms_f and rooms_f > s["rooms_max"]:
            log.debug("Spectra filtered: reason=rooms_out_of_range hz_id=%s rooms=%s max=%s", hz_id, rooms_f, s["rooms_max"])
            return None, "rooms_out_of_range"

        # ---- Built sqm ----
        area_el = card.select_one("ul.item-amenities li.h-area span")
        sqm_i: int | None = None
        if area_el:
            sqm_i = _first_int(area_el.get_text())

        if s.get("min_sqm") and sqm_i and sqm_i < s["min_sqm"]:
            log.debug("Spectra filtered: reason=sqm_too_small hz_id=%s sqm=%s min=%s", hz_id, sqm_i, s["min_sqm"])
            return None, "sqm_too_small"

        # ---- Floor ----
        floor_el = card.select_one("ul.item-amenities li[class^='h-f'] span")
        floor_i: int | None = None
        if floor_el:
            floor_txt = floor_el.get_text(strip=True)
            if floor_txt:
                if "קרקע" in floor_txt:
                    floor_i = 0
                else:
                    floor_i = _first_int(floor_txt)

        # ---- Address ----
        addr_el = card.select_one("address.item-address span")
        address = addr_el.get_text(strip=True) if addr_el else ""

        # ---- Detail URL ----
        link_el = card.select_one("div.listing-thumb a.listing-featured-thumb[href]")
        url = ""
        if link_el:
            href = link_el.get("href", "")
            url = href if href.startswith("http") else urljoin(WEB_BASE, href)

        # ---- Images from data-images ----
        images: list[str] = []
        raw_images = card.get("data-images", "")
        if raw_images:
            try:
                decoded = raw_images.replace("&quot;", '"')
                img_list = json.loads(decoded)
                for img in img_list:
                    if isinstance(img, dict) and img.get("image"):
                        images.append(img["image"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Fallback: cover image
        if not images:
            thumb_img = card.select_one("div.listing-thumb a img.wp-post-image")
            if thumb_img:
                src = thumb_img.get("src", "")
                if src:
                    images.append(src)

        # ---- Parking ----
        cars_el = card.select_one("ul.item-amenities li.h-cars span")
        parking = cars_el is not None

        # ---- Tags ----
        tags: list[str] = []
        labels = card.select(".labels-wrap a.label-status, .labels-wrap a.hz-label")
        for lbl in labels:
            txt = lbl.get_text(strip=True)
            if txt:
                tags.append(txt)

        # ---- Address parts ----
        street, house_number, neighborhood, city = _parse_address(address)

        # Spectra catalogs show cross-city listings (e.g. Barkan on Ariel page).
        # Only keep listings from the intended city.
        if city != "אריאל":
            log.debug("Spectra filtered: reason=wrong_city hz_id=%s city=%s", hz_id, city)
            return None, "wrong_city"

        log.debug("Spectra parsed: hz_id=%s price=%d rooms=%s sqm=%s city=%s type=%s", hz_id, price, rooms_f, sqm_i, city, listing_type)

        return Listing(
            source="spectra",
            source_id=hz_id,
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
            listing_type=listing_type,
            is_agent=True,
            parking=parking,
            images=images,
            tags=tags,
            source_payload={"_hz_id": hz_id},
        ), None


# ── helpers ──────────────────────────────────────────────────────────────────


def _first_int(s: str) -> int | None:
    m = re.search(r"(\d+)", s.replace(",", ""))
    return int(m.group(1)) if m else None


def _first_float(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _extract_jsonld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Place":
            return data
        if isinstance(data, dict) and "@graph" in data:
            for item in data["@graph"]:
                if item.get("@type") == "Place":
                    return item
    return None


def _parse_address(raw: str) -> tuple[str, str, str, str]:
    street = ""
    house_number = ""
    neighborhood = ""
    city = "אריאל"

    if not raw:
        return street, house_number, neighborhood, city

    # Strip "רחוב " prefix
    addr = re.sub(r"^רחוב\s+", "", raw.strip())

    # Try: "Street Number, City" or "City, Street Number" (comma-separated)
    parts = [p.strip() for p in addr.split(",")]
    if len(parts) >= 2:
        found_street_part = ""
        for part in parts[:]:
            m = re.match(r"(.+?)\s+(\d+[א-ת]?)$", part)
            if m:
                street = m.group(1).strip()
                house_number = m.group(2)
                found_street_part = part
                break
        # City is the other part (whichever one doesn't contain the number)
        remaining = [p for p in parts if p != found_street_part]
        city = remaining[-1].strip() if remaining else parts[-1].strip()
        if len(parts) >= 3:
            neighborhood = parts[1].strip() if len(parts) == 3 else ""
        if not street and len(parts) >= 1:
            street = parts[0].strip()
        return street, house_number, neighborhood, city

    # Try: "City Street Number" (no commas) — e.g. "אריאל שנהב 7"
    for city_prefix in ("אריאל", "ברקן", "נופים"):
        if addr.startswith(city_prefix + " "):
            city = city_prefix
            rest = addr[len(city_prefix):].strip()
            m = re.match(r"(.+?)\s+(\d+[א-ת]?)$", rest)
            if m:
                street = m.group(1).strip()
                house_number = m.group(2)
            else:
                street = rest
            return street, house_number, neighborhood, city

    # Simple: "Street Number"
    m = re.match(r"(.+?)\s+(\d+[א-ת]?)$", addr)
    if m:
        street = m.group(1).strip()
        house_number = m.group(2)
    else:
        street = addr

    return street, house_number, neighborhood, city


_GARDEN_RE = re.compile(
    r"(?:גינ[הת]|חצר)[^.\n]{0,40}?(\d{2,4})\s*מ[״\"']?ר",
)


def _extract_garden_sqm(text: str) -> int | None:
    m = _GARDEN_RE.search(text)
    if m:
        val = int(m.group(1))
        if 5 <= val <= 9999:
            return val
    return None
