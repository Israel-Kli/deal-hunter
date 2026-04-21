# OPERATING.md — deal-hunter

## Quick Start

```bash
# Local development
cd /Users/user/github/deal-hunter
.venv/bin/deal-hunter run --once --max-items 10   # quick smoke test
.venv/bin/deal-hunter run --once --enrich          # with detail-page enrichment (slower)
.venv/bin/deal-hunter dashboard                     # serve UI at http://127.0.0.1:8081

# Docker (production)
cp .env.example .env   # edit with your Telegram tokens
docker compose up -d
docker compose logs -f hunter
```

## Architecture

```
  [ Yad2 ]  [ OnMap ]  [ ad.co.il ]        [ nadlan.gov.il ]  [ Yad2 Deals ]
       \       |          /                        \              /
        +-- ScraperAdapter --+                  CompsProvider
                             |                             |
                             v                             v
                      [ Normalizer ] → canonical Listing → [ Scorer ]
                             |
                             v
                      [ Cross-source Dedup ]
                             |
                             v
                        [ SQLite repo ]
                             |
               +-------------+-------------+
               v                           v
        [ Dashboard UI ]          [ Telegram notifier ]
```

## Configuration

Edit `configs/config.json` (gitignored). Key sections:

| Section | Purpose |
|---------|---------|
| `search` | Price/rooms/sqm filters, max listing age, ground-floor exclusion |
| `cities` | Yad2 city codes + slugs (city_code from Yad2 API) |
| `sources` | Boolean toggles per source (yad2, onmap, ad, madlan) |
| `comps` | Boolean toggles for comps sources (yad2_deals, nadlan_gov) |
| `schedule` | Poll interval, request delay, max pages per source |
| `notifications` | Telegram bot token, chat ID, score threshold, price-drop % |
| `onmap_cities` | OnMap city slugs (English, e.g. "tel-aviv-yafo") |
| `ad_city_paths` | ad.co.il URL paths (e.g. "/city/tel-aviv", "/nadlansale") |

### Changing filters without rebuild

Just edit `configs/config.json` and restart the container. No rebuild needed:

```bash
docker compose restart hunter
```

### Adding a new source

1. Create `src/deal_hunter/adapters/{source}.py` implementing the `ScraperAdapter` protocol:
   - `source` class attribute (must match `Source` Literal in `models.py`)
   - `fetch_feed(filters) -> Iterable[Listing]`
   - `fetch_detail(listing) -> Listing` (can be a no-op if feed is complete)
2. Add the source literal to `models.py`: `Source = Literal["yad2", "madlan", "onmap", "ad", "new_source"]`
3. Register in `cli._adapters()` guarded by `cfg.sources.new_source`
4. Add config field if needed (e.g. `new_source_cities: list[str]`)
5. Write golden-fixture test: save a recorded feed response to `tests/fixtures/`, assert parsed Listing fields
6. Add to `config.example.json`

### Adding a new comps source

1. Create `src/deal_hunter/comps/{source}.py` implementing `CompsProvider`:
   - `comps_for(city, neighborhood, street, rooms, sqm, window_months) -> list[Comp]`
2. Register in `cli.cmd_comps_refresh`
3. Add toggle to `config.py` → `CompsCfg`

## Telegram Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) → get `TELEGRAM_BOT_TOKEN`
2. Send a message to your bot, then visit `https://api.telegram.org/bot<token>/getUpdates` to find your `chat_id`
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=-1001234567890
   ```

### Dry-run mode

Set `DRY_RUN=1` in the environment to log Telegram messages to stdout instead of sending:

```bash
DRY_RUN=1 .venv/bin/deal-hunter run --once
```

## Scoring

Score is 1–10, based on five weighted components:

| Component | Weight | What it measures |
|-----------|--------|-----------------|
| Price vs market | 30% (±3) | How far below the ₪/sqm band or fair-price estimate |
| Rental yield | 20% (±2) | Estimated gross yield (city-specific multipliers) |
| Physical attrs | 20% (±2) | Parking, elevator, balcony, mamad, renovation |
| Negotiation | 15% (±1.5) | Private seller bonus, price-drop history |
| Risk | 15% (-1.5) | No elevator on high floor, no mamad, high price |

`notifications.score_threshold` (default 7.0) controls which listings trigger alerts.

### Tuning market references

Edit `MARKET_REFS` in `src/deal_hunter/scoring/heuristic.py`. First match wins by substring. Add neighborhood-specific bands after a soak run reveals actual prices.

## Dedup

Cross-source dedup uses two tiers:

1. **Exact key**: `sha1(normalized_street + house_number + rooms_bucket + sqm_bucket)`
2. **Fuzzy fallback**: rapidfuzz Levenshtein ratio ≥ 0.85 within same city

Canonical IDs are assigned after each scan cycle. The dashboard's `/api/dedup` endpoint shows multi-source groups sorted by price spread (MAX-MIN across sources).

## Data Retention

- `purge_older_than(cutoff_iso)` deletes listings older than the cutoff (unless they have a recent `publish_date`)
- `price_history` accumulates indefinitely — consider periodic cleanup if the DB grows large
- `scan_log` accumulates indefinitely — same note

## ToS Posture

- **Personal-use only** — single tenant, no redistribution of scraped data
- **Polite rate-limiting** — `schedule.delay_between_requests_sec` (default 3.0s) with jitter
- **No credential sharing** — all scraping is anonymous (no login required)
- **Respect robots.txt spirit** — we don't hammer endpoints; delays are configurable
- **If a site blocks us**: escalate gracefully (Playwright → Camoufox → proxy rotation), but consider dropping the source if it's not worth the cat-and-mouse game

## Troubleshooting

### Yad2 returns 403 / empty feed

Build ID expired. The adapter re-fetches it per instance. If it persists, the Next.js build changed — check the feed URL construction in `adapters/yad2.py`.

### ad.co.il returns no listings

The `/nadlansale` feed may have pagination that resets. Try a city-specific path like `/city/tel-aviv` in `ad_city_paths`.

### OnMap returns no listings

Check that `onmap_cities` uses valid English slugs (e.g. "tel-aviv-yafo", not "תל אביב"). The OnMap API uses English city slugs.

### Dedup merging wrong listings

Check the `canonicalize_address` output for the addresses in question. False positives can happen when two units in the same building have the same street+number but different floors. The rooms/sqm bucket helps but isn't perfect. Tune the fuzzy threshold (default 0.85) in `dedup/canonicalizer.py`.

### Database is too large

```bash
# Check size
ls -lh data/deal-hunter.db

# Vacuum (reclaims space from deleted rows)
sqlite3 data/deal-hunter.db "VACUUM;"

# Purge old listings
.venv/bin/python -c "
from deal_hunter.repo.listings_repo import ListingsRepo
from datetime import datetime, timedelta
cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
with ListingsRepo('data/deal-hunter.db') as repo:
    n = repo.purge_older_than(cutoff)
    print(f'Purged {n} listings older than {cutoff}')
"
```

## Dashboard Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard HTML (RTL, dark theme) |
| `GET /api/listings` | All listings as JSON, sorted by score DESC |
| `GET /api/dedup` | Multi-source canonical groups with price spread |
| `GET /healthz` | Health check (returns "ok") |

## Comps Refresh

Pre-populate the `comps` table from closed-deal sources:

```bash
.venv/bin/deal-hunter comps refresh --max-listings 50 --sources nadlan yad2 --window-months 18
```

This is a one-time (or nightly) operation, not part of the regular scan cycle.
