#!/usr/bin/env python3
"""Sequentially rebuild the approved hot-add inventory through image-build API."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path


INVENTORY = [
    ("dnlab_opnsense", "/opt/img_dnlab/OPNsense-26.1.6-serial-amd64.img.bz2", "vrnetlab/dnlab_opnsense:26.1.6-dnlab"),
    ("nvidia_cumulusvx", "/opt/img_dnlab/cumulus-linux-5.16.1-vx-amd64.qcow2", "vrnetlab/nvidia_cumulusvx:5.16.1-vx-amd64-dnlab"),
    ("mikrotik_ros", "/opt/img_dnlab/chr-7.22.2.vmdk", "vrnetlab/mikrotik_routeros:7.22.2-dnlab"),
    ("cisco_vios", "/opt/img_dnlab/iosv-159-3-m10/cisco_vios-adventerprisek9-m.spa.159-3.m10.qcow2", "vrnetlab/cisco_vios_v2:adventerprisek9-m.spa.159-3.m10-dnlab"),
    ("cisco_vios", "/opt/img_dnlab/iosvl2-2020/vios_l2-adventerprisek9-m.ssa.high_iron_20200929.qcow2", "vrnetlab/cisco_vios_l2_v2:L2-20200929-dnlab"),
    ("juniper_vjunosrouter", "/opt/img_dnlab/vJunos-router-25.2R1.9.qcow2", "vrnetlab/juniper_vjunos-router_v2:25.2R1.9-dnlab"),
    ("juniper_vjunosrouter", "/opt/img_dnlab/vJunos-router-25.4R1.12.qcow2", "vrnetlab/juniper_vjunos-router_v2:25.4R1.12-dnlab"),
    ("juniper_vjunosswitch", "/opt/img_dnlab/vJunos-switch-25.4R1.12.qcow2", "vrnetlab/juniper_vjunos-switch_v2:25.4R1.12-dnlab"),
    ("juniper_vjunosevolved", "/opt/img_dnlab/vJunosEvolved-25.4R1.13-EVO.qcow2", "vrnetlab/juniper_vjunosevolved_v2:25.4R1.13-EVO-dnlab"),
    ("cisco_n9kv", "/opt/img_dnlab/n9kv-9300-10.5.5.M.qcow2", "vrnetlab/cisco_n9kv_v2:9300-10.5.5.M-dnlab"),
    ("cisco_n9kv", "/opt/img_dnlab/n9kv-9500-10.5.5.M.qcow2", "vrnetlab/cisco_n9kv_v2:9500-10.5.5.M-dnlab"),
    ("cisco_cat9kv", "/opt/img_dnlab/cat9000v-q200-17-15-03/cat9kv_prd.17.15.03.qcow2", "vrnetlab/cisco_cat9kv_v2:17.15.03-dnlab"),
    ("cisco_c9800cl", "/opt/img_dnlab/C9800-CL-universalk9.17.15.05.qcow2", "vrnetlab/cisco_c9800cl_v2:17.15.05-dnlab"),
    ("cisco_xrv9k", "/opt/img_dnlab/xrv9k-fullk9-x-25.2.2.qcow2", "vrnetlab/cisco_xrv9k_v2:25.2.2-dnlab"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers)) as response:
        return json.load(response)


def inspect_image(image: str) -> dict:
    raw = subprocess.run(
        ["docker", "image", "inspect", image], check=True, text=True,
        stdout=subprocess.PIPE,
    ).stdout
    data = json.loads(raw)[0]
    labels = data["Config"].get("Labels") or {}
    if labels.get("org.dnlab.capabilities") != "warm-links-v1":
        raise RuntimeError(f"{image}: missing warm-links-v1 label")
    return {"id": data["Id"], "labels": labels}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://198.18.2.2:8082")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--report", default="/tmp/dnlab-image-rebuild.json")
    args = parser.parse_args()
    report_path = Path(args.report)
    results: list[dict] = []
    if args.start and report_path.is_file():
        results = [
            item for item in json.loads(report_path.read_text())
            if item.get("job_status") == "success"
        ]

    for number, (kind, source_name, image) in enumerate(INVENTORY[args.start:], args.start + 1):
        source = Path(source_name)
        before = sha256(source)
        print(f"[{number}/{len(INVENTORY)}] {kind}: upload {source.name} sha256={before}", flush=True)
        upload = json.loads(subprocess.run(
            ["curl", "--silent", "--show-error", "--form", f"file=@{source}", f"{args.api}/uploads"],
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout)
        job = request_json(f"{args.api}/jobs", {"kind": kind, "source_path": upload["source_path"]})
        print(f"[{number}/{len(INVENTORY)}] job={job['id']} running", flush=True)
        while True:
            job = request_json(f"{args.api}/jobs/{job['id']}")
            if job["status"] in {"success", "failed"}:
                break
            print(f"[{number}/{len(INVENTORY)}] {job['status']}", flush=True)
            time.sleep(20)
        after = sha256(source)
        result = {
            "kind": kind, "source": str(source), "source_sha256_before": before,
            "source_sha256_after": after, "source_unchanged": before == after,
            "image": image, "job_id": job["id"], "job_status": job["status"],
            "job_log_tail": job["log"][-40:],
        }
        if job["status"] != "success":
            results.append(result)
            report_path.write_text(json.dumps(results, indent=2) + "\n")
            raise RuntimeError(f"{kind} build failed: {job['id']}")
        result.update(inspect_image(image))
        results.append(result)
        report_path.write_text(json.dumps(results, indent=2) + "\n")
        print(f"[{number}/{len(INVENTORY)}] success image={image} id={result['id']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
