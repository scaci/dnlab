"""Structured admin configuration parsers."""

from .devices_parser import DevicesConfigModel, parse_devices_config, serialize_devices_config
from .hosts_parser import HostsConfigModel, parse_hosts_config, serialize_hosts_config
from .paths_parser import PathsConfigModel, parse_paths_config, serialize_paths_config

__all__ = [
    "DevicesConfigModel",
    "HostsConfigModel",
    "PathsConfigModel",
    "parse_devices_config",
    "parse_hosts_config",
    "parse_paths_config",
    "serialize_devices_config",
    "serialize_hosts_config",
    "serialize_paths_config",
]
