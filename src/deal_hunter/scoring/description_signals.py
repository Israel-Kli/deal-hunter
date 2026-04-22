"""Text signals from listing description + tags for heuristic scoring."""

from __future__ import annotations

from deal_hunter.models import Listing
from deal_hunter.normalize.hebrew import strip_niqqud


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
    """Positive adjustment (capped) only for explicit phrases (same set as dashboard highlights)."""
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
