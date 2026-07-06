"""Backend helpers for the GUI device catalog."""

from __future__ import annotations

import json
from typing import Any

from app.config import settings


_CATALOG_CACHE: tuple[float | None, dict[str, Any]] | None = None


def _catalog() -> dict[str, Any]:
    path = settings.STATIC_DIR / "config" / "devices.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None

    global _CATALOG_CACHE
    if _CATALOG_CACHE and _CATALOG_CACHE[0] == mtime:
        return _CATALOG_CACHE[1]

    try:
        data = json.loads(path.read_text())
    except Exception:
        data = {"kinds": {}, "defaults": {}}
    if not isinstance(data, dict):
        data = {"kinds": {}, "defaults": {}}
    _CATALOG_CACHE = (mtime, data)
    return data


def kind_entry(kind: str | None) -> dict[str, Any]:
    kinds = _catalog().get("kinds") or {}
    entry = kinds.get(kind or "")
    return entry if isinstance(entry, dict) else {}


def deploy_kind(kind: str | None) -> str:
    entry = kind_entry(kind)
    return str(entry.get("deploy_kind") or kind or "")


def reload() -> None:
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


def default_env(kind: str | None) -> dict[str, str]:
    entry = kind_entry(kind)
    env = entry.get("env")
    if not isinstance(env, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in env.items()
        if k is not None and v is not None
    }


def resource_schema(kind: str | None) -> dict[str, Any]:
    """Return the data-driven resource schema for a GUI kind."""
    entry = kind_entry(kind)
    resources = entry.get("resources")
    return resources if isinstance(resources, dict) else {}


def node_features_catalog() -> dict[str, Any]:
    features = _catalog().get("node_features")
    return features if isinstance(features, dict) else {}


def node_features_for_kind(kind: str | None) -> dict[str, dict[str, Any]]:
    """Return enabled data-driven node features for a GUI kind."""
    entry = kind_entry(kind)
    enabled = entry.get("node_features") or []
    if isinstance(enabled, str):
        enabled = [enabled]
    if not isinstance(enabled, list):
        return {}
    catalog = node_features_catalog()
    out: dict[str, dict[str, Any]] = {}
    for key in enabled:
        feature_key = str(key)
        cfg = catalog.get(feature_key)
        if isinstance(cfg, dict):
            out[feature_key] = cfg
    return out


def clean_node_features_state(kind: str | None, state: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize per-node feature state against devices.json.

    Currently supports the generic ``checkbox-list`` UI shape. Unknown feature
    keys and unknown item keys are dropped so replacing/removing a catalog
    feature does not leave deploy-affecting stale state behind.
    """
    if not isinstance(state, dict):
        return {}
    features = node_features_for_kind(kind)
    cleaned: dict[str, Any] = {}
    for feature_key, raw_feature_state in state.items():
        cfg = features.get(str(feature_key))
        if not isinstance(cfg, dict):
            continue
        ui = cfg.get("ui")
        if not isinstance(ui, dict) or ui.get("type") != "checkbox-list":
            continue
        items = ui.get("items") or []
        if not isinstance(items, list):
            continue
        allowed = {
            str(item.get("key"))
            for item in items
            if isinstance(item, dict) and item.get("key") is not None
        }
        if not allowed or not isinstance(raw_feature_state, dict):
            continue
        feature_state = {
            str(key): bool(value)
            for key, value in raw_feature_state.items()
            if str(key) in allowed
        }
        if feature_state:
            cleaned[str(feature_key)] = feature_state
    return cleaned


def node_features_sidecar_for_kind(kind: str | None, state: dict[str, Any] | None) -> dict[str, Any]:
    """Return deploy-facing feature metadata for a node.

    The GUI keeps only user state in memory/API responses. The topology sidecar
    also carries the catalog ``materialize`` block so the multinode orchestrator
    can remain generic and avoid importing the GUI catalog.
    """
    cleaned = clean_node_features_state(kind, state)
    if not cleaned:
        return {}
    features = node_features_for_kind(kind)
    out: dict[str, Any] = {}
    for feature_key, feature_state in cleaned.items():
        cfg = features.get(feature_key) or {}
        materialize = cfg.get("materialize")
        entry: dict[str, Any] = {"state": feature_state}
        if isinstance(materialize, dict):
            entry["materialize"] = materialize
        out[feature_key] = entry
    return out


def kind_vendor(kind: str | None) -> str:
    entry = kind_entry(kind)
    defaults = _catalog().get("defaults") or {}
    return str(entry.get("vendor") or defaults.get("vendor") or "generic")


def vendor_color(vendor: str | None) -> str:
    vendors = _catalog().get("vendors") or {}
    defaults = _catalog().get("defaults") or {}
    default_vendor = str(defaults.get("vendor") or "generic")
    entry = vendors.get(vendor or "") or vendors.get(default_vendor) or {}
    if not isinstance(entry, dict):
        return "#888888"
    return str(entry.get("color") or "#888888")


def kind_type(kind: str | None) -> str:
    entry = kind_entry(kind)
    defaults = _catalog().get("defaults") or {}
    return str(entry.get("type") or defaults.get("type") or "router")


def icon_path(icon_type: str | None) -> str:
    icons = _catalog().get("icons") or {}
    defaults = _catalog().get("defaults") or {}
    default_type = str(defaults.get("type") or "router")
    return str(icons.get(icon_type or "") or icons.get(default_type) or "")


def resolve_image_kind(repository: str) -> str | None:
    repo_lower = repository.lower()
    base = repo_lower.split("/")[-1]
    full_base = "/".join(repo_lower.split("/")[-2:])

    for kind, entry in (_catalog().get("kinds") or {}).items():
        if not isinstance(entry, dict):
            continue
        patterns = entry.get("image_patterns") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        for pattern in patterns:
            fragment = str(pattern).lower()
            if fragment and (fragment in full_base or fragment in base):
                return str(kind)
    return None


def resolve_gui_kind(repository: str) -> str:
    """Catalog GUI kind for an image repo, with IMAGE_KIND_MAP fallback."""
    catalog_kind = resolve_image_kind(repository)
    if catalog_kind:
        return catalog_kind
    repo_lower = repository.lower()
    base = repo_lower.split("/")[-1]
    full_base = "/".join(repo_lower.split("/")[-2:])
    for fragment, kind in settings.IMAGE_KIND_MAP.items():
        if fragment in full_base or fragment in base:
            return kind
    return "linux"


def resolve_kind_and_vendor(repository: str) -> tuple[str, str]:
    """Resolve the GUI kind and vendor for an image repo in one place.

    Single enrichment point shared by the local (docker SDK) and remote
    (multinode API) image-discovery paths so they cannot diverge.
    """
    kind = resolve_gui_kind(repository)
    vendor = settings.KIND_VENDOR_MAP.get(kind) or kind_vendor(kind)
    return kind, vendor


def gui_kind_for_deploy_kind(deploy_kind_value: str | None, image: str | None = None) -> str | None:
    if image:
        matched = resolve_image_kind(image)
        if matched:
            entry = kind_entry(matched)
            if str(entry.get("deploy_kind") or matched) == (deploy_kind_value or ""):
                return matched

    if kind_entry(deploy_kind_value):
        return None

    for kind, entry in (_catalog().get("kinds") or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("deploy_kind") and str(entry.get("deploy_kind")) == (deploy_kind_value or ""):
            return str(kind)
    return None


def has_deploy_kind_alias(kind: str | None) -> bool:
    resolved = deploy_kind(kind)
    return bool(kind and resolved and resolved != kind)


def interface_overrides() -> dict[str, dict[str, Any]]:
    overrides: dict[str, dict[str, Any]] = {}
    for kind, entry in (_catalog().get("kinds") or {}).items():
        if not isinstance(entry, dict):
            continue
        interfaces = entry.get("interfaces")
        if isinstance(interfaces, dict):
            overrides[str(kind)] = interfaces
    return overrides


def interface_map(fallback: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Return per-kind interface naming patterns from devices.json.

    ``fallback`` is only used if the catalog has no interface definitions,
    keeping the GUI usable when devices.json is missing or malformed.
    """
    configured = interface_overrides()
    if configured:
        return configured
    return dict(fallback or {})
