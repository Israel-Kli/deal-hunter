"""Shared HTTP client for all adapters. curl_cffi impersonates a real browser TLS fingerprint."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from curl_cffi import requests as curl_requests

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}

# Per-host Cookie injection. Some sources (e.g. Yad2) sit behind a JS/WAF bot
# challenge (Radware) that a plain HTTP client cannot pass. Workaround: solve the
# challenge once in a real browser, copy the full Cookie request header, and put
# it in the matching env var below. It is attached only to requests for that host
# so cookies never leak cross-site.
_COOKIE_ENV_BY_HOST = {
    "yad2.co.il": "YAD2_COOKIES",
}

_URL_TRUNCATE = 150


def _shorten(url: str) -> str:
    return url if len(url) <= _URL_TRUNCATE else url[:_URL_TRUNCATE] + "..."


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
    short = _shorten(url)

    for host, env_name in _COOKIE_ENV_BY_HOST.items():
        if host in url:
            cookie = os.environ.get(env_name)
            if cookie:
                merged.setdefault("Cookie", cookie.strip())
                log.debug("fetch: attached %s cookie (%d chars) for %s", env_name, len(cookie), host)
            break

    proxy_url = (
        os.environ.get("HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("https_proxy")
    )
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    for attempt in range(retries):
        try:
            r = curl_requests.get(
                url,
                headers=merged,
                timeout=20,
                impersonate=impersonate,
                proxies=proxies,
            )
            if r.status_code == 200:
                body = r.text if not as_json else "(json)"
                log.debug("fetch OK: %s -> %d (%s bytes) %s", short, r.status_code, len(r.content), body[:80] if isinstance(body, str) else body)
                return r.json() if as_json else r.text
            log.warning("fetch %s -> %s", short, r.status_code)
            if r.status_code in (403, 429):
                time.sleep(backoff_sec * (attempt + 1))
        except Exception as e:
            last_err = e
            log.warning("fetch %s attempt %d error: %s", short, attempt + 1, e)
            time.sleep(backoff_sec * (attempt + 1))
    if last_err:
        log.error("fetch %s giving up: %s", short, last_err)
    return None
