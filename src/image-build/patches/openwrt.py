"""Per-kind patch plan for OpenWrt V2.

Applies:
  * persist-overlay in /launch.py — OpenWrt_V2 manages its overlay in
    launch.py, so the dnlab persistence hook must be injected there.
  * spin-reset in /launch.py — keep internal VM restart attempts from
    immediately retriggering the bootstrap timeout loop.
"""

from __future__ import annotations


KIND = "openwrt"

FILES = [
    "/launch.py",
]

_MARKER = "# dnlab-patched: openwrt-persist-overlay-v1"
_SPIN_MARKER = "# dnlab-patched: openwrt-spin-reset-v1"

_READY_MARKER = "# dnlab-patched: openwrt-readiness-v1"
_READY_ANCHOR = '''
        (ridx, match, res) = self.tn.expect([b"br-lan"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                self.logger.debug("VM started")
                # run main config!
                self.bootstrap_config()
                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("Startup complete in: %s" % startup_time)
                # mark as running
                self.running = True
                return
'''
_READY_REPLACEMENT = '''
        if self.spins and self.spins % 5 == 0:
            self.tn.write(b"\r\n")

        # dnlab-patched: openwrt-readiness-v1
        boot_patterns = [
            b"br-lan",
            b"root@OpenWrt",
            b"root@",
            b"Please press Enter",
            b"BusyBox",
        ]
        (ridx, match, res) = self.tn.expect(boot_patterns, 1)
        if match:  # got a match!
            self.logger.debug("VM started")
            # run main config!
            self.bootstrap_config()
            # close telnet connection
            self.tn.close()
            # startup time?
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info("Startup complete in: %s" % startup_time)
            # mark as running
            self.running = True
            return
'''

_REGEX_BLOCK = '''OPENWRT_BASE_IMAGE_RE = re.compile(
    r"openwrt-.*-x86-(?:64-)?(?:generic-)?generic-ext4-combined\\.img$"
)
'''

_CONSTANTS_BLOCK = '''OPENWRT_BASE_IMAGE_RE = re.compile(
    r"openwrt-.*-x86-(?:64-)?(?:generic-)?generic-ext4-combined\\.img$"
)
PERSIST_DIR = "/persist"
EPHEMERAL_OVERLAY_DIR = "/overlay"
'''

_HELPER_ANCHOR = '''def is_openwrt_base_image(filename):
    return bool(OPENWRT_BASE_IMAGE_RE.match(filename))
'''

_HELPER_BLOCK = '''def is_openwrt_base_image(filename):
    return bool(OPENWRT_BASE_IMAGE_RE.match(filename))


def overlay_image_for(disk_image):
    return re.sub(r"(\\.[^.]+$)", r"-overlay\\1", disk_image)


def prepare_persistent_overlay(vm, disk_image):
    """Move the writable overlay to /persist when a volume is mounted."""
    overlay_dir = PERSIST_DIR if os.path.isdir(PERSIST_DIR) else EPHEMERAL_OVERLAY_DIR
    os.makedirs(overlay_dir, exist_ok=True)
    overlay_img = overlay_image_for(disk_image)
    overlay_target = os.path.join(overlay_dir, os.path.basename(overlay_img))

    if not os.path.exists(overlay_target):
        if os.path.exists(overlay_img):
            shutil.move(overlay_img, overlay_target)
            vm.logger.info("Moved OpenWrt overlay to %s", overlay_target)
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
                overlay_target,
            ])
            vm.logger.info("Created OpenWrt overlay at %s", overlay_target)

    if os.path.exists(overlay_img) and not os.path.islink(overlay_img):
        os.remove(overlay_img)
    if not os.path.islink(overlay_img) or os.readlink(overlay_img) != overlay_target:
        if os.path.islink(overlay_img):
            os.remove(overlay_img)
        os.symlink(overlay_target, overlay_img)

    for idx, arg in enumerate(vm.qemu_args):
        if f"file={overlay_img}" in arg:
            vm.qemu_args[idx] = arg.replace(
                f"file={overlay_img}", f"file={overlay_target}"
            )

    if overlay_dir == PERSIST_DIR:
        vm.logger.info("OpenWrt persistence enabled via %s", overlay_target)
    else:
        vm.logger.warning("/persist not mounted; OpenWrt overlay remains ephemeral")
    return overlay_dir
'''

_INIT_ANCHOR = '''        super(OpenWRT_vm, self).__init__(
            username, password, disk_image=disk_image, ram=128
        )
'''

_INIT_REPLACEMENT = '''        super(OpenWRT_vm, self).__init__(
            username, password, disk_image=disk_image, ram=128
        )
        self.overlay_dir = prepare_persistent_overlay(self, disk_image)
'''

_OVERLAY_DIR_ANCHOR = '        overlay_dir = "/overlay"'
_OVERLAY_DIR_REPLACEMENT = "        overlay_dir = self.overlay_dir"

_SPIN_ANCHOR = "        self.start()\n\n    def bootstrap_spin(self):"
_SPIN_REPLACEMENT = (
    f"        {_SPIN_MARKER}\n"
    "        self.start()\n"
    "        self.spins = 0\n\n"
    "    def bootstrap_spin(self):"
)

_MGMT_CIDR_HELPERS = '''    def _mgmt_ipv4_cidr(self):
        iface = self._mgmt_ipv4_interface()
        return f"{iface.ip}/{iface.network.prefixlen}"
'''
_MGMT_CLASSIC_HELPERS = '''    def _mgmt_ipv4_address(self):
        return str(self._mgmt_ipv4_interface().ip)

    def _mgmt_ipv4_netmask(self):
        return str(self._mgmt_ipv4_interface().netmask)
'''
_MGMT_EXPECTED_CIDR = "        expected_mgmt_address_ipv4 = self._mgmt_ipv4_cidr()\n"
_MGMT_EXPECTED_CLASSIC = (
    "        expected_mgmt_address_ipv4 = self._mgmt_ipv4_address()\n"
    "        expected_mgmt_netmask = self._mgmt_ipv4_netmask()\n"
)
_MGMT_DEL_NETMASK = '''            self.tn.write(b"uci -q del network.mgmt.netmask\n")
            time.sleep(0.5)
'''
_MGMT_SET_NETMASK_12 = '''            self.tn.write(
                f"uci set network.mgmt.netmask='{expected_mgmt_netmask}'\n".encode(
                    "utf-8"
                )
            )
            time.sleep(0.5)
'''
_MGMT_SET_NETMASK_16 = '''                self.tn.write(
                    f"uci set network.mgmt.netmask='{expected_mgmt_netmask}'\n".encode(
                        "utf-8"
                    )
                )
                time.sleep(0.5)
'''


def _patch_mgmt_ipv4_classic(text: str) -> tuple[str, bool]:
    """Use OpenWrt's stable ipaddr+netmask form for static mgmt IPv4."""
    changed = False
    if _MGMT_CIDR_HELPERS in text:
        text = text.replace(_MGMT_CIDR_HELPERS, _MGMT_CLASSIC_HELPERS, 1)
        changed = True
    if _MGMT_EXPECTED_CIDR in text:
        text = text.replace(_MGMT_EXPECTED_CIDR, _MGMT_EXPECTED_CLASSIC, 1)
        changed = True
    if 'f"option netmask \'{expected_mgmt_netmask}\'" not in output' not in text:
        text = text.replace('''                    "option netmask" in output
''', '''                    f"option netmask '{expected_mgmt_netmask}'" not in output
''', 1)
        changed = True
    count = text.count(_MGMT_DEL_NETMASK)
    if count:
        text = text.replace(_MGMT_DEL_NETMASK, _MGMT_SET_NETMASK_12, 1)
        if count > 1:
            text = text.replace(_MGMT_DEL_NETMASK, _MGMT_SET_NETMASK_16, count - 1)
        changed = True
    return text, changed


def _patch_once(text: str, old: str, new: str) -> tuple[str, bool]:
    if old not in text:
        return text, False
    return text.replace(old, new, 1), True


def _patch_openwrt_launch(text: str) -> tuple[str, bool]:
    if _MARKER in text:
        new_text = text
        changed = False
    else:
        new_text, ok = _patch_once(text, _REGEX_BLOCK, f"{_MARKER}\n{_CONSTANTS_BLOCK}")
        if not ok:
            return text, False
        changed = True

        new_text, ok = _patch_once(new_text, _HELPER_ANCHOR, _HELPER_BLOCK)
        if not ok:
            return text, False

        new_text, ok = _patch_once(new_text, _INIT_ANCHOR, _INIT_REPLACEMENT)
        if not ok:
            return text, False

        new_text, ok = _patch_once(new_text, _OVERLAY_DIR_ANCHOR, _OVERLAY_DIR_REPLACEMENT)
        if not ok:
            return text, False

    if _SPIN_MARKER not in new_text and _SPIN_ANCHOR in new_text:
        new_text = new_text.replace(_SPIN_ANCHOR, _SPIN_REPLACEMENT, 1)
        changed = True

    if _READY_MARKER not in new_text and _READY_ANCHOR in new_text:
        new_text = new_text.replace(_READY_ANCHOR, _READY_REPLACEMENT, 1)
        changed = True

    new_text, mgmt_changed = _patch_mgmt_ipv4_classic(new_text)
    changed = changed or mgmt_changed

    return new_text, changed or _MARKER in new_text


def apply(path: str, text: str) -> tuple[str, list[str]]:
    new_text, ok = _patch_openwrt_launch(text)
    if not ok:
        raise RuntimeError(
            f"{path}: OpenWrt persistence anchors not found. Upstream launch.py "
            "likely changed; update patches/openwrt.py anchors."
        )
    if new_text != text:
        return new_text, [f"{path}: openwrt persist-overlay applied"]
    return new_text, [f"{path}: already patched"]
