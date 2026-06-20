from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(networks: dict) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"},
        setting={"networks": networks}, networktemplate=None, devices=(), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t",
                       fetched=("site", "setting", "devices"), failures=()),
    )


def _vlan(ir, vid):
    return ir.vlans[vid]


def test_two_names_same_vlan_id_records_collision():
    ir = MistAdapter().ingest(_raw({
        "corp": {"vlan_id": 10}, "guest": {"vlan_id": 10}, "iot": {"vlan_id": 30},
    })).ir
    assert _vlan(ir, 10).collisions == ("guest",)   # distinct OTHER name (winner=corp)
    assert _vlan(ir, 30).collisions == ()           # no collision


def test_repeated_same_name_is_not_a_collision():
    ir = MistAdapter().ingest(_raw({"corp": {"vlan_id": 10}})).ir
    assert _vlan(ir, 10).collisions == ()


def test_three_claimants_sorted_distinct_others():
    ir = MistAdapter().ingest(_raw({
        "corp": {"vlan_id": 10}, "lab": {"vlan_id": 10}, "guest": {"vlan_id": 10},
    })).ir
    # winner=corp (first); the other two, distinct + sorted
    assert _vlan(ir, 10).collisions == ("guest", "lab")
