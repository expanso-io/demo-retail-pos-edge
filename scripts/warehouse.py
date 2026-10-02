#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The data warehouse behind the DMZ: a SQLite sink and its read API.

Each store's pos-uplink job POSTs JSON arrays of shareable records to
/ingest through that store's WAN link. A record is stored once per txn_id;
a redelivery after a link cut is counted, not stored twice.

The warehouse runs its own card-number check, independent of the edge: every
string in every stored row is scanned for a Luhn-valid 13 to 19 digit run.
The board shows that count; it should always read zero.

    uv run -s scripts/warehouse.py --port 8026 --db .runtime/warehouse.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  txn_id TEXT PRIMARY KEY NOT NULL,
  join_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  register_id TEXT NOT NULL,
  total_cents INTEGER,
  temp_c REAL,
  sensor TEXT,
  edge_at TEXT,
  received_at REAL NOT NULL,
  queued_s REAL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_join ON records(join_id);
CREATE INDEX IF NOT EXISTS records_store ON records(store_id);
"""


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def card_numbers_in(obj: object) -> list[str]:
    """Every Luhn-valid card-like digit run in any string inside obj."""
    hits = []
    if isinstance(obj, dict):
        for v in obj.values():
            hits += card_numbers_in(v)
    elif isinstance(obj, list):
        for v in obj:
            hits += card_numbers_in(v)
    elif isinstance(obj, str):
        for m in CARD.finditer(obj):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                hits.append(digits)
    return hits


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Warehouse:
    def __init__(self, db: Path, profiles: Path):
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.duplicates = 0
        self.rejected = 0
        self.batches = 0
        self.bytes_in = 0
        self.card_hits = 0
        self.rows_scanned = 0
        self.per_store_batches: dict[str, int] = {}
        self.profiles_path = profiles
        self.online: set[str] = set()
        self.load_profiles()
        self.rescan()

    def load_profiles(self) -> None:
        try:
            self.online = set(json.loads(self.profiles_path.read_text())["join_ids"])
        except (OSError, ValueError, KeyError):
            self.online = set()

    def rescan(self) -> None:
        with self.lock:
            rows = self.conn.execute("SELECT body FROM records").fetchall()
            self.rows_scanned = len(rows)
            self.card_hits = sum(len(card_numbers_in(json.loads(b))) for (b,) in rows)

    def ingest(self, payload: bytes) -> int:
        data = json.loads(payload)
        records = data if isinstance(data, list) else [data]
        now = time.time()
        new = 0
        with self.lock:
            self.batches += 1
            self.bytes_in += len(payload)
            for rec in records:
                if not isinstance(rec, dict) or not rec.get("txn_id") or not rec.get("join_id"):
                    self.rejected += 1
                    continue
                ctx = rec.get("context", {})
                uplink = rec.get("uplink", {})
                queued_at = parse_ts(uplink.get("queued_at"))
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rec.get("txn_id"), rec.get("join_id", ""), ctx.get("store_id", "?"),
                     ctx.get("register_id", "?"), rec.get("total_cents"), ctx.get("temp_c"),
                     ctx.get("sensor"), rec.get("edge_at"), now,
                     None if queued_at is None else round(now - queued_at, 2),
                     json.dumps(rec)))
                if cur.rowcount:
                    new += 1
                    self.rows_scanned += 1
                    self.card_hits += len(card_numbers_in(rec))
                else:
                    self.duplicates += 1
            store = records[0].get("context", {}).get("store_id", "?") if records else "?"
            self.per_store_batches[store] = self.per_store_batches.get(store, 0) + 1
            self.conn.commit()
        return new

    def stats(self) -> dict:
        if not self.online:
            self.load_profiles()
        with self.lock:
            q = self.conn.execute
            total = q("SELECT count(*) FROM records").fetchone()[0]
            per_store = {s: {"rows": n, "last_received": r, "max_queued_s": mq}
                         for s, n, r, mq in q("SELECT store_id, count(*), max(received_at), "
                                                "max(queued_s) FROM records GROUP BY store_id")}
            join_ids = [j for (j,) in q("SELECT DISTINCT join_id FROM records")]
            multi = q("SELECT count(*) FROM (SELECT join_id FROM records GROUP BY join_id "
                      "HAVING count(DISTINCT store_id) > 1)").fetchone()[0]
            countries = q("SELECT count(DISTINCT json_extract(body, '$.context.country')) "
                          "FROM records").fetchone()[0]
            no_temp = q("SELECT count(*) FROM records WHERE sensor != 'ok'").fetchone()[0]
            recent = [json.loads(b) for (b,) in
                      q("SELECT body FROM records ORDER BY received_at DESC, rowid DESC "
                        "LIMIT 12")]
            return {
                "at": time.time(),
                "rows": total,
                "batches": self.batches,
                "bytes_in": self.bytes_in,
                "duplicates_ignored": self.duplicates,
                "rejected": self.rejected,
                "per_store": per_store,
                "shoppers": len(join_ids),
                "shoppers_multi_store": multi,
                "online_profiles": len(self.online),
                "online_matched": len(self.online.intersection(join_ids)),
                "countries": countries,
                "rows_without_temperature": no_temp,
                "card_scan": {"rows_scanned": self.rows_scanned, "card_numbers": self.card_hits},
                "recent": recent,
            }

    def record(self, txn_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT body, received_at, queued_s FROM records "
                                    "WHERE txn_id = ?", (txn_id,)).fetchone()
        if not row:
            return None
        return {"record": json.loads(row[0]), "received_at": row[1], "queued_s": row[2]}

    def reset(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM records")
            self.conn.commit()
            self.duplicates = self.batches = self.bytes_in = self.rejected = 0
            self.card_hits = self.rows_scanned = 0
            self.per_store_batches.clear()
        self.load_profiles()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8026)
    ap.add_argument("--db", default=str(ROOT / ".runtime" / "warehouse.db"))
    ap.add_argument("--profiles", default=str(ROOT / ".runtime" / "online-profiles.json"))
    args = ap.parse_args()
    wh = Warehouse(Path(args.db), Path(args.profiles))

    class H(BaseHTTPRequestHandler):
        def reply(self, code: int, obj: object) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            n = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(n)
            if self.path == "/ingest":
                try:
                    self.reply(200, {"stored": wh.ingest(payload)})
                except (ValueError, sqlite3.Error) as exc:
                    self.reply(400, {"error": str(exc)})
            elif self.path == "/reset":
                wh.reset()
                self.reply(200, {"reset": True})
            else:
                self.reply(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/stats":
                self.reply(200, wh.stats())
            elif self.path.startswith("/record/"):
                rec = wh.record(self.path[8:])
                self.reply(200 if rec else 404, rec or {"error": "not found"})
            else:
                self.reply(404, {"error": "not found"})

        def log_message(self, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    print(f"warehouse on 127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
