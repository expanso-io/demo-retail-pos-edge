#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""One-shot proof run: both shipped jobs, real Expanso Edge nodes, fixture swipes.

For two stores it starts a store source that serves the fixture swipes, a
window display per store, the real warehouse (mutual TLS) behind each
store's WAN link, and one `expanso-edge` node per store running pos-guard
and pos-uplink exactly as shipped. Then it checks every output against
fixtures/expected.json:

  phase 1  the uplink presents a certificate from the wrong authority:
           the warehouse must refuse it and every clean record must wait on
           the store's disk
  phase 2  the nodes restart with the right certificates while the WAN link
           is cut: the queue must survive the restart, nothing may land
  phase 3  the link returns: the queue must drain once, with no duplicates

A second pass runs a traced copy of pos-guard, which posts every stage's
real message to a collector, to record the input and output of each stage
for the explorer (dashboard/data/stages.json). That pass must reproduce the
same warehouse and quarantine contents as the shipped run.

    uv run -s scripts/fixture_run.py          # writes the dated report

It starts only localhost listeners and child processes, and stops every one
of them before it exits.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fixtures  # noqa: E402
import pki  # noqa: E402
import stores  # noqa: E402
import warehouse  # noqa: E402

VOLATILE = [("edge_at",), ("uplink", "queued_at"), ("context", "sensor_at")]
GUARD_TAPS = {"scan_swipe": "scanned", "join_id_and_strip": "joined",
              "add_store_temperature": "context", "to_quarantine": "quarantined"}


# What the explorer says about each stage. `resource` is the label in the job
# file; the line number is looked up in the committed YAML, so the explorer's
# pointers cannot drift from the pipelines.
STAGE_DEFS = [
    {"id": "raw", "title": "Raw swipe", "file": "pipelines/pos-guard.yaml",
     "resource": "store_database",
     "what": "The till writes what a card terminal prints: card number, track 2, CVV, expiry, "
             "cardholder, operator code, SKU lines and a signature, into the store's own "
             "database. pos-guard leases it from the store API and keeps the time the "
             "till captured it."},
    {"id": "scan", "title": "Scan", "file": "pipelines/pos-guard.yaml", "resource": "scan_swipe",
     "what": "Checks the signature against the till's enrolled key, the required fields, "
             "that the lines add up to the total, the value limit, that the record is not "
             "more than 120 seconds old, and text fields for injection patterns. It adds "
             "scan.reasons. An empty list passes; a swipe the scan cannot read at all, or "
             "from a register not enrolled here, is quarantined."},
    {"id": "join", "title": "Join ID and strip", "file": "pipelines/pos-guard.yaml",
     "resource": "join_id_and_strip",
     "what": "Replaces the card number with a one-way join ID (jid1: HMAC-SHA256 under the "
             "group's key, so the same card gives the same ID at every store). Resolves SKUs "
             "to product names and categories, adds the store, till, location and local time, "
             "and copies nothing that could identify a person. The fields it left behind are "
             "listed under stripped."},
    {"id": "context", "title": "Store context", "file": "pipelines/pos-guard.yaml",
     "resource": "add_store_temperature",
     "what": "Adds the store's temperature from the climate sensor's latest reading. A "
             "reading more than 15 seconds old is not used: the record goes without it and "
             "says the sensor is missing."},
    {"id": "route", "title": "Final scan, then quarantine or uplink",
     "file": "pipelines/pos-guard.yaml", "resource": "block_card_numbers",
     "what": "A last scan of the outgoing record for anything shaped like a card number, in "
             "any field. A hit is quarantined. Quarantined records are written to a file in "
             "the store with the reason and a masked note, and never leave. Clean records go "
             "to the uplink's queue on the store's disk, stamped with the store and the "
             "time they were queued."},
    {"id": "warehouse", "title": "Warehouse receipt", "file": "pipelines/pos-uplink.yaml",
     "resource": "warehouse_ingest",
     "what": "pos-uplink sends queued records in batches over HTTPS with the store's client "
             "certificate. The warehouse accepts a batch only from a certificate issued by "
             "the group's authority, only for that store's own records, and stores each "
             "transaction ID once. The record leaves the queue when the warehouse answers."},
]


def stage_defs() -> list[dict]:
    out = []
    for stage in STAGE_DEFS:
        lines = (ROOT / stage["file"]).read_text().splitlines()
        line = next((n for n, text in enumerate(lines, 1)
                     if text.strip().endswith(f"label: {stage['resource']}")), None)
        if line is None:
            raise SystemExit(f"{stage['file']} has no label {stage['resource']}")
        out.append({**stage, "line": line})
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, seconds: float, what: str) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise TimeoutError(f"timed out after {seconds:.0f}s waiting for {what}")


def say(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------ the fixture world


class Source:
    """The store database API pos-guard polls: events with leases, telemetry, acks."""

    def __init__(self, folder: Path, swipes: dict):
        self.db = stores.StoreDatabase(folder / "source.db")
        self.heartbeats, self.climate = swipes["heartbeats"], swipes["climate_temp_c"]
        self.store_ids = swipes["stores"]
        for case in swipes["cases"]:
            self.db.append(case["record"], None, captured_at=case["captured_at"])
        self.port = free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code: int, obj: object) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path.startswith("/events/"):
                    self.reply(200, outer.db.lease(self.path[8:]))
                elif self.path.startswith("/telemetry/"):
                    self.reply(200, outer.telemetry(self.path[11:]))
                else:
                    self.reply(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/ack/"):
                    ok = outer.db.acknowledge(self.path[5:], body["txn_id"])
                    self.reply(200 if ok else 404, {"collected": ok})
                else:
                    self.reply(404, {"error": "not found"})

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def telemetry(self, sid: str) -> list[dict]:
        now = time.time()
        out = []
        if sid in self.climate:
            out.append({"type": "climate", "sensor": f"{sid}-climate",
                        "temp_c": self.climate[sid], "at": now})
        for register, beat in self.heartbeats.get(sid, {}).items():
            out.append({"type": "heartbeat", "register_id": register,
                        "state": beat["state"], "at": now + beat["age_s"]})
        return out

    def pending(self) -> int:
        return sum(self.db.snapshot(s)["pending"] for s in self.store_ids)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.db.db.close()


class Window:
    """A store's window display; keeps every sales window it was sent."""

    def __init__(self, sid: str):
        self.display = stores.Display(sid)
        self.windows: list[dict] = []
        original = self.display.on_sales

        def record(body: dict) -> None:
            self.windows.append(dict(body))
            original(body)

        self.display.on_sales = record
        self.port = free_port()
        self.server = stores.serve_display(self.display, self.port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Taps:
    """Collects the stage messages the traced pos-guard posts."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.lock = threading.Lock()
        self.port = free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                with outer.lock:
                    outer.items.append(body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Wan:
    """The real warehouse behind a WAN link per store."""

    def __init__(self, folder: Path, certs: Path, store_ids: list[str]):
        self.wh = warehouse.Warehouse(folder / "warehouse.db", folder / "profiles.json")
        context = warehouse.tls_context(certs / "warehouse.pem", certs / "warehouse.key",
                                        certs / "ca.pem")
        self.server = warehouse.IngestServer(("127.0.0.1", 0), context, self.wh)
        self.port = self.server.server_address[1]
        self.batches: list[dict] = []
        original = self.wh.ingest

        def recording(payload: bytes, store: str) -> int:
            stored = original(payload, store)
            self.batches.append({"store": store, "records": json.loads(payload),
                                 "stored": stored})
            return stored

        self.wh.ingest = recording  # type: ignore[method-assign]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.links = {sid: stores.WanLink(free_port(), self.port) for sid in store_ids}

    def rows(self) -> int:
        with self.wh.lock:
            return self.wh.conn.execute("SELECT count(*) FROM records").fetchone()[0]

    def bodies(self) -> dict[str, dict]:
        with self.wh.lock:
            return {t: json.loads(b) for t, b in
                    self.wh.conn.execute("SELECT txn_id, body FROM records")}

    def row(self, txn: str) -> dict | None:
        return self.wh.record(txn)

    def close(self) -> None:
        for link in self.links.values():
            link.set_cut(True)
        self.server.shutdown()
        self.server.server_close()
        self.wh.conn.close()


# ------------------------------------------------------------------ edge nodes


def tap(stage: str) -> dict:
    """A processor that posts the current message to the collector and leaves it alone."""
    return {"branch": {
        "request_map": ('root = {"stage": "%s", "txn_id": this.txn_id.or(""), '
                        '"route": @route.or(null), "captured_at": @captured_at.or(null), '
                        '"message": this}' % stage),
        "processors": [{"http": {"url": "${TAP_URL}", "verb": "POST",
                                        "retries": 3, "retry_period": "1s"}}],
    }}


def traced_jobs(folder: Path) -> tuple[Path, Path]:
    """Copies of the shipped jobs with a tap after each stage. Nothing else differs."""
    guard = yaml.safe_load((ROOT / "pipelines" / "pos-guard.yaml").read_text())
    resources = {r["label"]: r for r in guard["config"]["processor_resources"]}
    resources["scan_swipe"]["processors"].insert(0, tap("collected"))
    for label, stage in GUARD_TAPS.items():
        resources[label]["processors"].append(tap(stage))
    resources["block_card_numbers"]["processors"].append(tap("checked"))
    uplink = yaml.safe_load((ROOT / "pipelines" / "pos-uplink.yaml").read_text())
    uplink["config"]["pipeline"]["processors"].append(tap("queued"))
    paths = (folder / "traced-guard.yaml", folder / "traced-uplink.yaml")
    paths[0].write_text(yaml.safe_dump(guard, sort_keys=False, width=100))
    paths[1].write_text(yaml.safe_dump(uplink, sort_keys=False, width=100))
    return paths


class Node:
    def __init__(self, sid: str, folder: Path):
        self.sid = sid
        self.dir = folder / sid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.api = free_port()
        self.outbox = free_port()
        self.process: subprocess.Popen | None = None

    @property
    def quarantine(self) -> Path:
        return self.dir / "quarantine.jsonl"

    @property
    def queue_db(self) -> Path:
        return self.dir / "uplink-queue.db"

    def queue_depth(self) -> int | None:
        if not self.queue_db.exists():
            return None
        try:
            with sqlite3.connect(f"file:{self.queue_db}?mode=ro", uri=True, timeout=1) as conn:
                return conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        except sqlite3.Error:
            return None

    def quarantined(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.quarantine.read_text().splitlines()]
        except OSError:
            return []

    def start(self, world: "World", certs: Path, jobs: tuple[Path, Path]) -> None:
        sid = self.sid
        profile = fixtures.store_profile(sid)
        config = self.dir / "config"
        config.mkdir(mode=0o700, exist_ok=True)
        for name, text in {
                "register-keys.json": json.dumps(fixtures.register_keys(sid)),
                "join-id-key.txt": fixtures.JOIN_ID_KEY + "\n",
                "store-profile.json": json.dumps({k: profile[k] for k in (
                    "store_id", "name", "district", "country", "tz", "lat", "lon")}),
                "store-catalog.json": json.dumps({sku: {"name": n, "category": c}
                                                  for sku, n, c, _, _ in stores.CATALOG})}.items():
            (config / name).write_text(text)
            (config / name).chmod(0o600)
        env = {**os.environ,
               "STORE_ID": sid,
               "POS_CONFIG_DIR": str(config),
               "STORE_SOURCE_URL": f"http://127.0.0.1:{world.source.port}",
               "OUTBOX_ADDR": f"127.0.0.1:{self.outbox}",
               "OUTBOX_URL": f"http://127.0.0.1:{self.outbox}/outbox",
               "DISPLAY_URL": f"http://127.0.0.1:{world.windows[sid].port}",
               "WAREHOUSE_HOST": f"127.0.0.1:{world.wan.links[sid].port}",
               "WAREHOUSE_CA_FILE": str(world.pki / "ca.pem"),
               "STORE_CLIENT_CERT_FILE": str(certs / "clients" / f"{sid}.pem"),
               "STORE_CLIENT_KEY_FILE": str(certs / "clients" / f"{sid}.key"),
               "QUARANTINE_FILE": str(self.quarantine),
               "UPLINK_QUEUE_DB": str(self.queue_db),
               "TAP_URL": f"http://127.0.0.1:{world.taps.port}/tap"}
        node = self.dir / "node.yaml"
        node.write_text(yaml.safe_dump({
            "name": f"pos-{sid}-edge", "api": {"listen_addr": f"127.0.0.1:{self.api}"},
            "log": {"level": "info", "format": "console"},
            "labels": {"demo": "retail-pos-edge", "role": "pos-store", "store": sid}}))
        data = self.dir / "local"
        shutil.rmtree(data, ignore_errors=True)
        log = (self.dir / "edge.log").open("ab")
        self.process = subprocess.Popen(
            ["expanso-edge", "run", "-c", str(node), "--no-watch", "--local",
             "--data-dir", str(data)], env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        endpoint = f"http://127.0.0.1:{self.api}"

        def ready() -> bool:
            if self.process.poll() is not None:
                raise RuntimeError(f"{sid}: edge exited early, see {self.dir / 'edge.log'}")
            return subprocess.run(["expanso-cli", "--endpoint", endpoint, "node", "list"],
                                  capture_output=True).returncode == 0

        wait_until(ready, 30, f"{sid} edge API")
        for job in jobs:
            done = subprocess.run(["expanso-cli", "--endpoint", endpoint, "job", "deploy",
                                   str(job)], capture_output=True, text=True)
            if done.returncode:
                raise RuntimeError(f"{sid}: could not deploy {job.name}: {done.stdout[:300]}"
                                   f"{done.stderr[:300]}")

    def stop(self) -> None:
        if not self.process:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        self.process = None


class World:
    def __init__(self, folder: Path, swipes: dict):
        self.folder = folder
        self.pki = folder / "pki"
        self.rogue = folder / "rogue"
        ids = swipes["stores"]
        pki.init(self.pki, ids)
        pki.init(self.rogue, ids, ca_name="Not the retail group CA")
        self.source = Source(folder, swipes)
        self.windows = {sid: Window(sid) for sid in ids}
        self.taps = Taps()
        self.wan = Wan(folder, self.pki, ids)
        self.nodes = {sid: Node(sid, folder) for sid in ids}

    def start_nodes(self, certs: Path, jobs: tuple[Path, Path]) -> None:
        for node in self.nodes.values():
            node.start(self, certs, jobs)

    def stop_nodes(self) -> None:
        for node in self.nodes.values():
            node.stop()

    def queue_total(self) -> int:
        return sum(n.queue_depth() or 0 for n in self.nodes.values())

    def close(self) -> list[int]:
        """Stops everything it started and returns any port still listening."""
        ports = [self.source.port, self.taps.port, self.wan.port,
                 *[w.port for w in self.windows.values()],
                 *[link.port for link in self.wan.links.values()],
                 *[n.api for n in self.nodes.values()], *[n.outbox for n in self.nodes.values()]]
        self.stop_nodes()
        self.wan.close()
        self.taps.close()
        for window in self.windows.values():
            window.close()
        self.source.close()
        time.sleep(0.5)
        with socket.socket() as probe:
            probe.settimeout(0.3)
            return [p for p in ports if probe.connect_ex(("127.0.0.1", p)) == 0]


# --------------------------------------------------------------------- checking


def strip_volatile(rec: dict) -> dict:
    rec = copy.deepcopy(rec)
    for path in VOLATILE:
        cursor = rec
        for key in path[:-1]:
            cursor = cursor.get(key, {})
        cursor.pop(path[-1], None)
    return rec


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, ok: bool, expected: object, actual: object) -> bool:
        self.rows.append({"name": name, "ok": bool(ok), "expected": expected, "actual": actual})
        if not ok:
            say(f"FAIL {name}: expected {expected!r}, got {actual!r}")
        return bool(ok)

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if not r["ok"]]


def check_outputs(checks: Checks, tag: str, expected: dict, swipes: dict, world: World) -> dict:
    """Compares the warehouse, quarantine files, display windows and roster."""
    totals = expected["totals"]
    bodies = world.wan.bodies()
    checks.add(f"{tag}: every swipe collected and acknowledged at the source",
               world.source.pending() == 0, 0, world.source.pending())
    checks.add(f"{tag}: warehouse rows", len(bodies) == totals["clean"], totals["clean"],
               len(bodies))
    for case_id, want in expected["warehouse"].items():
        txn = next(c["record"]["txn_id"] for c in swipes["cases"] if c["id"] == case_id)
        got = strip_volatile(bodies.get(txn, {}))
        checks.add(f"{tag}: warehouse record {case_id}", got == want, want, got)
    checks.add(f"{tag}: warehouse duplicates ignored", world.wan.wh.duplicates == 0, 0,
               world.wan.wh.duplicates)
    checks.add(f"{tag}: warehouse's own card-number scan", world.wan.wh.card_hits == 0, 0,
               world.wan.wh.card_hits)
    quarantine = {q["txn_id"]: q for n in world.nodes.values() for q in n.quarantined()}
    checks.add(f"{tag}: quarantine records", len(quarantine) == totals["quarantined"],
               totals["quarantined"], len(quarantine))
    for case_id, want in expected["quarantine"].items():
        got = quarantine.get(want["txn_id"], {})
        ok = (got.get("reason", "").startswith(want["reason_starts_with"])
              and got.get("note") == want["note"] and got.get("store_id") == want["store_id"]
              and got.get("register_id") == want["register_id"]
              and got.get("total_cents") == want["total_cents"] and got.get("reasons"))
        checks.add(f"{tag}: quarantine record {case_id}", ok, want, got)
    leaked = [q["txn_id"] for q in quarantine.values() if warehouse.card_numbers_in(q)]
    checks.add(f"{tag}: no card number in any quarantine record", not leaked, [], leaked)
    ids = {c: bodies[next(x["record"]["txn_id"] for x in swipes["cases"] if x["id"] == c)]
           ["join_id"] for c in ("clean-hot-drinks", "clean-same-card-other-store")
           if any(x["id"] == c for x in swipes["cases"])}
    checks.add(f"{tag}: one card, two stores, one join ID", len(set(ids.values())) == 1,
               "one value", ids)
    for sid, units in expected["window_units"].items():
        got: dict[str, int] = {}
        for window in world.windows[sid].windows:
            for row in window["by_category"]:
                got[row["category"]] = got.get(row["category"], 0) + row["units"]
        checks.add(f"{tag}: window display units at {sid}", got == units, units, got)
    roster = {}
    for window in world.windows.values():
        roster.update({r: v["status"] for r, v in window.display.roster.items()})
    checks.add(f"{tag}: register roster", roster == expected["roster"], expected["roster"],
               roster)
    return {"bodies": bodies, "quarantine": quarantine}


def deep_equal_after_strip(a: dict, b: dict) -> bool:
    return {k: strip_volatile(v) for k, v in a.items()} == {k: strip_volatile(v)
                                                            for k, v in b.items()}


# ----------------------------------------------------------------------- passes


def shipped_pass(work: Path, swipes: dict, expected: dict, checks: Checks) -> dict:
    """Phases 1 to 3 on the jobs exactly as committed."""
    jobs = (ROOT / "pipelines" / "pos-guard.yaml", ROOT / "pipelines" / "pos-uplink.yaml")
    world = World(work / "shipped", swipes)
    clean, ids = expected["totals"]["clean"], swipes["stores"]
    evidence: dict = {}
    try:
        say("phase 1: uplink presents a certificate from the wrong authority")
        world.start_nodes(world.rogue, jobs)
        wait_until(lambda: world.source.pending() == 0, 60, "every swipe acknowledged")
        wait_until(lambda: world.queue_total() == clean, 30, "clean records queued on disk")
        wait_until(lambda: world.wan.wh.refused_connections >= 1, 45, "a refused connection")
        time.sleep(3)
        evidence["phase1"] = {"queue": world.queue_total(), "rows": world.wan.rows(),
                              "refused_connections": world.wan.wh.refused_connections,
                              "quarantined": sum(len(n.quarantined()) for n in world.nodes.values())}
        checks.add("phase 1: wrong-authority certificate delivers nothing",
                   evidence["phase1"]["rows"] == 0, 0, evidence["phase1"]["rows"])
        checks.add("phase 1: the warehouse counted the refused connections",
                   evidence["phase1"]["refused_connections"] >= 1, ">= 1",
                   evidence["phase1"]["refused_connections"])
        checks.add("phase 1: every clean record waits on the store's disk",
                   evidence["phase1"]["queue"] == clean, clean, evidence["phase1"]["queue"])

        say("phase 2: nodes restart with the right certificates, WAN link cut")
        world.stop_nodes()
        for link in world.wan.links.values():
            link.set_cut(True)
        world.start_nodes(world.pki, jobs)
        time.sleep(12)
        evidence["phase2"] = {"queue": world.queue_total(), "rows": world.wan.rows()}
        checks.add("phase 2: the queue survives a node restart",
                   evidence["phase2"]["queue"] == clean, clean, evidence["phase2"]["queue"])
        checks.add("phase 2: nothing lands while the link is cut",
                   evidence["phase2"]["rows"] == 0, 0, evidence["phase2"]["rows"])

        say("phase 3: link restored, queue drains")
        for link in world.wan.links.values():
            link.set_cut(False)
        wait_until(lambda: world.wan.rows() == clean, 60, "the queue to drain")
        wait_until(lambda: world.queue_total() == 0, 30, "an empty queue")
        time.sleep(2)
        checks.add("phase 3: queue empty after the drain", world.queue_total() == 0, 0,
                   world.queue_total())
        wait_until(lambda: all(len(w.display.roster) >= 1 for w in world.windows.values()), 20,
                   "a roster")
        wait_until(lambda: len({r for w in world.windows.values() for r in w.display.roster})
                   == len(expected["roster"]), 25, "every register on the roster")
        results = check_outputs(checks, "shipped", expected, swipes, world)
        evidence.update(results)
        evidence["batches"] = world.wan.batches
        evidence["bytes_in"] = world.wan.wh.bytes_in
        evidence["batch_count"] = world.wan.wh.batches
        evidence["duplicates"] = world.wan.wh.duplicates
        evidence["per_store_rows"] = {sid: sum(1 for b in results["bodies"].values()
                                               if b["context"]["store_id"] == sid)
                                      for sid in ids}
        evidence["windows"] = {sid: w.windows for sid, w in world.windows.items()}
        evidence["roster"] = {r: v["status"] for w in world.windows.values()
                              for r, v in w.display.roster.items()}
    finally:
        evidence["left_listening"] = world.close()
    return evidence


def traced_pass(work: Path, swipes: dict, expected: dict, checks: Checks) -> dict:
    """The traced jobs against a reachable warehouse: stage messages and receipts."""
    world = World(work / "traced", swipes)
    clean = expected["totals"]["clean"]
    evidence: dict = {}
    try:
        say("traced pass: every stage's real message")
        jobs = traced_jobs(work)
        world.start_nodes(world.pki, jobs)
        wait_until(lambda: world.source.pending() == 0, 60, "every swipe acknowledged")
        wait_until(lambda: world.wan.rows() == clean, 60, "the warehouse to hold every clean record")
        wait_until(lambda: world.queue_total() == 0, 30, "an empty queue")
        wait_until(lambda: len({r for w in world.windows.values() for r in w.display.roster})
                   == len(expected["roster"]), 25, "every register on the roster")
        time.sleep(2)
        results = check_outputs(checks, "traced", expected, swipes, world)
        evidence.update(results)
        evidence["taps"] = list(world.taps.items)
        evidence["batches"] = world.wan.batches
        evidence["rows"] = {t: world.wan.row(t) for t in results["bodies"]}
    finally:
        evidence["left_listening"] = world.close()
    return evidence


def assemble_stages(swipes: dict, shipped: dict, traced: dict) -> list[dict]:
    """Per swipe, the real input and output of each stage."""
    by_txn: dict[str, dict[str, dict]] = {}
    for item in traced["taps"]:
        by_txn.setdefault(item["txn_id"], {})[item["stage"]] = item
    out = []
    for case in swipes["cases"]:
        rec = case["record"]
        txn = rec.get("txn_id", "unknown")
        taps = by_txn.get(txn, {})
        clean = case["id"].startswith("clean")
        scanned = taps.get("scanned", {}).get("message")
        checked = taps.get("checked", {})
        quarantined = taps.get("quarantined", {}).get("message")
        blocked_late = bool(quarantined) and "joined" in taps
        stages = [
            {"id": "raw", "ran": True, "input": rec,
             "output": {"captured_at": case["captured_at"], "record": rec}},
            {"id": "scan", "ran": "scanned" in taps,
             "input": taps.get("collected", {}).get("message"), "output": scanned},
        ]
        passed_scan = bool(scanned) and not scanned.get("scan", {}).get("reasons")
        stages.append({"id": "join", "ran": "joined" in taps, "input": scanned if passed_scan else None,
                       "output": taps.get("joined", {}).get("message"),
                       "skipped": None if "joined" in taps else "the scan failed, so the swipe never reaches this stage"})
        stages.append({"id": "context", "ran": "context" in taps,
                       "input": taps.get("joined", {}).get("message"),
                       "output": taps.get("context", {}).get("message"),
                       "skipped": None if "context" in taps else "the scan failed, so the swipe never reaches this stage"})
        if clean:
            queued = taps.get("queued", {}).get("message")
            route_in, route_out = taps.get("context", {}).get("message"), queued
            route_note = None
        else:
            route_in = (taps.get("context", {}).get("message") if blocked_late else scanned)
            route_out = quarantined
            route_note = None
        stages.append({"id": "route", "ran": True, "input": route_in, "output": route_out,
                       "route": "clean" if clean else "quarantine", "note": route_note,
                       "final_scan": checked.get("route")})
        receipt = None
        if clean:
            batch = next((b for b in traced["batches"]
                          if any(r.get("txn_id") == txn for r in b["records"])), None)
            row = traced["rows"].get(txn)
            receipt = {
                "input": {"request": "POST https://warehouse/ingest",
                          "authenticated_as": f"client certificate CN={batch['store']}" if batch else None,
                          "records_in_batch": len(batch["records"]) if batch else None,
                          "record": next((r for r in batch["records"] if r.get("txn_id") == txn), None) if batch else None},
                "output": {"status": 200, "response": {"stored": batch["stored"] if batch else None},
                           "stored_row": row},
            }
        stages.append({"id": "warehouse", "ran": clean,
                       "input": receipt["input"] if receipt else None,
                       "output": receipt["output"] if receipt else {
                           "warehouse": "no row",
                           "kept_in_store": quarantined},
                       "skipped": None if clean else "quarantined records never leave the store"})
        out.append({"id": case["id"], "title": case["title"], "store": case["store"],
                    "txn_id": txn, "outcome": "clean" if clean else "quarantined",
                    "reason": (quarantined or {}).get("reason"), "stages": stages})
    return out


# ----------------------------------------------------------------------- report


def tool_versions() -> dict:
    def run(*cmd: str) -> str:
        done = subprocess.run(cmd, capture_output=True, text=True)
        return (done.stdout or done.stderr).strip().splitlines()[0] if (done.stdout or done.stderr) else "?"
    return {"expanso-edge": run("expanso-edge", "version"), "expanso-cli": run("expanso-cli", "version"),
            "python": sys.version.split()[0], "openssl": run("openssl", "version")}


def write_outputs(swipes: dict, expected: dict, checks: Checks, shipped: dict, traced: dict,
                  stages: list[dict], started: float) -> tuple[Path, Path]:
    now = dt.datetime.now().astimezone()  # the dated report uses the local calendar day
    digest, revision = fixtures.inputs_digest(), fixtures.git_revision()
    passed = not checks.failed
    stamp = {"generated": now.isoformat(timespec="seconds"), "date": now.date().isoformat(),
             "revision": revision, "inputs_sha256": digest["sha256"],
             "tools": tool_versions(), "passed": passed,
             "checks": len(checks.rows), "seconds": round(time.time() - started)}
    stage_dir = ROOT / "fixtures" / "stages"
    stage_dir.mkdir(parents=True, exist_ok=True)
    for index, stage in enumerate(stage_defs()):
        for side in ("input", "output"):
            rows = [{"swipe": sc["id"], "message": sc["stages"][index][side]}
                    for sc in stages if sc["stages"][index][side] is not None]
            (stage_dir / f"{stage['id']}.{side}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    explorer = ROOT / "dashboard" / "data" / "stages.json"
    explorer.parent.mkdir(parents=True, exist_ok=True)
    explorer.write_text(json.dumps({**stamp, "stage_defs": stage_defs(), "scenarios": stages},
                                   indent=2, ensure_ascii=False) + "\n")
    report = ROOT / "docs" / "proof" / f"{now.date().isoformat()}-fixture-run.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(stamp, digest, swipes, expected, checks, shipped, stages))
    (report.parent / "latest.json").write_text(json.dumps(
        {**stamp, "report": report.name, "files": digest["files"]}, indent=2) + "\n")
    return report, explorer


def table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def render_report(stamp: dict, digest: dict, swipes: dict, expected: dict, checks: Checks,
                  shipped: dict, stages: list[dict]) -> str:
    p1, p2 = shipped["phase1"], shipped["phase2"]
    totals = expected["totals"]
    case_rows = []
    outcome = {c["name"]: c["ok"] for c in checks.rows}
    for scenario in stages:
        reason = scenario["reason"] or "sent to the warehouse"
        key = "warehouse" if scenario["outcome"] == "clean" else "quarantine"
        want = expected[key][scenario["id"]]
        want_text = ("a row in the warehouse" if key == "warehouse"
                     else want["reason_starts_with"].rstrip(": ") + "…")
        kind = "warehouse" if key == "warehouse" else "quarantine"
        passed = outcome.get(f"shipped: {kind} record {scenario['id']}", False)
        case_rows.append([f"`{scenario['id']}`", scenario["store"], want_text, reason,
                          "pass" if passed else "FAIL"])
    check_rows = [[("pass" if c["ok"] else "FAIL"), c["name"]] for c in checks.rows]
    first_clean = next(s for s in stages if s["outcome"] == "clean")
    first_bad = next(s for s in stages if s["id"] == "tampered")
    clean_record = json.dumps(shipped["bodies"][first_clean["txn_id"]], indent=2, ensure_ascii=False)
    bad_record = json.dumps(shipped["quarantine"][first_bad["txn_id"]], indent=2, ensure_ascii=False)
    return f"""# Fixture run: pos-guard and pos-uplink

**Result: {"PASS" if stamp["passed"] else "FAIL"}**, {stamp["checks"]} checks in {stamp["seconds"]} s.
Run on {stamp["generated"]}, at revision `{stamp["revision"]["short"]}`
{"with uncommitted changes to the proof inputs" if stamp["revision"]["dirty"] else "with a clean tree"}.
Inputs digest `{stamp["inputs_sha256"][:16]}`: the pipelines, store profiles,
store, warehouse and certificate code and the fixtures. `just test` fails when
the tree no longer matches this digest, so this report cannot describe a
different revision of the jobs.

Reproduce: `just proof` (about two minutes; needs `expanso-edge`, `expanso-cli`,
`uv` and `openssl`; starts localhost listeners only and stops them all).

## What ran

- `expanso-edge` nodes in local mode, one per store (`s1` Northgate, `s2` Riverside),
  each running `pipelines/pos-guard.yaml` and `pipelines/pos-uplink.yaml` exactly as
  committed.
- A store source serving {totals["swipes"]} raw swipes from `fixtures/swipes.json`: {totals["clean"]} clean
  sales and {totals["quarantined"]} faults, signed with enrolled till keys, plus register
  heartbeats and, for `s1` only, a climate sensor reading.
- A window display per store, and the real warehouse (`scripts/warehouse.py`:
  TLS 1.3, client certificate required) behind each store's WAN link
  (`stores.WanLink`, cuttable).
- Expected outputs computed in plain Python in `scripts/fixtures.py`, with the central
  side's own `jid1`, not read back from the pipeline.

Tools: {", ".join(f"{k} {v}" for k, v in stamp["tools"].items())}.

## Result per swipe

{table(["Swipe", "Store", "Expected", "Pipeline said", "Result"], case_rows)}

## The WAN leg

{table(["Phase", "Condition", "Warehouse rows", "Records queued on the store's disk", "Other"],
       [["1", "uplink presents a certificate from the wrong authority", p1["rows"], p1["queue"],
         f"{p1['refused_connections']} connections refused by the warehouse"],
        ["2", "right certificates, WAN link cut, nodes restarted", p2["rows"], p2["queue"],
         "queue file survived the restart"],
        ["3", "link restored", len(shipped["bodies"]), 0,
         f"{shipped['batch_count']} batches, {shipped['bytes_in']} bytes, "
         f"{shipped['duplicates']} duplicates"]])}

Delivery without a valid client certificate from the group's authority fails and the
records stay on disk; after the link returns they cross once. The rest of the negative
cases (plaintext, no certificate, TLS 1.2, another store's records) run against the
warehouse in `tests/test_wan_security.py`.

## Window display and register roster

{table(["Store", "Units by category sent to the window display"],
       [[sid, ", ".join(f"{c} {n}" for c, n in sorted(units.items()))]
        for sid, units in expected["window_units"].items()])}

Register roster at the end of the run: {", ".join(f"`{r}` {s}" for r, s in sorted(shipped["roster"].items()))}.

## Every check

{table(["Result", "Check"], check_rows)}

## What the warehouse stored for the first clean swipe

```json
{clean_record}
```

## What the store kept for the tampered swipe

```json
{bad_record}
```

## Files in this digest

{table(["File", "sha256"], [[f"`{k}`", v[:16]] for k, v in digest["files"].items()])}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", action="store_true", help="keep the work directory (logs, queues)")
    args = ap.parse_args()
    started = time.time()
    swipes = json.loads((ROOT / "fixtures" / "swipes.json").read_text())
    expected = json.loads((ROOT / "fixtures" / "expected.json").read_text())
    if fixtures.build()[0] != swipes:
        print("fixtures are out of date: run scripts/fixtures.py", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="pos-fixture-"))
    checks = Checks()
    ok = False
    try:
        shipped = shipped_pass(work, swipes, expected, checks)
        traced = traced_pass(work, swipes, expected, checks)
        checks.add("traced run reproduces the shipped run's warehouse records",
                   deep_equal_after_strip(shipped["bodies"], traced["bodies"]), "equal", "differs")
        left = shipped["left_listening"] + traced["left_listening"]
        checks.add("every listener and node stopped", not left, [], left)
        stages = assemble_stages(swipes, shipped, traced)
        report, explorer = write_outputs(swipes, expected, checks, shipped, traced, stages, started)
        ok = True
    finally:
        if args.keep or not ok:
            say(f"work directory kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    say(f"wrote {report.relative_to(ROOT)} and {explorer.relative_to(ROOT)}")
    failed = checks.failed
    say(f"{len(checks.rows) - len(failed)} of {len(checks.rows)} checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
