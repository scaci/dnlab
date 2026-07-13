import json
from pathlib import Path

from dnlab_multinode.services.clab_kind_policy import (
    LIVE,
    RECREATE,
    RESTART,
    LIVE_KINDS,
    RESTART_KINDS,
    expected_apply_mode,
    policy_for_deploy_kind,
)


def test_container_native_live_kinds_are_explicitly_qualified():
    assert expected_apply_mode("linux") == LIVE
    assert expected_apply_mode("nokia_srlinux") == LIVE
    assert expected_apply_mode("nokia_srsim") == LIVE


def test_ceos_is_restart_qualified():
    policy = policy_for_deploy_kind("arista_ceos")

    assert policy.mode == RESTART
    assert "restart" in policy.reason


def test_vm_and_unknown_kinds_default_to_recreate():
    # The GUI catalog maps nvidia_cumulusvx to deploy_kind generic_vm; the
    # multinode policy works on that resolved deploy kind.
    assert expected_apply_mode("generic_vm") == RECREATE
    assert expected_apply_mode("cisco_n9kv") == RECREATE
    assert expected_apply_mode("mikrotik_ros") == RECREATE
    assert expected_apply_mode("openwrt") == RECREATE
    assert expected_apply_mode("future_kind") == RECREATE


def test_blank_kind_is_conservative():
    policy = policy_for_deploy_kind(None)

    assert policy.deploy_kind == ""
    assert policy.mode == RECREATE


def test_device_catalog_deploy_kinds_are_conservative_until_qualified():
    root = Path(__file__).resolve().parents[3]
    catalog_path = root / "src" / "gui" / "app" / "views" / "static" / "config" / "devices.json"
    catalog = json.loads(catalog_path.read_text())
    deploy_kinds = {
        str(definition.get("deploy_kind") or gui_kind)
        for gui_kind, definition in catalog["kinds"].items()
    }

    assert "generic_vm" in deploy_kinds
    assert "linux" in deploy_kinds

    qualified = LIVE_KINDS | RESTART_KINDS
    unexpected_non_default = {
        kind: expected_apply_mode(kind)
        for kind in deploy_kinds
        if kind not in qualified and expected_apply_mode(kind) != RECREATE
    }

    assert unexpected_non_default == {}
    assert expected_apply_mode("nvidia_cumulusvx") == RECREATE
    assert expected_apply_mode("generic_vm") == RECREATE
