from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(*, devices, setting) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1"},
        setting=setting,
        networktemplate=None,
        devices=tuple(devices),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(
            acquired_at=datetime.now(UTC),
            host="t",
            fetched=("site", "setting", "devices"),
            failures=(),
        ),
    )


def test_ospf_metric_minted_and_absent_is_none():
    dev = {
        "mac": "001122334455",
        "type": "switch",
        "name": "sw",
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {
            "corp": {"metric": 50}, "guest": {}, "mgmt": {"metric": 0}}}},
    }
    setting = {
        "networks": {
            "corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"},
            "guest": {"vlan_id": 20, "subnet": "10.0.1.0/24"},
            "mgmt": {"vlan_id": 30, "subnet": "10.0.2.0/24"},
        }
    }
    ir = MistAdapter().ingest(_raw(devices=[dev], setting=setting)).ir
    by_name = {o.network_name: o for o in ir.ospf_intfs}
    assert by_name["corp"].metric == 50
    assert by_name["guest"].metric is None  # absent -> None
    assert by_name["mgmt"].metric == 0  # falsy-but-valid: 0 survives, not None


def test_ospf_metric_templated_is_none_but_carries_unresolved_token():
    # present-but-unparseable metric: metric=None AND metric_unresolved keeps the raw token
    # so an absent->templated edit is NOT an empty diff (would otherwise false-SAFE).
    dev = {
        "mac": "001122334455",
        "type": "switch",
        "name": "sw",
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {"corp": {"metric": "{{cost}}"}, "iot": {}}}},
    }
    setting = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"},
                            "iot": {"vlan_id": 30, "subnet": "10.0.2.0/24"}}}
    ir = MistAdapter().ingest(_raw(devices=[dev], setting=setting)).ir
    by_name = {o.network_name: o for o in ir.ospf_intfs}
    assert by_name["corp"].metric is None and by_name["corp"].metric_unresolved == "{{cost}}"
    assert by_name["iot"].metric is None and by_name["iot"].metric_unresolved is None  # absent
