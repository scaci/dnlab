from dnlab_multinode.services.ssh import SSHClient


class RecordingSSHClient(SSHClient):
    def __init__(self):
        super().__init__("127.0.0.1", "root", "/tmp/key", name="test")
        self.commands = []

    def run(self, command: str, timeout: int = 30, check: bool = True) -> str:
        self.commands.append((command, timeout, check))
        return ""


def test_existing_containerlab_commands_quote_topology_paths():
    client = RecordingSSHClient()
    topology = "/tmp/lab with spaces/demo.clab.yml"

    client.deploy_clab(topology, reconfigure=True)
    client.destroy_clab(topology)

    commands = [command for command, _timeout, _check in client.commands]
    quoted_topology = "'/tmp/lab with spaces/demo.clab.yml'"
    assert commands == [
        f"containerlab deploy -t {quoted_topology} --reconfigure",
        f"containerlab destroy -t {quoted_topology} --cleanup",
    ]
