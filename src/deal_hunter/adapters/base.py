"""ScraperAdapter protocol. All source adapters implement this."""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from deal_hunter.models import Listing, Source


class SearchFilters(dict):
    """Filters passed to each adapter. Keys honored per-source; unknown keys ignored."""


@runtime_checkable
class ScraperAdapter(Protocol):
    """Fetches listings from a single source and normalizes them to the canonical Listing."""

    source: Source

    def fetch_feed(self, filters: SearchFilters) -> Iterable[Listing]:
        """Yield partial Listing objects from the source's search feed."""

    def fetch_detail(self, listing: Listing) -> Listing:
        """Return the listing enriched with detail-page fields (amenities, description, etc.)."""
