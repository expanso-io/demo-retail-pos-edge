set shell := ["bash", "-euo", "pipefail", "-c"]

port := "8023"

_default:
    @just --list

# clean start on Expanso Cloud: jobs deployed but stopped, a node per store,
# registers, warehouse and board running. Start the jobs in the Cloud console.
up:
    @bash demo.sh up

# the same with a local control plane per node; the jobs run at once
up-local:
    @bash demo.sh up-local

# start pos-guard and pos-uplink in Expanso Cloud from the terminal
start-jobs:
    @bash demo.sh start-jobs

# stop everything and stop the jobs in Cloud
down:
    @bash demo.sh down

status:
    @bash demo.sh status

# drop or restore a store's network (s1..s4)
cut store="s3":
    @bash demo.sh cut {{store}}

restore store="s3":
    @bash demo.sh restore {{store}}

# a store's temperature sensor
sensor-off store="s4":
    @bash demo.sh sensor-off {{store}}

sensor-on store="s4":
    @bash demo.sh sensor-on {{store}}

# fault a till: tamper | leak | malformed | inject | crash
fault till="s1-r1" kind="tamper":
    @bash demo.sh fault {{till}} {{kind}}

test:
    uv run --quiet -s scripts/dashboard.py --check
    bash -n demo.sh
    uv run --quiet -s tests/test_stores.py

# Expanso pipelines: syntax, then the all-demos rules (logs, a real output,
# short lines, no blank lines in config, generate only for timers).
validate:
    @command -v expanso-edge > /dev/null || { echo "skip: expanso-edge not installed"; exit 0; }
    expanso-edge validate pipelines/*.yaml

pipeline-check:
    @uv run -s ../_demo-kit/lint-demo-pipelines.py .

video-check:
    @uv run -s ../_demo-kit/lint-demo-ui.py . --video-strict

# JavaScript anti-slop gate (oxlint rules shared across projects)
js-check:
    @[ -x ../../anti-slop/check ] || { echo "skip: anti-slop not present"; exit 0; }
    ../../anti-slop/check dashboard/app.js

check: test validate pipeline-check video-check js-check clean-check

# everything that must be true before a take: gates + live endpoint + checklist
record-check: check
    curl -fsS "http://localhost:{{port}}/api/state" > /dev/null || { echo "FAIL: dashboard not reachable, just up first"; exit 1; }
    @echo ""
    @echo "RECORD CHECKLIST"
    @echo "  [ ] demo-guidance/RECORDING.md read; Cloud console set to light (matches this board)"
    @echo "  [ ] browser in app mode, no browser chrome in frame"
    @echo "  [ ] fresh just up (true zero state): jobs stopped, warehouse empty"
    @echo "  [ ] dvv preflight --url http://localhost:{{port}} passed"

# human story/proof declaration; validates only and never starts anything
recording-preflight:
    @uv run -s ../_demo-kit/recording-preflight.py .

# prohibited names never reach a take
clean-check:
    @if rg -il '[b]acalhau|[i]llustrative|[s]imulated|[m]ock data' --glob '!justfile' --glob '!.runtime' --glob '!.cloud-state' . ; then echo "FAIL: prohibited word in tree"; exit 1; fi
    @echo "clean"
