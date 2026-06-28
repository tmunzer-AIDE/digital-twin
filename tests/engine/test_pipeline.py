"""simulate(): the 10-stage sequence with every failure a value -> decision."""

from dataclasses import replace as dc_replace
from datetime import UTC, datetime

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired.wlan_client_impact import WlanClientImpactCheck
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.engine.pipeline import simulate
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.providers.base import FetchError, RawSiteState, SiteScope, StateMeta
from digital_twin.redaction import REDACTED
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
AP = {"mac": "cc0000000001", "id": "ap-a", "type": "ap", "model": "AP45", "name": "ap-a"}


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


def _delete_op(object_type="wlan", object_id="w1", order=0):
    return {
        "action": "delete",
        "order": order,
        "object_type": object_type,
        "object_id": object_id,
        "payload": {},
    }


def _wlan(wid="w1", *, ssid="corp", enabled=True, for_site=True, template_id=None):
    row = {
        "id": wid,
        "name": f"{ssid}-{wid}",
        "ssid": ssid,
        "enabled": enabled,
        "for_site": for_site,
        "isolation": False,
        "apply_to": "site",
    }
    if template_id is not None:
        row["template_id"] = template_id
    return row


def _wireless_client(mac="11:22:33:44:55:66", *, ssid="corp"):
    return {"mac": mac, "ap_mac": AP["mac"], "ssid": ssid, "vlan_id": 10}


def _raw_wlan(*wlans, clients=()):
    return dc_replace(
        _raw(),
        devices=(SWITCH, AP),
        wlans=tuple(wlans),
        wireless_clients=tuple(clients),
        wired_clients=(),
        meta=StateMeta(
            acquired_at=datetime.now(UTC),
            host="t",
            fetched=("devices", "wlans", "wireless_clients", "wired_clients"),
            failures=(),
        ),
    )


def _wlan_registry():
    return CheckRegistry([WlanClientImpactCheck()])


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
    assert any(r.startswith("COVERAGE GAP [derived_gate]") for r in v.decision_reasons)
    assert v.check_results
    assert any(f.code == "coverage.gap" for f in v.findings)


class _NetworkErrorRegistry(CheckRegistry):
    def __init__(self) -> None:
        pass

    def run_all(self, ctx: CheckContext) -> tuple[CheckResult, ...]:
        return (
            CheckResult(
                check_id="fake.network",
                status=Status.FAIL,
                findings=(
                    Finding(
                        source=FindingSource.CHECK,
                        category=FindingCategory.NETWORK,
                        code="fake.network.error",
                        severity=Severity.ERROR,
                        confidence=Confidence(level=ConfidenceLevel.HIGH),
                        message="modeled network breakage",
                        subject=ObjectRef("vlan", "10"),
                    ),
                ),
                coverage=Coverage(state=CoverageState.COMPLETE),
                confidence=Confidence(level=ConfidenceLevel.HIGH),
                reasoning="forced modeled failure",
            ),
        )


def test_coverage_gap_plus_modeled_network_error_is_unsafe():
    ripple = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    v = simulate(
        _plan([_op(payload=ripple)]),
        provider=FakeProvider(),
        registry=_NetworkErrorRegistry(),
    )
    assert v.decision is Decision.UNSAFE
    assert any(f.code == "coverage.gap" for f in v.findings)
    assert any(f.code == "fake.network.error" for f in v.findings)


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
    # extra_routes.*.via is typed (array of next-hops) in the refreshed OAS; an int
    # is a real L0 TYPE violation on a persisted root the op below does not touch.
    return {**SWITCH, "id": "dev-er", "mac": "aa0000000099",
            "extra_routes": {"1.2.3.4/32": {"via": 123}}}


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


def test_unknown_attribute_on_switch_port_config_surfaces_and_is_unknown():
    # the motivating case: an agent proposes `disabled` on a SWITCH port_config entry
    # — a field the switch OAS does not document (it is gateway-only). The walker flags
    # it precisely (l0.schema.unknown_attribute), and since `disabled` is also an
    # unmodeled leaf the field gate floors to UNKNOWN. UNKNOWN wins, but the "not in
    # the OAS" finding rides along to tell the operator WHICH attribute is bogus.
    payload = {"port_config": {"ge-0/0/10": {"usage": "office", "disabled": True}}}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
        provider=FakeProvider(),
    )
    hits = [f for f in v.findings if f.code == "l0.schema.unknown_attribute"]
    assert hits, v.findings
    assert any("disabled" in f.evidence.get("path", "") for f in hits)
    assert v.decision is Decision.UNKNOWN, v.decision_reasons


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


def test_site_update_carries_config_diff():
    new = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=new)]), provider=FakeProvider())
    assert v.decision is not Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds and cds[SITE].object_type == "site_setting" and cds[SITE].action == "update"
    by = {c.path: c for c in cds[SITE].changes}
    assert by["networks.voice.vlan_id"].kind == "changed"
    assert by["networks.voice.vlan_id"].before == 30 and by["networks.voice.vlan_id"].after == 31


def test_field_gate_unknown_carries_config_diff():
    # dhcpd_config.corp.ip is OUT of scope (only type/servers/ip_start/ip_end/gateway
    # are allowlisted) -> field-gate UNKNOWN, in-loop. The diff must now be surfaced.
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN                      # non-load-bearing:
    cds = {d.object_id: d for d in v.config_diffs}             # UNKNOWN + diff coexist
    assert SITE in cds
    assert "dhcpd_config.corp.ip" in {c.path for c in cds[SITE].changes}


def test_derived_gate_unknown_carries_config_diff():
    # vars ripple passes the field gate (vars.* allowlisted) then fails the DERIVED
    # gate inside _simulate_site_state -> post-apply UNKNOWN reached via the final
    # unconditional attach.
    ripple = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    v = simulate(_plan([_op(payload=ripple)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any(r.startswith("COVERAGE GAP [derived_gate]") for r in v.decision_reasons)
    assert v.check_results
    assert any(f.code == "coverage.gap" for f in v.findings)
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds
    assert "vars.dhcp_ip" in {c.path for c in cds[SITE].changes}


def test_object_not_found_keeps_earlier_op_diffs():
    # op0 (site_setting, valid) builds a diff and applies; op1 (device, missing id)
    # hits object-not-found IN the loop -> UNKNOWN. op0's diff must survive.
    good = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    plan = _plan([
        _op(object_type="site_setting", object_id=SITE, payload=good, order=0),
        _op(object_type="device", object_id="nope", payload={"name": "x"}, order=1),
    ])
    v = simulate(plan, provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds                                  # earlier op survived
    assert "nope" not in cds                            # uncomputable op carries nothing


def test_out_of_scope_secret_leaf_redacted_in_surfaced_diff():
    # switch_mgmt.root_password is out-of-scope (field-gate UNKNOWN) AND secret-keyed.
    # Now that the diff is surfaced, the value must still be redacted.
    secret = {**SETTING, "switch_mgmt": {"root_password": "hunter2"}}
    v = simulate(_plan([_op(payload=secret)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    by = {c.path: c for c in cds[SITE].changes}
    assert by["switch_mgmt.root_password"].after == REDACTED
    assert by["switch_mgmt.root_password"].after != "hunter2"


def test_site_l0_fatal_carries_config_diff(monkeypatch):
    # Force L0 fatal (unreachable with natural payloads — effective is always a dict).
    # The diff is built BEFORE validate, so the L0-fatal early return must carry it.
    from digital_twin.adapters.mist.adapter import MistAdapter
    from digital_twin.adapters.mist.validate import L0Result
    monkeypatch.setattr(
        MistAdapter, "validate", lambda self, op, **k: L0Result(findings=(), fatal=True)
    )
    good = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=good)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds


def test_site_apply_reject_carries_config_diff(monkeypatch):
    # Force adapter.apply to reject (post-screen_op, post-build). The in-loop apply
    # rejection early return must carry the already-built diff.
    from digital_twin.adapters.mist.adapter import MistAdapter
    from digital_twin.contracts import Rejection
    monkeypatch.setattr(MistAdapter, "apply",
                        lambda self, raw, ops: Rejection(stage="apply", reasons=("forced",)))
    good = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=good)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds


def test_site_wlan_delete_with_active_client_is_unsafe_and_carries_config_diff():
    raw = _raw_wlan(_wlan("w1"), clients=(_wireless_client(),))
    v = simulate(
        _plan([_delete_op("wlan", "w1")]),
        provider=FakeProvider(raw=raw),
        registry=_wlan_registry(),
    )
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert any(f.code == "wireless.wlan.client_impact.coverage_lost" for f in v.findings)
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["w1"].object_type == "wlan"
    assert cds["w1"].action == "delete"
    assert any(c.path == "ssid" and c.kind == "removed" for c in cds["w1"].changes)


def test_site_wlan_disable_with_active_client_is_unsafe_and_carries_config_diff():
    raw = _raw_wlan(_wlan("w1"), clients=(_wireless_client(),))
    v = simulate(
        _plan([_op(object_type="wlan", object_id="w1", payload={"enabled": False})]),
        provider=FakeProvider(raw=raw),
        registry=_wlan_registry(),
    )
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert any(f.code == "wireless.wlan.client_impact.coverage_lost" for f in v.findings)
    cds = {d.object_id: d for d in v.config_diffs}
    by = {c.path: c for c in cds["w1"].changes}
    assert by["enabled"].kind == "changed"
    assert by["enabled"].before is True and by["enabled"].after is False


def test_site_wlan_delete_with_site_scope_survivor_is_safe_and_carries_config_diff():
    raw = _raw_wlan(_wlan("w1"), _wlan("w2"), clients=(_wireless_client(),))
    v = simulate(
        _plan([_delete_op("wlan", "w1")]),
        provider=FakeProvider(raw=raw),
        registry=_wlan_registry(),
    )
    assert v.decision is Decision.SAFE, v.decision_reasons
    assert v.check_results[0].status is Status.PASS
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["w1"].action == "delete"


def test_inherited_wlan_delete_is_unknown_and_keeps_computable_diff():
    raw = _raw_wlan(
        _wlan("w1", for_site=False, template_id="tmpl1"),
        clients=(_wireless_client(),),
    )
    v = simulate(
        _plan([_delete_op("wlan", "w1")]),
        provider=FakeProvider(raw=raw),
        registry=_wlan_registry(),
    )
    assert v.decision is Decision.UNKNOWN
    assert any("inherited" in reason for reason in v.decision_reasons)
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["w1"].action == "delete"


def test_missing_wlan_delete_is_unknown_without_fabricated_diff():
    raw = _raw_wlan(_wlan("w1"), clients=(_wireless_client(),))
    v = simulate(
        _plan([_delete_op("wlan", "missing")]),
        provider=FakeProvider(raw=raw),
        registry=_wlan_registry(),
    )
    assert v.decision is Decision.UNKNOWN
    assert any("no wlan with id 'missing'" in reason for reason in v.decision_reasons)
    assert v.config_diffs == ()


def test_port_config_overwrite_disable_is_simulated_not_unknown():
    # the reported bug: disabling ports via port_config_overwrite must simulate
    # (admin_disable + blackhole), not return UNKNOWN. Baseline gives ge-0/0/47 a
    # trunk uplink so the blast radius is real (REVIEW or UNSAFE, never INFO/SAFE).
    switch_with_trunk = {
        **SWITCH,
        "port_config": {**SWITCH["port_config"], "ge-0/0/47": {"usage": "uplink"}},
    }
    raw = dc_replace(_raw(), devices=(switch_with_trunk,))
    payload = {"port_config_overwrite": {"ge-0/0/47": {"disabled": True}}}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
        provider=FakeProvider(raw=raw),
    )
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    codes = {f.code for f in v.findings}
    assert "wired.port.admin_disable.impact" in codes, codes
    assert v.decision in (Decision.REVIEW, Decision.UNSAFE), v.decision


def test_local_port_auth_change_is_simulated_not_unknown():
    # enabling dot1x via local_port_config on a configured access port must
    # SIMULATE (REVIEW via wired.auth.access_change), not return UNKNOWN.
    # ge-0/0/0 must be locally-overridable up front (no_local_overwrite defaults
    # True, which would discard the local auth), so the local port_auth
    # deterministically reaches the resolver/check.
    sw_a = {**SWITCH, "port_config": {
        **SWITCH["port_config"], "ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    raw = dc_replace(_raw(), devices=(sw_a,))
    payload = {"local_port_config": {"ge-0/0/0": {"port_auth": "dot1x"}}}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
        provider=FakeProvider(raw=raw),
    )
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    codes = {f.code for f in v.findings}
    assert any(c.startswith("wired.auth.access_change") for c in codes), codes
    assert v.decision is Decision.REVIEW, v.decision


def test_voip_removal_flags_active_phone_e2e():
    # baseline: a phone access port offers data VLAN 10 + voice VLAN 30, with a
    # live phone on VLAN 30; the op drops voip_network from the phone usage ->
    # the port stops offering VLAN 30 -> client.impact vlan_removed (the VLAN may
    # still be healthy elsewhere, so blackhole would miss it).
    phone = {"mode": "access", "port_network": "corp", "voip_network": "voice"}
    setting = {**SETTING, "port_usages": {**SETTING["port_usages"], "phone": phone}}
    sw_a = {**SWITCH, "port_config": {
        "ge-0/0/0": {"usage": "phone"}, "ge-0/0/1": {"usage": "office"}}}
    raw0 = _raw()
    raw = dc_replace(
        raw0, setting=setting, devices=(sw_a,),
        wired_clients=({"device_mac": "aa0000000001", "port_id": "ge-0/0/0",
                        "mac": "ph01", "vlan": 30},),
        meta=dc_replace(raw0.meta, fetched=("devices", "wired_clients", "wireless_clients")),
    )
    # proposed: phone usage WITHOUT voip_network (partial payload — other roots persist)
    new_usages = {**setting["port_usages"], "phone": {"mode": "access", "port_network": "corp"}}
    v = simulate(_plan([_op(payload={"port_usages": new_usages})]), provider=FakeProvider(raw=raw))
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    assert "wired.client.impact.active_clients" in {f.code for f in v.findings}
    impacts = next(f for f in v.findings
                   if f.code == "wired.client.impact.active_clients").evidence["impacts"]
    assert any(i["impact"] == "vlan_removed" for i in impacts)
    assert v.decision is Decision.REVIEW


def test_mac_limit_lowered_below_clients_is_review():
    # a port with 2 observed wired clients; lowering mac_limit to 1 -> REVIEW.
    # CLIENTS_ACTIVE requires BOTH client keys in meta.fetched (ingest/clients.py);
    # _raw() ships fetched=("devices",), so widen it or this hits .unverified.
    sw_a = {**SWITCH, "port_config": {
        **SWITCH["port_config"], "ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    raw0 = _raw()
    raw = dc_replace(
        raw0, devices=(sw_a,),
        wired_clients=(
            {"device_mac": "aa0000000001", "port_id": "ge-0/0/0", "mac": "c1", "vlan": 10},
            {"device_mac": "aa0000000001", "port_id": "ge-0/0/0", "mac": "c2", "vlan": 10},
        ),
        meta=dc_replace(raw0.meta, fetched=("devices", "wired_clients", "wireless_clients")),
    )
    payload = {"local_port_config": {"ge-0/0/0": {"mac_limit": 1}}}
    v = simulate(_plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
                 provider=FakeProvider(raw=raw))
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    codes = {f.code for f in v.findings}
    assert "wired.port.mac_limit_exceeded.exceeded" in codes, codes
    assert v.decision is Decision.REVIEW


def test_enable_qos_change_is_review_not_unknown():
    sw_a = {**SWITCH, "port_config": {
        **SWITCH["port_config"], "ge-0/0/0": {"usage": "office", "no_local_overwrite": False}}}
    raw = dc_replace(_raw(), devices=(sw_a,))
    payload = {"local_port_config": {"ge-0/0/0": {"enable_qos": True}}}
    v = simulate(_plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
                 provider=FakeProvider(raw=raw))
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    assert any(c.startswith("wired.port.unmodeled_change") for c in {f.code for f in v.findings})
    assert v.decision is Decision.REVIEW


def test_l1_forced_vs_autonegotiating_peer_is_simulated_not_unknown():
    # pinning one end of a trunk uplink to a forced speed/duplex while the peer
    # autonegotiates must SIMULATE (REVIEW via autoneg_mismatch), not UNKNOWN.
    # The L1 check walks BoundaryView, so a REAL link is required — build a second
    # switch + two-sided LLDP port_stats. Both ends use the EXPLICIT `uplink`
    # usage (SETTING defines it) so the peer is config-stated (-> .autoneg_mismatch,
    # not .unverified).
    sw_a = {**SWITCH, "port_config": {**SWITCH["port_config"], "ge-0/0/47": {"usage": "uplink"}}}
    sw_b = {
        "mac": "bb0000000002", "id": "dev-b", "type": "switch", "model": "EX4100-48P",
        "name": "sw-b", "port_config": {"ge-0/0/47": {"usage": "uplink"}},
    }
    lldp = (  # two-sided LLDP -> link dev-a:ge-0/0/47 <-> dev-b:ge-0/0/47 (HIGH)
        {"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True,
         "neighbor_mac": "bb0000000002", "neighbor_port_desc": "ge-0/0/47"},
        {"mac": "bb0000000002", "port_id": "ge-0/0/47", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_desc": "ge-0/0/47"},
    )
    raw = dc_replace(_raw(), devices=(sw_a, sw_b), port_stats=lldp)
    payload = {"port_config": {"ge-0/0/47": {"usage": "uplink", "speed": "1g",
                                             "duplex": "full", "disable_autoneg": True}}}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
        provider=FakeProvider(raw=raw),
    )
    assert v.decision is not Decision.UNKNOWN, v.decision_reasons
    codes = {f.code for f in v.findings}
    assert "wired.l1.link_param_mismatch.autoneg_mismatch" in codes, codes
    assert v.decision in (Decision.REVIEW, Decision.UNSAFE), v.decision
