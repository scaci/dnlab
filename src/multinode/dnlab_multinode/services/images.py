"""Central dNLab image-name resolver.

Internal dNLab component images share one product version. Operators may
change the registry prefix for mirroring, but not per-component tags.
"""

from __future__ import annotations

import os


DEFAULT_DNLAB_VERSION = "latest"
DEFAULT_IMAGE_PREFIX = "dnlab/"

COMPONENTS = {
    "jumphost": "jumphost",
    "dns": "dns",
    "runtime-relay": "runtime-relay",
    "realnet-router": "realnet-router",
    "realnet-rr": "realnet-rr",
    "mgmt-anchor": "mgmt-anchor",
}


def dnlab_version() -> str:
    return (os.getenv("DNLAB_VERSION") or DEFAULT_DNLAB_VERSION).strip()


def dnlab_image_prefix() -> str:
    return (os.getenv("DNLAB_RUNTIME_IMAGE_PREFIX") or DEFAULT_IMAGE_PREFIX).strip()


def image_for(component: str) -> str:
    try:
        name = COMPONENTS[component]
    except KeyError as exc:
        raise ValueError(f"unknown dNLab image component: {component}") from exc
    return f"{dnlab_image_prefix()}{name}:{dnlab_version()}"


def runtime_images() -> dict[str, str]:
    return {component: image_for(component) for component in COMPONENTS}
