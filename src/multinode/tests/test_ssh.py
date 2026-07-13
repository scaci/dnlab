from dnlab_multinode.services.ssh import SSHClient


class RecordingSSHClient(SSHClient):
    def __init__(self):
        super().__init__("127.0.0.1", "root", "/tmp/key", name="test")
        self.commands = []

    def run(self, command: str, timeout: int = 30, check: bool = True) -> str:
        self.commands.append((command, timeout, check))
        return ""


def test_containerlab_commands_quote_topology_paths_and_nodes():
    client = RecordingSSHClient()
    topology = "/tmp/lab with spaces/demo.clab.yml"

    client.deploy_clab(topology, reconfigure=True)
    client.validate_clab(topology)
    client.apply_clab(topology, dry_run=True)
    client.lifecycle_clab("restart", topology, "node with spaces")
    client.inspect_clab_interfaces(topology)
    client.inspect_clab(topology)
    client.destroy_clab(topology, keep_mgmt_net=True)

    commands = [command for command, _timeout, _check in client.commands]
    quoted_topology = "'/tmp/lab with spaces/demo.clab.yml'"

    assert commands == [
        f"containerlab deploy -t {quoted_topology} --reconfigure",
        f"containerlab validate -t {quoted_topology}",
        f"containerlab apply -t {quoted_topology} --dry-run",
        f"containerlab restart -t {quoted_topology} --node 'node with spaces'",
        f"containerlab inspect interfaces -t {quoted_topology} --format json",
        f"containerlab inspect -t {quoted_topology} --format json",
        f"containerlab destroy -t {quoted_topology} --cleanup --keep-mgmt-net",
    ]

