"""Resource extraction from vrnetlab Docker images."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from dnlab_multinode.models.schedule import VDResources
from dnlab_multinode.models.topology import VDNode

log = logging.getLogger(__name__)

_CACHE_FILE = ".image-cache.json"

# Defaults when extraction fails
_DEFAULT_CPU = 2
_DEFAULT_RAM_MB = 4096


class ResourceError(Exception):
    pass


def extract_resources(
    images: dict[str, str],
    cache_dir: Path = Path("."),
    no_cache: bool = False,
    nodes: dict[str, VDNode] | None = None,
    resource_specs: dict[str, dict] | None = None,
) -> dict[str, VDResources]:
    """Extract CPU/RAM requirements for each VD image.

    Args:
        images: {node_name: image_name} mapping
        cache_dir: directory for .image-cache.json
        no_cache: if True, ignore cache and re-extract

    Returns: {node_name: VDResources}
    """
    cache = _load_cache(cache_dir) if not no_cache else {}
    results: dict[str, VDResources] = {}

    # Deduplicate: multiple nodes may use the same image
    unique_images = set(images.values())

    for image in unique_images:
        if image in cache and not no_cache:
            log.debug("Cache hit: %s → %d CPU, %d MB", image, cache[image]["cpu"], cache[image]["ram_mb"])
            continue

        log.info("Extracting resources from image: %s", image)
        cpu, ram_mb = _extract_from_launch_py(image)
        cache[image] = {
            "cpu": cpu,
            "ram_mb": ram_mb,
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
        }

    _save_cache(cache, cache_dir)

    for node_name, image in images.items():
        entry = cache.get(image, {"cpu": _DEFAULT_CPU, "ram_mb": _DEFAULT_RAM_MB})
        resource = VDResources(
            name=node_name,
            image=image,
            cpu=entry["cpu"],
            ram_mb=entry["ram_mb"],
            cpu_source=f"image:{image}",
            ram_mb_source=f"image:{image}",
        )
        node = (nodes or {}).get(node_name)
        spec = (resource_specs or {}).get(node_name)
        if node and spec:
            resource = _apply_resource_spec(resource, node, spec)
        results[node_name] = resource

    return results


def _apply_resource_spec(
    base: VDResources,
    node: VDNode,
    spec: dict,
) -> VDResources:
    """Resolve effective resources from a data-driven per-node schema.

    The schema describes where each resource comes from; this module
    does not know vendor-specific env var names. Supported source
    containers are intentionally generic node data locations:

    * ``env``   → ``node.env[<key>]``
    * ``extra`` → ``node.extra[<key>]`` or dotted nested paths
    * ``node``  → direct ``VDNode`` attribute
    """
    cpu, cpu_source = _resolve_resource_field(
        "cpu", spec.get("cpu"), node, base.cpu, base.cpu_source,
    )
    ram_mb, ram_source = _resolve_resource_field(
        "ram_mb", spec.get("ram_mb"), node, base.ram_mb, base.ram_mb_source,
    )
    return VDResources(
        name=base.name,
        image=base.image,
        cpu=cpu,
        ram_mb=ram_mb,
        cpu_source=cpu_source,
        ram_mb_source=ram_source,
    )


def _resolve_resource_field(
    field: str,
    field_spec,
    node: VDNode,
    fallback: int,
    fallback_source: str,
) -> tuple[int, str]:
    if not isinstance(field_spec, dict):
        return fallback, fallback_source

    source = str(field_spec.get("source") or "")
    key = field_spec.get("key")
    if not source or not key:
        return fallback, fallback_source

    raw = _read_node_value(node, source, str(key))
    if raw is None or raw == "":
        if "default" in field_spec:
            raw = field_spec.get("default")
        else:
            return fallback, fallback_source

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ResourceError(
            f"Invalid resource value for {node.name}.{field} from {source}:{key}: {raw!r}"
        ) from exc
    if value <= 0:
        raise ResourceError(
            f"Invalid resource value for {node.name}.{field} from {source}:{key}: {value}"
        )
    return value, f"{source}:{key}"


def _read_node_value(node: VDNode, source: str, key: str):
    if source == "env":
        return node.env.get(key)
    if source == "extra":
        return _read_mapping_path(node.extra, key)
    if source == "node":
        return getattr(node, key, None)
    return None


def _read_mapping_path(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _extract_from_launch_py(image: str) -> tuple[int, int]:
    """Run docker to extract launch.py and parse RAM/SMP from it."""
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "cat", image, "/launch.py"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log.warning("Cannot extract launch.py from %s: %s", image, result.stderr.strip())
            return _DEFAULT_CPU, _DEFAULT_RAM_MB

        return _parse_launch_py(result.stdout)
    except subprocess.TimeoutExpired:
        log.warning("Timeout extracting launch.py from %s", image)
        return _DEFAULT_CPU, _DEFAULT_RAM_MB
    except Exception as e:
        log.warning("Error extracting from %s: %s", image, e)
        return _DEFAULT_CPU, _DEFAULT_RAM_MB


def _parse_launch_py(source: str) -> tuple[int, int]:
    """Parse ram= and smp= from launch.py source code.

    Returns: (cpu, ram_mb)
    """
    cpu = _DEFAULT_CPU
    ram_mb = _DEFAULT_RAM_MB

    # Find ram=<int> in super().__init__(...) or self.ram = <int>
    ram_patterns = [
        r'ram\s*=\s*(\d+)',
    ]
    for pat in ram_patterns:
        m = re.search(pat, source)
        if m:
            ram_mb = int(m.group(1))
            log.debug("Parsed ram=%d", ram_mb)
            break

    # Find smp=<value>
    # Pattern 1: smp="4,sockets=1,cores=4,threads=1"
    m = re.search(r'smp\s*=\s*"(\d+)', source)
    if m:
        cpu = int(m.group(1))
        log.debug("Parsed smp (string)=%d", cpu)
    else:
        # Pattern 2: smp=4
        m = re.search(r'smp\s*=\s*(\d+)', source)
        if m:
            cpu = int(m.group(1))
            log.debug("Parsed smp (int)=%d", cpu)

    return cpu, ram_mb


def check_images_on_hosts(images: set[str], ssh_clients: dict) -> dict[str, list[str]]:
    """Check that all images exist on all hosts.

    Args:
        images: set of image names to check
        ssh_clients: {host_name: SSHClient} (must be connected)

    Returns: {image: [hosts_where_missing]}
    """
    missing: dict[str, list[str]] = {}

    for image in sorted(images):
        for host_name, client in ssh_clients.items():
            rc, _, _ = client.run_no_check(
                f"docker image inspect {image} --format '{{{{.Id}}}}'"
            )
            if rc != 0:
                missing.setdefault(image, []).append(host_name)
                log.warning("Image %s not found on %s", image, host_name)

    return missing


def sync_image_to_host(image: str, ssh_client, master_client=None) -> bool:
    """Sync an image from the master to a remote host via docker save/load.

    Uses: docker save <image> | ssh <host> docker load
    """
    host = ssh_client.host
    log.info("Syncing image %s → %s", image, host)

    try:
        # docker save on master, pipe via SSH to docker load on remote
        # This runs locally on the master, piping to the remote
        cmd = f"docker save {image} | ssh -o StrictHostKeyChecking=no {ssh_client.user}@{host} docker load"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            log.error("Image sync failed for %s → %s: %s", image, host, result.stderr.strip())
            return False

        log.info("Image synced: %s → %s", image, host)
        return True
    except Exception as e:
        log.error("Image sync error %s → %s: %s", image, host, e)
        return False


# ── Cache I/O ──────────────────────────────────────────────────────────

def _load_cache(cache_dir: Path) -> dict:
    cache_file = cache_dir / _CACHE_FILE
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict, cache_dir: Path) -> None:
    cache_file = cache_dir / _CACHE_FILE
    try:
        cache_file.write_text(json.dumps(cache, indent=2))
    except OSError as e:
        log.warning("Cannot save image cache: %s", e)
