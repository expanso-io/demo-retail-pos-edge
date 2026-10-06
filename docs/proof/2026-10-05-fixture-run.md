# Fixture run: pos-guard and pos-uplink

**Result: PASS**, 56 checks in 63 s.
Run on 2026-10-05T20:28:57-07:00, at revision `adff223`
with a clean tree.
Inputs digest `d517c36488ce61de`: the pipelines, store profiles,
store, warehouse and certificate code and the fixtures. `just test` fails when
the tree no longer matches this digest, so this report cannot describe a
different revision of the jobs.

Reproduce: `just proof` (about two minutes; needs `expanso-edge`, `expanso-cli`,
`uv` and `openssl`; starts localhost listeners only and stops them all).

## What ran

- `expanso-edge` nodes in local mode, one per store (`s1` Northgate, `s2` Riverside),
  each running `pipelines/pos-guard.yaml` and `pipelines/pos-uplink.yaml` exactly as
  committed.
- A store source serving 14 raw swipes from `fixtures/swipes.json`: 6 clean
  sales and 8 faults, signed with enrolled till keys, plus register
  heartbeats and, for `s1` only, a climate sensor reading.
- A window display per store, and the real warehouse (`scripts/warehouse.py`:
  TLS 1.3, client certificate required) behind each store's WAN link
  (`stores.WanLink`, cuttable).
- Expected outputs computed in plain Python in `scripts/fixtures.py`, with the central
  side's own `jid1`, not read back from the pipeline.

Tools: expanso-edge v2.1.21, expanso-cli Expanso CLI version v2.1.21, python 3.12.13, openssl OpenSSL 3.6.4 25 Aug 2026 (Library: OpenSSL 3.6.4 25 Aug 2026).

## Result per swipe

| Swipe | Store | Expected | Pipeline said | Result |
|---|---|---|---|---|
| `clean-hot-drinks` | s1 | a row in the warehouse | sent to the warehouse | pass |
| `clean-cold-drinks` | s1 | a row in the warehouse | sent to the warehouse | pass |
| `clean-same-card-other-store` | s2 | a row in the warehouse | sent to the warehouse | pass |
| `clean-three-lines` | s2 | a row in the warehouse | sent to the warehouse | pass |
| `clean-bakery` | s1 | a row in the warehouse | sent to the warehouse | pass |
| `clean-sandwiches` | s2 | a row in the warehouse | sent to the warehouse | pass |
| `tampered` | s1 | signature mismatch: altered after the register signed it… | signature mismatch: altered after the register signed it | pass |
| `card-number-in-note` | s2 | card number blocked: raw card number in note… | card number blocked: raw card number in note | pass |
| `malformed` | s1 | malformed: missing pan… | malformed: missing pan | pass |
| `injection` | s2 | injection pattern in a text field… | injection pattern in a text field | pass |
| `stale-replay` | s1 | stale… | stale: 400s old, possible replay | pass |
| `unknown-register` | s1 | unknown register: not enrolled here… | unknown register: not enrolled here | pass |
| `totals-mismatch` | s2 | totals: items add to 550, receipt says 700… | totals: items add to 550, receipt says 700 | pass |
| `over-limit` | s1 | range: total over limit… | range: total over limit | pass |

## The WAN leg

| Phase | Condition | Warehouse rows | Records queued on the store's disk | Other |
|---|---|---|---|---|
| 1 | uplink presents a certificate from the wrong authority | 0 | 6 | 14 connections refused by the warehouse |
| 2 | right certificates, WAN link cut, nodes restarted | 0 | 6 | queue file survived the restart |
| 3 | link restored | 6 | 0 | 2 batches, 4807 bytes, 0 duplicates |

Delivery without a valid client certificate from the group's authority fails and the
records stay on disk; after the link returns they cross once. The rest of the negative
cases (plaintext, no certificate, TLS 1.2, another store's records) run against the
warehouse in `tests/test_wan_security.py`.

## Window display and register roster

| Store | Units by category sent to the window display |
|---|---|
| s1 | bakery 2, cold drinks 2, hot drinks 2 |
| s2 | cold drinks 1, fresh fruit 1, ice cream 1, sandwiches 3, snacks 2 |

Register roster at the end of the run: `s1-r1` ok, `s1-r2` closed, `s1-r3` silent, `s2-r1` ok, `s2-r2` closed.

## Every check

| Result | Check |
|---|---|
| pass | phase 1: wrong-authority certificate delivers nothing |
| pass | phase 1: the warehouse counted the refused connections |
| pass | phase 1: every clean record waits on the store's disk |
| pass | phase 2: the queue survives a node restart |
| pass | phase 2: nothing lands while the link is cut |
| pass | phase 3: queue empty after the drain |
| pass | shipped: every swipe collected and acknowledged at the source |
| pass | shipped: warehouse rows |
| pass | shipped: warehouse record clean-hot-drinks |
| pass | shipped: warehouse record clean-cold-drinks |
| pass | shipped: warehouse record clean-same-card-other-store |
| pass | shipped: warehouse record clean-three-lines |
| pass | shipped: warehouse record clean-bakery |
| pass | shipped: warehouse record clean-sandwiches |
| pass | shipped: warehouse duplicates ignored |
| pass | shipped: warehouse's own card-number scan |
| pass | shipped: quarantine records |
| pass | shipped: quarantine record tampered |
| pass | shipped: quarantine record card-number-in-note |
| pass | shipped: quarantine record malformed |
| pass | shipped: quarantine record injection |
| pass | shipped: quarantine record stale-replay |
| pass | shipped: quarantine record unknown-register |
| pass | shipped: quarantine record totals-mismatch |
| pass | shipped: quarantine record over-limit |
| pass | shipped: no card number in any quarantine record |
| pass | shipped: one card, two stores, one join ID |
| pass | shipped: window display units at s1 |
| pass | shipped: window display units at s2 |
| pass | shipped: register roster |
| pass | traced: every swipe collected and acknowledged at the source |
| pass | traced: warehouse rows |
| pass | traced: warehouse record clean-hot-drinks |
| pass | traced: warehouse record clean-cold-drinks |
| pass | traced: warehouse record clean-same-card-other-store |
| pass | traced: warehouse record clean-three-lines |
| pass | traced: warehouse record clean-bakery |
| pass | traced: warehouse record clean-sandwiches |
| pass | traced: warehouse duplicates ignored |
| pass | traced: warehouse's own card-number scan |
| pass | traced: quarantine records |
| pass | traced: quarantine record tampered |
| pass | traced: quarantine record card-number-in-note |
| pass | traced: quarantine record malformed |
| pass | traced: quarantine record injection |
| pass | traced: quarantine record stale-replay |
| pass | traced: quarantine record unknown-register |
| pass | traced: quarantine record totals-mismatch |
| pass | traced: quarantine record over-limit |
| pass | traced: no card number in any quarantine record |
| pass | traced: one card, two stores, one join ID |
| pass | traced: window display units at s1 |
| pass | traced: window display units at s2 |
| pass | traced: register roster |
| pass | traced run reproduces the shipped run's warehouse records |
| pass | every listener and node stopped |

## What the warehouse stored for the first clean swipe

```json
{
  "basket": [
    {
      "category": "hot drinks",
      "name": "Flat white",
      "qty": 2,
      "sku": "HD-01",
      "unit_cents": 340
    },
    {
      "category": "bakery",
      "name": "Croissant",
      "qty": 1,
      "sku": "BK-01",
      "unit_cents": 210
    }
  ],
  "card_brand": "visa",
  "context": {
    "country": "DE",
    "district": "Northgate",
    "hour": 11,
    "lat": 52.55,
    "local_time": "2026-10-05T11:15:02+02:00",
    "lon": 13.41,
    "register_id": "s1-r1",
    "sensor": "ok",
    "sensor_at": 1791257275.736379,
    "store_id": "s1",
    "store_name": "Northgate",
    "temp_c": 21.4,
    "weekday": "Monday"
  },
  "currency": "EUR",
  "edge_at": "2026-10-05T20:27:55.748279-07:00",
  "entry_mode": "contactless",
  "join_id": "jid1_2b4482b6875ffdb4c6eff793",
  "note": "",
  "stripped": [
    "pan",
    "track2",
    "cvv",
    "expiry",
    "cardholder",
    "auth_code",
    "cashier",
    "loyalty_email"
  ],
  "total_cents": 890,
  "txn_id": "s1-r1-fixture-00001",
  "uplink": {
    "queued_at": "2026-10-05T20:28:26.380052-07:00",
    "store_id": "s1"
  }
}
```

## What the store kept for the tampered swipe

```json
{
  "note": "",
  "quarantined_at": "2026-10-05T20:27:55.74632-07:00",
  "reason": "signature mismatch: altered after the register signed it",
  "reasons": [
    "signature mismatch: altered after the register signed it"
  ],
  "register_id": "s1-r1",
  "store_id": "s1",
  "total_cents": 211,
  "txn_id": "s1-r1-fixture-00007"
}
```

## Files in this digest

| File | sha256 |
|---|---|
| `pipelines/pos-guard.yaml` | f0e4bafd3a99f917 |
| `pipelines/pos-uplink.yaml` | d999f03feeb5ea68 |
| `config/stores.json` | bb9e7123cc444550 |
| `scripts/stores.py` | 27935905f179a2ff |
| `scripts/warehouse.py` | 199fb3fe9e31af0f |
| `scripts/pki.py` | e6ac4621392ce8e7 |
| `scripts/fixtures.py` | fc326ffd3a3b084b |
| `scripts/fixture_run.py` | 42eb8b6059a93670 |
| `fixtures/swipes.json` | 24ee975b8df63474 |
| `fixtures/expected.json` | bf1b8f4e1ce46659 |
| `fixtures/replay/guard.input.jsonl` | fe9cdb32da08553e |
| `fixtures/replay/uplink.input.jsonl` | cf271788252bcc10 |
| `fixtures/replay/guard.expected.schema.json` | 50d55031fbd0782c |
| `fixtures/replay/uplink.expected.schema.json` | f0fbfc27f4ccead7 |
| `fixtures/replay/config/register-keys.json` | b7566774a3a19ee1 |
| `fixtures/replay/config/join-id-key.txt` | 8dd15ccf35f4495c |
| `fixtures/replay/config/store-profile.json` | 42b5e6baa758cca9 |
| `fixtures/replay/config/store-catalog.json` | b047c34559555868 |
