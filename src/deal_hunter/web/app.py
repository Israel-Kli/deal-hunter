"""Minimal HTTP dashboard server. stdlib only. Serves dashboard.html + /api/listings JSON."""

from __future__ import annotations

import http.server
import json
import logging
from pathlib import Path

from deal_hunter.config import Config
from deal_hunter.repo.listings_repo import ListingsRepo

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).parent / "templates"


def _make_handler(cfg: Config):
    db_path = Path(cfg.data_dir) / "deal-hunter.db"

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(TEMPLATES), **kw)

        def do_GET(self):
            if self.path == "/api/listings":
                with ListingsRepo(db_path) as repo:
                    rows = repo.all_for_dashboard()
                payload = {"count": len(rows), "listings": rows}
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

        def log_message(self, fmt, *args):
            log.info("http " + fmt, *args)

    return H


def serve(cfg: Config, host: str = "127.0.0.1") -> None:
    handler = _make_handler(cfg)
    server = http.server.HTTPServer((host, cfg.dashboard_port), handler)
    log.info("Dashboard → http://%s:%d", host, cfg.dashboard_port)
    server.serve_forever()
