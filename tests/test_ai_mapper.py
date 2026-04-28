"""Tests for the AI-based feature extraction (Gemini mapper).

Reuses GEMINI_API_KEY from the environment, just like the production code
in ai_mapper.py reads it via os.environ.get(cfg.api_key_env).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from deal_hunter.ai_mapper import (
    _apply_cache,
    _apply_result,
    _build_batch_prompt,
    _call_gemini,
    _needs_ai,
    _text_hash,
    extract_batch,
)
from deal_hunter.models import Listing


def _listing(desc: str = "", **kw) -> Listing:
    base = dict(
        source="yad2",
        source_id="ai1",
        url="https://example.com/ai1",
        price=1_000_000,
    )
    base.update(kw)
    base["description"] = desc
    return Listing(**base)


# ── Unit tests (no API key needed) ──────────────────────────────


def test_text_hash_deterministic():
    l = _listing("דירת 5 חדרים, מרפסת")
    h1 = _text_hash(l)
    h2 = _text_hash(l)
    assert h1 == h2
    assert len(h1) == 16


def test_text_hash_changes_on_diff_desc():
    l1 = _listing("דירת 5 חדרים")
    l2 = _listing("דירת 4 חדרים")
    assert _text_hash(l1) != _text_hash(l2)


def test_needs_ai_all_none():
    l = _listing("דירה")
    assert _needs_ai(l) is True


def test_needs_ai_partial():
    l = _listing("דירה", rooms=3.0, sqm=80, floor=2)
    assert _needs_ai(l) is True


def test_needs_ai_complete():
    l = _listing("דירה",
        rooms=3.0, sqm=80, sqm_build=90, floor=2,
        units_count=1, garden_sqm=50, lot_sqm=200,
        parking=True, balcony=True, elevator=False,
        renovated=True, ac=True)
    assert _needs_ai(l) is False


def test_apply_result_fills_none():
    l = _listing("דירה")
    ai = {"rooms": 4.0, "sqm": 100, "floor": 3}
    _apply_result(l, ai)
    assert l.rooms == 4.0
    assert l.sqm == 100
    assert l.floor == 3


def test_apply_result_does_not_override():
    l = _listing("דירה", rooms=3.0)
    ai = {"rooms": 4.0}
    _apply_result(l, ai)
    assert l.rooms == 3.0


def test_apply_result_sanity_bounds():
    l = _listing("דירה")
    _apply_result(l, {"rooms": 999.0})
    assert l.rooms is None


def test_apply_result_ignores_null():
    l = _listing("דירה")
    _apply_result(l, {"rooms": None, "sqm": None})
    assert l.rooms is None
    assert l.sqm is None


def test_apply_result_units_count():
    l = _listing("דירה")
    _apply_result(l, {"units_count": 3})
    assert l.units_count == 3


def test_apply_result_bool_not_numeric():
    l = _listing("דירה")
    _apply_result(l, {"rooms": False})
    assert l.rooms is None


def test_build_batch_prompt_contains_descriptions():
    listings = [_listing("דירת 5 חדרים"), _listing("דירת גן 80 מ״ר")]
    prompt = _build_batch_prompt(listings)
    assert "דירת 5 חדרים" in prompt
    assert "דירת גן 80 מ״ר" in prompt


def test_build_batch_prompt_includes_indices():
    l = _listing("דירה")
    prompt = _build_batch_prompt([l])
    assert "[0]" in prompt


def test_apply_cache_hit():
    repo = MagicMock()
    repo.get_dict.return_value = {"description": "test desc", "rooms": 4.0, "sqm": 100}
    l = _listing("test desc", rooms=None, sqm=None)
    result = _apply_cache(l, repo)
    assert result is True
    assert l.rooms == 4.0
    assert l.sqm == 100


def test_apply_cache_miss():
    repo = MagicMock()
    repo.get_dict.return_value = None
    l = _listing("test desc")
    result = _apply_cache(l, repo)
    assert result is False


def test_apply_cache_stale_desc():
    repo = MagicMock()
    repo.get_dict.return_value = {"description": "old desc", "rooms": 4.0}
    l = _listing("new desc")
    result = _apply_cache(l, repo)
    assert result is False
    assert l.rooms is None


# ── Tests reusing GEMINI_API_KEY from env (like the code does) ──


def test_extract_batch_disabled_when_cfg_off():
    cfg = MagicMock()
    cfg.enabled = False
    calls = extract_batch([_listing("דירה")], cfg, MagicMock())
    assert calls == 0


def test_extract_batch_noop_without_api_key():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.api_key_env = "GEMINI_API_KEY"
    cfg.batch_size = 20
    cfg.model = "gemini-2.0-flash"
    cfg.timeout_sec = 20.0
    with patch.dict(os.environ, {}, clear=True):
        calls = extract_batch([_listing("דירה")], cfg, MagicMock())
    assert calls == 0


def test_call_gemini_missing_import():
    with patch.dict("sys.modules", {"google": None}):
        result = _call_gemini("test", "fake-key", "gemini-2.0-flash", 10.0)
    assert result is None


def test_call_gemini_with_env_key():
    """Integration check: passes if GEMINI_API_KEY is set and Gemini responds."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return

    try:
        from google import genai
    except ImportError:
        return

    result = _call_gemini(
        'Return JSON: {"results": [{"rooms": 3.5}]}',
        api_key,
        "gemini-2.0-flash",
        30.0,
    )
    assert result is not None
    results = result.get("results", [])
    assert len(results) == 1
    assert results[0].get("rooms") == 3.5


def test_extract_batch_integration():
    """End-to-end test using GEMINI_API_KEY from env, same as production code."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return

    try:
        from google import genai
    except ImportError:
        return

    cfg = MagicMock()
    cfg.enabled = True
    cfg.api_key_env = "GEMINI_API_KEY"
    cfg.batch_size = 20
    cfg.model = "gemini-2.0-flash"
    cfg.timeout_sec = 30.0

    repo = MagicMock()
    repo.get_dict.return_value = None

    listings = [
        _listing("דירת 5 חדרים, 100 מ״ר, קומה 3, חניה, מרפסת, מעלית, משופץ"),
        _listing("דירת גן 3 חדרים, 70 מ״ר, גינה 50 מ״ר, יחידת דיור"),
    ]

    calls = extract_batch(listings, cfg, repo)
    assert calls >= 1

    assert listings[0].rooms is not None
    assert listings[1].sqm is not None
