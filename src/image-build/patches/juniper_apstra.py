"""Per-kind patch plan for Juniper Apstra.

Apstra already manages a persistent overlay in ``/launch.py``. Upstream uses
``/config/apstra_overlay.qcow2``; DnLab mounts VM state at ``/persist``.
This patch makes the launcher prefer ``/persist`` while retaining ``/config``
as a compatibility fallback for plain vrnetlab/containerlab runs.
"""

from __future__ import annotations


KIND = "juniper_apstra"

FILES = [
    "/launch.py",
]

_MARKER = "# dnlab-patched: juniper-apstra-persist-dir-v1"

_CONST_ANCHOR = '''BOOT_TIMEOUT_S = 600
'''

_CONST_REPLACEMENT = '''BOOT_TIMEOUT_S = 600
PERSIST_DIRS = ("/persist", "/config")
'''

_PERSIST_ANCHOR = '''        if os.path.isdir("/config"):
            persistent_overlay = "/config/apstra_overlay.qcow2"
'''

_PERSIST_REPLACEMENT = f'''        {_MARKER}
        persist_dir = next((path for path in PERSIST_DIRS if os.path.isdir(path)), None)
        if persist_dir:
            persistent_overlay = os.path.join(persist_dir, "apstra_overlay.qcow2")
'''

_WARNING_ANCHOR = '''                "/config not mounted — overlay is ephemeral and will not "
                "survive clab destroy. Create the bind-mount directory to "
                "enable persistence."
'''

_WARNING_REPLACEMENT = '''                "No persistence mount found — overlay is ephemeral and will not "
                "survive clab destroy. Mount /persist to enable dnlab "
                "persistence, or /config for plain vrnetlab compatibility."
'''

_RESOURCE_MARKER = "# dnlab-patched: juniper-apstra-env-resources-v1"
_RESOURCE_ANCHOR = '''# How long (seconds) to wait for the VM to reach a login prompt.
'''
_RESOURCE_HELPERS = '''# dnlab-patched: juniper-apstra-env-resources-v1
DEFAULT_VCPU = 4


def env_int(name, default, minimum=1):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


# How long (seconds) to wait for the VM to reach a login prompt.
'''
_SUPER_ANCHOR = '''        # ── initialise the vrnetlab base VM ───────────────────────────────────
        # super().__init__() sets up self.logger, self.qemu_args, and all
        # other base attributes.  Nothing on self must be accessed before this.
        super(Apstra_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=DEFAULT_RAM_MB,
        )
'''
_SUPER_REPLACEMENT = '''        # ── initialise the vrnetlab base VM ───────────────────────────────────
        # super().__init__() sets up self.logger, self.qemu_args, and all
        # other base attributes.  Nothing on self must be accessed before this.
        ram_mb = env_int("RAM", DEFAULT_RAM_MB, minimum=DEFAULT_RAM_MB)
        vcpu = env_int("VCPU", DEFAULT_VCPU, minimum=1)
        super(Apstra_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=ram_mb,
            cpu="host",
            smp=f"{vcpu},sockets=1,cores={vcpu},threads=1",
        )
'''


def _patch_once(text: str, old: str, new: str) -> tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, 1), True


def _patch_apstra_launch(text: str) -> tuple[str, bool]:
    if _MARKER in text:
        new_text = text
    else:
        new_text, ok = _patch_once(text, _CONST_ANCHOR, _CONST_REPLACEMENT)
        if not ok:
            return text, False

        new_text, ok = _patch_once(new_text, _PERSIST_ANCHOR, _PERSIST_REPLACEMENT)
        if not ok:
            return text, False

        new_text, ok = _patch_once(new_text, _WARNING_ANCHOR, _WARNING_REPLACEMENT)
        if not ok:
            return text, False

    new_text, ok = _patch_apstra_resources(new_text)
    if not ok:
        return text, False

    return new_text, True


def _patch_apstra_resources(text: str) -> tuple[str, bool]:
    changed = False
    if _RESOURCE_MARKER not in text:
        text, ok = _patch_once(text, _RESOURCE_ANCHOR, _RESOURCE_HELPERS)
        if not ok:
            return text, False
        changed = True
    if "ram_mb = env_int(\"RAM\", DEFAULT_RAM_MB" not in text:
        text, ok = _patch_once(text, _SUPER_ANCHOR, _SUPER_REPLACEMENT)
        if not ok:
            return text, False
        changed = True
    return text, changed or _RESOURCE_MARKER in text


def apply(path: str, text: str) -> tuple[str, list[str]]:
    new_text, ok = _patch_apstra_launch(text)
    if not ok:
        raise RuntimeError(
            f"{path}: Juniper Apstra persistence anchors not found. Upstream "
            "launch.py likely changed; update patches/juniper_apstra.py anchors."
        )
    if new_text != text:
        return new_text, [f"{path}: juniper-apstra persist-dir applied"]
    return new_text, [f"{path}: already patched"]
