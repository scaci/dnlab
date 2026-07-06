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

_MARKER = "# dnlab-patched: mikrotik-persist-overlay-v1"
_CONFIG_ANCHOR = 'CONFIG_FILE = "/ftpboot/config.auto.rsc"\n'
_CONFIG_REPLACEMENT = f'{_CONFIG_ANCHOR}PERSIST_DIR = "/persist"\n'

_TRACE_ANCHOR = '''logging.Logger.trace = trace
'''

_HELPERS = '''logging.Logger.trace = trace


# dnlab-patched: mikrotik-persist-overlay-v1
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


def _patch_once(text: str, old: str, new: str) -> tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, 1), True


def _patch_routeros_launch(text: str) -> tuple[str, bool]:
    if _MARKER in text:
        return text, True

    new_text, ok = _patch_once(text, _CONFIG_ANCHOR, _CONFIG_REPLACEMENT)
    if not ok:
        return text, False

    new_text, ok = _patch_once(new_text, _TRACE_ANCHOR, _HELPERS)
    if not ok:
        return text, False

    new_text, ok = _patch_once(new_text, _SUPER_ANCHOR, _SUPER_REPLACEMENT)
    if not ok:
        return text, False

    return new_text, True


def apply(path: str, text: str) -> tuple[str, list[str]]:
    new_text, ok = _patch_routeros_launch(text)
    if not ok:
        raise RuntimeError(
            f"{path}: RouterOS persistence anchors not found. Upstream launch.py "
            "likely changed; update patches/mikrotik_ros.py anchors."
        )
    if new_text != text:
        return new_text, [f"{path}: mikrotik persist-overlay applied"]
    return new_text, [f"{path}: already patched"]
