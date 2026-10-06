#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The data warehouse behind the DMZ: a SQLite sink and its read API.

Each store's pos-uplink job POSTs JSON arrays of shareable records to
/ingest through that store's WAN link. The ingest port speaks only TLS 1.3
and requires a client certificate issued by the group's CA (scripts/pki.py);
the certificate's common name is the store, and a batch carrying another
store's records is refused. There is no plaintext ingest and no token to
leak. A record is stored once per txn_id; a redelivery after a link cut is
counted, not stored twice.

The read API for the board (stats, one record, reset) is a separate
listener on the loopback interface and cannot ingest.

The warehouse runs its own card-number check, independent of the edge: every
string in every stored row is scanned for a Luhn-valid 13 to 19 digit run.
The board shows that count; it should always read zero.

    uv run -s scripts/warehouse.py --port 8641 --admin-port 8643 \\
        --tls-cert .secrets/pki/warehouse.pem --tls-key .secrets/pki/warehouse.key \\
        --client-ca .secrets/pki/ca.pem --db .runtime/warehouse.db
"""

from __future__ import annotations

import argparse
import json
import re
import socketserver
import sqlite3
import ssl
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


class Forbidden(Exception):
    """A store presented a valid certificate but sent another store's records."""


def peer_store(connection: ssl.SSLSocket) -> str | None:
    """The common name of the verified client certificate, or None."""
    cert = connection.getpeercert()
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class Warehouse:
    def __init__(self, db: Path, profiles: Path):
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.duplicates = 0
        self.rejected = 0
        self.refused_connections = 0
        self.forbidden_batches = 0
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

    def refuse_connection(self) -> None:
        with self.lock:
            self.refused_connections += 1

    def ingest(self, payload: bytes, store: str) -> int:
        """Stores a batch. `store` is the verified certificate name; every
        record in the batch must belong to it, or nothing is stored."""
        data = json.loads(payload)
        records = data if isinstance(data, list) else [data]
        for rec in records:
            owner = rec.get("context", {}).get("store_id") if isinstance(rec, dict) else None
            if owner != store:
                with self.lock:
                    self.forbidden_batches += 1
                raise Forbidden(f"certificate for {store} cannot deliver {owner} records")
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
            receipts = [{"record": json.loads(b), "received_at": at}
                        for b, at in q("SELECT body, received_at FROM records "
                                       "ORDER BY received_at DESC, rowid DESC LIMIT 60")]
            return {
                "at": time.time(),
                "rows": total,
                "batches": self.batches,
                "bytes_in": self.bytes_in,
                "duplicates_ignored": self.duplicates,
                "rejected": self.rejected,
                "wan": {"refused_connections": self.refused_connections,
                        "forbidden_batches": self.forbidden_batches},
                "per_store": per_store,
                "shoppers": len(join_ids),
                "shoppers_multi_store": multi,
                "online_profiles": len(self.online),
                "online_matched": len(self.online.intersection(join_ids)),
                "countries": countries,
                "rows_without_temperature": no_temp,
                "card_scan": {"rows_scanned": self.rows_scanned, "card_numbers": self.card_hits},
                "recent": recent,
                "recent_receipts": receipts,
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
            self.refused_connections = self.forbidden_batches = 0
            self.card_hits = self.rows_scanned = 0
            self.per_store_batches.clear()
        self.load_profiles()


def tls_context(cert: Path, key: Path, client_ca: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    ctx.load_verify_locations(client_ca)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def reply(handler: BaseHTTPRequestHandler, code: int, obj: object) -> None:
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class IngestServer(ThreadingHTTPServer):
    """The WAN-facing listener. The handshake runs in the worker thread, so a
    plaintext or unauthenticated peer can never stall the accept loop."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], context: ssl.SSLContext, wh: Warehouse):
        self.context = context
        self.wh = wh
        super().__init__(address, self.handler_class())

    def handler_class(self) -> type[BaseHTTPRequestHandler]:
        wh = self.wh

        class Ingest(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(n)
                store = peer_store(self.connection)  # type: ignore[arg-type]
                if self.path != "/ingest" or store is None:
                    reply(self, 404 if store else 401, {"error": "not found"})
                    return
                try:
                    reply(self, 200, {"stored": wh.ingest(payload, store)})
                except Forbidden as exc:
                    reply(self, 403, {"error": str(exc)})
                except (ValueError, sqlite3.Error) as exc:
                    reply(self, 400, {"error": str(exc)})

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                reply(self, 405, {"error": "ingest only"})

            def log_message(self, *args: object) -> None:
                pass

        return Ingest

    def process_request_thread(self, request: object, client_address: object) -> None:
        sock: socketserver.socket.socket = request  # type: ignore[assignment]
        try:
            sock.settimeout(5)
            tls = self.context.wrap_socket(sock, server_side=True)
        except (ssl.SSLError, OSError):
            self.wh.refuse_connection()
            self.shutdown_request(request)
            return
        super().process_request_thread(tls, client_address)  # type: ignore[arg-type]


def admin_server(port: int, wh: Warehouse) -> ThreadingHTTPServer:
    class Admin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/stats":
                reply(self, 200, wh.stats())
            elif self.path.startswith("/record/"):
                rec = wh.record(self.path[8:])
                reply(self, 200 if rec else 404, rec or {"error": "not found"})
            else:
                reply(self, 404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/reset":
                wh.reset()
                reply(self, 200, {"reset": True})
            else:
                reply(self, 404, {"error": "not found"})

        def log_message(self, *args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Admin)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1", help="interface for the TLS ingest port")
    ap.add_argument("--port", type=int, default=8641, help="TLS ingest port")
    ap.add_argument("--admin-port", type=int, default=8643, help="loopback read API")
    ap.add_argument("--db", default=str(ROOT / ".runtime" / "warehouse.db"))
    ap.add_argument("--profiles", default=str(ROOT / ".runtime" / "online-profiles.json"))
    ap.add_argument("--tls-cert", type=Path, required=True)
    ap.add_argument("--tls-key", type=Path, required=True)
    ap.add_argument("--client-ca", type=Path, required=True,
                    help="CA that issues the store certificates")
    args = ap.parse_args()
    wh = Warehouse(Path(args.db), Path(args.profiles))
    ingest = IngestServer((args.bind, args.port),
                          tls_context(args.tls_cert, args.tls_key, args.client_ca), wh)
    admin = admin_server(args.admin_port, wh)
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    print(f"warehouse: TLS ingest {args.bind}:{args.port}, read API 127.0.0.1:{args.admin_port}",
          flush=True)
    try:
        ingest.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
