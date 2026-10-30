"""Freebies pipeline runner. One pass: for each watch in config, scrape, diff against DB,
alert on new items, persist."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from deal_hunter import config as cfg_mod
from deal_hunter.freebies import agora, facebook, notify
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


def match_topics(text: str, topics: list[cfg_mod.FacebookTopicCfg]) -> list[str]:
    """Labels of every topic whose patterns match the post text."""
    return [
        t.label for t in topics
        if any(re.search(p, text, re.IGNORECASE) for p in t.patterns)
    ]


def run_facebook_once(cfg: cfg_mod.Config, *, seed_only: bool = False) -> int:
    """One pass over the configured Facebook groups. Returns the number of alerts sent.

    Raises FacebookAuthError when the session cookies no longer work; the first such
    failure also sends a Telegram warning (re-armed once a run succeeds).
    """
    fb = cfg.freebies.facebook
    if not (cfg.freebies.enabled and fb.enabled and fb.groups and fb.topics):
        log.debug("freebies facebook disabled or unconfigured")
        return 0

    data_dir = Path(cfg.data_dir)
    auth_marker = data_dir / "facebook_auth_alerted"
    labels = {g.id: g.label or g.id for g in fb.groups}
    try:
        by_group = facebook.collect_group_posts([g.id for g in fb.groups], scrolls=fb.scrolls)
    except facebook.FacebookAuthError as e:
        log.error("facebook: session not logged in: %s", e)
        if not auth_marker.exists():
            notify.send_text(
                f"Facebook freebies: session expired ({e}). Re-export FACEBOOK_COOKIES.",
                bot_token=cfg.notifications.telegram_bot_token,
                chat_id=cfg.notifications.telegram_chat_id,
            )
            auth_marker.touch()
        raise
    auth_marker.unlink(missing_ok=True)

    total_alerts = 0
    with FreebiesRepo(data_dir / "deal-hunter.db") as repo:
        for group_id, posts in by_group.items():
            label = labels[group_id]
            new_items: list[FreebieItem] = []
            matched = 0
            for post in posts:
                topics = match_topics(post.text, fb.topics)
                if not topics or not post.url:
                    continue
                matched += 1
                item = FreebieItem(
                    source="facebook",
                    source_id=post.post_id,
                    watch_label=f"{', '.join(topics)} · {label}",
                    title=facebook.title_of(post.text),
                    url=post.url,
                    posted_at=date.today().isoformat(),
                )
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
                "facebook %s: posts=%d matched=%d new=%d alerted=%d%s",
                label, len(posts), matched, len(new_items), alerted,
                " [seed]" if seed_only else "",
            )
            repo.log_scan(
                watch_label=f"facebook:{label}",
                fetched=len(posts),
                new=len(new_items) if not seed_only else matched,
                alerted=alerted,
            )
    return total_alerts
