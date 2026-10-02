# Recording script — <what this demo shows>

One take, about 2:00 end to end, with roughly 110 seconds of speech in it. Five
beats. Every screen state below is produced by the demo on its own clock; the
only thing the operator does is start it.

Copy this to `RECORDING_SCRIPT.md` in the demo root and fill it in. `just
video-check` fails without it.

## Before you roll

```bash
just up      # warm-up: prove it renders and the Edge is healthy
just down
```

Do the warm-up. The first `uv run` of a session resolves dependencies and the
first Edge start builds its data directory. Neither belongs in a take.

Then run the environment preflight — Chrome in `--app` mode, display scaled
down, Cloud console matched to the demo's declared theme, notifications off:

```bash
~/.claude/skills/demo-video-verify/scripts/dvv preflight --url <DEMO_URL>
```

Recording surface: `<DEMO_URL>` full screen.

**Timing.** Offsets are seconds after `<the anchor event>`. The driver waits for
that anchor rather than guessing, so a slipped take runs longer but never
desynchronises.

**Pace.** The spoken text below is ~280 words. Beat spacing assumes a brisk 155
words a minute. Slow down and the beats arrive before you do.

---

## Beat 1 — <NAME> · 0:00–0:15

**Screen state before you speak.** <What must be true on screen.>

**Driver** — <command, or "nothing to type">

> <The contradiction. Not a preamble — state the problem as something that
> cannot be true, or state the conclusion. Never "So one thing we get a lot of
> questions about is…">

---

## Beat 2 — <NAME> · 0:15–0:40

**Screen state before you speak.** <...>

> <One sentence per idea. Name the pain before the feature.>

---

## Beat 3 — <NAME> · 0:40–1:05

**Screen state before you speak.** <The failure mode firing — link drop,
bad reading quarantined, threshold crossed. Something going wrong on purpose is
the most persuasive thing in any demo.>

> <...>

---

## Beat 4 — <NAME> · 1:05–1:30

**Screen state before you speak.** <The payoff, with the headline number
rendered.>

> <**Say the number out loud, slowly, then stop talking for a beat.** Every
> figure rendered on screen must be spoken. If it isn't worth saying, don't
> render it.>

---

## Beat 5 — CLOSE · 1:30–2:00

**Screen state before you speak.** <Final state, number still visible.>

> <The closing line. Write it here and say it verbatim — it is the sentence
> people repeat.>

---

## Required lines

Anything listed here must appear in the take. `demo-video-verify` checks the
transcript against them.

- "<the headline number, spoken>"
- "<the closing line>"

## Prohibited

Never spoken or shown: [B]acalhau, program names, classification-adjacent claims,
third-party vendor names. Every quantitative claim either cites a public source
in `docs/RESEARCH.md` or is introduced as representative.

## After the take

```bash
~/.claude/skills/demo-video-verify/scripts/dvv verify <VIDEO> --script RECORDING_SCRIPT.md
```

Non-zero exit means it is not publishable yet.
