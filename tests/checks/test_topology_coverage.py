from digital_twin.checks.wired.topology_coverage import TopologyCoverageCheck
from digital_twin.ir import EntityRef, IRDiff, Modified


def _modified(*fields: str) -> IRDiff:
    return IRDiff(
        added=(),
        removed=(),
        modified=(Modified(EntityRef("port", "S:ge-0/0/1"), fields),),
    )


def test_topology_sensitive_port_change_applies():
    check = TopologyCoverageCheck()
    assert check.applies_to(_modified("disabled"))
    assert check.applies_to(_modified("native_vlan", "tagged_vlans"))


def test_independent_port_policy_change_does_not_apply():
    check = TopologyCoverageCheck()
    assert not check.applies_to(_modified("auth"))
    assert not check.applies_to(_modified("mac_limit"))
    assert not check.applies_to(_modified("misc"))
    assert not check.applies_to(_modified("profile"))
    assert not check.applies_to(_modified("stp_policy"))


def test_topology_entity_add_or_remove_applies():
    check = TopologyCoverageCheck()
    assert check.applies_to(IRDiff((EntityRef("link", "l1"),), (), ()))
    assert check.applies_to(IRDiff((), (EntityRef("device", "S"),), ()))
    assert check.applies_to(IRDiff((EntityRef("vlan", "20"),), (), ()))
    assert check.applies_to(IRDiff((), (EntityRef("vlan", "10"),), ()))


def test_only_topology_dependent_vlan_modifications_apply():
    check = TopologyCoverageCheck()
    dhcp = Modified(EntityRef("vlan", "10"), ("dhcp_sources",))
    name = Modified(EntityRef("vlan", "10"), ("name",))
    assert check.applies_to(IRDiff((), (), (dhcp,)))
    assert not check.applies_to(IRDiff((), (), (name,)))
