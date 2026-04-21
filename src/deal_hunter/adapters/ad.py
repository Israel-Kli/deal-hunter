"""ad.co.il for-sale adapter.

ad.co.il is a classic server-rendered site — no internal JSON API, no bot
protection observed. Each listing is a ``div.card.overflow-hidden`` on the
``/nadlansale`` feed with these reliable child bits:

* ``a[href^="/ad/<id>"]`` → listing id + title (city + neighborhood in Hebrew)
* ``p.card-text``                           → street + house number
* ``i.fa-bed``                              → rooms (Hebrew "X חד'")
* ``i.fa-expand-arrows-alt``                → square meters ("X מ"ר")
* ``div.price``                             → sale price "X,XXX,XXX ₪"
* ``picture.card-img-top img``              → thumbnail URL (protocol-relative)
* text "מקודם" inside card                   → sponsored/promoted badge

Feed endpoint:
    GET https://www.ad.co.il/nadlansale           (full board)
    GET https://www.ad.co.il/nadlansale?pageindex=N
    GET https://www.ad.co.il/city/<slug>          (per-city view)

Pagination is server-side via the ``pageindex`` query param; the footer shows
the last page number ("עמוד N מתוך M"), so we parse that to know when to stop.

Detail pages (``/ad/<id>``) add floor + amenities; we enrich best-effort via
``fa-check`` / ``fa-times`` icons adjacent to Hebrew labels.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Iterable
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from deal_hunter.adapters.base import SearchFilters
from deal_hunter.http_client import fetch
from deal_hunter.models import Listing

log = logging.getLogger(__name__)

WEB_BASE = "https://www.ad.co.il"
DEFAULT_PATH = "/nadlansale"

AD_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    "Referer": f"{WEB_BASE}/",
}

# ── Hebrew → Listing bool field map for detail-page enrichment ─────────────
AMENITY_LABELS: dict[str, str] = {
    "מעלית": "elevator",
    "חניה": "parking",
    "מרפסת": "balcony",
    "מרפסת שמש": "balcony",
    'ממ"ד': "mamad",
    "ממד": "mamad",
    "מזגן": "ac",
    "משופצת": "renovated",
}

# Agent/agency hints in the card-contacts block of detail pages.
AGENCY_KEYWORDS = [
    "תיווך", 'נדל"ן', "מתווך", "סוכנ", "נכסים",
    "רימקס", "RE/MAX", "קולדוול", "century", "סנצ'ורי",
]


class AdAdapter:
    source = "ad"

    def __init__(
        self,
        city_paths: list[str],
        search: dict[str, Any],
        *,
        max_pages: int = 5,
        request_delay_sec: float = 2.0,
        enrich_details: bool = False,
    ):
        # Each entry is a full URL path like "/nadlansale" or "/city/tel-aviv".
        # An empty list defaults to the national for-sale board.
        self.city_paths = city_paths or [DEFAULT_PATH]
        self.search = search
        self.max_pages = max_pages
        self.request_delay = request_delay_sec
        self.enrich_details = enrich_details

    # ---- public ScraperAdapter surface ---------------------------------

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        for path in self.city_paths:
            yield from self._iter_path(path)

    def fetch_detail(self, listing: Listing) -> Listing:
        """Enrich the listing in-place with floor + amenities from its detail page."""
        try:
            html = fetch(listing.url, headers=AD_HEADERS, as_json=False)
        except Exception as e:
            log.debug("ad detail fetch failed for %s: %s", listing.source_id, e)
            return listing
        if not isinstance(html, str):
            return listing
        soup = BeautifulSoup(html, "html.parser")
        _apply_detail_enrichment(listing, soup)
        return listing

    # ---- internals ------------------------------------------------------

    def _iter_path(self, path: str) -> Iterable[Listing]:
        total_pages: int | None = None
        seen_ids: set[str] = set()
        filter_stats: dict[str, int] = {}
        for page in range(1, self.max_pages + 1):
            url = self._build_feed_url(path, page)
            html = fetch(url, headers=AD_HEADERS, as_json=False)
            if not isinstance(html, str) or not html:
                log.warning("ad %s page %d: empty/bad response", path, page)
                break
            soup = BeautifulSoup(html, "html.parser")

            if total_pages is None:
                total_pages = _parse_total_pages(soup)
                log.info("ad %s: %s page(s) reported", path, total_pages or "?")

            cards = soup.select("div.card.overflow-hidden")
            if not cards:
                log.info("ad %s page %d: no cards, stopping", path, page)
                break
            yielded = 0
            for card in cards:
                listing = self._parse_card(card)
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
                yielded += 1
                yield listing
            log.info(
                "ad %s page %d: %d cards, %d emitted", path, page, len(cards), yielded
            )

            if total_pages is not None and page >= total_pages:
                break
            time.sleep(self.request_delay + random.uniform(0.2, 0.8))
        if filter_stats:
            total_filtered = sum(filter_stats.values())
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(filter_stats.items(), key=lambda x: -x[1]))
            log.info("ad %s filter stats (%d filtered): %s", path, total_filtered, summary)

    def _build_feed_url(self, path: str, page: int) -> str:
        if page <= 1:
            return f"{WEB_BASE}{path}"
        # ad.co.il uses ?pageindex=N (not page=N)
        sep = "&" if "?" in path else "?"
        return f"{WEB_BASE}{path}{sep}{urlencode({'pageindex': page})}"

    def _parse_card(self, card: Tag) -> Listing | None:
        a = card.find("a", href=re.compile(r"^/ad/\d+"))
        if not isinstance(a, Tag):
            return None
        href = a["href"]
        m = re.search(r"/ad/(\d+)", href if isinstance(href, str) else "")
        if not m:
            return None
        source_id = m.group(1)

        title_el = a.find("h2", class_="card-title")
        title = _text(title_el)
        city, neighborhood = _split_city_title(title)

        street_p = card.find("p", class_="card-text")
        street = _text(street_p)
        house_number = _trailing_number(street)

        # Price
        price_el = card.find("div", class_="price")
        price = _parse_price(_text(price_el))
        if price is None:
            return None

        # Icons → rooms / sqm
        rooms: float | None = None
        sqm: int | None = None
        for i in card.select("i[class*=fa-]"):
            cls = " ".join(i.get("class", []) if isinstance(i.get("class"), list) else [])
            wrapper_txt = _text(i.parent)
            if "fa-bed" in cls:
                rooms = _first_float(wrapper_txt)
            elif "fa-expand-arrows-alt" in cls:
                sqm_v = _first_float(wrapper_txt)
                sqm = int(sqm_v) if sqm_v is not None else None

        # Image (protocol-relative URLs on ad.co.il)
        images: list[str] = []
        img = card.find("img")
        if isinstance(img, Tag):
            src = img.get("src") or ""
            if isinstance(src, str) and src:
                images.append(_absolutize(src))

        tags: list[str] = []
        if "מקודם" in card.get_text():
            tags.append("promoted")

        url = f"{WEB_BASE}{href}" if isinstance(href, str) else WEB_BASE

        return Listing(
            source="ad",
            source_id=source_id,
            url=url,
            city=city,
            neighborhood=neighborhood,
            street=street,
            house_number=house_number,
            address=", ".join(x for x in [street, neighborhood, city] if x),
            rooms=rooms,
            sqm=sqm,
            floor=None,  # not on feed cards; enrich via detail
            price=price,
            price_per_sqm=round(price / sqm) if sqm else None,
            listing_type="apartment",
            images=images,
            tags=tags,
            source_payload={"_path": "feed"},
        )

    def _passes_filters(self, listing: Listing) -> str | None:
        s = self.search
        if listing.price < s.get("price_min", 0) or listing.price > s.get("price_max", 10**12):
            return "price_out_of_range"
        if listing.rooms is not None:
            if not (s.get("rooms_min", 0) <= listing.rooms <= s.get("rooms_max", 99)):
                return "rooms_out_of_range"
        if s.get("min_sqm") and listing.sqm and listing.sqm < s["min_sqm"]:
            return "sqm_too_small"
        if s.get("exclude_ground_floor") and listing.floor == 0:
            return "ground_floor"
        return None


# ── parsing helpers (module-level, pure) ────────────────────────────────────


def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if isinstance(el, Tag) else ""


def _parse_price(s: str) -> int | None:
    """'6,000,000 ₪' → 6000000. Also handles '1,670K ₪' shorthand."""
    if not s:
        return None
    s = s.replace("₪", "").strip()
    m = re.search(r"([\d,\.]+)\s*([kKmM]?)", s)
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


def _first_float(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _trailing_number(street: str) -> str:
    """'הקוממיות 16' → '16'."""
    m = re.search(r"(\d+[A-Za-z]?)\s*$", street or "")
    return m.group(1) if m else ""


def _split_city_title(title: str) -> tuple[str, str]:
    """'תל אביב יפו נוה שאנן' → (city='תל אביב יפו', neighborhood='נוה שאנן').

    Feed titles are "<city> [<neighborhood>]" with a space. Known multi-word
    cities are special-cased; otherwise we fall back to first word = city,
    remainder = neighborhood.
    """
    if not title:
        return "", ""
    multi = [
        "תל אביב יפו",
        "תל אביב",
        "בת ים",
        "רמת גן",
        "גבעת שמואל",
        "גבעת ברנר",
        "גבעתיים",
        "ראשון לציון",
        "פתח תקווה",
        "קריית אונו",
        "קרית אונו",
        "קריית גת",
        "קריית ביאליק",
        "קריית מוצקין",
        "קריית ים",
        "קריית אתא",
        "נוף הגליל",
        "באר שבע",
        "כפר סבא",
        "הוד השרון",
        "רמת השרון",
        "רמת הגולן",
        "מעלה אדומים",
        "יד בנימין",
        "אור יהודה",
        "אור עקיבא",
    ]
    for m in multi:
        if title.startswith(m):
            rest = title[len(m):].strip()
            return m, rest
    parts = title.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _absolutize(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return WEB_BASE + src
    return src


def _parse_total_pages(soup: BeautifulSoup) -> int | None:
    """Parse the 'עמוד N מתוך M' label or count .page-item links."""
    for el in soup.select(".card-pagination, nav[aria-label=Pagination]"):
        m = re.search(r"מתוך\s+(\d+)", el.get_text(" ", strip=True))
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def _apply_detail_enrichment(listing: Listing, soup: BeautifulSoup) -> None:
    """Fill floor + amenity bools + description from a parsed detail page."""
    # Floor: i.fa-building → "קומה 19"
    for i in soup.select("i[class*=fa-building]"):
        txt = _text(i.parent)
        m = re.search(r"קומה\s+(-?\d+)", txt)
        if m:
            try:
                listing.floor = int(m.group(1))
            except ValueError:
                pass
            break

    # Amenities: walk each i.fa-check / i.fa-times in the info block.
    for icon in soup.select("i.fa-check, i.fa-times"):
        classes = icon.get("class") or []
        if not isinstance(classes, list):
            continue
        is_check = any("fa-check" in c for c in classes)
        parent_txt = _text(icon.parent)
        if not parent_txt or len(parent_txt) > 30:
            continue
        # Strip the icon class text residue — parent_txt is already .get_text()
        for label, field in AMENITY_LABELS.items():
            if label in parent_txt:
                # Never overwrite a True with a False (multiple labels may map to same field).
                if is_check or not getattr(listing, field, False):
                    setattr(listing, field, bool(is_check))
                break

    # Description
    og = soup.find("meta", attrs={"property": "og:description"})
    if og is not None and isinstance(og, Tag):
        content = og.get("content")
        if isinstance(content, str) and content:
            listing.description = content.strip()[:2000]

    # Agent/agency heuristic
    contact = soup.find(class_=re.compile(r"card-contacts"))
    if isinstance(contact, Tag):
        txt = contact.get_text(" ", strip=True)
        if any(kw in txt for kw in AGENCY_KEYWORDS):
            listing.is_agent = True
