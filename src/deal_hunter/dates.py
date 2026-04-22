"""Shared date parsing and merging for listing timelines."""

from __future__ import annotations

import re
from datetime import date, datetime


def parse_yyyy_mm_dd(s: str) -> date | None:
    if not s or len(s) < 10:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_dd_mm_yyyy(s: str) -> date | None:
    if not s:
        return None
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s.strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def earliest_yyyy_mm_dd(*values: str | None) -> str:
    """Return the earliest valid calendar date among YYYY-MM-DD strings, or ``\"\"``."""
    parsed: list[date] = []
    for v in values:
        if not v:
            continue
        d = parse_yyyy_mm_dd(v)
        if d:
            parsed.append(d)
    return min(parsed).isoformat() if parsed else ""
