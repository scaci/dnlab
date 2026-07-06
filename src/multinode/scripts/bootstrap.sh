#!/usr/bin/env bash
# Create (or refresh) the dnlab-multinode venv and install the package from a
# built wheel. Idempotent: re-running rebuilds the wheel and reinstalls the
# package against the current setup.py without using editable mode.
#
# Usage:
#   scripts/bootstrap.sh                # install/refresh
#   scripts/bootstrap.sh --recreate     # wipe venv and rebuild from scratch
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="$ROOT/venv"

if [[ "${1:-}" == "--recreate" ]]; then
  echo "[bootstrap] removing existing venv at $VENV"
  rm -rf "$VENV"
fi

if [[ ! -d "$VENV" ]]; then
  echo "[bootstrap] creating venv at $VENV"
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel
WHEELHOUSE="$ROOT/.wheelhouse"
rm -rf "$WHEELHOUSE"
mkdir -p "$WHEELHOUSE"
"$VENV/bin/pip" wheel --quiet --wheel-dir "$WHEELHOUSE" "$ROOT"
"$VENV/bin/pip" install --quiet --no-index --find-links "$WHEELHOUSE" dnlab-multinode

echo "[bootstrap] dnlab-multinode ready. Try:"
echo "  $VENV/bin/dnlab-multinode --help"
echo "  $VENV/bin/dnlab-image-sync --help"
