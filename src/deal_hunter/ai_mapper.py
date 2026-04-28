"""AI-based feature extraction from Hebrew listing text via Gemini."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from deal_hunter.models import Listing

log = logging.getLogger(__name__)

_AI_FIELDS: tuple[str, ...] = (
    "rooms", "sqm", "sqm_build", "floor",
    "units_count", "garden_sqm", "lot_sqm",
    "parking", "balcony", "elevator", "renovated", "ac",
)

_NUM_CAST: dict[str, type] = {
    "rooms": float,
    "sqm": int,
    "sqm_build": int,
    "floor": int,
    "units_count": int,
    "garden_sqm": int,
    "lot_sqm": int,
}

_SANITY: dict[str, tuple[int | float, int | float]] = {
    "rooms": (0.5, 30),
    "sqm": (10, 10000),
    "sqm_build": (10, 10000),
    "floor": (-10, 200),
    "units_count": (1, 100),
    "garden_sqm": (5, 9999),
    "lot_sqm": (5, 99999),
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rooms": {"type": "number", "nullable": True},
                    "sqm": {"type": "integer", "nullable": True},
                    "sqm_build": {"type": "integer", "nullable": True},
                    "floor": {"type": "integer", "nullable": True},
                    "units_count": {"type": "integer", "nullable": True},
                    "garden_sqm": {"type": "integer", "nullable": True},
                    "lot_sqm": {"type": "integer", "nullable": True},
                    "parking": {"type": "boolean", "nullable": True},
                    "balcony": {"type": "boolean", "nullable": True},
                    "elevator": {"type": "boolean", "nullable": True},
                    "renovated": {"type": "boolean", "nullable": True},
                    "ac": {"type": "boolean", "nullable": True},
                },
            },
        },
    },
    "required": ["results"],
}

_PROMPT_HEADER = """You are a Hebrew real estate listing parser. For each listing below, extract structured fields from the Hebrew text. Return valid JSON matching the schema. No other text.

Rules:
- rooms: number of rooms (חדרים), usually a float like 3.5.
- sqm: advertised area in square meters. From text like "80 מ\"ר", "שטח 100", "100 מטר". NOT the price.
- sqm_build: built area (שטח בנוי, שטח מבונה, שטח ברוטו) if mentioned. DIFFERENT from advertised sqm.
- floor: floor number (קומה). Negative for basement/מרתף, 0 for ground/קרקע.
- units_count: number of separate housing units (יחידות דיור). Count if the listing says it is divided/eligible for splitting (מחולק, פיצול, חלוקה, יחידות דיור, יחידת דיור). Count the number mentioned, NOT just 1 for the property itself. null if not mentioned.
- garden_sqm: garden or yard area (גינה, גינת, חצר, garden) in sqm.
- lot_sqm: land/plot size (מגרש, קרקע, שטח המגרש) in sqm. Different from garden_sqm.
- parking: true if חניה/חנייה mentioned positively (have one). false only if "אין חניה" or "ללא חניה". null if unclear.
- balcony: true if מרפסת/מרפסות mentioned.
- elevator: true if מעלית mentioned.
- renovated: true if משופץ/שיפוץ/שופץ/חדש from contractor mentioned.
- ac: true if מזגן/מיזוג mentioned.

IMPORTANT: Return null (not 0, not false) for ANY field you cannot determine with confidence. Never guess.
Do NOT confuse listing price (in ₪) with sqm.

Listings:"""


def _text_hash(listing: Listing) -> str:
    parts = [listing.description or ""]
    if listing.tags:
        parts.append(" ".join(listing.tags))
    if listing.address:
        parts.append(listing.address)
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _needs_ai(listing: Listing) -> bool:
    for field in _AI_FIELDS:
        if getattr(listing, field) is None:
            return True
    return False


def _apply_cache(listing: Listing, repo: Any) -> bool:
    try:
        old = repo.get_dict(listing.source, listing.source_id)
    except Exception:
        return False

    if old is None:
        return False

    old_desc = old.get("description", "") or ""
    if old_desc != listing.description:
        return False

    applied = False
    for field in _AI_FIELDS:
        if getattr(listing, field) is None:
            old_val = old.get(field)
            if old_val is not None:
                setattr(listing, field, old_val)
                applied = True

    return applied


def _build_batch_prompt(listings: list[Listing]) -> str:
    parts = []
    for i, listing in enumerate(listings):
        chunks = [listing.description or ""]
        if listing.tags:
            chunks.append("תגיות: " + " ".join(listing.tags))
        if listing.address:
            chunks.append("כתובת: " + listing.address)
        text = " | ".join(chunks)[:900]
        parts.append(f"[{i}] {text}")

    return _PROMPT_HEADER + "\n\n" + "\n\n".join(parts)


def _call_gemini(prompt: str, api_key: str, model: str, timeout: float) -> dict | None:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log.warning("google-genai not installed; AI mapper disabled")
        return None

    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        log.warning("Gemini client init failed: %s", e)
        return None

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                temperature=0.0,
                max_output_tokens=8192,
            ),
        )
    except TypeError:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": _RESPONSE_SCHEMA,
                    "temperature": 0.0,
                    "max_output_tokens": 8192,
                },
            )
        except Exception as e:
            log.warning("Gemini API error: %s", e)
            return None
    except Exception as e:
        log.warning("Gemini API error: %s", e)
        return None

    try:
        text = response.text
        return json.loads(text)
    except (json.JSONDecodeError, AttributeError, ValueError) as e:
        log.warning("Gemini response parse error: %s", e)
        return None


def _apply_result(listing: Listing, ai_result: dict) -> None:
    if not isinstance(ai_result, dict):
        return

    for field in _AI_FIELDS:
        if field not in ai_result:
            continue
        val = ai_result[field]
        if val is None:
            continue
        if getattr(listing, field) is not None:
            continue

        if field in _NUM_CAST:
            if isinstance(val, bool):
                continue
            try:
                val = _NUM_CAST[field](val)
            except (ValueError, TypeError):
                continue
            lo, hi = _SANITY[field]
            if val < lo or val > hi:
                continue

        setattr(listing, field, val)


def extract_batch(
    listings: list[Listing],
    cfg: Any,
    repo: Any,
) -> int:
    """Extract features from all listings via Gemini. Modifies listings in place.

    Returns number of API calls made.
    """
    if not getattr(cfg, "enabled", False):
        return 0

    api_key = os.environ.get(getattr(cfg, "api_key_env", ""), "")
    if not api_key:
        log.warning("AI mapper: %s env var not set", getattr(cfg, "api_key_env", "GEMINI_API_KEY"))
        return 0

    to_process = []
    for listing in listings:
        if not _needs_ai(listing):
            continue
        if _apply_cache(listing, repo):
            log.debug("AI cache hit: %s %s", listing.source, listing.source_id)
            continue
        to_process.append(listing)

    if not to_process:
        return 0

    batch_size = getattr(cfg, "batch_size", 20)
    model = getattr(cfg, "model", "gemini-2.0-flash")
    timeout = getattr(cfg, "timeout_sec", 20.0)
    calls = 0
    last_call = 0.0

    for i in range(0, len(to_process), batch_size):
        batch = to_process[i : i + batch_size]

        elapsed = time.time() - last_call
        if elapsed < 4.0 and calls > 0:
            time.sleep(4.0 - elapsed)

        prompt = _build_batch_prompt(batch)
        result = _call_gemini(prompt, api_key, model, timeout)
        last_call = time.time()
        calls += 1

        if result is None:
            continue

        results_list = result.get("results", [])
        if isinstance(results_list, list) and len(results_list) == len(batch):
            for listing, ai in zip(batch, results_list):
                _apply_result(listing, ai)
        else:
            log.warning(
                "AI batch result mismatch: expected %d got %s",
                len(batch),
                len(results_list) if isinstance(results_list, list) else type(results_list).__name__,
            )

    if calls:
        log.info("AI mapper: %d call(s) for %d listings", calls, len(to_process))

    return calls
