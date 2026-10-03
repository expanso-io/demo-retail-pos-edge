#!/usr/bin/env bash
# Point of sale at the edge. Run it through just (see justfile): just --list
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

PORT="${PORT:-8640}"
WAREHOUSE_PORT=8641
STORES_PORT=8642
OUTBOX_BASE=8650     # store N's pos-guard -> pos-uplink, on the node
WAN_BASE=8660        # store N's WAN link to the warehouse
DISPLAY_BASE=8670    # store N's window display
API_BASE=8680        # store N's local edge API (local mode)
RUNTIME="$ROOT/.runtime"
CLOUD_STATE="$ROOT/.cloud-state"
ENV_FILE="$ROOT/.env"
JOBS=(pos-guard pos-uplink)
read -r -a STORES <<<"$(jq -r '[.stores[].store_id] | join(" ")' config/stores.json)"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

store_n() { jq -r --arg s "$1" '.stores | map(.store_id) | index($s) + 1' config/stores.json; }

# ------------------------------------------------------------------ .env

# Reads one name from the project-local .env; nothing outside this checkout.
env_get() {
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

check_env() {
  [[ -f "$ENV_FILE" ]] || die "no .env. Copy env.example to .env (README: Setup)"
  local perm
  perm="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
  [[ "$perm" == 600 ]] || die ".env must be owner-only. Run: chmod 600 .env"
  # The group's join key and the register enrollment seed are generated once
  # and kept in .env; every store and the central side read the same values.
  local name
  for name in JOIN_ID_KEY REGISTER_KEY_SEED; do
    if [[ -z "$(env_get "$name")" ]]; then
      printf '%s=%s\n' "$name" "$(openssl rand -hex 32)" >>"$ENV_FILE"
      say "generated $name in .env"
    fi
  done
}

check_cloud_env() {
  local name
  for name in EXPANSO_CLI_ENDPOINT EXPANSO_CLI_API_KEY \
    EXPANSO_EDGE_BOOTSTRAP_TOKEN; do
    [[ -n "$(env_get "$name")" ]] || die "$name missing from .env (README: Setup)"
  done
}

# -------------------------------------------------------------- processes

pid_alive() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

stop_pidfile() {
  local file="$1" pid
  [[ -f "$file" ]] || return 0
  pid="$(cat "$file")"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$file"
}

wait_http() {
  local url="$1" what="$2" log="$3"
  for _ in $(seq 1 150); do
    curl -fsS --max-time 1 "$url" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  tail -n 20 "$log" >&2
  die "$what did not start"
}

warehouse_up() {
  pid_alive "$RUNTIME/warehouse.pid" && return 0
  port_busy "$WAREHOUSE_PORT" && die "port $WAREHOUSE_PORT is in use"
  nohup uv run --quiet -s scripts/warehouse.py --port "$WAREHOUSE_PORT" \
    >|"$RUNTIME/warehouse.log" 2>&1 &
  echo $! >|"$RUNTIME/warehouse.pid"
  wait_http "http://127.0.0.1:$WAREHOUSE_PORT/stats" warehouse "$RUNTIME/warehouse.log"
  say "warehouse: 127.0.0.1:$WAREHOUSE_PORT"
}

stores_up() {
  pid_alive "$RUNTIME/stores.pid" && return 0
  port_busy "$STORES_PORT" && die "port $STORES_PORT is in use"
  REGISTER_KEY_SEED="$(env_get REGISTER_KEY_SEED)" JOIN_ID_KEY="$(env_get JOIN_ID_KEY)" \
    nohup uv run --quiet -s scripts/stores.py serve --control-port "$STORES_PORT" \
    >|"$RUNTIME/stores.log" 2>&1 &
  echo $! >|"$RUNTIME/stores.pid"
  wait_http "http://127.0.0.1:$STORES_PORT/state" stores "$RUNTIME/stores.log"
  say "stores: $(head -1 "$RUNTIME/stores.log")"
}

board_up() {
  pid_alive "$RUNTIME/board.pid" && return 0
  port_busy "$PORT" && die "port $PORT is in use"
  nohup uv run --quiet -s scripts/dashboard.py --port "$PORT" \
    >|"$RUNTIME/board.log" 2>&1 &
  echo $! >|"$RUNTIME/board.pid"
  wait_http "http://127.0.0.1:$PORT/api/state" board "$RUNTIME/board.log"
  say "board: http://localhost:$PORT"
}

node_dir() { echo "$RUNTIME/edge/$1"; }

write_node_config() {
  local store="$1" n dir
  n="$(store_n "$store")"
  dir="$(node_dir "$store")"
  mkdir -p "$dir"
  {
    echo "name: pos-$store-edge"
    echo "api:"
    echo "  listen_addr: 127.0.0.1:$((API_BASE + n))"
    echo "log:"
    echo "  level: info"
    echo "  format: console"
    echo "labels:"
    echo "  demo: retail-pos-edge"
    echo "  role: pos-store"
    echo "  store: \"$store\""
  } >|"$dir/node.yaml"
}

edge_up() {
  local store="$1" mode="$2" n dir data
  n="$(store_n "$store")"
  dir="$(node_dir "$store")"
  pid_alive "$dir/edge.pid" && return 0
  write_node_config "$store"
  local args=(run -c "$dir/node.yaml" --no-watch)
  if [[ "$mode" == local ]]; then
    data="$dir/local"
    rm -rf "$data"
    args+=(--local --data-dir "$data")
  else
    # The node's Cloud identity lives in this checkout, owner-only.
    data="$CLOUD_STATE/$store"
    mkdir -p "$data"
    chmod 700 "$CLOUD_STATE" "$data"
    if [[ ! -f "$data/auth/credentials.creds" ]]; then
      EXPANSO_EDGE_BOOTSTRAP_TOKEN="$(env_get EXPANSO_EDGE_BOOTSTRAP_TOKEN)" \
        expanso-edge bootstrap -c "$dir/node.yaml" --data-dir "$data" \
        >|"$dir/bootstrap.log" 2>&1 || {
          tail -n 8 "$dir/bootstrap.log" >&2
          die "store $store: bootstrap failed (token expired or revoked?)"
        }
    fi
    args+=(--data-dir "$data")
  fi
  # Everything the jobs read from their node. Secrets stay in this process's
  # environment; the job files only name them.
  STORE_ID="$store" \
  STORE_PROFILE="$(uv run --quiet -s scripts/stores.py profile "$store")" \
  REGISTER_KEYS="$(REGISTER_KEY_SEED="$(env_get REGISTER_KEY_SEED)" \
    uv run --quiet -s scripts/stores.py keys "$store")" \
  JOIN_ID_KEY="$(env_get JOIN_ID_KEY)" \
  STORE_SOURCE_URL="http://127.0.0.1:$STORES_PORT" \
  OUTBOX_ADDR="127.0.0.1:$((OUTBOX_BASE + n))" \
  OUTBOX_URL="http://127.0.0.1:$((OUTBOX_BASE + n))/outbox" \
  DISPLAY_URL="http://127.0.0.1:$((DISPLAY_BASE + n))" \
  WAREHOUSE_URL="http://127.0.0.1:$((WAN_BASE + n))/ingest" \
  QUARANTINE_FILE="$dir/quarantine.jsonl" \
  UPLINK_QUEUE_DB="$dir/uplink-queue.db" \
    nohup expanso-edge "${args[@]}" >|"$dir/edge.log" 2>&1 &
  echo $! >|"$dir/edge.pid"
  echo "$mode" >|"$RUNTIME/mode"
}

edges_down() {
  local store
  for store in "${STORES[@]}"; do stop_pidfile "$(node_dir "$store")/edge.pid"; done
}

local_cli() {
  local store="$1" n
  shift
  n="$(store_n "$store")"
  expanso-cli --endpoint "http://127.0.0.1:$((API_BASE + n))" "$@"
}

deploy_local() {
  local store="$1" job out
  for _ in $(seq 1 100); do
    local_cli "$store" node list >/dev/null 2>&1 && break
    pid_alive "$(node_dir "$store")/edge.pid" || {
      tail -n 20 "$(node_dir "$store")/edge.log" >&2
      die "store $store's edge node exited"
    }
    sleep 0.2
  done
  for job in "${JOBS[@]}"; do
    if out="$(local_cli "$store" job deploy "pipelines/$job.yaml" 2>&1)"; then
      say "  $store: $job deployed"
    elif grep -q NO_CHANGES_DETECTED <<<"$out"; then
      local_cli "$store" job rerun "$job" >/dev/null
      say "  $store: $job unchanged; restarted"
    else
      printf '%s\n' "$out" | sed -n '1,12p' >&2
      die "could not deploy $job to store $store"
    fi
  done
}

wipe_store_state() {
  local store dir
  rm -f "$RUNTIME/store-events.db" "$RUNTIME/store-events.db-wal" \
    "$RUNTIME/store-events.db-shm"
  for store in "${STORES[@]}"; do
    dir="$(node_dir "$store")"
    rm -f "$dir/quarantine.jsonl" "$dir/uplink-queue.db"*
  done
}

# ------------------------------------------------------------------- cloud

# The key is always passed explicitly, so no CLI profile elsewhere on this
# machine is ever read or used.
cloud_cli() {
  expanso-cli --endpoint "$(env_get EXPANSO_CLI_ENDPOINT)" \
    --api-key "$(env_get EXPANSO_CLI_API_KEY)" "$@"
}

cloud_node_states() {
  cloud_cli node list -f json -L role=pos-store 2>/dev/null | jq -r '
    group_by(.spec.name)[] |
    (.[0].spec.name) as $n |
    ([.[].status.connection_state] | if index("connected") then "connected"
      else (first // "none") end) as $s |
    "\($n):\($s)"' 2>/dev/null || true
}

# Jobs in the workspace with no node selector would land on the store nodes.
# Fails closed: if the workspace cannot be listed, up stops.
foreign_jobs() {
  local jobs
  jobs="$(cloud_cli job list -f json -l 200 2>"$RUNTIME/joblist.err")" \
    || die "could not list jobs in Expanso Cloud: $(head -c 200 "$RUNTIME/joblist.err")"
  jq -r '
    .[] | select(.spec.name != "pos-guard" and .spec.name != "pos-uplink")
    | select((.spec.selector.match_labels // {}) == {}
             and ((.spec.selector.match_expressions // []) | length) == 0)
    | select(.status.state.state_type as $s
             | ["stopped", "completed", "failed"] | index($s) | not)
    | .spec.name' <<<"$jobs"
}

deploy_cloud() {
  local job out
  for job in "${JOBS[@]}"; do
    if out="$(cloud_cli job deploy "pipelines/$job.yaml" 2>&1)"; then
      say "  $job deployed to Expanso Cloud (selector role=pos-store)"
    elif grep -q NO_CHANGES_DETECTED <<<"$out"; then
      say "  $job unchanged in Expanso Cloud"
    else
      printf '%s\n' "$out" | sed -n '1,12p' >&2
      die "could not deploy $job to Expanso Cloud"
    fi
  done
}

# Stops both jobs in Cloud. strict=0 (down) only warns if Cloud is unreachable.
jobs_stop() {
  local strict="${1:-1}" job out
  for job in "${JOBS[@]}"; do
    if out="$(cloud_cli job stop "$job" --force 2>&1)"; then
      say "  $job stopped in Expanso Cloud"
    elif grep -qiE "already|not running|stopped|not found" <<<"$out"; then
      say "  $job not running in Expanso Cloud"
    elif [[ "$strict" == 1 ]]; then
      printf '%s\n' "$out" | sed -n '1,10p' >&2
      die "could not stop $job in Expanso Cloud"
    else
      say "  WARN: could not stop $job in Expanso Cloud"
    fi
  done
}

jobs_start() {
  local job
  for job in "${JOBS[@]}"; do
    cloud_cli job rerun "$job" >/dev/null
    say "  $job started in Expanso Cloud"
  done
}

wait_nodes_connected() {
  local want="${#STORES[@]}" states connected=0
  for _ in $(seq 1 90); do
    states="$(cloud_node_states)"
    connected="$(grep -c ':connected$' <<<"$states" || true)"
    [[ "$connected" -ge "$want" ]] && break
    sleep 2
  done
  [[ "$connected" -ge "$want" ]] || die "only $connected of $want store nodes connected:
$states"
  say "  $want store nodes connected"
}

# A node restarts the jobs it last ran from its own data dir and keeps them
# running until its next reconcile with Cloud (every 30 s). Wait until every
# store's executions stop, including the guard's outbound source polling.
wait_ingest_closed() {
  local store n node nodes running open=1
  local -a filters=()
  nodes="$(cloud_cli node list -f json -L role=pos-store)" \
    || die "could not verify store nodes"
  while IFS= read -r node; do
    [[ -n "$node" ]] && filters+=(--node-id "$node")
  done < <(jq -r '.[].id' <<<"$nodes")
  [[ "${#filters[@]}" -gt 0 ]] || die "no store node IDs to verify"
  say "  waiting for any job a node cached from its last run to stop..."
  sleep 35
  for _ in $(seq 1 60); do
    open=0
    for store in "${STORES[@]}"; do
      n="$(store_n "$store")"
      port_busy "$((OUTBOX_BASE + n))" && open=1
    done
    if running="$(cloud_cli execution list --state running --limit 1000 \
      "${filters[@]}" -f json)"; then
      if [[ "$running" != "No executions found" ]]; then
        jq -e 'type == "array" and length == 0' <<<"$running" >/dev/null || open=1
      fi
    else
      open=1
    fi
    [[ "$open" == 0 ]] && return 0
    sleep 2
  done
  die "a store node is still running a job after Cloud stopped it"
}

# ---------------------------------------------------------------- commands

# The start command. Starts from nothing: warehouse empty, both jobs deployed
# to Expanso Cloud but stopped, a node per store connected, registers ringing
# into their store databases, the board up. The presenter starts pos-guard and
# pos-uplink in the Expanso Cloud console; the board shows it from Cloud.
cmd_up() {
  local store foreign
  check_env
  check_cloud_env
  cmd_down >/dev/null
  mkdir -p "$RUNTIME"
  foreign="$(foreign_jobs)"
  [[ -z "$foreign" ]] || die "active jobs with no node selector in the \
workspace would land on the store nodes: $(tr '\n' ' ' <<<"$foreign")
  Ask for a dedicated workspace, or run: just up-local"
  deploy_cloud
  jobs_stop
  say "starting ${#STORES[@]} Expanso Edge nodes connected to Expanso Cloud..."
  for store in "${STORES[@]}"; do edge_up "$store" cloud; done
  wait_nodes_connected
  wait_ingest_closed
  wipe_store_state
  rm -f "$RUNTIME/warehouse.db"
  warehouse_up
  stores_up
  board_up
  say ""
  say "ready: pos-guard and pos-uplink are stopped, so the registers ring up"
  say "into store databases. Start both in the Expanso Cloud console."
}

# The same demo with a local control plane per node and no Cloud account.
cmd_up_local() {
  local store
  check_env
  cmd_down >/dev/null
  mkdir -p "$RUNTIME"
  wipe_store_state
  rm -f "$RUNTIME/warehouse.db"
  warehouse_up
  stores_up
  say "starting ${#STORES[@]} Expanso Edge nodes (local control plane)..."
  for store in "${STORES[@]}"; do edge_up "$store" local; done
  for store in "${STORES[@]}"; do deploy_local "$store"; done
  board_up
}

cmd_down() {
  stop_pidfile "$RUNTIME/board.pid"
  stop_pidfile "$RUNTIME/stores.pid"
  edges_down
  stop_pidfile "$RUNTIME/warehouse.pid"
  say "stopped board, stores, edge nodes and warehouse"
  if [[ "$(cat "$RUNTIME/mode" 2>/dev/null)" == cloud && -f "$ENV_FILE" \
        && -n "$(env_get EXPANSO_CLI_API_KEY)" ]]; then
    jobs_stop 0
  fi
  rm -f "$RUNTIME/mode"
}

cmd_status() {
  local store
  for name in warehouse stores board; do
    pid_alive "$RUNTIME/$name.pid" && say "$name: up" || say "$name: down"
  done
  for store in "${STORES[@]}"; do
    if pid_alive "$(node_dir "$store")/edge.pid"; then
      say "$store edge: up ($(cat "$RUNTIME/mode" 2>/dev/null))"
    else
      say "$store edge: down"
    fi
  done
}

control() {
  curl -fsS -X POST -H 'Content-Type: application/json' -d "$2" \
    "http://127.0.0.1:$STORES_PORT/$1"
  echo
}

usage() {
  cat <<'EOF'
usage: ./demo.sh <command>

  up            clean start on Expanso Cloud: jobs deployed but stopped, a
                node per store, registers, warehouse and board running.
                Start pos-guard and pos-uplink in the Expanso Cloud console.
  up-local      the same with a local control plane per node; jobs run at once
  start-jobs    start both jobs in Expanso Cloud from the terminal
  down          stop everything and stop the jobs in Cloud
  status        what is running
  cut S | restore S          drop or restore store S's network (s1..s4)
  sensor-off S | sensor-on S the store's temperature sensor
  fault R KIND  KIND = tamper | leak | malformed | inject | crash (R = s1-r2)
  register R open|closed     switch a register on or off
EOF
}

main() {
  local cmd="${1:-help}"
  [[ $# -gt 0 ]] && shift
  mkdir -p "$RUNTIME"
  chmod 700 "$RUNTIME"
  case "$cmd" in
    up) cmd_up ;;
    up-local) cmd_up_local ;;
    start-jobs) check_cloud_env; jobs_start ;;
    down) cmd_down ;;
    status) cmd_status ;;
    cut) control link "{\"store_id\": \"${1:?store}\", \"cut\": true}" ;;
    restore) control link "{\"store_id\": \"${1:?store}\", \"cut\": false}" ;;
    sensor-off) control sensor "{\"store_id\": \"${1:?store}\", \"on\": false}" ;;
    sensor-on) control sensor "{\"store_id\": \"${1:?store}\", \"on\": true}" ;;
    fault) control fault "{\"register_id\": \"${1:?register}\", \"kind\": \"${2:?kind}\"}" ;;
    register) control register "{\"register_id\": \"${1:?register}\", \"state\": \"${2:?state}\"}" ;;
    help|-h|--help) usage ;;
    *) usage >&2; exit 2 ;;
  esac
}

main "$@"
