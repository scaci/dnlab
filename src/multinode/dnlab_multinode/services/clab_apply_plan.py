"""Conservative parser and policy checks for ``containerlab apply --dry-run``.

Containerlab 0.77 prints the apply plan as a human-readable table, not as JSON.
This module deliberately recognizes only stable, observed action rows. Unknown
or unparsable output is not treated as proof of safety; it simply cannot be used
for hard enforcement yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from dnlab_multinode.services import clab_kind_policy


@dataclass(frozen=True)
class ApplyPlanEntry:
    action: str
    details: str
    nodes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApplyPlanViolation:
    node: str
    action: str
    apply_mode: str
    details: str


_KNOWN_ACTIONS = {
    "deploy lab",
    "added links",
    "removed links",
    "changed links",
    "added nodes",
    "deleted nodes",
    "restarted nodes",
    "recreated nodes",
}


def parse_apply_plan(output: str) -> list[ApplyPlanEntry]:
    """Parse recognized rows from Containerlab's dry-run plan table."""

    entries: list[ApplyPlanEntry] = []
    for line in (output or "").splitlines():
        if "│" not in line:
            continue
        cells = [cell.strip() for cell in line.split("│")[1:-1]]
        if len(cells) < 2:
            continue
        action = _normalize_action(cells[0])
        details = cells[1].strip()
        if action not in _KNOWN_ACTIONS:
            continue
        entries.append(
            ApplyPlanEntry(
                action=action,
                details=details,
                nodes=_nodes_for_action(action, details),
            )
        )
    return entries


def policy_violations(
    entries: list[ApplyPlanEntry],
    node_apply_modes: dict[str, str],
) -> list[ApplyPlanViolation]:
    """Return clear dry-run action violations for the resolved node policies."""

    violations: list[ApplyPlanViolation] = []
    for entry in entries:
        if entry.action == "recreated nodes":
            allowed_modes = {clab_kind_policy.RECREATE}
        elif entry.action == "restarted nodes":
            allowed_modes = {clab_kind_policy.RESTART}
        else:
            continue

        for node in entry.nodes:
            mode = node_apply_modes.get(node)
            if mode and mode not in allowed_modes:
                violations.append(
                    ApplyPlanViolation(
                        node=node,
                        action=entry.action,
                        apply_mode=mode,
                        details=entry.details,
                    )
                )
    return violations


def entries_to_dicts(entries: list[ApplyPlanEntry]) -> list[dict[str, object]]:
    """Serialize recognized apply plan entries for dNLab state/status."""

    return [
        {
            "action": entry.action,
            "details": entry.details,
            "nodes": list(entry.nodes),
        }
        for entry in entries
    ]


def entries_summary(entries: list[ApplyPlanEntry]) -> str:
    """Return a compact human-readable summary for CLI tables."""

    return ", ".join(
        f"{entry.action}: {entry.details}" if entry.details else entry.action
        for entry in entries
    )


def dicts_summary(entries: list[dict]) -> str:
    """Return a compact summary from serialized apply plan entries."""

    return ", ".join(
        (
            f"{str(entry.get('action', ''))}: {str(entry.get('details', ''))}"
            if entry.get("details")
            else str(entry.get("action", ""))
        ).strip()
        for entry in entries
        if isinstance(entry, dict) and entry.get("action")
    )


def _normalize_action(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _nodes_for_action(action: str, details: str) -> tuple[str, ...]:
    if action in {"recreated nodes", "restarted nodes", "added nodes", "deleted nodes"}:
        return _nodes_from_node_list(details)
    if action in {"added links", "removed links", "changed links"}:
        return _nodes_from_link_details(details)
    return ()


def _nodes_from_node_list(details: str) -> tuple[str, ...]:
    nodes: list[str] = []
    for part in re.split(r",\s*", details):
        match = re.match(r"([A-Za-z0-9_.-]+)", part.strip())
        if match:
            nodes.append(match.group(1))
    return tuple(dict.fromkeys(nodes))


def _nodes_from_link_details(details: str) -> tuple[str, ...]:
    nodes: list[str] = []
    for endpoint in re.findall(r"([A-Za-z0-9_.-]+):[A-Za-z0-9_.-]+", details):
        nodes.append(endpoint)
    return tuple(dict.fromkeys(nodes))
