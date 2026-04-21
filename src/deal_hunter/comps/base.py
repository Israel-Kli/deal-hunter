"""CompsProvider protocol. Closed-deal sources (Yad2 Deals, nadlan.gov.il) implement this."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from deal_hunter.models import Comp


@runtime_checkable
class CompsProvider(Protocol):
    """Returns comparable closed deals for a given address/property."""

    name: str

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
        """Return closed-deal comps matching the query. Empty list on miss."""
