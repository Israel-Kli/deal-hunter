"""SQLite repository for listings, price_history, comps, scan_log.

Kept intentionally small — a dict-shaped surface over sqlite3. No ORM.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from deal_hunter.dates import earliest_yyyy_mm_dd
from deal_hunter.effective import (
    effective_garden_sqm,
    effective_lot_sqm,
    effective_price_per_sqm,
    effective_rooms,
    effective_sqm,
    effective_sqm_build,
    effective_units,
)
from deal_hunter.models import Listing, ScanResult

log = logging.getLogger(__name__)

_UNSET = object()

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


class ListingsRepo:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()
        self.conn.commit()
        log.debug("Opened DB: %s", self.path)

    def _migrate(self) -> None:
        cur = self.conn.execute("PRAGMA table_info(listings)")
        columns = {row["name"] for row in cur.fetchall()}
        added: list[str] = []
        if "sqm_build" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN sqm_build INTEGER")
            added.append("sqm_build")
        if "first_listed_date" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN first_listed_date TEXT DEFAULT ''")
            added.append("first_listed_date")
        if "is_favorite" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0")
            added.append("is_favorite")
        if "user_notes" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN user_notes TEXT NOT NULL DEFAULT ''")
            added.append("user_notes")
        for col in ("units_count", "garden_sqm", "lot_sqm"):
            if col not in columns:
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col} INTEGER")
                added.append(col)
        if "rooms_user" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN rooms_user REAL")
            added.append("rooms_user")
        for col in ("sqm_user", "sqm_build_user", "units_count_user", "garden_sqm_user", "lot_sqm_user"):
            if col not in columns:
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col} INTEGER")
                added.append(col)
        if "year_built" not in columns:
            self.conn.execute("ALTER TABLE listings ADD COLUMN year_built INTEGER")
            added.append("year_built")
        if added:
            log.info("DB migration: added columns %s", added)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ListingsRepo":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ---- listings ------------------------------------------------------

    def get(self, source: str, source_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        return dict(row) if row else None

    def upsert(self, listing: Listing) -> tuple[bool, int | None]:
        """Insert or update. Returns (is_new, previous_price_if_changed)."""
        now = datetime.utcnow().isoformat()
        existing = self.get(listing.source, listing.source_id)
        prev_price: int | None = None
        is_new = existing is None

        incoming_first = earliest_yyyy_mm_dd(
            listing.first_listed_date,
            listing.publish_date,
        )
        if existing:
            listing.first_listed_date = earliest_yyyy_mm_dd(
                incoming_first,
                existing.get("first_listed_date") or "",
                existing.get("publish_date") or "",
            )
        else:
            listing.first_listed_date = incoming_first

        if is_new:
            listing.first_seen_at = listing.first_seen_at or datetime.utcnow()
            listing.last_seen_at = datetime.utcnow()
        else:
            listing.first_seen_at = (
                datetime.fromisoformat(existing["first_seen_at"])
                if existing.get("first_seen_at")
                else datetime.utcnow()
            )
            listing.last_seen_at = datetime.utcnow()
            if existing.get("price") and existing["price"] != listing.price:
                prev_price = existing["price"]

        log.debug(
            "UPSERT %s:%s => %s",
            listing.source, listing.source_id,
            listing.model_dump_json(ensure_ascii=False),
        )

        self.conn.execute(
            """INSERT INTO listings (
                source, source_id, url,
                city, neighborhood, street, house_number, address,
                rooms, sqm, sqm_build, floor,
                price, price_before, price_per_sqm,
                listing_type, is_agent,
                parking, elevator, balcony, ac, mamad, renovated,
                description, images_json, tags_json, lat, lon,
                publish_date, first_listed_date, first_seen_at, last_seen_at,
                canonical_id,
                fair_price_estimate, fair_price_low, fair_price_high,
                score, score_reasons, source_payload,
                is_favorite, user_notes,
                units_count, garden_sqm, lot_sqm,
                rooms_user,
                sqm_user, sqm_build_user, units_count_user, garden_sqm_user, lot_sqm_user,
                year_built
            ) VALUES (
                ?,?,?, ?,?,?,?,?,
                ?,?,?,?,
                ?,?,?,
                ?,?,
                ?,?,?,?,?,?,
                ?,?,?,?,?,
                ?,?,?,?,
                ?,
                ?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,
                ?,
                ?,?,?,?,
                ?
            )
            ON CONFLICT(source, source_id) DO UPDATE SET
                url=excluded.url,
                city=excluded.city,
                neighborhood=excluded.neighborhood,
                street=excluded.street,
                house_number=excluded.house_number,
                address=excluded.address,
                rooms=excluded.rooms,
                sqm=excluded.sqm,
                sqm_build=excluded.sqm_build,
                floor=excluded.floor,
                price=excluded.price,
                price_before=excluded.price_before,
                price_per_sqm=excluded.price_per_sqm,
                listing_type=excluded.listing_type,
                is_agent=excluded.is_agent,
                parking=excluded.parking,
                elevator=excluded.elevator,
                balcony=excluded.balcony,
                ac=excluded.ac,
                mamad=excluded.mamad,
                renovated=excluded.renovated,
                description=excluded.description,
                images_json=excluded.images_json,
                tags_json=excluded.tags_json,
                lat=excluded.lat,
                lon=excluded.lon,
                publish_date=excluded.publish_date,
                first_listed_date=excluded.first_listed_date,
                last_seen_at=excluded.last_seen_at,
                canonical_id=excluded.canonical_id,
                fair_price_estimate=excluded.fair_price_estimate,
                fair_price_low=excluded.fair_price_low,
                fair_price_high=excluded.fair_price_high,
                score=excluded.score,
                score_reasons=excluded.score_reasons,
                source_payload=excluded.source_payload,
                units_count=excluded.units_count,
                garden_sqm=excluded.garden_sqm,
                lot_sqm=excluded.lot_sqm,
                year_built=excluded.year_built
            """,
            (
                listing.source, listing.source_id, listing.url,
                listing.city, listing.neighborhood, listing.street, listing.house_number, listing.address,
                listing.rooms, listing.sqm, listing.sqm_build, listing.floor,
                listing.price, listing.price_before, listing.price_per_sqm,
                listing.listing_type, int(listing.is_agent),
                int(listing.parking), int(listing.elevator), int(listing.balcony),
                int(listing.ac), int(listing.mamad), int(listing.renovated),
                listing.description,
                json.dumps(listing.images, ensure_ascii=False),
                json.dumps(listing.tags, ensure_ascii=False),
                listing.lat, listing.lon,
                listing.publish_date,
                listing.first_listed_date,
                listing.first_seen_at.isoformat(), listing.last_seen_at.isoformat(),
                listing.canonical_id,
                listing.fair_price_estimate, listing.fair_price_low, listing.fair_price_high,
                listing.score,
                json.dumps(listing.score_reasons, ensure_ascii=False),
                json.dumps(listing.source_payload, ensure_ascii=False),
                0,
                "",
                listing.units_count, listing.garden_sqm, listing.lot_sqm,
                listing.rooms_user,
                listing.sqm_user, listing.sqm_build_user,
                listing.units_count_user, listing.garden_sqm_user,
                listing.lot_sqm_user,
                listing.year_built,
            ),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO price_history (source, source_id, ts, price) VALUES (?,?,?,?)",
            (listing.source, listing.source_id, now, listing.price),
        )
        self.conn.commit()
        return is_new, prev_price

    def update_user_fields(
        self,
        source: str,
        source_id: str,
        *,
        is_favorite: bool | None = None,
        user_notes: str | None = None,
        rooms_user: float | None = _UNSET,
        sqm_user: int | None = _UNSET,
        sqm_build_user: int | None = _UNSET,
        units_count_user: int | None = _UNSET,
        garden_sqm_user: int | None = _UNSET,
        lot_sqm_user: int | None = _UNSET,
    ) -> bool:
        """Update dashboard-only fields including overrides.
        Pass a value to set, None to clear override, omit to leave unchanged.
        """
        sets: list[str] = []
        args: list[Any] = []
        if is_favorite is not None:
            sets.append("is_favorite=?")
            args.append(1 if is_favorite else 0)
        if user_notes is not None:
            sets.append("user_notes=?")
            args.append(user_notes[:2000])

        override_map = {
            "rooms_user": rooms_user,
            "sqm_user": sqm_user,
            "sqm_build_user": sqm_build_user,
            "units_count_user": units_count_user,
            "garden_sqm_user": garden_sqm_user,
            "lot_sqm_user": lot_sqm_user,
        }
        for col, val in override_map.items():
            if val is not _UNSET:
                sets.append(f"{col}=?")
                args.append(val)

        if not sets:
            return False
        args.extend([source, source_id])
        cur = self.conn.execute(
            f"UPDATE listings SET {', '.join(sets)} WHERE source=? AND source_id=?",
            args,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_dict(self, source: str, source_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()
        return dict(row) if row else None

    def all_for_dashboard(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM listings "
            "WHERE floor IS NULL OR floor <= 1 "
            "ORDER BY score DESC NULLS LAST, first_seen_at DESC"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["images"] = json.loads(d.get("images_json") or "[]")
            d["tags"] = json.loads(d.get("tags_json") or "[]")
            d["score_reasons"] = json.loads(d.get("score_reasons") or "{}")
            for bf in ("parking", "elevator", "balcony", "ac", "mamad", "renovated", "is_agent", "is_favorite"):
                d[bf] = bool(d.get(bf))
            d["user_notes"] = d.get("user_notes") or ""
            fl = (d.get("first_listed_date") or "").strip()
            pub = (d.get("publish_date") or "").strip()
            if not fl and pub:
                d["first_listed_date"] = pub
            d.pop("images_json", None)
            d.pop("tags_json", None)
            d.pop("source_payload", None)
            d["rooms_eff"] = effective_rooms(d)
            d["sqm_eff"] = effective_sqm(d)
            d["sqm_build_eff"] = effective_sqm_build(d)
            d["units_count_eff"] = effective_units(d)
            d["garden_sqm_eff"] = effective_garden_sqm(d)
            d["lot_sqm_eff"] = effective_lot_sqm(d)
            d["price_per_sqm_eff"] = effective_price_per_sqm(d)
            yb = d.get("year_built")
            if yb and isinstance(yb, (int, float)):
                from datetime import datetime
                d["building_age"] = datetime.utcnow().year - int(yb)
            else:
                d["building_age"] = None
            out.append(d)
        return out

    def delete_listing(self, source: str, source_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM listings WHERE source=? AND source_id=?",
            (source, source_id),
        )
        self.conn.execute(
            "DELETE FROM price_history WHERE source=? AND source_id=?",
            (source, source_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def purge_older_than(self, cutoff_iso: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM listings WHERE first_seen_at < ? AND (publish_date = '' OR publish_date < ?)",
            (cutoff_iso, cutoff_iso[:10]),
        )
        self.conn.commit()
        return cur.rowcount

    def reset_all(self) -> dict[str, int]:
        deleted_listings = self.conn.execute("DELETE FROM listings").rowcount
        deleted_history = self.conn.execute("DELETE FROM price_history").rowcount
        deleted_comps = self.conn.execute("DELETE FROM comps").rowcount
        deleted_scan_log = self.conn.execute("DELETE FROM scan_log").rowcount
        self.conn.commit()
        self.conn.execute("VACUUM")
        log.info(
            "DB reset: %d listings, %d price_history, %d comps, %d scan_log",
            deleted_listings, deleted_history, deleted_comps, deleted_scan_log,
        )
        return {
            "listings": deleted_listings,
            "price_history": deleted_history,
            "comps": deleted_comps,
            "scan_log": deleted_scan_log,
        }

    def reset_source(self, source: str) -> dict[str, int]:
        deleted_listings = self.conn.execute(
            "DELETE FROM listings WHERE source=?", (source,)
        ).rowcount
        deleted_history = self.conn.execute(
            "DELETE FROM price_history WHERE source=?", (source,)
        ).rowcount
        deleted_scan_log = self.conn.execute(
            "DELETE FROM scan_log WHERE source=?", (source,)
        ).rowcount
        self.conn.commit()
        log.info(
            "DB reset source=%s: %d listings, %d price_history, %d scan_log",
            source, deleted_listings, deleted_history, deleted_scan_log,
        )
        return {
            "listings": deleted_listings,
            "price_history": deleted_history,
            "scan_log": deleted_scan_log,
        }

    # ---- comps ---------------------------------------------------------

    def upsert_comps(self, provider: str, comps: Iterable[dict[str, Any]]) -> int:
        now = datetime.utcnow().isoformat()
        n = 0
        for c in comps:
            try:
                self.conn.execute(
                    """INSERT OR REPLACE INTO comps
                    (provider, address_hash, deal_date, price, sqm, rooms,
                     city, neighborhood, street, house_number, year_built, raw, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        provider, c["address_hash"], c["deal_date"], c["price"],
                        c.get("sqm"), c.get("rooms"),
                        c.get("city", ""), c.get("neighborhood", ""),
                        c.get("street", ""), c.get("house_number", ""),
                        c.get("year_built"), json.dumps(c.get("raw", {}), ensure_ascii=False),
                        now,
                    ),
                )
                n += 1
            except Exception as e:
                log.warning("upsert_comps: skipping row for %s: %s", provider, e)
                continue
        self.conn.commit()
        return n

    # ---- scan log ------------------------------------------------------

    def log_scan(self, result: ScanResult) -> None:
        self.conn.execute(
            """INSERT INTO scan_log (ts, source, fetched, new, updated, price_drops, alerted, duration_sec, errors)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                datetime.utcnow().isoformat(),
                result.source, result.fetched, result.new, result.updated,
                result.price_drops, result.alerted, result.duration_sec,
                json.dumps(result.errors, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        log.debug("logged scan: %s %d fetched", result.source, result.fetched)

    # ---- dedup ---------------------------------------------------------

    def list_uncanonical(self) -> list[dict[str, Any]]:
        """Return all listings that lack a canonical_id."""
        rows = self.conn.execute(
            "SELECT source, source_id, city, neighborhood, street, "
            "house_number, address, rooms, sqm, price "
            "FROM listings WHERE canonical_id IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_canonical_ids(self, assignments: list[tuple[str, str, str]]) -> int:
        """Batch-update canonical_id for (source, source_id, canonical_id) tuples.

        Returns the number of rows updated.
        """
        if not assignments:
            return 0
        self.conn.executemany(
            "UPDATE listings SET canonical_id=? WHERE source=? AND source_id=?",
            [(cid, src, sid) for cid, src, sid in assignments],
        )
        self.conn.commit()
        return self.conn.total_changes  # approximate; caller should track
