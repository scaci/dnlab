from patches.cisco_cat9kv import _patch_launch


def test_current_v2_launch_marker_is_upgraded():
    current = '''\
        # dnlab-patched: c9800cl-v2-nic-map-v4
        min_dp_nics = 2 if self.is_c9800 else 8
        max_dp_nics = 3 if self.is_c9800 else 9
        self.num_nics = max_dp_nics
    def gen_mgmt(self):
        if self.is_c9800:
            self.logger.info("C9800 image: creating QEMU mgmt NIC for IOS GigabitEthernet1")
        return super().gen_mgmt()
        provisioned_slots = max(self.highest_provisioned_nic_num, self.num_provisioned_nics)
        nics = max(0, self.min_nics - provisioned_slots)

        self.logger.debug("Insufficient NICs defined. Generating %s dummy nics", nics)
        self.create_dummy_tap_ifup()

        res = []
        pci_bus_ctr = provisioned_slots

        for i in range(0, nics):
            interface_name = f"dummy{str(i + provisioned_slots + 1)}"
            pci_bus_ctr += 1
'''
    patched, ok = _patch_launch(current)
    assert ok
    assert "IOS GigabitEthernet2" in patched
    assert "IOS GigabitEthernet1" not in patched
