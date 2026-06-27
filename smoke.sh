#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILES="${COMPOSE_FILES:-${COMPOSE_FILE:-compose.yml}}"
PROXY_URL="${DNLAB_SMOKE_PROXY_URL:-https://localhost:${DNLAB_PROXY_HTTPS_PORT:-443}/}"
IFS=: read -r -a COMPOSE_FILE_LIST <<EOF
$COMPOSE_FILES
EOF

compose() {
  local args=()
  local file
  for file in "${COMPOSE_FILE_LIST[@]}"; do
    args+=("-f" "$file")
  done
  docker compose "${args[@]}" "$@"
}

curl_args=(-sSk)
if [ -n "${DNLAB_SMOKE_CURL_RESOLVE:-}" ]; then
  curl_args+=(--resolve "$DNLAB_SMOKE_CURL_RESOLVE")
fi

echo "== compose status =="
compose ps

echo "== proxy =="
proxy_code="$(curl "${curl_args[@]}" -o /tmp/dnlab-smoke-proxy.html -w '%{http_code}' --max-time 5 "$PROXY_URL")"
test "$proxy_code" = "200"
echo "proxy ${PROXY_URL} -> ${proxy_code}"

echo "== gui isolation =="
compose exec -T gui sh -lc 'test ! -S /var/run/docker.sock'
compose exec -T gui python - <<'PY'
import importlib.util
import sys

from app.main import create_app
from app.config import settings
from app.services.realnet_bgp import route_reflector_status

assert settings.DNLAB_MULTINODE_API_URL, "DNLAB_MULTINODE_API_URL is required in the Docker GUI target"
assert settings.DNLAB_IMAGE_BUILD_API_URL, "DNLAB_IMAGE_BUILD_API_URL is required in the Docker GUI target"
assert importlib.util.find_spec("dnlab_multinode") is None, "dnlab_multinode package is installed in GUI image"
app = create_app()
loaded = sorted(name for name in sys.modules if name.startswith("dnlab_multinode"))
assert not loaded, f"GUI app loaded dnlab_multinode modules: {loaded[:20]}"
paths = {getattr(route, "path", "") for route in app.routes}
assert "/api/labs/{lab_id}/follow-rabbit/sessions" in paths, "Plus Follow the Rabbit public API route missing"
status = route_reflector_status()
assert status.get("container") == "dnlab-realnet-rr", status
print({
    "routes": len(app.routes),
    "dnlab_modules_loaded_count": len(loaded),
    "multinode_api": settings.DNLAB_MULTINODE_API_URL,
    "image_build_api": settings.DNLAB_IMAGE_BUILD_API_URL,
    "realnet_rr": status,
})
PY

echo "== multinode api =="
compose exec -T multinode python - <<'PY'
import importlib.util
import json
from urllib.request import Request, urlopen


def request(method, path, payload=None, timeout=20):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {} if payload is None else {"Content-Type": "application/json"}
    req = Request(f"http://127.0.0.1:8081{path}", data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as response:
        return response.status, json.load(response)


status, health = request("GET", "/health")
assert status == 200 and health.get("ok") is True, health
assert importlib.util.find_spec("dnlab_multinode.services.follow_rabbit") is not None, "Plus Follow the Rabbit service missing"

hosts_content = """infrastructure:
  master:
    host: localhost
    ssh_user: root
  workers: {}
"""
status, validation = request("POST", "/hosts/validate", {"content": hosts_content})
assert status == 200 and validation.get("ok") is True, validation

status, rr = request("POST", "/realnet/rr/status", {"hosts_file": "/etc/dnlab/hosts.yml"})
assert status == 200 and rr.get("container") == "dnlab-realnet-rr", rr

status, images = request("GET", "/docker/images")
assert status == 200 and isinstance(images.get("images"), list), images

print({
    "health": health,
    "hosts_validate": validation,
    "realnet_rr": rr,
    "images": len(images["images"]),
    "follow_rabbit": "available",
})
PY

echo "== lab cleanup daemon =="
compose ps lab-cleanup
compose exec -T lab-cleanup sh -lc '
  for i in $(seq 1 30); do
    if dnlab-lab-cleanup status --json >/tmp/dnlab-lab-cleanup-status.json; then
      cat /tmp/dnlab-lab-cleanup-status.json
      exit 0
    fi
    sleep 1
  done
  echo "lab cleanup state was not published" >&2
  exit 1
'

echo "== image-build api =="
compose exec -T image-build python - <<'PY'
import json
from urllib.request import urlopen


def get(path, timeout=20):
    with urlopen(f"http://127.0.0.1:8082{path}", timeout=timeout) as response:
        return response.status, json.load(response)


status, health = get("/health")
assert status == 200 and health.get("ok") is True, health
status, kinds = get("/kinds")
assert status == 200 and kinds.get("available") is True, kinds
assert isinstance(kinds.get("patchable"), list), kinds
print({"health": health, "patchable": len(kinds["patchable"])})
PY

echo "== syslog guardrail =="
if rg -n "syslog|log-shipper|syslog_mount" "${COMPOSE_FILE_LIST[@]}"; then
  echo "runtime syslog/log-shipper reference found in docker compose files" >&2
  exit 1
fi

echo "smoke ok"
