"""Shared logging setup for dNLab infrastructure services."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from dnlab_multinode.services.paths import PATHS


def setup_service_logging(
    *,
    service: str,
    filename: str,
    root_name: str = "dnlab_multinode",
    debug: bool = False,
) -> Path:
    """Configure console and rotating file logging for one service."""
    root = logging.getLogger(root_name)
    root.setLevel(logging.DEBUG)
    if root.handlers:
        return Path(PATHS.log_root) / service / filename

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    log_dir = Path(PATHS.log_root) / service
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / filename
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    root.info("Logging initialized -> %s", log_file)
    return log_file
