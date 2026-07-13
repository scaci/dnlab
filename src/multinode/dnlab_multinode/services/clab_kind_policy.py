"""Containerlab apply lifecycle policy per deploy kind.

The policy is intentionally keyed by Containerlab ``kind``/dNLab
``deploy_kind`` rather than by the GUI kind.  The GUI catalog remains the
source of truth for aliases such as ``nvidia_cumulusvx`` → ``generic_vm``;
once a topology reaches the multinode runtime we only trust the resolved
deploy kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ApplyMode = Literal["live", "restart", "recreate"]

LIVE = "live"
RESTART = "restart"
RECREATE = "recreate"


@dataclass(frozen=True)
class KindPolicy:
    """Runtime expectation for Containerlab ``apply`` on a deploy kind."""

    deploy_kind: str
    mode: ApplyMode
    reason: str


# Kinds qualified for live reconciliation of safe topology mutations such as
# link add/remove without replacing the container.
LIVE_KINDS = frozenset({
    "linux",
    "nokia_srlinux",
    "nokia_srsim",
})

# cEOS is container-native, but topology/env changes observed through
# Containerlab apply require at least a restart-level qualification.
RESTART_KINDS = frozenset({
    "arista_ceos",
})


def policy_for_deploy_kind(deploy_kind: str | None) -> KindPolicy:
    """Return the conservative Containerlab apply policy for a deploy kind."""

    kind = (deploy_kind or "").strip()
    if kind in LIVE_KINDS:
        return KindPolicy(kind, LIVE, "qualified container-native live reconciliation")
    if kind in RESTART_KINDS:
        return KindPolicy(kind, RESTART, "qualified container-native restart reconciliation")
    return KindPolicy(kind, RECREATE, "default conservative policy for VM/unknown kinds")


def expected_apply_mode(deploy_kind: str | None) -> ApplyMode:
    """Convenience accessor for matrix tests and deployment planning."""

    return policy_for_deploy_kind(deploy_kind).mode
