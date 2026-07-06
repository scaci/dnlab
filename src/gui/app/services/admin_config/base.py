"""Shared helpers for structured admin config files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class ConfigParseError(ValueError):
    """Raised when a config file cannot be parsed or validated."""


class ConfigFileModel(BaseModel):
    key: str
    path: str
    exists: bool
    data: Any
    warnings: list[str] = []


def read_text_or_default(path: Path, default: str) -> tuple[str, bool]:
    if path.exists():
        return path.read_text(encoding="utf-8"), True
    return default, False


def load_yaml_mapping(content: str, key: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content) or {}
    except Exception as exc:
        raise ConfigParseError(f"{key} parse failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigParseError(f"{key} must be a mapping")
    return parsed


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)


def reject_empty(value: str | None, label: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ConfigParseError(f"{label} is required")
    return clean
