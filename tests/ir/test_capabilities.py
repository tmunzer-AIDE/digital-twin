from digital_twin.ir.capabilities import Capability, IRCapability


def test_capability_values_are_stable_strings():
    assert IRCapability.WIRED_L2.value == "wired.l2"
    assert IRCapability.CLIENTS_ACTIVE.value == "clients.active"
    assert IRCapability.STP_STATE.value == "stp.state"
    assert IRCapability.L3_EXITS.value == "l3.exits"


def test_bidirectional_is_not_a_capability():
    assert not hasattr(IRCapability, "LINKS_BIDIRECTIONAL")


def test_capabilities_are_set_members():
    caps = {IRCapability.WIRED_L2, IRCapability.STP_STATE}
    assert IRCapability.WIRED_L2 in caps
    assert IRCapability.CLIENTS_ACTIVE not in caps


def test_ircapability_members_are_capabilities_and_interchange_with_strings():
    cap: Capability = IRCapability.WIRED_L2  # enum member IS a Capability (str)
    assert cap == "wired.l2"
    assert IRCapability.WIRED_L2 in {"wired.l2"}  # equality/hash match the value
    future: Capability = "analysis.reachability"  # later capabilities are the same type
    assert isinstance(future, str)
