"""Parser tests for the agora.co.il freebies scraper."""

from __future__ import annotations

from pathlib import Path

from deal_hunter.freebies.agora import build_feed_url, parse_items

FIXTURE = Path(__file__).parent.parent / "fixtures" / "agora_search.html"
WATCH_LABEL = "מייבש גוש דן"


def _parsed():
    return parse_items(FIXTURE.read_text(encoding="utf-8"), watch_label=WATCH_LABEL)


def test_parses_all_rows_from_fixture():
    items = _parsed()
    assert len(items) == 10


def test_first_item_core_fields():
    items = _parsed()
    first = items[0]
    assert first.source == "agora"
    assert first.source_id == "3181573"
    assert first.title == "מייבש גז"
    assert first.city == "קרית אונו"
    assert first.condition == 2
    assert first.url == "https://www.agora.co.il/cache/2026-06/3181573_o.asp"
    assert first.image_url == "https://www.agora.co.il/showPhoto.asp?id=3181573"
    assert first.posted_at == "2026-06-17"
    assert first.watch_label == WATCH_LABEL


def test_item_without_image_has_none():
    items = {i.source_id: i for i in _parsed()}
    # Row #2 in the fixture: סביון, מיבש כביסה — no photoIcon link
    no_img = items["3177632"]
    assert no_img.image_url is None
    assert no_img.url == "https://www.agora.co.il/cache/2026-06/3177632_o.asp"


def test_different_month_buckets_resolved_per_row():
    """Each row may live in a different YYYY-MM cache bucket; URL must reflect that."""
    items = {i.source_id: i for i in _parsed()}
    # Row #3 onward come from earlier months (2026-05 etc.)
    older = items["3172826"]  # 21/5/2026
    assert older.url == "https://www.agora.co.il/cache/2026-05/3172826_o.asp"
    assert older.posted_at == "2026-05-21"


def test_all_items_have_required_fields():
    items = _parsed()
    for it in items:
        assert it.source_id.isdigit()
        assert it.title
        assert it.city
        assert it.url.startswith("https://www.agora.co.il/cache/")
        assert it.url.endswith("_o.asp")
        assert it.posted_at  # ISO date or today fallback


def test_condition_class_extracted():
    items = _parsed()
    conds = {i.condition for i in items}
    # Fixture has condition1 and condition2 rows.
    assert 2 in conds


def test_no_duplicate_source_ids():
    items = _parsed()
    ids = [i.source_id for i in items]
    assert len(ids) == len(set(ids))


# ── URL builder ────────────────────────────────────────────────────────────


def test_build_feed_url_minimum():
    url = build_feed_url(keyword="מייבש", city="גוש דן והמרכז", condition=2)
    assert url.startswith("https://www.agora.co.il/toGet.asp?")
    assert "dealType=1" in url
    assert "condition=2" in url
    # Hebrew url-encoded
    assert "iseek=" in url and "takeCity=" in url


def test_build_feed_url_with_category_and_subcategory():
    url = build_feed_url(
        keyword="מיטת קומותיים",
        city="גוש דן והמרכז",
        condition=2,
        category=2,
        subcategory=20016,
    )
    assert "category=2" in url
    assert "subcategory=20016" in url


def test_build_feed_url_omits_unset_category():
    url = build_feed_url(keyword="x", city="y", condition=2)
    assert "category=" not in url
    assert "subcategory=" not in url
