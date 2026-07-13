from __future__ import annotations

import importlib.util
from pathlib import Path


PATCH_PATH = Path(__file__).parents[1] / "patches" / "mikrotik_ros.py"
SPEC = importlib.util.spec_from_file_location("dnlab_mikrotik_patch", PATCH_PATH)
assert SPEC and SPEC.loader
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


UPSTREAM = '''CONFIG_FILE = "/ftpboot/config.auto.rsc"

logging.Logger.trace = trace

class ROS_vm:
    def __init__(self, username, password, disk_image, ram_size, arch, **extra_args):
        super(ROS_vm, self).__init__(username, password, disk_image=disk_image, ram=ram_size, driveif="virtio", arch=arch, **extra_args)

    def bootstrap_spin(self):
        (ridx, match, res) = self.tn.expect([b"MikroTik Login", b"RouterOS Login"], 1)
        if match:
            if ridx in (0, 1):  # login
                if ridx == 0:
                    self.wait_write("admin+ct", wait="MikroTik Login: ")
                elif ridx == 1:
                    self.wait_write("admin+ct", wait="RouterOS Login: ")
                self.wait_write("", wait="Password: ")

                if self.arch != "aarch64":
                    self.wait_write("n", wait="Do you want to see the software license? [Y/n]: ")

                # run main config!
                self.bootstrap_config()

    def bootstrap_config(self):
        self.logger.info("completed bootstrap configuration")
'''


def test_patch_tracks_reused_overlay_and_supports_persisted_login() -> None:
    patched, messages = PATCH.apply("/launch.py", UPSTREAM)

    assert "mikrotik-persist-overlay-v2" in patched
    assert "_dnlab_persistent_overlay_reused" in patched
    assert ".dnlab-initialized" in patched
    assert "_dnlab_persistent_overlay_marker" in patched
    assert "skipping bootstrap configuration" in patched
    assert 'rb"[^\\r\\n]+ Login"' in patched
    assert 'self.wait_write("admin+ct", wait=" Login: ")' in patched
    assert "self.password" in patched
    assert messages == ["/launch.py: mikrotik persist-overlay v2 applied"]

    again, messages = PATCH.apply("/launch.py", patched)
    assert again == patched
    assert messages == ["/launch.py: already patched"]


def test_patch_upgrades_v1_output() -> None:
    v2, _ = PATCH.apply("/launch.py", UPSTREAM)
    v1 = v2.replace(
        "# dnlab-patched: mikrotik-persist-overlay-v2",
        "# dnlab-patched: mikrotik-persist-overlay-v1",
        1,
    ).replace(
        '    initialized_marker = persistent_overlay + ".dnlab-initialized"\n'
        "    vm._dnlab_persistent_overlay_marker = initialized_marker\n"
        "    vm._dnlab_persistent_overlay_reused = os.path.exists(initialized_marker)\n"
        "    if not os.path.exists(persistent_overlay):\n",
        "    if not os.path.exists(persistent_overlay):\n",
        1,
    )
    for replacement, anchor in (
        (PATCH._LOGIN_EXPECT_REPLACEMENT, PATCH._LOGIN_EXPECT_ANCHOR),
        (PATCH._LOGIN_MATCH_REPLACEMENT, PATCH._LOGIN_MATCH_ANCHOR),
        (PATCH._LOGIN_WRITE_REPLACEMENT, PATCH._LOGIN_WRITE_ANCHOR),
        (PATCH._LICENSE_REPLACEMENT, PATCH._LICENSE_ANCHOR),
        (PATCH._BOOTSTRAP_DONE_REPLACEMENT, PATCH._BOOTSTRAP_DONE_ANCHOR),
        (PATCH._BOOTSTRAP_CALL_REPLACEMENT, PATCH._BOOTSTRAP_CALL_ANCHOR),
    ):
        v1 = v1.replace(replacement, anchor, 1)

    upgraded, _ = PATCH.apply("/launch.py", v1)

    assert "mikrotik-persist-overlay-v1" not in upgraded
    assert "mikrotik-persist-overlay-v2" in upgraded
    assert "_dnlab_persistent_overlay_reused" in upgraded
    assert 'rb"[^\\r\\n]+ Login"' in upgraded


def test_patch_upgrades_unmarked_legacy_output_without_duplication() -> None:
    v2, _ = PATCH.apply("/launch.py", UPSTREAM)
    legacy = v2.replace(
        "# dnlab-patched: mikrotik-persist-overlay-v2\n", "", 1
    ).replace(
        '    initialized_marker = persistent_overlay + ".dnlab-initialized"\n'
        "    vm._dnlab_persistent_overlay_marker = initialized_marker\n"
        "    vm._dnlab_persistent_overlay_reused = os.path.exists(initialized_marker)\n"
        "    if not os.path.exists(persistent_overlay):\n",
        "    if not os.path.exists(persistent_overlay):\n",
        1,
    )
    for replacement, anchor in (
        (PATCH._LOGIN_EXPECT_REPLACEMENT, PATCH._LOGIN_EXPECT_ANCHOR),
        (PATCH._LOGIN_MATCH_REPLACEMENT, PATCH._LOGIN_MATCH_ANCHOR),
        (PATCH._LOGIN_WRITE_REPLACEMENT, PATCH._LOGIN_WRITE_ANCHOR),
        (PATCH._LICENSE_REPLACEMENT, PATCH._LICENSE_ANCHOR),
        (PATCH._BOOTSTRAP_DONE_REPLACEMENT, PATCH._BOOTSTRAP_DONE_ANCHOR),
        (PATCH._BOOTSTRAP_CALL_REPLACEMENT, PATCH._BOOTSTRAP_CALL_ANCHOR),
    ):
        legacy = legacy.replace(replacement, anchor, 1)

    upgraded, _ = PATCH.apply("/launch.py", legacy)

    assert upgraded.count("PERSIST_DIR = \"/persist\"") == 1
    assert upgraded.count("def prepare_persistent_overlay(vm, disk_image):") == 1
    assert upgraded.count("prepare_persistent_overlay(self, disk_image)") == 1
    assert "mikrotik-persist-overlay-v2" in upgraded
    assert ".dnlab-initialized" in upgraded
