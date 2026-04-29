"""Minimal HTTP dashboard server. stdlib only. Serves dashboard.html + /api/listings JSON."""

from __future__ import annotations

import threading
import http.server
import json
import logging
from pathlib import Path

from deal_hunter.config import Config
from deal_hunter.dedup.canonicalizer import load_existing_groups
from deal_hunter.effective import (
    effective_garden_sqm,
    effective_lot_sqm,
    effective_price_per_sqm,
    effective_rooms,
    effective_sqm,
    effective_sqm_build,
    effective_units,
)
from deal_hunter.models import Listing
from deal_hunter.repo.listings_repo import ListingsRepo
from deal_hunter.scoring.heuristic import score_listing

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).parent / "templates"


def _make_handler(cfg: Config):
    db_path = Path(cfg.data_dir) / "deal-hunter.db"
    _scan_lock = threading.Lock()
    _scan_in_progress = [False]

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(TEMPLATES), **kw)

        def do_GET(self):
            if self.path == "/api/listings":
                with ListingsRepo(db_path) as repo:
                    rows = repo.all_for_dashboard()
                    last_updated = repo.conn.execute(
                        "SELECT MAX(last_seen_at) FROM listings"
                    ).fetchone()[0]
                payload = {
                    "count": len(rows),
                    "listings": rows,
                    "last_updated": last_updated,
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/dedup":
                with ListingsRepo(db_path) as repo:
                    groups = load_existing_groups(repo.conn)
                payload = {
                    "groups": [
                        {
                            "canonical_id": g.canonical_id,
                            "city": g.city,
                            "street": g.street_normalized,
                            "house_number": g.house_number,
                            "rooms_b": g.rooms_b,
                            "sqm_b": g.sqm_b,
                            "member_count": len(g.members),
                            "price_spread": g.price_spread,
                            "cheapest_price": g.cheapest.price if g.cheapest else None,
                            "cheapest_source": g.cheapest.source if g.cheapest else None,
                            "cheapest_url": g.cheapest.url if g.cheapest else None,
                            "sources": list({m.source for m in g.members}),
                        }
                        for g in sorted(
                            groups.values(),
                            key=lambda g: g.price_spread,
                            reverse=True,
                        )
                        if len(g.members) > 1
                    ],
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/healthz":
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
                return
            if self.path == "/":
                self.path = "/dashboard.html"
            return super().do_GET()

        def _write_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/api/listing/user":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(400, {"status": "error", "error": "Invalid JSON"})
                    return
                source = data.get("source")
                source_id = data.get("source_id")
                if not source or not source_id:
                    self._write_json(400, {"status": "error", "error": "source and source_id required"})
                    return
                fav = data.get("is_favorite")
                notes = data.get("user_notes")
                if fav is None and notes is None:
                    self._write_json(400, {"status": "error", "error": "is_favorite and/or user_notes required"})
                    return
                is_f = None
                if fav is not None:
                    is_f = bool(fav)
                notes_s = None
                if notes is not None:
                    if not isinstance(notes, str):
                        self._write_json(400, {"status": "error", "error": "user_notes must be a string"})
                        return
                    notes_s = notes[:2000]
                with ListingsRepo(db_path) as repo:
                    ok = repo.update_user_fields(
                        str(source), str(source_id), is_favorite=is_f, user_notes=notes_s
                    )
                if not ok:
                    self._write_json(404, {"status": "error", "error": "Listing not found"})
                    return
                self._write_json(200, {"status": "ok", "ok": True})
                return
            if self.path == "/api/listing/edit":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(400, {"status": "error", "error": "Invalid JSON"})
                    return
                source = data.get("source")
                source_id = data.get("source_id")
                if not source or not source_id:
                    self._write_json(400, {"status": "error", "error": "source and source_id required"})
                    return
                with ListingsRepo(db_path) as repo:
                    row = repo.get_dict(str(source), str(source_id))
                    if not row:
                        self._write_json(404, {"status": "error", "error": "Listing not found"})
                        return
                    overrides: dict = {}
                    for col in ("rooms_user", "sqm_user", "sqm_build_user", "units_count_user", "garden_sqm_user"):
                        if col in data:
                            val = data[col]
                            if val is not None:
                                try:
                                    val = float(val) if col == "rooms_user" else int(val)
                                except (ValueError, TypeError):
                                    self._write_json(400, {"status": "error", "error": f"{col} must be a number or null"})
                                    return
                            overrides[col] = val
                    ok = repo.update_user_fields(str(source), str(source_id), **overrides)
                    row = repo.get_dict(str(source), str(source_id))
                    listing_for_score = Listing(
                        source=row["source"],
                        source_id=row["source_id"],
                        url=row.get("url") or "",
                        city=row.get("city") or "",
                        neighborhood=row.get("neighborhood") or "",
                        street=row.get("street") or "",
                        house_number=row.get("house_number") or "",
                        address=row.get("address") or "",
                        rooms=row.get("rooms"),
                        sqm=row.get("sqm"),
                        sqm_build=row.get("sqm_build"),
                        floor=row.get("floor"),
                        price=row["price"],
                        price_before=row.get("price_before"),
                        price_per_sqm=row.get("price_per_sqm"),
                        listing_type=row.get("listing_type") or "",
                        is_agent=bool(row.get("is_agent")),
                        parking=bool(row.get("parking")),
                        elevator=bool(row.get("elevator")),
                        balcony=bool(row.get("balcony")),
                        ac=bool(row.get("ac")),
                        mamad=bool(row.get("mamad")),
                        renovated=bool(row.get("renovated")),
                        description=row.get("description") or "",
                        tags=json.loads(row.get("tags_json") or "[]"),
                        lat=row.get("lat"),
                        lon=row.get("lon"),
                        publish_date=row.get("publish_date") or "",
                        first_listed_date=row.get("first_listed_date") or "",
                        is_favorite=bool(row.get("is_favorite")),
                        user_notes=row.get("user_notes") or "",
                        rooms_user=row.get("rooms_user"),
                        sqm_user=row.get("sqm_user"),
                        sqm_build_user=row.get("sqm_build_user"),
                        units_count_user=row.get("units_count_user"),
                        garden_sqm_user=row.get("garden_sqm_user"),
                        units_count=row.get("units_count"),
                        garden_sqm=row.get("garden_sqm"),
                        lot_sqm=row.get("lot_sqm"),
                        year_built=row.get("year_built"),
                    )
                    new_score, new_reasons = score_listing(listing_for_score)
                    repo.conn.execute(
                        "UPDATE listings SET score=?, score_reasons=? WHERE source=? AND source_id=?",
                        (new_score, json.dumps(new_reasons, ensure_ascii=False), str(source), str(source_id)),
                    )
                    repo.conn.commit()
                body = json.dumps({
                    "status": "ok",
                    "ok": True,
                    "score": new_score,
                    "score_reasons": new_reasons,
                    "rooms_eff": effective_rooms(row),
                    "sqm_eff": effective_sqm(row),
                    "sqm_build_eff": effective_sqm_build(row),
                    "units_count_eff": effective_units(row),
                    "garden_sqm_eff": effective_garden_sqm(row),
                    "lot_sqm_eff": effective_lot_sqm(row),
                    "price_per_sqm_eff": effective_price_per_sqm(row),
                    "year_built": row.get("year_built"),
                    "building_age": new_reasons.get("building_age"),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/reset":
                with ListingsRepo(db_path) as repo:
                    deleted = repo.reset_all()
                payload = {"status": "ok", "deleted": deleted}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/reset-source":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(400, {"status": "error", "error": "Invalid JSON"})
                    return
                source = data.get("source")
                if not source:
                    self._write_json(400, {"status": "error", "error": "source required"})
                    return
                with ListingsRepo(db_path) as repo:
                    deleted = repo.reset_source(str(source))
                payload = {"status": "ok", "source": source, "deleted": deleted}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/scan":
                if _scan_in_progress[0]:
                    body = json.dumps({"status": "error", "error": "Scan already in progress"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                scan_source = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > 0:
                    raw = self.rfile.read(length)
                    try:
                        data = json.loads(raw.decode("utf-8"))
                        scan_source = data.get("source")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                def _run_scan(_src=scan_source):
                    _scan_in_progress[0] = True
                    try:
                        from deal_hunter.cli import run_once
                        alerts = run_once(cfg, enrich=False, source=_src)
                        log.info("Manual scan completed: %d alerts", alerts)
                    except Exception:
                        log.exception("Manual scan failed")
                    finally:
                        _scan_in_progress[0] = False
                threading.Thread(target=_run_scan, daemon=True).start()
                summary = "Scan started for " + (scan_source or "all sources") + ". Refresh page when done."
                payload = {"status": "ok", "summary": summary}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if self.path == "/api/listing/delete":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._write_json(400, {"status": "error", "error": "Invalid JSON"})
                    return
                source = data.get("source")
                source_id = data.get("source_id")
                if not source or not source_id:
                    self._write_json(400, {"status": "error", "error": "source and source_id required"})
                    return
                with ListingsRepo(db_path) as repo:
                    ok = repo.delete_listing(str(source), str(source_id))
                if not ok:
                    self._write_json(404, {"status": "error", "error": "Listing not found"})
                    return
                self._write_json(200, {"status": "ok", "ok": True})
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            log.info("http " + fmt, *args)

    return H


def serve(cfg: Config, host: str = "127.0.0.1") -> None:
    handler = _make_handler(cfg)
    server = http.server.HTTPServer((host, cfg.dashboard_port), handler)
    log.info("Dashboard → http://%s:%d", host, cfg.dashboard_port)
    server.serve_forever()
