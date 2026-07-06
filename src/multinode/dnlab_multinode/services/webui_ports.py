"""Allocazione delle porte host-side per le Web UI dei VD.

Pool sito-wide configurato in ``hosts.yml`` (``infrastructure.webui_ports``):

.. code-block:: yaml

    infrastructure:
      webui_ports:
        port_range: "8443-8999"
        bind_ip: "127.0.0.1"

Le allocazioni avvengono al **deploy time** (analogamente a
``allocate_jumphost_ssh_port``): l'allocator scansiona le porte già
pubblicate dai container ``clab-*-*`` su ``bind_ip`` (qualsiasi
proto), ne ricava il "used set" e ritorna la prima porta libera
nel range richiesto.

Sticky reuse
------------
Per evitare che le porte cambino fra deploy successivi dello stesso
lab, il chiamante può passare ``preferred=<port>``: se quella porta
non risulta in uso, la riconfermiamo. Le allocazioni precedenti
vengono memorizzate dal chiamante in ``LabState.webui_allocations``
e ripassate qui all'occorrenza.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from dnlab_multinode.services.ssh import SSHClient

log = logging.getLogger(__name__)


class WebUIPortAllocationError(Exception):
    """Range esaurito o spec non valida."""


def parse_port_range(spec: str) -> tuple[int, int]:
    """Parsa ``<low>-<high>`` (inclusivo)."""
    if not re.fullmatch(r"\d+-\d+", spec or ""):
        raise WebUIPortAllocationError(
            f"webui port range must be '<low>-<high>', got {spec!r}"
        )
    low, high = (int(p) for p in spec.split("-"))
    if not (1 <= low <= high <= 65535):
        raise WebUIPortAllocationError(
            f"webui port range {spec!r} invalid "
            f"(require 1 <= low <= high <= 65535)"
        )
    return low, high


def _ports_in_use_for_webui(client: SSHClient, bind_ip: str) -> set[int]:
    """Porte host-side già pubblicate da container ``clab-*-*`` su
    ``bind_ip``. Tipo proto e numero container-side non ci interessano:
    contiamo solo quale numero host-side è già occupato.
    """
    rc, out, _ = client.run_no_check(
        "docker ps --filter 'name=clab-' --format '{{.Names}}\t{{.Ports}}'"
    )
    if rc != 0 or not out:
        return set()

    used: set[int] = set()
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ports_str = parts[1].strip()
        if not ports_str:
            continue
        # Ogni mapping: "<host-ip>:<host-port>-><cont-port>/<proto>".
        # Multi-mapping virgola-separati. Ignoriamo udp/tcp distinction.
        for chunk in ports_str.split(","):
            chunk = chunk.strip()
            if "->" not in chunk:
                continue
            lhs = chunk.split("->", 1)[0]
            if ":" not in lhs:
                continue
            ip_part, _, port_part = lhs.rpartition(":")
            if ip_part != bind_ip:
                # Bind diversi non collidono dal punto di vista del
                # kernel — un :443 su 127.0.0.1 e un :443 su 0.0.0.0
                # sono in conflitto solo se entrambi includono il
                # nostro bind. Pessimismo opportunistico: contiamo
                # anche bind 0.0.0.0 se siamo su un IP specifico.
                if not (ip_part == "0.0.0.0" or bind_ip == "0.0.0.0"):
                    continue
            try:
                used.add(int(port_part))
            except ValueError:
                continue
    return used


def allocate_webui_port(
    client: SSHClient,
    bind_ip: str,
    port_range: str,
    *,
    used_extra: Iterable[int] | None = None,
    preferred: int | None = None,
) -> int:
    """Alloca una porta host-side libera nel range.

    * ``used_extra`` — porte aggiuntive da considerare occupate (utili
      quando l'allocator viene chiamato più volte nello stesso giro
      di deploy: le porte appena assegnate non sono ancora visibili
      via ``docker ps``).
    * ``preferred`` — se valorizzato e la porta non è occupata, viene
      riconfermata (sticky cross-deploy).

    Solleva :class:`WebUIPortAllocationError` se il range è esaurito.
    """
    low, high = parse_port_range(port_range)
    used = _ports_in_use_for_webui(client, bind_ip)
    if used_extra:
        used |= set(used_extra)

    if preferred is not None and low <= preferred <= high and preferred not in used:
        return preferred

    for port in range(low, high + 1):
        if port not in used:
            return port

    raise WebUIPortAllocationError(
        f"WebUI port range {port_range} on {bind_ip} exhausted "
        f"({len(used)} in use). Widen the range in hosts.yml or destroy "
        f"some labs to free ports."
    )
