"""Tests for effective-value helpers."""

from __future__ import annotations

from deal_hunter.effective import (
    effective_garden_sqm,
    effective_lot_sqm,
    effective_price_per_sqm,
    effective_sqm,
    effective_sqm_build,
    effective_units,
)


def test_sqm_prefers_user_override():
    d = {"sqm": 100, "sqm_user": 120}
    assert effective_sqm(d) == 120


def test_sqm_falls_back_to_scraped():
    d = {"sqm": 100, "sqm_user": None}
    assert effective_sqm(d) == 100


def test_sqm_null_when_both_none():
    d = {"sqm": None, "sqm_user": None}
    assert effective_sqm(d) is None


def test_sqm_build_prefers_user():
    d = {"sqm_build": 80, "sqm_build_user": 90}
    assert effective_sqm_build(d) == 90


def test_sqm_build_fallthrough():
    d = {"sqm_build": 80, "sqm_build_user": None}
    assert effective_sqm_build(d) == 80


def test_units_prefers_user():
    d = {"units_count": 2, "units_count_user": 4}
    assert effective_units(d) == 4


def test_units_fallthrough():
    d = {"units_count": 2, "units_count_user": None}
    assert effective_units(d) == 2


def test_lot_sqm_prefers_user():
    d = {"lot_sqm": 200, "lot_sqm_user": 250}
    assert effective_lot_sqm(d) == 250


def test_garden_sqm_prefers_user():
    d = {"garden_sqm": 50, "garden_sqm_user": 80}
    assert effective_garden_sqm(d) == 80


def test_price_per_sqm_derived_from_effective_sqm():
    d = {"price": 1_000_000, "sqm": 100, "sqm_user": 80}
    assert effective_price_per_sqm(d) == 12500  # 1M / 80


def test_price_per_sqm_falls_back_to_stored():
    d = {"price": 1_000_000, "sqm": None, "sqm_user": None, "price_per_sqm": 10000}
    assert effective_price_per_sqm(d) == 10000


def test_price_per_sqm_none_when_no_data():
    assert effective_price_per_sqm({"price": 1_000_000}) is None
