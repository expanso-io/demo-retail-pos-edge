#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Offline checks for the producer and the warehouse. No processes started.

The end-to-end proof is the running Cloud-managed demo; this covers the pieces that must agree with pos-guard's Bloblang.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import random
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stores  # noqa: E402
import warehouse  # noqa: E402


def test_pans_are_luhn_valid() -> None:
    rng = random.Random(1)
    for _ in range(200):
        pan = stores.luhn_pan("4", 16, rng)
        assert warehouse.luhn_ok(pan), pan


def test_join_id_is_stable_and_one_way() -> None:
    a = stores.join_id("k" * 32, "4539148803436467")
    assert a == stores.join_id("k" * 32, "4539148803436467")
    assert a != stores.join_id("j" * 32, "4539148803436467")
    assert a.startswith("jid1_") and len(a) == 29
    assert "4539" not in a


def test_signature_matches_pos_guard_canon() -> None:
    # pos-guard signs txn|till|ts|total|pan|sku:qty:unit,... under the till key
    rec = {"txn_id": "s1-r1-abc-00001", "register_id": "s1-r1", "ts": "2026-10-01T10:00:00.000Z",
           "total_cents": 760, "pan": "4539148803436467",
           "items": [{"sku": "CD-01", "qty": 1, "unit_cents": 420},
                     {"sku": "HD-01", "qty": 1, "unit_cents": 340}]}
    canon = "s1-r1-abc-00001|s1-r1|2026-10-01T10:00:00.000Z|760|4539148803436467|CD-01:1:420,HD-01:1:340"
    want = hmac.new(b"key", canon.encode(), hashlib.sha256).hexdigest()
    reg = stores.Register.__new__(stores.Register)
    reg.key = "key"
    assert reg.sign(rec) == want


def test_till_record_is_raw_terminal_data() -> None:
    # The till emits what a terminal prints; names and categories come from
    # the catalog at the edge, never from the register.
    rng = random.Random(7)
    store = {"store_id": "s1", "registers": 1}
    shopper = stores.make_shoppers([store], 1, rng)[0]
    assert shopper["track2"].startswith(shopper["pan"] + "=" + shopper["expiry"])
    assert len(shopper["cvv"]) == 3 and shopper["name"].isupper()
    assert warehouse.card_numbers_in({"t": shopper["track2"]}) == [shopper["pan"]]
    catalog = {sku: (name, category) for sku, name, category, _, _ in stores.CATALOG}
    world = stores.World.__new__(stores.World)
    world.sensor_temp = lambda _sid: None
    reg = stores.Register.__new__(stores.Register)
    reg.world = world
    for line in reg.pick_items(rng, None):
        assert set(line) == {"sku", "qty", "unit_cents"}, line
        assert line["sku"] in catalog


def test_warehouse_scan_finds_card_numbers_anywhere() -> None:
    assert warehouse.card_numbers_in({"note": "card 4539 1488 0343 6467 declined"})
    assert warehouse.card_numbers_in({"a": [{"b": "4539148803436467"}]})
    assert not warehouse.card_numbers_in({"join_id": "jid1_0c027fc5f20cc43fb27dba81",
                                          "local_time": "2026-10-02T05:23:35+02:00"})


def test_warehouse_scan_ignores_digits_inside_identifiers() -> None:
    # 310087491616811 is Luhn-valid and sits inside a real join ID's hex.
    assert not warehouse.card_numbers_in({"join_id": "jid1_5b310087491616811bec96d9"})
    assert warehouse.card_numbers_in({"note": "card 4111 1111 1111 1111, declined"})
    assert warehouse.card_numbers_in({"note": "4111-1111-1111-1111"})
    assert warehouse.card_numbers_in({"note": "(4111111111111111)"})


def test_database_retains_uncollected_records_across_restart() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        path = Path(directory) / "events.db"
        database = stores.StoreDatabase(path)
        rec = {"txn_id": "s1-r1-one", "register_id": "s1-r1", "ts": "old"}
        database.append(rec, None, captured_at=100)
        database.append(rec, None, captured_at=101)
        assert database.snapshot("s1") == {
            "total": 1, "pending": 1, "collected": 0, "last_txn": "s1-r1-one"}
        assert not database.acknowledge("s1", rec["txn_id"])
        leased = database.lease("s1", now=200)
        assert leased == [{"record": rec, "captured_at": 100}]
        assert database.lease("s1", now=201) == []
        database.db.close()
        database = stores.StoreDatabase(path)
        assert database.snapshot("s1")["pending"] == 1
        assert database.lease("s1", now=261) == leased
        assert not database.acknowledge("s2", rec["txn_id"])
        assert database.acknowledge("s1", rec["txn_id"])
        assert database.acknowledge("s1", rec["txn_id"])
        assert database.snapshot("s1")["collected"] == 1
        assert database.lease("s1", now=400) == []
        assert database.read(rec["txn_id"])["record"] == rec
        database.db.close()


def test_telemetry_poll_preserves_source_freshness() -> None:
    world = stores.World.__new__(stores.World)
    world.telemetry = {"s1": {}}
    world.telemetry_lock = threading.Lock()
    world.telemetry_polled = {}
    world.sensor_sent = stores.collections.Counter()
    body = {"type": "climate", "temp_c": 22, "at": 100}
    world.remember_telemetry("s1", "climate", body)
    assert world.read_telemetry("s1") == [body]
    assert world.read_telemetry("s1")[0]["at"] == 100
    assert world.sensor_sent["s1"] == 2


def test_warehouse_receipts_are_ordered_and_deduplicated() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT / ".runtime") as folder:
        sink = warehouse.Warehouse(Path(folder) / "warehouse.db", Path(folder) / "profiles.json")
        records = [{"txn_id": f"receipt-{i}", "join_id": "jid1_test",
                    "total_cents": i, "context": {"store_id": "s1"}} for i in range(65)]
        sink.ingest(json.dumps(records).encode(), "s1")
        sink.ingest(json.dumps(records).encode(), "s1")
        state = sink.stats()
        receipts = state["recent_receipts"]
        assert state["rows"] == 65
        assert len(receipts) == 60
        assert receipts[0]["record"]["txn_id"] == "receipt-64"
        assert receipts[-1]["record"]["txn_id"] == "receipt-5"
        assert receipts[0]["received_at"] > 0
        sink.conn.close()


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ok: {len(tests)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
