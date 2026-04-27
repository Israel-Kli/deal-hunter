"""Telegram notifier. Honors DRY_RUN by logging instead of sending."""

from __future__ import annotations

import logging
import os
import time

import requests

from deal_hunter.effective import effective_price_per_sqm
from deal_hunter.models import Listing

log = logging.getLogger(__name__)


def _fmt(listing: Listing) -> str:
    score = listing.score or 0
    fair = listing.fair_price_estimate
    fair_line = f"\n🎯 הערכה: ₪{fair:,}" if fair else ""
    return (
        f"🔥 <b>הזדמנות! [{score}/10]</b>\n"
        f"📍 {listing.address}\n"
        f"💰 ₪{listing.price:,}"
        f" ({effective_price_per_sqm(listing) or 0:,}/מ\"ר)"
        f"{fair_line}\n"
        f"🛏 {listing.rooms or '?'} חד׳ | 📐 {listing.sqm or '?'}מ\"ר | קומה {listing.floor if listing.floor is not None else '?'}\n"
        f"🏷 מקור: {listing.source}\n"
        f"🔗 <a href=\"{listing.url}\">צפה במקור</a>"
    )


def send(listings: list[Listing], *, bot_token: str, chat_id: str, limit: int = 10) -> int:
    """Send up to `limit` messages. Respects DRY_RUN env var. Returns count sent (or logged)."""
    dry = os.environ.get("DRY_RUN") == "1"
    if not listings:
        return 0
    if not dry and (not bot_token or not chat_id):
        log.warning("telegram disabled: missing token or chat_id")
        return 0
    n = 0
    for listing in listings[:limit]:
        msg = _fmt(listing)
        if dry:
            log.info("[DRY_RUN telegram] %s", msg.replace("\n", " | "))
            n += 1
            continue
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": False},
                timeout=10,
            )
            n += 1
            time.sleep(0.5)
        except Exception as e:
            log.warning("telegram send error: %s", e)
    return n
