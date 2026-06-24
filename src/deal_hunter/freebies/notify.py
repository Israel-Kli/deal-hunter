"""Telegram notifier for freebies. Separate from notify/telegram.py, which is typed
against the real-estate Listing model."""

from __future__ import annotations

import logging
import os
import time

import requests

from deal_hunter.freebies.models import FreebieItem

log = logging.getLogger(__name__)


def _fmt(item: FreebieItem) -> str:
    lines = [f"🆕 <b>{item.title}</b>"]
    if item.city:
        lines.append(f"📍 {item.city}")
    lines.append(f"🔍 {item.watch_label}")
    lines.append(f"🔗 <a href=\"{item.url}\">צפה במודעה</a>")
    return "\n".join(lines)


def send(items: list[FreebieItem], *, bot_token: str, chat_id: str, limit: int = 20) -> list[FreebieItem]:
    """Send up to `limit` messages. Returns the items that were sent (or logged in DRY_RUN)."""
    dry = os.environ.get("DRY_RUN") == "1"
    sent: list[FreebieItem] = []
    if not items:
        return sent
    if not dry and (not bot_token or not chat_id):
        log.warning("telegram disabled: missing token or chat_id")
        return sent
    for item in items[:limit]:
        msg = _fmt(item)
        if dry:
            log.info("[DRY_RUN telegram freebies] %s", msg.replace("\n", " | "))
            sent.append(item)
            continue
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            sent.append(item)
            time.sleep(0.5)
        except Exception as e:
            log.warning("telegram freebies send error: %s", e)
    return sent
