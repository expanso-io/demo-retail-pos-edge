# demo-retail-pos-edge — agent rules

The rules in `../AGENTS.md` govern this directory; this file only adds to
them. Build order: claim → beat script → components on the showcase →
port the winners here → `just check`.

- The claim: **Card data never leaves the store as a card number: each swipe becomes a one-way join ID with its context, checked before it touches the core network** — every element on screen serves it or gets cut.
- Palette: stamped **light** (declared as `color-scheme` in
  `dashboard/styles.css`). Neither dark nor light is the default — palette
  follows the subject and the host it lives in (`../AGENTS.md` → Theme). If
  this demo should be the other one, re-stamp with `--palette` rather than
  hand-flipping colours; the linter checks the ground agrees with the
  declaration.
- Design language: `../_demo-kit/DESIGN_SYSTEM.md`. Re-hue the tokens for this
  subject; keep the token names.
- Every quantitative claim cites `docs/RESEARCH.md` or is introduced as
  representative.
