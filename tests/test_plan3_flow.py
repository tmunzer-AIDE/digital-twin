"""Plan-3 slice: envelope -> object gate -> field gate -> L0 -> apply -> compile
-> derived gate, in pipeline order, on synthetic Mist-shaped state.

The engine that sequences these is Plan 5; this test IS the sequence, proving
the pieces compose and that gates fire at the right stage for each scenario."""

from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.apply import get_object
from digital_twin.contracts import Rejection
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects

SITE = "s1"
SETTING = {
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {
        "office": {"mode": "access", "port_network": "corp"},
        "uplink": {"mode": "trunk", "port_network": "corp", "networks": ["voice"]},
    },
    "vars": {"dhcp_ip": "10.0.0.2"},
    "dhcpd_config": {"corp": {"ip": "{{dhcp_ip}}"}},
}
SWITCH = {
    "mac": "aa0000000001",
    "id": "dev-a",
    "type": "switch",
    "model": "EX4100-48P",
    "name": "sw-a",
    "port_config": {"ge-0/0/0-1": {"usage": "office"}},
}


def _raw() -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id=SITE),
        site={"id": SITE},
        setting=SETTING,
        networktemplate=None,
        devices=(SWITCH,),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=("devices",), failures=()),
    )


def _pipeline(plan_dict):
    """The Plan-3 slice in spec pipeline order; returns ('ok', ir_pair) or the Rejection."""
    plan = parse_change_plan(plan_dict)
    if isinstance(plan, Rejection):
        return plan
    rejection = check_objects(plan)
    if rejection:
        return rejection
    adapter, raw = MistAdapter(), _raw()
    for op in sorted(plan.ops, key=lambda o: o.order):  # rolling pre-op state
        current = get_object(raw, op.object_type, op.object_id)
        if current is None:
            return Rejection(stage="apply", reasons=(f"unknown {op.object_type}",))
        rejection = screen_op(op.object_type, current, op.payload)
        if rejection:
            return rejection
        l0 = adapter.validate(op)
        if l0.fatal:
            return Rejection(stage="l0", reasons=tuple(f.message for f in l0.findings))
        applied = adapter.apply(raw, (op,))
        assert isinstance(applied, RawSiteState)
        raw = applied
    baseline, proposed = adapter.ingest(_raw()), adapter.ingest(raw)
    rejection = check_derived(baseline.site_effective, proposed.site_effective)
    if rejection:
        return rejection
    for did, base_eff in baseline.device_effective.items():
        rejection = check_derived(
            base_eff, proposed.device_effective.get(did, {}), artifact=f"device {did}"
        )
        if rejection:
            return rejection
    return ("ok", (baseline.ir, proposed.ir))


def _plan(ops):
    return {"source": "mist", "scope": {"org_id": "o1", "site_id": SITE}, "ops": ops}


def test_in_scope_site_setting_change_passes_all_gates():
    new_setting = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    result = _pipeline(
        _plan(
            [
                {
                    "action": "update",
                    "order": 0,
                    "object_type": "site_setting",
                    "object_id": SITE,
                    "payload": new_setting,
                }
            ]
        )
    )
    assert isinstance(result, tuple) and result[0] == "ok"
    baseline_ir, proposed_ir = result[1]
    assert set(baseline_ir.vlans) == {10, 30}  # IR.vlans: Mapping[vlan_id, Vlan]
    assert set(proposed_ir.vlans) == {10, 31}  # the change reached the IR


def test_out_of_scope_raw_path_stops_at_field_gate():
    new_setting = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    result = _pipeline(
        _plan(
            [
                {
                    "action": "update",
                    "order": 0,
                    "object_type": "site_setting",
                    "object_id": SITE,
                    "payload": new_setting,
                }
            ]
        )
    )
    assert isinstance(result, Rejection) and result.stage == "field_gate"


def test_vars_ripple_stops_at_derived_gate():
    # raw change touches only vars.* (allowed) — but compiles into dhcpd_config
    new_setting = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    result = _pipeline(
        _plan(
            [
                {
                    "action": "update",
                    "order": 0,
                    "object_type": "site_setting",
                    "object_id": SITE,
                    "payload": new_setting,
                }
            ]
        )
    )
    assert isinstance(result, Rejection) and result.stage == "derived_gate"
    assert any("dhcpd_config" in reason for reason in result.reasons)


def test_template_op_stops_at_object_gate():
    result = _pipeline(
        _plan(
            [
                {
                    "action": "update",
                    "order": 0,
                    "object_type": "networktemplate",
                    "object_id": "nt1",
                    "payload": {},
                }
            ]
        )
    )
    assert isinstance(result, Rejection) and result.stage == "object_gate"


def test_malformed_envelope_stops_at_envelope():
    result = _pipeline({"source": "mist", "scope": {"org_id": "o1"}, "ops": "nope"})
    assert isinstance(result, Rejection) and result.stage == "envelope"


def test_device_port_change_flows_through_to_proposed_ir():
    new_device = {**SWITCH, "port_config": {"ge-0/0/0-1": {"usage": "uplink"}}}
    result = _pipeline(
        _plan(
            [
                {
                    "action": "update",
                    "order": 0,
                    "object_type": "device",
                    "object_id": "dev-a",
                    "payload": new_device,
                }
            ]
        )
    )
    assert isinstance(result, tuple)
    _, (baseline_ir, proposed_ir) = result
    port = proposed_ir.port("aa0000000001:ge-0/0/0")
    assert port.profile == "uplink"
    assert baseline_ir.port("aa0000000001:ge-0/0/0").profile == "office"
