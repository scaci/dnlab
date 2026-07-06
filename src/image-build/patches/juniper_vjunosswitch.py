"""Per-kind patch plan for juniper_vjunosswitch (vrnetlab vJunos-switch).

Applies persist-overlay to ``/vrnetlab.py``. The V2 launch.py starts the
device without an injected Junos config disk, so it does not need the old
optional IPv6 config-template patch.
"""

from __future__ import annotations

from . import _common


KIND = "juniper_vjunosswitch"

FILES = [
    "/vrnetlab.py",
]


def apply(path: str, text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    new_text, ok = _common.patch_persist_overlay(text)
    if not ok:
        raise RuntimeError(
            f"{path}: persist-overlay anchor not found. Upstream vrnetlab.py "
            "likely changed; update patches/_common.py:_OVERLAY_ANCHOR."
        )
    if new_text != text:
        notes.append(f"{path}: persist-overlay applied")
    else:
        notes.append(f"{path}: already patched")

    return new_text, notes
