"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from deal_hunter import config as cfg_mod
from deal_hunter.adapters.base import SearchFilters
from deal_hunter.adapters.yad2 import Yad2Adapter
from deal_hunter.models import Listing, ScanResult
from deal_hunter.notify import telegram
from deal_hunter.repo.listings_repo import ListingsRepo
from deal_hunter.scoring.heuristic import score_listing

log = logging.getLogger("deal_hunter")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _adapters(cfg: cfg_mod.Config):
    out = []
    if cfg.sources.yad2:
        out.append(
            Yad2Adapter(
                cities=[c.model_dump() for c in cfg.cities],
                search=cfg.search.model_dump(),
                max_pages=cfg.schedule.max_pages,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
        )
    # Madlan / OnMap / ad adapters added in M3.
    return out


def run_once(cfg: cfg_mod.Config, *, enrich: bool = False, max_items: int | None = None) -> int:
    """Scrape, score, upsert, alert. Returns number of alerts sent/logged."""
    db_path = Path(cfg.data_dir) / "deal-hunter.db"
    total_alerts = 0
    with ListingsRepo(db_path) as repo:
        for adapter in _adapters(cfg):
            started = time.time()
            result = ScanResult(source=adapter.source)
            alert_queue: list[Listing] = []
            try:
                count = 0
                for listing in adapter.fetch_feed(SearchFilters()):
                    if enrich:
                        try:
                            adapter.fetch_detail(listing)
                        except Exception as e:
                            result.errors.append(f"enrich {listing.source_id}: {e}")
                    score, reasons = score_listing(listing)
                    listing.score = score
                    listing.score_reasons = reasons
                    is_new, prev_price = repo.upsert(listing)
                    result.fetched += 1
                    count += 1
                    if is_new:
                        result.new += 1
                    else:
                        result.updated += 1
                        if prev_price and listing.price < prev_price:
                            drop_pct = (prev_price - listing.price) / prev_price * 100
                            if drop_pct >= cfg.notifications.price_drop_pct:
                                result.price_drops += 1
                                alert_queue.append(listing)
                    if is_new and score >= cfg.notifications.score_threshold:
                        alert_queue.append(listing)
                    if max_items and count >= max_items:
                        break
            except Exception as e:
                log.exception("adapter %s failed", adapter.source)
                result.errors.append(str(e))

            if alert_queue:
                sent = telegram.send(
                    alert_queue,
                    bot_token=cfg.notifications.telegram_bot_token,
                    chat_id=cfg.notifications.telegram_chat_id,
                )
                result.alerted = sent
                total_alerts += sent

            result.duration_sec = round(time.time() - started, 2)
            repo.log_scan(result)
            log.info(
                "scan %s: fetched=%d new=%d updated=%d drops=%d alerted=%d in %.1fs errors=%d",
                result.source, result.fetched, result.new, result.updated,
                result.price_drops, result.alerted, result.duration_sec, len(result.errors),
            )
    return total_alerts


def cmd_run(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load(args.config)
    if args.once:
        run_once(cfg, enrich=args.enrich, max_items=args.max_items)
        return 0
    interval_sec = cfg.schedule.poll_interval_minutes * 60
    log.info("Running scheduler loop, interval=%ds", interval_sec)
    while True:
        try:
            run_once(cfg, enrich=args.enrich)
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("run_once failed")
        log.info("sleeping %ds (next run %s)", interval_sec,
                 datetime.utcnow().replace(microsecond=0).isoformat())
        time.sleep(interval_sec)


def cmd_dashboard(args: argparse.Namespace) -> int:
    from deal_hunter.web.app import serve

    cfg = cfg_mod.load(args.config)
    serve(cfg)
    return 0


def main() -> int:
    _setup_logging()
    p = argparse.ArgumentParser(prog="deal-hunter")
    p.add_argument("--config", default="configs/config.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Scrape + score + alert")
    run_p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    run_p.add_argument("--enrich", action="store_true", help="Fetch detail pages (slower)")
    run_p.add_argument("--max-items", type=int, default=None, help="Cap listings per source (dev)")
    run_p.set_defaults(func=cmd_run)

    dash_p = sub.add_parser("dashboard", help="Serve the dashboard HTTP")
    dash_p.set_defaults(func=cmd_dashboard)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
