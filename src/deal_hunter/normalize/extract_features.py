from __future__ import annotations

import re

from deal_hunter.models import Listing
from deal_hunter.normalize.hebrew import strip_niqqud

_SQM = r'מ[״"\'׳]?\s*ר'

_UNITS_RE = re.compile(r"(\d{1,2})\s*יחידות?\s*דיור")
_LOT_RE = re.compile(r"מגרש[^.\n]{0,40}?(\d{2,4})\s*" + _SQM)
_GARDEN_RE = re.compile(
    r"(?:גינ[הת]|חצר)[^.\n]{0,40}?(\d{2,4})\s*" + _SQM
)


def extract_features(listing: Listing) -> None:
    text_parts = [listing.description or ""]
    if listing.tags:
        text_parts.append(" ".join(listing.tags))
    raw = " ".join(text_parts)
    text = strip_niqqud(raw).casefold()

    m = _UNITS_RE.search(text)
    if m:
        val = int(m.group(1))
        if val > 0:
            listing.units_count = val

    m = _LOT_RE.search(text)
    if m:
        val = int(m.group(1))
        if 10 <= val <= 9999:
            listing.lot_sqm = val

    m = _GARDEN_RE.search(text)
    if m:
        val = int(m.group(1))
        if 5 <= val <= 9999:
            listing.garden_sqm = val
