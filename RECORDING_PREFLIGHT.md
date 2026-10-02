---
# This is a declaration of readiness, not a command that starts anything.
# `recording-preflight.py` validates this front matter and the required sections.
status: BLOCKED # PASS | PASS_WITH_NAMED_WARNINGS | BLOCKED
record_check: NOT_RUN # PASS only after `just record-check` has passed
record_check_evidence: "" # command, date/time, and the result you observed
accepted_warnings: [] # required and non-empty only for PASS_WITH_NAMED_WARNINGS
---

# Recording preflight — <what this demo proves>

This is the final human review before a take. It does **not** start a recorder,
runtime, deployment, Cloud operation, publish action, or browser. It records why
the already-running, mechanically checked demo is ready to put on camera.

`just record-check` is the mechanical lane: tests, UI lint, and the demo's
live endpoint. This file is the story/proof lane. Passing one does not prove the
other.

## Claim

<One defensible sentence the intended viewer should remember.>

## Audience and objection

<Who is watching, what they doubt, and why this take answers it.>

## Capture surface

<The localhost URL or named Cloud screen that will be in the take. State the
intended viewport and whether a 9:16 crop is planned.>

## Opening state

<The true zero or pre-action state visible before narration starts.>

## Operator action

<The one deliberate action that advances the story. “Nothing” is acceptable
only when the demo truthfully advances on its own clock.>

## Visible outcome

<The exact on-screen change that proves the claim, not a generic success card.>

## Truth boundary

<What is local, simulated, Cloud-accepted, downstream-received, or planned.
Do not present one lane as evidence of another.>

## Proof evidence

<Direct evidence for every claimed Cloud, integration, deployment, delivery, or
publication fact. Link to the owning system or say the claim is representative.>

## Decision

<Why the selected status above is honest. For PASS_WITH_NAMED_WARNINGS, name
each warning, its viewer impact, and why it is accepted for this take.>
