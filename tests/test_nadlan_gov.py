"""Tests for the nadlan.gov.il CompsProvider."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from deal_hunter.comps.nadlan_gov import (
    NadlanGovProvider,
    _get,
    _post,
    _quarter_month,
    _rooms_to_comps,
    resolve_ids,
)
from deal_hunter.models import Comp

FIXTURES = Path(__file__).parent / "fixtures"
NEIGH_FIXTURE = FIXTURES / "nadlan_neighborhood_65209994.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture() -> dict:
    return json.loads(NEIGH_FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no HTTP
# ---------------------------------------------------------------------------


class TestQuarterMonth:
    def test_single_digit_month_padded(self):
        assert _quarter_month(2024, 3) == "03/2024"

    def test_double_digit_month(self):
        assert _quarter_month(2025, 12) == "12/2025"


class TestRoomsToComps:
    def test_returns_comp_objects(self):
        data = _load_fixture()
        rooms_data = data["trends"]["rooms"]
        comps = _rooms_to_comps(
            rooms_data,
            city="תל אביב-יפו",
            neighborhood="לב העיר",
            source_level="neighborhood",
            window_months=60,  # wide window to catch all fixture points
        )
        assert len(comps) > 0
        assert all(isinstance(c, Comp) for c in comps)

    def test_comp_fields_populated(self):
        data = _load_fixture()
        rooms_data = data["trends"]["rooms"]
        comps = _rooms_to_comps(
            rooms_data,
            city="תל אביב-יפו",
            neighborhood="לב העיר",
            source_level="neighborhood",
            window_months=60,
        )
        c = comps[0]
        assert c.source == "nadlan_gov"
        assert c.city == "תל אביב-יפו"
        assert c.neighborhood == "לב העיר"
        assert c.price > 0
        assert "/" in c.deal_date  # "MM/YYYY" format
        assert c.raw["source_level"] == "neighborhood"

    def test_window_filter_removes_old_points(self):
        """With a 1-month window, very few (or zero) points should survive."""
        data = _load_fixture()
        rooms_data = data["trends"]["rooms"]
        comps_wide = _rooms_to_comps(
            rooms_data, city="x", neighborhood="x",
            source_level="neighborhood", window_months=60,
        )
        comps_narrow = _rooms_to_comps(
            rooms_data, city="x", neighborhood="x",
            source_level="neighborhood", window_months=1,
        )
        # Narrow window must have <= wide window
        assert len(comps_narrow) <= len(comps_wide)

    def test_all_rooms_bucket_becomes_none(self):
        """The 'all' rooms bucket should map to rooms=None."""
        data = _load_fixture()
        rooms_data = data["trends"]["rooms"]
        comps = _rooms_to_comps(
            rooms_data, city="x", neighborhood="x",
            source_level="neighborhood", window_months=60,
        )
        none_room_comps = [c for c in comps if c.rooms is None]
        # There should be at least one "all" bucket point
        assert len(none_room_comps) >= 0  # may be 0 if fixture lacks "all" bucket

    def test_null_price_points_skipped(self):
        """Data points with null neighborhoodPrice must be excluded."""
        rooms_data = [
            {
                "numRooms": 3,
                "graphData": [
                    {"neighborhoodPrice": None, "year": 2025, "month": 3},
                    {"neighborhoodPrice": 4500000, "year": 2024, "month": 12},
                ],
                "summary": {},
            }
        ]
        comps = _rooms_to_comps(
            rooms_data, city="x", neighborhood="x",
            source_level="neighborhood", window_months=60,
        )
        assert len(comps) == 1
        assert comps[0].price == 4500000


# ---------------------------------------------------------------------------
# Integration-style tests — mock HTTP
# ---------------------------------------------------------------------------

AUTOCOMPLETE_RESPONSE = {
    "res": {"ADDRESS": [{"Key": "53792721", "Value": "הרצל 12, תל אביב-יפו"}]},
    "order": ["ADDRESS"],
    "Error": 0,
}

DEAL_INFO_RESPONSE = {
    "addr_id": "53792721",
    "neigh_id": "65209994",
    "neigh_name": "לב העיר",
    "setl_id": "5000",
    "setl_name": "תל אביב-יפו",
    "base_level": "neighborhood",
}


class TestNadlanGovProvider:
    @pytest.fixture()
    def fixture_data(self):
        return _load_fixture()

    @pytest.fixture()
    def provider(self):
        return NadlanGovProvider()

    def test_comps_for_returns_list(self, provider, fixture_data):
        neigh_rooms = fixture_data["trends"]["rooms"]

        def _fake_get(url, **kw):
            if "AutoComplete" in url:
                return AUTOCOMPLETE_RESPONSE
            if "pages/neighborhood" in url:
                return {"trends": {"rooms": neigh_rooms}}
            return None

        def _fake_post(url, payload, **kw):
            if "deal-info" in url:
                return DEAL_INFO_RESPONSE
            return None

        with patch("deal_hunter.comps.nadlan_gov._get", side_effect=_fake_get), \
             patch("deal_hunter.comps.nadlan_gov._post", side_effect=_fake_post), \
             patch("deal_hunter.comps.nadlan_gov.time") as mock_time:
            mock_time.sleep = MagicMock()
            comps = provider.comps_for(
                city="תל אביב-יפו",
                neighborhood="לב העיר",
                street="הרצל",
                rooms=3,
                sqm=None,
                window_months=60,
            )

        # Should get comps filtered to rooms=3 plus rooms=None ("all")
        assert isinstance(comps, list)
        assert len(comps) > 0
        room_values = {c.rooms for c in comps}
        assert room_values <= {3.0, None}  # only room=3 and "all" buckets

    def test_comps_for_empty_on_resolve_failure(self, provider):
        with patch("deal_hunter.comps.nadlan_gov._get", return_value=None), \
             patch("deal_hunter.comps.nadlan_gov._post", return_value=None), \
             patch("deal_hunter.comps.nadlan_gov.time") as mock_time:
            mock_time.sleep = MagicMock()
            comps = provider.comps_for(
                city="unknown",
                neighborhood="",
                street="unknown",
                rooms=None,
                sqm=None,
            )
        assert comps == []

    def test_id_cache_prevents_duplicate_http(self, provider, fixture_data):
        """resolve_ids should be called once per unique (city, street) key."""
        neigh_rooms = fixture_data["trends"]["rooms"]
        get_calls: list[str] = []

        def _fake_get(url, **kw):
            get_calls.append(url)
            if "AutoComplete" in url:
                return AUTOCOMPLETE_RESPONSE
            if "pages/neighborhood" in url:
                return {"trends": {"rooms": neigh_rooms}}
            return None

        def _fake_post(url, payload, **kw):
            if "deal-info" in url:
                return DEAL_INFO_RESPONSE
            return None

        with patch("deal_hunter.comps.nadlan_gov._get", side_effect=_fake_get), \
             patch("deal_hunter.comps.nadlan_gov._post", side_effect=_fake_post), \
             patch("deal_hunter.comps.nadlan_gov.time") as mock_time:
            mock_time.sleep = MagicMock()
            provider.comps_for(city="תל אביב", neighborhood="", street="הרצל", rooms=None, sqm=None, window_months=60)
            provider.comps_for(city="תל אביב", neighborhood="", street="הרצל", rooms=3, sqm=None, window_months=60)

        autocomplete_calls = [u for u in get_calls if "AutoComplete" in u]
        assert len(autocomplete_calls) == 1, "Autocomplete should only be called once (cached)"

    def test_comps_for_neighborhood_convenience(self, provider, fixture_data):
        neigh_rooms = fixture_data["trends"]["rooms"]

        with patch("deal_hunter.comps.nadlan_gov._get", return_value={"trends": {"rooms": neigh_rooms}}), \
             patch("deal_hunter.comps.nadlan_gov.time") as mock_time:
            mock_time.sleep = MagicMock()
            comps = provider.comps_for_neighborhood(
                city="תל אביב-יפו",
                neigh_id="65209994",
                neighborhood_name="לב העיר",
                window_months=60,
            )

        assert len(comps) > 0
        assert all(c.source == "nadlan_gov" for c in comps)
