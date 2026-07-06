from dataclasses import fields

from dnlab_multinode.services import paths


def test_paths_include_dockerization_keys_without_syslog_mount():
    names = {field.name for field in fields(paths.Paths)}

    assert "gui_dir" in names
    assert "multinode_dir" in names
    assert "image_build_dir" in names
    assert "vrnetlab_dir" in names
    assert "image_build_workspace" in names
    assert "syslog_mount" not in names

    for key in (
        "gui_dir",
        "multinode_dir",
        "image_build_dir",
        "vrnetlab_dir",
        "image_build_workspace",
    ):
        assert key in paths._DEFAULTS
