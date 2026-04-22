"""Canonical data models shared across the pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["yad2", "madlan", "onmap", "ad"]


class Listing(BaseModel):
    """Normalized listing from any source. Source-specific extras live in `source_payload`."""

    model_config = ConfigDict(extra="forbid")

    source: Source
    source_id: str
    url: str

    city: str = ""
    neighborhood: str = ""
    street: str = ""
    house_number: str = ""
    address: str = ""

    rooms: float | None = None
    sqm: int | None = None
    sqm_build: int | None = None
    floor: int | None = None

    price: int
    price_before: int | None = None
    price_per_sqm: int | None = None

    listing_type: str = ""
    is_agent: bool = False

    parking: bool = False
    elevator: bool = False
    balcony: bool = False
    ac: bool = False
    mamad: bool = False
    renovated: bool = False

    description: str = ""
    images: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    lat: float | None = None
    lon: float | None = None

    publish_date: str = ""
    first_listed_date: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    canonical_id: str | None = None

    fair_price_estimate: int | None = None
    fair_price_low: int | None = None
    fair_price_high: int | None = None

    score: float | None = None
    score_reasons: dict[str, Any] = Field(default_factory=dict)

    source_payload: dict[str, Any] = Field(default_factory=dict)

    is_favorite: bool = False
    user_notes: str = ""


class Comp(BaseModel):
    """A single closed-deal / comparable sale."""

    model_config = ConfigDict(extra="forbid")

    source: str
    address: str
    city: str = ""
    neighborhood: str = ""
    street: str = ""
    house_number: str = ""
    deal_date: str
    price: int
    sqm: int | None = None
    rooms: float | None = None
    year_built: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ScanResult(BaseModel):
    """One run summary per adapter."""

    source: Source
    fetched: int = 0
    new: int = 0
    updated: int = 0
    price_drops: int = 0
    alerted: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_sec: float = 0.0
