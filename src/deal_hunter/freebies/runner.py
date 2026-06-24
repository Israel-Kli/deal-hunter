"""Freebies pipeline runner. One pass: for each watch in config, scrape, diff against DB,
alert on new items, persist."""

from __future__ import annotations

import logging
from pathlib import Path

from deal_hunter import config as cfg_mod
from deal_hunter.freebies import agora, notify
from deal_hunter.freebies.models import FreebieItem
from deal_hunter.freebies.repo import FreebiesRepo

log = logging.getLogger(__name__)


def _fetch_for_watch(watch: cfg_mod.FreebieWatchCfg) -> list[FreebieItem]:
    if watch.source != "agora":
        log.warning("freebies: unknown source %r for watch %r — skipping", watch.source, watch.label)
        return []
    items = agora.fetch_items(
        watch_label=watch.label,
        keyword=watch.keyword,
        city=watch.city,
        condition=watch.condition,
        deal_type=watch.deal_type,
        category=watch.category,
        subcategory=watch.subcategory,
    )
    required = watch.title_must_contain
    if required:
        before = len(items)
        items = [i for i in items if all(tok in i.title for tok in required)]
        dropped = before - len(items)
        if dropped:
            log.info(
                "freebies %s: dropped %d/%d items missing required tokens %s",
                watch.label, dropped, before, required,
            )
    return items


def run_once(cfg: cfg_mod.Config, *, seed_only: bool = False) -> int:
    """One pass over all configured freebie watches.

    Returns the number of Telegram alerts sent (0 in seed-only mode).
    """
    if not cfg.freebies.enabled:
        log.debug("freebies disabled, skipping")
        return 0
    if not cfg.freebies.watches:
        log.debug("freebies enabled but no watches configured")
        return 0

    db_path = Path(cfg.data_dir) / "deal-hunter.db"
    total_alerts = 0
    with FreebiesRepo(db_path) as repo:
        for watch in cfg.freebies.watches:
            try:
                items = _fetch_for_watch(watch)
            except Exception as e:
                log.exception("freebies %s: scrape failed", watch.label)
                repo.log_scan(
                    watch_label=watch.label, fetched=0, new=0, alerted=0,
                    errors=str(e)[:500],
                )
                continue

            new_items: list[FreebieItem] = []
            for item in items:
                is_new = repo.upsert(item, mark_alerted=seed_only)
                if is_new and not seed_only:
                    new_items.append(item)

            alerted = 0
            if new_items:
                sent = notify.send(
                    new_items,
                    bot_token=cfg.notifications.telegram_bot_token,
                    chat_id=cfg.notifications.telegram_chat_id,
                )
                for item in sent:
                    repo.mark_alerted(item.source, item.source_id)
                alerted = len(sent)
                total_alerts += alerted

            log.info(
                "freebies %s: fetched=%d new=%d alerted=%d%s",
                watch.label, len(items), len(new_items), alerted,
                " [seed]" if seed_only else "",
            )
            repo.log_scan(
                watch_label=watch.label,
                fetched=len(items),
                new=len(new_items) if not seed_only else len(items),
                alerted=alerted,
            )

    return total_alerts
