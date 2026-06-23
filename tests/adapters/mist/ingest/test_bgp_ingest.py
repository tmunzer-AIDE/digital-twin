"""GS28: role-aware _bgp ingest pass tests (switch + gateway)."""
from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.ir import DeviceRole
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _base_raw(**overrides) -> dict:
    """Common RawSiteState kwargs."""
    return {
        "scope": SiteScope(org_id="o1", site_id="s1"),
        "site": {"id": "s1"},
        "setting": {},
        "networktemplate": None,
        "sitetemplate": None,
        "gatewaytemplate": None,
        "devices": (),
        "device_stats": (),
        "port_stats": (),
        "wireless_clients": (),
        "wired_clients": (),
        "derived_setting": None,
        "meta": StateMeta(
            acquired_at=datetime.now(UTC),
            host="t",
            fetched=("site", "setting", "devices"),
            failures=(),
        ),
        **overrides,
    }


def make_raw_switch(bgp_config: dict) -> RawSiteState:
    """RawSiteState with one switch whose effective config has bgp_config.

    The switch ingester reads bgp_config from device/site effective config
    (ctx.device_effective.get(did) or ctx.site_effective). We put bgp_config
    in the setting (site_effective) so it flows through compile_site."""
    return RawSiteState(
        **_base_raw(
            setting={"bgp_config": bgp_config},
            devices=(
                {"mac": "001122334455", "type": "switch", "name": "sw1"},
            ),
        )
    )


def make_raw_gateway(bgp_config: dict) -> RawSiteState:
    """RawSiteState with a gateway device whose bgp_config is in the gatewaytemplate.

    Gateway peers are minted from the materialized bgp_config (via _materialize
    in adapter.py which copies gatewaytemplate-compiled keys onto the raw device).
    We put bgp_config in the gatewaytemplate so compile_gateway_device includes it,
    and _materialize surfaces it onto dev."""
    return RawSiteState(
        **_base_raw(
            setting={},
            gatewaytemplate={"bgp_config": bgp_config},
            devices=(
                {"mac": "aabbccddeeff", "type": "gateway", "name": "gw1"},
            ),
        )
    )


def _peers(ir):
    return {p.neighbor_ip: p for p in ir.bgp_peers}


def test_switch_bgp_minted_from_effective(make_raw_switch=make_raw_switch):
    # make_raw_switch: a RawSiteState with one switch whose effective config has bgp_config.
    raw = make_raw_switch(bgp_config={
        "underlay": {"type": "external", "local_as": 65000,
                     "neighbors": {"10.0.0.2": {"neighbor_as": 65001},
                                   "10.0.0.3": {"neighbor_as": 65002, "disabled": True}}}})
    ir = MistAdapter().ingest(raw).ir
    peers = _peers(ir)
    assert peers["10.0.0.2"].role is DeviceRole.SWITCH
    assert peers["10.0.0.2"].local_as == 65000 and peers["10.0.0.2"].neighbor_as == 65001
    assert peers["10.0.0.2"].session_type == "external"
    assert peers["10.0.0.3"].disabled is True


def test_templated_tokens_carried_not_collapsed(make_raw_switch=make_raw_switch):
    raw = make_raw_switch(bgp_config={
        "s": {"type": "{{kind}}", "local_as": "{{asn}}",
              "neighbors": {"10.0.0.2": {"neighbor_as": "{{peer_asn}}", "disabled": "{{flag}}"}}}})
    p = _peers(MistAdapter().ingest(raw).ir)["10.0.0.2"]
    assert p.session_type is None and p.session_type_unresolved == "{{kind}}"
    assert p.local_as is None and p.local_as_unresolved == "{{asn}}"
    assert p.neighbor_as is None and p.neighbor_as_unresolved == "{{peer_asn}}"
    assert p.disabled is False and p.disabled_unresolved == "{{flag}}"


def test_non_literal_neighbor_ip_is_unresolved(make_raw_switch=make_raw_switch):
    raw = make_raw_switch(bgp_config={
        "s": {"type": "external", "local_as": 65000,
              "neighbors": {"{{peer}}": {"neighbor_as": 65001}}}})
    peers = _peers(MistAdapter().ingest(raw).ir)
    assert peers["{{peer}}"].unresolved is True


def test_same_neighbor_two_sessions_differing_attrs_is_ambiguous(make_raw_switch=make_raw_switch):
    raw = make_raw_switch(bgp_config={
        "a": {"type": "external", "local_as": 65000,
              "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}},
        "b": {"type": "internal", "local_as": 65000,
              "neighbors": {"10.0.0.2": {"neighbor_as": 65000}}}})
    peers = _peers(MistAdapter().ingest(raw).ir)
    assert peers["10.0.0.2"].ambiguous is True  # one peer, marked ambiguous, NOT last-win


def test_gateway_bgp_minted_from_materialized_config(make_raw_gateway=make_raw_gateway):
    # make_raw_gateway: builds a RawSiteState with a gateway device + gatewaytemplate
    # whose gateway_effective bgp_config has `via`.
    raw = make_raw_gateway(bgp_config={
        "wan": {"type": "external", "local_as": 65000, "via": "wan",
                "neighbors": {"203.0.113.1": {"neighbor_as": 65010}}}})
    p = _peers(MistAdapter().ingest(raw).ir)["203.0.113.1"]
    assert p.role is DeviceRole.GATEWAY and p.via == "wan"
