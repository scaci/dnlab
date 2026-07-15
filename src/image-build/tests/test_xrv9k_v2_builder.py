from pathlib import Path


LAUNCH = Path("/opt/vrnetlab/cisco/xrv9k_V2/docker/launch.py")
DOCKERFILE = Path("/opt/vrnetlab/cisco/xrv9k_V2/docker/Dockerfile")


def test_xrv9k_v2_builder_separates_bootstrap_and_user_console_without_base_setup():
    source = LAUNCH.read_text(encoding="utf-8")

    assert 'self.xr_console_port = 5002 + self.num if use_ovmf else 5000 + self.num' in source
    assert 'self.user_console_port = 5000 + self.num' in source
    assert 'open("/run/dnlab-console-port", "w", encoding="ascii")' in source
    assert "stream.write(str(self.user_console_port))" in source
    assert "stream.write(str(self.xr_console_port))" not in source
    assert "Creating initial user" not in source
    assert "def apply_config(" not in source
    compile(source, str(LAUNCH), "exec")


def test_xrv9k_v2_healthcheck_allows_first_boot_baking():
    source = DOCKERFILE.read_text(encoding="utf-8")

    assert "--start-period=30m" in source
    assert 'CMD ["/healthcheck.py"]' in source
