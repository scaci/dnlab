from __future__ import annotations

import json

import pytest
import yaml

from dnlab_multinode.models.schedule import HostAssignment, SchedulePlan
from dnlab_multinode.models.state import DeploymentState, NodeRuntimeState, RuntimeLinkState
from dnlab_multinode.models.topology import Link, VDNode
from dnlab_multinode.controllers.node import NodeLifecycleController
from dnlab_multinode.services import warm_links
from dnlab_multinode.services.generator import generate_micro_topology_files
from dnlab_multinode.utils import naming


FRR_IMAGE = "vrnetlab/dnlab_frr:10.6.1-dnlab"


class _ImageClient:
    def __init__(self, rc=0, output="{}"):
        self.rc = rc
        self.output = output
        self.commands = []

    def run_no_check(self, command, timeout=None):
        self.commands.append((command, timeout))
        return self.rc, self.output, ""


def _plan(topo, nodes, links=None):
    return SchedulePlan(
        lab_name=topo.name,
        assignments={"master": HostAssignment("master", "10.0.0.10", vd_names=nodes)},
        local_links=links or [],
    )


def test_certified_frr_micro_topology_precreates_stable_warm_ports(topo_factory):
    nodes = {"R1": VDNode(name="R1", kind="linux", image=FRR_IMAGE)}
    warm_links.apply_image_labels(nodes["R1"], {
        "org.dnlab.capabilities": "warm-links-v1",
        "org.dnlab.warm-links.status": "validated",
        "org.opencontainers.image.base.digest": "repo@sha256:known",
    })
    topo = topo_factory(nodes=nodes, links=[], num_workers=0)

    text = generate_micro_topology_files(topo, _plan(topo, ["R1"]))["master"]["R1"]
    parsed = yaml.safe_load(text)
    node = parsed["topology"]["nodes"]["R1"]

    assert node["env"]["DNLAB_WARM_PORTS"] == "8"
    assert node["env"]["DNLAB_NIC_POLL_INTERVAL"] == "0.05"
    assert len(parsed["topology"]["links"]) == 8
    assert [
        "R1:eth8",
        f"host:{naming.runtime_port_endpoint(topo.name, 'R1', 'eth8')}",
    ] in [item["endpoints"] for item in parsed["topology"]["links"]]


def test_experimental_capability_is_enabled_without_explicit_opt_in(topo_factory):
    node = VDNode(name="ow", kind="openwrt", image="vrnetlab/openwrt:future-dnlab")
    topo = topo_factory(nodes={"ow": node}, links=[], num_workers=0)
    assert warm_links.capacity_for_node(topo, "ow") == 0

    warm_links.apply_image_labels(node, {
        "org.dnlab.capabilities": "warm-links-v1",
        "org.dnlab.warm-links.status": "experimental",
    })
    assert warm_links.capacity_for_node(topo, "ow") == 8


def test_capacity_honours_used_index_override_and_cap(topo_factory):
    nodes = {
        "R1": VDNode(
            name="R1", kind="openwrt", image="vrnetlab/openwrt:future-dnlab",
            env={
                "DNLAB_WARM_PORTS": "12",
                warm_links.IMAGE_STATUS_ENV: "experimental",
            },
        ),
        "R2": VDNode(name="R2", kind="linux", image="alpine"),
    }
    link = Link("R1", "eth20", "R2", "eth1")
    topo = topo_factory(nodes=nodes, links=[link], num_workers=0)
    assert warm_links.capacity_for_node(topo, "R1") == 20

    nodes["R1"].env["DNLAB_WARM_PORTS"] = "65"
    with pytest.raises(ValueError, match="exceeds 64"):
        warm_links.capacity_for_node(topo, "R1")


def test_cluster4_allowlist_is_exact():
    assert "vrnetlab/juniper_vjunos-router_v2:25.4R1.12-dnlab" in warm_links.CLUSTER4_CANDIDATE_IMAGES
    assert "vrnetlab/juniper_vjunos-router_v2:25.4R1.13-dnlab" not in warm_links.CLUSTER4_CANDIDATE_IMAGES
    assert all("vmx" not in image and "vqfx" not in image and "vsrx" not in image
               for image in warm_links.CLUSTER4_CANDIDATE_IMAGES)


def test_mutable_tag_is_not_trusted_without_inspected_labels():
    node = VDNode(name="R1", kind="linux", image=FRR_IMAGE)
    assert not warm_links.is_enabled(node)
    assert warm_links.apply_image_labels(node, {
        "org.dnlab.capabilities": "warm-links-v1",
        "org.dnlab.warm-links.status": "validated",
        "org.opencontainers.image.base.digest": "repo@sha256:known",
    }) == "validated"
    assert warm_links.is_enabled(node)
    assert node.env[warm_links.BASE_DIGEST_ENV] == "sha256:known"


def test_experimental_status_still_requires_capability_label():
    node = VDNode(name="ow", kind="openwrt", image="vrnetlab/openwrt:future-dnlab")
    warm_links.apply_image_labels(node, {})
    assert not warm_links.is_enabled(node)


def test_host_image_inspection_uses_exact_local_oci_labels():
    node = VDNode(name="R1", kind="linux", image=FRR_IMAGE)
    client = _ImageClient(output=json.dumps({
        "org.dnlab.capabilities": "warm-links-v1",
        "org.dnlab.warm-links.status": "validated",
        "org.opencontainers.image.base.digest": "sha256:known",
    }))

    assert warm_links.inspect_image_on_host(node, client) == "validated"
    assert warm_links.status_for_node(node) == "validated"
    assert client.commands[0][1] == 30
    assert "docker image inspect" in client.commands[0][0]


def test_missing_host_image_is_reported_and_never_enabled():
    node = VDNode(
        name="R1", kind="linux", image=FRR_IMAGE,
        env={"DNLAB_WARM_LINKS_EXPERIMENTAL": "true"},
    )

    assert warm_links.inspect_image_on_host(node, _ImageClient(rc=1)) == "missing"
    assert warm_links.status_for_node(node) == "missing"
    assert not warm_links.is_enabled(node)


def test_added_node_plan_preserves_existing_placement_and_classifies_new_link(topo_factory):
    nodes = {
        "old": VDNode(name="old", kind="linux", image=FRR_IMAGE),
        "new": VDNode(name="new", kind="linux", image=FRR_IMAGE),
    }
    link = Link("old", "eth1", "new", "eth1")
    topo = topo_factory(nodes=nodes, links=[link], num_workers=1)
    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.topo = topo
    ctrl.state = DeploymentState(lab_name=topo.name, topology_file="/tmp/lab.yml")
    ctrl.state.node_runtime = {
        "old": NodeRuntimeState(node="old", host="worker1"),
    }

    plan = ctrl._plan_with_added_node("new", "master")

    assert plan.host_for_vd("old") == "worker1"
    assert plan.host_for_vd("new") == "master"
    assert len(plan.cross_host_links) == 1
    assert plan.cross_host_links[0].source_node == "old"


def test_hot_add_creates_management_anchor_on_newly_active_host(
    topo_factory, tmp_path, monkeypatch,
):
    class Client:
        def __init__(self):
            self.uploads = []
            self.deploys = []

        def upload_text(self, content, path):
            self.uploads.append((path, content))

        def deploy_clab(self, path, reconfigure=False):
            self.deploys.append((path, reconfigure))

    topo = topo_factory(
        nodes={"new": VDNode(name="new", kind="linux", image=FRR_IMAGE)},
        links=[], num_workers=1,
    )
    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.topo = topo
    ctrl.state_dir = tmp_path
    ctrl.state = DeploymentState(lab_name=topo.name, topology_file="/tmp/lab.yml")
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=[]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["new"]),
        },
    )
    client = Client()
    monkeypatch.setattr(
        "dnlab_multinode.controllers.node.save_state", lambda *_args: None,
    )

    assert ctrl._ensure_mgmt_anchor("worker1", plan, {"worker1": client}) is True
    assert client.deploys == [
        (naming.mgmt_anchor_topology_file(topo.name, "worker1"), True),
    ]
    assert "worker1" in ctrl.state.mgmt_anchors
    assert ctrl._ensure_mgmt_anchor("worker1", plan, {"worker1": client}) is False
    assert len(client.deploys) == 1


def test_hot_add_resolves_literal_underlay_ips(topo_factory):
    class Client:
        def __init__(self, address):
            self.address = address

        def run_no_check(self, command, timeout=None):
            assert "addr show dev infra" in command
            return 0, self.address, ""

    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.topo = topo_factory(nodes={}, links=[], num_workers=1)
    ctrl.topo.underlay_iface = "infra"

    assert ctrl._resolve_underlay_ips({
        "master": Client("10.255.255.124"),
        "worker1": Client("10.255.255.125"),
    }) == {
        "master": "10.255.255.124",
        "worker1": "10.255.255.125",
    }


def test_new_hot_link_above_boot_capacity_requires_restart(topo_factory):
    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.state = DeploymentState(lab_name="lab", topology_file="/tmp/lab.yml")
    ctrl.state.node_runtime = {
        "R1": NodeRuntimeState(
            node="R1", state="running", warm_ports=8,
            hot_links_status="validated",
        ),
    }
    link = RuntimeLinkState(
        id="l0", link_type="real_net",
        endpoint_a={"node": "R1", "iface": "eth9"},
        endpoint_b={"real_net": "wan"},
    )

    with pytest.raises(Exception, match="restart the node"):
        ctrl._validate_new_hot_link(link)


def test_cold_started_endpoint_does_not_require_warm_capability():
    class Client:
        def run_no_check(self, command, timeout=None):
            return (0, "exists", "") if "rt-cold-e1" in command else (1, "", "")

    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.state = DeploymentState(lab_name="lab", topology_file="/tmp/lab.yml")
    ctrl.state.node_runtime = {
        "hot": NodeRuntimeState(
            node="hot", state="running", warm_ports=8,
            hot_links_status="validated",
        ),
        "cold": NodeRuntimeState(
            node="cold", state="running", warm_ports=0,
            hot_links_status="experimental",
        ),
    }
    link = RuntimeLinkState(
        id="l0", link_type="same_host",
        endpoint_a={"node": "hot", "iface": "eth3"},
        endpoint_b={"node": "cold", "iface": "eth1"},
        host_a="worker1", host_b="worker1",
        host_endpoint_a="wp-hot-e3", host_endpoint_b="rt-cold-e1",
        warm_a=True, warm_b=False,
    )

    ctrl._validate_new_hot_link(link, {"worker1": Client()})


def test_running_non_warm_endpoint_without_host_veth_is_rejected():
    class Client:
        def run_no_check(self, command, timeout=None):
            return 1, "", ""

    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.state = DeploymentState(lab_name="lab", topology_file="/tmp/lab.yml")
    ctrl.state.node_runtime = {
        "R1": NodeRuntimeState(
            node="R1", state="running", hot_links_status="unsupported",
        ),
    }
    link = RuntimeLinkState(
        id="l0", link_type="real_net",
        endpoint_a={"node": "R1", "iface": "eth2"},
        endpoint_b={"real_net": "wan"},
        host_a="worker1", host_endpoint_a="missing-e2",
    )

    with pytest.raises(Exception, match="not enabled for hot links"):
        ctrl._validate_new_hot_link(link, {"worker1": Client()})


def test_legacy_experimental_endpoint_without_warm_ports_requires_restart():
    class Client:
        def run_no_check(self, command, timeout=None):
            return 1, "", ""

    ctrl = NodeLifecycleController.__new__(NodeLifecycleController)
    ctrl.state = DeploymentState(lab_name="lab", topology_file="/tmp/lab.yml")
    ctrl.state.node_runtime = {
        "R1": NodeRuntimeState(
            node="R1", state="running", hot_links_status="experimental",
        ),
    }
    link = RuntimeLinkState(
        id="l0", link_type="real_net",
        endpoint_a={"node": "R1", "iface": "eth2"},
        endpoint_b={"real_net": "wan"},
        host_a="worker1", host_endpoint_a="missing-e2",
    )

    with pytest.raises(Exception, match="only has 0 warm ports"):
        ctrl._validate_new_hot_link(link, {"worker1": Client()})
