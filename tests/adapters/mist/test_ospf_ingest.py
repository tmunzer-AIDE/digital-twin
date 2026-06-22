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
        "ospf_areas": {"0": {"networks": {"corp": {"metric": 50}, "guest": {}}}},
    }
    setting = {
        "networks": {
            "corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"},
            "guest": {"vlan_id": 20, "subnet": "10.0.1.0/24"},
        }
    }
    ir = MistAdapter().ingest(_raw(devices=[dev], setting=setting)).ir
    by_name = {o.network_name: o for o in ir.ospf_intfs}
    assert by_name["corp"].metric == 50
    assert by_name["guest"].metric is None  # absent -> None


def test_ospf_metric_templated_is_none():
    dev = {
        "mac": "001122334455",
        "type": "switch",
        "name": "sw",
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {"corp": {"metric": "{{cost}}"}}}},
    }
    setting = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"}}}
    ir = MistAdapter().ingest(_raw(devices=[dev], setting=setting)).ir
    assert next(o for o in ir.ospf_intfs if o.network_name == "corp").metric is None
