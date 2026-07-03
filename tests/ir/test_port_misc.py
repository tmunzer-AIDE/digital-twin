from digital_twin.ir.entities import PortMisc


def test_default_is_all_default():
    assert PortMisc() == PortMisc()


def test_lone_flip_is_non_default():
    assert PortMisc(inter_switch_link=True) != PortMisc()
    assert PortMisc(storm_control="percentage=50") != PortMisc()
    assert PortMisc(poe_priority="high") != PortMisc()
    assert PortMisc(community_vlan_id=811) != PortMisc()
    assert PortMisc(inter_isolation_network_link=True) != PortMisc()
    assert PortMisc(stp_required=True) != PortMisc()
    assert PortMisc(stp_no_root_port=True) != PortMisc()
    assert PortMisc(stp_p2p=True) != PortMisc()
    assert PortMisc(use_vstp=True) != PortMisc()
