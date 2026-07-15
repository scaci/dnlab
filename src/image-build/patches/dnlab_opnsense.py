"""dNLab OPNsense image patch plan."""

from . import _common

KIND = "dnlab_opnsense"
FILES = ["/vrnetlab.py"]


def apply(path: str, text: str) -> tuple[str, list[str]]:
    new_text, ok = _common.patch_persist_overlay(text)
    if not ok:
        raise RuntimeError(f"{path}: persist-overlay anchor not found")
    return new_text, [f"{path}: persist-overlay applied" if new_text != text else f"{path}: already patched"]
