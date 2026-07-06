"""RealNet BGP admin config and per-router allocation helpers."""

from __future__ import annotations

import ipaddress
import logging
import random
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
import httpx

from app.config import settings
from app.models.topology import Topology
from app.services.containerlab_service import ContainerLabService
from app.services.paths import PATHS


log = logging.getLogger(__name__)


PRIVATE_AS_RANGES = (
    (64512, 65534),
    (4200000000, 4294967294),
)


class RealNetBgpError(ValueError):
    pass


@dataclass(frozen=True)
class RealNetBgpConfig:
    rr_as: int = 64512
    rr_ip: str = ""
    host_net: str = ""
    router_as_pool: str = "64513-65534"
    router_ip_pool: str = ""
    realnet_network_pool: str = "100.64.0.0/10"
    rr_password: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rr_as": self.rr_as,
            "rr_ip": self.rr_ip,
            "host_net": self.host_net,
            "router_as_pool": self.router_as_pool,
            "router_ip_pool": self.router_ip_pool,
            "realnet_network_pool": self.realnet_network_pool,
            "rr_password": self.rr_password,
        }


def normalize_config(raw: dict[str, Any] | None, *, require_endpoint: bool = True) -> RealNetBgpConfig:
    raw = raw or {}
    cfg = RealNetBgpConfig(
        rr_as=_as_int(raw.get("rr_as") or raw.get("bgp_as") or RealNetBgpConfig.rr_as, "RR AS"),
        rr_ip=str(raw.get("rr_ip") or "").strip(),
        host_net=str(raw.get("host_net") or "").strip(),
        router_as_pool=str(raw.get("router_as_pool") or raw.get("lab_as_pool") or RealNetBgpConfig.router_as_pool).strip(),
        router_ip_pool=str(raw.get("router_ip_pool") or "").strip(),
        realnet_network_pool=str(raw.get("realnet_network_pool") or RealNetBgpConfig.realnet_network_pool).strip(),
        rr_password=str(raw.get("rr_password") or "").strip(),
    )
    validate_config(cfg, require_endpoint=require_endpoint)
    return cfg


def validate_config(cfg: RealNetBgpConfig, *, require_endpoint: bool = True) -> None:
    _validate_private_as(cfg.rr_as, "RR AS")
    _parse_as_pool(cfg.router_as_pool)
    if require_endpoint or cfg.rr_ip or cfg.host_net:
        if not cfg.rr_ip:
            raise RealNetBgpError("RR IP is required")
        if not cfg.host_net:
            raise RealNetBgpError("Host network is required")
        try:
            rr_ip = ipaddress.ip_address(cfg.rr_ip)
            host_net = ipaddress.ip_network(cfg.host_net, strict=False)
        except ValueError as exc:
            raise RealNetBgpError(f"Invalid RR IP or host network: {exc}") from exc
        if rr_ip not in host_net:
            raise RealNetBgpError("RR IP must belong to Host network")
    if cfg.router_ip_pool:
        _parse_ip_pool(cfg.router_ip_pool)
    _parse_realnet_network_pool(cfg.realnet_network_pool)
    if cfg.rr_password:
        _validate_bgp_password(cfg.rr_password, "RR BGP password")


def config_from_hosts_model(hosts_model) -> RealNetBgpConfig:
    infra = hosts_model.data.extra_infrastructure or {}
    realnet = infra.get("realnet") or {}
    if not isinstance(realnet, dict):
        realnet = {}
    return normalize_config(realnet)


def realnet_bgp_status(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return read-only BGP readiness for UI/API consumers."""
    try:
        cfg = normalize_config(raw, require_endpoint=False)
    except RealNetBgpError as exc:
        return {"configured": False, "skipped": True, "reason": str(exc)}
    if not cfg.rr_ip or not cfg.host_net:
        return {
            "configured": False,
            "skipped": True,
            "reason": "RealNet BGP RR IP/Host network not configured",
        }
    try:
        validate_config(cfg, require_endpoint=True)
    except RealNetBgpError as exc:
        return {"configured": False, "skipped": True, "reason": str(exc)}
    return {"configured": True, "skipped": False, "reason": ""}


def update_hosts_model_realnet_bgp(hosts_model, payload: dict[str, Any]):
    hosts_model.data.extra_infrastructure = dict(hosts_model.data.extra_infrastructure or {})
    realnet = dict(hosts_model.data.extra_infrastructure.get("realnet") or {})
    payload = dict(payload or {})
    if not payload.get("rr_password"):
        payload["rr_password"] = realnet.get("rr_password") or generate_bgp_password()
    cfg = normalize_config(payload)
    realnet.update(cfg.as_dict())
    hosts_model.data.extra_infrastructure["realnet"] = realnet
    return hosts_model, cfg


def generate_bgp_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def rr_as_from_hosts() -> int:
    path = Path(settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
    if not path.exists():
        return RealNetBgpConfig.rr_as
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    infra = raw.get("infrastructure") or {}
    realnet = infra.get("realnet") or {}
    if not isinstance(realnet, dict):
        return RealNetBgpConfig.rr_as
    return _as_int(realnet.get("rr_as") or realnet.get("bgp_as") or RealNetBgpConfig.rr_as, "RR AS")


def ensure_route_reflector_from_hosts() -> dict[str, Any]:
    """Ensure the global RealNet route reflector container on the master.

    This is infrastructure-level, not lab-level. Missing/partial BGP config is
    reported as skipped so GUI startup can continue.
    """
    hosts_path = Path(settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
    if settings.DNLAB_MULTINODE_API_URL:
        return _ensure_route_reflector_via_api(hosts_path)
    from types import SimpleNamespace

    from dnlab_multinode.models.topology import RealNetInfraCfg
    from dnlab_multinode.services.hosts_config import load_hosts_config
    from dnlab_multinode.services.ssh import SSHClient
    from dnlab_multinode.services import realnet as realnet_svc

    hosts = load_hosts_config(hosts_path)
    rn = hosts.realnet
    if not rn.rr_ip or not rn.host_net:
        return {
            "ok": False,
            "skipped": True,
            "reason": "RealNet BGP RR IP/Host network not configured",
        }
    infra = RealNetInfraCfg(
        network=rn.network,
        bridge=rn.bridge,
        ipv4_subnet=rn.ipv4_subnet,
        ipv4_gw=rn.ipv4_gw,
        image=rn.image,
        wan_iface=rn.wan_iface,
        rr_as=rn.rr_as,
        rr_ip=rn.rr_ip,
        host_net=rn.host_net,
        router_as_pool=rn.router_as_pool,
        router_ip_pool=rn.router_ip_pool,
        realnet_network_pool=rn.realnet_network_pool,
        rr_password=rn.rr_password,
    )
    topo = SimpleNamespace(realnet_infra=infra)
    client = SSHClient(
        host=hosts.master.host,
        user=hosts.master.ssh_user,
        key_path=hosts.master.ssh_key,
        name=hosts.master.name,
    )
    client.connect()
    try:
        realnet_svc.deploy_route_reflector(topo, client)
        status = route_reflector_status(client)
        return {"ok": True, "skipped": False, **status}
    finally:
        client.close()


def route_reflector_status(client=None) -> dict[str, Any]:
    if client is None and settings.DNLAB_MULTINODE_API_URL:
        return _route_reflector_status_via_api(
            Path(settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
        )

    from dnlab_multinode.services.hosts_config import load_hosts_config
    from dnlab_multinode.services.ssh import SSHClient

    owns_client = client is None
    if client is None:
        hosts = load_hosts_config(settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
        client = SSHClient(
            host=hosts.master.host,
            user=hosts.master.ssh_user,
            key_path=hosts.master.ssh_key,
            name=hosts.master.name,
        )
        client.connect()
    try:
        rc, out, _ = client.run_no_check(
            "docker inspect -f '{{.State.Running}} {{.Config.Image}}' dnlab-realnet-rr"
        )
        if rc != 0:
            return {"running": False, "container": "dnlab-realnet-rr", "image": ""}
        parts = (out or "").split(maxsplit=1)
        return {
            "running": bool(parts and parts[0] == "true"),
            "container": "dnlab-realnet-rr",
            "image": parts[1] if len(parts) > 1 else "",
        }
    finally:
        if owns_client:
            client.close()


async def ensure_route_reflector_on_startup() -> None:
    import asyncio

    try:
        result = await asyncio.to_thread(ensure_route_reflector_from_hosts)
        if result.get("ok"):
            log.info("realnet-rr ensured at startup: %s", result)
        else:
            log.warning("realnet-rr startup ensure skipped/failed: %s", result)
    except Exception as exc:
        log.warning("realnet-rr startup ensure failed: %s", exc)


def _ensure_route_reflector_via_api(hosts_path: Path) -> dict[str, Any]:
    url = f"{settings.DNLAB_MULTINODE_API_URL}/realnet/rr/reconcile"
    payload = {"hosts_file": str(hosts_path)}
    try:
        response = httpx.post(url, json=payload, timeout=None)
    except httpx.HTTPError as exc:
        raise RealNetBgpError(f"multinode RealNet RR API request failed: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise RealNetBgpError(str(detail or f"multinode RealNet RR API returned HTTP {response.status_code}"))
    try:
        data = response.json()
    except ValueError as exc:
        raise RealNetBgpError("multinode RealNet RR API returned non-JSON response") from exc
    return data if isinstance(data, dict) else {"result": data}


def _route_reflector_status_via_api(hosts_path: Path) -> dict[str, Any]:
    url = f"{settings.DNLAB_MULTINODE_API_URL}/realnet/rr/status"
    payload = {"hosts_file": str(hosts_path)}
    try:
        response = httpx.post(url, json=payload, timeout=None)
    except httpx.HTTPError as exc:
        raise RealNetBgpError(f"multinode RealNet RR status API request failed: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise RealNetBgpError(str(detail or f"multinode RealNet RR status API returned HTTP {response.status_code}"))
    try:
        data = response.json()
    except ValueError as exc:
        raise RealNetBgpError("multinode RealNet RR status API returned non-JSON response") from exc
    return data if isinstance(data, dict) else {"result": data}


def allocate_for_realnet_node(node_extra: dict[str, Any], *, current_path: Path) -> dict[str, Any]:
    """Return node extra with BGP AS/IP allocated when bgp is enabled."""
    extra = dict(node_extra or {})
    if not bool(extra.get("bgp")):
        return extra
    cfg = _load_hosts_realnet_bgp_config()
    used_as, used_ip = _used_realnet_bgp_allocations(current_path=current_path, current_extra=extra)
    if not extra.get("bgp_as"):
        extra["bgp_as"] = _first_free_as(cfg.router_as_pool, used_as)
    else:
        asn = _as_int(extra["bgp_as"], "router BGP AS")
        _validate_private_as(asn, "router BGP AS")
        extra["bgp_as"] = asn
    if cfg.router_ip_pool:
        if not extra.get("bgp_router_ip"):
            extra["bgp_router_ip"] = _first_free_ip(cfg.router_ip_pool, used_ip)
        else:
            ip = str(ipaddress.ip_address(str(extra["bgp_router_ip"]).strip()))
            extra["bgp_router_ip"] = ip
    if not extra.get("bgp_password"):
        extra["bgp_password"] = generate_bgp_password()
    else:
        _validate_bgp_password(str(extra["bgp_password"]), "router BGP password")
    return extra


def ensure_rr_password_in_hosts(hosts_path: Path | None = None) -> str:
    """Ensure infrastructure.realnet.rr_password exists in hosts.yml."""
    path = Path(hosts_path or settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(raw, dict):
        raw = {}
    infra = raw.setdefault("infrastructure", {})
    if not isinstance(infra, dict):
        infra = {}
        raw["infrastructure"] = infra
    realnet = infra.setdefault("realnet", {})
    if not isinstance(realnet, dict):
        realnet = {}
        infra["realnet"] = realnet
    password = str(realnet.get("rr_password") or "").strip()
    if password:
        _validate_bgp_password(password, "RR BGP password")
        return password
    password = generate_bgp_password()
    realnet["rr_password"] = password
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return password


def normalize_realnet_lan(node_extra: dict[str, Any], *, current_path: Path) -> dict[str, Any]:
    """Normalize/allocate the RealNet LAN network and gateway.

    If both fields are blank, a /24 is picked from the admin pool. If the
    supplied values are explicit, they are validated and preserved.
    """
    extra = dict(node_extra or {})
    pool = _parse_realnet_network_pool(_load_realnet_network_pool())
    requested_network = str(extra.get("network") or "").strip()
    requested_gw = str(extra.get("ipv4") or "").strip()
    if requested_network or requested_gw:
        if requested_network and not requested_gw:
            network = _parse_lan_network(requested_network)
            if network.prefixlen > 30:
                raise RealNetBgpError(
                    "RealNet LAN network is too small for an automatic gateway. "
                    "Set both values or leave them empty so dNLab can assign them."
                )
            gateway = ipaddress.ip_interface(f"{network.network_address + 1}/{network.prefixlen}")
        elif requested_gw and not requested_network:
            if "/" not in requested_gw:
                raise RealNetBgpError(
                    "RealNet LAN network is required when the gateway has no prefix. "
                    "Set both values or leave them empty so dNLab can assign them."
                )
            try:
                gateway = ipaddress.ip_interface(requested_gw)
            except ValueError as exc:
                raise RealNetBgpError(f"Invalid RealNet LAN gateway: {exc}") from exc
            network = gateway.network
            if network.version != 4:
                raise RealNetBgpError("RealNet LAN network must be IPv4")
            network, gateway = _parse_lan_pair(str(network), str(gateway))
        else:
            network, gateway = _parse_lan_pair(requested_network, requested_gw)
        overlaps = _used_realnet_lan_networks(current_path=current_path, current_extra=extra)
        if any(network.overlaps(other) for other in overlaps):
            raise RealNetBgpError(
                "RealNet LAN network overlaps an existing RealNet network. "
                "Change network/gateway or leave them empty so dNLab can assign them."
            )
    else:
        network = _first_free_realnet_lan(pool, current_path=current_path, current_extra=extra)
        gateway = ipaddress.ip_interface(f"{network.network_address + 1}/{network.prefixlen}")
    extra["network"] = str(network)
    extra["ipv4"] = f"{gateway.ip}/{network.prefixlen}"
    return extra


def importable_realnet_routers(current_lab_id: str, labs: list[Any]) -> list[dict[str, Any]]:
    clab = ContainerLabService()
    out: list[dict[str, Any]] = []
    for lab in labs:
        lab_id = str(lab.id)
        if lab_id == current_lab_id:
            continue
        path = settings.TOPOLOGIES_DIR / f"{lab_id}.yml"
        if not path.exists():
            continue
        try:
            topo = clab.load_topology_from_file(path)
        except Exception:
            continue
        router = next((n for n in topo.nodes if n.kind == "_real_net" and bool((n.extra or {}).get("bgp"))), None)
        if not router:
            continue
        extra = router.extra or {}
        if not extra.get("bgp_as") or not extra.get("bgp_router_ip"):
            continue
        out.append({
            "lab_id": lab_id,
            "lab_name": lab.name,
            "owner_username": lab.owner.username if getattr(lab, "owner", None) else None,
            "owner_role": getattr(getattr(getattr(lab, "owner", None), "role", None), "value", None),
            "realnet_node": router.name,
            "bgp_as": int(extra["bgp_as"]),
            "bgp_router_ip": str(extra["bgp_router_ip"]),
        })
    return out


def scrub_realnet_passwords(topology_dump: dict[str, Any], *, can_view_password: bool) -> dict[str, Any]:
    """Remove per-router BGP password from topology payloads when not allowed."""
    if can_view_password:
        return topology_dump
    for node in topology_dump.get("nodes") or []:
        if not isinstance(node, dict) or node.get("kind") != "_real_net":
            continue
        extra = node.get("extra")
        if isinstance(extra, dict):
            extra.pop("bgp_password", None)
    return topology_dump


def ensure_single_realnet(topo: Topology, *, new_node: str | None = None) -> None:
    names = [n.name for n in topo.nodes if n.kind == "_real_net"]
    if new_node:
        names.append(new_node)
    if len(set(names)) > 1:
        raise RealNetBgpError("Only one realnet-router is allowed per lab")


def _load_hosts_realnet_bgp_config() -> RealNetBgpConfig:
    path = Path(settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    infra = (raw or {}).get("infrastructure") or {}
    realnet = infra.get("realnet") or {}
    try:
        return normalize_config(realnet)
    except RealNetBgpError as exc:
        raise RealNetBgpError(
            f"Configure Admin > RealNet BGP before enabling BGP mode: {exc}"
        ) from exc


def _load_realnet_network_pool() -> str:
    path = Path(settings.DNLAB_MULTINODE_HOSTS or PATHS.hosts_file)
    if not path.exists():
        return RealNetBgpConfig.realnet_network_pool
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    infra = raw.get("infrastructure") or {}
    realnet = infra.get("realnet") or {}
    if not isinstance(realnet, dict):
        return RealNetBgpConfig.realnet_network_pool
    return str(realnet.get("realnet_network_pool") or RealNetBgpConfig.realnet_network_pool).strip()


def _used_realnet_bgp_allocations(*, current_path: Path, current_extra: dict[str, Any]) -> tuple[set[int], set[str]]:
    clab = ContainerLabService()
    used_as: set[int] = set()
    used_ip: set[str] = set()
    current_as = current_extra.get("bgp_as")
    current_ip = current_extra.get("bgp_router_ip")
    for path in settings.TOPOLOGIES_DIR.glob("*.yml"):
        try:
            topo = clab.load_topology_from_file(path)
        except Exception:
            continue
        for node in topo.nodes:
            if node.kind != "_real_net":
                continue
            extra = node.extra or {}
            if not extra.get("bgp"):
                continue
            if path == current_path and extra.get("bgp_as") == current_as and extra.get("bgp_router_ip") == current_ip:
                continue
            if extra.get("bgp_as"):
                try:
                    used_as.add(int(extra["bgp_as"]))
                except (TypeError, ValueError):
                    pass
            if extra.get("bgp_router_ip"):
                try:
                    used_ip.add(str(ipaddress.ip_address(str(extra["bgp_router_ip"]))))
                except ValueError:
                    pass
    return used_as, used_ip


def _used_realnet_lan_networks(*, current_path: Path, current_extra: dict[str, Any]) -> list[ipaddress.IPv4Network]:
    clab = ContainerLabService()
    used: list[ipaddress.IPv4Network] = []
    current_network = str(current_extra.get("network") or "")
    current_ipv4 = str(current_extra.get("ipv4") or "")
    for path in settings.TOPOLOGIES_DIR.glob("*.yml"):
        try:
            topo = clab.load_topology_from_file(path)
        except Exception:
            continue
        for node in topo.nodes:
            if node.kind != "_real_net":
                continue
            extra = node.extra or {}
            if path == current_path and (
                str(extra.get("network") or "") == current_network
                or str(extra.get("ipv4") or "") == current_ipv4
            ):
                continue
            try:
                used.append(_network_from_lan_extra(extra))
            except RealNetBgpError:
                continue
    return used


def _network_from_lan_extra(extra: dict[str, Any]) -> ipaddress.IPv4Network:
    raw_net = str(extra.get("network") or "").strip()
    if raw_net:
        try:
            network = ipaddress.ip_network(raw_net, strict=False)
        except ValueError as exc:
            raise RealNetBgpError(f"Invalid RealNet LAN network: {exc}") from exc
        if network.version != 4:
            raise RealNetBgpError("RealNet LAN network must be IPv4")
        return network
    raw_gw = str(extra.get("ipv4") or "").strip()
    if raw_gw and "/" in raw_gw:
        try:
            return ipaddress.ip_interface(raw_gw).network
        except ValueError as exc:
            raise RealNetBgpError(f"Invalid RealNet LAN gateway: {exc}") from exc
    raise RealNetBgpError("RealNet LAN network/gateway missing")


def _parse_lan_pair(network_value: str, gateway_value: str) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Interface]:
    network = _parse_lan_network(network_value)
    try:
        gateway = ipaddress.ip_interface(gateway_value if "/" in gateway_value else f"{gateway_value}/{network.prefixlen}")
    except ValueError as exc:
        raise RealNetBgpError(f"Invalid RealNet LAN gateway: {exc}") from exc
    if gateway.version != 4:
        raise RealNetBgpError("RealNet LAN gateway must be IPv4")
    if gateway.ip not in network:
        raise RealNetBgpError(
            "RealNet LAN gateway must belong to the RealNet LAN network. "
            "Change network/gateway or leave them empty so dNLab can assign them."
        )
    if gateway.ip in (network.network_address, network.broadcast_address):
        raise RealNetBgpError(
            "RealNet LAN gateway cannot be the network or broadcast address. "
            "Change network/gateway or leave them empty so dNLab can assign them."
        )
    return network, ipaddress.ip_interface(f"{gateway.ip}/{network.prefixlen}")


def _parse_lan_network(network_value: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(network_value, strict=False)
    except ValueError as exc:
        raise RealNetBgpError(f"Invalid RealNet LAN network: {exc}") from exc
    if network.version != 4:
        raise RealNetBgpError("RealNet LAN network must be IPv4")
    return network


def _parse_realnet_network_pool(value: str) -> ipaddress.IPv4Network:
    try:
        pool = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError as exc:
        raise RealNetBgpError(f"RealNet node network pool invalid: {exc}") from exc
    if pool.version != 4:
        raise RealNetBgpError("RealNet node network pool must be IPv4")
    if pool.prefixlen > 24:
        raise RealNetBgpError("RealNet node network pool must contain at least one /24")
    return pool


def _first_free_realnet_lan(
    pool: ipaddress.IPv4Network,
    *,
    current_path: Path,
    current_extra: dict[str, Any],
) -> ipaddress.IPv4Network:
    used = _used_realnet_lan_networks(current_path=current_path, current_extra=current_extra)
    subnets = list(pool.subnets(new_prefix=24)) if pool.prefixlen < 24 else [pool]
    random.shuffle(subnets)
    for subnet in subnets:
        if not any(subnet.overlaps(other) for other in used):
            return subnet
    raise RealNetBgpError("RealNet node network pool exhausted")


def _parse_as_pool(value: str) -> tuple[int, int]:
    try:
        low_s, high_s = str(value).split("-", 1)
        low, high = int(low_s), int(high_s)
    except Exception as exc:
        raise RealNetBgpError("Router AS pool must use '<low>-<high>'") from exc
    if low > high:
        raise RealNetBgpError("Router AS pool low value must be <= high value")
    _validate_private_as(low, "router AS pool low")
    _validate_private_as(high, "router AS pool high")
    if not any(low >= a and high <= b for a, b in PRIVATE_AS_RANGES):
        raise RealNetBgpError("Router AS pool cannot span different private AS ranges")
    return low, high


def _parse_ip_pool(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if "/" in raw:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError as exc:
            raise RealNetBgpError(f"Invalid Router IP pool: {exc}") from exc
        return [str(ip) for ip in net.hosts()]
    try:
        low_s, high_s = raw.split("-", 1)
        low, high = ipaddress.ip_address(low_s.strip()), ipaddress.ip_address(high_s.strip())
    except Exception as exc:
        raise RealNetBgpError("Router IP pool must use CIDR or '<low>-<high>'") from exc
    if low.version != high.version or int(low) > int(high):
        raise RealNetBgpError("Router IP pool range is invalid")
    return [str(ipaddress.ip_address(i)) for i in range(int(low), int(high) + 1)]


def _first_free_as(pool: str, used: set[int]) -> int:
    low, high = _parse_as_pool(pool)
    for asn in range(low, high + 1):
        if asn not in used:
            return asn
    raise RealNetBgpError("Router AS pool exhausted")


def _first_free_ip(pool: str, used: set[str]) -> str:
    for ip in _parse_ip_pool(pool):
        if ip not in used:
            return ip
    raise RealNetBgpError("Router IP pool exhausted")


def _as_int(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RealNetBgpError(f"{label} must be an integer") from exc


def _validate_private_as(asn: int, label: str) -> None:
    if not any(low <= asn <= high for low, high in PRIVATE_AS_RANGES):
        raise RealNetBgpError(f"{label} must be in private BGP AS ranges")


def _validate_bgp_password(value: str, label: str) -> None:
    if not (1 <= len(value) <= 80):
        raise RealNetBgpError(f"{label} must be 1-80 characters")
    if any(ch.isspace() for ch in value):
        raise RealNetBgpError(f"{label} cannot contain whitespace")
