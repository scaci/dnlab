#!/usr/bin/env bash
set -euo pipefail

# Destructive integration smoke for the opt-in per-host runtime.
# It only creates resources whose names start with dnlab-apply-it.

command -v containerlab >/dev/null
command -v docker >/dev/null

version="$(containerlab version --short)"
case "$version" in
  0.7[7-9].*|0.[89][0-9].*|[1-9].*) ;;
  *) echo "containerlab >=0.77.0 required, found $version" >&2; exit 1 ;;
esac

workdir="$(mktemp -d /tmp/dnlab-apply-it.XXXXXX)"
topology="$workdir/dnlab-apply-it.clab.yml"
persist="$workdir/persist"
mkdir -p "$persist"

cleanup() {
  containerlab destroy -t "$topology" --cleanup >/dev/null 2>&1 || true
  rm -rf "$workdir"
}
trap cleanup EXIT

cat >"$topology" <<EOF
name: dnlab-apply-it
topology:
  nodes:
    n1:
      kind: linux
      image: alpine:3
      cmd: sleep infinity
      binds:
        - $persist:/persist
      env:
        DNLAB_REVISION: "one"
EOF

containerlab apply -t "$topology" --dry-run | grep -q "deploy lab"
containerlab apply -t "$topology"
docker exec clab-dnlab-apply-it-n1 sh -c \
  'printf persisted-by-guest >/persist/marker'

sed -i 's/DNLAB_REVISION: "one"/DNLAB_REVISION: "two"/' "$topology"
containerlab apply -t "$topology" --dry-run | grep -q "recreated nodes"
containerlab apply -t "$topology"

test "$(cat "$persist/marker")" = "persisted-by-guest"
test "$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \
  clab-dnlab-apply-it-n1 | grep '^DNLAB_REVISION=')" = "DNLAB_REVISION=two"

containerlab stop -t "$topology" --node n1
test "$(docker inspect -f '{{.State.Status}}' clab-dnlab-apply-it-n1)" = "exited"
containerlab start -t "$topology" --node n1
test "$(docker inspect -f '{{.State.Status}}' clab-dnlab-apply-it-n1)" = "running"

echo "containerlab apply integration smoke: PASS"
