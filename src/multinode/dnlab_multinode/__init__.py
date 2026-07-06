"""dnlab-multinode — ContainerLab Multi-Node Orchestrator.

Public Python API. Programmatic callers (e.g. the dnlab-gui façade) should
import from this top-level module rather than reaching into submodules.

Example::

    from dnlab_multinode import DeployController, ProgressEvent

    def on_progress(evt: ProgressEvent) -> None:
        print(f"[{evt.phase}] {evt.detail}")

    state = DeployController(
        "triangle.yml",
        hosts_file="/etc/dnlab/hosts.yml",
        progress=on_progress,
    ).run()
"""

from dnlab_multinode.controllers.deploy import DeployController, DeployError
from dnlab_multinode.controllers.destroy import DestroyController, DestroyError
from dnlab_multinode.controllers.node import NodeLifecycleController, NodeLifecycleError
from dnlab_multinode.controllers.plan import PlanController, PlanError
from dnlab_multinode.controllers.realnet import RealNetLifecycleController, RealNetLifecycleError
from dnlab_multinode.controllers.status import (
    StatusController, StatusReport, NodeStatus, HostStatus, InfraStatus,
)
from dnlab_multinode.controllers.sync import SyncController
from dnlab_multinode.models.schedule import SchedulePlan, HostAssignment, CrossHostLink
from dnlab_multinode.models.state import (
    DeploymentState, MgmtState, JumphostState, DnsState,
    RuntimeRelayState, HostScheduleState, VxlanLinkState,
    NodeRuntimeState, RuntimeLinkState,
)
from dnlab_multinode.models.topology import (
    DistributedTopology, InfraHost, VDNode, Link, MgmtConfig, JumphostConfig,
    JumphostNet,
)
from dnlab_multinode.services.config import parse_topology, ConfigError
from dnlab_multinode.services.hosts_config import (
    HostsConfig, HostsConfigError, MgmtDefaults, ImageSyncConfig,
    JumphostNetConfig, load_hosts_config, resolve_hosts_file,
)
from dnlab_multinode.services.progress import (
    ProgressEvent, ProgressCallback, NullProgress, log_progress,
)

__all__ = [
    # Controllers
    "PlanController", "PlanError",
    "DeployController", "DeployError",
    "DestroyController", "DestroyError",
    "NodeLifecycleController", "NodeLifecycleError",
    "RealNetLifecycleController", "RealNetLifecycleError",
    "SyncController",
    "StatusController", "StatusReport", "NodeStatus", "HostStatus", "InfraStatus",
    # Topology / scheduling models
    "DistributedTopology", "InfraHost", "VDNode", "Link",
    "MgmtConfig", "JumphostConfig", "JumphostNet",
    "SchedulePlan", "HostAssignment", "CrossHostLink",
    # Runtime state models
    "DeploymentState", "MgmtState", "JumphostState", "DnsState",
    "RuntimeRelayState", "HostScheduleState",
    "VxlanLinkState", "NodeRuntimeState", "RuntimeLinkState",
    # Config
    "parse_topology", "ConfigError",
    "HostsConfig", "HostsConfigError", "MgmtDefaults", "ImageSyncConfig",
    "JumphostNetConfig",
    "load_hosts_config", "resolve_hosts_file",
    # Progress API
    "ProgressEvent", "ProgressCallback", "NullProgress", "log_progress",
]
