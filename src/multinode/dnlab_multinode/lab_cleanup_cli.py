"""CLI entrypoint for the lab-cleanup daemon."""

from __future__ import annotations

import json
import logging
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
from dnlab_multinode.services.logging_config import setup_service_logging

console = Console()
log = logging.getLogger("dnlab_multinode.lab_cleanup")


def _setup_logging(debug: bool = False) -> None:
    setup_service_logging(
        service="lab-cleanup",
        filename="dnlab-lab-cleanup.log",
        debug=debug,
    )


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
