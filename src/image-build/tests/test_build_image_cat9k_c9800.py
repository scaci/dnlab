from pathlib import Path

import pytest

import build_image


def _cat_builder(root: Path) -> Path:
    builder = root / "cisco" / "cat9kv_V2"
    builder.mkdir(parents=True)
    (builder / "Makefile").write_text(
        """\
IMAGE_FORMAT=qcow2
IMAGE_GLOB=*.qcow2
VENDOR=Cisco
VERSION=$(shell echo $(IMAGE) | grep -oE '[0-9]{2}\\.[0-9]{2}\\.[0-9]{2}' | head -n1)
version-test:
\t@echo Version: $(VERSION)
""",
        encoding="utf-8",
    )
    return builder


def test_cat9k_and_c9800_resolve_only_v2_builder(tmp_path):
    builder = _cat_builder(tmp_path)
    legacy = tmp_path / "cisco" / "cat9kv"
    legacy.mkdir()

    assert build_image.KIND_VRNETLAB_DIR["cisco_cat9kv"] == ["cisco/cat9kv_V2"]
    assert build_image.KIND_VRNETLAB_DIR["cisco_c9800cl"] == ["cisco/cat9kv_V2"]
    assert build_image.resolve_vrnetlab_dir("cisco_cat9kv", tmp_path) == builder
    assert build_image.resolve_vrnetlab_dir("cisco_c9800cl", tmp_path) == builder


def test_v2_mappings_never_fall_back_to_legacy_directory():
    for candidates in build_image.KIND_VRNETLAB_DIR.values():
        if any("_V2" in candidate for candidate in candidates):
            assert all("_V2" in candidate for candidate in candidates)


@pytest.mark.parametrize("kind", ["cisco_cat9kv", "cisco_c9800cl"])
def test_missing_v2_builder_fails_without_legacy_fallback(tmp_path, kind):
    (tmp_path / "cisco" / "cat9kv").mkdir(parents=True)
    with pytest.raises(SystemExit, match="cat9kv_V2"):
        build_image.resolve_vrnetlab_dir(kind, tmp_path)


def test_cat9k_and_c9800_filename_tags(tmp_path):
    builder = _cat_builder(tmp_path)

    assert build_image._docker_tag_for(
        builder, "cat9kv_prd.17.15.03.qcow2"
    ) == "vrnetlab/cisco_cat9kv_v2:17.15.03"
    assert build_image._docker_tag_for(
        builder, "C9800-CL-universalk9.17.15.05.qcow2"
    ) == "vrnetlab/cisco_c9800cl_v2:17.15.05"


def test_vios_l2_uses_distinct_final_repository():
    raw = "vrnetlab/cisco_vios_v2:L2-20200929"
    assert build_image._patch_source_tag("cisco_vios", raw) == (
        "vrnetlab/cisco_vios_l2_v2:L2-20200929"
    )
