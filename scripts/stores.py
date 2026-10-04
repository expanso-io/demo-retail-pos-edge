#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The stores: registers, climate sensors, window displays and WAN links.

This is the demo's external producer. Expanso does everything between a
register and the warehouse; this process only plays the devices in the
store and each store's network link:

* registers ring up baskets for a pool of shoppers, sign each swipe with
  their enrolled key and commit it to their store database; they also record
  a heartbeat every few seconds
* one climate sensor per store reports the temperature
* one window display per store receives what the edge sends back
* one WAN link per store carries the uplink to the warehouse and can be cut

Secrets come from the environment (REGISTER_KEY_SEED, JOIN_ID_KEY); nothing
is read from outside this checkout. A control API on localhost lets the
board switch registers, inject faults, kill a sensor or cut a link.

    uv run -s scripts/stores.py serve --control-port 8642
    uv run -s scripts/stores.py keys s1      # REGISTER_KEYS JSON for s1
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import random
import socket
import sqlite3
import uuid
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "stores.json"

WAN_BASE = 8660
DISPLAY_BASE = 8670
WAREHOUSE_PORT = 8641

HEARTBEAT_S = 3.0
SENSOR_S = 3.0

# (sku, name, category, unit_cents, heat sensitivity per degree above 22 C)
CATALOG = [
    ("HD-01", "Flat white", "hot drinks", 340, -0.12),
    ("HD-02", "Black tea", "hot drinks", 260, -0.12),
    ("HD-03", "Hot chocolate", "hot drinks", 380, -0.14),
    ("CD-01", "Iced latte", "cold drinks", 420, 0.16),
    ("CD-02", "Sparkling water", "cold drinks", 190, 0.14),
    ("CD-03", "Lemonade", "cold drinks", 280, 0.16),
    ("IC-01", "Ice cream tub", "ice cream", 450, 0.30),
    ("BK-01", "Croissant", "bakery", 210, 0.0),
    ("BK-02", "Sourdough loaf", "bakery", 480, 0.0),
    ("SW-01", "Chicken wrap", "sandwiches", 590, 0.0),
    ("SW-02", "Cheese sandwich", "sandwiches", 450, 0.0),
    ("SN-01", "Crisps", "snacks", 160, 0.0),
    ("SN-02", "Fruit cup", "fresh fruit", 320, 0.03),
]

FIRST = ["Ana", "Ben", "Chloe", "Daan", "Eva", "Femke", "Jonas", "Lena",
         "Lucas", "Mila", "Noah", "Sanne", "Tim", "Yara", "Lars", "Emma"]
LAST = ["Bakker", "Visser", "Schmidt", "Weber", "Jansen", "Meyer", "Smit",
        "Koch", "de Vries", "Wagner", "Mulder", "Becker", "Bos", "Hoffmann"]
# Operator codes as a terminal prints them; the roster stays in the store.
CASHIERS = ["OP03", "OP07", "OP11", "OP14", "OP19", "OP22"]


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def register_ids(store: dict) -> list[str]:
    return [f"{store['store_id']}-r{n}" for n in range(1, store["registers"] + 1)]


def register_key(seed: str, register_id: str) -> str:
    return hmac.new(seed.encode(), register_id.encode(), hashlib.sha256).hexdigest()[:32]


def join_id(key: str, pan: str) -> str:
    # The central side's own implementation of jid1. pos-guard computes the
    # same thing at the edge; the warehouse's match count proves they agree.
    return "jid1_" + hmac.new(key.encode(), pan.encode(), hashlib.sha256).hexdigest()[:24]


def luhn_pan(prefix: str, length: int, rng: random.Random) -> str:
    body = prefix + "".join(str(rng.randrange(10)) for _ in range(length - len(prefix) - 1))
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return body + str((10 - total % 10) % 10)


def make_shoppers(stores: list[dict], count: int, rng: random.Random) -> list[dict]:
    brands = [("4", "visa", 16), ("51", "mastercard", 16), ("55", "mastercard", 16),
              ("4", "visa", 16), ("37", "amex", 15)]
    shoppers = []
    for i in range(count):
        prefix, brand, length = rng.choice(brands)
        pan = luhn_pan(prefix, length, rng)
        expiry = f"{rng.randint(27, 31)}{rng.randint(1, 12):02d}"  # YYMM, as on track 2
        discretionary = "".join(str(rng.randrange(10)) for _ in range(9))
        shoppers.append({
            "pan": pan,
            "brand": brand,
            # Track 1 name field: SURNAME/INITIAL, upper case, no punctuation
            "name": f"{rng.choice(LAST).upper().replace(' ', '')}/{rng.choice(FIRST)[0]}",
            "expiry": expiry,
            "track2": f"{pan}={expiry}101{discretionary}",
            "cvv": f"{rng.randrange(1000):03d}",
            "home": stores[i % len(stores)]["store_id"],
            "email": f"shopper{i:04d}@example.net" if rng.random() < 0.3 else None,
            "online": rng.random() < 0.35,
        })
    return shoppers


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def post_json(url: str, body: dict, timeout: float = 4.0) -> int:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        return resp.status


class StoreDatabase:
    """Durable source records. Only the pipeline acknowledges collection."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            txn_id TEXT PRIMARY KEY, store_id TEXT NOT NULL,
            record TEXT NOT NULL, fault TEXT, captured_at REAL NOT NULL,
            lease_until REAL NOT NULL DEFAULT 0, collected_at REAL)""")
        self.db.commit()

    def append(self, rec: dict, fault: str | None, captured_at: float | None = None) -> None:
        at = time.time() if captured_at is None else captured_at
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events "
                "(txn_id,store_id,record,fault,captured_at) VALUES (?,?,?,?,?)",
                (rec["txn_id"], rec["register_id"].split("-r")[0],
                 json.dumps(rec), fault, at))

    def read(self, txn: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT record,fault,captured_at FROM events WHERE txn_id=?", (txn,)).fetchone()
        return {"record": json.loads(row[0]), "fault": row[1], "at": row[2]} if row else None

    def lease(self, sid: str, now: float | None = None) -> list[dict]:
        at = time.time() if now is None else now
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT txn_id,record,captured_at FROM events "
                "WHERE store_id=? AND collected_at IS NULL AND lease_until<=? "
                "ORDER BY captured_at,txn_id LIMIT 20", (sid, at)).fetchall()
            self.db.executemany("UPDATE events SET lease_until=? WHERE txn_id=?",
                                [(at + 60, row[0]) for row in rows])
        return [{"record": json.loads(row[1]), "captured_at": row[2]} for row in rows]

    def acknowledge(self, sid: str, txn: str) -> bool:
        with self.lock, self.db:
            cursor = self.db.execute(
                "UPDATE events SET collected_at=COALESCE(collected_at,?) "
                "WHERE store_id=? AND txn_id=? AND lease_until>0", (time.time(), sid, txn))
            return cursor.rowcount == 1

    def snapshot(self, sid: str) -> dict:
        with self.lock:
            total, collected = self.db.execute(
                "SELECT COUNT(*),COUNT(collected_at) FROM events WHERE store_id=?", (sid,)).fetchone()
            last = self.db.execute(
                "SELECT txn_id FROM events WHERE store_id=? "
                "ORDER BY captured_at DESC,txn_id DESC LIMIT 1", (sid,)).fetchone()
        return {"total": total, "pending": total - collected,
                "collected": collected, "last_txn": last[0] if last else None}


class Register:
    def __init__(self, world: World, store: dict, register_id: str, key: str):
        self.world = world
        self.store = store
        self.id = register_id
        self.key = key
        self.state = "open"          # open | closed | crashed
        self.sent = 0                # durably stored at the store
        self.refused = 0             # local database write failures
        self.faults = 0              # faulty swipes this register produced
        self.pending_fault: str | None = None
        self.last_txn: str | None = None
        self.last_activity = 0.0
        self.last_fault_kind: str | None = None
        self.last_fault_at = 0.0
        self.seq = 0
        self.lock = threading.Lock()

    def pick_items(self, rng: random.Random, temp_c: float | None) -> list[dict]:
        t = 22.0 if temp_c is None else temp_c
        weights = [max(0.05, 1.0 + heat * (t - 22.0)) for *_, heat in CATALOG]
        picks = rng.choices(CATALOG, weights=weights, k=rng.choice([1, 1, 2, 2, 3, 4]))
        items: dict[str, dict] = {}
        for sku, name, category, cents, _ in picks:
            # The till knows SKUs and prices only; names and categories are
            # looked up at the edge from the catalog.
            line = items.setdefault(sku, {"sku": sku, "qty": 0, "unit_cents": cents})
            line["qty"] += 1
        return list(items.values())

    def sign(self, rec: dict) -> str:
        lines = ",".join(f"{i['sku']}:{i['qty']}:{i['unit_cents']}" for i in rec["items"])
        canon = "|".join([rec["txn_id"], rec["register_id"], rec["ts"],
                          str(rec["total_cents"]), rec["pan"], lines])
        return hmac.new(self.key.encode(), canon.encode(), hashlib.sha256).hexdigest()

    def build(self, rng: random.Random) -> tuple[dict, str | None]:
        shopper = self.world.pick_shopper(self.store["store_id"], rng)
        items = self.pick_items(rng, self.world.sensor_temp(self.store["store_id"]))
        self.seq += 1
        rec = {
            "txn_id": f"{self.id}-{self.world.boot}-{self.seq:05d}",
            "register_id": self.id,
            "ts": now_iso(),
            "pan": shopper["pan"],
            "track2": shopper["track2"],
            "cvv": shopper["cvv"],
            "expiry": shopper["expiry"],
            "cardholder": shopper["name"],
            "card_brand": shopper["brand"],
            "entry_mode": rng.choice(["contactless", "contactless", "chip", "swipe"]),
            "auth_code": "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(6)),
            "cashier": self.world.cashier(self.id),
            "items": items,
            "total_cents": sum(i["qty"] * i["unit_cents"] for i in items),
            "currency": self.world.currency,
            "note": "",
        }
        if shopper["email"]:
            rec["loyalty_email"] = shopper["email"]
        fault = self.pending_fault
        self.pending_fault = None
        if fault == "leak":
            # The register itself writes the card number into a free-text
            # note: correctly signed, so only the final scan can catch it.
            pan = rec["pan"]
            spaced = " ".join(pan[i:i + 4] for i in range(0, len(pan), 4))
            rec["note"] = f"card {spaced} declined once, retried"
        rec["sig"] = self.sign(rec)
        if fault == "tamper":
            # Altered after signing: price and total changed together, so
            # the totals still add up and only the signature gives it away.
            rec["items"][0]["unit_cents"] = 1
            rec["total_cents"] = sum(i["qty"] * i["unit_cents"] for i in rec["items"])
        elif fault == "malformed":
            del rec["pan"]
            rec["items"][0]["qty"] = "two"
        elif fault == "inject":
            rec["items"][0]["sku"] = "<script>fetch('//x.example/k')</script>"
            rec["sig"] = self.sign(rec)
        return rec, fault

    def ring(self, rng: random.Random) -> None:
        rec, fault = self.build(rng)
        self.world.remember_raw(rec, fault)
        with self.lock:
            self.last_txn = rec["txn_id"]
            self.last_activity = time.time()
            if fault:
                self.faults += 1
                self.last_fault_kind = fault
                self.last_fault_at = time.time()
        with self.lock:
            self.sent += 1

    def heartbeat(self, state: str) -> None:
        self.world.remember_telemetry(self.store["store_id"], self.id,
            {"type": "heartbeat", "register_id": self.id, "state": state,
             "at": time.time()})

    def run(self) -> None:
        rng = random.Random(hash(self.id) ^ int(time.time()))
        next_sale = time.time() + rng.uniform(0.5, 4.0)
        next_beat = 0.0
        while not self.world.stopping.is_set():
            now = time.time()
            if self.state == "open":
                if now >= next_beat:
                    self.heartbeat("open")
                    next_beat = now + HEARTBEAT_S
                if now >= next_sale:
                    self.ring(rng)
                    next_sale = now + rng.expovariate(1 / self.world.mean_gap_s)
            time.sleep(0.05)

    def set_state(self, state: str) -> None:
        if state == "closed" and self.state == "open":
            self.heartbeat("closed")   # a clean sign-off
        if state == "open" and self.state != "open":
            self.heartbeat("open")
        self.state = state

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "state": self.state,
                "sent": self.sent,
                "refused": self.refused,
                "faults": self.faults,
                "last_txn": self.last_txn,
                "last_activity": self.last_activity,
                "last_fault_kind": self.last_fault_kind,
                "last_fault_at": self.last_fault_at,
                "pending_fault": self.pending_fault,
            }


class Display:
    """A store's window display. It shows only what the edge sends it."""

    def __init__(self, store_id: str):
        self.store_id = store_id
        self.window: dict | None = None
        self.previous: dict | None = None
        self.roster: dict[str, dict] = {}
        self.updates = 0
        self.lock = threading.Lock()

    def on_sales(self, body: dict) -> None:
        with self.lock:
            self.previous, self.window = self.window, body
            self.window["received_at"] = time.time()
            self.updates += 1

    def on_roster(self, body: dict) -> None:
        with self.lock:
            body["received_at"] = time.time()
            self.roster[body.get("register_id", "?")] = body

    def snapshot(self) -> dict:
        with self.lock:
            return {"window": self.window, "previous": self.previous,
                    "roster": dict(self.roster), "updates": self.updates}


class WanLink:
    """A store's uplink: a TCP relay to the warehouse that can be cut.

    When cut, the listener closes and open connections are dropped, so the
    edge's uplink gets connection refused, exactly as with a real outage.
    """

    def __init__(self, port: int, target: int):
        self.port = port
        self.target = target
        self.cut = False
        self.bytes_up = 0
        self.bytes_down = 0
        self.conns: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.server: socket.socket | None = None
        self.listen()

    def listen(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(64)
        self.server = srv
        threading.Thread(target=self.accept_loop, args=(srv,), daemon=True).start()

    def accept_loop(self, srv: socket.socket) -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", self.target), timeout=3)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.conns.update({client, upstream})
            threading.Thread(target=self.pipe, args=(client, upstream, "up"), daemon=True).start()
            threading.Thread(target=self.pipe, args=(upstream, client, "down"), daemon=True).start()

    def pipe(self, src: socket.socket, dst: socket.socket, direction: str) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
                with self.lock:
                    if direction == "up":
                        self.bytes_up += len(chunk)
                    else:
                        self.bytes_down += len(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
                with self.lock:
                    self.conns.discard(s)

    def set_cut(self, cut: bool) -> None:
        if cut == self.cut:
            return
        self.cut = cut
        if cut:
            if self.server:
                self.server.close()
                self.server = None
            with self.lock:
                conns = list(self.conns)
            for s in conns:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        else:
            self.listen()

    def snapshot(self) -> dict:
        with self.lock:
            return {"cut": self.cut, "bytes_up": self.bytes_up, "bytes_down": self.bytes_down}


class World:
    def __init__(self, seed: str, join_key: str, runtime: Path, mean_gap_s: float):
        cfg = load_config()
        self.currency = cfg["currency"]
        self.stores = cfg["stores"]
        for n, store in enumerate(self.stores, 1):
            store["n"] = n
        self.mean_gap_s = mean_gap_s
        self.boot = uuid.uuid4().hex[:12]
        self.stopping = threading.Event()
        rng = random.Random(7)
        self.shoppers = make_shoppers(self.stores, 480, rng)
        self.by_home = collections.defaultdict(list)
        for s in self.shoppers:
            self.by_home[s["home"]].append(s)
        self.registers = {
            rid: Register(self, store, rid, register_key(seed, rid))
            for store in self.stores for rid in register_ids(store)
        }
        self.cashiers = {rid: CASHIERS[i % len(CASHIERS)]
                         for i, rid in enumerate(self.registers)}
        self.sensor_on = {s["store_id"]: True for s in self.stores}
        self.sensor_sent = collections.Counter()
        self.displays = {s["store_id"]: Display(s["store_id"]) for s in self.stores}
        self.links = {s["store_id"]: WanLink(WAN_BASE + s["n"], WAREHOUSE_PORT)
                      for s in self.stores}
        self.database = StoreDatabase(runtime / "store-events.db")
        self.telemetry: dict[str, dict[str, dict]] = {s["store_id"]: {} for s in self.stores}
        self.telemetry_lock = threading.Lock()
        self.telemetry_polled: dict[str, float] = {}
        self.started = time.time()
        # The central side's online profiles: join IDs only, never a card.
        runtime.mkdir(parents=True, exist_ok=True)
        profiles = sorted(join_id(join_key, s["pan"]) for s in self.shoppers if s["online"])
        out = runtime / "online-profiles.json"
        out.write_text(json.dumps({"algorithm": "jid1", "join_ids": profiles}))
        os.chmod(out, 0o600)

    def pick_shopper(self, store_id: str, rng: random.Random) -> dict:
        if rng.random() < 0.25:
            return rng.choice(self.shoppers)      # a visitor from another store
        return rng.choice(self.by_home[store_id])

    def cashier(self, register_id: str) -> str:
        return self.cashiers[register_id]

    def sensor_temp(self, store_id: str) -> float | None:
        if not self.sensor_on[store_id]:
            return None
        store = next(s for s in self.stores if s["store_id"] == store_id)
        drift = 1.2 * math.sin((time.time() - self.started) / 90 + store["n"])
        return round(store["base_temp_c"] + drift, 1)

    def remember_raw(self, rec: dict, fault: str | None) -> None:
        self.database.append(rec, fault)

    def remember_telemetry(self, sid: str, key: str, body: dict) -> None:
        with self.telemetry_lock:
            self.telemetry[sid][key] = body

    def read_telemetry(self, sid: str) -> list[dict]:
        with self.telemetry_lock:
            self.sensor_sent[sid] += 1
            self.telemetry_polled[sid] = time.time()
            return list(self.telemetry[sid].values())

    def sensor_loop(self, store: dict) -> None:
        sid = store["store_id"]
        while not self.stopping.is_set():
            temp = self.sensor_temp(sid)
            if temp is not None:
                self.remember_telemetry(sid, "climate",
                    {"type": "climate", "sensor": f"{sid}-climate",
                     "temp_c": temp, "at": time.time()})
            self.stopping.wait(SENSOR_S)

    def start(self) -> None:
        for reg in self.registers.values():
            threading.Thread(target=reg.run, daemon=True).start()
        for store in self.stores:
            threading.Thread(target=self.sensor_loop, args=(store,), daemon=True).start()
            self.serve_display(store)

    def serve_display(self, store: dict) -> None:
        display = self.displays[store["store_id"]]

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return
                if not isinstance(body, dict):
                    self.send_response(422)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/sales"):
                    display.on_sales(body)
                elif self.path.startswith("/roster"):
                    display.on_roster(body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", DISPLAY_BASE + store["n"]), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def state(self) -> dict:
        stores = []
        for store in self.stores:
            sid = store["store_id"]
            stores.append({
                "store_id": sid,
                "name": store["name"],
                "district": store["district"],
                "country": store["country"],
                "registers": [self.registers[r].snapshot() for r in register_ids(store)],
                "sensor": {"on": self.sensor_on[sid], "temp_c": self.sensor_temp(sid),
                           "sent": self.sensor_sent[sid],
                           "endpoint": f"/telemetry/{sid}",
                           "last_poll": self.telemetry_polled.get(sid, 0)},
                "database": self.database.snapshot(sid),
                "display": self.displays[sid].snapshot(),
                "link": self.links[sid].snapshot(),
            })
        return {"at": time.time(), "boot": self.boot, "stores": stores}

    def control(self, path: str, body: dict) -> dict:
        if path == "/register":
            reg = self.registers[body["register_id"]]
            reg.set_state(body["state"])
            return {"register_id": reg.id, "state": reg.state}
        if path == "/fault":
            reg = self.registers[body["register_id"]]
            kind = body["kind"]
            if kind == "crash":
                reg.set_state("crashed")
            elif kind in {"tamper", "leak", "malformed", "inject"}:
                if reg.state != "open":
                    reg.set_state("open")
                reg.pending_fault = kind
            else:
                raise ValueError(f"unknown fault {kind}")
            return {"register_id": reg.id, "fault": kind}
        if path == "/sensor":
            self.sensor_on[body["store_id"]] = bool(body["on"])
            return {"store_id": body["store_id"], "on": self.sensor_on[body["store_id"]]}
        if path == "/link":
            self.links[body["store_id"]].set_cut(bool(body["cut"]))
            return {"store_id": body["store_id"], "cut": bool(body["cut"])}
        if path == "/reset":
            for reg in self.registers.values():
                reg.pending_fault = None
                reg.set_state("open")
            for sid in self.sensor_on:
                self.sensor_on[sid] = True
            for link in self.links.values():
                link.set_cut(False)
            return {"reset": True}
        raise KeyError(path)


def serve(args: argparse.Namespace) -> int:
    seed = os.environ.get("REGISTER_KEY_SEED", "")
    join_key = os.environ.get("JOIN_ID_KEY", "")
    if len(seed) < 16 or len(join_key) < 16:
        print("REGISTER_KEY_SEED and JOIN_ID_KEY must be set (demo.sh reads .env)",
              file=sys.stderr)
        return 2
    world = World(seed, join_key, Path(args.runtime), args.mean_gap)

    class Control(BaseHTTPRequestHandler):
        def reply(self, code: int, obj: object) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/state":
                self.reply(200, world.state())
            elif self.path.startswith("/raw/"):
                item = world.database.read(self.path[5:])
                self.reply(200 if item else 404, item or {"error": "not found"})
            elif self.path.startswith("/events/") and self.path[8:] in world.sensor_on:
                self.reply(200, world.database.lease(self.path[8:]))
            elif self.path.startswith("/telemetry/") and self.path[11:] in world.sensor_on:
                self.reply(200, world.read_telemetry(self.path[11:]))
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/ack/"):
                    ok = world.database.acknowledge(self.path[5:], body["txn_id"])
                    self.reply(200 if ok else 404, {"collected": ok})
                else:
                    self.reply(200, world.control(self.path, body))
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self.reply(400, {"error": str(exc)})

        def log_message(self, *args: object) -> None:
            pass

    world.start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.control_port), Control)
    count = len(world.registers)
    print(f"stores: {len(world.stores)} stores, {count} registers; control on "
          f"127.0.0.1:{args.control_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        world.stopping.set()
    return 0


def keys(args: argparse.Namespace) -> int:
    seed = os.environ.get("REGISTER_KEY_SEED", "")
    if len(seed) < 16:
        print("REGISTER_KEY_SEED must be set", file=sys.stderr)
        return 2
    store = next(s for s in load_config()["stores"] if s["store_id"] == args.store)
    print(json.dumps({r: register_key(seed, r) for r in register_ids(store)},
                     separators=(",", ":")))
    return 0


def profile(args: argparse.Namespace) -> int:
    store = next(s for s in load_config()["stores"] if s["store_id"] == args.store)
    fields = ("store_id", "name", "district", "country", "tz", "lat", "lon")
    print(json.dumps({k: store[k] for k in fields}, separators=(",", ":")))
    return 0


def catalog(args: argparse.Namespace) -> int:
    # What the edge adds to a SKU: the product's name and category.
    print(json.dumps({sku: {"name": name, "category": category}
                      for sku, name, category, _, _ in CATALOG}, separators=(",", ":")))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--control-port", type=int, default=8642)
    s.add_argument("--runtime", default=str(ROOT / ".runtime"))
    s.add_argument("--mean-gap", type=float, default=4.0,
                   help="mean seconds between sales per register")
    k = sub.add_parser("keys")
    k.add_argument("store")
    p = sub.add_parser("profile")
    p.add_argument("store")
    sub.add_parser("catalog")
    sub.add_parser("list")
    args = ap.parse_args()
    if args.cmd == "serve":
        return serve(args)
    if args.cmd == "keys":
        return keys(args)
    if args.cmd == "profile":
        return profile(args)
    if args.cmd == "catalog":
        return catalog(args)
    for store in load_config()["stores"]:
        print(store["store_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
