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
from deal_hunter.adapters.onmap import OnMapAdapter
from deal_hunter.adapters.yad2 import Yad2Adapter
from deal_hunter.models import Listing, ScanResult
from deal_hunter.notify import telegram
from deal_hunter.repo.listings_repo import ListingsRepo
from deal_hunter.scoring.heuristic import score_listing
from deal_hunter.valuation.fair_price import enrich_listing_fair_price

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
    if cfg.sources.onmap:
        slugs = cfg.onmap_cities or ["tel-aviv-yafo"]
        out.append(
            OnMapAdapter(
                city_slugs=slugs,
                search=cfg.search.model_dump(),
                max_pages=cfg.schedule.max_pages,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
        )
    # Madlan / ad adapters added later in M3.
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
                    # Fair-price valuation (uses comps already in DB from prior runs)
                    try:
                        enrich_listing_fair_price(listing, repo.conn)
                    except Exception as e:
                        log.debug("fair_price skipped for %s: %s", listing.source_id, e)
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


def _comps_dicts_from_comps(comps: list) -> list[dict]:
    """Convert list[Comp] → list[dict] ready for repo.upsert_comps."""
    import hashlib
    out = []
    for c in comps:
        ah = hashlib.sha1(
            "|".join([c.city, c.neighborhood, c.street, c.house_number]).lower().encode()
        ).hexdigest()[:16]
        out.append({
            "address_hash": ah,
            "deal_date": c.deal_date,
            "price": c.price,
            "sqm": c.sqm,
            "rooms": c.rooms,
            "city": c.city,
            "neighborhood": c.neighborhood,
            "street": c.street,
            "house_number": c.house_number,
            "year_built": c.year_built,
            "raw": c.raw,
        })
    return out


def cmd_comps_refresh(args: argparse.Namespace) -> int:
    """Pre-populate the comps table from nadlan.gov.il and Yad2 deals."""
    import json as _json

    cfg = cfg_mod.load(args.config)
    db_path = Path(cfg.data_dir) / "deal-hunter.db"
    max_listings = args.max_listings
    sources = args.sources  # e.g. ["nadlan", "yad2"] or ["nadlan"] or ["yad2"]

    with ListingsRepo(db_path) as repo:
        rows = repo.conn.execute(
            "SELECT source, source_id, city, neighborhood, street, source_payload "
            "FROM listings ORDER BY score DESC NULLS LAST LIMIT ?",
            (max_listings,),
        ).fetchall()

        if not rows:
            log.info("comps refresh: no listings in DB yet — run 'deal-hunter run --once' first")
            return 0

        total = 0

        # ── nadlan.gov.il (primary) ──────────────────────────────────────
        if "nadlan" in sources:
            from deal_hunter.comps.nadlan_gov import NadlanGovProvider
            nadlan = NadlanGovProvider()
            seen_neighborhoods: set[tuple[str, str]] = set()

            for row in rows:
                city = row[2]
                neighborhood = row[3]
                street = row[4] if len(row) > 4 else ""
                payload = _json.loads(row[5] if len(row) > 5 else row[4] or "{}")

                # De-duplicate: one nadlan call per (city, street) pair
                key = (city.lower(), street.lower())
                if key in seen_neighborhoods:
                    continue
                seen_neighborhoods.add(key)

                comps = nadlan.comps_for(
                    city=city,
                    neighborhood=neighborhood,
                    street=street,
                    rooms=None,        # fetch all room buckets
                    sqm=None,
                    window_months=getattr(args, "window_months", 18),
                )
                if not comps:
                    continue
                dicts = _comps_dicts_from_comps(comps)
                n = repo.upsert_comps("nadlan_gov", dicts)
                total += n
                log.info(
                    "comps refresh [nadlan]: city=%s street=%s → %d comps", city, street, n
                )

        # ── Yad2 deals (secondary, per-listing HTML) ─────────────────────
        if "yad2" in sources:
            from deal_hunter.comps.yad2_deals import Yad2DealsProvider

            adapter = Yad2Adapter(
                cities=[c.model_dump() for c in cfg.cities],
                search=cfg.search.model_dump(),
                max_pages=cfg.schedule.max_pages,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
            provider = Yad2DealsProvider()
            bid = adapter._get_build_id()
            if not bid:
                log.error("comps refresh [yad2]: cannot get Yad2 build id")
            else:
                for row in rows:
                    source = row[0]
                    token = row[1]
                    city = row[2]
                    neighborhood = row[3]
                    payload = _json.loads(row[5] if len(row) > 5 else row[4] or "{}")
                    slug = payload.get(
                        "_slug", cfg.cities[0].slug if cfg.cities else "tel-aviv-area"
                    )

                    if source != "yad2":
                        continue

                    comps = provider.comps_for_listing(
                        token, bid, slug,
                        city=city, neighborhood=neighborhood,
                    )
                    if not comps:
                        continue
                    dicts = _comps_dicts_from_comps(comps)
                    n = repo.upsert_comps("yad2_deals", dicts)
                    total += n
                    log.info("comps refresh [yad2]: token=%s → %d comps", token, n)

        log.info("comps refresh done: %d total comps upserted", total)
    return 0


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

    comps_p = sub.add_parser("comps", help="Comps management")
    comps_sub = comps_p.add_subparsers(dest="comps_cmd", required=True)
    refresh_p = comps_sub.add_parser("refresh", help="Pull closed-deal comps for top listings")
    refresh_p.add_argument(
        "--max-listings", type=int, default=50,
        help="How many top-scored listings to fetch comps for (default 50)",
    )
    refresh_p.add_argument(
        "--sources", nargs="+", default=["nadlan", "yad2"],
        choices=["nadlan", "yad2"],
        help="Which comps sources to pull (default: nadlan yad2)",
    )
    refresh_p.add_argument(
        "--window-months", type=int, default=18,
        help="How many months back to include (default 18)",
    )
    refresh_p.set_defaults(func=cmd_comps_refresh)

    dash_p = sub.add_parser("dashboard", help="Serve the dashboard HTTP")
    dash_p.set_defaults(func=cmd_dashboard)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
