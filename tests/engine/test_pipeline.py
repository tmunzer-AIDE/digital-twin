"""simulate(): the 10-stage sequence with every failure a value -> decision."""

from dataclasses import replace as dc_replace
from datetime import UTC, datetime

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import FetchError, RawSiteState, SiteScope, StateMeta
from digital_twin.verdict.decision import Decision

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


class FakeProvider:
    def __init__(self, raw=None):
        self._raw = raw if raw is not None else _raw()

    def fetch_site(self, scope, *, include_derived=False):
        return self._raw

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        return {SITE: self._raw}


def _plan(ops):
    return {"source": "mist", "scope": {"org_id": "o1", "site_id": SITE}, "ops": ops}


def _op(object_type="site_setting", object_id=SITE, payload=None, order=0):
    return {
        "action": "update",
        "order": order,
        "object_type": object_type,
        "object_id": object_id,
        "payload": payload if payload is not None else dict(SETTING),
    }


class NeverFetch:
    def fetch_site(self, scope, *, include_derived=False):
        raise AssertionError("fetch must not run before the pre-fetch stages")

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        raise AssertionError("fetch must not run before the pre-fetch stages")


def test_malformed_envelope_unknown_without_fetch():
    v = simulate({"source": "mist", "ops": "nope"}, provider=NeverFetch())
    assert v.decision is Decision.UNKNOWN
    assert any("envelope" in r for r in v.decision_reasons)


def test_unsupported_object_type_unknown_pre_fetch():
    v = simulate(
        _plan([_op(object_type="networktemplate", object_id="nt1", payload={})]),
        provider=NeverFetch(),
    )
    assert v.decision is Decision.UNKNOWN
    assert any("object_gate" in r for r in v.decision_reasons)


def test_total_fetch_failure_is_unknown():
    err = FetchError(
        scope=SiteScope("o1", SITE), failures=(), acquired_at=datetime.now(UTC), host="t"
    )
    v = simulate(_plan([_op()]), provider=FakeProvider(raw=err))
    assert v.decision is Decision.UNKNOWN
    assert any("baseline" in r or "fetch" in r for r in v.decision_reasons)


def test_total_fetch_failure_still_carries_state_meta():
    # FetchError has host/acquired_at/failures — agents must see WHAT failed
    # even when no baseline is usable (acceptance: state_meta rides every
    # verdict that had a fetch)
    from digital_twin.providers.base import FetchFailure

    err = FetchError(
        scope=SiteScope("o1", SITE),
        failures=(FetchFailure(object="setting", error="503"),),
        acquired_at=datetime.now(UTC),
        host="api.test",
    )
    v = simulate(_plan([_op()]), provider=FakeProvider(raw=err))
    assert v.state_meta is not None
    assert v.state_meta.host == "api.test"
    assert ("setting", "503") in v.state_meta.fetch_failures


def test_out_of_scope_raw_path_unknown():
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("field_gate" in r for r in v.decision_reasons)


def test_vars_ripple_unknown_at_derived_gate():
    ripple = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    v = simulate(_plan([_op(payload=ripple)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("derived_gate" in r for r in v.decision_reasons)


def test_unknown_target_object_is_unknown():
    v = simulate(
        _plan([_op(object_type="device", object_id="ghost", payload={"name": "x"})]),
        provider=FakeProvider(),
    )
    assert v.decision is Decision.UNKNOWN
    assert any("apply" in r for r in v.decision_reasons)


def test_l0_findings_reach_verdict():
    bad_type = {**SETTING, "networks": "oops"}
    v = simulate(_plan([_op(payload=bad_type)]), provider=FakeProvider())
    # the field gate fires too (networks subtree replaced by a string), but the
    # L0 finding must be present in the flat findings list regardless
    assert any(f.code.startswith("l0.schema") for f in v.findings)


def test_in_scope_change_runs_checks_and_carries_state_meta():
    new = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=new)]), provider=FakeProvider())
    assert v.decision is not Decision.UNKNOWN
    assert v.check_results  # checks ran
    assert v.state_meta is not None and v.state_meta.host == "t"
    assert v.trace_ref  # a run id
    assert not v.ir_diff.is_empty()


def test_cosmetic_noop_is_safe():
    v = simulate(_plan([_op()]), provider=FakeProvider())  # payload == current
    assert v.decision is Decision.SAFE


def test_partial_payload_with_omitted_roots_passes_natively():
    # Mist update semantics (confirmed): ROOT attributes omitted from the
    # payload PERSIST — they are not deleted. A partial payload touching only
    # in-scope roots must pass the gates without any special mode.
    partial = {
        "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}},  # the change
        # port_usages / vars / dhcpd_config intentionally OMITTED -> kept
    }
    v = simulate(_plan([_op(payload=partial)]), provider=FakeProvider())
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    assert v.check_results  # checks actually ran


def test_dash_marker_deletion_of_out_of_scope_root_is_unknown():
    # deletion is explicit ({"-attribute": ""}) — and deleting an out-of-scope
    # root is still out of scope
    v = simulate(_plan([_op(payload={"-dhcpd_config": ""})]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("dhcpd_config" in r for r in v.decision_reasons)


def test_conflicting_set_and_delete_is_unknown():
    payload = {"networks": dict(SETTING["networks"]), "-networks": ""}
    v = simulate(_plan([_op(payload=payload)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("conflict" in r.lower() for r in v.decision_reasons)


def _switch_with_extra_routes():
    # extra_routes.*.via is typed `string` in the committed OAS but Mist stores an
    # ARRAY of next-hops — a live, already-accepted config the twin must not flag.
    return {**SWITCH, "id": "dev-er", "mac": "aa0000000099",
            "extra_routes": {"1.2.3.4/32": {"via": ["1.1.1.1"]}}}


def test_l0_scopes_to_changed_roots_by_default():
    # the op touches only `notes`; the persisted extra_routes root (stale OAS
    # type) must NOT surface a schema violation by default
    raw = dc_replace(_raw(), devices=(_switch_with_extra_routes(),))
    plan = _plan([_op(object_type="device", object_id="dev-er", payload={"notes": "x"})])
    v = simulate(plan, provider=FakeProvider(raw=raw))
    assert not any(f.code.startswith("l0.schema") for f in v.findings), v.findings


def test_l0_full_object_option_surfaces_untouched_root_violation():
    # opt-in whole-object validation re-checks persisted roots too
    raw = dc_replace(_raw(), devices=(_switch_with_extra_routes(),))
    plan = _plan([_op(object_type="device", object_id="dev-er", payload={"notes": "x"})])
    v = simulate(plan, provider=FakeProvider(raw=raw), l0_full_object=True)
    assert any(f.code.startswith("l0.schema") for f in v.findings)


def test_l0_findings_name_their_object():
    # every L0 finding must carry a subject identifying WHICH object it's about —
    # the device op's id/type plus the friendly name from the fetched config
    raw = dc_replace(_raw(), devices=(_switch_with_extra_routes(),))
    plan = _plan([_op(object_type="device", object_id="dev-er", payload={"notes": "x"})])
    v = simulate(plan, provider=FakeProvider(raw=raw), l0_full_object=True)
    l0 = [f for f in v.findings if f.code.startswith("l0.schema")]
    assert l0, "expected an L0 finding to stamp"
    subj = l0[0].subject
    assert subj is not None
    assert subj.kind == "device"
    assert subj.id == "dev-er"
    assert subj.name == "sw-a"  # SWITCH fixture's name, carried from fetched config


def test_partial_device_payload_passes_l0_required():
    # L0 validates the EFFECTIVE object (current + update), so a partial device
    # payload without top-level 'type' (schema-required) yields no false finding
    partial_dev = {"name": "renamed"}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=partial_dev)]),
        provider=FakeProvider(),
    )
    assert not any("'type' is a required property" in f.message for f in v.findings)
    assert v.decision is not Decision.UNKNOWN


def test_normal_verdict_carries_diagrams():
    payload = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=payload)]), provider=FakeProvider())
    assert v.diagrams  # non-empty: at least the L2 chart
    assert any(d.view == "l2" for d in v.diagrams)


def test_unknown_short_circuit_has_no_diagrams():
    # an out-of-scope raw path returns via _unknown() -> no diagrams
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert v.diagrams == ()


def test_simulate_rejects_org_plan_with_unknown_not_crash():
    from digital_twin.engine.pipeline import simulate
    from digital_twin.verdict.decision import Decision

    class _AnyProvider:
        pass

    plan = {"source": "mist", "scope": {"org_id": "o1"},  # no site_id
            "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                     "object_id": "nt1", "payload": {}}]}
    v = simulate(plan, provider=_AnyProvider())  # guarded before fetch — never touches provider
    assert v.decision is Decision.UNKNOWN
    assert any("simulate_org_template" in r for r in v.decision_reasons)
