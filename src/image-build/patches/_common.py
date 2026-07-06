"""Shared text transforms applied by per-kind patcher modules.

The dnlab patcher rebuilds upstream vrnetlab images with a
``-dnlab`` suffix after applying one or more transforms to the
``launch.py`` and ``vrnetlab.py`` files that live at ``/`` inside the
container.

Transforms are intentionally surgical (single-anchor `str.replace`)
rather than full AST rewrites: upstream launch.py files evolve but the
anchor strings we target (the overlay computation and the hardcoded
defaults) are stable idioms across vendors.

Each transform returns ``(new_text, applied: bool)``. ``applied`` is
False when the anchor was not found — callers decide whether that is a
fatal error or a warning (e.g. patch_env_credentials is optional and
skipped silently when defaults are already removed).
"""

from __future__ import annotations


# ── anchors ────────────────────────────────────────────────────────────

# Both upstream vrnetlab.py and the per-kind launch.py compute the
# overlay disk path via this exact idiom. Redirecting it to /persist
# lets us bind-mount overlay persistence from the host without patching
# qemu-img invocations.
_OVERLAY_ANCHOR = 'overlay_disk_image = re.sub(r"(\\.[^.]+$)", r"-overlay\\1", disk_image)'

_OVERLAY_REPLACEMENT = (
    'overlay_disk_image = ('
    '"/persist/overlay.qcow2" if os.path.isdir("/persist") '
    'else re.sub(r"(\\.[^.]+$)", r"-overlay\\1", disk_image)'
    ')'
)

# Marker we inject alongside the patch so the image is self-identifying
# (useful for provenance checks at deploy time).
_PROVENANCE_MARKER = "# dnlab-patched: persist-overlay-v1"

_OPTIONAL_IPV6_MARKER = "# dnlab-patched: optional-mgmt-ipv6-v1"


def patch_persist_overlay(text: str) -> tuple[str, bool]:
    """Redirect overlay qcow2 to ``/persist/overlay.qcow2`` when the bind
    mount exists.

    Falls back to upstream behaviour when ``/persist`` is absent, so the
    patched image still boots standalone (e.g. in CI without a host
    bind mount).

    Indent-aware: the anchor appears with different leading whitespace
    depending on the file (e.g. 8 spaces in cisco launch.py, 12 spaces
    inside the ``else:`` block of vrnetlab.py). We detect the anchor
    line's indent and reuse it for both the provenance marker and the
    replacement statement, so the patched block stays syntactically
    valid in either context.
    """
    if _PROVENANCE_MARKER in text:
        # Idempotent: already patched, skip.
        return text, True

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(_OVERLAY_ANCHOR):
            continue
        indent = line[: len(line) - len(stripped)]
        # Preserve the original line's trailing newline by reading it
        # from the matched line rather than assuming "\n".
        eol = "\n" if line.endswith("\n") else ""
        lines[i] = (
            f"{indent}{_PROVENANCE_MARKER}{eol}"
            f"{indent}{_OVERLAY_REPLACEMENT}{eol}"
        )
        return "".join(lines), True
    return text, False


def patch_env_credentials(text: str) -> tuple[str, bool]:
    """Replace hardcoded argparse defaults for username/password with
    environment-variable lookups.

    Upstream launch.py files ship with defaults like ``--username vrnetlab
    --password VR-netlab9``. The dnlab-gui deployment model forbids
    credential injection from the UI, so the image should instead read
    ``USERNAME`` / ``PASSWORD`` from the process environment (which the
    orchestrator populates from the topology ``env`` block).

    Returns applied=False if the anchor is not present (the file is
    already environment-driven or the vendor uses a different idiom).
    """
    before = text
    # Common upstream pattern — two separate argparse calls with string
    # literals. We do not force a specific default here; callers control
    # credentials via USERNAME/PASSWORD env vars.
    anchors = [
        ('parser.add_argument("--username", default="vrnetlab", help="Username")',
         'parser.add_argument("--username", default=os.environ.get("USERNAME", "vrnetlab"), help="Username")'),
        ('parser.add_argument("--password", default="VR-netlab9", help="Password")',
         'parser.add_argument("--password", default=os.environ.get("PASSWORD", "VR-netlab9"), help="Password")'),
    ]
    for old, new in anchors:
        if old in text:
            text = text.replace(old, new, 1)
    return text, text != before


def patch_optional_mgmt_ipv6(text: str) -> tuple[str, bool]:
    """Allow launch.py templates to render when vrnetlab has no IPv6
    management address configured.

    Some images unconditionally replace IPv6 placeholders with
    ``self.mgmt_address_ipv6`` / ``self.mgmt_gw_ipv6``. In IPv4-only
    containerlab deployments those attributes can be ``None``, which
    makes ``str.replace`` raise ``TypeError`` before QEMU starts.
    """
    if _OPTIONAL_IPV6_MARKER in text:
        return text, True

    before = text
    anchors = [
        (
            'cfg = cfg.replace("{MGMT_IP_IPV6}", self.mgmt_address_ipv6)',
            'cfg = cfg.replace("{MGMT_IP_IPV6}", self.mgmt_address_ipv6 or "")',
        ),
        (
            'cfg = cfg.replace("{MGMT_GW_IPV6}", self.mgmt_gw_ipv6)',
            'cfg = cfg.replace("{MGMT_GW_IPV6}", self.mgmt_gw_ipv6 or "")',
        ),
    ]
    for old, new in anchors:
        text = text.replace(old, new, 1)
    if text == before:
        return text, False

    first_anchor = anchors[0][1]
    text = text.replace(first_anchor, f"{_OPTIONAL_IPV6_MARKER}\n        {first_anchor}", 1)
    return text, True
