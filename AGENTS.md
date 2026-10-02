# demo-retail-pos-edge — agent rules

The rules in `../AGENTS.md` govern this directory; this file only adds to
them.

- The claim: **Card data never leaves the store as a card number: each
  swipe becomes a one-way join ID with its context, checked before it
  touches the core network.** Every element on screen serves it or gets cut.
- Confidential origin: never name the prospect, its group companies or the
  people in the meeting, anywhere. It is "a retail and payments group".
  Her problems are listed in `docs/FLOW_OUTLINE.md`; build to those and no
  more. No "illustrative", "simulated", "mock" or "sample" labels
  (`just clean-check` enforces the words).
- Expanso does the work: scan, join ID, stripping, context, card-number
  block, quarantine, display window, disk queue and drain are all in
  `pipelines/`. Custom code is only `scripts/stores.py` (tills, sensors,
  displays, WAN links), `scripts/warehouse.py` and the board.
- The till signature canon in `scripts/stores.py` (`Register.sign`) must
  match `scan_swipe` in `pos-guard.yaml`; `tests/test_stores.py` pins it.
- Bloblang: a line may not start with `.method`; break chains with `let`.
  A `mapping` starts from an empty root, so a `catch` mapping must begin
  `root = this`.
- The uplink uses a `retry` output on purpose: without it the `sqlite`
  buffer re-reads a failed batch's records and the drain delivers
  duplicates.
- Pipeline INFO logs go to `<data-dir>/executions/*/logs/pipeline.log`, not
  the node console (WARN and up only).
- Secrets live only in `.env` (mode 600) and `.cloud-state/`; demo.sh
  generates `JOIN_ID_KEY` and `REGISTER_KEY_SEED` once. Expanso CLI calls
  always pass `--endpoint` and `--api-key`; never a profile, never
  `~/.expanso`, never `expanso-edge` without `--data-dir`.
- Presenter UI is localhost only. The board drives simulators and links,
  never Cloud jobs.
- Before a commit: `just check` (includes the JS anti-slop gate; fix
  spacing with `oxlint --fix` from `../../anti-slop`).

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
