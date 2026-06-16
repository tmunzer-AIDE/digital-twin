from digital_twin.scope.device_profile_gate import device_profile_rejection

# effective maps are keyed by device_id(mac) (normalized: lower, no colons).
GW = {"port_config": {"ge-0/0/0": {"disabled": False}}}          # baseline gateway eff
GW2 = {"port_config": {"ge-0/0/0": {"disabled": True}}}          # proposed: disabled flip


def test_profiled_gateway_with_overridable_leaf_diff_rejects():
    rej = device_profile_rejection(
        devices=[{"type": "gateway", "mac": "AA:BB:CC:DD:EE:01", "deviceprofile_id": "p1"}],
        baseline_eff={"aabbccddee01": GW},
        proposed_eff={"aabbccddee01": GW2},     # port_config.*.disabled is gateway-overridable
    )
    assert rej is not None and rej.stage == "device_profile_gate"


def test_ap_profile_does_not_taint():
    rej = device_profile_rejection(
        devices=[{"type": "ap", "mac": "m2", "deviceprofile_id": "p2"}],
        baseline_eff={}, proposed_eff={},
    )
    assert rej is None


def test_profiled_device_with_no_overridable_diff_does_not_taint():
    rej = device_profile_rejection(
        devices=[{"type": "gateway", "mac": "AA:BB:CC:DD:EE:01", "deviceprofile_id": "p1"}],
        baseline_eff={"aabbccddee01": GW}, proposed_eff={"aabbccddee01": GW},
    )
    assert rej is None


def test_non_overridable_leaf_diff_does_not_taint():
    base = {"aabbccddee01": {"routing": {"x": 1}}}
    prop = {"aabbccddee01": {"routing": {"x": 2}}}
    rej = device_profile_rejection(
        devices=[{"type": "gateway", "mac": "AA:BB:CC:DD:EE:01", "deviceprofile_id": "p1"}],
        baseline_eff=base, proposed_eff=prop,
    )
    assert rej is None


def test_device_without_profile_does_not_taint():
    rej = device_profile_rejection(
        devices=[{"type": "switch", "mac": "AA:BB:CC:DD:EE:02"}],   # no deviceprofile_id
        baseline_eff={"aabbccddee02": {"port_config": {"p": {"disabled": False}}}},
        proposed_eff={"aabbccddee02": {"port_config": {"p": {"disabled": True}}}},
    )
    assert rej is None
