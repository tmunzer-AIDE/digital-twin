"""DhcpScope: a SERVING dhcpd entry's range facts. Identity is provider:network
(exactly how dhcpd_config is keyed); vlan/subnet are DIFFED FIELDS so an
unknown->known resolution is a modification, never remove+add (GS25 review)."""

import pytest

from digital_twin.ir import DhcpScope, IRBuilder, diff_ir
from digital_twin.ir.model import IRValidationError
from tests.factories import sw


def _build(*scopes):
    b = IRBuilder().add_device(sw("S"))
    for s in scopes:
        b.add_dhcp_scope(s)
    return b.build()


def test_identity_is_provider_and_network_only():
    s = DhcpScope(provider="site", network="corp", vlan=10, ip_start="10.0.0.10")
    assert s.id == "site:corp"


def test_vlan_resolution_change_diffs_as_modified_not_remove_add():
    base = _build(DhcpScope(provider="site", network="corp", vlan=None))
    prop = _build(DhcpScope(provider="site", network="corp", vlan=10))
    d = diff_ir(base, prop)
    assert not d.added and not d.removed
    assert [m.ref.id for m in d.modified] == ["site:corp"]
    assert d.touches("dhcp_scope")


def test_duplicate_scope_id_rejected():
    b = IRBuilder().add_device(sw("S"))
    b.add_dhcp_scope(DhcpScope(provider="site", network="corp"))
    with pytest.raises(IRValidationError):
        b.add_dhcp_scope(DhcpScope(provider="site", network="corp"))


def test_gateway_provider_must_be_a_known_device():
    b = IRBuilder().add_device(sw("S"))
    b.add_dhcp_scope(DhcpScope(provider="GW-MISSING", network="corp"))
    with pytest.raises(IRValidationError):
        b.build()


def test_scopes_sorted_by_id_in_built_ir():
    ir = _build(
        DhcpScope(provider="site", network="zeta"),
        DhcpScope(provider="site", network="alpha"),
    )
    assert [s.id for s in ir.dhcp_scopes] == ["site:alpha", "site:zeta"]


def test_trust_and_snooping_are_diffable_facts():
    from dataclasses import replace

    from tests.factories import trunk_port

    def build(trusted, snooping):
        b = IRBuilder().add_device(replace(sw("S"), dhcp_snooping=snooping))
        b.add_port(replace(trunk_port("S", "ge-0/0/1", tagged=(10,)), dhcp_trusted=trusted))
        return b.build()

    d = diff_ir(build(True, None), build(False, ("corp",)))
    assert d.touches("port") and d.touches("device")
