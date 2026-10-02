#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Serve the board and /api/state for demo-retail-pos-edge. Localhost only.

/api/state merges what the running system reports, nothing invented:

* the stores process: registers, sensors, window displays, WAN links
* the warehouse: rows landed, its own card-number scan, join-ID matches
* each store node's quarantine file and disk queue (read-only)
* Expanso Cloud: which store nodes run pos-guard and pos-uplink (cloud mode),
  or which jobs are listening on each node (local control plane)

POST /api/control/<verb> drives only the simulators and the WAN links. The
board never deploys, starts or stops a job; that happens in Expanso Cloud.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
RUNTIME = ROOT / ".runtime"
CONFIG = ROOT / "config" / "stores.json"
STORES_URL = "http://127.0.0.1:8027"
WAREHOUSE_URL = "http://127.0.0.1:8026"
SWIPE_BASE, OUTBOX_BASE = 7300, 7340
JOBS = ("pos-guard", "pos-uplink")
CONTROL_VERBS = {"register", "fault", "sensor", "link", "reset"}


def get_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def post_json(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode(errors="replace")}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 502, {"error": str(exc)}


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def env_file() -> dict[str, str]:
    out = {}
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def store_ids() -> list[str]:
    return [s["store_id"] for s in json.loads(CONFIG.read_text())["stores"]]


def queue_depth(store: str) -> int | None:
    db = RUNTIME / "edge" / store / "uplink-queue.db"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.3) as conn:
            return conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    except sqlite3.Error:
        return None


def quarantine(store: str) -> tuple[int, list[dict]]:
    path = RUNTIME / "edge" / store / "quarantine.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0, []
    recent = []
    for line in lines[-12:]:
        try:
            recent.append(json.loads(line))
        except ValueError:
            continue
    return len(lines), recent


class CloudWatch:
    """Polls Expanso Cloud every few seconds for each store node's jobs."""

    def __init__(self) -> None:
        self.state: dict = {"ok": False, "nodes": {}, "error": None, "at": 0}
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def cli(self, env: dict[str, str], *args: str) -> list:
        # Endpoint and key are passed explicitly: no CLI profile is ever read.
        out = subprocess.run(
            ["expanso-cli", "--endpoint", env["EXPANSO_CLI_ENDPOINT"],
             "--api-key", env["EXPANSO_CLI_API_KEY"], *args, "-f", "json"],
            capture_output=True, text=True, timeout=12, check=True)
        try:
            data = json.loads(out.stdout or "[]")
        except ValueError:
            return []  # e.g. a stopped job with no executions prints a sentence
        return data if isinstance(data, list) else []

    def poll(self) -> dict:
        env = env_file()
        if not env.get("EXPANSO_CLI_API_KEY"):
            return {"ok": False, "nodes": {}, "error": "no Cloud credentials"}
        nodes = self.cli(env, "node", "list", "-L", "role=pos-store")
        running: dict[str, set[str]] = {}
        for job in JOBS:
            execs = self.cli(env, "job", "executions", job, "--limit", "100")
            running[job] = {e["node_id"] for e in execs
                            if e["status"]["desired_state"]["state_type"] == "running"
                            and e["status"]["observed_state"]["state_type"] == "running"}
        out: dict[str, dict] = {}
        for n in nodes:
            store = n["spec"]["name"].removeprefix("pos-").removesuffix("-edge")
            now = {"connected": n["status"]["connection_state"] == "connected",
                   **{job: n["id"] in running[job] for job in JOBS}}
            prev = out.get(store, {k: False for k in now})
            out[store] = {k: now[k] or prev[k] for k in now}
        return {"ok": True, "nodes": out, "error": None}

    def loop(self) -> None:
        while True:
            if (RUNTIME / "mode").exists() and (RUNTIME / "mode").read_text().strip() == "cloud":
                try:
                    result = self.poll()
                except (subprocess.SubprocessError, ValueError, KeyError, OSError) as exc:
                    result = {"ok": False, "nodes": self.state.get("nodes", {}),
                              "error": str(exc)[:200]}
                result["at"] = time.time()
                with self.lock:
                    self.state = result
            time.sleep(3)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


class Scenario:
    """An optional automatic cycle of the failures, for an unattended board.

    It only calls the same simulator controls as the presenter keys.
    """

    STEPS = [
        (10, "fault", {"register_id": "s1-r1", "kind": "tamper"}),
        (12, "fault", {"register_id": "s2-r1", "kind": "leak"}),
        (4, "link", {"store_id": "s3", "cut": True}),
        (26, "link", {"store_id": "s3", "cut": False}),
        (10, "sensor", {"store_id": "s4", "on": False}),
        (22, "sensor", {"store_id": "s4", "on": True}),
        (6, "fault", {"register_id": "s1-r3", "kind": "crash"}),
        (28, "register", {"register_id": "s1-r3", "state": "open"}),
        (8, "fault", {"register_id": "s3-r2", "kind": "malformed"}),
        (14, "fault", {"register_id": "s4-r2", "kind": "inject"}),
    ]

    def __init__(self) -> None:
        self.on = False
        self.step = 0
        self.wake = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def set(self, on: bool) -> None:
        self.on = on
        self.step = 0
        self.wake.set()

    def loop(self) -> None:
        while True:
            if not self.on:
                self.wake.wait()
                self.wake.clear()
                continue
            delay, verb, body = self.STEPS[self.step % len(self.STEPS)]
            if self.wake.wait(delay):
                self.wake.clear()
                continue
            if self.on:
                post_json(f"{STORES_URL}/{verb}", body)
                self.step += 1


def mode() -> str:
    try:
        return (RUNTIME / "mode").read_text().strip()
    except OSError:
        return "off"


def build_state(cloud: CloudWatch, scenario: Scenario) -> dict:
    sim = get_json(f"{STORES_URL}/state") or {"stores": []}
    wh = get_json(f"{WAREHOUSE_URL}/stats") or {}
    cl = cloud.snapshot()
    current = mode()
    feed: list[dict] = []
    for n, store in enumerate(sim.get("stores", []), 1):
        sid = store["store_id"]
        count, recent = quarantine(sid)
        feed += recent
        local = {"pos-guard": listening(SWIPE_BASE + n), "pos-uplink": listening(OUTBOX_BASE + n)}
        node = cl.get("nodes", {}).get(sid, {}) if current == "cloud" else {}
        store["edge"] = {
            "connected": node.get("connected", current == "local" and any(local.values())),
            "jobs": {job: (node.get(job, False) if current == "cloud" else local[job])
                     for job in JOBS},
            "listening": local,
        }
        store["quarantined"] = count
        store["queue"] = queue_depth(sid)
        store["warehouse_rows"] = wh.get("per_store", {}).get(sid, {}).get("rows", 0)
    feed.sort(key=lambda q: q.get("quarantined_at", ""), reverse=True)
    return {
        "at": time.time(),
        "mode": current,
        "cloud": {"ok": cl.get("ok", False), "error": cl.get("error"), "at": cl.get("at", 0)},
        "stores": sim.get("stores", []),
        "warehouse": {k: wh.get(k) for k in (
            "rows", "shoppers", "shoppers_multi_store", "online_profiles", "online_matched",
            "countries", "card_scan", "duplicates_ignored", "rows_without_temperature")},
        "latest_txn": (wh.get("recent") or [{}])[0].get("txn_id"),
        "quarantine": feed[:10],
        "auto": scenario.on,
    }


def inspect(txn: str | None) -> dict:
    if not txn:
        stats = get_json(f"{WAREHOUSE_URL}/stats") or {}
        txn = (stats.get("recent") or [{}])[0].get("txn_id")
    if not txn:
        return {"txn_id": None}
    raw = get_json(f"{STORES_URL}/raw/{txn}")
    shared = get_json(f"{WAREHOUSE_URL}/record/{txn}")
    return {"txn_id": txn, "raw": raw and raw.get("record"),
            "shared": shared and shared.get("record"),
            "queued_s": shared and shared.get("queued_s")}


class Handler(SimpleHTTPRequestHandler):
    cloud: CloudWatch
    scenario: Scenario

    def reply(self, code: int, obj: object) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.startswith("/api/state"):
            self.reply(200, build_state(self.cloud, self.scenario))
        elif self.path.startswith("/api/inspect"):
            txn = self.path.partition("txn=")[2] or None
            self.reply(200, inspect(txn))
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        verb = self.path.removeprefix("/api/control/")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self.reply(400, {"error": "bad json"})
            return
        if verb == "auto":
            self.scenario.set(bool(body.get("on")))
            self.reply(200, {"auto": self.scenario.on})
        elif verb in CONTROL_VERBS:
            self.reply(*post_json(f"{STORES_URL}/{verb}", body))
        else:
            self.reply(404, {"error": "unknown control"})

    def end_headers(self) -> None:
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8023)
    ap.add_argument("--check", action="store_true", help="validate the state contract and exit")
    args = ap.parse_args()

    if args.check:
        assert (DASHBOARD / "index.html").is_file()
        assert store_ids(), "config/stores.json lists no stores"
        for name in ("app.js", "styles.css", "fonts/fonts.css"):
            assert (DASHBOARD / name).is_file(), f"dashboard/{name} missing"
        print(f"ok: dashboard files, {len(store_ids())} stores configured")
        return 0

    Handler.cloud = CloudWatch()
    Handler.scenario = Scenario()
    handler = partial(Handler, directory=str(DASHBOARD))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"board on http://localhost:{args.port}", flush=True)
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
