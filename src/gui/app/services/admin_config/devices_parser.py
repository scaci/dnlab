"""Structured parser for the GUI device catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .base import ConfigParseError, reject_empty

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class VendorEntry(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=128)
    color: str = Field(default="#888888", max_length=16)
    extra: dict[str, Any] = {}


class IconEntry(BaseModel):
    type: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4096)


class WebuiEntry(BaseModel):
    scheme: Literal["http", "https"] = "https"
    port: int = Field(ge=1, le=65535)
    path: str = Field(default="/", max_length=512)
    label: str = Field(default="Web UI", max_length=128)


class DeviceKindEntry(BaseModel):
    kind: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    vendor: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    deploy_kind: str | None = Field(default=None, max_length=128)
    image_patterns: list[str] = []
    mgmt_iface: str | None = Field(default=None, max_length=256)
    webui: list[WebuiEntry] = []
    extra: dict[str, Any] = {}


class DevicesConfigData(BaseModel):
    defaults: dict[str, Any] = {}
    vendors: list[VendorEntry] = []
    icons: list[IconEntry] = []
    kinds: list[DeviceKindEntry] = []
    metadata: dict[str, Any] = {}


class DevicesConfigModel(BaseModel):
    key: str = "devices"
    path: str
    exists: bool
    data: DevicesConfigData
    warnings: list[str] = []


def parse_devices_config(content: str, path: Path, exists: bool) -> DevicesConfigModel:
    try:
        raw = json.loads(content or "{}")
    except Exception as exc:
        raise ConfigParseError(f"devices parse failed: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigParseError("devices.json must be an object")

    vendors_raw = _mapping(raw, "vendors")
    icons_raw = _mapping(raw, "icons")
    kinds_raw = _mapping(raw, "kinds")
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigParseError("devices.defaults must be an object")

    vendors = [_vendor_from_mapping(id_, value or {}) for id_, value in vendors_raw.items()]
    icons = [IconEntry(type=str(type_), path=str(icon_path)) for type_, icon_path in icons_raw.items()]
    kinds = [_kind_from_mapping(kind, value or {}) for kind, value in kinds_raw.items()]
    metadata = {
        key: value for key, value in raw.items()
        if key not in {"defaults", "vendors", "icons", "kinds"}
    }
    model = DevicesConfigModel(
        path=str(path),
        exists=exists,
        data=DevicesConfigData(
            defaults=defaults,
            vendors=vendors,
            icons=icons,
            kinds=kinds,
            metadata=metadata,
        ),
    )
    _validate_devices_model(model)
    return model


def serialize_devices_config(model: DevicesConfigModel) -> str:
    _validate_devices_model(model)
    data: dict[str, Any] = {}
    data.update(model.data.metadata or {})
    data["defaults"] = model.data.defaults or {}
    data["vendors"] = {
        reject_empty(v.id, "vendor id"): _drop_none({
            **(v.extra or {}),
            "title": reject_empty(v.title, f"{v.id}.title"),
            "color": _validate_color(v.color, v.id),
        })
        for v in model.data.vendors
    }
    data["icons"] = {
        reject_empty(i.type, "icon type"): reject_empty(i.path, f"{i.type}.path")
        for i in model.data.icons
    }
    data["kinds"] = {
        reject_empty(k.kind, "kind"): _kind_to_mapping(k)
        for k in model.data.kinds
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigParseError(f"devices.{key} must be an object")
    return value


def _vendor_from_mapping(id_: str, raw: dict[str, Any]) -> VendorEntry:
    if not isinstance(raw, dict):
        raise ConfigParseError(f"vendor {id_} must be an object")
    extra = {key: value for key, value in raw.items() if key not in {"title", "color"}}
    return VendorEntry(
        id=str(id_),
        title=str(raw.get("title") or id_),
        color=str(raw.get("color") or "#888888"),
        extra=extra,
    )


def _kind_from_mapping(kind: str, raw: dict[str, Any]) -> DeviceKindEntry:
    if not isinstance(raw, dict):
        raise ConfigParseError(f"kind {kind} must be an object")
    extra = {
        key: value for key, value in raw.items()
        if key not in {
            "label",
            "vendor",
            "type",
            "deploy_kind",
            "image_patterns",
            "mgmt_iface",
            "webui",
        }
    }
    image_patterns_raw = raw.get("image_patterns") or []
    if isinstance(image_patterns_raw, str):
        image_patterns_raw = [image_patterns_raw]
    if not isinstance(image_patterns_raw, list):
        raise ConfigParseError(f"{kind}.image_patterns must be a list")
    webui_raw = raw.get("webui") or []
    if not isinstance(webui_raw, list):
        raise ConfigParseError(f"{kind}.webui must be a list")
    return DeviceKindEntry(
        kind=str(kind),
        label=str(raw.get("label") or kind),
        vendor=str(raw.get("vendor") or "generic"),
        type=str(raw.get("type") or "router"),
        deploy_kind=None if raw.get("deploy_kind") is None else str(raw.get("deploy_kind")),
        image_patterns=[str(pattern) for pattern in image_patterns_raw if pattern is not None],
        mgmt_iface=None if raw.get("mgmt_iface") is None else str(raw.get("mgmt_iface")),
        webui=[WebuiEntry(**item) for item in webui_raw],
        extra=extra,
    )


def _kind_to_mapping(kind: DeviceKindEntry) -> dict[str, Any]:
    data: dict[str, Any] = {}
    data.update(kind.extra or {})
    data["label"] = reject_empty(kind.label, f"{kind.kind}.label")
    data["vendor"] = reject_empty(kind.vendor, f"{kind.kind}.vendor")
    data["type"] = reject_empty(kind.type, f"{kind.kind}.type")
    if kind.deploy_kind:
        data["deploy_kind"] = reject_empty(kind.deploy_kind, f"{kind.kind}.deploy_kind")
    if kind.image_patterns:
        data["image_patterns"] = [
            reject_empty(pattern, f"{kind.kind}.image_patterns")
            for pattern in kind.image_patterns
        ]
    data["mgmt_iface"] = kind.mgmt_iface
    if kind.webui:
        data["webui"] = [
            {
                "scheme": port.scheme,
                "port": port.port,
                "path": port.path if port.path.startswith("/") else f"/{port.path}",
                "label": reject_empty(port.label, f"{kind.kind}.webui.label"),
            }
            for port in kind.webui
        ]
    return _drop_none(data)


def _validate_devices_model(model: DevicesConfigModel) -> None:
    vendor_ids = set()
    for vendor in model.data.vendors:
        if vendor.id in vendor_ids:
            raise ConfigParseError(f"duplicate vendor: {vendor.id}")
        vendor_ids.add(vendor.id)
        _validate_color(vendor.color, vendor.id)

    icon_types = set()
    for icon in model.data.icons:
        if icon.type in icon_types:
            raise ConfigParseError(f"duplicate icon type: {icon.type}")
        icon_types.add(icon.type)

    kind_ids = set()
    for kind in model.data.kinds:
        if kind.kind in kind_ids:
            raise ConfigParseError(f"duplicate kind: {kind.kind}")
        kind_ids.add(kind.kind)
        if vendor_ids and kind.vendor not in vendor_ids:
            raise ConfigParseError(f"{kind.kind}: unknown vendor {kind.vendor}")
        if icon_types and kind.type not in icon_types:
            raise ConfigParseError(f"{kind.kind}: unknown type {kind.type}")


def _validate_color(value: str, label: str) -> str:
    color = reject_empty(value, f"{label}.color")
    if not _HEX_COLOR.match(color):
        raise ConfigParseError(f"{label}.color must be #RRGGBB")
    return color


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}
