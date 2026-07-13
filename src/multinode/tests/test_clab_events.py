from __future__ import annotations

from types import SimpleNamespace

from dnlab_multinode.models.state import DeploymentState, NodeRuntimeState
from dnlab_multinode.services import clab_events


def test_normalise_event_maps_container_to_node():
    event = clab_events.normalise_event(
        {
            "ContainerName": "clab-demo-r1",
            "Action": "start",
            "State": "running",
        },
        lab_name="demo",
        host="worker1",
        container_to_node={"clab-demo-r1": "r1"},
    )

    assert event is not None
    assert event["host"] == "worker1"
    assert event["status"] == "ok"
    assert "r1" in event["detail"]
    assert event["data"]["node"] == "r1"
    assert event["data"]["container"] == "clab-demo-r1"


def test_normalise_event_keeps_interface_context():
    event = clab_events.normalise_event(
        {
            "Node": "r1",
            "Interface": "eth1",
            "Peer": "r2:eth1",
            "Event": "link-up",
        },
        lab_name="demo",
        host="worker1",
        container_to_node={},
    )

    assert event is not None
    assert event["status"] == "progress"
    assert event["data"]["interface"] == "eth1"
    assert event["data"]["peer"] == "r2:eth1"
    assert "peer=r2:eth1" in event["detail"]


def test_normalise_event_prefers_containerlab_labels():
    event = clab_events.normalise_event(
        {
            "ContainerName": "docker-name",
            "Action": "start",
            "labels": {
                "clab-lab-name": "demo",
                "clab-node-name": "r1",
            },
        },
        lab_name="demo",
        host="worker1",
        container_to_node={"docker-name": "fallback"},
    )

    assert event is not None
    assert event["data"]["node"] == "r1"
    assert event["data"]["event_lab"] == "demo"
    assert event["data"]["labels"]["clab-node-name"] == "r1"


def test_event_records_accepts_common_wrappers():
    payload = {"events": [{"Event": "start"}, {"Event": "stop"}]}

    assert [row["Event"] for row in clab_events._event_records(payload)] == [
        "start",
        "stop",
    ]


def test_inspect_nodes_resolves_container_names():
    payload = {
        "containers": [
            {"name": "clab-demo-r1", "state": "running"},
            {"name": "clab-demo-r2", "state": "running"},
        ]
    }

    assert clab_events._inspect_nodes(
        payload,
        {"r1", "r2"},
        {"clab-demo-r1": "r1", "clab-demo-r2": "r2"},
    ) == {"r1", "r2"}


def test_inspect_nodes_resolves_flat_containerlab_labels():
    payload = {
        "containers": [
            {
                "name": "opaque",
                "label.clab-node-name": "r1",
                "label.clab-lab-name": "demo",
            }
        ]
    }

    assert clab_events._inspect_nodes(payload, {"r1"}, {}) == {"r1"}


def test_interface_counts_accepts_nested_node_maps():
    payload = {
        "r1": {
            "interfaces": [
                {"interface": "eth1", "state": "up"},
                {"interface": "eth2", "state": "down"},
            ]
        },
        "r2": {"interfaces": [{"ifname": "eth1"}]},
    }

    assert clab_events._interface_counts(payload, {"r1", "r2"}, {}) == {
        "r1": 2,
        "r2": 1,
    }


def test_watcher_resyncs_on_each_stream_reconnect(monkeypatch):
    published = []
    watcher_ref = {}

    class FakeSSHClient:
        streams = 0

        def __init__(self, **kwargs):
            pass

        def connect(self):
            pass

        def close(self):
            pass

        def inspect_clab(self, topology_file):
            return '{"containers":[{"name":"clab-demo-r1","state":"running"}]}'

        def inspect_clab_interfaces(self, topology_file):
            return '{"r1":{"interfaces":[{"interface":"eth1"}]}}'

        def stream_clab_events(self, topology_file, *, stop_event, on_line, window_seconds):
            type(self).streams += 1
            on_line('{"ContainerName":"clab-demo-r1","Action":"start","State":"running"}')
            if type(self).streams >= 2:
                watcher_ref["watcher"].stop()
            return 124, ""

    monkeypatch.setattr(clab_events, "SSHClient", FakeSSHClient)
    state = DeploymentState(lab_name="demo", topology_file="/tmp/demo.yml")
    state.node_runtime = {
        "r1": NodeRuntimeState(
            node="r1",
            host="worker1",
            container="clab-demo-r1",
            topology_file="/tmp/demo.clab.yml",
        )
    }
    watcher = clab_events._LabEventsWatcher(
        key="topic:demo",
        lab_name="demo",
        topic="topic",
        topology_by_host={"worker1": "/tmp/demo.clab.yml"},
        hosts={
            "worker1": SimpleNamespace(
                host="127.0.0.1",
                ssh_user="root",
                ssh_key="/tmp/key",
            )
        },
        state=state,
        publish=published.append,
        window_seconds=5,
        reconnect_delay_seconds=0,
    )
    watcher_ref["watcher"] = watcher

    watcher._run_host("worker1", watcher.hosts["worker1"], "/tmp/demo.clab.yml")

    resync_events = [
        event for event in published
        if event["data"].get("event") == "resync"
    ]
    runtime_events = [
        event for event in published
        if event["data"].get("event") == "start"
    ]
    assert len(resync_events) == 2
    assert resync_events[0]["data"]["interfaces_by_node"] == {"r1": 1}
    assert len(runtime_events) == 1
