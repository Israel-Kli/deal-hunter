# deal-hunter

Multi-source monitor for **undervalued residential for-sale listings** in Israel.

Pulls listings from Yad2, Madlan, OnMap, ad.co.il on a schedule; scores each against
a heuristic fair-price signal built from Yad2 Deals API and nadlan.gov.il closed deals;
sends Telegram alerts with direct ad links; serves a dashboard for browsing.

**Personal use only.** Respect each site's ToS; keep the crawler polite; single tenant.

## Status

Greenfield build. See `../. claude/plans/see-my-requirements-as-mutable-volcano.md`
for the full plan and milestones.

The `_upstream/` directory is a read-only reference copy of
[Eilons/realestate-opportunity-finder](https://github.com/Eilons/realestate-opportunity-finder),
from which the Yad2 fetcher, scoring formula, and dashboard UI were ported.

## Layout

```
src/deal_hunter/
  adapters/       # source scrapers behind a ScraperAdapter protocol
  comps/          # closed-deals providers behind a CompsProvider protocol
  normalize/      # Hebrew normalization, sqm buckets
  valuation/      # fair-price heuristic from comps
  scoring/        # heuristic investment scorer
  dedup/          # cross-source canonicalizer
  repo/           # SQLite schema + repository
  notify/         # Telegram sender
  web/            # dashboard http server + templates
  cli.py          # entrypoint
```

## Run (dev)

```
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp configs/config.example.json configs/config.json  # edit filters
deal-hunter run --once --dry-run
```
