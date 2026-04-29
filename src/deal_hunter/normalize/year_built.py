"""Extract year_built from Hebrew real-estate text.

Handles two patterns:
  1. Direct year: "שנת בניה 2022", "נבנה בשנת 2018", "בנייה: 2020"
  2. Property age → convert to year: "בן שנתיים", "גיל 5 שנים", "נכס בן 10"
"""

from __future__ import annotations

import re
from datetime import datetime

_YEAR_DIRECT_RE = re.compile(
    r"(?:"
    r"שנת\s+(?:ה)?ב(?:ני[יה]ה?|ניי[ה]?)\s*[:\-=]?\s*(?P<y1>\d{4})"       # שנת בניה 2022
    r"|"
    r"נבנ[הת]\w*\s*(?:ב(?:שנת|חודש|שנים)?\s*)?(?P<y2>\d{4})"              # נבנה ב-2018 / נבנתה 2020 / נבנה בשנת 2018
    r"|"
    r"משנת\s+(?P<y3>\d{4})"                                                # משנת 2015 (but NOT בשנת which matches renovation)
    r"|"
    r"ב(?:ני[יה]ה?|ניי[ה]?)\s*(?:מקורית?|ראשונית?|חדשה?)?\s*[:\-=]?\s*(?P<y4>\d{4})"  # בניה 2024, בנייה: 2020
    r"|"
    r"שנת\s*בניה\s*(?P<y5>\d{4})(?:$|[,.\s])"                            # שנת בניה XXXX (word order reversed)
    r"|"
    r"(?:בית|דירה|נכס|מבנה|קוטג|וילה)\s+חדש[ה]?\s*(?:מ(?:קבלן|ן\s+היסוד))?\s*(?:משנת\s+)?(?P<y6>\d{4})"  # בית חדש 2024
    r"|"
    r"חדש[ה]?\s*(?:מ(?:קבלן|היסוד))?\s*(?:משנת\s+)?(?P<y7>\d{4})"        # חדשה 2023
    r")",
    re.UNICODE,
)

_AGE_RE = re.compile(
    r"(?:"
    r"גיל\s*[:\-=]?\s*(?P<a1>\d{1,2})\s*(?:שנה|שנים|שגיל)?"              # גיל 5, גיל: 10 שנים
    r"|"
    r"(?:נכס|בית|דירה|מבנה|קוטג|וילה|מבנה)\s*(?:הוא\s+)?בן\s+(?P<a2>\d{1,2})\s*(?:שנה|שנים)?"  # נכס בן 5
    r"|"
    r"(?:נכס|בית|דירה|מבנה|קוטג|וילה)\s*(?:היא\s+)?בת\s+(?P<a3>\d{1,2})\s*(?:שנה|שנים)?"        # דירה בת 3
    r"|"
    r"בן\s+(?P<a4>\d{1,2})\s*(?:שנה|שנים|$)"                             # בן 10
    r"|"
    r"בת\s+(?P<a5>\d{1,2})\s*(?:שנה|שנים)"                               # בת 20 שנה
    r"|"
    r"(?P<a6>\d{1,2})\s*(?:שנה|שנים)\s*(?:גיל|ישן|ישנה)"                 # 5 שנים ישן
    r"|"
    r"בן\s+שנתיים"                                                         # בן שנתיים → age 2
    r"|"
    r"בת\s+שנתיים"                                                         # בת שנתיים → age 2
    r"|"
    r"בן\s+שנה(?![ים])"                                                    # בן שנה → age 1
    r"|"
    r"בת\s+שנה(?![ים])"                                                    # בת שנה → age 1
    r")",
    re.UNICODE,
)


def extract_year_built(text: str | None, *, current_year: int | None = None) -> int | None:
    """Extract the construction year from Hebrew property text.

    Returns the year as a 4-digit integer, or None if not found.
    """
    if not text:
        return None

    # 1. Try direct year patterns
    m = _YEAR_DIRECT_RE.search(text)
    if m:
        for g in ("y1", "y2", "y3", "y4", "y5", "y6", "y7"):
            val = m.group(g)
            if val:
                year = int(val)
                if 1900 <= year <= 2100:
                    return year

    # 2. Try age patterns → convert to year
    m = _AGE_RE.search(text)
    if m:
        age = None
        for g in ("a1", "a2", "a3", "a4", "a5", "a6"):
            val = m.group(g)
            if val:
                age = int(val)
                break
        if age is None:
            # Word-based age: "בן שנתיים" → 2, "בת שנתיים" → 2, "בן שנה" → 1, "בת שנה" → 1
            if "שנתיים" in m.group(0):
                age = 2
            elif m.group(0):
                age = 1
        if age is not None and 0 <= age <= 150:
            if current_year is None:
                current_year = datetime.utcnow().year
            return current_year - age

    return None
