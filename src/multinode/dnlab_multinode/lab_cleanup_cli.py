"""CLI entrypoint for the lab-cleanup daemon."""

from __future__ import annotations

import json
import logging
import logging.handlers
import signal
from pathlib import Path

import click
from rich.console import Console

from dnlab_multinode.services.hosts_config import load_hosts_config
from dnlab_multinode.services.lab_cleanup import (
    DEFAULT_STATE_FILE,
    LabCleanupDaemon,
    read_state_file,
    reconcile_once,
)
from dnlab_multinode.services.paths import PATHS

console = Console()
log = logging.getLogger("dnlab_multinode.lab_cleanup")


def _setup_logging(debug: bool = False) -> None:
    root = logging.getLogger("dnlab_multinode")
    root.setLevel(logging.DEBUG)
    if root.handlers:
        return
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    log_dir = Path(PATHS.log_dir_multinode)
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "dnlab-lab-cleanup.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)


@click.group()
@click.option("-d", "--debug", is_flag=True, help="Enable debug logging")
def main(debug: bool) -> None:
    """Lab cleanup daemon for dnlab-multinode."""
    _setup_logging(debug)


@main.command()
@click.option("--hosts", "hosts_file", default=None, help="Path to global hosts file")
@click.option("--state-file", default=None, help="Override cleanup state file path")
def start(hosts_file: str | None, state_file: str | None) -> None:
    """Run the daemon loop (blocks until SIGINT/SIGTERM)."""
    hosts = load_hosts_config(hosts_file)
    daemon = LabCleanupDaemon(
        hosts,
        Path(state_file) if state_file else DEFAULT_STATE_FILE,
    )

    def _stop(_signum, _frame):
        log.info("caught signal, stopping lab-cleanup daemon")
        daemon.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    daemon.run()


@main.command()
@click.option("--hosts", "hosts_file", default=None, help="Path to global hosts file")
@click.option("--dry-run/--execute", default=True, help="Plan cleanup without mutating hosts")
@click.option("--state-file", default=None, help="Override cleanup state file path")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON")
def sync(hosts_file: str | None, dry_run: bool, state_file: str | None, as_json: bool) -> None:
    """Run one cleanup reconcile pass."""
    hosts = load_hosts_config(hosts_file)
    report = reconcile_once(
        hosts,
        state_file=Path(state_file) if state_file else DEFAULT_STATE_FILE,
        dry_run=dry_run,
    )
    data = report.to_dict()
    if as_json:
        console.print(json.dumps(data, indent=2, sort_keys=True))
        return
    actions = sum(len(plan.actions) for plan in report.labs.values())
    mode = "planned" if report.dry_run else "executed"
    console.print(f"[green][✓] Cleanup {mode}:[/green] {actions} actions")


@main.command()
@click.option("--state-file", default=None, help="Override cleanup state file path")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON")
def status(state_file: str | None, as_json: bool) -> None:
    """Read the last cleanup state snapshot."""
    data = read_state_file(Path(state_file) if state_file else DEFAULT_STATE_FILE)
    if data is None:
        console.print("[yellow]lab-cleanup daemon never ran or state is unavailable[/yellow]")
        raise SystemExit(1)
    if as_json:
        console.print(json.dumps(data, indent=2, sort_keys=True))
        return
    console.print(f"[bold]Updated:[/bold] {data.get('updated_at', '-')}")
    console.print(f"[bold]Labs:[/bold] {len(data.get('labs', {}))}")
    console.print(f"[bold]Dry-run:[/bold] {data.get('dry_run')}")


if __name__ == "__main__":
    main()
