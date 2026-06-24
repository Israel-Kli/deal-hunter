"""agora.co.il free-items scraper.

agora.co.il is a classic server-rendered classifieds board for items people are
giving away ("חפצים למסירה"). Listings render as ``tr.objectsTitleTr`` rows
inside ``table#objectsTable`` on the ``/toGet.asp`` search page. Each row carries:

* ``onclick="showObjectDetails('YYYY-MM', <id>)"`` → month bucket + numeric id
* ``td.objectName a[href=/cache/YYYY-MM/<id>_o.asp]`` → title + cached ad URL
* ``td.objectState span.condition<N>``                → numeric condition class
* ``td.area``                                         → city / locality
* ``td.photoIcon a[href*='showPhoto.asp']``           → photo popup (only when image exists)
* ``td.regDate[title="DD/MM/YYYY HH:MM"]``            → exact post timestamp

Feed endpoint:
    GET https://www.agora.co.il/toGet.asp
        ?dealType=1
        &iseek=<keyword>
        &takeCity=<area>
        &condition=<N>
        [&category=<N>&subcategory=<N>]

The page returns all matches in a single response — no pagination observed.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Iterable
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from deal_hunter.freebies.models import FreebieItem
from deal_hunter.http_client import fetch

log = logging.getLogger(__name__)

WEB_BASE = "https://www.agora.co.il"
FEED_PATH = "/toGet.asp"

AGORA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
    "Referer": f"{WEB_BASE}/",
}

_ONCLICK_RE = re.compile(r"showObjectDetails\(\s*'([\d-]+)'\s*,\s*(\d+)\s*\)")
_CONDITION_CLASS_RE = re.compile(r"condition(\d+)")
_REG_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")


def build_feed_url(
    *,
    keyword: str,
    city: str,
    condition: int,
    deal_type: int = 1,
    category: int | None = None,
    subcategory: int | None = None,
) -> str:
    params: dict[str, str | int] = {
        "dealType": deal_type,
        "iseek": keyword,
        "takeCity": city,
        "condition": condition,
    }
    if category is not None:
        params["category"] = category
    if subcategory is not None:
        params["subcategory"] = subcategory
    return f"{WEB_BASE}{FEED_PATH}?{urlencode(params)}"


def fetch_items(
    *,
    watch_label: str,
    keyword: str,
    city: str,
    condition: int,
    deal_type: int = 1,
    category: int | None = None,
    subcategory: int | None = None,
) -> list[FreebieItem]:
    """Fetch a watch's search page and return parsed items. Empty list on failure."""
    url = build_feed_url(
        keyword=keyword,
        city=city,
        condition=condition,
        deal_type=deal_type,
        category=category,
        subcategory=subcategory,
    )
    # agora.co.il blocks the default chrome120 TLS fingerprint from Azure IPs (403).
    # Firefox impersonation passes through cleanly.
    html = fetch(url, headers=AGORA_HEADERS, as_json=False, impersonate="firefox133")
    if not isinstance(html, str) or not html:
        log.warning("agora %s: empty/bad response", watch_label)
        return []
    return parse_items(html, watch_label=watch_label)


def parse_items(html: str, *, watch_label: str) -> list[FreebieItem]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[FreebieItem] = []
    seen: set[str] = set()
    for row in soup.select("tr.objectsTitleTr"):
        item = _parse_row(row, watch_label=watch_label)
        if item is None:
            continue
        if item.source_id in seen:
            continue
        seen.add(item.source_id)
        out.append(item)
    return out


def _parse_row(row: Tag, *, watch_label: str) -> FreebieItem | None:
    onclick = row.get("onclick") or ""
    if not isinstance(onclick, str):
        return None
    m = _ONCLICK_RE.search(onclick)
    if not m:
        return None
    month_bucket = m.group(1)
    source_id = m.group(2)

    name_a = row.select_one("td.objectName a")
    if not isinstance(name_a, Tag):
        return None
    title = name_a.get_text(" ", strip=True)
    if not title:
        return None

    href = name_a.get("href")
    if isinstance(href, str) and href:
        url = _absolutize(href)
    else:
        url = f"{WEB_BASE}/cache/{month_bucket}/{source_id}_o.asp"

    city = ""
    area_el = row.select_one("td.area")
    if isinstance(area_el, Tag):
        city = area_el.get_text(" ", strip=True)

    condition: int | None = None
    cond_el = row.select_one("td.objectState span")
    if isinstance(cond_el, Tag):
        classes = cond_el.get("class") or []
        if isinstance(classes, list):
            for cls in classes:
                cm = _CONDITION_CLASS_RE.fullmatch(cls)
                if cm:
                    try:
                        condition = int(cm.group(1))
                    except ValueError:
                        pass
                    break

    image_url: str | None = None
    photo_a = row.select_one("td.photoIcon a[href]")
    if isinstance(photo_a, Tag):
        ph = photo_a.get("href")
        if isinstance(ph, str) and ph and "showPhoto" in ph:
            image_url = _absolutize(ph)

    posted_at = ""
    reg_el = row.select_one("td.regDate")
    if isinstance(reg_el, Tag):
        title_attr = reg_el.get("title")
        if isinstance(title_attr, str):
            posted_at = _parse_dmy(title_attr) or ""
    if not posted_at:
        posted_at = date.today().isoformat()

    return FreebieItem(
        source="agora",
        source_id=source_id,
        watch_label=watch_label,
        title=title,
        city=city,
        condition=condition,
        url=url,
        image_url=image_url,
        posted_at=posted_at,
    )


def _parse_dmy(s: str) -> str | None:
    """'17/6/2026 21:43' → '2026-06-17'. Returns None if unparseable."""
    if not s:
        return None
    m = _REG_DATE_RE.search(s)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _absolutize(src: str) -> str:
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return WEB_BASE + src
    return f"{WEB_BASE}/{src}"
