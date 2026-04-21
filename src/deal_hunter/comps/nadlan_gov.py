"""nadlan.gov.il CompsProvider.

Fetches pre-aggregated closed-deal price statistics from the Israeli Tax
Authority real-estate data portal (data.nadlan.gov.il).

Two-step flow
─────────────
1. ``/deal-info`` POST  →  resolve a free-text address to internal IDs
   (neigh_id, setl_id, addr_id …).
   Endpoint: https://api.nadlan.gov.il/deal-info
   No auth / recaptcha required.

2. Static JSON GET  →  neighbourhood/settlement buy-trends page.
   URL pattern:
     https://data.nadlan.gov.il/api/pages/neighborhood/buy/{neigh_id}.json
     https://data.nadlan.gov.il/api/pages/settlement/buy/{setl_id}.json

   Returns quarterly median prices broken down by room count.
   These files are publicly accessible without any token.

Address resolution
──────────────────
The govmap TldSearch autocomplete maps a Hebrew free-text query to an
``addr_id``, which is then passed to ``/deal-info`` to get the internal
neighbourhood and settlement IDs.

Autocomplete:
  GET https://es.govmap.gov.il/TldSearch/api/AutoComplete
      ?query=<address>&ids=276267023&gid=govmap

What we produce
───────────────
The trends JSON does NOT contain individual closed-deal rows — it contains
quarterly median prices per room-count bucket.  We synthesise one ``Comp``
per (room_count, quarter) data point with:
  - ``price``    = neighbourhood median price for that quarter
  - ``deal_date``= "MM/YYYY" representation of the quarter start month
  - ``sqm``      = None  (not available at this aggregation level)
  - ``source``   = "nadlan_gov"
  - ``raw``      includes ``settlement_price`` and ``country_price`` for context

This is sufficient to drive ``valuation/fair_price.py`` which computes a
weighted median from the comps table.

Caching
───────
Address resolution results are cached in memory for the lifetime of the
provider instance so that multiple listings in the same building/neighbourhood
do not trigger repeated HTTP round-trips.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from deal_hunter.models import Comp

log = logging.getLogger(__name__)

_AUTOCOMPLETE_URL = (
    "https://es.govmap.gov.il/TldSearch/api/AutoComplete"
    "?query={query}&ids=276267023&gid=govmap"
)
_DEAL_INFO_URL = "https://api.nadlan.gov.il/deal-info"
_PAGES_BASE = "https://data.nadlan.gov.il/api/pages"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nadlan.gov.il/",
    "Accept": "application/json, text/plain, */*",
}


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, *, timeout: int = 10) -> dict | list | None:
    """GET a JSON/BOM-JSON URL. Returns parsed object or None on failure."""
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        log.debug("nadlan GET %s → %s", url, exc)
        return None
    # Strip UTF-8 BOM that the S3 bucket includes
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.debug("nadlan GET %s JSON parse error: %s", url, exc)
        return None


def _post(url: str, payload: dict, *, timeout: int = 10) -> dict | None:
    """POST JSON payload, return parsed response dict or None."""
    data = json.dumps(payload).encode()
    headers = {**_HEADERS, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.debug("nadlan POST %s → %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Address resolution
# ---------------------------------------------------------------------------

def _autocomplete_addr_id(query: str) -> str | None:
    """Return the first ADDRESS Key from govmap autocomplete, or None."""
    url = _AUTOCOMPLETE_URL.format(query=urllib.parse.quote(query))
    data = _get(url)
    if not isinstance(data, dict):
        return None
    addresses = data.get("res", {}).get("ADDRESS", [])
    if not addresses:
        return None
    return str(addresses[0]["Key"])


def _deal_info(addr_id: str) -> dict | None:
    """POST to /deal-info → {neigh_id, setl_id, …}. No auth needed."""
    result = _post(_DEAL_INFO_URL, {"base_name": "addr_id", "base_id": addr_id})
    if not isinstance(result, dict):
        return None
    if "neigh_id" not in result and "setl_id" not in result:
        return None
    return result


def resolve_ids(city: str, street: str, house_number: str = "") -> dict | None:
    """Resolve a Hebrew address to nadlan internal IDs.

    Returns a dict with at least one of ``neigh_id`` / ``setl_id``, or None.
    """
    query = f"{street} {house_number} {city}".strip()
    addr_id = _autocomplete_addr_id(query)
    if not addr_id:
        # Retry without house number
        query = f"{street} {city}".strip()
        addr_id = _autocomplete_addr_id(query)
    if not addr_id:
        log.debug("nadlan: no addr_id for '%s'", query)
        return None
    time.sleep(0.3)
    info = _deal_info(addr_id)
    if not info:
        log.debug("nadlan: deal-info returned nothing for addr_id=%s", addr_id)
    return info


# ---------------------------------------------------------------------------
# Trend-page fetching and Comp synthesis
# ---------------------------------------------------------------------------

def _quarter_month(year: int, month: int) -> str:
    """Convert year/month to 'MM/YYYY' deal_date string."""
    return f"{month:02d}/{year}"


def _fetch_trends(level: str, level_id: str) -> list[dict] | None:
    """Fetch the buy-trends JSON for a neighbourhood or settlement.

    Returns the ``trends.rooms`` list or None.
    """
    url = f"{_PAGES_BASE}/{level}/buy/{level_id}.json"
    data = _get(url)
    if not isinstance(data, dict):
        return None
    rooms: list = data.get("trends", {}).get("rooms", [])
    return rooms if rooms else None


def _rooms_to_comps(
    rooms_data: list[dict],
    *,
    city: str,
    neighborhood: str,
    source_level: str,
    window_months: int,
) -> list[Comp]:
    """Convert nadlan trend rooms data into synthetic Comp objects.

    Each (room_count × quarter) data point with a non-null neighbourhoodPrice
    becomes one Comp.  Points older than ``window_months`` are dropped.
    """
    cutoff = datetime.now()
    cutoff_months = cutoff.year * 12 + cutoff.month - window_months

    comps: list[Comp] = []
    for bucket in rooms_data:
        num_rooms = bucket.get("numRooms")
        if num_rooms == "all":
            num_rooms_float: float | None = None
        else:
            try:
                num_rooms_float = float(num_rooms)
            except (TypeError, ValueError):
                num_rooms_float = None

        for point in bucket.get("graphData", []):
            price_val = point.get("neighborhoodPrice")
            if not price_val:
                continue
            year = point.get("year")
            month = point.get("month")
            if not year or not month:
                continue
            # Window filter
            point_months = year * 12 + month
            if point_months < cutoff_months:
                continue

            deal_date = _quarter_month(year, month)
            raw_extra: dict[str, Any] = {
                "source_level": source_level,
                "settlement_price": point.get("settlementPrice"),
                "country_price": point.get("countryPrice"),
            }
            summary = bucket.get("summary", {})
            if summary:
                raw_extra["summary"] = summary

            comps.append(
                Comp(
                    source="nadlan_gov",
                    address=f"{neighborhood}, {city}",
                    city=city,
                    neighborhood=neighborhood,
                    street="",
                    house_number="",
                    deal_date=deal_date,
                    price=int(price_val),
                    sqm=None,
                    rooms=num_rooms_float,
                    year_built=None,
                    raw=raw_extra,
                )
            )

    return comps


# ---------------------------------------------------------------------------
# CompsProvider
# ---------------------------------------------------------------------------

class NadlanGovProvider:
    """CompsProvider backed by data.nadlan.gov.il public trend JSON files.

    Implements the CompsProvider protocol (see ``comps/base.py``).

    Unlike ``Yad2DealsProvider``, this provider resolves addresses to
    nadlan internal IDs and fetches aggregated quarterly median prices —
    so it works without any per-listing HTML and can answer
    ``comps_for(city, neighborhood, street, rooms, sqm)`` directly.

    Caching: ``resolve_ids`` results are memoised per (city, street,
    house_number) key for the provider's lifetime.
    """

    name = "nadlan_gov"

    def __init__(self) -> None:
        self._id_cache: dict[str, dict | None] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, city: str, street: str, house_number: str) -> dict | None:
        cache_key = f"{city}|{street}|{house_number}".lower()
        if cache_key not in self._id_cache:
            self._id_cache[cache_key] = resolve_ids(city, street, house_number)
        return self._id_cache[cache_key]

    def _fetch_comps_for_ids(
        self,
        ids: dict,
        *,
        city: str,
        neighborhood: str,
        window_months: int,
    ) -> list[Comp]:
        """Try neighbourhood first, fall back to settlement."""
        neigh_id = ids.get("neigh_id")
        setl_id = ids.get("setl_id")
        neigh_name = ids.get("neigh_name", neighborhood)

        # Neighbourhood level (more granular)
        if neigh_id:
            time.sleep(0.3)
            rooms_data = _fetch_trends("neighborhood", str(neigh_id))
            if rooms_data:
                return _rooms_to_comps(
                    rooms_data,
                    city=city,
                    neighborhood=neigh_name or neighborhood,
                    source_level="neighborhood",
                    window_months=window_months,
                )

        # Settlement fallback
        if setl_id:
            time.sleep(0.3)
            rooms_data = _fetch_trends("settlement", str(setl_id))
            if rooms_data:
                return _rooms_to_comps(
                    rooms_data,
                    city=city,
                    neighborhood=neighborhood,
                    source_level="settlement",
                    window_months=window_months,
                )

        return []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def comps_for(
        self,
        *,
        city: str,
        neighborhood: str,
        street: str,
        rooms: float | None,
        sqm: int | None,
        window_months: int = 18,
    ) -> list[Comp]:
        """Return synthetic median-price comps for the given location.

        Optionally filters to only the requested ``rooms`` bucket (plus the
        "all" bucket).  If ``rooms`` is None, all buckets are returned.
        """
        ids = self._resolve(city, street, "")
        if not ids:
            log.info(
                "nadlan_gov: could not resolve ids for city=%s street=%s", city, street
            )
            return []

        all_comps = self._fetch_comps_for_ids(
            ids,
            city=city,
            neighborhood=neighborhood or ids.get("neigh_name", ""),
            window_months=window_months,
        )

        if not all_comps:
            return []

        # Filter by room count when requested
        if rooms is not None:
            all_comps = [
                c for c in all_comps
                if c.rooms is None or c.rooms == rooms
            ]

        log.info(
            "nadlan_gov: %d comps for %s / %s (rooms=%s, window=%dm)",
            len(all_comps), city, street, rooms, window_months,
        )
        return all_comps

    def comps_for_neighborhood(
        self,
        *,
        city: str,
        neigh_id: str,
        neighborhood_name: str = "",
        window_months: int = 18,
    ) -> list[Comp]:
        """Convenience method when the nadlan neigh_id is already known."""
        time.sleep(0.3)
        rooms_data = _fetch_trends("neighborhood", neigh_id)
        if not rooms_data:
            return []
        return _rooms_to_comps(
            rooms_data,
            city=city,
            neighborhood=neighborhood_name,
            source_level="neighborhood",
            window_months=window_months,
        )
