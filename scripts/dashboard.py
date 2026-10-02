#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Serve the dashboard and /api/state for demo-retail-pos-edge.

Stdlib only. Replace build_state() with real pipeline reads; the shape it
returns is the contract app.js renders.
"""

from __future__ import annotations

import argparse
import json
import random
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"

_rows = 0


def build_state() -> dict:
    """Representative until wired to the real pipeline."""
    global _rows
    src = random.randint(380, 450)
    edge = random.randint(14, 21)
    _rows += random.randint(5, 17)
    return {"src": src, "edge": edge, "rows": _rows, "nodes": 14}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/api/state"):
            body = json.dumps(build_state()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8023)
    ap.add_argument("--check", action="store_true", help="validate state contract and exit")
    args = ap.parse_args()

    if args.check:
        state = build_state()
        assert isinstance(state, dict) and {"src", "edge", "rows", "nodes"} <= state.keys()
        json.dumps(state)
        assert (DASHBOARD / "index.html").is_file()
        print("ok: state contract + dashboard files")
        return 0

    handler = partial(Handler, directory=str(DASHBOARD))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"dashboard on http://localhost:{args.port}")
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
