#!/usr/bin/env bash
# Thin wrapper around apply.py so the CLI feels shell-native.
#   apply.sh cisco_n9kv vrnetlab/cisco_n9kv_v2:9300-10.5.5.M
set -euo pipefail
exec python3 "$(dirname "$0")/apply.py" "$@"
