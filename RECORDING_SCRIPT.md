# Recording script: card data stays in the store

One take, about 2:00 end to end, with roughly 110 seconds of speech. Five
beats. The registers, sensors and links run on their own clock; the
operator starts the two jobs in Expanso Cloud and presses presenter keys on
the board.

## Before you roll

```bash
just up-local   # warm-up: proves the nodes, jobs and board render
just down
just up         # the take: jobs deployed to Expanso Cloud and stopped
```

`just up` starts from nothing: warehouse empty, a node per store connected
to Expanso Cloud, `pos-guard` and `pos-uplink` deployed but stopped, the
tills already ringing with nothing listening. Open the board at
`http://localhost:8023` in an app-mode window, and the Expanso Cloud
console (set to light) on the Jobs page in a second window.

Then run the environment preflight:

```bash
~/.claude/skills/demo-video-verify/scripts/dvv preflight \
  --url http://localhost:8023
```

Recording surface: the board full screen, cutting to the Cloud console for
beat 2.

**Presenter keys** (board focused): `1`-`4` pick a store, `t` tamper a
till's swipe, `l` a till writes the card number into a receipt note, `x`
crash a till, `s` toggle the store's temperature sensor, `n` drop or
restore the store's network, `r` reset everything, `p` show the key bar.

**Timing.** Offsets are seconds after the jobs show running on the board.

---

## Beat 1: THE TRAP · 0:00-0:15

**Screen state before you speak.** Four stores, twelve tills ringing up.
Blue swipes leave every till and fade at a grey Expanso node. Warehouse:
0 records.

**Driver**: nothing to type.

> A retail and payments group wants its card data to make online search and
> recommendations smarter. But card data cannot leave the card business.
> Every one of these swipes carries a card number.

---

## Beat 2: THE THING, LIVE · 0:15-0:45

**Screen state before you speak.** Cut to the Cloud console; start
`pos-guard` and `pos-uplink`. Back on the board the four nodes turn green,
the Cloud box reads `pos-guard 4/4 · pos-uplink 4/4 running`, and green
records cross the DMZ.

**Driver**: start both jobs in the Expanso Cloud console.

> Expanso Cloud pushes two jobs to a node in every store. Look at one swipe.
> On the left, what the till sent: card number, cardholder, cashier. On the
> right, what left the store: a join ID, the basket, the store, the till,
> local time and the store's temperature. The card number became a one-way
> keyed hash, the same algorithm at every store, defined once centrally.
> Riverside is at twenty-nine degrees, and its window display is already
> selling cold drinks.

---

## Beat 3: THE BREAK, ON PURPOSE · 0:45-1:20

**Screen state before you speak.** Each key below, a few seconds apart.

**Driver**: `1` `t`, then `2` `l`, then `2` `n`, then `3` `x`, then `4` `s`.

> A till that was tampered with: the signature fails, and the record stays
> in the store with its reason. A till that typed a card number into a
> receipt note: blocked before the DMZ. Riverside loses its network: its
> tills keep ringing, its display keeps updating, and the records wait on
> the store's own disk. A till crashes, and its store's node flags it
> silent. The temperature sensor dies, and records keep flowing, marked
> sensor missing.

Then `2` `n` to restore Riverside: the queue drains as a burst.

---

## Beat 4: THE PAYOFF · 1:20-1:45

**Screen state before you speak.** Warehouse card, after Riverside drained.

> The warehouse scanned every record that landed. Card numbers found:
> **zero.** *(pause)* Yet it recognises the same shoppers across stores, and
> joins them to online profiles, by join ID alone.

Say the two shopper numbers as they read on screen.

---

## Beat 5: CLOSE · 1:45-2:00

**Screen state before you speak.** Warehouse still showing 0 card numbers.

> The card number never leaves the store. What leaves is safe to share, even
> across borders, and it still knows who bought what.

---

## Required lines

- "Card numbers found: zero."
- "The card number never leaves the store."

## Prohibited

Never spoken or shown: the prospect, its group companies or anyone from the
meeting; program names; third-party vendor names; "illustrative",
"simulated", "mock" or "sample". The group's efficiency goal is background
for the presenter, not a line in the take.

## After the take

```bash
~/.claude/skills/demo-video-verify/scripts/dvv verify <VIDEO> \
  --script RECORDING_SCRIPT.md
just down
```
