from digital_twin.ir.entities import PortMisc, StpPolicy


def test_default_is_all_default():
    assert PortMisc() == PortMisc()


def test_lone_flip_is_non_default():
    # Spec-2: the four STP knobs graduated to StpPolicy (see
    # test_stp_policy_lone_flip_is_non_default below) — PortMisc now covers
    # only the remaining five knobs.
    assert PortMisc(inter_switch_link=True) != PortMisc()
    assert PortMisc(storm_control="percentage=50") != PortMisc()
    assert PortMisc(poe_priority="high") != PortMisc()
    assert PortMisc(community_vlan_id=811) != PortMisc()
    assert PortMisc(inter_isolation_network_link=True) != PortMisc()


def test_stp_policy_default_is_all_default():
    assert StpPolicy() == StpPolicy()


def test_stp_policy_lone_flip_is_non_default():
    # Spec-2: the four STP knobs graduated out of PortMisc into StpPolicy
    assert StpPolicy(stp_required=True) != StpPolicy()
    assert StpPolicy(stp_no_root_port=True) != StpPolicy()
    assert StpPolicy(stp_p2p=True) != StpPolicy()
    assert StpPolicy(use_vstp=True) != StpPolicy()
