"""Per-kind patch plan for cisco_vios (vrnetlab Cisco vIOS/vIOSL2).

Applies:
  * persist-overlay in /vrnetlab.py
  * blank-startup in /launch.py — vIOS must boot without applying a
    default startup configuration when /config/startup-config.cfg is not
    mounted. A user-provided startup-config is still applied and saved.
"""

from __future__ import annotations

from . import _common


KIND = "cisco_vios"

FILES = [
    "/vrnetlab.py",
    "/launch.py",
]


_BLANK_STARTUP_MARKER = "# dnlab-patched: vios-blank-startup-v1"

_BLANK_STARTUP_ANCHOR = """        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.fatal("Failed to find startup configuration file")
            con.close()
            return
        
        with open(STARTUP_CONFIG_FILE, "r") as config:
"""

_BLANK_STARTUP_REPLACEMENT = f"""        {_BLANK_STARTUP_MARKER}
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.info("No startup configuration file found; leaving device unconfigured")
            con.close()
            return

        self.logger.info("Startup configuration file found")
        with open(STARTUP_CONFIG_FILE, "r") as config:
"""

_SCRAPLI_BLOCK_ANCHOR = """    def apply_config(self):
        scrapli_timeout = vrnetlab.getenv_uint("SCRAPLI_TIMEOUT", vrnetlab.DEFAULT_SCRAPLI_TIMEOUT)
"""

_SCRAPLI_BLOCK_REPLACEMENT = f"""    def apply_config(self):
        {_BLANK_STARTUP_MARKER}
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.info("No startup configuration file found; leaving device unconfigured")
            return

        scrapli_timeout = vrnetlab.getenv_uint("SCRAPLI_TIMEOUT", vrnetlab.DEFAULT_SCRAPLI_TIMEOUT)
"""

_ALREADY_BLANK_STARTUP_ANCHOR = """    def apply_config(self):
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.info("No startup configuration file found; leaving device unconfigured")
            return

        scrapli_timeout = vrnetlab.getenv_uint("SCRAPLI_TIMEOUT", vrnetlab.DEFAULT_SCRAPLI_TIMEOUT)
"""

_ALREADY_BLANK_STARTUP_REPLACEMENT = f"""    def apply_config(self):
        {_BLANK_STARTUP_MARKER}
        if not os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.info("No startup configuration file found; leaving device unconfigured")
            return

        scrapli_timeout = vrnetlab.getenv_uint("SCRAPLI_TIMEOUT", vrnetlab.DEFAULT_SCRAPLI_TIMEOUT)
"""


def _patch_vios_blank_startup(text: str) -> tuple[str, bool]:
    """Allow vIOS to boot with no mounted startup-config.

    vIOS_V2 upstream opens a scrapli session and treats a missing
    /config/startup-config.cfg as fatal. The preferred patch avoids
    opening scrapli at all when no startup-config is present. The
    secondary anchor keeps this compatible with the legacy vios launch.py
    if someone applies the same kind patch to that image.
    """
    if _BLANK_STARTUP_MARKER in text:
        return text, True

    if _ALREADY_BLANK_STARTUP_ANCHOR in text:
        return text.replace(
            _ALREADY_BLANK_STARTUP_ANCHOR,
            _ALREADY_BLANK_STARTUP_REPLACEMENT,
            1,
        ), True

    if _SCRAPLI_BLOCK_ANCHOR in text:
        return text.replace(_SCRAPLI_BLOCK_ANCHOR, _SCRAPLI_BLOCK_REPLACEMENT, 1), True

    if _BLANK_STARTUP_ANCHOR in text:
        return text.replace(_BLANK_STARTUP_ANCHOR, _BLANK_STARTUP_REPLACEMENT, 1), True

    return text, False


def apply(path: str, text: str) -> tuple[str, list[str]]:
    """Return (new_text, notes). Raises if a required anchor is missing."""
    notes: list[str] = []

    if path == "/vrnetlab.py":
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

    if path == "/launch.py":
        new_text, ok = _patch_vios_blank_startup(text)
        if not ok:
            raise RuntimeError(
                f"{path}: vios blank-startup anchor not found. Upstream launch.py "
                "likely changed; update patches/cisco_vios.py anchors."
            )
        if new_text != text:
            notes.append(f"{path}: vios blank-startup applied")
        else:
            notes.append(f"{path}: already patched")
        return new_text, notes

    raise RuntimeError(f"{path}: no patch plan for this file")
