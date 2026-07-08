"""CLI entrypoint for the image-sync daemon (``dnlab-image-sync``)."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from dnlab_multinode.services.hosts_config import (
    HostsConfigError, load_hosts_config, resolve_hosts_file,
)
from dnlab_multinode.services.image_sync import (
    DEFAULT_STATE_FILE, ImageSyncDaemon,
    list_master_images, filter_images, reconcile_once, read_state_file,
)
from dnlab_multinode.services.logging_config import setup_service_logging

console = Console()
log = logging.getLogger(__name__)


def _setup_logging(debug: bool) -> None:
    setup_service_logging(
        service="image-sync",
        filename="dnlab-image-sync.log",
        debug=debug,
    )


@click.group()
@click.option("-d", "--debug", is_flag=True, help="Enable debug logging")
@click.option("--hosts", "hosts_file", default=None,
              help="Global hosts file (default: /etc/dnlab/hosts.yml "
                   "or $DNLAB_MULTINODE_HOSTS)")
@click.option("--state-file", default=None,
              help=f"State JSON path (default: {DEFAULT_STATE_FILE})")
@click.pass_context
def main(ctx: click.Context, debug: bool, hosts_file: str | None,
         state_file: str | None) -> None:
    """Image-sync daemon for dnlab-multinode."""
    _setup_logging(debug)
    ctx.ensure_object(dict)
    ctx.obj["hosts_file"] = hosts_file
    ctx.obj["state_file"] = Path(state_file) if state_file else DEFAULT_STATE_FILE


def _load(ctx: click.Context):
    try:
        return load_hosts_config(ctx.obj["hosts_file"])
    except HostsConfigError as e:
        console.print(f"[red][✗] {e}[/red]")
        sys.exit(1)


# ── start ────────────────────────────────────────────────────────────

@main.command()
@click.option("--no-remove-extra", is_flag=True,
              help="Do not run docker rmi on extra worker images")
@click.pass_context
def start(ctx: click.Context, no_remove_extra: bool) -> None:
    """Run the daemon loop (blocks until SIGINT/SIGTERM)."""
    hosts = _load(ctx)
    daemon = ImageSyncDaemon(
        hosts, ctx.obj["state_file"],
        remove_extra=not no_remove_extra,
    )

    def _handle_sig(_signo, _frame):
        log.info("caught signal — stopping daemon")
        daemon.stop()

    def _handle_trigger(_signo, _frame):
        # SIGUSR1 = "reconcile now" — sent by dnlab-gui or by the
        # operator (`systemctl kill -s SIGUSR1 dnlab-image-sync`).
        log.info("caught SIGUSR1 — triggering immediate reconcile")
        daemon.trigger_reconcile()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGUSR1, _handle_trigger)

    # When running dockerized the GUI can no longer signal us via systemd, so
    # expose a tiny HTTP control surface (status + reconcile) that the
    # multinode API proxies. Enabled by setting DNLAB_IMAGE_SYNC_API_PORT.
    api_port = os.getenv("DNLAB_IMAGE_SYNC_API_PORT")
    if api_port:
        from dnlab_multinode.image_sync_api import serve_in_thread
        serve_in_thread(
            daemon,
            os.getenv("DNLAB_IMAGE_SYNC_API_HOST", "127.0.0.1"),
            int(api_port),
        )

    daemon.run()


# ── sync (one-shot) ──────────────────────────────────────────────────

@main.command()
@click.option("--image", default=None,
              help="Reconcile only a specific image name (still requires the "
                   "image to pass the include/exclude filter)")
@click.option("--all", "all_images", is_flag=True,
              help="Reconcile every filtered image (default)")
@click.option("--no-remove-extra", is_flag=True,
              help="Do not run docker rmi on extra worker images")
@click.pass_context
def sync(ctx: click.Context, image: str | None, all_images: bool,
         no_remove_extra: bool) -> None:
    """Run a single reconcile pass and exit."""
    hosts = _load(ctx)
    if image and all_images:
        console.print("[red][✗] --image and --all are mutually exclusive[/red]")
        sys.exit(1)

    if image:
        original = hosts.image_sync
        original.include = [image]
        original.exclude = []
        console.print(f"[*] Forcing reconcile of: {image}")

    state = reconcile_once(
        hosts, ctx.obj["state_file"],
        remove_extra=not no_remove_extra,
    )

    _print_state(state.to_dict())


# ── status ───────────────────────────────────────────────────────────

@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Print the last published state (from the state file)."""
    data = read_state_file(ctx.obj["state_file"])
    if data is None:
        console.print(
            f"[yellow]No state file at {ctx.obj['state_file']} — "
            f"daemon never ran or not reachable[/yellow]"
        )
        sys.exit(2)

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    _print_state(data)


# ── helpers ──────────────────────────────────────────────────────────

def _print_state(data: dict) -> None:
    console.print(f"\n[bold]Updated at:[/bold] {data.get('updated_at', '—')}")
    console.print(f"[bold]Reconciles:[/bold] {data.get('reconcile_count', 0)}  "
                  f"([dim]last took {data.get('last_reconcile_duration_ms', 0)}ms[/dim])")
    console.print(f"[bold]Interval:[/bold] {data.get('interval_seconds', 0)}s")

    master = data.get("master", {})
    console.print(f"\n[bold]Master:[/bold] {master.get('host', '—')} — "
                  f"{len(master.get('images', {}))} filtered image(s)")

    workers = data.get("workers", {})
    if not workers:
        console.print("[yellow]No workers configured[/yellow]")
        return

    table = Table(title="Workers")
    table.add_column("Name")
    table.add_column("Host")
    table.add_column("Reachable")
    table.add_column("Images", justify="right")
    table.add_column("Missing", justify="right")
    table.add_column("Extra", justify="right")
    table.add_column("Last sync")
    table.add_column("Error")
    for name, w in workers.items():
        table.add_row(
            name,
            w.get("host", "—"),
            "[green]yes[/green]" if w.get("reachable") else "[red]no[/red]",
            str(len(w.get("images", {}))),
            str(len(w.get("missing", []))),
            str(len(w.get("extra", []))),
            w.get("last_sync_at", "—") or "—",
            w.get("last_error", "") or "",
        )
    console.print(table)


if __name__ == "__main__":
    main()
