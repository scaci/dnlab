#!/usr/bin/env bash
# Generate the dnlab-gui SSH keypair (if missing) and install its public
# key on every host listed in the orchestrator inventory, plus on every
# currently running jumphost. Idempotent: re-run after adding a new
# worker to hosts.yml.
#
# Separating the GUI key from the orchestrator key (~/.ssh/id_ed25519)
# keeps audit trails on destination hosts distinct. Default locations
# match /etc/dnlab/paths.yml and can be overridden with environment
# variables or --hosts.
#
# Usage:
#   scripts/setup-gui-ssh-key.sh               # default
#   scripts/setup-gui-ssh-key.sh --force       # overwrite existing keypair
#   scripts/setup-gui-ssh-key.sh --hosts /path/to/hosts.yml
#
# Bootstrap path: the master uses its orchestrator key to distribute
# the GUI pubkey to workers. The jumphost authorized_keys is refreshed
# on any running jumphost via `docker exec`; labs deployed after this
# script has run pick up the GUI key automatically at deploy time.
set -euo pipefail

cd "$(dirname "$0")/.."

# ── Locations (match /etc/dnlab/paths.yml defaults) ─────────────────
GUI_KEY_PATH="${DNLABGUI_SSH_KEY:-/root/.ssh/dnlab-gui.key}"
ORCH_KEY_PATH="${DNLAB_ORCH_SSH_KEY:-/root/.ssh/id_ed25519}"
HOSTS_FILE="${DNLAB_MULTINODE_HOSTS:-/etc/dnlab/hosts.yml}"

FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)  FORCE=1; shift ;;
    --hosts)  HOSTS_FILE="$2"; shift 2 ;;
    -h|--help)
      sed -n '3,17p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -r "$HOSTS_FILE" ]]; then
  echo "[error] hosts inventory not readable: $HOSTS_FILE" >&2
  exit 2
fi

# ── 1. Generate keypair if missing ──────────────────────────────────
if [[ -f "$GUI_KEY_PATH" && $FORCE -eq 0 ]]; then
  echo "[1/4] GUI key already present at $GUI_KEY_PATH (skip generation)"
else
  if [[ -f "$GUI_KEY_PATH" ]]; then
    echo "[1/4] --force given: overwriting $GUI_KEY_PATH"
    rm -f "$GUI_KEY_PATH" "$GUI_KEY_PATH.pub"
  else
    echo "[1/4] generating $GUI_KEY_PATH"
  fi
  mkdir -p "$(dirname "$GUI_KEY_PATH")"
  chmod 700 "$(dirname "$GUI_KEY_PATH")"
  ssh-keygen -t ed25519 -f "$GUI_KEY_PATH" -N "" -q \
    -C "dnlab-gui@$(hostname -s)"
fi

PUBKEY="$(cat "$GUI_KEY_PATH.pub")"
if [[ -z "$PUBKEY" ]]; then
  echo "[error] empty pubkey at $GUI_KEY_PATH.pub" >&2
  exit 3
fi

# ── 2. Parse hosts.yml to a list of user@host entries ───────────────
# Simple YAML reader: we only need `host:` and `ssh_user:` under the
# `infrastructure:` tree. Python is already available on any host that
# runs dnlab-gui, so shell out to it for robust parsing.
mapfile -t TARGETS < <(
  python3 - "$HOSTS_FILE" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}
infra = cfg.get("infrastructure") or {}
out = []
m = infra.get("master") or {}
if m.get("host"):
    out.append(f"{m.get('ssh_user','root')}@{m['host']}")
for name, w in (infra.get("workers") or {}).items():
    if w.get("host"):
        out.append(f"{w.get('ssh_user','root')}@{w['host']}")
print("\n".join(out))
PY
)

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "[error] no hosts resolved from $HOSTS_FILE" >&2
  exit 4
fi

echo "[2/4] targets: ${TARGETS[*]}"

# ── 3. Push pubkey to every host (idempotent append) ────────────────
# Use the orchestrator key as bootstrap credential. For the master
# itself (localhost) we skip SSH and append directly.
LOCAL_HOSTNAMES=("$(hostname -s)" "$(hostname)" "localhost")
append_authorized_keys_local() {
  local home
  home="$(eval echo ~"$1")"
  mkdir -p "$home/.ssh"
  chmod 700 "$home/.ssh"
  touch "$home/.ssh/authorized_keys"
  chmod 600 "$home/.ssh/authorized_keys"
  grep -qxF "$PUBKEY" "$home/.ssh/authorized_keys" \
    || printf '%s\n' "$PUBKEY" >> "$home/.ssh/authorized_keys"
}

echo "[3/4] installing pubkey on hosts..."
for t in "${TARGETS[@]}"; do
  user="${t%@*}"
  host="${t#*@}"
  is_local=0
  for h in "${LOCAL_HOSTNAMES[@]}"; do
    [[ "$host" == "$h" ]] && { is_local=1; break; }
  done
  if [[ $is_local -eq 1 ]]; then
    echo "  → $t (local append)"
    append_authorized_keys_local "$user"
  else
    echo "  → $t"
    # shellcheck disable=SC2087
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -i "$ORCH_KEY_PATH" "$t" bash -s <<EOF
set -e
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF '$PUBKEY' ~/.ssh/authorized_keys \
  || printf '%s\n' '$PUBKEY' >> ~/.ssh/authorized_keys
EOF
  fi
done

# ── 4. Refresh authorized_keys on any running jumphost container ────
# New labs deployed after this script will get the merged keyset from
# collect_authorized_pubkeys() in the orchestrator. For labs already
# up, we inject the key live so the GUI works without requiring a
# destroy/redeploy.
echo "[4/4] refreshing running jumphost containers..."
# shellcheck disable=SC2207
JH_CONTAINERS=($(docker ps --format '{{.Names}}' | grep -E '^dnlab-.*-jumphost$' || true))
if [[ ${#JH_CONTAINERS[@]} -eq 0 ]]; then
  echo "  (no running jumphosts)"
else
  for jh in "${JH_CONTAINERS[@]}"; do
    echo "  → $jh"
    docker exec "$jh" bash -c "
      mkdir -p /home/labuser/.ssh
      chmod 700 /home/labuser/.ssh
      touch /home/labuser/.ssh/authorized_keys
      chmod 600 /home/labuser/.ssh/authorized_keys
      grep -qxF '$PUBKEY' /home/labuser/.ssh/authorized_keys \
        || printf '%s\n' '$PUBKEY' >> /home/labuser/.ssh/authorized_keys
      chown -R labuser:labuser /home/labuser/.ssh
    "
  done
fi

cat <<EOF

[done] dnlab-gui key distributed.
  private: $GUI_KEY_PATH
  public:  $GUI_KEY_PATH.pub
  hosts:   ${TARGETS[*]}

Next steps:
  - Restart dnlab-gui so app.config picks up the new key:
      systemctl restart dnlab-gui
  - Verify console works: open any VD console in the GUI.
  - To rotate later: re-run with --force.
EOF
