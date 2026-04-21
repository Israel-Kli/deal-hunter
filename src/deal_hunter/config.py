"""Config loader. JSON file on disk, env vars override secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rooms_min: float = 2.5
    rooms_max: float = 5.0
    price_min: int = 1_500_000
    price_max: int = 10_000_000
    min_sqm: int = 55
    max_listing_age_days: int = 30
    exclude_ground_floor: bool = False
    property_types: list[str] = Field(default_factory=list)


class CityCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    city_code: str
    slug: str = "tel-aviv-area"
    yad2_id: str = ""
    onmap_slug: str = ""
    hebrew_name: str = ""
    filter_areas: bool = False
    area_keywords: list[str] = Field(default_factory=list)


class SourcesCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yad2: bool = True
    madlan: bool = False
    onmap: bool = False
    ad: bool = False


class CompsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yad2_deals: bool = True
    nadlan_gov: bool = False


class ScheduleCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_interval_minutes: int = 60
    delay_between_requests_sec: float = 3.0
    max_pages: int = 10


class NotificationsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    score_threshold: float = 7.0
    price_drop_pct: float = 3.0


class ScoringCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_threshold: float = 7.0


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: SearchCfg = Field(default_factory=SearchCfg)
    cities: list[CityCfg] = Field(default_factory=list)
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    comps: CompsCfg = Field(default_factory=CompsCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    notifications: NotificationsCfg = Field(default_factory=NotificationsCfg)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    dashboard_port: int = 8081
    dashboard_host: str = "127.0.0.1"
    data_dir: str = "data"
    onmap_cities: list[str] = Field(default_factory=list)
    ad_city_paths: list[str] = Field(default_factory=list)


def _env_override(cfg: dict[str, Any]) -> dict[str, Any]:
    n = cfg.setdefault("notifications", {})
    if tok := os.environ.get("TELEGRAM_BOT_TOKEN"):
        n["telegram_bot_token"] = tok
    if cid := os.environ.get("TELEGRAM_CHAT_ID"):
        n["telegram_chat_id"] = cid
    return cfg


def load(path: str | Path = "configs/config.json") -> Config:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    return Config.model_validate(_env_override(raw))
