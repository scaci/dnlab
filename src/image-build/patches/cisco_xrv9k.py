"""Per-kind patch plan for cisco_xrv9k (vrnetlab Cisco XRv9k).

Applies persist-overlay to ``/vrnetlab.py``. XRv9k's ``launch.py``
does not compute a separate overlay path; it either uses the base
class disk device as-is or rewrites the QEMU disk device by reading the
path that the base class already placed in ``qemu_args``. Persisting
the base overlay is therefore enough for both legacy and UEFI/virtio
XRv9k images.
"""

from __future__ import annotations

from . import _common


KIND = "cisco_xrv9k"

FILES = [
    "/vrnetlab.py",
]


def apply(path: str, text: str) -> tuple[str, list[str]]:
    """Return (new_text, notes). Raises if a required anchor is missing."""
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
