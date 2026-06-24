"""End-to-end-ish test for the freebies runner: scrape→dedup→alert."""

from __future__ import annotations

import os
from pathlib import Path

from deal_hunter import config as cfg_mod
from deal_hunter.freebies import runner
from deal_hunter.freebies.repo import FreebiesRepo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "agora_search.html"


def _cfg(tmp_path: Path) -> cfg_mod.Config:
    return cfg_mod.Config(
        data_dir=str(tmp_path),
        freebies=cfg_mod.FreebiesCfg(
            enabled=True,
            watches=[
                cfg_mod.FreebieWatchCfg(
                    label="מייבש גוש דן",
                    source="agora",
                    keyword="מייבש",
                    city="גוש דן והמרכז",
                    condition=2,
                ),
            ],
        ),
    )


def _patch_fetch(monkeypatch):
    """Force agora.fetch_items to return the fixture-parsed items."""
    html = FIXTURE.read_text(encoding="utf-8")
    from deal_hunter.freebies import agora

    def _stub(*, watch_label: str, **_kw):
        return agora.parse_items(html, watch_label=watch_label)

    monkeypatch.setattr(agora, "fetch_items", _stub)


def test_first_run_alerts_all_items_second_run_alerts_none(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    cfg = _cfg(tmp_path)

    sent_first = runner.run_once(cfg)
    assert sent_first == 10

    sent_second = runner.run_once(cfg)
    assert sent_second == 0

    with FreebiesRepo(Path(cfg.data_dir) / "deal-hunter.db") as repo:
        count = repo.conn.execute("SELECT COUNT(*) FROM freebie_items").fetchone()[0]
        assert count == 10
        alerted = repo.conn.execute(
            "SELECT COUNT(*) FROM freebie_items WHERE alerted_at IS NOT NULL"
        ).fetchone()[0]
        assert alerted == 10
        scans = repo.conn.execute("SELECT COUNT(*) FROM freebie_scan_log").fetchone()[0]
        assert scans == 2


def test_seed_only_inserts_items_without_alerting(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "1")
    cfg = _cfg(tmp_path)

    sent = runner.run_once(cfg, seed_only=True)
    assert sent == 0

    with FreebiesRepo(Path(cfg.data_dir) / "deal-hunter.db") as repo:
        count = repo.conn.execute("SELECT COUNT(*) FROM freebie_items").fetchone()[0]
        assert count == 10
        # All seeded rows are marked as already alerted so the next real run is silent.
        alerted = repo.conn.execute(
            "SELECT COUNT(*) FROM freebie_items WHERE alerted_at IS NOT NULL"
        ).fetchone()[0]
        assert alerted == 10

    # A subsequent normal run sees no new items.
    sent2 = runner.run_once(cfg)
    assert sent2 == 0


def test_disabled_freebies_short_circuits(tmp_path, monkeypatch):
    _patch_fetch(monkeypatch)
    cfg = cfg_mod.Config(
        data_dir=str(tmp_path),
        freebies=cfg_mod.FreebiesCfg(enabled=False, watches=[]),
    )
    assert runner.run_once(cfg) == 0
    # No DB file should be created.
    assert not (Path(cfg.data_dir) / "deal-hunter.db").exists()
