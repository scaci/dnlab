from dnlab_multinode.models.schedule import SchedulePlan
from dnlab_multinode.services import netsetup


class FakeSSH:
    def __init__(self):
        self.commands = []

    def run(self, cmd, check=True):
        self.commands.append(cmd)
        return ""


def test_setup_mgmt_infra_allows_bridge_in_docker_user(topo_factory):
    topo = topo_factory(name="demo", num_workers=1)
    plan = SchedulePlan(
        lab_name=topo.name,
        assignments={},
        vrf_table_id=951,
        mgmt_vxlan_id=2851,
    )
    client = FakeSSH()

    netsetup.setup_mgmt_infra(
        topo,
        plan,
        client,
        "master",
        {"master": "10.0.0.10", "worker1": "10.0.0.11"},
    )

    assert any(
        "iptables -C DOCKER-USER -i br-demo "
        "-m comment --comment 'dnlab mgmt demo' -j ACCEPT" in cmd
        and "iptables -I DOCKER-USER 1 -i br-demo" in cmd
        for cmd in client.commands
    )
    assert any(
        "iptables -C DOCKER-USER -o br-demo "
        "-m comment --comment 'dnlab mgmt demo' -j ACCEPT" in cmd
        and "iptables -I DOCKER-USER 1 -o br-demo" in cmd
        for cmd in client.commands
    )


def test_teardown_mgmt_infra_removes_docker_user_bridge_rules():
    client = FakeSSH()

    netsetup.teardown_mgmt_infra("demo", "br-demo", client, "master")

    assert any(
        "iptables -C DOCKER-USER -i br-demo "
        "-m comment --comment 'dnlab mgmt demo' -j ACCEPT" in cmd
        and "iptables -D DOCKER-USER -i br-demo" in cmd
        for cmd in client.commands
    )
    assert any(
        "iptables -C DOCKER-USER -o br-demo "
        "-m comment --comment 'dnlab mgmt demo' -j ACCEPT" in cmd
        and "iptables -D DOCKER-USER -o br-demo" in cmd
        for cmd in client.commands
    )
