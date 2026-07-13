#!/usr/bin/env bash
set -euo pipefail

# Destructive guest-disk persistence qualification for dNLab-patched vrnetlab
# images. This is intentionally stronger than checking the host bind mount:
# it writes state from inside the NOS guest, then verifies the state after
# Containerlab restart and recreate.
#
# Automated cases:
#   - frr: integrated FRR configuration written through vtysh.
#   - openwrt: marker file in the guest filesystem.
#   - routeros: /system note value in the RouterOS configuration database.
#
# OpenWrt remains the default so existing v1 RouterOS images do not produce a
# misleading failure. Select cases with, for example:
#   DNLAB_GUEST_PERSIST_CASES=frr,openwrt,routeros ./integration_vrnetlab_guest_persistence.sh

command -v containerlab >/dev/null
command -v docker >/dev/null
command -v python3 >/dev/null

version="$(containerlab version --short)"
case "$version" in
  0.7[7-9].*|0.[89][0-9].*|[1-9].*) ;;
  *) echo "containerlab >=0.77.0 required, found $version" >&2; exit 1 ;;
esac

wait_healthy() {
  local container="$1"
  local timeout="${2:-180}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    local status
    status="$(docker inspect -f '{{.State.Health.Status}} {{.State.Status}}' "$container" 2>/dev/null || true)"
    case "$status" in
      healthy*) return 0 ;;
    esac
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "$container did not become healthy within ${timeout}s" >&2
  return 1
}

wait_startup_complete() {
  local container="$1"
  local expected="$2"
  local timeout="${3:-180}"
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    local count
    count="$(docker logs "$container" 2>&1 | grep -c 'Startup complete' || true)"
    if [ "$count" -ge "$expected" ]; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  echo "$container did not report Startup complete $expected time(s)" >&2
  docker logs --tail 80 "$container" >&2 || true
  return 1
}

run_openwrt() (
  image="${DNLAB_GUEST_PERSIST_OPENWRT_IMAGE:-}"
  if [ -z "$image" ]; then
    image="$(
      docker images --format '{{.Repository}}:{{.Tag}}' \
        | grep '^vrnetlab/openwrt_openwrt_v2:' \
        | sort -Vr \
        | head -n1
    )"
  fi

  if [ -z "$image" ] || ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "SKIP openwrt guest persistence: missing image vrnetlab/openwrt_openwrt_v2" >&2
    return 0
  fi

  workdir="$(mktemp -d /tmp/dnlab-guest-persist-openwrt.XXXXXX)"
  topology="$workdir/dnlab-guest-persist-openwrt.clab.yml"
  persist="$workdir/persist"
  container="clab-dnlab-guest-persist-openwrt-n1"
  marker="dnlab-guest-persist-$(date +%s)"
  mkdir -p "$persist"

  cleanup() {
    containerlab destroy -t "$topology" --cleanup >/dev/null 2>&1 || true
    rm -rf "$workdir"
  }
  trap cleanup EXIT

  write_topology() {
    local revision="$1"
    cat >"$topology" <<EOF
name: dnlab-guest-persist-openwrt
topology:
  nodes:
    n1:
      kind: openwrt
      image: $image
      binds:
        - $persist:/persist
      env:
        DNLAB_GUEST_REVISION: "$revision"
EOF
  }

  guest_console_cmd() {
    local command="$1"
    local expected="$2"
    python3 - "$container" "$command" "$expected" <<'PY'
import socket
import subprocess
import sys
import time

container, command, expected = sys.argv[1:4]
ip = subprocess.check_output(
    ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
    text=True,
).strip()
if not ip:
    raise SystemExit(f"container {container} has no docker IP")

sock = socket.create_connection((ip, 5000), timeout=10)
sock.settimeout(1)

def read_until(pattern: bytes, timeout: int) -> bytes:
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
            if chunk:
                buf += chunk
                if pattern in buf:
                    return buf
        except socket.timeout:
            sock.sendall(b"\n")
    return buf

read_until(b"root@OpenWrt", 45)
sentinel = b"DNLAB_GUEST_PERSIST_DONE"
sock.sendall(command.encode("utf-8") + b"; echo DNLAB_GUEST_PERSIST_DONE\n")
out = read_until(sentinel, 30).decode("utf-8", "ignore")
sock.close()
print(out)
if expected not in out:
    raise SystemExit(f"expected {expected!r} in guest console output")
PY
  }

  write_topology one
  containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"
  containerlab apply -t "$topology"
  wait_healthy "$container"

  guest_console_cmd \
    "mkdir -p /root; echo '$marker' > /root/dnlab-persist-marker; sync; cat /root/dnlab-persist-marker" \
    "$marker"

  containerlab restart -t "$topology" --node n1
  wait_healthy "$container"
  guest_console_cmd "cat /root/dnlab-persist-marker" "$marker"

  write_topology two
  containerlab apply -t "$topology" --dry-run | grep -q "recreated nodes"
  containerlab apply -t "$topology"
  wait_healthy "$container"
  guest_console_cmd "cat /root/dnlab-persist-marker" "$marker"

  echo "vrnetlab guest persistence integration: PASS openwrt restart+recreate"
)

run_routeros() (
  image="${DNLAB_GUEST_PERSIST_ROUTEROS_IMAGE:-}"
  if [ -z "$image" ]; then
    image="$(
      docker images --format '{{.Repository}}:{{.Tag}}' \
        | grep '^vrnetlab/mikrotik_routeros:.*-dnlab$' \
        | sort -Vr \
        | head -n1
    )"
  fi

  if [ -z "$image" ] || ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "routeros guest persistence requires a local vrnetlab/mikrotik_routeros:*dnlab image" >&2
    return 1
  fi
  if ! docker run --rm --entrypoint sh "$image" -lc \
    "grep -q 'mikrotik-persist-overlay-v2' /launch.py"; then
    echo "routeros image $image uses the legacy persistence patch; rebuild it with the v2 patch" >&2
    return 1
  fi

  workdir="$(mktemp -d /tmp/dnlab-guest-persist-routeros.XXXXXX)"
  topology="$workdir/dnlab-guest-persist-routeros.clab.yml"
  persist="$workdir/persist"
  container="clab-dnlab-guest-persist-routeros-n1"
  marker="dnlab-routeros-persist-$(date +%s)"
  mkdir -p "$persist"

  cleanup() {
    containerlab destroy -t "$topology" --cleanup >/dev/null 2>&1 || true
    rm -rf "$workdir"
  }
  trap cleanup EXIT

  write_topology() {
    local revision="$1"
    cat >"$topology" <<EOF
name: dnlab-guest-persist-routeros
topology:
  nodes:
    n1:
      kind: mikrotik_ros
      image: $image
      binds:
        - $persist:/persist
      env:
        DNLAB_GUEST_REVISION: "$revision"
EOF
  }

  guest_console_cmd() {
    local command="$1"
    local expected="$2"
    python3 - "$container" "$command" "$expected" <<'PY'
import socket
import subprocess
import sys
import time

container, command, expected = sys.argv[1:4]
ip = subprocess.check_output(
    ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
    text=True,
).strip()
sock = socket.create_connection((ip, 5000), timeout=10)
sock.settimeout(1)
out = b""
for _ in range(5):
    sock.sendall(b"\n")
    try:
        out += sock.recv(4096)
    except socket.timeout:
        pass
sock.sendall(command.encode("utf-8") + b"\n")
end = time.time() + 20
while time.time() < end:
    try:
        chunk = sock.recv(4096)
        if chunk:
            out += chunk
            if expected.encode("utf-8") in out:
                break
    except socket.timeout:
        sock.sendall(b"\n")
sock.close()
decoded = out.decode("utf-8", "ignore")
print(decoded)
if expected not in decoded:
    raise SystemExit(f"expected {expected!r} in RouterOS console output")
PY
  }

  write_topology one
  containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"
  containerlab apply -t "$topology"
  wait_startup_complete "$container" 1
  guest_console_cmd \
    "/system note set note=\"$marker\" show-at-login=no; :put (\"DNLAB_NOTE=\" . [/system note get note])" \
    "DNLAB_NOTE=$marker"

  containerlab restart -t "$topology" --node n1
  wait_startup_complete "$container" 2
  guest_console_cmd \
    ":put (\"DNLAB_NOTE=\" . [/system note get note])" \
    "DNLAB_NOTE=$marker"

  write_topology two
  containerlab apply -t "$topology" --dry-run | grep -q "recreated nodes"
  containerlab apply -t "$topology"
  wait_startup_complete "$container" 1
  guest_console_cmd \
    ":put (\"DNLAB_NOTE=\" . [/system note get note])" \
    "DNLAB_NOTE=$marker"

  echo "vrnetlab guest persistence integration: PASS routeros restart+recreate"
)

run_frr() (
  image="${DNLAB_GUEST_PERSIST_FRR_IMAGE:-}"
  if [ -z "$image" ]; then
    image="$(
      docker images --format '{{.Repository}}:{{.Tag}}' \
        | grep '^vrnetlab/dnlab_frr:' \
        | sort -Vr \
        | head -n1
    )"
  fi

  if [ -z "$image" ] || ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "SKIP FRR guest persistence: missing image vrnetlab/dnlab_frr" >&2
    return 0
  fi

  workdir="$(mktemp -d /tmp/dnlab-guest-persist-frr.XXXXXX)"
  topology="$workdir/dnlab-guest-persist-frr.clab.yml"
  persist="$workdir/persist"
  container="clab-dnlab-guest-persist-frr-n1"
  route="198.51.100.0/24"
  expected_hostname="n1"
  mkdir -p "$persist"

  cleanup() {
    containerlab destroy -t "$topology" --cleanup >/dev/null 2>&1 || true
    rm -rf "$workdir"
  }
  trap cleanup EXIT

  write_topology() {
    local revision="$1"
    cat >"$topology" <<EOF
name: dnlab-guest-persist-frr
topology:
  nodes:
    n1:
      kind: linux
      image: $image
      binds:
        - $persist:/persist
      env:
        CLAB_MGMT_PASSTHROUGH: "true"
        DNLAB_GUEST_REVISION: "$revision"
EOF
  }

  guest_vtysh_cmd() {
    local command="$1"
    local expected="$2"
    python3 - "$container" "$command" "$expected" <<'PY'
import socket
import subprocess
import sys
import time

container, command, expected = sys.argv[1:4]
ip = subprocess.check_output(
    ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
    text=True,
).strip()
if not ip:
    raise SystemExit(f"container {container} has no docker IP")

sock = socket.create_connection((ip, 5000), timeout=10)
sock.settimeout(1)

def read_until(pattern: bytes, timeout: int) -> bytes:
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
            if chunk:
                buf += chunk
                if pattern in buf:
                    return buf
        except socket.timeout:
            sock.sendall(b"\r\n")
    return buf

read_until(b"# ", 60)
sock.sendall(command.replace("\n", "\r\n").encode("utf-8") + b"\r\n")
time.sleep(2)
out = read_until(expected.encode("utf-8"), 30).decode("utf-8", "ignore")
sock.close()
print(out)
if expected not in out:
    raise SystemExit(f"expected {expected!r} in FRR console output")
PY
  }

  write_topology one
  containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"
  containerlab apply -t "$topology"
  wait_healthy "$container"
  guest_vtysh_cmd \
    $'configure terminal\nip route 198.51.100.0/24 Null0\nend\nwrite memory\nshow running-config' \
    "$route"
  grep -q "ip route $route Null0" "$persist/frr/frr.conf" || {
    echo "FRR persisted config missing route after write memory:" >&2
    sed -n '1,120p' "$persist/frr/frr.conf" >&2
    return 1
  }
  grep -q "hostname $expected_hostname" "$persist/frr/frr.conf" || {
    echo "FRR persisted config missing topology hostname after write memory:" >&2
    sed -n '1,120p' "$persist/frr/frr.conf" >&2
    return 1
  }

  containerlab restart -t "$topology" --node n1
  wait_healthy "$container"
  guest_vtysh_cmd "show running-config" "$route"
  guest_vtysh_cmd "show running-config" "hostname $expected_hostname"

  write_topology two
  containerlab apply -t "$topology" --dry-run | grep -q "recreated nodes"
  containerlab apply -t "$topology"
  wait_healthy "$container"
  guest_vtysh_cmd "show running-config" "$route"
  guest_vtysh_cmd "show running-config" "hostname $expected_hostname"
  grep -q "ip route $route Null0" "$persist/frr/frr.conf"
  grep -q "hostname $expected_hostname" "$persist/frr/frr.conf"

  echo "vrnetlab guest persistence integration: PASS frr restart+recreate"
)

cases="${DNLAB_GUEST_PERSIST_CASES:-openwrt}"
IFS=',' read -r -a requested_cases <<<"$cases"
for requested_case in "${requested_cases[@]}"; do
  case "$requested_case" in
    frr) run_frr ;;
    openwrt) run_openwrt ;;
    routeros) run_routeros ;;
    *) echo "unknown guest persistence case: $requested_case" >&2; exit 2 ;;
  esac
done
