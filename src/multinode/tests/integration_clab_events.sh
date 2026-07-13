#!/usr/bin/env bash
set -euo pipefail

# Destructive integration smoke for Containerlab events + inspect resync.
# It creates only resources whose names start with dnlab-events-it.

command -v containerlab >/dev/null
command -v docker >/dev/null

version="$(containerlab version --short)"
case "$version" in
  0.7[7-9].*|0.[89][0-9].*|[1-9].*) ;;
  *) echo "containerlab >=0.77.0 required, found $version" >&2; exit 1 ;;
esac

if ! docker image inspect alpine:3 >/dev/null 2>&1; then
  echo "missing image alpine:3" >&2
  exit 1
fi

workdir="$(mktemp -d /tmp/dnlab-events-it.XXXXXX)"
topology="$workdir/dnlab-events-it.clab.yml"
events_log="$workdir/events.jsonl"
inspect_log="$workdir/inspect.json"
interfaces_log="$workdir/interfaces.json"
events_pid=""

cleanup() {
  if [ -n "$events_pid" ]; then
    kill "$events_pid" >/dev/null 2>&1 || true
    wait "$events_pid" >/dev/null 2>&1 || true
  fi
  containerlab destroy -t "$topology" --cleanup >/dev/null 2>&1 || true
  rm -rf "$workdir"
}
trap cleanup EXIT

cat >"$topology" <<'EOF'
name: dnlab-events-it
topology:
  nodes:
    n1:
      kind: linux
      image: alpine:3
      cmd: sleep infinity
    n2:
      kind: linux
      image: alpine:3
      cmd: sleep infinity
EOF

containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"
containerlab apply -t "$topology"

timeout 25s containerlab events -t "$topology" --format json --initial-state \
  >"$events_log" 2>"$workdir/events.err" &
events_pid="$!"

wait_for_events() {
  local pattern="$1"
  local timeout="${2:-20}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if grep -Eq "$pattern" "$events_log" 2>/dev/null; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  echo "events log did not match pattern: $pattern" >&2
  echo "--- events stdout ---" >&2
  cat "$events_log" >&2 || true
  echo "--- events stderr ---" >&2
  cat "$workdir/events.err" >&2 || true
  return 1
}

wait_for_events 'clab-dnlab-events-it-n1|clab-dnlab-events-it-n2|\"n1\"|\"n2\"'

containerlab inspect -t "$topology" --format json >"$inspect_log"
grep -Eq 'clab-dnlab-events-it-n1|\"n1\"' "$inspect_log"

containerlab inspect interfaces -t "$topology" --format json >"$interfaces_log"
grep -Eq 'clab-dnlab-events-it-n1|\"n1\"|eth0' "$interfaces_log"

cat >"$topology" <<'EOF'
name: dnlab-events-it
topology:
  nodes:
    n1:
      kind: linux
      image: alpine:3
      cmd: sleep infinity
    n2:
      kind: linux
      image: alpine:3
      cmd: sleep infinity
  links:
    - endpoints: ["n1:eth1", "n2:eth1"]
EOF

containerlab apply -t "$topology" --dry-run | grep -Eq "added links|changed links|apply"
containerlab apply -t "$topology"
containerlab inspect interfaces -t "$topology" --format json >"$interfaces_log"
grep -Eq 'eth1|n1' "$interfaces_log"

containerlab stop -t "$topology" --node n1 >/dev/null
wait_for_events 'stop|die|exited|destroy|disconnect|down|clab-dnlab-events-it-n1' 10

containerlab start -t "$topology" --node n1 >/dev/null
wait_for_events 'start|running|connect|up|clab-dnlab-events-it-n1' 10

echo "containerlab events integration smoke: PASS"
