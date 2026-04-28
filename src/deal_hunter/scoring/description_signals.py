from __future__ import annotations

import logging
import re

from deal_hunter.models import Listing
from deal_hunter.normalize.hebrew import strip_niqqud

log = logging.getLogger(__name__)

_HEB_CHAR = re.compile(r"[\u0590-\u05FF]")

_UNITS_COUNT_FROM_TEXT = re.compile(
    r"(?:מחולק[ת]?\s*(?:ל[-\s]*)?"
    r")?"
    r"(\d{1,3})"
    r"[\s\u200e\u200f]*"
    r"(?:יחידות|יחידת)"
    r"[\s\u200e\u200f]*"
    r"(?:דיור|מניבות?)"
)
_UNITS_DIVIDED_FROM_TEXT = re.compile(
    r"מחולק[ת]?\s*(?:ל[-\s]*)?"
    r"(\d{1,3})"
    r"[\s\u200e\u200f]*"
    r"(?:יחידות|דירות)"
)


def _standalone_word(text: str, word: str) -> bool:
    for m in re.finditer(re.escape(word), text):
        before = text[m.start() - 1] if m.start() > 0 else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if not _HEB_CHAR.match(before) and not _HEB_CHAR.match(after):
            return True
    return False


def _try_extract_units_count(text: str) -> int | None:
    for pat in (_UNITS_COUNT_FROM_TEXT, _UNITS_DIVIDED_FROM_TEXT):
        m = pat.search(text)
        if m:
            val = int(m.group(1))
            if val > 0:
                return val

    # Fallback: when no explicit count, count distinct "יחידת דיור" mentions
    unit_mentions = re.findall(
        r"(?:^|[\s\u200e\u200f])(?:ו)?יחידת\s+דיור", text
    )
    if len(unit_mentions) >= 2:
        has_separator = any(
            kw in text for kw in ("נוספת", "נוסף", "נפרדת", "נפרד", "סטודיו", "שנייה", "אחרת")
        )
        if has_separator:
            return len(unit_mentions)

    return None


def combined_search_text(listing: Listing) -> str:
    parts = [listing.description or ""]
    if listing.tags:
        parts.append(" ".join(listing.tags))
    raw = " ".join(parts)
    he = strip_niqqud(raw)
    return he.casefold()


_GARDEN_MARKERS = (
    "גינה גדולה",
    "גינה ענקית",
    "גינה עשירה",
    "גינה רחבה",
    "חצר גדולה",
    "חצר ענקית",
    "שטח גינה",
    "מגרש גדול",
)


def multi_unit_bonus_and_matches(
    text: str, effective_units: int | None = None
) -> tuple[float, list[str], str]:
    matched: list[str] = []
    bonus = 0.0
    source: str = "description"

    if effective_units is not None and effective_units >= 2:
        bonus = min(2.5, 0.5 * (effective_units - 1))
        matched.append(f"{effective_units} יחידות (מפורש)")
        source = "structured"
        return bonus, matched, source

    t = text

    # Try to extract count from text when effective_units is None
    units_from_text = _try_extract_units_count(t)
    if units_from_text is not None and units_from_text >= 2:
        bonus = min(2.5, 0.5 * (units_from_text - 1))
        matched.append(f"{units_from_text} יחידות (מהטקסט)")
        source = "description_count"
        return bonus, matched, source

    if "יחידות דיור" in t:
        matched.append("יחידות דיור")
        bonus += 0.38
    if "יחידת דיור" in t:
        matched.append("יחידת דיור")
        bonus += 0.38
    if "מחולקת" in t:
        matched.append("מחולקת")
        bonus += 0.32
    elif "מחולק" in t:
        matched.append("מחולק")
        bonus += 0.28
    if "apartment" in t:
        matched.append("apartment")
        bonus += 0.28
    if "יחידות דיור" not in t and _standalone_word(t, "יחידות"):
        matched.append("יחידות")
        bonus += 0.22
    if "יחידת דיור" not in t and _standalone_word(t, "יחידה"):
        matched.append("יחידה")
        bonus += 0.22
    cap = 2.0
    return min(cap, bonus), matched, source


def garden_bonus_and_matches(
    text: str,
    effective_garden_sqm: int | None = None,
) -> tuple[float, list[str], str]:
    found = []
    source = "description"

    garden = effective_garden_sqm

    sqm_val = None
    sqm_label = ""
    if garden is not None and garden > 0:
        sqm_val = garden
        sqm_label = "גינה"

    if sqm_val is not None:
        sqm_val = max(sqm_val, 0)
        if sqm_val <= 30:
            raw = 0.0
        elif sqm_val <= 300:
            raw = 1.5 * (sqm_val - 30) / 270
        else:
            raw = 1.5
        found.append(f"{sqm_label} {sqm_val} מ\"ר")
        source = "structured"
        return raw, found, source

    for m in _GARDEN_MARKERS:
        if m in text:
            found.append(m)
    if not found:
        return 0.0, [], source
    raw = min(0.42, 0.14 * len(found))
    return raw, found, source


def garden_bonus_scoring(
    listing: Listing,
    text: str,
    effective_garden_sqm: int | None = None,
) -> tuple[float, dict[str, object]]:
    g_pts, g_hit, g_src = garden_bonus_and_matches(
        text, effective_garden_sqm
    )
    detail: dict[str, object] = {
        "garden_bonus": round(g_pts, 3),
        "garden_bonus_source": g_src,
    }
    if g_hit:
        detail["matched_garden_phrases"] = g_hit
    return g_pts, detail
