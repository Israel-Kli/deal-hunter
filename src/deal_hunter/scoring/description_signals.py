"""Text signals from listing description + tags for heuristic scoring."""

from __future__ import annotations

import re

from deal_hunter.models import Listing
from deal_hunter.normalize.hebrew import strip_niqqud

_HEB_CHAR = re.compile(r"[\u0590-\u05FF]")


def _standalone_word(text: str, word: str) -> bool:
    """True if ``word`` appears at least once with no Hebrew letter immediately before/after."""
    for m in re.finditer(re.escape(word), text):
        before = text[m.start() - 1] if m.start() > 0 else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if not _HEB_CHAR.match(before) and not _HEB_CHAR.match(after):
            return True
    return False


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


def multi_unit_bonus_and_matches(text: str) -> tuple[float, list[str]]:
    """Positive adjustment (capped): phrases + standalone יחידות/יחידה (same rules as dashboard highlights)."""
    matched: list[str] = []
    bonus = 0.0
    t = text
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
    cap = 1.0
    return min(cap, bonus), matched


def garden_bonus_and_matches(text: str) -> tuple[float, list[str]]:
    found = [m for m in _GARDEN_MARKERS if m in text]
    if not found:
        return 0.0, []
    raw = min(0.42, 0.14 * len(found))
    return raw, found


def room_count_bonus(rooms: float | None) -> float:
    if rooms is None or rooms < 4.5:
        return 0.0
    span = max(7.0 - 4.5, 0.1)
    return min(0.48, ((rooms - 4.5) / span) * 0.48)


def outdoor_and_rooms_bonus(listing: Listing, text: str) -> tuple[float, dict[str, object]]:
    g_pts, g_hit = garden_bonus_and_matches(text)
    r_pts = room_count_bonus(listing.rooms)
    combined = min(0.78, g_pts + r_pts)
    detail: dict[str, object] = {
        "garden_bonus": round(g_pts, 3),
        "room_layout_bonus": round(r_pts, 3),
        "outdoor_rooms_bonus": round(combined, 3),
    }
    if g_hit:
        detail["matched_garden_phrases"] = g_hit
    return combined, detail
