"""Hebrew text normalization for cross-source address dedup.

Removes Niqqud (vowel points), expands common abbreviations, strips stopwords,
and normalizes whitespace so that addresses like:
  "רח' הרצל 12"  vs  "רחוב הרצל  12"  vs  "הרצל 12"
all converge to the same canonical form for matching.
"""

from __future__ import annotations

import re
import unicodedata

# Niqqud / cantillation marks — Unicode blocks 0591-05C7
_NIQQUD_RE = re.compile(r"[\u0591-\u05C7\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5]")

# Common Hebrew address abbreviations → full forms
# We use (?:^|\s) prefix and (\s|$) suffix instead of \b (doesn't work with Hebrew).
_ABBREVS: list[tuple[str, str]] = [
    (r"(?:^|\s)רח'(?=\s|$)", "רחוב"),
    (r"(?:^|\s)שכ'(?=\s|$)", "שכונה"),
    (r"(?:^|\s)מרפ'(?=\s|$)", "מרפסת"),
    (r"(?:^|\s)ממ['״]ד(?=\s|$)", "ממד"),
    (r"(?:^|\s)מ['״]ד(?=\s|$)", "ממד"),
    (r"(?:^|\s)מ['״]ר(?=\s|$)", "מטר"),
    (r"(?:^|\s)חד'?(?=\s|$)", "חדר"),
    (r"(?:^|\s)מס['״]ד(?=\s|$)", "מסדרון"),
    (r"(?:^|\s)כניסה(?=\s|$)", ""),
    (r"(?:^|\s)בניין(?=\s|$)", ""),
    (r"(?:^|\s)דירה(?=\s|$)", ""),
]


def _expand_abbreviations(text: str) -> str:
    """Expand Hebrew abbreviations, preserving leading whitespace."""
    for pattern, repl in _ABBREVS:
        def _replacer(m: re.Match) -> str:
            leading = m.group(0)[:1] if m.group(0)[0].isspace() else ""
            return leading + repl
        text = re.sub(pattern, _replacer, text)
    return text

# Stopwords to drop when building a canonical address key.
# These add noise but carry no disambiguating value for dedup.
STOPWORDS: set[str] = {
    "רחוב", "הרחוב", "רח", "שכונה", "קומה", "כניסה", "בניין",
    "דירה", "דירות", "למכירה", "למכר", "למכירה", "למכירה",
    "חדר", "חדרים", "מטר", "מטרים",
    "ה", "של", "את", "על", "עם", "ב", "מן", "אל", "גם",
    "אזור", "תעשייה", "תעשיה", "מסחרי", "מרכז",
    "ישוב", "עיר",
}


def strip_niqqud(text: str) -> str:
    """Remove Hebrew vowel points and cantillation marks."""
    return _NIQQUD_RE.sub("", text)


def normalize_hebrew(text: str) -> str:
    """Full normalization pipeline: Niqqud → abbreviations → stopwords → whitespace."""
    if not text:
        return ""
    text = strip_niqqud(text)
    # Normalize apostrophe/quote variants that appear in Hebrew text
    # Map various quote marks to a single canonical form
    text = text.replace("'", "'").replace('"', '"').replace('"', '"').replace("״", '"')
    # Expand abbreviations
    text = _expand_abbreviations(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonicalize_address(text: str) -> str:
    """Produce a dedup-friendly key from an address string.

    Drops stopwords, lowercases (Hebrew has no case, but keeps Latin consistent),
    and collapses to a single token stream.
    """
    norm = normalize_hebrew(text)
    tokens = norm.split()
    filtered = [t for t in tokens if t not in STOPWORDS]
    return " ".join(filtered)


def extract_street_number(addr: str) -> tuple[str, str]:
    """Split an address into (street_name, house_number).

    Handles: 'הרצל 12', 'רחוב הרצל 12', 'הרב פרנקל 63',
    'ביאליק 24א', 'דרך בגין 100'.
    Returns the trailing token as house_number if it starts with a digit.
    """
    norm = normalize_hebrew(addr)
    tokens = norm.split()
    if not tokens:
        return "", ""
    last = tokens[-1]
    # House number: starts with digit, optionally followed by letter(s)
    if re.match(r"^\d+[A-Za-zא-ת]?$", last):
        return " ".join(tokens[:-1]), last
    return norm, ""


def sqm_bucket(sqm: int | None, bucket_size: int = 10) -> str:
    """Round sqm down to the nearest bucket for canonical key.

    E.g. sqm=73, bucket_size=10 → '70'. None → ''.
    """
    if sqm is None:
        return ""
    return str((sqm // bucket_size) * bucket_size)


def rooms_bucket(rooms: float | None) -> str:
    """Round rooms to nearest 0.5 for canonical key.

    E.g. rooms=4.3 → '4.0', rooms=3.7 → '3.5', None → ''.
    """
    if rooms is None:
        return ""
    return str(round(rooms * 2) / 2)
