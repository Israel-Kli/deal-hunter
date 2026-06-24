"""Data model for freebies — small parallel pipeline alongside the real-estate listings."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


FreebieSource = Literal["agora"]


class FreebieItem(BaseModel):
    """A single free-item ad scraped from a freebies board."""

    model_config = ConfigDict(extra="forbid")

    source: FreebieSource
    source_id: str
    watch_label: str

    title: str
    city: str = ""
    condition: int | None = None

    url: str
    image_url: str | None = None

    posted_at: str = ""
    first_seen_at: datetime | None = None
