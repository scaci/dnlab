from types import SimpleNamespace

import build_image


def _args(tmp_path, *, source_name="chr-7.19.vmdk", keep_upstream=False):
    source = tmp_path / source_name
    source.write_bytes(b"routeros-disk")
    return SimpleNamespace(
        kind="mikrotik_ros",
        source=str(source),
        vrnetlab_root=str(tmp_path / "vrnetlab"),
        with_persistence=True,
        keep_upstream=keep_upstream,
        force=False,
        dry_run=True,
    )


def test_mikrotik_persistence_build_uses_image_build_patch_recipe(monkeypatch, tmp_path):
    work_dir = tmp_path / "vrnetlab" / "mikrotik" / "routeros"
    work_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(build_image, "_resolve_vrnetlab_dir", lambda _kind, _root: work_dir)
    monkeypatch.setattr(build_image, "image_globs_for", lambda _dir: ["*.vmdk", "*.vdi"])
    monkeypatch.setattr(build_image, "_docker_tag_for", lambda _dir, _name: "vrnetlab/mikrotik_routeros:7.19")
    monkeypatch.setattr(build_image, "_make_build_cmd", lambda _dir: ["make", "docker-image"])
    monkeypatch.setattr(build_image, "_run", lambda cmd, **_kwargs: calls.append(cmd))

    assert build_image.cmd_build(_args(tmp_path)) == 0

    assert [
        build_image.sys.executable,
        str(build_image.APPLY_SCRIPT),
        "mikrotik_ros",
        "vrnetlab/mikrotik_routeros:7.19",
    ] in calls
    assert ["docker", "rmi", "vrnetlab/mikrotik_routeros:7.19"] in calls
    assert not any("--tag-suffix=" in part for call in calls for part in call)


def test_pre_suffixed_vrnetlab_tag_rebuilds_same_tag_through_patch_recipe(monkeypatch, tmp_path):
    work_dir = tmp_path / "vrnetlab" / "mikrotik" / "routeros"
    work_dir.mkdir(parents=True)
    calls: list[list[str]] = []

    monkeypatch.setattr(build_image, "_resolve_vrnetlab_dir", lambda _kind, _root: work_dir)
    monkeypatch.setattr(build_image, "image_globs_for", lambda _dir: ["*.vmdk", "*.vdi"])
    monkeypatch.setattr(
        build_image,
        "_docker_tag_for",
        lambda _dir, _name: "vrnetlab/mikrotik_routeros:7.19-amd64-dnlab",
    )
    monkeypatch.setattr(build_image, "_make_build_cmd", lambda _dir: ["make", "docker-image"])
    monkeypatch.setattr(build_image, "_run", lambda cmd, **_kwargs: calls.append(cmd))

    assert build_image.cmd_build(_args(tmp_path)) == 0

    assert [
        build_image.sys.executable,
        str(build_image.APPLY_SCRIPT),
        "mikrotik_ros",
        "vrnetlab/mikrotik_routeros:7.19-amd64-dnlab",
        "--tag-suffix=",
    ] in calls
    assert [
        "docker",
        "tag",
        "vrnetlab/mikrotik_routeros:7.19-amd64-dnlab",
        "vrnetlab/mikrotik_routeros:7.19-dnlab",
    ] in calls
    assert ["docker", "rmi", "vrnetlab/mikrotik_routeros:7.19-amd64-dnlab"] not in calls
