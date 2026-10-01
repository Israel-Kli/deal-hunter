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

- **No Docker on the VM** — the 1 GiB Azure VM was too tight; systemd + venv directly
- **SQLite** — single-file DB in `data/deal-hunter.db`
- **stdlib HTTP server** — dashboard uses `http.server`, no Flask/FastAPI dependency
- **APScheduler** — blocking scheduler for periodic scan cycles with jitter

## Scoring System

Heuristic 1-10 score (`scoring/heuristic.py` + `scoring/description_signals.py`):

1. **Price vs market** — smooth piecewise adjustment of ₪/m² vs band; band from comps (`fair_price_estimate`) when available, else `MARKET_REFS` by city/neighborhood.
2. **Description** — bonus for יחידות דיור, יחידת דיור, standalone יחידות / יחידה (word-boundary style), מחולק/מחולקת, apartment — same set as yellow highlights; capped bonus for garden/lot phrases + gradual bonus for many rooms (combined cap).
3. **Physical** — parking, balcony, renovated, ground-floor discount (elevator and mamad are not scored).
4. **Seller** — private listings score higher than broker (`is_agent`).
5. **Negotiation** — price-drop ramp vs previous ask.
6. **Risk** — very high price liquidity penalty only.

Market bands defined in `scoring/heuristic.py:MARKET_REFS`. Add new cities here.

## Deployment

AWS EC2 (Amazon Linux 2023, 2 vCPU, 2 GiB RAM + 2 GiB swap, il-central-1):
- **IP**: `51.85.56.244`
- **SSH**: `ssh aws-ec2` (key-based via `~/.ssh/config`)
- **Path**: `/opt/deal-hunter`, dedicated `deal-hunter` user
- **Python**: 3.14.7 prebuilt at `/opt/py314`; the venv is built from it
- **Services**: `deal-hunter` (scraper), `deal-hunter-dashboard` (port 8081)
- **Memory limits**: scraper `MemoryMax=800M`, dashboard `MemoryMax=300M`

This replaced the previous Azure VM after it stopped working; the migration
followed `docs/deployment.md`.

### SSH Access

Key-based auth configured in `~/.ssh/config`:
```
Host aws-ec2
    HostName 51.85.56.244
    User ec2-user
    IdentityFile ~/.ssh/azure_il
```

### Config

`configs/config.json` on the VM controls:
- `search` — filters (rooms, price, sqm, property_types)
- `cities` — which cities to scrape (Yad2 id/slug, onmap slug, Hebrew name)
- `sources` — enable/disable scrapers (yad2, onmap, ad, komo, …)
- `schedule` — poll interval, request delay, max pages
- `gsheets` / `freebies` / `ai_mapper` — sheet sync, agora watches, Gemini enrichment
- `dashboard_host` — bind address (0.0.0.0 for external access)

### Update Flow (local → VM, no DB reset)

After **any code or template change** you want live on the VM: commit and push from your dev machine, then pull on the VM and restart services. This updates the running app only; it does **not** touch `data/deal-hunter.db` (avoid `/api/reset` and avoid deleting the DB as part of deploy).

**1. Locally (from the repo root)**

```bash
git add -A && git status   # review; exclude anything you must not ship
git commit -m "Describe the change"
git push origin main       # or your branch, then merge to main as you prefer
```

**2. On the VM (install step only if `pyproject.toml` / deps changed)**

```bash
ssh aws-ec2 "sudo -u deal-hunter git -C /opt/deal-hunter pull --ff-only origin main && sudo -u deal-hunter bash -c 'cd /opt/deal-hunter && .venv/bin/python -m pip install -e . --quiet' && sudo systemctl restart deal-hunter deal-hunter-dashboard"
```

If dependencies did not change, you can skip the install:

```bash
ssh aws-ec2 "sudo -u deal-hunter git -C /opt/deal-hunter pull --ff-only origin main && sudo systemctl restart deal-hunter deal-hunter-dashboard"
```

The git checkout is owned by the `deal-hunter` user, so pulls run via
`sudo -u deal-hunter git -C /opt/deal-hunter`. The repo is public, so no deploy
key is needed.

**3. Agent / automation convention**

Whenever changes are merged to the branch the VM tracks: **push → pull on VM → restart** both units so the dashboard and scraper load the new code. Do not deploy by restarting alone without `git pull` on the server.

### Useful Commands

```bash
# Check services
ssh aws-ec2 "sudo systemctl status deal-hunter deal-hunter-dashboard"

# View scraper logs
ssh aws-ec2 "sudo journalctl -u deal-hunter -f --no-pager"

# Trigger manual scan
ssh aws-ec2 "sudo -u deal-hunter bash -c 'cd /opt/deal-hunter && .venv/bin/deal-hunter run --once --max-items 30'"

# Dashboard via SSH tunnel (if the security group is closed)
ssh -L 8081:localhost:8081 aws-ec2
# Then open http://localhost:8081
```

## Comps Status

Both comps sources are disabled in the live config (`nadlan_gov: false`):
- **nadlan_gov** — SSL handshake failure from cloud IPs (Israeli gov APIs blocked Azure IPs)
- **yad2_deals** — Deals table requires JS rendering (not in SSR HTML)

Scoring falls back to `MARKET_REFS` bands. `fair_price_estimate` is always `None`.

## Testing

```bash
.venv/bin/python -m pytest tests/
```

Test fixtures in `tests/fixtures/` include Yad2, OnMap, Ad, and nadlan.gov.il sample data.
