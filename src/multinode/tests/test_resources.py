"""Tests for resource extraction and image cache."""

import json
from pathlib import Path

from dnlab_multinode.services.resources import (
    ResourceError, _parse_launch_py, extract_resources,
)
from dnlab_multinode.models.topology import VDNode
from dnlab_multinode.services.config import parse_topology


def test_parse_smp_string():
    src = '''
    super().__init__(..., ram=8192, smp="4,sockets=1,cores=4,threads=1")
    '''
    cpu, ram_mb = _parse_launch_py(src)
    assert cpu == 4
    assert ram_mb == 8192


def test_parse_smp_int():
    src = '''
    super().__init__(ram=4096, smp=2)
    '''
    cpu, ram_mb = _parse_launch_py(src)
    assert cpu == 2
    assert ram_mb == 4096


def test_parse_ram_only():
    src = 'self.ram = 16384'
    cpu, ram_mb = _parse_launch_py(src)
    assert ram_mb == 16384


def test_parse_defaults_on_empty():
    cpu, ram_mb = _parse_launch_py("# nothing interesting")
    # Defaults
    assert cpu == 2
    assert ram_mb == 4096


def test_cache_hit(tmp_path: Path):
    """When cache is populated, extraction should not be re-run."""
    cache_file = tmp_path / ".image-cache.json"
    cache_file.write_text(json.dumps({
        "fake:latest": {"cpu": 8, "ram_mb": 16384, "extracted_at": "2024-01-01"},
    }))

    result = extract_resources(
        {"node1": "fake:latest"}, cache_dir=tmp_path, no_cache=False,
    )
    assert result["node1"].cpu == 8
    assert result["node1"].ram_mb == 16384


def test_cache_miss_unknown_image(tmp_path: Path, monkeypatch):
    """When image unknown and docker fails, defaults are used."""
    def fake_extract(image):
        return 2, 4096

    import dnlab_multinode.services.resources as res_mod
    monkeypatch.setattr(res_mod, "_extract_from_launch_py", fake_extract)

    result = extract_resources(
        {"node1": "unknown:latest"}, cache_dir=tmp_path, no_cache=False,
    )
    assert result["node1"].cpu == 2
    assert result["node1"].ram_mb == 4096

    # Cache file should have been written
    assert (tmp_path / ".image-cache.json").exists()


def test_resource_schema_env_overrides_image_defaults(tmp_path: Path):
    cache_file = tmp_path / ".image-cache.json"
    cache_file.write_text(json.dumps({
        "fake:latest": {"cpu": 2, "ram_mb": 4096, "extracted_at": "2024-01-01"},
    }))
    nodes = {
        "node1": VDNode(
            name="node1", kind="fake", image="fake:latest",
            env={"MY_CPU": "6", "MY_MEM": "12288"},
        ),
    }
    specs = {
        "node1": {
            "cpu": {"source": "env", "key": "MY_CPU", "type": "int"},
            "ram_mb": {"source": "env", "key": "MY_MEM", "type": "int", "unit": "mb"},
        },
    }

    result = extract_resources(
        {"node1": "fake:latest"},
        cache_dir=tmp_path,
        nodes=nodes,
        resource_specs=specs,
    )

    assert result["node1"].cpu == 6
    assert result["node1"].ram_mb == 12288
    assert result["node1"].cpu_source == "env:MY_CPU"
    assert result["node1"].ram_mb_source == "env:MY_MEM"


def test_resource_schema_allows_same_image_different_node_env(tmp_path: Path):
    cache_file = tmp_path / ".image-cache.json"
    cache_file.write_text(json.dumps({
        "fake:latest": {"cpu": 2, "ram_mb": 4096, "extracted_at": "2024-01-01"},
    }))
    nodes = {
        "small": VDNode(name="small", kind="fake", image="fake:latest", env={"C": "1", "M": "1024"}),
        "large": VDNode(name="large", kind="fake", image="fake:latest", env={"C": "8", "M": "16384"}),
    }
    specs = {
        name: {
            "cpu": {"source": "env", "key": "C", "type": "int"},
            "ram_mb": {"source": "env", "key": "M", "type": "int"},
        }
        for name in nodes
    }

    result = extract_resources(
        {"small": "fake:latest", "large": "fake:latest"},
        cache_dir=tmp_path,
        nodes=nodes,
        resource_specs=specs,
    )

    assert (result["small"].cpu, result["small"].ram_mb) == (1, 1024)
    assert (result["large"].cpu, result["large"].ram_mb) == (8, 16384)


def test_resource_schema_invalid_value_fails_plan(tmp_path: Path):
    cache_file = tmp_path / ".image-cache.json"
    cache_file.write_text(json.dumps({
        "fake:latest": {"cpu": 2, "ram_mb": 4096, "extracted_at": "2024-01-01"},
    }))
    nodes = {
        "node1": VDNode(
            name="node1", kind="fake", image="fake:latest",
            env={"CPU_FIELD": "nope", "MEM_FIELD": "4096"},
        ),
    }
    specs = {
        "node1": {
            "cpu": {"source": "env", "key": "CPU_FIELD", "type": "int"},
            "ram_mb": {"source": "env", "key": "MEM_FIELD", "type": "int"},
        },
    }

    try:
        extract_resources(
            {"node1": "fake:latest"},
            cache_dir=tmp_path,
            nodes=nodes,
            resource_specs=specs,
        )
    except ResourceError as exc:
        assert "node1.cpu" in str(exc)
    else:
        raise AssertionError("ResourceError not raised")


def test_parse_topology_reads_resource_sidecar(tmp_path: Path, monkeypatch):
    from dnlab_multinode.services.hosts_config import (
        HostsConfig, ImageSyncConfig, InfraHost, MgmtDefaults,
    )

    topo_file = tmp_path / "lab.yml"
    topo_file.write_text(
        "name: lab\n"
        "topology:\n"
        "  nodes:\n"
        "    R1:\n"
        "      kind: linux\n"
        "      image: alpine\n"
        "# dnlab-gui-resources: "
        "{\"R1\":{\"cpu\":{\"source\":\"env\",\"key\":\"C\"},\"ram_mb\":{\"source\":\"env\",\"key\":\"M\"}}}\n"
    )
    hosts = HostsConfig(
        master=InfraHost(name="master", host="127.0.0.1", ssh_user="root", ssh_key=""),
        workers={},
        underlay_iface="eth0",
        mgmt_defaults=MgmtDefaults(),
        image_sync=ImageSyncConfig(),
    )

    topo = parse_topology(topo_file, hosts_config=hosts)

    assert topo.resource_specs["R1"]["cpu"]["key"] == "C"
    assert topo.resource_specs["R1"]["ram_mb"]["key"] == "M"


def test_parse_topology_reads_node_features_sidecar(tmp_path: Path):
    from dnlab_multinode.services.hosts_config import (
        HostsConfig, ImageSyncConfig, InfraHost, MgmtDefaults,
    )

    topo_file = tmp_path / "lab.yml"
    topo_file.write_text(
        "name: lab\n"
        "topology:\n"
        "  nodes:\n"
        "    R1:\n"
        "      kind: linux\n"
        "      image: quay.io/frrouting/frr:10.2.6-dnlab\n"
        "# dnlab-gui-node-features: "
        "{\"R1\":{\"frr_daemons\":{\"state\":{\"bgpd\":false},"
        "\"materialize\":{\"type\":\"persist-key-value-bool-file\",\"path\":\"frr/daemons\"}}}}\n"
    )
    hosts = HostsConfig(
        master=InfraHost(name="master", host="127.0.0.1", ssh_user="root", ssh_key=""),
        workers={},
        underlay_iface="eth0",
        mgmt_defaults=MgmtDefaults(),
        image_sync=ImageSyncConfig(),
    )

    topo = parse_topology(topo_file, hosts_config=hosts)

    assert topo.node_features["R1"]["frr_daemons"]["state"]["bgpd"] is False
    assert topo.node_features["R1"]["frr_daemons"]["materialize"]["path"] == "frr/daemons"


def test_parse_topology_reads_node_ids_sidecar(tmp_path: Path):
    from dnlab_multinode.services.hosts_config import (
        HostsConfig, ImageSyncConfig, InfraHost, MgmtDefaults,
    )

    topo_file = tmp_path / "lab.yml"
    topo_file.write_text(
        "name: lab\n"
        "topology:\n"
        "  nodes:\n"
        "    R1:\n"
        "      kind: linux\n"
        "      image: quay.io/frrouting/frr:10.2.6-dnlab\n"
        "# dnlab-gui-node-ids: "
        "{\"R1\":\"11111111-1111-4111-8111-111111111111\"}\n"
    )
    hosts = HostsConfig(
        master=InfraHost(name="master", host="127.0.0.1", ssh_user="root", ssh_key=""),
        workers={},
        underlay_iface="eth0",
        mgmt_defaults=MgmtDefaults(),
        image_sync=ImageSyncConfig(),
    )

    topo = parse_topology(topo_file, hosts_config=hosts)

    assert topo.nodes["R1"].persist_id == "11111111-1111-4111-8111-111111111111"


def test_parse_topology_keeps_dnlab_frr_linux_kind(tmp_path: Path):
    from dnlab_multinode.services.hosts_config import (
        HostsConfig, ImageSyncConfig, InfraHost, MgmtDefaults,
    )

    topo_file = tmp_path / "lab.yml"
    topo_file.write_text(
        "name: lab\n"
        "topology:\n"
        "  nodes:\n"
        "    R1:\n"
        "      kind: linux\n"
        "      image: registry.example/vrnetlab/dnlab_frr:10.6.1-dnlab\n"
        "      env:\n"
        "        CLAB_MGMT_PASSTHROUGH: 'true'\n"
        "# dnlab-gui-node-ids: "
        "{\"R1\":\"11111111-1111-4111-8111-111111111111\"}\n"
        "# dnlab-gui-node-features: "
        "{\"R1\":{\"frr_daemons\":{\"state\":{\"bgpd\":true},"
        "\"materialize\":{\"type\":\"persist-key-value-bool-file\","
        "\"path\":\"frr/daemons\"}}}}\n"
    )
    hosts = HostsConfig(
        master=InfraHost(name="master", host="127.0.0.1", ssh_user="root", ssh_key=""),
        workers={},
        underlay_iface="eth0",
        mgmt_defaults=MgmtDefaults(),
        image_sync=ImageSyncConfig(),
    )

    topo = parse_topology(topo_file, hosts_config=hosts)

    node = topo.nodes["R1"]
    assert node.kind == "linux"
    assert node.persist_id == "11111111-1111-4111-8111-111111111111"
    assert node.env["CLAB_MGMT_PASSTHROUGH"] == "true"
    assert topo.node_features["R1"]["frr_daemons"]["state"]["bgpd"] is True


def test_parse_topology_ignores_unsafe_node_persist_id(tmp_path: Path):
    from dnlab_multinode.services.hosts_config import (
        HostsConfig, ImageSyncConfig, InfraHost, MgmtDefaults,
    )

    topo_file = tmp_path / "lab.yml"
    topo_file.write_text(
        "name: lab\n"
        "topology:\n"
        "  nodes:\n"
        "    R1:\n"
        "      kind: linux\n"
        "      image: quay.io/frrouting/frr:10.2.6-dnlab\n"
        "# dnlab-gui-node-ids: {\"R1\":\"../escape\"}\n"
    )
    hosts = HostsConfig(
        master=InfraHost(name="master", host="127.0.0.1", ssh_user="root", ssh_key=""),
        workers={},
        underlay_iface="eth0",
        mgmt_defaults=MgmtDefaults(),
        image_sync=ImageSyncConfig(),
    )

    topo = parse_topology(topo_file, hosts_config=hosts)

    assert topo.nodes["R1"].persist_id == ""
