"""simulate(): the 10-stage sequence with every failure a value -> decision."""

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


def test_merge_payloads_lets_partial_payloads_through():
    # a hand-written PARTIAL payload omits out-of-scope fields the current
    # object has -> full-PUT semantics flag every omission (correct but harsh);
    # merge mode overlays the partial payload onto the fetched current object
    partial = {
        "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}},  # the change
        "port_usages": dict(SETTING["port_usages"]),
        "vars": dict(SETTING["vars"]),
        # dhcpd_config intentionally OMITTED
    }
    strict = simulate(_plan([_op(payload=partial)]), provider=FakeProvider())
    assert strict.decision is Decision.UNKNOWN  # omission = deletion = out of scope

    merged = simulate(_plan([_op(payload=partial)]), provider=FakeProvider(), merge_payloads=True)
    assert merged.decision is not Decision.UNKNOWN  # current fields preserved
    assert merged.check_results  # checks actually ran


def test_merge_payloads_null_still_deletes():
    # in merge mode an EXPLICIT null is the delete operator — and deleting an
    # out-of-scope field is still out of scope
    partial = {"dhcpd_config": None}
    v = simulate(_plan([_op(payload=partial)]), provider=FakeProvider(), merge_payloads=True)
    assert v.decision is Decision.UNKNOWN
    assert any("dhcpd_config" in r for r in v.decision_reasons)
