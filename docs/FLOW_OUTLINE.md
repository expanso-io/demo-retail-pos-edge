# Flow outline: point of sale at the edge

One page. Beats, architecture, and which of her problems each beat answers.

**Claim.** Card data never leaves the store as a card number: each swipe
becomes a one-way join ID with its context, checked before it touches the
core network.

Framing: a retail and payments group. No names anywhere.

## Her problems (and only these)

| # | Problem | Where the demo answers it |
|---|---|---|
| P1 | Card data cannot leave the card business | Beat 2: the card number stops at the store's edge node |
| P2 | Anonymize at the source, safe to cross borders | Beat 2: one-way keyed hash to a join ID, personal fields stripped |
| P3 | Same customer ID on both sides | Beat 4: the same card gives the same join ID at every store and on the central side |
| P4 | Context with each record | Beat 2: store, register, location, time, store temperature |
| P5 | Untrusted terminals, scan before the DMZ | Beat 3: tampered and malformed records quarantined at the store |
| P6 | Results back to the store and to the warehouse | Beat 2 and 4: store display reacts; warehouse fills |


## Beats (about 2:00)

1. **The trap (0:00-0:15).** Four stores ring up sales. Every swipe carries
   a card number, and the card number is not allowed out of the card
   business. Registers are cartoon figures; particles leave each one.
2. **The thing, live (0:15-0:45).** Start the two jobs in Expanso Cloud.
   Each store's edge node lights up. The inspector shows one real swipe on
   the left (card number, cardholder name, cashier) and the record that left
   on the right: a join ID, the basket, store, register, location, local
   time, store temperature, and the fields that were stripped. The store's
   window display changes with what is selling there. (P1, P2, P4, P6)
3. **The break, on purpose (0:45-1:20).** Presenter keys, one at a time:
   a tampered register (signature fails, quarantined with its reason),
   a register that leaks a card number into a receipt note (blocked before
   the DMZ), a store's network drop (that store's uplink queues to disk while
   its registers and display keep working, then drains as a burst), a dead
   temperature sensor (records keep flowing, marked "sensor missing"), a
   register that crashes (the store's edge flags it silent). (P5)
4. **The payoff (1:20-1:45).** Warehouse panel: records landed, **card
   numbers in the warehouse: 0** (a regex scan of every stored row, run
   live), shoppers recognised at more than one store by join ID alone, and
   join IDs matching the central side's online profiles. (P3, P6)
5. **Close (1:45-2:00).** Number still on screen. Closing line.

## Architecture

```
 store N (x4, 2-4 registers each)                  core network (DMZ)
 ┌────────────────────────────────────────┐
 │ registers ─POST signed swipe─┐         │
 │ climate sensor, heartbeats ──┤         │
 │                              ▼         │
 │  Expanso Edge node  job pos-guard      │
 │   verify signature, schema, ranges,    │
 │   injection scan; HMAC card -> join ID;│
 │   strip personal fields; add context;  │
 │   final card-number scan (block)       │
 │    ├─> quarantine file (stays in store)│
 │    ├─> store display (10 s sales window)
 │    └─> job pos-uplink (localhost)      │
 │         sqlite disk buffer ── WAN link ┼──> warehouse (SQLite + read API)
 └────────────────────────────────────────┘        ▲
          ▲  both jobs deployed from Expanso Cloud  │ online profiles keyed by
          │  (selector role=pos-store)              │ the same join ID
```

- **Expanso does the work.** Everything between a register and the
  warehouse is pipeline config in `pipelines/pos-guard.yaml` and
  `pipelines/pos-uplink.yaml`. The join-ID algorithm (HMAC-SHA256, version
  `jid1`) is defined in the job deployed from Cloud; its key is the node's
  provisioned secret, the same at every store and on the central side.
- **Custom code only:** register, sensor and display simulators plus each
  store's WAN link (`scripts/stores.py`), the warehouse sink and its read API
  (`scripts/warehouse.py`), and the board (`scripts/dashboard.py`,
  `dashboard/`).
- **Board:** storefronts with registers as figures (idle, ringing up,
  offline, alarmed), particles born per register at measured counts, edge
  node with the Expanso mark, quarantine bin, DMZ gate, warehouse. Panels:
  before/after inspector, quarantine with reasons, uplink queue per store,
  warehouse proof. Presenter controls drive only simulators and links;
  the board never deploys, starts or stops a job. Light, dark toggle.
- **Control plane:** the shared Cloud workspace has no running job without
  a selector today (checked 2026-10-01), so the jobs run from Expanso
  Cloud. `just up-local` runs a local control plane per node; the board
  shows which mode is live.
