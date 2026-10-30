"""Facebook group scraper for the freebies pipeline.

Facebook has no public API for groups, so this drives headless Chromium with the
cookies of a group-member account and reads the Relay GraphQL responses the feed
page fires while scrolling. Posts are objects with ``post_id``, ``message.text``,
``wwwURL`` and ``actors``; reading the JSON avoids the DOM, which virtualizes
off-screen posts and hides permalinks until hover.

The cookie header comes from the ``FACEBOOK_COOKIES`` env var (never the repo).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

log = logging.getLogger(__name__)

GROUP_URL = "https://www.facebook.com/groups/{group_id}?sorting_setting=CHRONOLOGICAL"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_BLOCKED_RESOURCES = {"image", "media", "font"}
_AUTH_URL_MARKERS = ("/login", "/checkpoint")


class FacebookAuthError(RuntimeError):
    """The cookies no longer give a logged-in session."""


@dataclass(frozen=True)
class FbPost:
    post_id: str
    text: str
    url: str
    author: str


def parse_cookie_header(header: str) -> list[dict[str, Any]]:
    cookies = []
    for part in header.strip().strip('"').split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        cookies.append(
            {"name": name, "value": value, "domain": ".facebook.com", "path": "/", "secure": True}
        )
    return cookies


def extract_posts(response_bodies: Iterable[str]) -> list[FbPost]:
    """Parse concatenated GraphQL response bodies (one JSON document per line)."""
    posts: dict[str, FbPost] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            msg = o.get("message")
            post_id = o.get("post_id")
            if isinstance(msg, dict) and isinstance(msg.get("text"), str) and post_id:
                actors = o.get("actors") or [{}]
                url = (o.get("wwwURL") or "").split("?")[0]
                posts.setdefault(
                    str(post_id),
                    FbPost(
                        post_id=str(post_id),
                        text=msg["text"],
                        url=url,
                        author=actors[0].get("name") or "",
                    ),
                )
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for body in response_bodies:
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                walk(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(posts.values())


def collect_group_posts(
    group_ids: list[str],
    *,
    cookie_header: str | None = None,
    scrolls: int = 3,
    pause_sec: float = 2.0,
) -> dict[str, list[FbPost]]:
    """Visit each group once and return its newest posts, keyed by group id."""
    from playwright.sync_api import sync_playwright

    cookie_header = cookie_header or os.environ.get("FACEBOOK_COOKIES", "")
    if not cookie_header:
        raise FacebookAuthError("FACEBOOK_COOKIES is not set")

    out: dict[str, list[FbPost]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = browser.new_context(
                locale="he-IL", viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT
            )
            ctx.add_cookies(parse_cookie_header(cookie_header))
            ctx.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCES
                else route.continue_(),
            )
            for group_id in group_ids:
                out[group_id] = _scrape_group(ctx, group_id, scrolls=scrolls, pause_sec=pause_sec)
                # Spread requests out; fixed-rate polling of many groups looks automated.
                time.sleep(random.uniform(3, 8))
        finally:
            browser.close()
    return out


def _scrape_group(ctx: Any, group_id: str, *, scrolls: int, pause_sec: float) -> list[FbPost]:
    bodies: list[str] = []

    def on_response(resp: Any) -> None:
        if "/api/graphql" in resp.url:
            try:
                bodies.append(resp.text())
            except Exception:
                log.debug("facebook %s: unreadable graphql body", group_id)

    page = ctx.new_page()
    try:
        page.on("response", on_response)
        page.goto(GROUP_URL.format(group_id=group_id), wait_until="domcontentloaded", timeout=60_000)
        if any(m in page.url for m in _AUTH_URL_MARKERS):
            raise FacebookAuthError(f"redirected to {page.url.split('?')[0]}")
        page.wait_for_timeout(5000)
        for _ in range(scrolls):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(int(pause_sec * 1000))
        return extract_posts(bodies)
    finally:
        page.close()


_FIRST_LINE_MAX = 160


def title_of(text: str) -> str:
    """One-line summary of a post: its first non-empty line, trimmed."""
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line if len(line) <= _FIRST_LINE_MAX else line[: _FIRST_LINE_MAX - 1] + "…"
    return ""
