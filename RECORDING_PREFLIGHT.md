---
# This is a declaration of readiness, not a command that starts anything.
# `recording-preflight.py` validates this front matter and the required sections.
status: BLOCKED # PASS | PASS_WITH_NAMED_WARNINGS | BLOCKED
record_check: NOT_RUN # PASS only after `just record-check` has passed
record_check_evidence: "" # command, date/time, and the result you observed
accepted_warnings: [] # required and non-empty only for PASS_WITH_NAMED_WARNINGS
---

# Recording preflight — card data stays in the store

This is the final human review before a take. It does **not** start a recorder,
runtime, deployment, Cloud operation, publish action, or browser.

## Claim

Card data never leaves the store as a card number: each swipe becomes a
one-way join ID with its context, checked before it touches the core network.

## Audience and objection

The payments lead of a retail and payments group. Her objection: regulated
card data cannot leave the card business, and terminals in stores cannot be
trusted. The take shows the card number replaced at the store, untrusted
records kept in the store, and the warehouse holding none.

## Capture surface

`http://localhost:8023` full screen at 1440x900 in an app-mode window, with
a cut to the Expanso Cloud console Jobs page (light) to start the two jobs.
No 9:16 crop planned.

## Opening state

After `just up`: both jobs deployed and stopped, four nodes connected, tills
ringing with blue swipes fading at grey nodes, warehouse 0 records, nothing
kept, nothing on disk.

## Operator action

Start `pos-guard` and `pos-uplink` in the Expanso Cloud console, then the
presenter keys listed in `RECORDING_SCRIPT.md`.

## Visible outcome

Nodes turn green, records cross the DMZ, the inspector shows a raw swipe
beside the record that left; tampered and card-number records appear under
"Kept in the store"; a dropped store queues on disk and drains; the
warehouse shows 0 card numbers found.

## Truth boundary

Tills, sensors, window displays and WAN links are local processes
(`scripts/stores.py`); the warehouse is a local SQLite sink. The two jobs
are deployed, started and stopped by Expanso Cloud and run on four local
Expanso Edge nodes. Every number on the board is measured from those
processes.

## Proof evidence

Observed 2026-10-01 on Expanso Cloud: both jobs Running (not Degraded) a
minute after start and through a network drop; Riverside's queue drained
to 0 with 0 duplicates; 1,483 records, 0 card numbers found. Not yet
observed: the Cloud console Logs and Monitoring pages during a run
(`expanso-cli job logs` fails with a websocket handshake error for every
job in the workspace), `just record-check`, and the dvv preflight.

## Decision

BLOCKED until someone signed in to the Cloud console confirms the Logs view
streams and both Monitoring charts move during a run, then runs
`just record-check` and the dvv preflight.
