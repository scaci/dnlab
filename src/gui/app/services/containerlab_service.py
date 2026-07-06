"""ContainerLab wrapper: YAML I/O + running-labs inspector.

Historically this module also ran ``clab deploy/destroy`` directly. In
M7 fase 2 those paths moved behind the multinode orchestrator
(:mod:`app.services.multinode_service`); what remains here is:

* **YAML read/write** — serialise/deserialise topology files. All
  callers now pass an explicit :class:`~pathlib.Path`; the previous
  name-based lookup (``<name>.yml`` in ``TOPOLOGIES_DIR``) would
  silently pick the wrong file once two users could have labs with
  the same display name.
* **Running-labs inspector** — ``clab inspect --all`` on the master,
  used only by the home-page "running now" widget.

Position metadata (``_POS_COMMENT_PREFIX``) is preserved as a comment
line appended after the YAML document — a convention the frontend's
topology editor relies on.
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import yaml
from yaml.dumper import SafeDumper

log = logging.getLogger(__name__)

from app.config import settings


class _FlowList(list):
    """Marker subclass so the YAML dumper renders this list in flow style."""
    pass


def _flow_list_representer(dumper: SafeDumper, data: _FlowList):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


SafeDumper.add_representer(_FlowList, _flow_list_representer)
from app.models.lab import Lab, ContainerInfo
from app.models.topology import Topology

# Marker for GUI position data stored as a YAML comment
_POS_COMMENT_PREFIX = "# dnlab-gui-positions: "

# Sidecar Web UI: GUI source of truth for ports to expose via
# ``ports:`` clab al deploy time. Vedi
# /root/dnlab-dev-docs/plans/2026-04-25-webui-ports-clab-native.md.
_WEBUI_COMMENT_PREFIX = "# dnlab-gui-webui: "
_NODE_IDS_COMMENT_PREFIX = "# dnlab-gui-node-ids: "
_NODE_OVERRIDES_COMMENT_PREFIX = "# dnlab-gui-node-overrides: "
_NODE_FEATURES_COMMENT_PREFIX = "# dnlab-gui-node-features: "
_GUI_KINDS_COMMENT_PREFIX = "# dnlab-gui-kinds: "
_RESOURCES_COMMENT_PREFIX = "# dnlab-gui-resources: "


def _gui_node_feature_state_from_sidecar(sidecar: dict[str, dict]) -> dict[str, dict]:
    """Extract GUI state from node-feature sidecar entries.

    New sidecars store ``{feature: {state, materialize}}`` for deploy. Older
    state-only sidecars are accepted for compatibility.
    """
    out: dict[str, dict] = {}
    for node, features in sidecar.items():
        if not isinstance(features, dict):
            continue
        node_state: dict[str, dict] = {}
        for feature_key, payload in features.items():
            if not isinstance(payload, dict):
                continue
            state = payload.get("state")
            if isinstance(state, dict):
                node_state[str(feature_key)] = dict(state)
            else:
                node_state[str(feature_key)] = dict(payload)
        if node_state:
            out[str(node)] = node_state
    return out


class ContainerLabService:
    def __init__(self) -> None:
        self._bin = settings.CONTAINERLAB_BIN

    # ------------------------------------------------------------------
    # Topology file I/O
    # ------------------------------------------------------------------

    def save_topology_to(self, path: Path, topology: Topology) -> Path:
        """Serialize a Topology to the explicit path.

        ``topology.name`` is written as the clab ``name:`` field — the
        caller is responsible for setting it to the netname (derived
        from the lab UUID) before invoking us, so that every identity
        on the wire (container names, bridge, persist dir) stays
        unique across users.
        """
        log.info("Saving topology '%s' → %s (%d nodes, %d links)",
                 topology.name, path, len(topology.nodes), len(topology.links))
        # Normalize GUI node overrides before rendering YAML. Runtime
        # assets/binds are materialized by dnlab-multinode for target
        # host; the source topology only carries the sidecar intent.
        try:
            from app.services import node_overrides
            from app.services import node_override_plugins
            for node in topology.nodes:
                state = (topology.gui_node_overrides_state or {}).get(node.name)
                if state:
                    applied = node_overrides.apply_state(node, state)
                    if applied:
                        topology.gui_node_overrides_state[node.name] = applied
                        plugin = node_override_plugins.for_state(applied, node.kind, node.image)
                        materialize = getattr(plugin, "materialize", None)
                        if callable(materialize):
                            materialize(node, applied, path, topology.name)
                    else:
                        topology.gui_node_overrides_state.pop(node.name, None)
        except Exception as exc:
            log.warning("Unable to materialize node overrides for %s: %s", path, exc)

        clab_dict = topology.to_clab_dict()
        self._ensure_node_ids(topology)

        # Render endpoint lists in YAML flow style: "- [ A:eth1, B:eth2 ]".
        for lk in clab_dict.get("topology", {}).get("links", []):
            if "endpoints" in lk:
                lk["endpoints"] = _FlowList(lk["endpoints"])

        with path.open("w") as fh:
            yaml.dump(clab_dict, fh, Dumper=SafeDumper,
                      default_flow_style=False, sort_keys=False)

        positions = {
            n.name: {"x": round(n.position.x, 1), "y": round(n.position.y, 1)}
            for n in topology.nodes
        }
        if positions:
            with path.open("a") as fh:
                fh.write(f"\n{_POS_COMMENT_PREFIX}{json.dumps(positions)}\n")

        try:
            from app.services import device_catalog
            gui_kinds = {
                n.name: n.kind
                for n in topology.nodes
                if device_catalog.has_deploy_kind_alias(n.kind)
            }
        except Exception:
            gui_kinds = {}
        if gui_kinds:
            with path.open("a") as fh:
                fh.write(f"{_GUI_KINDS_COMMENT_PREFIX}{json.dumps(gui_kinds)}\n")

        # Sidecar Web UI: write ONLY entries associated with nodes
        # ancora esistenti, e omettiamo ``host_port`` (vive nel state
        # multinode; it is sticky across deployments).
        node_names = {n.name for n in topology.nodes}
        webui_state = {
            name: entries
            for name, entries in (topology.gui_webui_state or {}).items()
            if name in node_names and entries
        }
        if webui_state:
            with path.open("a") as fh:
                fh.write(f"{_WEBUI_COMMENT_PREFIX}{json.dumps(webui_state)}\n")

        node_id_state = {
            name: node_id
            for name, node_id in (topology.gui_node_ids_state or {}).items()
            if name in node_names and node_id
        }
        if node_id_state:
            with path.open("a") as fh:
                fh.write(f"{_NODE_IDS_COMMENT_PREFIX}{json.dumps(node_id_state)}\n")

        node_override_state = {
            name: state
            for name, state in (topology.gui_node_overrides_state or {}).items()
            if name in node_names and state
        }
        if node_override_state:
            with path.open("a") as fh:
                fh.write(f"{_NODE_OVERRIDES_COMMENT_PREFIX}{json.dumps(node_override_state)}\n")

        try:
            from app.services import device_catalog
            node_feature_state = {}
            for node in topology.nodes:
                state = (topology.gui_node_features_state or {}).get(node.name)
                sidecar = device_catalog.node_features_sidecar_for_kind(node.kind, state)
                if sidecar:
                    node_feature_state[node.name] = sidecar
        except Exception as exc:
            log.warning("Unable to materialize node features for %s: %s", path, exc)
            node_feature_state = {
                name: state
                for name, state in (topology.gui_node_features_state or {}).items()
                if name in node_names and state
            }
        if node_feature_state:
            topology.gui_node_features_state = node_feature_state
            with path.open("a") as fh:
                fh.write(f"{_NODE_FEATURES_COMMENT_PREFIX}{json.dumps(node_feature_state)}\n")

        try:
            from app.services import device_catalog
            resource_state = {
                n.name: device_catalog.resource_schema(n.kind)
                for n in topology.nodes
                if device_catalog.resource_schema(n.kind)
            }
        except Exception:
            resource_state = {}
        if resource_state:
            with path.open("a") as fh:
                fh.write(f"{_RESOURCES_COMMENT_PREFIX}{json.dumps(resource_state)}\n")

        return path

    def load_topology_from_file(self, path: Path) -> Topology:
        """Load a ContainerLab YAML and return a :class:`Topology`."""
        raw_text = path.read_text()

        gui_positions: dict[str, dict] = {}
        gui_kinds: dict[str, str] = {}
        gui_webui: dict[str, list[dict]] = {}
        gui_node_ids: dict[str, str] = {}
        gui_node_overrides: dict[str, dict] = {}
        gui_node_features: dict[str, dict] = {}
        for line in raw_text.splitlines():
            if line.startswith(_POS_COMMENT_PREFIX):
                try:
                    gui_positions = json.loads(line[len(_POS_COMMENT_PREFIX):])
                except json.JSONDecodeError:
                    pass
            elif line.startswith(_GUI_KINDS_COMMENT_PREFIX):
                try:
                    parsed = json.loads(line[len(_GUI_KINDS_COMMENT_PREFIX):])
                    if isinstance(parsed, dict):
                        gui_kinds = {
                            str(name): str(kind)
                            for name, kind in parsed.items()
                            if kind is not None
                        }
                except json.JSONDecodeError:
                    log.warning("Sidecar gui-kinds malformato in %s", path)
            elif line.startswith(_WEBUI_COMMENT_PREFIX):
                try:
                    parsed = json.loads(line[len(_WEBUI_COMMENT_PREFIX):])
                    if isinstance(parsed, dict):
                        gui_webui = parsed
                except json.JSONDecodeError:
                    log.warning("Sidecar webui malformato in %s", path)
            elif line.startswith(_NODE_IDS_COMMENT_PREFIX):
                try:
                    parsed = json.loads(line[len(_NODE_IDS_COMMENT_PREFIX):])
                    if isinstance(parsed, dict):
                        gui_node_ids = {
                            str(name): str(node_id)
                            for name, node_id in parsed.items()
                            if name and node_id
                        }
                except json.JSONDecodeError:
                    log.warning("Sidecar node-ids malformato in %s", path)
            elif line.startswith(_NODE_OVERRIDES_COMMENT_PREFIX):
                try:
                    parsed = json.loads(line[len(_NODE_OVERRIDES_COMMENT_PREFIX):])
                    if isinstance(parsed, dict):
                        gui_node_overrides = parsed
                except json.JSONDecodeError:
                    log.warning("Sidecar node-overrides malformato in %s", path)
            elif line.startswith(_NODE_FEATURES_COMMENT_PREFIX):
                try:
                    parsed = json.loads(line[len(_NODE_FEATURES_COMMENT_PREFIX):])
                    if isinstance(parsed, dict):
                        gui_node_features = parsed
                except json.JSONDecodeError:
                    log.warning("Sidecar node-features malformato in %s", path)

        data: dict[str, Any] = yaml.safe_load(raw_text) or {}
        data.setdefault("name", path.stem)
        topo = self._parse_clab_dict(data)

        try:
            from app.services import device_catalog
            for node in topo.nodes:
                if node.name in gui_kinds:
                    node.kind = gui_kinds[node.name]
                    continue
                resolved = device_catalog.gui_kind_for_deploy_kind(node.kind, node.image)
                if resolved:
                    node.kind = resolved
        except Exception as exc:
            log.warning("Unable to restore GUI kinds for %s: %s", path, exc)

        try:
            from app.services import node_kind_plugins
            node_kind_plugins.migrate_topology(topo, path)
        except Exception as exc:
            log.warning("Unable to run node kind migrations for %s: %s", path, exc)

        for node in topo.nodes:
            if node.name in gui_positions:
                pos = gui_positions[node.name]
                node.position.x = float(pos.get("x", node.position.x))
                node.position.y = float(pos.get("y", node.position.y))

        # Sidecar webui (source-of-truth post-migrazione).
        if gui_webui:
            topo.gui_webui_state = {
                name: list(entries)
                for name, entries in gui_webui.items()
                if isinstance(entries, list)
            }
        if gui_node_ids:
            topo.gui_node_ids_state = {
                name: node_id
                for name, node_id in gui_node_ids.items()
                if topo.get_node(name)
            }
        self._ensure_node_ids(topo)
        if gui_node_overrides:
            topo.gui_node_overrides_state = {
                name: dict(state)
                for name, state in gui_node_overrides.items()
                if isinstance(state, dict)
            }
        if gui_node_features:
            topo.gui_node_features_state = _gui_node_feature_state_from_sidecar(gui_node_features)

        # Silent migration: vecchio formato ``webui_ports:`` in
        # ``node.extra``. Move to ``gui_webui_state`` IF there is not
        # already an entry from the sidecar (the sidecar wins as newer),
        # poi rimuoviamo la chiave da extra. Al prossimo save il YAML
        # will be clean.
        migrated = 0
        for node in topo.nodes:
            legacy = node.extra.pop("webui_ports", None)
            if not legacy:
                continue
            if topo.gui_webui_state.get(node.name):
                continue  # sidecar already canonical
            entries = []
            for e in legacy:
                if not isinstance(e, dict):
                    continue
                cport = e.get("port")
                if not cport:
                    continue
                entries.append({
                    "container_port": int(cport),
                    "scheme": e.get("scheme", "https"),
                    "path":   e.get("path", "/"),
                    "label":  e.get("label", ""),
                    "source": "user",
                })
            if entries:
                topo.gui_webui_state[node.name] = entries
                migrated += 1
        if migrated:
            log.info(
                "Topology %s: migrated legacy webui_ports for %d node(s) → "
                "gui_webui_state sidecar (next save cleans the YAML)",
                path, migrated,
            )

        return topo

    @staticmethod
    def _ensure_node_ids(topology: Topology) -> None:
        """Assign stable UUIDs to deployable VD nodes that do not have one."""
        current = {
            node.name: str((topology.gui_node_ids_state or {}).get(node.name) or uuid.uuid4())
            for node in topology.nodes
            if node.kind != "_real_net"
        }
        topology.gui_node_ids_state = current

    # ------------------------------------------------------------------
    # Running-labs inspector
    # ------------------------------------------------------------------

    async def inspect_all(self) -> list[Lab]:
        ok, out = await self._run(self._bin, "inspect", "--all", "--format", "json")
        if not ok:
            return []
        return self._parse_inspect_json(out)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _run(*cmd: str) -> tuple[bool, str]:
        log.debug("Exec: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        if not out:
            out = err
        log.debug("Exec exit=%d stdout_len=%d stderr_len=%d",
                  proc.returncode, len(stdout), len(stderr))
        if proc.returncode != 0 and err:
            log.debug("Exec stderr: %s", err[:500])
        return proc.returncode == 0, out

    @staticmethod
    def _parse_inspect_json(raw: str) -> list[Lab]:
        """Parse the JSON output of `containerlab inspect` (format v0.74).

        Output format: ``{ "<lab_name>": [ {container}, ... ], ... }``.
        ``<lab_name>`` here is the netname on disk (``name:`` in YAML).
        """
        labs: dict[str, Lab] = {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        if not isinstance(data, dict):
            return []

        for lab_name, containers_raw in data.items():
            if not isinstance(containers_raw, list):
                continue

            if lab_name not in labs:
                labs[lab_name] = Lab(name=lab_name, status="running")

            for c in containers_raw:
                full_name = c.get("name", "")
                prefix = f"clab-{lab_name}-"
                node_name = full_name[len(prefix):] if full_name.startswith(prefix) else full_name

                labs[lab_name].containers.append(ContainerInfo(
                    name=full_name,
                    container_id=c.get("container_id", "")[:12],
                    image=c.get("image", ""),
                    kind=c.get("kind", ""),
                    state=c.get("state", ""),
                    ipv4_address=c.get("ipv4_address", "").split("/")[0],
                    ipv6_address=c.get("ipv6_address", "").split("/")[0],
                    lab_name=lab_name,
                    node_name=node_name,
                ))

        return list(labs.values())

    @staticmethod
    def _parse_clab_dict(data: dict[str, Any]) -> Topology:
        from app.models.node import Node, NodePosition
        from app.models.link import Link

        name = data.get("name", "unnamed")
        topo = data.get("topology", {})
        raw_nodes = topo.get("nodes", {}) or {}
        raw_links = topo.get("links", []) or []

        nodes = []
        for idx, (node_name, node_data) in enumerate(raw_nodes.items()):
            node_data = node_data or {}
            col = idx % 4
            row = idx // 4
            kind = node_data.get("kind", "linux")
            if kind == "_real_net":
                raw_extra = node_data.get("extra")
                if isinstance(raw_extra, dict):
                    extra = dict(raw_extra)
                else:
                    extra = {
                        k: v for k, v in node_data.items()
                        if k not in ("kind", "image")
                    }
            else:
                extra = {
                    k: v for k, v in node_data.items()
                    if k not in ("kind", "image")
                }
            nodes.append(
                Node(
                    name=node_name,
                    kind=kind,
                    image=node_data.get("image", ""),
                    position=NodePosition(x=150.0 + col * 180.0, y=150.0 + row * 160.0),
                    extra=extra,
                )
            )

        links = []
        for lk in raw_links:
            endpoints = lk.get("endpoints", [])
            if len(endpoints) == 2:
                a_node, a_iface = (endpoints[0].split(":", 1) + [""])[:2]
                b_node, b_iface = (endpoints[1].split(":", 1) + [""])[:2]
                links.append(
                    Link(
                        source=a_node,
                        source_iface=a_iface,
                        target=b_node,
                        target_iface=b_iface,
                    )
                )

        extra = {k: v for k, v in data.items() if k not in ("name", "topology")}
        # Silent migration: drop legacy user-supplied mgmt.network/mgmt.bridge.
        # Names are now derived deterministically from the lab UUID.
        mgmt_block = extra.get("mgmt")
        if isinstance(mgmt_block, dict):
            mgmt_block.pop("network", None)
            mgmt_block.pop("bridge", None)
            if not mgmt_block:
                extra.pop("mgmt", None)
        return Topology(name=name, nodes=nodes, links=links, extra=extra)
