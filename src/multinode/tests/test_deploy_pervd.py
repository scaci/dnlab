"""Tests for per-VD deploy orchestration."""

from dnlab_multinode.controllers.deploy import DeployController, DeployError
from dnlab_multinode.models.schedule import CrossHostLink, HostAssignment, SchedulePlan
from dnlab_multinode.models.state import (
    DeploymentState, NodeRuntimeState, RuntimeLinkState,
)
from dnlab_multinode.models.topology import VDNode


class FakeClient:
    def __init__(self, name, fail_on=None):
        self.name = name
        self.fail_on = fail_on or set()
        self.uploads = []
        self.deploys = []
        self.deploy_options = []
        self.destroys = []
        self.runs = []
        self.events = []
        self.validations = []
        self.applies = []

    def upload_text(self, content, remote_path):
        self.events.append(("upload", remote_path))
        self.uploads.append((remote_path, content))

    def connect(self):
        self.events.append(("connect",))

    def close(self):
        self.events.append(("close",))

    def deploy_clab(self, remote_path, *, reconfigure=False):
        self.events.append(("deploy", remote_path, reconfigure))
        self.deploys.append(remote_path)
        self.deploy_options.append((remote_path, reconfigure))
        if remote_path in self.fail_on:
            raise RuntimeError("clab exploded")
        return "ok"

    def destroy_clab(self, remote_path):
        self.events.append(("destroy", remote_path))
        self.destroys.append(remote_path)
        return "ok"

    def validate_clab(self, remote_path):
        self.events.append(("validate", remote_path))
        self.validations.append(remote_path)
        return "valid"

    def apply_clab(self, remote_path, *, dry_run=False):
        self.events.append(("apply", remote_path, dry_run))
        self.applies.append((remote_path, dry_run))
        if remote_path in self.fail_on:
            raise RuntimeError("clab apply exploded")
        return "ok"

    def run(self, command, **kwargs):
        self.runs.append((command, kwargs))
        return ""

    def run_no_check(self, command, **kwargs):
        self.runs.append((command, kwargs))
        if command.startswith("docker inspect"):
            names = command.rsplit(" ", 1)[-1].split()
            return 0, "\n".join("running" for _ in names), ""
        return 0, "", ""


def _controller_for(topo):
    ctrl = DeployController("/tmp/lab.yml")
    ctrl._state = DeploymentState(lab_name=topo.name, topology_file="/tmp/lab.yml")
    ctrl._underlay_ips = {
        host_name: host.host
        for host_name, host in topo.all_hosts.items()
    }
    return ctrl


def test_pervd_deploy_is_sequential_within_each_host(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
            "R3": VDNode(name="R3", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1", "R2"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R3"]),
        },
    )
    ctrl = _controller_for(topo)
    ctrl._clients = {
        "master": FakeClient("master"),
        "worker1": FakeClient("worker1"),
    }

    ctrl._deploy_clab(topo, plan)

    assert ctrl._clients["master"].deploys == [
        "/tmp/dnlab-lab-R1-master.clab.yml",
        "/tmp/dnlab-lab-R2-master.clab.yml",
    ]
    assert ctrl._clients["worker1"].deploys == [
        "/tmp/dnlab-lab-R3-worker1.clab.yml",
    ]
    assert set(ctrl._state.node_runtime) == {"R1", "R2", "R3"}
    assert ctrl._state.node_runtime["R1"].container == "clab-dnlab-lab-R1-R1"
    assert ctrl._state.scheduling["master"].topology_file == ""


def test_mgmt_anchors_deploy_before_vd_microtopologies(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=0,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1", "R2"]),
        },
    )
    ctrl = _controller_for(topo)
    ctrl._clients = {"master": FakeClient("master")}

    ctrl._deploy_mgmt_anchors(topo, plan)
    ctrl._deploy_clab(topo, plan)

    assert ctrl._clients["master"].destroys == []
    assert ctrl._clients["master"].events[:2] == [
        ("upload", "/tmp/dnlab-lab-mgmt-master.clab.yml"),
        ("deploy", "/tmp/dnlab-lab-mgmt-master.clab.yml", True),
    ]
    assert ctrl._clients["master"].deploy_options == [
        ("/tmp/dnlab-lab-mgmt-master.clab.yml", True),
        ("/tmp/dnlab-lab-R1-master.clab.yml", False),
        ("/tmp/dnlab-lab-R2-master.clab.yml", False),
    ]
    assert ctrl._clients["master"].deploys == [
        "/tmp/dnlab-lab-mgmt-master.clab.yml",
        "/tmp/dnlab-lab-R1-master.clab.yml",
        "/tmp/dnlab-lab-R2-master.clab.yml",
    ]
    assert ctrl._state.mgmt_anchors["master"].container == (
        "clab-dnlab-lab-mgmt-master-mgmt-anchor"
    )
    assert ctrl._state.phases_completed[:2] == ["mgmt_anchor", "dnlab"]


def test_per_host_apply_run_skips_mgmt_anchor_phase(topo_factory, monkeypatch):
    topo = topo_factory(num_workers=0)
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1", "R2"]),
        },
    )
    phases = []

    class FakePlanController:
        def __init__(self, *args, **kwargs):
            self.topo = topo

        def run(self):
            return plan

    monkeypatch.setattr(
        "dnlab_multinode.controllers.deploy.PlanController",
        FakePlanController,
    )
    monkeypatch.setattr(
        "dnlab_multinode.controllers.deploy.create_clients",
        lambda hosts: {"master": FakeClient("master")},
    )
    monkeypatch.setattr(
        "dnlab_multinode.controllers.deploy.state_svc.load_state",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dnlab_multinode.controllers.deploy.state_svc.save_state",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "dnlab_multinode.controllers.deploy.persistence_svc.save_placement_history",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        DeployController,
        "_select_runtime_mode",
        lambda self: setattr(self._state, "runtime_mode", "per-host-apply"),
    )

    def fake_phase(self, phase, detail, fn, *args, **kwargs):
        phases.append(phase)
        if phase == "mgmt-anchor":
            raise AssertionError("per-host-apply must not deploy mgmt-anchor")
        return None

    monkeypatch.setattr(DeployController, "_phase", fake_phase)

    state = DeployController("/tmp/lab.yml").run()

    assert state.runtime_mode == "per-host-apply"
    assert "mgmt-anchor" not in phases
    assert "dnlab-deploy" in phases


def test_per_host_apply_has_global_validate_and_dry_run_barriers(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
    )
    ctrl = _controller_for(topo)
    ctrl._state.runtime_mode = "per-host-apply"
    ctrl._clients = {
        "master": FakeClient("master"),
        "worker1": FakeClient("worker1"),
    }

    ctrl._apply_clab_per_host(
        topo,
        plan,
        {"master": "name: lab", "worker1": "name: lab"},
    )

    all_events = (
        ctrl._clients["master"].events + ctrl._clients["worker1"].events
    )
    apply_events = [event for event in all_events if event[0] == "apply"]
    assert sum(event[2] is True for event in apply_events) == 2
    assert sum(event[2] is False for event in apply_events) == 2
    assert ctrl._state.scheduling["master"].topology_file == (
        "/tmp/dnlab-lab-master.clab.yml"
    )
    assert ctrl._state.node_runtime["R1"].container == "clab-lab-R1"
    assert ctrl._state.node_runtime["R2"].container == "clab-lab-R2"
    assert ctrl._state.node_runtime["R1"].apply_mode == "live"
    assert ctrl._state.mgmt_anchors == {}
    assert ctrl._per_host_apply_mutated is True


def test_per_host_apply_does_not_mutate_when_validation_fails(topo_factory):
    topo = topo_factory(
        nodes={"R1": VDNode(name="R1", kind="linux", image="alpine")},
        links=[],
        num_workers=0,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
        },
    )
    ctrl = _controller_for(topo)
    path = "/tmp/dnlab-lab-master.clab.yml"
    ctrl._clients = {"master": FakeClient("master", fail_on={path})}

    # Make validation fail before apply is considered.
    def fail_validate(_):
        raise RuntimeError("invalid kind")

    ctrl._clients["master"].validate_clab = fail_validate
    try:
        ctrl._apply_clab_per_host(topo, plan, {"master": "name: lab"})
    except DeployError:
        pass
    else:
        raise AssertionError("validation failure must abort per-host apply")

    assert ctrl._clients["master"].applies == []


def test_per_host_apply_does_not_mutate_when_dry_run_fails(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
    )
    ctrl = _controller_for(topo)
    worker = FakeClient("worker1")

    def fail_dry_run(remote_path, *, dry_run=False):
        worker.applies.append((remote_path, dry_run))
        if dry_run:
            raise RuntimeError("dry-run rejected topology")
        return "ok"

    worker.apply_clab = fail_dry_run
    ctrl._clients = {"master": FakeClient("master"), "worker1": worker}

    try:
        ctrl._apply_clab_per_host(
            topo,
            plan,
            {"master": "name: lab", "worker1": "name: lab"},
        )
    except DeployError:
        pass
    else:
        raise AssertionError("dry-run failure must abort per-host apply")

    assert ctrl._clients["master"].applies == [
        ("/tmp/dnlab-lab-master.clab.yml", True),
    ]
    assert worker.applies == [("/tmp/dnlab-lab-worker1.clab.yml", True)]
    assert ctrl._state.host_apply_plan == {"master": []}
    assert ctrl._state.host_apply_status == {}
    assert ctrl._state.scheduling == {}
    assert ctrl._state.node_runtime == {}


def test_per_host_apply_rejects_dry_run_actions_outside_kind_policy(topo_factory):
    topo = topo_factory(
        nodes={"R1": VDNode(name="R1", kind="linux", image="alpine")},
        links=[],
        num_workers=0,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
        },
    )
    ctrl = _controller_for(topo)
    master = FakeClient("master")

    def dry_run_with_recreate(remote_path, *, dry_run=False):
        master.applies.append((remote_path, dry_run))
        if dry_run:
            return """
            │ recreated nodes │ R1 (config drift: Env) │
            """
        raise AssertionError("real apply must not run after policy violation")

    master.apply_clab = dry_run_with_recreate
    ctrl._clients = {"master": master}

    try:
        ctrl._apply_clab_per_host(topo, plan, {"master": "name: lab"})
    except DeployError as exc:
        assert "outside dNLab kind policy" in str(exc)
        assert "R1" in str(exc)
    else:
        raise AssertionError("policy violation must abort per-host apply")

    assert master.applies == [("/tmp/dnlab-lab-master.clab.yml", True)]
    assert ctrl._state.host_apply_plan["master"][0]["action"] == "recreated nodes"
    assert ctrl._state.host_apply_plan["master"][0]["nodes"] == ["R1"]
    assert ctrl._state.host_apply_status == {}
    assert ctrl._state.scheduling == {}
    assert ctrl._state.node_runtime == {}


def test_per_host_apply_records_partial_failure_for_retry(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
    )
    ctrl = _controller_for(topo)
    worker = FakeClient("worker1")

    def fail_real_apply(remote_path, *, dry_run=False):
        worker.applies.append((remote_path, dry_run))
        if not dry_run:
            raise RuntimeError("clab apply exploded")
        return "ok"

    worker.apply_clab = fail_real_apply
    ctrl._clients = {"master": FakeClient("master"), "worker1": worker}

    try:
        ctrl._apply_clab_per_host(
            topo,
            plan,
            {"master": "name: lab", "worker1": "name: lab"},
        )
    except DeployError:
        pass
    else:
        raise AssertionError("partial apply must report failure")

    assert ctrl._partial_per_host_apply is True
    assert ctrl._state.reconcile_required is True
    assert ctrl._state.host_apply_status == {
        "master": "applied",
        "worker1": "error",
    }
    assert ctrl._state.node_runtime["R2"].state == "error"


def test_per_host_apply_retry_after_partial_failure_converges(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
    )
    ctrl = _controller_for(topo)
    worker = FakeClient("worker1")
    fail_once = {"enabled": True}

    def fail_first_real_apply(remote_path, *, dry_run=False):
        worker.applies.append((remote_path, dry_run))
        if not dry_run and fail_once["enabled"]:
            fail_once["enabled"] = False
            raise RuntimeError("transient apply failure")
        return "ok"

    worker.apply_clab = fail_first_real_apply
    ctrl._clients = {"master": FakeClient("master"), "worker1": worker}

    try:
        ctrl._apply_clab_per_host(
            topo,
            plan,
            {"master": "name: lab", "worker1": "name: lab"},
        )
    except DeployError:
        pass
    else:
        raise AssertionError("first apply must fail")

    assert ctrl._state.host_apply_status["worker1"] == "error"
    assert ctrl._state.reconcile_required is True
    assert ctrl._state.node_runtime["R2"].state == "error"

    ctrl._apply_clab_per_host(
        topo,
        plan,
        {"master": "name: lab", "worker1": "name: lab"},
    )

    assert ctrl._state.reconcile_required is False
    assert ctrl._state.host_apply_status == {
        "master": "applied",
        "worker1": "applied",
    }
    assert ctrl._state.node_runtime["R2"].state == "starting"
    assert ctrl._state.node_runtime["R2"].last_error == ""


def test_per_host_runtime_links_create_new_cross_host_link(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    cross = CrossHostLink(
        vxlan_id=3001,
        source_node="R1",
        source_iface="eth1",
        target_node="R2",
        target_iface="eth1",
        source_host="master",
        target_host="worker1",
        source_host_iface="r1-e1-vx",
        target_host_iface="r2-e1-vx",
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
        cross_host_links=[cross],
    )
    ctrl = _controller_for(topo)
    ctrl._state.runtime_mode = "per-host-apply"
    ctrl._state.node_runtime = {
        "R1": NodeRuntimeState(
            node="R1", state="starting", host="master", container="clab-lab-R1",
        ),
        "R2": NodeRuntimeState(
            node="R2", state="starting", host="worker1", container="clab-lab-R2",
        ),
    }
    master = FakeClient("master")
    worker = FakeClient("worker1")
    ctrl._clients = {"master": master, "worker1": worker}

    ctrl._deploy_runtime_links(topo, plan)

    assert ctrl._state.runtime_links[0].state == "up"
    assert ctrl._state.reconcile_required is False
    assert any("containerlab tools vxlan create" in cmd for cmd, _ in master.runs)
    assert any("containerlab tools vxlan create" in cmd for cmd, _ in worker.runs)


def test_per_host_runtime_links_delete_stale_cross_host_link(topo_factory):
    topo = topo_factory(num_workers=1)
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=[]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=[]),
        },
    )
    ctrl = _controller_for(topo)
    ctrl._state.runtime_mode = "per-host-apply"
    ctrl._state.runtime_links = [
        RuntimeLinkState(
            id="vx0",
            link_type="cross_host",
            endpoint_a={"node": "R1", "iface": "eth1"},
            endpoint_b={"node": "R2", "iface": "eth1"},
            host_a="master",
            host_b="worker1",
            host_endpoint_a="old-r1-e1",
            host_endpoint_b="old-r2-e1",
            vxlan_id=3001,
            state="up",
        )
    ]
    master = FakeClient("master")
    worker = FakeClient("worker1")
    ctrl._clients = {"master": master, "worker1": worker}

    ctrl._deploy_runtime_links(topo, plan)

    assert ctrl._state.runtime_links == []
    assert ctrl._state.vxlan_dataplane == []
    assert ctrl._state.reconcile_required is False
    assert any("ip link delete vx-old-r1-e1" in cmd for cmd, _ in master.runs)
    assert any("ip link delete vx-old-r2-e1" in cmd for cmd, _ in worker.runs)


def test_per_host_runtime_links_mark_partial_when_endpoint_not_running(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    cross = CrossHostLink(
        vxlan_id=3001,
        source_node="R1",
        source_iface="eth1",
        target_node="R2",
        target_iface="eth1",
        source_host="master",
        target_host="worker1",
        source_host_iface="r1-e1-vx",
        target_host_iface="r2-e1-vx",
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
        cross_host_links=[cross],
    )
    ctrl = _controller_for(topo)
    ctrl._state.runtime_mode = "per-host-apply"
    ctrl._state.node_runtime = {
        "R1": NodeRuntimeState(
            node="R1", state="running", host="master", container="clab-lab-R1",
        ),
        "R2": NodeRuntimeState(
            node="R2", state="starting", host="worker1", container="clab-lab-R2",
        ),
    }
    master = FakeClient("master")
    worker = FakeClient("worker1")

    def worker_status(command, **kwargs):
        worker.runs.append((command, kwargs))
        if command.startswith("docker inspect"):
            return 0, "exited", ""
        return 0, "", ""

    worker.run_no_check = worker_status
    ctrl._clients = {"master": master, "worker1": worker}

    ctrl._deploy_runtime_links(topo, plan)

    assert ctrl._state.runtime_links[0].state == "partial"
    assert "R2" in ctrl._state.runtime_links[0].last_error
    assert ctrl._state.reconcile_required is True
    assert not any("containerlab tools vxlan create" in cmd for cmd, _ in master.runs)


def test_deploy_rejects_implicit_runtime_mode_change_for_deployed_lab(topo_factory):
    topo = topo_factory(num_workers=0)
    ctrl = _controller_for(topo)
    ctrl._previous_state = DeploymentState(
        lab_name=topo.name,
        topology_file="/tmp/lab.yml",
        runtime_mode="per-vd",
    )
    ctrl._state.runtime_mode = "per-host-apply"

    try:
        ctrl._guard_runtime_mode_transition()
    except DeployError as exc:
        assert "Refusing implicit switch" in str(exc)
    else:
        raise AssertionError("deployed lab must not switch runtime mode implicitly")


def test_deploy_allows_runtime_mode_change_for_offline_previous_state(topo_factory):
    topo = topo_factory(num_workers=0)
    ctrl = _controller_for(topo)
    ctrl._previous_state = DeploymentState(
        lab_name=topo.name,
        topology_file="/tmp/lab.yml",
        dnlab_deployed=False,
        runtime_mode="per-vd",
    )
    ctrl._state.runtime_mode = "per-host-apply"

    ctrl._guard_runtime_mode_transition()


def test_requested_per_host_runtime_does_not_fall_back_when_capability_missing(
    topo_factory, monkeypatch,
):
    class OldContainerlabClient:
        def run_no_check(self, command):
            if command == "containerlab version --short":
                return 0, "0.76.9", ""
            return 0, "help", ""

    topo = topo_factory(num_workers=0)
    ctrl = _controller_for(topo)
    ctrl._clients = {"master": OldContainerlabClient()}
    monkeypatch.setenv("DNLAB_CONTAINERLAB_RUNTIME_MODE", "per-host-apply")

    try:
        ctrl._select_runtime_mode()
    except DeployError as exc:
        assert "Refusing fallback to per-vd" in str(exc)
    else:
        raise AssertionError("requested per-host runtime must not fall back")

    assert ctrl._state.runtime_mode == "per-vd"
    assert ctrl._state.containerlab_versions == {"master": "0.76.9"}


def test_mgmt_anchor_partial_failure_is_tracked_for_rollback(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=1,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1"]),
            "worker1": HostAssignment("worker1", "10.0.0.11", vd_names=["R2"]),
        },
    )
    ctrl = _controller_for(topo)
    failing_path = "/tmp/dnlab-lab-mgmt-worker1.clab.yml"
    ctrl._clients = {
        "master": FakeClient("master"),
        "worker1": FakeClient("worker1", fail_on={failing_path}),
    }

    try:
        ctrl._deploy_mgmt_anchors(topo, plan)
    except DeployError as exc:
        msg = str(exc)
        assert "worker1" in msg
        assert failing_path in msg
        assert "clab exploded" in msg
    else:
        raise AssertionError("mgmt anchor failure should raise DeployError")

    assert ctrl._state.phases_completed == ["mgmt_anchor"]
    assert set(ctrl._state.mgmt_anchors) == {"master"}

    ctrl._rollback(topo)

    assert ctrl._clients["master"].runs == [
        (
            "containerlab destroy -t /tmp/dnlab-lab-mgmt-master.clab.yml --cleanup",
            {"check": False},
        )
    ]


def test_pervd_deploy_error_includes_host_vd_and_topology_file(topo_factory):
    topo = topo_factory(
        nodes={
            "R1": VDNode(name="R1", kind="linux", image="alpine"),
            "R2": VDNode(name="R2", kind="linux", image="alpine"),
        },
        links=[],
        num_workers=0,
    )
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={
            "master": HostAssignment("master", "10.0.0.10", vd_names=["R1", "R2"]),
        },
    )
    ctrl = _controller_for(topo)
    failing_path = "/tmp/dnlab-lab-R2-master.clab.yml"
    ctrl._clients = {"master": FakeClient("master", fail_on={failing_path})}

    try:
        ctrl._deploy_clab(topo, plan)
    except DeployError as exc:
        msg = str(exc)
        assert "master" in msg
        assert "R2" in msg
        assert failing_path in msg
        assert "clab exploded" in msg
    else:
        raise AssertionError("deploy failure should raise DeployError")
