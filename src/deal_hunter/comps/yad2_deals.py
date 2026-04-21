"""Yad2 closed-deal comparable sales extractor.

Implements CompsProvider by fetching the item HTML page for a given listing
and parsing the server-side-rendered `data-testid="deals-history"` table.

The deals table is NOT in the Next.js JSON — it is SSR'd only in the HTML.
Ported from Eilons' _extract_deals_from_html / _extract_comparable_sales_html
(upstream lines 538-720); restructured to return list[Comp] and to share the
HTML fetch with the enrichment pass in the Yad2 adapter.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from typing import Any

from deal_hunter.http_client import fetch
from deal_hunter.models import Comp

log = logging.getLogger(__name__)

BASE = "https://www.yad2.co.il"


def _clean(cell: str) -> str:
    """Strip HTML tags and whitespace/bidi marks from a table cell."""
    text = re.sub(r"<[^>]+>", "", cell)
    text = re.sub(r"[\u200f\u200e\xa0]", "", text)
    return text.strip()


def _address_hash(city: str, neighborhood: str, street: str, house_number: str) -> str:
    key = "|".join([city, neighborhood, street, house_number]).lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def extract_comps_from_html(html: str, *, source_city: str = "", source_neighborhood: str = "") -> list[Comp]:
    """Parse the deals-history table from a Yad2 item HTML page.

    Returns a list of Comp objects. Returns [] on no table / parse failure.

    Table columns (RTL Hebrew):
        כתובת | סוג נכס | ת. עסקה | חד׳ | מ״ר | קומה | נבנה | מחיר
         addr   type     date    rooms  sqm  floor  built  price
    """
    if not html:
        return []

    # Prefer the section with data-testid="deals-history"
    section_match = re.search(
        r'data-testid="deals-history"(.*?)</section>',
        html,
        re.DOTALL,
    )
    if not section_match:
        # Fallback: find table after the heading text
        section_match = re.search(
            r"נכסים שנמכרו באזור.*?<tbody[^>]*>(.*?)</tbody>",
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if not section_match:
            return []
        tbody = section_match.group(1)
    else:
        section_html = section_match.group(1)
        tbody_match = re.search(r"<tbody[^>]*>(.*?)</tbody>", section_html, re.DOTALL)
        tbody = tbody_match.group(1) if tbody_match else section_html

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody, re.DOTALL)
    if not rows:
        return []

    comps: list[Comp] = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if len(cells) < 7:
            continue

        clean = [_clean(c) for c in cells]

        address_raw = clean[0]   # כתובת
        # prop_type  = clean[1] # סוג נכס  (unused for Comp)
        deal_date = clean[2]    # ת. עסקה  e.g. "03/2024"
        rooms_raw = clean[3]    # חד׳
        sqm_raw = clean[4]      # מ״ר
        floor_raw = clean[5]    # קומה
        built_raw = clean[6] if len(clean) > 6 else ""
        price_raw = clean[7] if len(clean) > 7 else ""

        # Parse price: "2,300,000 ₪" → 2300000
        price_digits = re.sub(r"[^\d]", "", price_raw)
        if not price_digits or not address_raw:
            continue
        price = int(price_digits)

        # Parse rooms
        rooms: float | None = None
        try:
            rooms = float(rooms_raw.replace("½", ".5").replace("٥", ".5"))
        except (ValueError, AttributeError):
            pass

        # Parse sqm
        sqm: int | None = None
        sqm_digits = re.sub(r"[^\d]", "", sqm_raw)
        if sqm_digits:
            sqm = int(sqm_digits)

        # Parse year_built
        year_built: int | None = None
        built_digits = re.sub(r"[^\d]", "", built_raw)
        if built_digits and len(built_digits) == 4:
            year_built = int(built_digits)

        # Split address into street + house_number heuristically
        street, house_number = _split_address(address_raw)

        ah = _address_hash(
            source_city, source_neighborhood, street, house_number
        )

        comps.append(
            Comp(
                source="yad2_deals",
                address=address_raw,
                city=source_city,
                neighborhood=source_neighborhood,
                street=street,
                house_number=house_number,
                deal_date=deal_date,
                price=price,
                sqm=sqm,
                rooms=rooms,
                year_built=year_built,
                raw={
                    "floor": floor_raw,
                    "address_hash": ah,
                },
            )
        )

    return comps


def _split_address(address: str) -> tuple[str, str]:
    """Best-effort split of 'רחוב הרצל 12' → ('רחוב הרצל', '12')."""
    m = re.match(r"^(.*?)\s+(\d[\d\w/]*)$", address.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return address.strip(), ""


class Yad2DealsProvider:
    """CompsProvider that fetches comparable sales from Yad2 item HTML pages.

    Usage:
        provider = Yad2DealsProvider()
        comps = provider.comps_for_listing(listing, build_id=bid, slug=slug)

    The `comps_for` method satisfies the CompsProvider Protocol but requires
    a prior listing (token) to know which page to fetch — so callers should
    use `comps_for_listing` when they have the full listing available.
    """

    name = "yad2_deals"

    def fetch_html(self, token: str, build_id: str, slug: str) -> str | None:
        """Fetch the item HTML page. Returns raw HTML or None."""
        time.sleep(1.0 + random.uniform(0.2, 0.8))
        html_url = f"{BASE}/realestate/item/{slug}/{token}"
        html = fetch(html_url, as_json=False)
        if not html:
            log.warning("Yad2DealsProvider: failed to fetch %s", html_url)
        return html

    def comps_for_listing(
        self,
        token: str,
        build_id: str,
        slug: str,
        *,
        city: str = "",
        neighborhood: str = "",
        html: str | None = None,
    ) -> list[Comp]:
        """Extract comps for a specific listing token.

        If `html` is provided (already fetched by the enrichment pass), no
        extra HTTP request is made.
        """
        if html is None:
            html = self.fetch_html(token, build_id, slug)
        if not html:
            return []
        return extract_comps_from_html(html, source_city=city, source_neighborhood=neighborhood)

    def comps_for(
        self,
        *,
        city: str,
        neighborhood: str,
        street: str,
        rooms: float | None,
        sqm: int | None,
        window_months: int = 18,
    ) -> list[Comp]:
        """Protocol-satisfying stub.

        The Yad2 deals table is per-listing HTML, not a bulk API — so we
        can't efficiently query by address. The repo-level query in
        valuation/fair_price.py reads from the `comps` table directly.
        This method exists for protocol compliance; prefer `comps_for_listing`.
        """
        log.debug("Yad2DealsProvider.comps_for called without listing context — returning []")
        return []
