# Point of sale at the edge

**Card data never leaves the store as a card number: each swipe becomes a one-way join ID with its context, checked before it touches the core network**

Scaffolded by `_demo-kit/new-demo.py` on 2026-10-01. Before building anything,
pick the use-case archetype in `../_demo-kit/PATTERNS.md` and read
`../_demo-kit/DESIGN_SYSTEM.md` — new components are built on the showcase
first, never here.

```bash
just up        # dashboard on :8023
just check     # tests + video-strict lint
just record-check   # everything that must be true before a take
```

Cloud workspace: `just workspace-init` installs the shared `expanso-demos`
profile and enrolls this demo's project-local edge identity. Use
`just workspace-check` for a read-only verification.
