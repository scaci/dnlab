"""Global host inventory loader.

The physical hosts (master + workers), their SSH credentials, the shared
mgmt defaults / image-sync filter, and the **shared jumphost network**
live in a single site-wide file — by default
``/etc/dnlab/hosts.yml`` — so that a topology file can be pure
ContainerLab YAML and stay portable across sites.

The jumphost network is shared infrastructure: one Docker network on the
master hosts all lab jumphosts. Per-lab the topology only specifies the
jumphost image (for per-lab image overrides); the IP is auto-assigned
from the pool at deploy time.

File schema::

    infrastructure:
      master:
        host: 10.0.0.1
        ssh_user: root
        ssh_key: ~/.ssh/id_ed25519
      workers:
        worker1:
          host: 10.0.0.2
          ssh_user: root
          ssh_key: ~/.ssh/id_ed25519
      underlay_iface: eth0
      jumphost_net:                     # shared across all labs
        network: dnlab-jumphost
        bridge: br-dnlab-jh
        ipv4_subnet: 192.168.100.0/24
        ipv4_gw: 192.168.100.1

    defaults:
      mgmt:
        ipv4_subnet: 172.20.0.0/24
        ipv4_gw: 172.20.0.1

    image_sync:          # optional, used by the image-sync daemon
      enabled: true
      include: ["vrnetlab/*", "dnlab/runtime-relay", "dnlab/mgmt-anchor"]
      exclude: ["dnlab/jumphost", "dnlab/dns",
                "postgres", "<none>:<none>"]
      interval_seconds: 300

The location can be overridden via the ``DNLAB_MULTINODE_HOSTS``
environment variable, or by passing an explicit path to
:func:`load_hosts_config`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dnlab_multinode.models.topology import (
    CephFSConfig, InfraHost, JumphostConfig, PersistenceConfig,
)
from dnlab_multinode.services.images import image_for
from dnlab_multinode.services.paths import PATHS

log = logging.getLogger(__name__)


DEFAULT_HOSTS_FILE = PATHS.hosts_file
ENV_VAR = "DNLAB_MULTINODE_HOSTS"


class HostsConfigError(Exception):
    pass


@dataclass
class MgmtDefaults:
    ipv4_subnet: str = "172.20.0.0/24"
    ipv4_gw: str = "172.20.0.1"


@dataclass
class JumphostNetConfig:
    """Shared jumphost docker network (master-local)."""
    network: str = "dnlab-jumphost"
    bridge: str = "br-dnlab-jh"
    ipv4_subnet: str = "192.168.100.0/24"
    ipv4_gw: str = "192.168.100.1"
    # Host-side port publishing for per-lab SSH access.
    # `ssh_port_range` is an inclusive "<low>-<high>" range; one port is
    # allocated per lab at deploy time. `ssh_bind_ip` is the master-side
    # IP the port is published on ("0.0.0.0" = all interfaces).
    ssh_port_range: str = "2200-2299"
    ssh_bind_ip: str = "0.0.0.0"


@dataclass
class WebUIPortsConfig:
    """Pool host-side per le porte Web UI dei VD pubblicate via clab.

    Allocazione dinamica, sticky cross-deploy via ``LabState``. Pool
    disgiunto da ``JumphostNetConfig.ssh_port_range`` (validation a
    carico del loader).
    """
    port_range: str = "8443-8999"
    bind_ip: str = "127.0.0.1"


@dataclass
class RealNetConfig:
    """Shared WAN network for unmanaged per-lab real_net routers."""
    network: str = "dnlab-realnet"
    bridge: str = "br-dnlab-rn"
    ipv4_subnet: str = "192.168.101.0/24"
    ipv4_gw: str = "192.168.101.1"
    image: str = field(default_factory=lambda: image_for("realnet-router"))
    wan_iface: str = ""
    rr_as: int = 64512
    rr_ip: str = ""
    host_net: str = ""
    router_as_pool: str = "64513-65534"
    router_ip_pool: str = ""
    realnet_network_pool: str = "100.64.0.0/10"
    rr_password: str = ""


@dataclass
class ImageSyncConfig:
    enabled: bool = True
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(
        default_factory=lambda: [
            "dnlab/jumphost",
            "dnlab/dns",
            "dnlab/realnet-router",
            "dnlab/realnet-rr",
            "postgres",
            "<none>:<none>",
        ]
    )
    interval_seconds: int = 300


@dataclass
class LabCleanupConfig:
    enabled: bool = True
    interval_seconds: int = 300
    grace_seconds: int = 600
    dry_run: bool = False


@dataclass
class FollowRabbitConfig:
    max_sessions: int = 1


@dataclass
class HostsConfig:
    """Parsed contents of the global hosts file.

    ``jumphost`` is kept as an optional field **only** for the
    backward-compatibility path where a legacy topology still carries an
    in-topology ``infrastructure:`` block alongside a ``jumphost:`` block.
    In the supported layout the jumphost lives in the topology file and
    is threaded through :func:`~dnlab_multinode.services.config.parse_topology`.
    """
    master: InfraHost
    workers: dict[str, InfraHost]
    underlay_iface: str
    mgmt_defaults: MgmtDefaults
    image_sync: ImageSyncConfig
    lab_cleanup: LabCleanupConfig = field(default_factory=LabCleanupConfig)
    jumphost_net: JumphostNetConfig = field(default_factory=JumphostNetConfig)
    webui_ports: WebUIPortsConfig = field(default_factory=WebUIPortsConfig)
    realnet: RealNetConfig = field(default_factory=RealNetConfig)
    follow_the_rabbit: FollowRabbitConfig = field(default_factory=FollowRabbitConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    jumphost: JumphostConfig | None = None
    source_path: Path | None = None   # where this was loaded from (for errors)

    @property
    def all_hosts(self) -> dict[str, InfraHost]:
        return {"master": self.master, **self.workers}


def resolve_hosts_file(explicit: str | Path | None = None) -> Path:
    """Resolve the path of the hosts file.

    Precedence: explicit argument > ``DNLAB_MULTINODE_HOSTS`` env var >
    ``/etc/dnlab/hosts.yml``.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_HOSTS_FILE)


def load_hosts_config(path: str | Path | None = None) -> HostsConfig:
    """Load and validate the global hosts file.

    Raises :class:`HostsConfigError` if the file is missing or malformed.
    """
    resolved = resolve_hosts_file(path)
    if not resolved.exists():
        raise HostsConfigError(
            f"Global hosts file not found: {resolved}. "
            f"Create it or set {ENV_VAR} to a different location."
        )

    log.info("Loading hosts config: %s", resolved)
    try:
        with resolved.open() as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise HostsConfigError(f"Invalid YAML in {resolved}: {exc}") from exc

    if not isinstance(raw, dict):
        raise HostsConfigError(f"{resolved}: root must be a mapping")

    return _parse_hosts_dict(raw, source_path=resolved)


def _parse_hosts_dict(raw: dict, source_path: Path | None = None) -> HostsConfig:
    infra = raw.get("infrastructure")
    if not infra:
        raise HostsConfigError("Missing required section: 'infrastructure'")

    master_cfg = infra.get("master")
    if not master_cfg or not master_cfg.get("host"):
        raise HostsConfigError("Missing or incomplete 'infrastructure.master'")

    master = InfraHost(
        name="master",
        host=master_cfg["host"],
        ssh_user=master_cfg.get("ssh_user", "root"),
        ssh_key=os.path.expanduser(master_cfg.get("ssh_key", "~/.ssh/id_ed25519")),
        is_master=True,
    )

    workers: dict[str, InfraHost] = {}
    for wname, wcfg in (infra.get("workers") or {}).items():
        if not wcfg or not wcfg.get("host"):
            raise HostsConfigError(f"Worker '{wname}' has no 'host' field")
        workers[wname] = InfraHost(
            name=wname,
            host=wcfg["host"],
            ssh_user=wcfg.get("ssh_user", "root"),
            ssh_key=os.path.expanduser(wcfg.get("ssh_key", "~/.ssh/id_ed25519")),
        )

    underlay_iface = infra.get("underlay_iface", "eth0")

    jh_net_cfg = infra.get("jumphost_net") or {}
    ssh_bind_ip = jh_net_cfg.get("ssh_bind_ip", JumphostNetConfig.ssh_bind_ip)
    try:
        ipaddress.ip_address(ssh_bind_ip)
    except ValueError as exc:
        raise HostsConfigError(
            f"infrastructure.jumphost_net.ssh_bind_ip: invalid IPv4 "
            f"address '{ssh_bind_ip}'"
        ) from exc

    ssh_port_range = jh_net_cfg.get("ssh_port_range", JumphostNetConfig.ssh_port_range)
    if not re.fullmatch(r"\d+-\d+", str(ssh_port_range)):
        raise HostsConfigError(
            f"infrastructure.jumphost_net.ssh_port_range: expected "
            f"'<low>-<high>', got '{ssh_port_range}'"
        )
    _low, _high = (int(p) for p in str(ssh_port_range).split("-"))
    if not (1 <= _low <= _high <= 65535):
        raise HostsConfigError(
            f"infrastructure.jumphost_net.ssh_port_range: invalid range "
            f"'{ssh_port_range}' (must satisfy 1 <= low <= high <= 65535)"
        )

    jumphost_net = JumphostNetConfig(
        network=jh_net_cfg.get("network", JumphostNetConfig.network),
        bridge=jh_net_cfg.get("bridge", JumphostNetConfig.bridge),
        ipv4_subnet=jh_net_cfg.get("ipv4_subnet", JumphostNetConfig.ipv4_subnet),
        ipv4_gw=jh_net_cfg.get("ipv4_gw", JumphostNetConfig.ipv4_gw),
        ssh_port_range=str(ssh_port_range),
        ssh_bind_ip=ssh_bind_ip,
    )

    # ── infrastructure.webui_ports (pool clab `ports:` per le Web UI dei VD)
    webui_cfg = infra.get("webui_ports") or {}
    webui_bind_ip = webui_cfg.get("bind_ip", WebUIPortsConfig.bind_ip)
    try:
        ipaddress.ip_address(webui_bind_ip)
    except ValueError as exc:
        raise HostsConfigError(
            f"infrastructure.webui_ports.bind_ip: invalid IPv4 address "
            f"'{webui_bind_ip}'"
        ) from exc
    webui_port_range = webui_cfg.get("port_range", WebUIPortsConfig.port_range)
    if not re.fullmatch(r"\d+-\d+", str(webui_port_range)):
        raise HostsConfigError(
            f"infrastructure.webui_ports.port_range: expected '<low>-<high>', "
            f"got '{webui_port_range}'"
        )
    _wlow, _whigh = (int(p) for p in str(webui_port_range).split("-"))
    if not (1 <= _wlow <= _whigh <= 65535):
        raise HostsConfigError(
            f"infrastructure.webui_ports.port_range: invalid range "
            f"'{webui_port_range}' (require 1 <= low <= high <= 65535)"
        )
    # Sovrapposizione col jumphost SSH range → solo warning, non errore:
    # un operatore può volutamente accettarlo (improbabile ma legittimo).
    if not (_whigh < _low or _wlow > _high):
        log.warning(
            "infrastructure.webui_ports.port_range %s overlaps "
            "infrastructure.jumphost_net.ssh_port_range %s — "
            "allocations from the two pools may collide",
            webui_port_range, ssh_port_range,
        )
    webui_ports = WebUIPortsConfig(
        port_range=str(webui_port_range),
        bind_ip=webui_bind_ip,
    )

    realnet_cfg = infra.get("realnet") or {}
    realnet_defaults = RealNetConfig()
    realnet = RealNetConfig(
        network=realnet_cfg.get("network", realnet_defaults.network),
        bridge=realnet_cfg.get("bridge", realnet_defaults.bridge),
        ipv4_subnet=realnet_cfg.get("ipv4_subnet", realnet_defaults.ipv4_subnet),
        ipv4_gw=realnet_cfg.get("ipv4_gw", realnet_defaults.ipv4_gw),
        image=realnet_cfg.get("image", realnet_defaults.image),
        wan_iface=realnet_cfg.get("wan_iface", realnet_defaults.wan_iface),
        rr_as=int(realnet_cfg.get("rr_as") or realnet_cfg.get("bgp_as") or realnet_defaults.rr_as),
        rr_ip=realnet_cfg.get("rr_ip", realnet_defaults.rr_ip),
        host_net=realnet_cfg.get("host_net", realnet_defaults.host_net),
        router_as_pool=realnet_cfg.get("router_as_pool") or realnet_cfg.get("lab_as_pool") or realnet_defaults.router_as_pool,
        router_ip_pool=realnet_cfg.get("router_ip_pool", realnet_defaults.router_ip_pool),
        realnet_network_pool=realnet_cfg.get("realnet_network_pool", realnet_defaults.realnet_network_pool),
        rr_password=realnet_cfg.get("rr_password", realnet_defaults.rr_password),
    )
    try:
        ipaddress.ip_network(realnet.ipv4_subnet, strict=False)
        ipaddress.ip_address(realnet.ipv4_gw)
    except ValueError as exc:
        raise HostsConfigError(f"infrastructure.realnet: invalid subnet/gateway: {exc}") from exc
    _validate_realnet_bgp(realnet)

    persistence_cfg = infra.get("persistence") or raw.get("persistence") or {}
    persistence = _parse_persistence_config(persistence_cfg)

    # ``jumphost:`` at this level is only kept for the legacy
    # backward-compat path. In the supported layout the block lives in
    # the topology file, not here.
    jh_cfg = raw.get("jumphost")
    jumphost: JumphostConfig | None = None
    if jh_cfg:
        jumphost = JumphostConfig(
            image=jh_cfg.get("image", JumphostConfig().image),
        )

    defaults_cfg = (raw.get("defaults") or {}).get("mgmt") or {}
    mgmt_defaults = MgmtDefaults(
        ipv4_subnet=defaults_cfg.get("ipv4_subnet", "172.20.0.0/24"),
        ipv4_gw=defaults_cfg.get("ipv4_gw", "172.20.0.1"),
    )

    isync_cfg = raw.get("image_sync") or {}
    image_sync = ImageSyncConfig(
        enabled=bool(isync_cfg.get("enabled", True)),
        include=list(isync_cfg.get("include") or ImageSyncConfig().include),
        exclude=list(isync_cfg.get("exclude") or ImageSyncConfig().exclude),
        interval_seconds=int(isync_cfg.get("interval_seconds", 300)),
    )
    cleanup_cfg = raw.get("lab_cleanup") or {}
    if not isinstance(cleanup_cfg, dict):
        raise HostsConfigError("lab_cleanup must be a mapping")
    try:
        cleanup_interval = int(cleanup_cfg.get("interval_seconds", LabCleanupConfig.interval_seconds))
        cleanup_grace = int(cleanup_cfg.get("grace_seconds", LabCleanupConfig.grace_seconds))
    except (TypeError, ValueError) as exc:
        raise HostsConfigError("lab_cleanup interval/grace values must be integers") from exc
    if cleanup_interval < 30:
        raise HostsConfigError("lab_cleanup.interval_seconds must be >= 30")
    if cleanup_grace < 0:
        raise HostsConfigError("lab_cleanup.grace_seconds must be >= 0")
    lab_cleanup = LabCleanupConfig(
        enabled=bool(cleanup_cfg.get("enabled", True)),
        interval_seconds=cleanup_interval,
        grace_seconds=cleanup_grace,
        dry_run=bool(cleanup_cfg.get("dry_run", False)),
    )

    # Top-level ``follow_the_rabbit``; fall back to the legacy ``plus.follow_the_rabbit``
    # block for backward compatibility with hosts.yml files written before the merge.
    rabbit_cfg = raw.get("follow_the_rabbit")
    if rabbit_cfg is None:
        legacy_plus = raw.get("plus") or {}
        if not isinstance(legacy_plus, dict):
            raise HostsConfigError("plus must be a mapping")
        rabbit_cfg = legacy_plus.get("follow_the_rabbit")
    rabbit_cfg = rabbit_cfg or {}
    if not isinstance(rabbit_cfg, dict):
        raise HostsConfigError("follow_the_rabbit must be a mapping")
    try:
        max_sessions = int(rabbit_cfg.get("max_sessions", FollowRabbitConfig.max_sessions))
    except (TypeError, ValueError) as exc:
        raise HostsConfigError("follow_the_rabbit.max_sessions must be an integer") from exc
    if max_sessions < 0:
        raise HostsConfigError("follow_the_rabbit.max_sessions must be >= 0")
    follow_the_rabbit = FollowRabbitConfig(max_sessions=max_sessions)

    return HostsConfig(
        master=master,
        workers=workers,
        underlay_iface=underlay_iface,
        jumphost_net=jumphost_net,
        webui_ports=webui_ports,
        realnet=realnet,
        persistence=persistence,
        lab_cleanup=lab_cleanup,
        follow_the_rabbit=follow_the_rabbit,
        jumphost=jumphost,
        mgmt_defaults=mgmt_defaults,
        image_sync=image_sync,
        source_path=source_path,
    )


def _parse_persistence_config(raw: dict) -> PersistenceConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HostsConfigError("infrastructure.persistence must be a mapping")

    backend = str(raw.get("backend", PersistenceConfig.backend)).strip() or "local-sticky"
    if backend not in {"local-sticky", "cephfs"}:
        raise HostsConfigError(
            "infrastructure.persistence.backend must be one of: local-sticky, cephfs"
        )

    root = str(raw.get("root", PATHS.persist_root)).rstrip("/") or PATHS.persist_root
    allow_fallback = bool(raw.get("allow_migration_fallback", True))

    ceph_raw = raw.get("cephfs") or {}
    if not isinstance(ceph_raw, dict):
        raise HostsConfigError("infrastructure.persistence.cephfs must be a mapping")
    ceph = CephFSConfig(
        mountpoint=str(ceph_raw.get("mountpoint", root)).rstrip("/") or root,
        expected_fstype=str(ceph_raw.get("expected_fstype", CephFSConfig.expected_fstype)),
        marker=str(ceph_raw.get("marker", CephFSConfig.marker)),
        require_shared_marker=bool(
            ceph_raw.get("require_shared_marker", CephFSConfig.require_shared_marker)
        ),
    )
    return PersistenceConfig(
        backend=backend,
        root=root,
        allow_migration_fallback=allow_fallback,
        cephfs=ceph,
    )


def _validate_realnet_bgp(realnet: RealNetConfig) -> None:
    def private_as(asn: int) -> bool:
        return 64512 <= asn <= 65534 or 4200000000 <= asn <= 4294967294

    if not private_as(int(realnet.rr_as)):
        raise HostsConfigError("infrastructure.realnet.rr_as must be a private BGP AS")
    try:
        low_s, high_s = str(realnet.router_as_pool).split("-", 1)
        low, high = int(low_s), int(high_s)
    except Exception as exc:
        raise HostsConfigError("infrastructure.realnet.router_as_pool must be '<low>-<high>'") from exc
    if low > high or not private_as(low) or not private_as(high):
        raise HostsConfigError("infrastructure.realnet.router_as_pool must be inside private BGP AS ranges")
    if not ((64512 <= low <= high <= 65534) or (4200000000 <= low <= high <= 4294967294)):
        raise HostsConfigError("infrastructure.realnet.router_as_pool cannot span private AS ranges")
    if realnet.rr_ip or realnet.host_net:
        try:
            rr_ip = ipaddress.ip_address(realnet.rr_ip)
            host_net = ipaddress.ip_network(realnet.host_net, strict=False)
        except ValueError as exc:
            raise HostsConfigError(f"infrastructure.realnet: invalid rr_ip/host_net: {exc}") from exc
        if rr_ip not in host_net:
            raise HostsConfigError("infrastructure.realnet.rr_ip must belong to host_net")
    if realnet.router_ip_pool:
        raw = str(realnet.router_ip_pool)
        try:
            if "/" in raw:
                ipaddress.ip_network(raw, strict=False)
            else:
                low_ip, high_ip = [ipaddress.ip_address(p.strip()) for p in raw.split("-", 1)]
                if low_ip.version != high_ip.version or int(low_ip) > int(high_ip):
                    raise ValueError("invalid range")
        except ValueError as exc:
            raise HostsConfigError(f"infrastructure.realnet.router_ip_pool invalid: {exc}") from exc
    try:
        pool = ipaddress.ip_network(str(realnet.realnet_network_pool), strict=False)
        if pool.version != 4 or pool.prefixlen > 24:
            raise ValueError("must be an IPv4 CIDR containing at least one /24")
    except ValueError as exc:
        raise HostsConfigError(f"infrastructure.realnet.realnet_network_pool invalid: {exc}") from exc
    if realnet.rr_password:
        if len(str(realnet.rr_password)) > 80 or any(ch.isspace() for ch in str(realnet.rr_password)):
            raise HostsConfigError("infrastructure.realnet.rr_password must be 1-80 characters without whitespace")
def hosts_config_from_legacy_topology(raw: dict) -> HostsConfig:
    """Build a HostsConfig from the legacy in-topology ``infrastructure:`` /
    ``jumphost:`` blocks.

    Used for backward-compatibility: if a topology still carries those
    sections, we honour them and log a deprecation warning.
    """
    synthetic = {
        "infrastructure": raw.get("infrastructure") or {},
        "jumphost": raw.get("jumphost") or {},
    }
    return _parse_hosts_dict(synthetic, source_path=None)
