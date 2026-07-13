"""Per-kind patch plan for mikrotik_ros (vrnetlab RouterOS/CHR).

Applies persist-overlay in ``/launch.py``. RouterOS uses vmdk/vdi input
images and the base vrnetlab VM creates a qcow2 writable overlay next to
that disk. The patch moves/creates that overlay under ``/persist`` when
mounted and rewrites QEMU args to boot from the persistent path.
"""

from __future__ import annotations


KIND = "mikrotik_ros"

FILES = [
    "/launch.py",
]

_MARKER_V1 = "# dnlab-patched: mikrotik-persist-overlay-v1"
_MARKER = "# dnlab-patched: mikrotik-persist-overlay-v2"
_CONFIG_ANCHOR = 'CONFIG_FILE = "/ftpboot/config.auto.rsc"\n'
_CONFIG_REPLACEMENT = f'{_CONFIG_ANCHOR}PERSIST_DIR = "/persist"\n'

_TRACE_ANCHOR = '''logging.Logger.trace = trace
'''
_LEGACY_HELPER_ANCHOR = '''def overlay_image_for(disk_image):
'''

_HELPERS = '''logging.Logger.trace = trace


# dnlab-patched: mikrotik-persist-overlay-v2
def overlay_image_for(disk_image):
    return re.sub(r"(\\.[^.]+$)", r"-overlay\\1", disk_image)


def prepare_persistent_overlay(vm, disk_image):
    """Use /persist for the RouterOS writable overlay when mounted."""
    if not os.path.isdir(PERSIST_DIR):
        vm.logger.warning("/persist not mounted; RouterOS overlay remains ephemeral")
        return

    os.makedirs(PERSIST_DIR, exist_ok=True)
    overlay_img = overlay_image_for(disk_image)
    persistent_overlay = os.path.join(PERSIST_DIR, os.path.basename(overlay_img))

    initialized_marker = persistent_overlay + ".dnlab-initialized"
    vm._dnlab_persistent_overlay_marker = initialized_marker
    vm._dnlab_persistent_overlay_reused = os.path.exists(initialized_marker)
    if not os.path.exists(persistent_overlay):
        if os.path.exists(overlay_img):
            vrnetlab.run_command(["mv", overlay_img, persistent_overlay])
            vm.logger.info("Moved RouterOS overlay to %s", persistent_overlay)
        else:
            vrnetlab.run_command([
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                vm._overlay_disk_image_format(),
                "-b",
                disk_image,
                persistent_overlay,
            ])
            vm.logger.info("Created RouterOS overlay at %s", persistent_overlay)

    if os.path.exists(overlay_img) and not os.path.islink(overlay_img):
        os.remove(overlay_img)
    if not os.path.islink(overlay_img) or os.readlink(overlay_img) != persistent_overlay:
        if os.path.islink(overlay_img):
            os.remove(overlay_img)
        os.symlink(persistent_overlay, overlay_img)

    for idx, arg in enumerate(vm.qemu_args):
        if f"file={overlay_img}" in arg:
            vm.qemu_args[idx] = arg.replace(
                f"file={overlay_img}",
                f"file={persistent_overlay}",
            )
            vm.logger.info("RouterOS persistence enabled via %s", persistent_overlay)
            break
'''

_SUPER_ANCHOR = '''        super(ROS_vm, self).__init__(username, password, disk_image=disk_image, ram=ram_size, driveif="virtio", arch=arch, **extra_args)
'''
_SUPER_REPLACEMENT = '''        super(ROS_vm, self).__init__(username, password, disk_image=disk_image, ram=ram_size, driveif="virtio", arch=arch, **extra_args)
        prepare_persistent_overlay(self, disk_image)
'''

_LOGIN_EXPECT_ANCHOR = '''        (ridx, match, res) = self.tn.expect([b"MikroTik Login", b"RouterOS Login"], 1)
'''
_LOGIN_EXPECT_REPLACEMENT = '''        (ridx, match, res) = self.tn.expect(
            [b"MikroTik Login", b"RouterOS Login", rb"[^\\r\\n]+ Login"], 1
        )
'''
_LOGIN_MATCH_ANCHOR = '''            if ridx in (0, 1):  # login
'''
_LOGIN_MATCH_REPLACEMENT = '''            if ridx in (0, 1, 2):  # login
'''
_LOGIN_WRITE_ANCHOR = '''                elif ridx == 1:
                    self.wait_write("admin+ct", wait="RouterOS Login: ")
                self.wait_write("", wait="Password: ")
'''
_LOGIN_WRITE_REPLACEMENT = '''                elif ridx == 1:
                    self.wait_write("admin+ct", wait="RouterOS Login: ")
                else:
                    # A reused persistent overlay displays the configured
                    # system identity instead of the vendor login prompt.
                    self.wait_write("admin+ct", wait=" Login: ")
                self.wait_write(
                    self.password
                    if getattr(self, "_dnlab_persistent_overlay_reused", False)
                    else "",
                    wait="Password: ",
                )
'''
_LICENSE_ANCHOR = '''                if self.arch != "aarch64":
'''
_LICENSE_REPLACEMENT = '''                if self.arch != "aarch64" and not getattr(
                    self, "_dnlab_persistent_overlay_reused", False
                ):
'''
_BOOTSTRAP_DONE_ANCHOR = '''        self.logger.info("completed bootstrap configuration")
'''
_BOOTSTRAP_DONE_REPLACEMENT = '''        self.logger.info("completed bootstrap configuration")
        persistent_marker = getattr(self, "_dnlab_persistent_overlay_marker", None)
        if persistent_marker:
            with open(persistent_marker, "a", encoding="utf-8"):
                pass
'''
_BOOTSTRAP_CALL_ANCHOR = '''                # run main config!
                self.bootstrap_config()
'''
_BOOTSTRAP_CALL_REPLACEMENT = '''                # A persistent RouterOS disk already contains identity,
                # credentials and management configuration. Reapplying the
                # first-boot commands can hang on duplicate resources.
                if getattr(self, "_dnlab_persistent_overlay_reused", False):
                    self.logger.info(
                        "persistent RouterOS overlay reused; skipping bootstrap configuration"
                    )
                else:
                    self.bootstrap_config()
'''


def _patch_bootstrap(text: str) -> tuple[str, bool]:
    replacements = (
        (_LOGIN_EXPECT_ANCHOR, _LOGIN_EXPECT_REPLACEMENT),
        (_LOGIN_MATCH_ANCHOR, _LOGIN_MATCH_REPLACEMENT),
        (_LOGIN_WRITE_ANCHOR, _LOGIN_WRITE_REPLACEMENT),
        (_LICENSE_ANCHOR, _LICENSE_REPLACEMENT),
        (_BOOTSTRAP_DONE_ANCHOR, _BOOTSTRAP_DONE_REPLACEMENT),
        (_BOOTSTRAP_CALL_ANCHOR, _BOOTSTRAP_CALL_REPLACEMENT),
    )
    for old, new in replacements:
        if new in text:
            continue
        text, ok = _patch_once(text, old, new)
        if not ok:
            return text, False
    return text, True


def _patch_once(text: str, old: str, new: str) -> tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, 1), True


def _patch_routeros_launch(text: str) -> tuple[str, bool]:
    if _MARKER in text:
        return text, True

    legacy_patched = (
        _MARKER_V1 in text
        or (
            "PERSIST_DIR = \"/persist\"" in text
            and "def prepare_persistent_overlay(vm, disk_image):" in text
            and "prepare_persistent_overlay(self, disk_image)" in text
        )
    )
    if legacy_patched:
        if _MARKER_V1 in text:
            new_text = text.replace(_MARKER_V1, _MARKER, 1)
        else:
            new_text, ok = _patch_once(
                text, _LEGACY_HELPER_ANCHOR, f"{_MARKER}\n{_LEGACY_HELPER_ANCHOR}"
            )
            if not ok:
                return text, False
        old = "    if not os.path.exists(persistent_overlay):\n"
        new = (
            '    initialized_marker = persistent_overlay + ".dnlab-initialized"\n'
            "    vm._dnlab_persistent_overlay_marker = initialized_marker\n"
            "    vm._dnlab_persistent_overlay_reused = "
            "os.path.exists(initialized_marker)\n"
            "    if not os.path.exists(persistent_overlay):\n"
        )
        new_text, ok = _patch_once(new_text, old, new)
        if not ok:
            return text, False
        return _patch_bootstrap(new_text)

    new_text, ok = _patch_once(text, _CONFIG_ANCHOR, _CONFIG_REPLACEMENT)
    if not ok:
        return text, False

    new_text, ok = _patch_once(new_text, _TRACE_ANCHOR, _HELPERS)
    if not ok:
        return text, False

    new_text, ok = _patch_once(new_text, _SUPER_ANCHOR, _SUPER_REPLACEMENT)
    if not ok:
        return text, False

    return _patch_bootstrap(new_text)


def apply(path: str, text: str) -> tuple[str, list[str]]:
    new_text, ok = _patch_routeros_launch(text)
    if not ok:
        raise RuntimeError(
            f"{path}: RouterOS persistence anchors not found. Upstream launch.py "
            "likely changed; update patches/mikrotik_ros.py anchors."
        )
    if new_text != text:
        return new_text, [f"{path}: mikrotik persist-overlay v2 applied"]
    return new_text, [f"{path}: already patched"]
