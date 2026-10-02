set shell := ["bash", "-euo", "pipefail", "-c"]

port := "8023"

_default:
    @just --list

# start the dashboard (PID in .runtime/)
up:
    mkdir -p .runtime
    nohup uv run -s scripts/dashboard.py --port {{port}} > .runtime/dashboard.log 2>&1 & echo $! > .runtime/dashboard.pid
    @echo "dashboard on http://localhost:{{port}}"

down:
    -[ -f .runtime/dashboard.pid ] && kill "$(cat .runtime/dashboard.pid)" 2>/dev/null && rm .runtime/dashboard.pid
    @echo "down"

status:
    @[ -f .runtime/dashboard.pid ] && ps -p "$(cat .runtime/dashboard.pid)" > /dev/null && echo "up (pid $(cat .runtime/dashboard.pid))" || echo "down"

# Shared Expanso Cloud workspace. Separate workspaces require explicit flags
# on expanso-demo-init.py so the override cannot be mistaken for the default.
workspace-init:
    @uv run -s ../_demo-kit/expanso-demo-init.py .

workspace-check:
    @uv run -s ../_demo-kit/expanso-demo-init.py . --check

test:
    uv run -s scripts/dashboard.py --check

video-check:
    @uv run -s ../_demo-kit/lint-demo-ui.py . --video-strict

# Expanso pipelines: syntax, then the all-demos rules (logs, a real output,
# short lines, no blank lines in config, generate only for timers).
validate:
    @command -v expanso-edge > /dev/null || { echo "skip: expanso-edge not installed"; exit 0; }
    expanso-edge validate pipelines/*.yaml

pipeline-check:
    @uv run -s ../_demo-kit/lint-demo-pipelines.py .

# Drive the pipeline's input from outside: the demo's data source.
produce rate="2":
    uv run -s scripts/producer.py --rate {{rate}}

check: test validate pipeline-check video-check

# everything that must be true before a take: gates + live endpoint + checklist
record-check: check
    curl -fsS "http://localhost:{{port}}/api/state" > /dev/null || { echo "FAIL: dashboard not reachable — just up first"; exit 1; }
    @echo ""
    @echo "RECORD CHECKLIST"
    @echo "  [ ] demo-guidance/RECORDING.md read; console set to light (matches this board); resolution dropped"
    @echo "  [ ] Chrome --app, no browser chrome in frame"
    @echo "  [ ] true zero state confirmed (no finished session on screen)"
    @echo "  [ ] dvv preflight --url http://localhost:{{port}} passed"

# human story/proof declaration; validates only and never starts anything
recording-preflight:
    @uv run -s ../_demo-kit/recording-preflight.py .

# prohibited names never reach a take (add customer/competitor names here)
clean-check:
    @if rg -il '[b]acalhau' --glob '!justfile' . ; then echo "FAIL: prohibited name in tree"; exit 1; fi
    @echo "clean"
