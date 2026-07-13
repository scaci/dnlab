"""Containerlab feature detection for the opt-in per-host runtime."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

PER_VD = "per-vd"
PER_HOST_APPLY = "per-host-apply"
RUNTIME_MODE_ENV = "DNLAB_CONTAINERLAB_RUNTIME_MODE"
MIN_APPLY_VERSION = (0, 77, 0)


@dataclass(frozen=True)
class ContainerlabCapabilities:
    version: str
    validate: bool
    apply: bool
    lifecycle: bool
    events: bool
    inspect_interfaces: bool

    @property
    def per_host_apply(self) -> bool:
        return (
            _version_tuple(self.version) >= MIN_APPLY_VERSION
            and self.apply
            and self.lifecycle
            and self.events
            and self.inspect_interfaces
        )


def requested_runtime_mode() -> str:
    value = (os.getenv(RUNTIME_MODE_ENV) or PER_VD).strip().lower()
    if value not in {PER_VD, PER_HOST_APPLY}:
        raise ValueError(
            f"{RUNTIME_MODE_ENV} must be '{PER_VD}' or '{PER_HOST_APPLY}', got {value!r}"
        )
    return value


def probe(client) -> ContainerlabCapabilities:
    rc, version, _ = client.run_no_check("containerlab version --short")
    if rc != 0:
        version = "0.0.0"

    def supports(command: str) -> bool:
        result, _, _ = client.run_no_check(f"containerlab {command} --help")
        return result == 0

    return ContainerlabCapabilities(
        version=version.strip().lstrip("v"),
        validate=supports("validate"),
        apply=supports("apply"),
        lifecycle=all(supports(cmd) for cmd in ("start", "stop", "restart")),
        events=supports("events"),
        inspect_interfaces=supports("inspect interfaces"),
    )


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())
