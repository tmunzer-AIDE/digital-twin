"""Enrichment is annotation-only: identity from BASELINE, subnet from baseline
Vlan.subnet, dhcp_vlan_touched from the delta. None of it changes the verdict."""
from dataclasses import fields

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.wired.client_impact import _IDENTITY_FIELDS, ClientImpactCheck
from digital_twin.ir import (
    AttachKind,
    Client,
    ClientEnrichment,
    ClientKind,
    Device,
    DeviceRole,
    DhcpScope,
    IRCapability,
    Port,
    PortMode,
    Vlan,
)
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _build(*, native: int, subnet=None, enrich=None, dhcp_trusted=None,
           dhcp_sources=(), snooping=None, scope=False) -> object:
    """vlan 10 ('corp'). Optional dhcp dimensions exercise _dhcp_vlan_touched's
    four triggers: dhcp_sources, a serving DhcpScope, device.dhcp_snooping, port trust."""
    b = IRBuilder()
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.add_device(Device(id="sw1", role=DeviceRole.SWITCH, site="s1", dhcp_snooping=snooping))
    # Port.mode is REQUIRED (entities.py:116) — set it explicitly
    p = Port(id="sw1:ge-0/0/1", device_id="sw1", name="ge-0/0/1", mode=PortMode.ACCESS,
             native_vlan=native, dhcp_trusted=dhcp_trusted)
    b.add_port(p)
    b.add_vlan(Vlan(vlan_id=10, name="corp", subnet=subnet, dhcp_sources=tuple(dhcp_sources)))
    if scope:  # provider="site" needs no gateway device (build validation skips it)
        b.add_dhcp_scope(DhcpScope(provider="site", network="corp", vlan=10))
    b.add_client(Client(mac="aabbcc000001", kind=ClientKind.WIRED,
                        attach_kind=AttachKind.PORT, attach_id="sw1:ge-0/0/1", vlan=10))
    b.set_client_enrichment(enrich or {})  # atomic publish (see Task 3)
    return b.build()


def _ctx(base, prop) -> CheckContext:
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_identity_fields_stay_in_sync_with_the_record():
    # adding a NEW ClientEnrichment field forces a conscious choice here, so a fresh
    # identity field can never silently drop out of evidence, and `meta` can never leak in.
    assert set(_IDENTITY_FIELDS) | {"meta"} == {f.name for f in fields(ClientEnrichment)}


def test_identity_from_baseline_and_subnet():
    enrich = {"aabbcc000001": ClientEnrichment(hostname="r2d2", family="Printer", mfg="HP")}
    base = _build(native=10, subnet="10.0.0.0/24", enrich=enrich)
    prop = _build(native=20, subnet="10.0.0.0/24", enrich={})  # vlan_move; proposed has NO enrich
    res = ClientImpactCheck().run(_ctx(base, prop))
    entry = res.findings[0].evidence["impacts"][0]
    assert entry["impact"] == "vlan_move"
    assert entry["identity"] == {"hostname": "r2d2", "family": "Printer", "mfg": "HP"}
    assert entry["subnet"] == "10.0.0.0/24"            # from BASELINE vlan
    assert entry["dhcp_vlan_touched"] is False


def test_identity_omitted_when_no_enrichment():
    base = _build(native=10)
    prop = _build(native=20)
    entry = ClientImpactCheck().run(_ctx(base, prop)).findings[0].evidence["impacts"][0]
    assert "identity" not in entry and entry["subnet"] is None


def _touched(base, prop) -> bool:
    # every arm moves native 10->20 so client.impact emits a vlan_move entry to annotate
    entry = ClientImpactCheck().run(_ctx(base, prop)).findings[0].evidence["impacts"][0]
    return entry["dhcp_vlan_touched"]


def test_dhcp_vlan_touched_on_port_trust_flip():  # (d)
    assert _touched(_build(native=10, dhcp_trusted=True),
                    _build(native=20, dhcp_trusted=False)) is True


def test_dhcp_vlan_touched_on_dhcp_sources_change():  # (a)
    assert _touched(_build(native=10, dhcp_sources=()),
                    _build(native=20, dhcp_sources=("site",))) is True


def test_dhcp_vlan_touched_on_serving_scope_change():  # (b)
    assert _touched(_build(native=10, scope=False),
                    _build(native=20, scope=True)) is True


def test_dhcp_vlan_touched_on_applicable_snooping_change():  # (c) snooping now covers corp
    assert _touched(_build(native=10, dhcp_sources=("site",), snooping=None),
                    _build(native=20, dhcp_sources=("site",), snooping=("*",))) is True


def test_dhcp_vlan_NOT_touched_when_snooping_change_misses_client_vlan():  # (c) negative
    assert _touched(_build(native=10, dhcp_sources=("site",), snooping=("other",)),
                    _build(native=20, dhcp_sources=("site",), snooping=("other", "extra"))) is False
