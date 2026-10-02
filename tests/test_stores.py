#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Offline checks for the producer and the warehouse. No processes started.

The end-to-end proof is the running demo (just up-local, then the presenter
keys); this covers the pieces that must agree with pos-guard's Bloblang.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import sys
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


def test_warehouse_scan_finds_card_numbers_anywhere() -> None:
    assert warehouse.card_numbers_in({"note": "card 4539 1488 0343 6467 declined"})
    assert warehouse.card_numbers_in({"a": [{"b": "4539148803436467"}]})
    assert not warehouse.card_numbers_in({"join_id": "jid1_0c027fc5f20cc43fb27dba81",
                                          "local_time": "2026-10-02T05:23:35+02:00"})


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ok: {len(tests)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
