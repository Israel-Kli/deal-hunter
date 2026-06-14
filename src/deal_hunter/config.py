"""Config loader. JSON file on disk, env vars override secrets."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


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
    yad2_region: str = ""
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
    reariel: bool = False
    spectra: bool = False
    nadlanh: bool = False
    simplestate: bool = False
    komo: bool = False


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


class AIMapperCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    model: str = "gemini-2.0-flash"
    batch_size: int = 20
    timeout_sec: float = 20.0
    api_key_env: str = "GEMINI_API_KEY"


class GSheetsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    sheet_id: str = ""
    credentials_path: str = "credentials.json"
    tab_name: str = "Deal Hunter-2026"
    disappeared_cutoff_minutes: int | None = None
    audit_max_entries: int = 20


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: SearchCfg = Field(default_factory=SearchCfg)
    cities: list[CityCfg] = Field(default_factory=list)
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    comps: CompsCfg = Field(default_factory=CompsCfg)
    schedule: ScheduleCfg = Field(default_factory=ScheduleCfg)
    notifications: NotificationsCfg = Field(default_factory=NotificationsCfg)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    ai_mapper: AIMapperCfg = Field(default_factory=AIMapperCfg)
    gsheets: GSheetsCfg = Field(default_factory=GSheetsCfg)
    dashboard_port: int = 8081
    dashboard_host: str = "127.0.0.1"
    data_dir: str = "data"
    onmap_cities: list[str] = Field(default_factory=list)
    ad_city_paths: list[str] = Field(default_factory=list)


def _env_override(cfg: dict[str, Any]) -> dict[str, Any]:
    n = cfg.setdefault("notifications", {})
    if tok := os.environ.get("TELEGRAM_BOT_TOKEN"):
        n["telegram_bot_token"] = tok
        log.debug("TELEGRAM_BOT_TOKEN overridden from env")
    if cid := os.environ.get("TELEGRAM_CHAT_ID"):
        n["telegram_chat_id"] = cid
        log.debug("TELEGRAM_CHAT_ID overridden from env")
    g = cfg.setdefault("gsheets", {})
    if sid := os.environ.get("GSHEET_ID"):
        g["sheet_id"] = sid
        log.debug("GSHEET_ID overridden from env")
    if gc := os.environ.get("GSHEET_CREDENTIALS"):
        g["credentials_path"] = gc
        log.debug("GSHEET_CREDENTIALS overridden from env")
    return cfg


def load(path: str | Path = "configs/config.json") -> Config:
    p = Path(path)
    log.info("Loading config: %s", p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    return Config.model_validate(_env_override(raw))
