# Deal Hunter — CLAUDE Context

## Project Overview

Multi-source undervalued for-sale listings monitor for Israeli real estate boards. Scrapes Yad2, OnMap, and ad.co.il, scores listings by investment potential, deduplicates across sources, and alerts via Telegram.

## Architecture

```
src/deal_hunter/
├── adapters/       # Source scrapers (Yad2, OnMap, Ad) — implement ScraperAdapter protocol
├── comps/          # Comparable sales providers (nadlan_gov, yad2_deals)
├── dedup/          # Cross-source dedup via canonical address keys + fuzzy clustering
├── normalize/      # Hebrew text normalization utilities
├── notify/         # Telegram notifier
├── repo/           # SQLite repository (listings, comps, scan_log, price_history)
├── scoring/        # Heuristic investment scorer (1-10 scale)
├── valuation/      # Fair price estimator from comps
├── web/            # Minimal stdlib HTTP dashboard (dashboard.html + /api/*)
├── cli.py          # CLI entry point (deal-hunter run|dashboard|comps)
├── config.py       # Pydantic config models + JSON loader
├── http_client.py  # Shared HTTP client (curl_cffi)
└── models.py       # Canonical data models (Listing, Comp, ScanResult)
```

## Key Design Decisions

- **No Docker on Azure VM** — 1 GiB RAM too tight; uses systemd + venv directly
- **SQLite** — single-file DB in `data/deal-hunter.db`
- **stdlib HTTP server** — dashboard uses `http.server`, no Flask/FastAPI dependency
- **APScheduler** — blocking scheduler for periodic scan cycles with jitter

## Scoring System

Heuristic 1-10 score (`scoring/heuristic.py` + `scoring/description_signals.py`):

1. **Price vs market** — smooth piecewise adjustment of ₪/m² vs band; band from comps (`fair_price_estimate`) when available, else `MARKET_REFS` by city/neighborhood.
2. **Description** — bonus only for explicit phrases (יחידות דיור, יחידת דיור, מחולק/מחולקת, apartment), same as yellow highlights in the dashboard; capped bonus for garden/lot phrases + gradual bonus for many rooms (combined cap).
3. **Physical** — parking, balcony, renovated, ground-floor discount (elevator and mamad are not scored).
4. **Seller** — private listings score higher than broker (`is_agent`).
5. **Negotiation** — price-drop ramp vs previous ask.
6. **Risk** — very high price liquidity penalty only.

Market bands defined in `scoring/heuristic.py:MARKET_REFS`. Add new cities here.

## Deployment

Azure VM (Standard B2ats v2, 2 vCPU, 1 GiB RAM, Ubuntu 22.04):
- **IP**: `51.4.97.109`
- **SSH**: `ssh azure-test` (key-based via `~/.ssh/config`)
- **Path**: `/opt/deal-hunter`
- **Services**: `deal-hunter` (scraper), `deal-hunter-dashboard` (port 8081)
- **Swap**: 2GB, swappiness=10
- **Memory limit**: `MemoryMax=800M` cgroup

### SSH Access

Key-based auth configured in `~/.ssh/config`:
```
Host azure-test
    HostName 51.4.97.109
    User azure-test
    IdentityFile ~/.ssh/azure_test
    StrictHostKeyChecking no
```
Backup password: `Avtozavodsky2`

### Config

`configs/config.json` on VM controls:
- `search` — filters (rooms, price, sqm, property_types)
- `cities` — which cities to scrape (city_code + slug for Yad2 URL)
- `sources` — enable/disable scrapers
- `schedule` — poll interval, request delay, max pages
- `dashboard_host` — bind address (0.0.0.0 for external access)

### Update Flow (local → Azure, no DB reset)

After **any code or template change** you want live on the VM: commit and push from your dev machine, then pull on Azure and restart services. This updates the running app only; it does **not** touch `data/deal-hunter.db` (avoid `/api/reset` and avoid deleting the DB as part of deploy).

**1. Locally (from the repo root)**

```bash
git add -A && git status   # review; exclude anything you must not ship
git commit -m "Describe the change"
git push origin main       # or your branch, then merge to main as you prefer
```

**2. On the Azure VM (install step only if `pyproject.toml` / deps changed)**

```bash
ssh azure-test "cd /opt/deal-hunter && git pull && .venv/bin/pip install -e . --quiet && sudo systemctl restart deal-hunter deal-hunter-dashboard"
```

If dependencies did not change, you can skip the install:

```bash
ssh azure-test "cd /opt/deal-hunter && git pull && sudo systemctl restart deal-hunter deal-hunter-dashboard"
```

**3. Agent / automation convention**

Whenever changes are merged to the branch the VM tracks: **push → pull on Azure → restart** both units so the dashboard and scraper load the new code. Do not deploy by restarting alone without `git pull` on the server.

### Useful Commands

```bash
# Check services
ssh azure-test "sudo systemctl status deal-hunter deal-hunter-dashboard"

# View scraper logs
ssh azure-test "journalctl -u deal-hunter -f --no-pager"

# Trigger manual scan
ssh azure-test "cd /opt/deal-hunter && sudo -u deal-hunter bash -c 'source .env && .venv/bin/deal-hunter run --once --max-items 30'"

# Dashboard via SSH tunnel (if NSG not open)
ssh -L 8081:localhost:8081 azure-test
# Then open http://localhost:8081
```

## Comps Status

Both comps sources are currently non-functional on Azure:
- **nadlan_gov** — SSL handshake failure (Israeli gov APIs block Azure IPs)
- **yad2_deals** — Deals table requires JS rendering (not in SSR HTML)

Scoring falls back to `MARKET_REFS` bands. `fair_price_estimate` is always `None`.

## Testing

```bash
pytest tests/
```

Test fixtures in `tests/fixtures/` include Yad2, OnMap, Ad, and nadlan.gov.il sample data.
