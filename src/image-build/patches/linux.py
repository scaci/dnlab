"""Per-kind patch plan for containerlab linux images running FRR.

FRR's upstream container image starts from ``/usr/lib/frr/docker-start`` and
keeps its runtime configuration under ``/etc/frr``. DnLab mounts persistent
node state at ``/persist``; this patch redirects the key FRR config files to
``/persist/frr`` when that mount is available, while keeping the image usable
without persistence.
"""

from __future__ import annotations


KIND = "linux"

FILES = [
    "/usr/lib/frr/docker-start",
]

_MARKER = "# dnlab-patched: linux-frr-persist-config-v1"

_ANCHOR = '''source /usr/lib/frr/frrcommon.sh
/usr/lib/frr/watchfrr $(daemon_list)
'''

_REPLACEMENT = f'''source /usr/lib/frr/frrcommon.sh

dnlab_prepare_persistent_frr() {{
        {_MARKER}
        if [ ! -d "/persist" ]; then
                log_warning_msg "/persist not mounted; FRR config changes are ephemeral"
                return
        fi

        persist_frr="/persist/frr"
        mkdir -p "$persist_frr"

        for name in daemons frr.conf vtysh.conf; do
                src="/etc/frr/$name"
                dst="$persist_frr/$name"
                if [ ! -e "$dst" ]; then
                        if [ -e "$src" ] && [ ! -L "$src" ]; then
                                cp -a "$src" "$dst"
                        elif [ "$name" = "frr.conf" ]; then
                                touch "$dst"
                        fi
                fi
                if [ -e "$dst" ]; then
                        rm -f "$src"
                        ln -s "$dst" "$src"
                fi
        done

        chown -R frr:frr "$persist_frr" 2>/dev/null || true
        chmod 640 "$persist_frr"/daemons "$persist_frr"/frr.conf 2>/dev/null || true
        log_success_msg "FRR persistence enabled via $persist_frr"
}}

dnlab_prepare_persistent_frr
/usr/lib/frr/watchfrr $(daemon_list)
'''


def apply(path: str, text: str) -> tuple[str, list[str]]:
    if _MARKER in text:
        return text, [f"{path}: already patched"]

    if _ANCHOR not in text:
        raise RuntimeError(
            f"{path}: FRR docker-start anchor not found. Upstream image "
            "likely changed; update patches/linux.py anchors."
        )

    return (
        text.replace(_ANCHOR, _REPLACEMENT, 1),
        [f"{path}: linux/frr persist-config applied"],
    )
