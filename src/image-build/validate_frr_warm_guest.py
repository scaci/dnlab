#!/usr/bin/env python3
"""FRR two-guest warm-link acceptance test.

Requires a warm-patched FRR image. It verifies carrier state through each
guest's vtysh console and traffic across a host bridge while applying the same
attach/detach ordering used by the multinode runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path


TELNET_CLIENT = r"""
import select
import socket
import sys
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
command = sys.argv[1]
payload = ("\r" + command + "\r").encode()
s = socket.create_connection(("127.0.0.1", 5000), 3)
s.setblocking(False)
deadline = time.monotonic() + 15
sent_at = time.monotonic() + 0.5
sent = False
last_data = None
out = bytearray()
pending = bytearray()

while time.monotonic() < deadline:
    if not sent and time.monotonic() >= sent_at:
        s.sendall(payload)
        sent = True
    readable, _, _ = select.select([s], [], [], 0.1)
    if not readable:
        if sent and last_data and time.monotonic() - last_data > 0.7:
            break
        continue
    data = s.recv(65535)
    if not data:
        break
    pending.extend(data)
    last_data = time.monotonic()
    index = 0
    while index < len(pending):
        if pending[index] != IAC:
            out.append(pending[index])
            index += 1
            continue
        if index + 1 >= len(pending):
            break
        verb = pending[index + 1]
        if verb == IAC:
            out.append(IAC)
            index += 2
        elif verb in (DO, DONT, WILL, WONT):
            if index + 2 >= len(pending):
                break
            option = pending[index + 2]
            if verb == DO:
                s.sendall(bytes((IAC, WONT, option)))
            elif verb == WILL:
                s.sendall(bytes((IAC, DONT, option)))
            index += 3
        elif verb == SB:
            end = pending.find(bytes((IAC, SE)), index + 2)
            if end < 0:
                break
            index = end + 2
        else:
            index += 2
    del pending[:index]
sys.stdout.buffer.write(out)
"""

LAB = "wqfrrguest"
BRIDGE = "br-wqfrrguest"
CONTAINERS = {"r1": f"clab-{LAB}-r1", "r2": f"clab-{LAB}-r2"}
MGMT = {"r1": "198.18.230.2", "r2": "198.18.230.3"}
HOST_IFACES = {"r1": "wqfgr1e1", "r2": "wqfgr2e1"}


def run(cmd: list[str], *, check: bool = True, capture: bool = False):
    return subprocess.run(
        cmd, check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def topology(image: str) -> str:
    lines = [
        f"name: {LAB}",
        "mgmt:",
        f"  network: {LAB}-mgmt",
        f"  bridge: br-{LAB[:11]}",
        "  ipv4-subnet: 198.18.230.0/24",
        "  ipv4-gw: 198.18.230.1",
        "topology:",
        "  nodes:",
    ]
    for node in ("r1", "r2"):
        lines.extend([
            f"    {node}:",
            "      kind: linux",
            f"      image: {image}",
            f"      mgmt-ipv4: {MGMT[node]}",
            "      env:",
            "        CLAB_MGMT_PASSTHROUGH: \"true\"",
            "        DNLAB_WARM_PORTS: \"8\"",
            "        DNLAB_NIC_POLL_INTERVAL: \"0.05\"",
        ])
    lines.append("  links:")
    for node in ("r1", "r2"):
        for index in range(1, 9):
            host_iface = HOST_IFACES[node] if index == 1 else f"{node}wq{index}"
            lines.append(
                f'    - endpoints: ["{node}:eth{index}", "host:{host_iface}"]'
            )
    return "\n".join(lines) + "\n"


def wait_healthy(container: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run([
            "docker", "inspect", "-f",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container,
        ], check=False, capture=True)
        state, _, health = result.stdout.strip().partition("|")
        if state in {"exited", "dead"}:
            raise RuntimeError(f"{container} entered {state}")
        if health == "healthy" or (state == "running" and not health):
            return
        time.sleep(2)
    raise TimeoutError(f"timeout waiting for {container}")


def guest(container: str, command: str) -> str:
    """Run vtysh commands on the released vrnetlab serial console."""
    result = subprocess.run(
        [
            "docker", "exec", container,
            "python3", "-c", TELNET_CLIENT, command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    out = result.stdout.decode(errors="replace").replace("\r", "")
    err = result.stderr.decode(errors="replace")
    if result.returncode != 0 or "% Unknown command:" in out:
        raise RuntimeError(
            f"serial command failed: {command}: "
            f"{out[-1000:]} {err[-500:]}"
        )
    return out


def carrier_value(container: str) -> str:
    out = guest(container, "show interface eth1")
    matches = re.findall(r"line protocol is (up|down)", out, re.IGNORECASE)
    if not matches:
        raise RuntimeError(f"no carrier state in serial output: {out[-1000:]}")
    return "1" if matches[-1].lower() == "up" else "0"


def carrier(container: str, state: str) -> None:
    result = run(
        ["docker", "exec", container, "dnlab-linkctl", "eth1", state],
        capture=True,
    )
    if result.stdout.strip() != f"OK eth1 {state}":
        raise RuntimeError(result.stdout + result.stderr)


def host_ping(address: str, *, reachable: bool) -> str:
    result = run(
        ["ping", "-c", "3" if reachable else "1", "-W", "1", address],
        check=False, capture=True,
    )
    succeeded = result.returncode == 0
    if succeeded != reachable:
        expectation = "reachable" if reachable else "unreachable"
        raise RuntimeError(
            f"{address} expected {expectation}: {result.stdout}{result.stderr}"
        )
    return result.stdout + result.stderr


def bridge_create() -> None:
    run(["ip", "link", "del", BRIDGE], check=False)
    run(["ip", "link", "add", BRIDGE, "type", "bridge"])
    run(["ip", "link", "set", BRIDGE, "up"])
    run(["ip", "address", "add", "10.255.0.6/29", "dev", BRIDGE])
    for iface in HOST_IFACES.values():
        run(["ip", "link", "set", iface, "master", BRIDGE])
        run(["ip", "link", "set", iface, "up"])


def bridge_detach() -> None:
    for iface in HOST_IFACES.values():
        run(["ip", "link", "set", iface, "nomaster"])


def bridge_attach() -> None:
    for iface in HOST_IFACES.values():
        run(["ip", "link", "set", iface, "master", BRIDGE])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--report")
    args = parser.parse_args()
    report = {"image": args.image, "status": "failed", "guest": "FRR"}
    with tempfile.TemporaryDirectory(prefix="dnlab-frr-guest-") as td:
        topo = Path(td) / "guest.clab.yml"
        topo.write_text(topology(args.image), encoding="utf-8")
        try:
            run(["containerlab", "deploy", "-t", str(topo)])
            for container in CONTAINERS.values():
                wait_healthy(container)
            for node, container in CONTAINERS.items():
                address = "10.255.0.1/29" if node == "r1" else "10.255.0.2/29"
                guest(
                    container,
                    "configure terminal\rinterface eth1\r"
                    f"ip address {address}\rno shutdown\rend",
                )
                initial = carrier_value(container)
                if initial != "0":
                    raise RuntimeError(f"{node}: initial eth1 carrier is {initial}, expected 0")

            bridge_create()
            for container in CONTAINERS.values():
                carrier(container, "up")
            for node, container in CONTAINERS.items():
                if carrier_value(container) != "1":
                    raise RuntimeError(f"{node}: eth1 did not become operational")
            host_ping("10.255.0.1", reachable=True)
            host_ping("10.255.0.2", reachable=True)

            for container in CONTAINERS.values():
                carrier(container, "down")
            for node, container in CONTAINERS.items():
                if carrier_value(container) != "0":
                    raise RuntimeError(f"{node}: eth1 carrier remained up after detach")
            bridge_detach()
            ping_down = host_ping("10.255.0.2", reachable=False)

            bridge_attach()
            for container in CONTAINERS.values():
                carrier(container, "up")
            host_ping("10.255.0.1", reachable=True)
            host_ping("10.255.0.2", reachable=True)
            report.update({
                "status": "passed",
                "initial_carrier": "down",
                "attach_traffic": "passed",
                "detach_carrier": "down",
                "reattach_traffic": "passed",
                "ping_down_output": ping_down[-500:],
            })
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            for container in CONTAINERS.values():
                run(
                    ["docker", "exec", container, "dnlab-linkctl", "eth1", "down"],
                    check=False, capture=True,
                )
            run(["ip", "link", "del", BRIDGE], check=False)
            run(["containerlab", "destroy", "-t", str(topo), "--cleanup"], check=False)

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
