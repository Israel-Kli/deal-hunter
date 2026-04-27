"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from deal_hunter import config as cfg_mod
from deal_hunter.adapters.base import SearchFilters
from deal_hunter.adapters.ad import AdAdapter
from deal_hunter.adapters.nadlanh import NadlanhAdapter
from deal_hunter.adapters.onmap import OnMapAdapter
from deal_hunter.adapters.reariel import RearielAdapter
from deal_hunter.adapters.simplestate import SimplestateAdapter
from deal_hunter.adapters.spectra import SpectraAdapter
from deal_hunter.adapters.yad2 import Yad2Adapter
from deal_hunter.dedup.canonicalizer import CanonicalGroup, dedup_batch, load_existing_groups
from deal_hunter.models import Listing, ScanResult
from deal_hunter.notify import telegram
from deal_hunter.repo.listings_repo import ListingsRepo
from deal_hunter.normalize.extract_features import extract_features
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
        slugs = cfg.onmap_cities or [c.onmap_slug for c in cfg.cities if c.onmap_slug]
        if not slugs:
            log.warning(
                "OnMap enabled but no city slugs: set `onmap_cities` or `cities[].onmap_slug` "
                "(e.g. tel-aviv-yafo, ramat-gan)"
            )
        else:
            allowed_cities = [c.hebrew_name for c in cfg.cities if c.hebrew_name]
            out.append(
                OnMapAdapter(
                    city_slugs=slugs,
                    search=cfg.search.model_dump(),
                    allowed_cities=allowed_cities,
                    max_pages=cfg.schedule.max_pages,
                    request_delay_sec=cfg.schedule.delay_between_requests_sec,
                )
            )
    if cfg.sources.ad:
        paths = cfg.ad_city_paths or ["/nadlansale"]
        allowed_cities = [c.hebrew_name for c in cfg.cities if c.hebrew_name]
        out.append(
            AdAdapter(
                city_paths=paths,
                search=cfg.search.model_dump(),
                allowed_cities=allowed_cities,
                max_pages=cfg.schedule.max_pages,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
                enrich_details=True,
            )
        )
    # Madlan adapter still deferred (PerimeterX CAPTCHA).
    if cfg.sources.reariel:
        allowed_cities = [c.hebrew_name for c in cfg.cities if c.hebrew_name]
        out.append(
            RearielAdapter(
                search=cfg.search.model_dump(),
                allowed_cities=allowed_cities,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
        )
    if cfg.sources.spectra:
        out.append(
            SpectraAdapter(
                search=cfg.search.model_dump(),
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
        )
    if cfg.sources.nadlanh:
        out.append(
            NadlanhAdapter(
                search=cfg.search.model_dump(),
                max_pages=cfg.schedule.max_pages,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
        )
    if cfg.sources.simplestate:
        out.append(
            SimplestateAdapter(
                business_ids=[877],  # מפתח העיר — Mafteach Ha'Ir
                search=cfg.search.model_dump(),
                page_size=100,
                request_delay_sec=cfg.schedule.delay_between_requests_sec,
            )
        )
    return out


def run_once(cfg: cfg_mod.Config, *, enrich: bool = False, max_items: int | None = None) -> int:
    """Scrape, score, upsert, dedup, alert. Returns number of alerts sent/logged."""
    db_path = Path(cfg.data_dir) / "deal-hunter.db"
    total_alerts = 0
    fresh_listings: list[Listing] = []
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
                    extract_features(listing)
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

        # ── Cross-source dedup pass ──────────────────────────────────────
        try:
            existing = load_existing_groups(repo.conn)
            uncanonical = repo.list_uncanonical()
            if uncanonical:
                listings_to_dedup: list[Listing] = []
                for row in uncanonical:
                    l = Listing(
                        source=row["source"],
                        source_id=row["source_id"],
                        url="",
                        city=row["city"],
                        neighborhood=row["neighborhood"],
                        street=row["street"],
                        house_number=row["house_number"],
                        address=row["address"],
                        rooms=row["rooms"],
                        sqm=row["sqm"],
                        price=row["price"],
                    )
                    listings_to_dedup.append(l)
                groups = dedup_batch(listings_to_dedup, existing)
                assignments = [
                    (l.canonical_id, l.source, l.source_id)
                    for l in listings_to_dedup
                    if l.canonical_id
                ]
                if assignments:
                    repo.update_canonical_ids(assignments)
                    merged = sum(1 for g in groups.values() if len(g.members) > 1)
                    log.info(
                        "dedup: %d listings → %d canonical groups, %d multi-source",
                        len(listings_to_dedup), len(groups), merged,
                    )
        except Exception:
            log.exception("dedup pass failed")

    return total_alerts


def cmd_run(args: argparse.Namespace) -> int:
    cfg = cfg_mod.load(args.config)
    if args.once:
        run_once(cfg, enrich=args.enrich, max_items=args.max_items)
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    import random

    interval_min = cfg.schedule.poll_interval_minutes
    jitter_sec = 120  # ±2 min random jitter so sources don't all hit at the same wall-clock second

    log.info("Starting APScheduler, base interval=%dm, jitter=±%ds", interval_min, jitter_sec)

    def _job() -> None:
        try:
            run_once(cfg, enrich=args.enrich)
        except Exception:
            log.exception("run_once failed")

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _job,
        trigger=IntervalTrigger(
            minutes=interval_min,
            jitter=random.randint(0, jitter_sec),
        ),
        id="scan-cycle",
        name="Deal Hunter scan cycle",
        replace_existing=True,
        max_instances=1,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")
    return 0


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
    serve(cfg, host=cfg.dashboard_host)
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
