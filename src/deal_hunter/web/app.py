"""Minimal HTTP dashboard server. stdlib only. Serves dashboard.html + /api/listings JSON."""

from __future__ import annotations

import threading
import http.server
import json
import logging
from pathlib import Path

from deal_hunter.config import Config
from deal_hunter.dedup.canonicalizer import load_existing_groups
from deal_hunter.repo.listings_repo import ListingsRepo

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
            if self.path == "/api/scan":
                if _scan_in_progress[0]:
                    body = json.dumps({"status": "error", "error": "Scan already in progress"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(409)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                def _run_scan():
                    _scan_in_progress[0] = True
                    try:
                        from deal_hunter.cli import run_once
                        alerts = run_once(cfg, enrich=False)
                        log.info("Manual scan completed: %d alerts", alerts)
                    except Exception as e:
                        log.exception("Manual scan failed")
                    finally:
                        _scan_in_progress[0] = False
                threading.Thread(target=_run_scan, daemon=True).start()
                payload = {"status": "ok", "summary": "Scan started. Refresh page when done."}
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

        def log_message(self, fmt, *args):
            log.info("http " + fmt, *args)

    return H


def serve(cfg: Config, host: str = "127.0.0.1") -> None:
    handler = _make_handler(cfg)
    server = http.server.HTTPServer((host, cfg.dashboard_port), handler)
    log.info("Dashboard → http://%s:%d", host, cfg.dashboard_port)
    server.serve_forever()
