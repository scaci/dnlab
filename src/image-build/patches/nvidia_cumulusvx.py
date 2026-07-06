"""Per-kind patch plan for NVIDIA Cumulus VX.

Cumulus VX boots factory and manages its writable qcow2 overlay in
``/launch.py``. DnLab mounts persistent VM state at ``/persist``; this
patch makes the launcher prefer that path while keeping ``/config`` as a
compatibility fallback for plain containerlab/vrnetlab runs.
"""

from __future__ import annotations


KIND = "nvidia_cumulusvx"

FILES = [
    "/launch.py",
]

_MARKER = "# dnlab-patched: cumulus-vx-persist-dir-v1"

_CONST_ANCHOR = '''BOOT_SPIN_LIMIT = 6000
'''

_CONST_REPLACEMENT = '''BOOT_SPIN_LIMIT = 6000
PERSIST_DIRS = ("/persist", "/config")
'''

_METHOD_ANCHOR = '''    def _enable_persistent_overlay(self, disk_image):
        if not os.path.isdir("/config"):
            self.logger.warning(
                "/config not mounted; Cumulus VX disk changes are ephemeral"
            )
            return

        persistent_overlay = "/config/cumulusvx_overlay.qcow2"
'''

_METHOD_REPLACEMENT = f'''    def _enable_persistent_overlay(self, disk_image):
        {_MARKER}
        persist_dir = next((path for path in PERSIST_DIRS if os.path.isdir(path)), None)
        if not persist_dir:
            self.logger.warning(
                "No persistence mount found; Cumulus VX disk changes are ephemeral"
            )
            return

        persistent_overlay = os.path.join(persist_dir, "cumulusvx_overlay.qcow2")
'''


def _patch_once(text: str, old: str, new: str) -> tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, 1), True


def _patch_cumulus_launch(text: str) -> tuple[str, bool]:
    if _MARKER in text:
        return text, True

    new_text, ok = _patch_once(text, _CONST_ANCHOR, _CONST_REPLACEMENT)
    if not ok:
        return text, False

    new_text, ok = _patch_once(new_text, _METHOD_ANCHOR, _METHOD_REPLACEMENT)
    if not ok:
        return text, False

    return new_text, True


def apply(path: str, text: str) -> tuple[str, list[str]]:
    new_text, ok = _patch_cumulus_launch(text)
    if not ok:
        raise RuntimeError(
            f"{path}: Cumulus VX persistence anchors not found. Upstream launch.py "
            "likely changed; update patches/cumulus_vx.py anchors."
        )
    if new_text != text:
        return new_text, [f"{path}: cumulus-vx persist-dir applied"]
    return new_text, [f"{path}: already patched"]
