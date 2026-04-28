from __future__ import annotations

import logging
import re

from deal_hunter.models import Listing
from deal_hunter.normalize.hebrew import strip_niqqud

log = logging.getLogger("deal_hunter.extract_features")

_SQM = r'מ[״"\'׳]?\s*ר'

_UNITS_RE = re.compile(
    r"(?:מחולק[ת]?\s*(?:ל[-\s]*)?"   # optional "מחולק/מחולקת ל/ל-"
    r")?"                             # close and make the whole prefix optional
    r"(\d{1,3})"                      # capture the count (1-3 digits for up to 999)
    r"[\s\u200e\u200f]*"               # whitespace + Unicode direction marks
    r"(?:יחידות|יחידת)"               # plural or singular construct (יחידת ≠ יחידות)
    r"[\s\u200e\u200f]*"
    r"(?:דיור|מניבות?)"              # followed by "דיור" or "מניבה/מניבות"
)
# "מחולק[ת] ל-N יחידות" without explicit "דיור"/"מניבות" — prefix is REQUIRED here
_UNITS_DIVIDED_RE = re.compile(
    r"מחולק[ת]?\s*(?:ל[-\s]*)?"
    r"(\d{1,3})"
    r"[\s\u200e\u200f]*"
    r"(?:יחידות|דירות)"
)
# Negative: "יחידות הורים" / "יחידות אורחים" etc. (internal suites, not separate housing)
_UNITS_FALSE_POSITIVE_RE = re.compile(
    r"יחידות?\s*(?:הורים?|אורחים?|מתבגרים?|יחידת\s*הורים?|שינה|רחצה|מיזוג|מזגנים?)"
)
_GARDEN_RE = re.compile(
    r"(?:גינ[הת]|חצר)[^.\n]{0,40}?(\d{2,4})\s*" + _SQM
)
_LOT_RE = re.compile(
    r"מגרש[^.\n]{0,30}?(\d{2,5})\s*" + _SQM
)


def _extract_units_count(text: str) -> int | None:
    for pat in (_UNITS_RE, _UNITS_DIVIDED_RE):
        m = pat.search(text)
        if m:
            val = int(m.group(1))
            if val <= 0:
                continue
            # Check for false positives like "יחידות הורים"
            fp = _UNITS_FALSE_POSITIVE_RE.search(text)
            if fp and fp.start() <= m.start() + len(m.group(0)) and fp.start() >= m.start() - 10:
                continue
            return val

    # Fallback: count distinct mentions of "יחידת דיור" or "יחידות דיור"
    # when no explicit count number is given (e.g. "וילה עם יחידת דיור נפרדת ויחידת דיור סטודיו נוספת")
    unit_mentions = re.findall(
        r"(?:^|[\s\u200e\u200f])(?:ו)?יחידת\s+דיור", text
    )
    if len(unit_mentions) >= 2:
        # Verify at least one mention has a marker suggesting a separate unit
        # (not just the same unit repeated)
        has_separator = any(
            kw in text for kw in ("נוספת", "נוסף", "נפרדת", "נפרד", "סטודיו", "שנייה", "אחרת")
        )
        if has_separator:
            return len(unit_mentions)

    # Single "יחידת דיור" without a count — likely 1 unit (e.g. "יש יחידת דיור מניבה")
    if len(unit_mentions) == 1:
        single = re.search(r"(?:^|[\s\u200e\u200f])(?:ו)?יחידת\s+דיור", text)
        if single:
            before = text[max(0, single.start() - 40):single.start()]
            if not re.search(r"(?:ללא|אין|אינו|איננו|לא\s+(?:כולל|מכיל|יש|קיים|נכלל))\b", before):
                return 1

    return None


def extract_features(listing: Listing) -> None:
    text_parts = [listing.description or ""]
    if listing.tags:
        text_parts.append(" ".join(listing.tags))
    raw = " ".join(text_parts)
    text = strip_niqqud(raw).casefold()

    if listing.units_count is None:
        val = _extract_units_count(text)
        if val is not None:
            listing.units_count = val
            log.info(
                "%s %s: extracted units_count=%d from description",
                listing.source, listing.source_id, val,
            )

    if listing.garden_sqm is None:
        m = _GARDEN_RE.search(text)
        if m:
            val = int(m.group(1))
            if 5 <= val <= 9999:
                listing.garden_sqm = val

    if listing.lot_sqm is None:
        m = _LOT_RE.search(text)
        if m:
            val = int(m.group(1))
            if 20 <= val <= 99999:
                listing.lot_sqm = val
