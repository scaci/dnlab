"""Per-kind patch plan for cisco_cat9kv / cisco_c9800cl V2.

Applies persist-overlay to ``/vrnetlab.py``. Cat9kv_V2 and C9800-CL
use the base vrnetlab VM overlay disk path directly; persisting the
base overlay is enough for the VM state to survive destroy/deploy.

Also keeps C9800-CL data interfaces contiguous after a real QEMU
management NIC. IOS-XE 17.15 enumerates the QEMU management NIC as
GigabitEthernet2, then data eth1 -> GigabitEthernet3,
eth2 -> GigabitEthernet4, eth3 -> GigabitEthernet5.
"""

from __future__ import annotations

from . import _common


KIND = "cisco_cat9kv"

FILES = [
    "/launch.py",
    "/vrnetlab.py",
]

_C9800_NIC_MARKER_V1 = "# dnlab-patched: c9800cl-v2-nic-map-v1"
_C9800_NIC_MARKER_V2 = "# dnlab-patched: c9800cl-v2-nic-map-v2"
_C9800_NIC_MARKER_V3 = "# dnlab-patched: c9800cl-v2-nic-map-v3"
_C9800_NIC_MARKER = "# dnlab-patched: c9800cl-v2-nic-map-v4"

_C9800_NIC_ANCHOR = '''        min_dp_nics = 2 if self.is_c9800 else 8

        super().__init__(
'''

_C9800_NIC_REPLACEMENT = f'''        {_C9800_NIC_MARKER}
        min_dp_nics = 2 if self.is_c9800 else 8
        max_dp_nics = 3 if self.is_c9800 else 9

        super().__init__(
'''

_C9800_NUM_NICS_ANCHOR = "        self.num_nics = 3 if self.is_c9800 else 9"
_C9800_NUM_NICS_REPLACEMENT = "        self.num_nics = max_dp_nics"

_C9800_GEN_MGMT_OLD_MARKER = 'self.logger.info("C9800 image: not creating a separate QEMU mgmt NIC")'
_C9800_GEN_MGMT_OLD_G1_MARKER = 'self.logger.info("C9800 image: creating QEMU mgmt NIC for GigabitEthernet1")'
_C9800_GEN_MGMT_MARKER = 'self.logger.info("C9800 image: creating QEMU mgmt NIC for IOS GigabitEthernet2")'
_C9800_GEN_MGMT_ANCHOR = '''        else:
            self.logger.info("C9800 image without startup config; not attaching bootstrap ISO")

    def create_boot_image(self):
'''

_C9800_GEN_MGMT_REPLACEMENT = '''        else:
            self.logger.info("C9800 image without startup config; not attaching bootstrap ISO")

    def gen_mgmt(self):
        if self.is_c9800:
            self.logger.info("C9800 image: creating QEMU mgmt NIC for IOS GigabitEthernet2")
        return super().gen_mgmt()

    def create_boot_image(self):
'''

_C9800_DUMMY_OLD = '''        nics = self.min_nics - self.num_provisioned_nics

        self.logger.debug("Insufficient NICs defined. Generating %s dummy nics", nics)
        self.create_dummy_tap_ifup()

        res = []
        pci_bus_ctr = self.num_provisioned_nics

        for i in range(0, nics):
            interface_name = f"dummy{str(i + self.num_provisioned_nics)}"
            pci_bus_ctr += 1
'''

_C9800_DUMMY_NEW = '''        provisioned_slots = max(self.highest_provisioned_nic_num, self.num_provisioned_nics)
        nics = max(0, self.min_nics - provisioned_slots)

        self.logger.debug("Insufficient NICs defined. Generating %s dummy nics", nics)
        self.create_dummy_tap_ifup()

        res = []
        pci_bus_ctr = provisioned_slots

        for i in range(0, nics):
            interface_name = f"dummy{str(i + provisioned_slots + 1)}"
            pci_bus_ctr += 1
'''


def _patch_launch(text: str) -> tuple[str, bool]:
    if _C9800_NIC_MARKER_V1 in text:
        text = text.replace(_C9800_NIC_MARKER_V1, _C9800_NIC_MARKER, 1)
        text = text.replace(
            "        min_dp_nics = 3 if self.is_c9800 else 8",
            "        min_dp_nics = 2 if self.is_c9800 else 8",
            1,
        )
        text = text.replace(
            "        max_dp_nics = 2 if self.is_c9800 else 9",
            "        max_dp_nics = 3 if self.is_c9800 else 9",
            1,
        )
    elif _C9800_NIC_MARKER_V2 in text:
        text = text.replace(_C9800_NIC_MARKER_V2, _C9800_NIC_MARKER, 1)
        text = text.replace(
            "        min_dp_nics = 3 if self.is_c9800 else 8",
            "        min_dp_nics = 2 if self.is_c9800 else 8",
            1,
        )
    elif _C9800_NIC_MARKER_V3 in text:
        text = text.replace(_C9800_NIC_MARKER_V3, _C9800_NIC_MARKER, 1)
        text = text.replace(
            "        min_dp_nics = 3 if self.is_c9800 else 8",
            "        min_dp_nics = 2 if self.is_c9800 else 8",
            1,
        )
    elif _C9800_NIC_MARKER not in text:
        if _C9800_NIC_ANCHOR not in text or _C9800_NUM_NICS_ANCHOR not in text:
            return text, False

        text = text.replace(_C9800_NIC_ANCHOR, _C9800_NIC_REPLACEMENT, 1)
        text = text.replace(_C9800_NUM_NICS_ANCHOR, _C9800_NUM_NICS_REPLACEMENT, 1)

    if _C9800_GEN_MGMT_OLD_MARKER in text:
        text = text.replace(
            '''    def gen_mgmt(self):
        if self.is_c9800:
            self.logger.info("C9800 image: not creating a separate QEMU mgmt NIC")
            return []
        return super().gen_mgmt()
''',
            '''    def gen_mgmt(self):
        if self.is_c9800:
            self.logger.info("C9800 image: creating QEMU mgmt NIC for IOS GigabitEthernet2")
        return super().gen_mgmt()
''',
            1,
        )

    if _C9800_GEN_MGMT_OLD_G1_MARKER in text:
        text = text.replace(_C9800_GEN_MGMT_OLD_G1_MARKER, _C9800_GEN_MGMT_MARKER, 1)

    if _C9800_GEN_MGMT_MARKER in text:
        pass
    elif _C9800_GEN_MGMT_ANCHOR in text:
        text = text.replace(_C9800_GEN_MGMT_ANCHOR, _C9800_GEN_MGMT_REPLACEMENT, 1)
    else:
        return text, False

    if _C9800_DUMMY_OLD in text:
        text = text.replace(_C9800_DUMMY_OLD, _C9800_DUMMY_NEW, 1)
    elif _C9800_DUMMY_NEW not in text:
        return text, False

    return text, True


def apply(path: str, text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    if path == "/launch.py":
        new_text, ok = _patch_launch(text)
        if not ok:
            raise RuntimeError(
                f"{path}: c9800cl V2 NIC-map anchors not found. Upstream "
                "launch.py likely changed; update patches/cisco_cat9kv.py."
            )
        if new_text != text:
            notes.append(f"{path}: c9800cl V2 NIC-map applied")
        else:
            notes.append(f"{path}: already patched")
        return new_text, notes

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
