from dnlab_multinode.services import clab_capabilities


class FakeClient:
    def __init__(self, version="0.77.0", missing=()):
        self.version = version
        self.missing = set(missing)
        self.commands = []

    def run_no_check(self, command):
        self.commands.append(command)
        if command == "containerlab version --short":
            return 0, self.version, ""
        name = command.removeprefix("containerlab ").removesuffix(" --help")
        return (1, "", "missing") if name in self.missing else (0, "help", "")


def test_probe_accepts_complete_077_runtime():
    capabilities = clab_capabilities.probe(FakeClient())

    assert capabilities.version == "0.77.0"
    assert capabilities.per_host_apply is True


def test_probe_rejects_old_or_incomplete_runtime():
    assert not clab_capabilities.probe(FakeClient(version="0.76.9")).per_host_apply
    assert not clab_capabilities.probe(
        FakeClient(missing={"apply"})
    ).per_host_apply


def test_probe_accepts_077_without_standalone_validate():
    capabilities = clab_capabilities.probe(
        FakeClient(missing={"validate"}),
    )

    assert capabilities.validate is False
    assert capabilities.per_host_apply is True


def test_requested_runtime_mode_defaults_to_per_vd(monkeypatch):
    monkeypatch.delenv(clab_capabilities.RUNTIME_MODE_ENV, raising=False)
    assert clab_capabilities.requested_runtime_mode() == "per-vd"


def test_requested_runtime_mode_accepts_per_host(monkeypatch):
    monkeypatch.setenv(
        clab_capabilities.RUNTIME_MODE_ENV, "per-host-apply",
    )
    assert clab_capabilities.requested_runtime_mode() == "per-host-apply"


def test_requested_runtime_mode_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv(clab_capabilities.RUNTIME_MODE_ENV, "auto")

    try:
        clab_capabilities.requested_runtime_mode()
    except ValueError as exc:
        assert clab_capabilities.RUNTIME_MODE_ENV in str(exc)
        assert "per-host-apply" in str(exc)
    else:
        raise AssertionError("invalid runtime mode must fail explicitly")
