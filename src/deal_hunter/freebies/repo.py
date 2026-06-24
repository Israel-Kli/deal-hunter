"""Tiny sqlite repo for freebie items. Separate tables from the real-estate schema."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from deal_hunter.freebies.models import FreebieItem

log = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS freebie_items (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    watch_label TEXT NOT NULL,
    title TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    condition INTEGER,
    url TEXT NOT NULL,
    image_url TEXT,
    posted_at TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    alerted_at TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS freebie_scan_log (
    ts TEXT NOT NULL,
    watch_label TEXT NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT ''
);
"""


class FreebiesRepo:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        log.debug("Opened freebies DB: %s", self.path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "FreebiesRepo":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def exists(self, source: str, source_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM freebie_items WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        return row is not None

    def upsert(self, item: FreebieItem, *, mark_alerted: bool = False) -> bool:
        """Insert if new, otherwise update last_seen_at. Returns True if newly inserted."""
        now = datetime.utcnow().isoformat()
        existing = self.conn.execute(
            "SELECT first_seen_at FROM freebie_items WHERE source=? AND source_id=?",
            (item.source, item.source_id),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """INSERT INTO freebie_items (
                    source, source_id, watch_label, title, city, condition,
                    url, image_url, posted_at,
                    first_seen_at, last_seen_at, alerted_at
                ) VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?)""",
                (
                    item.source, item.source_id, item.watch_label,
                    item.title, item.city, item.condition,
                    item.url, item.image_url, item.posted_at,
                    now, now, now if mark_alerted else None,
                ),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE freebie_items SET last_seen_at=?, watch_label=? "
            "WHERE source=? AND source_id=?",
            (now, item.watch_label, item.source, item.source_id),
        )
        self.conn.commit()
        return False

    def mark_alerted(self, source: str, source_id: str) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "UPDATE freebie_items SET alerted_at=? WHERE source=? AND source_id=?",
            (now, source, source_id),
        )
        self.conn.commit()

    def log_scan(
        self,
        *,
        watch_label: str,
        fetched: int,
        new: int,
        alerted: int,
        errors: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO freebie_scan_log (ts, watch_label, fetched, new, alerted, errors) "
            "VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), watch_label, fetched, new, alerted, errors),
        )
        self.conn.commit()
