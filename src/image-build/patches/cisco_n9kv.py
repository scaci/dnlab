"""Per-kind patch plan for cisco_n9kv (vrnetlab N9000v).

Applies:
  * persist-overlay in /vrnetlab.py  — base class overlay computation
  * persist-overlay in /launch.py    — n9kv duplicates the computation
                                        to rewrite qemu_args; both
                                        call-sites must see the same
                                        overlay path or the index lookup
                                        in launch.py breaks.

``patch_env_credentials`` is a no-op here: the dnlab-gui deployment
model no longer injects credentials from the UI, but we leave the
transform available so external operators can re-enable it if they
ship a patched image outside the GUI.
"""

from __future__ import annotations

from . import _common


KIND = "cisco_n9kv"

# Files to patch, in the order (launch.py after vrnetlab.py to ensure
# the index-lookup in launch.py matches what vrnetlab.py produces).
FILES = [
    "/vrnetlab.py",
    "/launch.py",
]


def apply(path: str, text: str) -> tuple[str, list[str]]:
    """Return (new_text, notes). Raises if a required anchor is missing."""
    notes: list[str] = []

    new_text, ok = _common.patch_persist_overlay(text)
    if not ok:
        raise RuntimeError(
            f"{path}: persist-overlay anchor not found. Upstream launch.py "
            "likely changed; update patches/_common.py:_OVERLAY_ANCHOR."
        )
    if new_text != text:
        notes.append(f"{path}: persist-overlay applied")
    else:
        notes.append(f"{path}: already patched")

    return new_text, notes
