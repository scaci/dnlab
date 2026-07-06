#!/usr/bin/env bash
# Create (or refresh) the dnlab-gui venv and install GUI requirements.
# The Docker target talks to dnlab-multinode through DNLAB_MULTINODE_API_URL and
# must not install dnlab-multinode in the GUI environment.
#
# Usage:
#   scripts/bootstrap.sh                         # install/refresh GUI only
#   scripts/bootstrap.sh --recreate              # wipe venv and rebuild GUI only
#   scripts/bootstrap.sh --with-local-multinode  # optional standalone fallback
#
# When --with-local-multinode is used, the package is built and installed as a
# wheel from DNLAB_MULTINODE_DIR, not installed in editable mode.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="$ROOT/venv"
MULTINODE_DIR="${DNLAB_MULTINODE_DIR:-/opt/dnlab/src/multinode}"
WITH_LOCAL_MULTINODE=0

for arg in "$@"; do
  case "$arg" in
    --recreate)
      echo "[bootstrap] removing existing venv at $VENV"
      rm -rf "$VENV"
      ;;
    --with-local-multinode)
      WITH_LOCAL_MULTINODE=1
      ;;
    *)
      echo "[bootstrap] error: unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$VENV" ]]; then
  echo "[bootstrap] creating venv at $VENV"
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel
"$VENV/bin/pip" install --quiet -r "$ROOT/requirements.txt"

if [[ "$WITH_LOCAL_MULTINODE" == "1" ]]; then
  if [[ ! -d "$MULTINODE_DIR" ]]; then
    echo "[bootstrap] error: dnlab-multinode not found at $MULTINODE_DIR" >&2
    echo "  set DNLAB_MULTINODE_DIR to the correct path and re-run" >&2
    exit 2
  fi
  WHEELHOUSE="$ROOT/.wheelhouse"
  rm -rf "$WHEELHOUSE"
  mkdir -p "$WHEELHOUSE"
  "$VENV/bin/pip" wheel --quiet --wheel-dir "$WHEELHOUSE" "$MULTINODE_DIR"
  "$VENV/bin/pip" install --quiet --no-index --find-links "$WHEELHOUSE" dnlab-multinode
fi

echo "[bootstrap] dnlab-gui ready. Start with:"
echo "  $VENV/bin/python3 $ROOT/run.py"
echo "or via systemd:"
echo "  systemctl start dnlab-gui"
echo "For the Docker target, start the compose stack from /root/dnlab-dev-docs/docker."
