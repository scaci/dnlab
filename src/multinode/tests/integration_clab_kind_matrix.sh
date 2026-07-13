#!/usr/bin/env bash
set -euo pipefail

# Destructive qualification runner for Containerlab apply/lifecycle behavior.
#
# Default behavior runs only the lightweight linux/live case. Optional NOS cases
# can be supplied through DNLAB_KIND_MATRIX_CASES as semicolon-separated records:
#
#   name|kind|image|expected_mode|boot_wait_seconds|cmd
#
# Example:
#
#   DNLAB_KIND_MATRIX_RUN_HEAVY=1 \
#   DNLAB_KIND_MATRIX_CASES='ceos|arista_ceos|ceos:4.34.0F|restart|240|;n9kv|cisco_n9kv|vrnetlab/cisco_n9kv:10.4.3|recreate|600|' \
#   src/multinode/tests/integration_clab_kind_matrix.sh
#
# To validate the known local vrnetlab images without booting NOS containers:
#
#   DNLAB_KIND_MATRIX_AUTO_VRNETLAB=1 \
#   DNLAB_KIND_MATRIX_DRY_RUN_ONLY=1 \
#   src/multinode/tests/integration_clab_kind_matrix.sh
#
# For heavy NOS batches that should not repeat the lightweight linux/live case:
#
#   DNLAB_KIND_MATRIX_SKIP_LINUX=1 \
#   DNLAB_KIND_MATRIX_RUN_HEAVY=1 \
#   DNLAB_KIND_MATRIX_CASES='...' \
#   src/multinode/tests/integration_clab_kind_matrix.sh
#
# The runner creates only Containerlab resources whose names start with
# dnlab-kind-matrix- and removes them on exit.

command -v containerlab >/dev/null
command -v docker >/dev/null

version="$(containerlab version --short)"
case "$version" in
  0.7[7-9].*|0.[89][0-9].*|[1-9].*) ;;
  *) echo "containerlab >=0.77.0 required, found $version" >&2; exit 1 ;;
esac

workdir="$(mktemp -d /tmp/dnlab-kind-matrix.XXXXXX)"
failures=0
passes=0
skips=0

cleanup() {
  find "$workdir" -name '*.clab.yml' -print0 2>/dev/null \
    | while IFS= read -r -d '' topology; do
        containerlab destroy -t "$topology" --cleanup >/dev/null 2>&1 || true
      done
  rm -rf "$workdir"
}
trap cleanup EXIT

record_pass() {
  passes=$((passes + 1))
  echo "PASS $1"
}

record_skip() {
  skips=$((skips + 1))
  echo "SKIP $1: $2"
}

record_fail() {
  failures=$((failures + 1))
  echo "FAIL $1: $2" >&2
}

image_available() {
  docker image inspect "$1" >/dev/null 2>&1
}

wait_container_running() {
  local container="$1"
  local timeout="${2:-30}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if [ "$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)" = "running" ]; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

check_persist_marker() {
  local case_name="$1"
  local persist="$2"
  local phase="$3"
  if [ "$(cat "$persist/marker" 2>/dev/null || true)" != "matrix" ]; then
    record_fail "$case_name" "persistence marker lost after $phase"
    return 1
  fi
  return 0
}

run_linux_live_case() {
  local case_name="linux-live"
  local topology="$workdir/dnlab-kind-matrix-linux.clab.yml"

  if ! image_available "alpine:3"; then
    record_skip "$case_name" "missing image alpine:3"
    return
  fi

  cat >"$topology" <<'EOF'
name: dnlab-kind-matrix-linux
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

  if ! containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"; then
    record_fail "$case_name" "initial dry-run did not report deploy lab"
    return
  fi
  if ! containerlab apply -t "$topology"; then
    record_fail "$case_name" "initial apply failed"
    return
  fi

  cat >"$topology" <<'EOF'
name: dnlab-kind-matrix-linux
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

  if ! containerlab apply -t "$topology" --dry-run | grep -Eq "added links|changed links|apply"; then
    record_fail "$case_name" "link-add dry-run did not report a link change"
    return
  fi
  if ! containerlab apply -t "$topology"; then
    record_fail "$case_name" "link-add apply failed"
    return
  fi

  docker exec clab-dnlab-kind-matrix-linux-n1 ip addr add 192.0.2.1/30 dev eth1
  docker exec clab-dnlab-kind-matrix-linux-n2 ip addr add 192.0.2.2/30 dev eth1
  docker exec clab-dnlab-kind-matrix-linux-n1 ip link set eth1 up
  docker exec clab-dnlab-kind-matrix-linux-n2 ip link set eth1 up

  if ! docker exec clab-dnlab-kind-matrix-linux-n1 ping -c 3 -W 1 192.0.2.2 >/dev/null; then
    record_fail "$case_name" "traffic failed after live link add"
    return
  fi

  containerlab stop -t "$topology" --node n1 >/dev/null
  if [ "$(docker inspect -f '{{.State.Status}}' clab-dnlab-kind-matrix-linux-n1)" != "exited" ]; then
    record_fail "$case_name" "node stop did not leave n1 exited"
    return
  fi
  containerlab start -t "$topology" --node n1 >/dev/null
  if ! wait_container_running clab-dnlab-kind-matrix-linux-n1 30; then
    record_fail "$case_name" "node start did not return n1 to running"
    return
  fi

  cat >"$topology" <<'EOF'
name: dnlab-kind-matrix-linux
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

  if ! containerlab apply -t "$topology" --dry-run | grep -Eq "removed links|deleted endpoint|changed links|apply"; then
    record_fail "$case_name" "link-remove dry-run did not report a link change"
    return
  fi
  if ! containerlab apply -t "$topology"; then
    record_fail "$case_name" "link-remove apply failed"
    return
  fi

  record_pass "$case_name"
}

write_single_node_topology() {
  local topology="$1"
  local lab="$2"
  local kind="$3"
  local image="$4"
  local persist="$5"
  local revision="$6"
  local cmd="${7:-}"

  {
    echo "name: $lab"
    echo "topology:"
    echo "  nodes:"
    echo "    n1:"
    echo "      kind: $kind"
    echo "      image: $image"
    if [ -n "$cmd" ]; then
      echo "      cmd: $cmd"
    fi
    echo "      binds:"
    echo "        - $persist:/persist"
    echo "      env:"
    echo "        DNLAB_MATRIX_REVISION: \"$revision\""
  } >"$topology"
}

run_optional_case() {
  local name="$1"
  local kind="$2"
  local image="$3"
  local expected="$4"
  local boot_wait="$5"
  local cmd="${6:-}"
  local lab="dnlab-kind-matrix-$name"
  local topology="$workdir/$lab.clab.yml"
  local persist="$workdir/$lab-persist"

  if ! image_available "$image"; then
    record_skip "$name" "missing image $image"
    return
  fi

  mkdir -p "$persist"
  write_single_node_topology "$topology" "$lab" "$kind" "$image" "$persist" "one" "$cmd"

  if [ "${DNLAB_KIND_MATRIX_DRY_RUN_ONLY:-0}" = "1" ]; then
    if containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"; then
      record_pass "$name/$kind/dry-run"
    else
      record_fail "$name" "dry-run failed for kind=$kind image=$image"
    fi
    return
  fi

  if [ "${DNLAB_KIND_MATRIX_RUN_HEAVY:-0}" != "1" ]; then
    record_skip "$name" "image present but DNLAB_KIND_MATRIX_RUN_HEAVY=1 not set"
    return
  fi

  if ! containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"; then
    record_fail "$name" "initial dry-run did not report deploy lab"
    return
  fi
  if ! containerlab apply -t "$topology"; then
    record_fail "$name" "initial apply failed"
    return
  fi
  if ! wait_container_running "clab-$lab-n1" "$boot_wait"; then
    record_fail "$name" "container did not reach running within ${boot_wait}s"
    return
  fi

  persist_marker_available=0
  if docker exec "clab-$lab-n1" sh -c 'printf matrix >/persist/marker' >/dev/null 2>&1; then
    persist_marker_available=1
    write_single_node_topology "$topology" "$lab" "$kind" "$image" "$persist" "two" "$cmd"
    dry_run="$(containerlab apply -t "$topology" --dry-run || true)"
    if [ "$expected" = "recreate" ] && ! grep -q "recreated nodes" <<<"$dry_run"; then
      record_fail "$name" "expected recreate, dry-run was: $dry_run"
      return
    fi
    if ! containerlab apply -t "$topology"; then
      record_fail "$name" "revision apply failed"
      return
    fi
    if ! check_persist_marker "$name" "$persist" "revision apply"; then
      return
    fi
  else
    echo "WARN $name: could not write /persist marker from inside guest; continuing lifecycle only"
  fi

  if ! containerlab stop -t "$topology" --node n1 >/dev/null; then
    record_fail "$name" "node stop failed"
    return
  fi
  if ! containerlab start -t "$topology" --node n1 >/dev/null; then
    record_fail "$name" "node start failed"
    return
  fi
  if ! wait_container_running "clab-$lab-n1" "$boot_wait"; then
    record_fail "$name" "container did not return to running after start"
    return
  fi
  if [ "$persist_marker_available" = "1" ] && ! check_persist_marker "$name" "$persist" "stop/start"; then
    return
  fi
  if ! containerlab restart -t "$topology" --node n1 >/dev/null; then
    record_fail "$name" "node restart failed"
    return
  fi
  if ! wait_container_running "clab-$lab-n1" "$boot_wait"; then
    record_fail "$name" "container did not return to running after restart"
    return
  fi
  if [ "$persist_marker_available" = "1" ] && ! check_persist_marker "$name" "$persist" "restart"; then
    return
  fi

  record_pass "$name/$kind/$expected"
}

local_image() {
  local repository="$1"
  docker images --format '{{.Repository}}:{{.Tag}}' \
    | grep "^${repository}:" \
    | sort -Vr \
    | head -n1
}

append_case() {
  local row="$1"
  if [ -n "${DNLAB_KIND_MATRIX_CASES:-}" ]; then
    DNLAB_KIND_MATRIX_CASES="${DNLAB_KIND_MATRIX_CASES};${row}"
  else
    DNLAB_KIND_MATRIX_CASES="$row"
  fi
  export DNLAB_KIND_MATRIX_CASES
}

append_case_if_image() {
  local name="$1"
  local kind="$2"
  local repository="$3"
  local expected="$4"
  local boot_wait="$5"
  local image
  image="$(local_image "$repository" || true)"
  if [ -n "$image" ]; then
    append_case "$name|$kind|$image|$expected|$boot_wait|"
  fi
}

auto_append_vrnetlab_cases() {
  if [ "${DNLAB_KIND_MATRIX_AUTO_VRNETLAB:-0}" != "1" ]; then
    return
  fi

  append_case_if_image "dnlab-frr" "linux" "vrnetlab/dnlab_frr" "live" "120"
  append_case_if_image "c9800cl" "cisco_cat9kv" "vrnetlab/cisco_c9800cl_v2" "recreate" "900"
  append_case_if_image "cat9kv" "cisco_cat9kv" "vrnetlab/cisco_cat9kv_v2" "recreate" "900"
  append_case_if_image "n9kv" "cisco_n9kv" "vrnetlab/cisco_n9kv_v2" "recreate" "900"
  append_case_if_image "vios-l2" "cisco_vios" "vrnetlab/cisco_vios_l2_v2" "recreate" "600"
  append_case_if_image "vios" "cisco_vios" "vrnetlab/cisco_vios_v2" "recreate" "600"
  append_case_if_image "xrv9k" "cisco_xrv9k" "vrnetlab/cisco_xrv9k_v2" "recreate" "900"
  append_case_if_image "vjunos-router" "juniper_vjunosrouter" "vrnetlab/juniper_vjunos-router_v2" "recreate" "900"
  append_case_if_image "vjunos-switch" "juniper_vjunosswitch" "vrnetlab/juniper_vjunos-switch_v2" "recreate" "900"
  append_case_if_image "vjunos-evolved" "juniper_vjunosevolved" "vrnetlab/juniper_vjunosevolved_v2" "recreate" "900"
  append_case_if_image "routeros" "mikrotik_ros" "vrnetlab/mikrotik_routeros" "recreate" "300"
  # dNLab catalog maps GUI kind nvidia_cumulusvx to deploy_kind generic_vm.
  append_case_if_image "cumulusvx" "generic_vm" "vrnetlab/nvidia_cumulusvx" "recreate" "600"
  append_case_if_image "openwrt" "openwrt" "vrnetlab/openwrt_openwrt_v2" "recreate" "300"
}

run_optional_cases() {
  auto_append_vrnetlab_cases

  local matrix="${DNLAB_KIND_MATRIX_CASES:-}"
  if [ -z "$matrix" ]; then
    return
  fi

  local IFS=';'
  local rows=($matrix)
  local row
  for row in "${rows[@]}"; do
    [ -n "$row" ] || continue
    local name kind image expected boot_wait cmd
    IFS='|' read -r name kind image expected boot_wait cmd <<<"$row"
    if [ -z "${name:-}" ] || [ -z "${kind:-}" ] || [ -z "${image:-}" ] || [ -z "${expected:-}" ]; then
      record_fail "matrix-record" "invalid record: $row"
      continue
    fi
    run_optional_case "$name" "$kind" "$image" "$expected" "${boot_wait:-300}" "${cmd:-}"
  done
}

if [ "${DNLAB_KIND_MATRIX_SKIP_LINUX:-0}" != "1" ]; then
  run_linux_live_case
fi
run_optional_cases

echo "SUMMARY pass=$passes skip=$skips fail=$failures"
if [ "$failures" -gt 0 ]; then
  exit 1
fi
