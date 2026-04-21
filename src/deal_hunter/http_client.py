"""Shared HTTP client for all adapters. curl_cffi impersonates a real browser TLS fingerprint."""

from __future__ import annotations

import logging
import time
from typing import Any

from curl_cffi import requests as curl_requests

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}


def fetch(
    url: str,
    *,
    as_json: bool = True,
    retries: int = 3,
    backoff_sec: float = 2.0,
    headers: dict[str, str] | None = None,
    impersonate: str = "chrome120",
) -> Any | None:
    """GET `url` with browser-TLS impersonation. Returns parsed JSON dict or raw text. None on persistent failure."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = curl_requests.get(url, headers=merged, timeout=20, impersonate=impersonate)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            log.warning("fetch %s -> %s", url, r.status_code)
            if r.status_code in (403, 429):
                time.sleep(backoff_sec * (attempt + 1))
        except Exception as e:
            last_err = e
            log.warning("fetch %s attempt %d error: %s", url, attempt + 1, e)
            time.sleep(backoff_sec * (attempt + 1))
    if last_err:
        log.error("fetch %s giving up: %s", url, last_err)
    return None
