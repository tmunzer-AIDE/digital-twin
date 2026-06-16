"""Tests for switch compile sitetemplate layer (Task 7)."""

from digital_twin.adapters.mist.compile.switch import compile_site


def test_compile_site_includes_sitetemplate_layer():
    nt = {"networks": {"corp": {"vlan_id": 10}}}
    st = {"networks": {"corp": {"vlan_id": 20}}}
    ss = {"networks": {}}
    assert compile_site(nt, ss, sitetemplate=st)["networks"]["corp"]["vlan_id"] == 20
