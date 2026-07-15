#!/usr/bin/env python3
"""Technical warm-link qualification for one derived vrnetlab image.

This validates image construction, boot health, carrier control, range checks,
20-cycle stability and basic resource counters. Vendor dataplane traffic is a
separate certification gate and is intentionally not inferred from this test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APPLY = ROOT / "apply.py"

DEPLOY_KINDS = {
    "dnlab_frr": "linux",
    "dnlab_opnsense": "freebsd",
    "nvidia_cumulusvx": "generic_vm",
    "cisco_nxos": "cisco_n9kv",
    "cisco_c9800cl": "cisco_cat9kv",
}


def run(cmd: list[str], *, check: bool = True, capture: bool = False):
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def docker_exec(container: str, *args: str, check: bool = True):
    return run(["docker", "exec", container, *args], check=check, capture=True)


def wait_healthy(container: str, timeout: int) -> float:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        result = run(
            [
                "docker", "inspect", "-f",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                container,
            ],
            check=False,
            capture=True,
        )
        state, _, health = result.stdout.strip().partition("|")
        if state in {"exited", "dead"}:
            raise RuntimeError(f"{container} entered terminal state {state}")
        if health == "healthy" or (state == "running" and not health):
            return time.monotonic() - started
        time.sleep(2)
    raise TimeoutError(f"timeout waiting for {container} health")


def inspect_container(container: str) -> dict:
    result = run(
        ["docker", "inspect", "-f", "{{.Id}}|{{.RestartCount}}|{{.State.StartedAt}}", container],
        capture=True,
    )
    identity, restarts, started = result.stdout.strip().split("|", 2)
    return {"id": identity, "restarts": int(restarts), "started_at": started}


def qemu_metrics(container: str) -> dict:
    script = r'''
pid=$(pgrep -o -f 'qemu-system|qemu-kvm')
test -n "$pid" || exit 3
rss=$(awk '/VmRSS:/{print $2}' /proc/$pid/status)
fds=$(find /proc/$pid/fd -mindepth 1 -maxdepth 1 | wc -l)
taps=$(find /sys/class/net -maxdepth 1 -name 'tap*' | wc -l)
netdevs=$(find /sys/class/net -mindepth 1 -maxdepth 1 | wc -l)
filters=$(
  for path in /sys/class/net/eth* /sys/class/net/tap*; do
    test -e "$path" || continue
    dev=$(basename "$path")
    tc filter show dev "$dev" ingress 2>/dev/null
    tc filter show dev "$dev" egress 2>/dev/null
  done | wc -l
)
printf '%s|%s|%s|%s|%s\n' "$rss" "$fds" "$taps" "$netdevs" "$filters"
'''
    result = docker_exec(container, "sh", "-c", script)
    rss, fds, taps, netdevs, filters = result.stdout.strip().split("|")
    return {
        "qemu_rss_kib": int(rss),
        "qemu_fds": int(fds),
        "taps": int(taps),
        "netdevs": int(netdevs),
        "tc_filter_lines": int(filters),
    }


def topology(
    lab: str, image: str, ports: int, subnet_octet: int, deploy_kind: str,
    vswitch_path: Path | None = None,
) -> str:
    lines = [
        f"name: {lab}",
        "mgmt:",
        f"  network: {lab}-mgmt",
        f"  bridge: br-{lab[:11]}",
        f"  ipv4-subnet: 198.18.{subnet_octet}.0/24",
        f"  ipv4-gw: 198.18.{subnet_octet}.1",
        "topology:",
        "  nodes:",
        "    dut:",
        f"      kind: {deploy_kind}",
        f"      image: {image}",
        "      env:",
        "        CLAB_MGMT_PASSTHROUGH: \"true\"",
        f"        DNLAB_WARM_PORTS: \"{ports}\"",
        "        DNLAB_NIC_POLL_INTERVAL: \"0.05\"",
    ]
    if vswitch_path is not None:
        lines.extend([
            "      binds:",
            f'        - "{vswitch_path}:/vswitch.xml"',
        ])
    lines.append("  links:")
    for index in range(1, ports + 1):
        lines.append(f'    - endpoints: ["dut:eth{index}", "host:wq{subnet_octet:02d}e{index}"]')
    return "\n".join(lines) + "\n"


def qualify(args) -> dict:
    token = hashlib.sha1(f"{args.kind}:{args.image}".encode()).hexdigest()[:6]
    suffix = f"-warmqual-{token}"
    test_image = args.image if args.prebuilt else f"{args.image}{suffix}"
    lab = f"wq{token}"[:10]
    container = f"clab-{lab}-dut"
    subnet_octet = 20 + (int(token[:2], 16) % 180)
    report = {
        "kind": args.kind,
        "base_image": args.image,
        "test_image": test_image,
        "warm_ports": args.ports,
        "cycles": args.cycles,
        "status": "failed",
    }

    with tempfile.TemporaryDirectory(prefix="dnlab-warm-qual-") as td:
        topo = Path(td) / "qual.clab.yml"
        deploy_kind = args.deploy_kind or DEPLOY_KINDS.get(args.kind, args.kind)
        report["deploy_kind"] = deploy_kind
        vswitch_path = None
        if args.kind == "cisco_cat9kv":
            vswitch_path = Path(td) / "vswitch.xml"
            vswitch_path.write_text(
                "<switch>\n"
                "  <asic_type>UADP</asic_type>\n"
                "  <port_count>24</port_count>\n"
                "  <serial_number>FOCWARM00001</serial_number>\n"
                "  <prod_serial_number>FOCWARM00001</prod_serial_number>\n"
                "</switch>\n",
                encoding="utf-8",
            )
            report["vswitch_xml"] = True
        topo.write_text(
            topology(
                lab, test_image, args.ports, subnet_octet, deploy_kind,
                vswitch_path,
            ),
            encoding="utf-8",
        )
        try:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            if not args.prebuilt:
                subprocess.run(
                    [sys.executable, str(APPLY), args.kind, args.image, f"--tag-suffix={suffix}"],
                    check=True,
                    env=env,
                )
            labels = json.loads(run(
                ["docker", "image", "inspect", test_image, "--format", "{{json .Config.Labels}}"],
                capture=True,
            ).stdout)
            report["labels"] = labels
            if labels.get("org.dnlab.capabilities") != "warm-links-v1":
                raise RuntimeError("missing warm-links-v1 capability label")

            run(["containerlab", "deploy", "-t", str(topo)])
            report["boot_seconds"] = round(wait_healthy(container, args.timeout), 3)
            before = inspect_container(container)
            metrics_before = qemu_metrics(container)

            indexes = sorted({1, max(1, (args.ports + 1) // 2), args.ports})
            for index in indexes:
                iface = f"eth{index}"
                for state in ("down", "up", "down"):
                    result = docker_exec(container, "dnlab-linkctl", iface, state)
                    if result.stdout.strip() != f"OK {iface} {state}":
                        raise RuntimeError(result.stdout + result.stderr)
            invalid = docker_exec(
                container, "dnlab-linkctl", f"eth{args.ports + 1}", "up", check=False,
            )
            if invalid.returncode == 0 or "exceeds configured warm-port count" not in invalid.stderr:
                raise RuntimeError("out-of-range interface was not rejected")

            for _ in range(args.cycles):
                docker_exec(container, "dnlab-linkctl", "eth1", "down")
                docker_exec(container, "dnlab-linkctl", "eth1", "up")
            docker_exec(container, "dnlab-linkctl", "eth1", "down")

            after = inspect_container(container)
            metrics_after = qemu_metrics(container)
            if before != after:
                raise RuntimeError(f"container identity changed: {before} -> {after}")
            if metrics_after["qemu_fds"] != metrics_before["qemu_fds"]:
                raise RuntimeError("QEMU file descriptor count changed across cycles")
            if metrics_after["tc_filter_lines"] != metrics_before["tc_filter_lines"]:
                raise RuntimeError("tc filter count changed across cycles")
            report.update({
                "container": after,
                "metrics_before": metrics_before,
                "metrics_after": metrics_after,
                "tested_ports": indexes,
                "status": "technical-pass",
            })
            return report
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            if run(["docker", "inspect", container], check=False, capture=True).returncode == 0:
                logs = run(["docker", "logs", container], check=False, capture=True)
                report["container_logs_tail"] = (logs.stdout + logs.stderr)[-12000:]
                report["container"] = inspect_container(container)
            return report
        finally:
            run(["containerlab", "destroy", "-t", str(topo), "--cleanup"], check=False)
            if not args.prebuilt and not args.keep_image:
                run(["docker", "image", "rm", test_image], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind")
    parser.add_argument("image")
    parser.add_argument("--ports", type=int, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--deploy-kind")
    parser.add_argument("--keep-image", action="store_true")
    parser.add_argument("--prebuilt", action="store_true",
                        help="qualify the exact supplied image without creating a derivative")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        report = qualify(args)
    except Exception as exc:
        report = {
            "kind": args.kind,
            "base_image": args.image,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "technical-pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
