from digital_twin.ir.entities import PortMisc


def test_default_is_all_default():
    assert PortMisc() == PortMisc()


def test_lone_flip_is_non_default():
    assert PortMisc(enable_qos=True) != PortMisc()
    assert PortMisc(inter_switch_link=True) != PortMisc()
    assert PortMisc(storm_control="percentage=50") != PortMisc()
