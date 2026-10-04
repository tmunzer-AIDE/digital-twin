"""Public simulations for dependency checks: final state, partial evidence and scope."""

from copy import deepcopy
from dataclasses import replace

import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.checks.base import CoverageState, Status
from digital_twin.engine.pipeline import simulate
from digital_twin.ir import IRBuilder, IRValidationError, StaticRoute, device_id, diff_ir
from digital_twin.providers.base import FetchFailure
from digital_twin.verdict.decision import Decision
from tests.adapters.mist.fixtures import SITE_EFFECTIVE, SWITCH_A, raw_site
from tests.factories import sw

DID = device_id(SWITCH_A["mac"])
RADIUS = "wired.auth.radius_missing"
ROUTE = "wired.l3.static_route_reachability"
CONTROL = "wired.l3.control_plane_reachability"
STORM = "wired.port.storm_control_policy"


class FakeProvider:
    def __init__(self, raw):
        self.raw = raw

    def fetch_site(self, scope, *, include_derived=False):
        return self.raw


def _raw(*, setting=None, devices=None):
    return replace(
        raw_site(devices=devices or (deepcopy(SWITCH_A),)),
        setting=deepcopy(SITE_EFFECTIVE) if setting is None else setting,
    )


def _op(payload, *, kind="site_setting", oid="s1"):
    return {"action": "update", "object_type": kind, "object_id": oid, "payload": payload}


def _run(raw, *ops):
    verdict = simulate(
        {
            "source": "mist",
            "scope": {"org_id": "o1", "site_id": "s1"},
            "ops": [{**op, "order": i} for i, op in enumerate(ops)],
        },
        provider=FakeProvider(raw),
    )
    assert all(r.status is not Status.CHECK_ERROR for r in verdict.check_results)
    return verdict


def _result(verdict, cid):
    return next(r for r in verdict.check_results if r.check_id == cid)


def _auth_setting(**extra):
    setting = deepcopy(SITE_EFFECTIVE)
    setting["port_usages"]["office"]["port_auth"] = "dot1x"
    setting.update(extra)
    return setting


def _server(host="192.0.2.10", secret="synthetic-old-secret"):
    return {"host": host, "secret": secret}


def test_enabling_auth_without_backend_is_review_with_exact_ports_and_causes():
    v = _run(_raw(), _op({"port_usages": _auth_setting()["port_usages"]}))
    assert v.decision is Decision.REVIEW
    findings = _result(v, RADIUS).findings
    missing = [f for f in findings if f.code.endswith(".introduced")]
    assert {f.evidence["port"] for f in missing} == {f"{DID}:ge-0/0/{n}" for n in (0, 1)}
    assert all(f.caused_by and any(c.ref.kind == "port" for c in f.caused_by) for f in missing)


@pytest.mark.parametrize(
    "backend",
    [
        {"radius_config": {"auth_servers": [_server()]}},
        {"mist_nac": {"enabled": True}},
    ],
)
def test_backend_added_with_auth_avoids_false_missing_backend(backend):
    v = _run(
        _raw(),
        _op({"port_usages": _auth_setting()["port_usages"]}),
        _op(backend, kind="device", oid="dev-a"),
    )
    assert v.decision is Decision.REVIEW  # admission still requires verification
    assert not any(f.code.endswith(".introduced") for f in _result(v, RADIUS).findings)


def test_removing_last_backend_is_review_not_predicted_auth_failure():
    setting = _auth_setting(radius_config={"auth_servers": [_server()]})
    v = _run(_raw(setting=setting), _op({"radius_config": {"auth_servers": []}}))
    assert v.decision is Decision.REVIEW
    assert any(f.code.endswith(".introduced") for f in _result(v, RADIUS).findings)
    assert _result(v, RADIUS).coverage.state is CoverageState.PARTIAL


@pytest.mark.parametrize(
    "new_server", [_server("192.0.2.11"), _server(secret="synthetic-new-secret")]
)
def test_same_backend_count_still_detects_server_or_credential_edit_without_secret_leak(new_server):
    raw = _raw(setting=_auth_setting(radius_config={"auth_servers": [_server()]}))
    v = _run(raw, _op({"radius_config": {"auth_servers": [new_server]}}))
    assert v.decision is Decision.REVIEW
    assert any(f.code.endswith(".backend_changed") for f in _result(v, RADIUS).findings)
    assert "synthetic-old-secret" not in repr(v)
    assert "synthetic-new-secret" not in repr(v)


def test_unresolved_backend_is_not_asserted_absent():
    v = _run(_raw(), _op(_auth_setting(radius_config={"auth_servers": [_server("{{server}}")]})))
    assert v.decision is Decision.REVIEW
    assert not any(f.code.endswith(".introduced") for f in _result(v, RADIUS).findings)
    assert any("unresolved" in n for n in _result(v, RADIUS).coverage.notes)


def test_missing_inherited_evidence_is_not_asserted_absent():
    raw = _raw()
    raw = replace(
        raw,
        meta=replace(
            raw.meta,
            fetched=tuple(x for x in raw.meta.fetched if x != "setting"),
            failures=(FetchFailure(object="setting", error="unavailable"),),
        ),
    )
    v = _run(raw, _op({"port_usages": _auth_setting()["port_usages"]}))
    assert v.decision is Decision.REVIEW
    assert not any(f.code.endswith(".introduced") for f in _result(v, RADIUS).findings)
    assert _result(v, RADIUS).coverage.state is CoverageState.PARTIAL


def test_preexisting_missing_backend_does_not_floor_unrelated_mac_limit_change():
    setting = _auth_setting()
    usages = deepcopy(setting["port_usages"])
    usages["office"]["mac_limit"] = 10
    v = _run(_raw(setting=setting), _op({"port_usages": usages}))
    assert v.decision is Decision.SAFE
    findings = _result(v, RADIUS).findings
    assert findings and all(f.code.endswith(".preexisting") and not f.caused_by for f in findings)


def test_disabled_auth_ports_do_not_require_backend():
    dev = {
        **deepcopy(SWITCH_A),
        "port_config_overwrite": {"ge-0/0/0-1": {"disabled": True}},
    }
    out = MistAdapter().ingest(_raw(setting=_auth_setting(), devices=(dev,)))
    assert out.ir is not None
    assert out.ir.ports[f"{DID}:ge-0/0/0"].auth is not None
    assert out.ir.ports[f"{DID}:ge-0/0/0"].disabled
    from digital_twin.analysis.context import AnalysisContext
    from digital_twin.checks.base import CheckContext
    from digital_twin.checks.wired.radius_missing import RadiusMissingCheck

    r = RadiusMissingCheck().run(
        CheckContext(AnalysisContext(out.ir), AnalysisContext(out.ir), diff_ir(out.ir, out.ir))
    )
    assert not r.findings


def test_duplicate_auth_edits_are_explicitly_unsupported():
    v = _run(
        _raw(),
        _op({"port_usages": _auth_setting()["port_usages"]}),
        _op({"port_usages": SITE_EFFECTIVE["port_usages"]}),
    )
    assert v.decision is Decision.UNKNOWN
    assert any("same object" in reason for reason in v.decision_reasons)


def test_device_radius_overrides_inherited_backend():
    raw = _raw(
        setting=_auth_setting(radius_config={"auth_servers": [_server()]}),
        devices=({**deepcopy(SWITCH_A), "radius_config": {"auth_servers": []}},),
    )
    out = MistAdapter().ingest(raw)
    assert out.ir is not None and out.ir.devices[DID].authenticator_count == 0


def test_null_server_members_omitted_by_full_payload_do_not_create_backend_change():
    dev = {
        **deepcopy(SWITCH_A),
        "radius_config": {"auth_servers": [{**_server(), "port": None, "keywrap_enabled": None}]},
    }
    raw = _raw(setting=_auth_setting(), devices=(dev,))
    payload = {**deepcopy(dev), "radius_config": {"auth_servers": [_server()]}}
    payload["port_config_overwrite"] = {"ge-0/0/0": {"mac_limit": 10}}
    v = _run(raw, _op(payload, kind="device", oid="dev-a"))
    assert v.decision is Decision.SAFE
    assert not _result(v, RADIUS).findings


@pytest.mark.parametrize("mask", ["255.255.255.128", "{{mask}}"])
def test_netmask_edit_rechecks_unchanged_route(mask):
    raw = _raw(
        setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": {"0.0.0.0/0": {"via": "10.0.10.254"}}}
    )
    v = _run(
        raw,
        _op(
            {
                "other_ip_configs": {
                    "corp": {
                        "type": "static",
                        "ip": "10.0.10.1",
                        "netmask": mask,
                    }
                }
            },
            kind="device",
            oid="dev-a",
        ),
    )
    assert v.decision is Decision.REVIEW
    assert any(f.code.endswith(".recursive_resolution") for f in _result(v, ROUTE).findings)
    assert any(
        c.ref.kind == "l3intf" and "netmask" in c.fields
        for f in _result(v, ROUTE).findings
        for c in f.caused_by
    )


def test_route_remove_at_site_and_restore_on_device_preserves_final_route():
    routes = {"0.0.0.0/0": {"via": "10.0.10.254"}}
    raw = _raw(setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": routes})
    v = _run(
        raw, _op({"extra_routes": {}}), _op({"extra_routes": routes}, kind="device", oid="dev-a")
    )
    assert v.decision is Decision.REVIEW  # explain the masked site edit
    assert not _result(v, ROUTE).findings and not _result(v, CONTROL).findings
    assert any(f.code.startswith("scope.effective_noop") for f in v.findings)


def test_auth_site_change_masked_by_device_is_no_missing_backend():
    raw = _raw()
    v = _run(
        raw,
        _op({"port_usages": _auth_setting()["port_usages"]}),
        _op({"port_usages": SITE_EFFECTIVE["port_usages"]}, kind="device", oid="dev-a"),
    )
    assert v.decision is Decision.REVIEW
    assert not _result(v, RADIUS).findings
    assert any(f.code.startswith("scope.effective_noop") for f in v.findings)


def test_storm_site_change_masked_by_device_has_no_threshold_warning():
    v = _run(
        _raw(),
        _storm_op({"percentage": 10}),
        _op({"port_usages": SITE_EFFECTIVE["port_usages"]}, kind="device", oid="dev-a"),
    )
    assert v.decision is Decision.REVIEW
    assert not _result(v, STORM).findings
    assert any(f.code.startswith("scope.effective_noop") for f in v.findings)


@pytest.mark.parametrize(
    ("root", "prefix", "via"),
    [
        ("extra_routes", "0.0.0.0/0", "10.0.10.254"),
        ("extra_routes6", "::/0", "2001:db8::1"),
    ],
)
def test_removing_configured_default_warns_per_address_family(root, prefix, via):
    setting = {**deepcopy(SITE_EFFECTIVE), root: {prefix: {"via": via}}}
    v = _run(_raw(setting=setting), _op({root: {}}))
    assert v.decision is Decision.REVIEW
    f = next(f for f in _result(v, CONTROL).findings if f.code.endswith(".default_route_lost"))
    assert f.evidence["lost_configured_defaults"] == [prefix]
    assert f.affected_entities == (DID,)
    assert any(c.ref.kind == "static_route" for c in f.caused_by)


def test_unrelated_device_default_is_not_an_alternate_path():
    a = {**deepcopy(SWITCH_A), "extra_routes": {"0.0.0.0/0": {"via": "10.0.10.254"}}}
    b = {**deepcopy(a), "id": "dev-b", "mac": "bb0000000001"}
    v = _run(_raw(devices=(a, b)), _op({"extra_routes": {}}, kind="device", oid="dev-a"))
    assert v.decision is Decision.REVIEW
    assert [f.affected_entities for f in _result(v, CONTROL).findings] == [(DID,)]
    assert _result(v, CONTROL).findings[0].code.endswith(".default_route_lost")


@pytest.mark.parametrize(
    ("entry", "suffix"),
    [
        ({"via": "10.0.10.254"}, "forwarding_unverified"),
        ({"via": "192.0.2.1"}, "recursive_resolution"),
        ({"via": "{{next_hop}}"}, "unresolved"),
        ({"discard": True}, "discard"),
        ({"via": "2001:db8::1"}, "unresolved"),
    ],
)
def test_route_intent_never_proves_live_forwarding(entry, suffix):
    v = _run(_raw(), _op({"extra_routes": {"198.51.100.0/24": entry}}))
    assert v.decision is Decision.REVIEW
    assert _result(v, ROUTE).coverage.state is CoverageState.PARTIAL
    assert any(f.code.endswith("." + suffix) for f in _result(v, ROUTE).findings)


def test_connected_interface_change_rechecks_unchanged_route():
    raw = _raw(
        setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": {"0.0.0.0/0": {"via": "10.0.10.254"}}}
    )
    v = _run(
        raw,
        _op(
            {
                "other_ip_configs": {
                    "corp": {
                        "type": "static",
                        "ip": "10.0.20.1",
                        "netmask": "255.255.255.0",
                    }
                }
            },
            kind="device",
            oid="dev-a",
        ),
    )
    assert v.decision is Decision.REVIEW
    assert any(f.code.endswith(".recursive_resolution") for f in _result(v, ROUTE).findings)
    assert any(c.ref.kind == "l3intf" for f in _result(v, ROUTE).findings for c in f.caused_by)


def test_preexisting_unresolved_route_does_not_floor_independent_port_change():
    raw = _raw(
        setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": {"0.0.0.0/0": {"via": "{{next_hop}}"}}}
    )
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["office"]["mac_limit"] = 10
    v = _run(raw, _op({"port_usages": usages}))
    assert v.decision is Decision.SAFE
    assert not _result(v, ROUTE).findings


def test_duplicate_route_edits_are_explicitly_unsupported():
    routes = {"0.0.0.0/0": {"via": "10.0.10.254"}}
    raw = _raw(setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": routes})
    v = _run(raw, _op({"extra_routes": {}}), _op({"extra_routes": routes}))
    assert v.decision is Decision.UNKNOWN
    assert any("same object" in reason for reason in v.decision_reasons)


@pytest.mark.parametrize(
    "payload",
    [
        {"extra_routes": {"198.51.100.0/24": {"via": "10.0.10.254", "metric": 1}}},
        {
            "extra_routes": {
                "198.51.100.0/24": {"next_qualified": {"10.0.10.254": {"preference": 20}}}
            }
        },
    ],
)
def test_unmodeled_route_attributes_remain_unknown(payload):
    assert _run(_raw(), _op(payload, kind="device", oid="dev-a")).decision is Decision.UNKNOWN


def test_device_vrf_with_routes_is_modeled_and_requires_review():
    payload = {
        "vrf_instances": {
            "guest": {
                "networks": ["corp"],
                "extra_routes": {"0.0.0.0/0": {"via": "10.0.10.254"}},
            }
        }
    }
    v = _run(_raw(), _op(payload, kind="device", oid="dev-a"))
    assert v.decision is Decision.REVIEW
    assert any(f.code.startswith("wired.l3.control_plane_reachability") for f in v.findings)


def test_static_route_ids_are_device_scoped_validated_and_diffed():
    route = StaticRoute("S", "0.0.0.0/0", ("192.0.2.1",))
    builder = IRBuilder().add_device(sw("S")).add_static_route(route)
    with pytest.raises(IRValidationError, match="duplicate static route"):
        builder.add_static_route(route)
    base = IRBuilder().add_device(sw("S")).build()
    assert diff_ir(base, builder.build()).touches("static_route")
    with pytest.raises(IRValidationError, match="unknown device"):
        IRBuilder().add_static_route(route).build()


def _storm_op(value):
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["uplink"]["storm_control"] = value
    return _op({"port_usages": usages})


def test_lower_storm_threshold_requires_review_with_values():
    v = _run(_raw(), _storm_op({"percentage": 10}))
    assert v.decision is Decision.REVIEW
    f = next(f for f in _result(v, STORM).findings if f.code.endswith(".threshold_lowered"))
    assert f.evidence == {"percentage_before": 80, "percentage_after": 10}
    assert f.caused_by


def test_storm_shutdown_on_declared_interswitch_port_has_specific_warning():
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["uplink"].update(inter_switch_link=True, storm_control={"disable_port": True})
    v = _run(_raw(), _op({"port_usages": usages}))
    assert v.decision is Decision.REVIEW
    assert any(f.code.endswith(".uplink_shutdown") for f in _result(v, STORM).findings)


def test_preexisting_storm_shutdown_is_context_only_on_unrelated_port_edit():
    setting = deepcopy(SITE_EFFECTIVE)
    setting["port_usages"]["uplink"].update(
        inter_switch_link=True, storm_control={"disable_port": True}
    )
    usages = deepcopy(setting["port_usages"])
    usages["office"]["mac_limit"] = 10
    v = _run(_raw(setting=setting), _op({"port_usages": usages}))
    assert v.decision is Decision.SAFE
    assert all(
        f.code.endswith(".preexisting") and not f.caused_by for f in _result(v, STORM).findings
    )


def test_storm_default_object_is_noop():
    v = _run(_raw(), _storm_op({"disable_port": False, "percentage": 80}))
    assert v.decision is Decision.SAFE and not v.findings


def test_templated_storm_threshold_is_unresolved_not_defaulted():
    v = _run(_raw(), _storm_op({"percentage": "{{burst}}"}))
    assert v.decision is Decision.REVIEW
    f = next(f for f in _result(v, STORM).findings if f.code.endswith(".unresolved"))
    assert f.evidence["percentage_after"] is None


def test_unresolved_storm_object_does_not_invent_default_percentage():
    v = _run(_raw(), _storm_op("{{storm}}"))
    assert v.decision is Decision.REVIEW
    f = next(f for f in _result(v, STORM).findings if f.code.endswith(".unresolved"))
    assert f.evidence["percentage_after"] is None


def test_duplicate_storm_edits_are_explicitly_unsupported():
    v = _run(_raw(), _storm_op({"percentage": 10}), _storm_op({"percentage": 80}))
    assert v.decision is Decision.UNKNOWN
    assert any("same object" in reason for reason in v.decision_reasons)


def test_masked_site_edit_reports_devices_and_leaf_without_ir_change():
    dev = {
        **deepcopy(SWITCH_A),
        "port_usages": {"office": {**SITE_EFFECTIVE["port_usages"]["office"], "mac_limit": 20}},
    }
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["office"]["mac_limit"] = 10
    v = _run(_raw(devices=(dev,)), _op({"port_usages": usages}))
    assert v.decision is Decision.REVIEW and v.ir_diff.is_empty()
    f = next(f for f in v.findings if f.code == "scope.effective_noop.fully_overridden")
    assert f.evidence["overridden_devices_by_path"] == {"port_usages.office.mac_limit": [DID]}


def test_partially_masked_site_edit_keeps_both_device_groups():
    dev = {
        **deepcopy(SWITCH_A),
        "port_usages": {"office": {**SITE_EFFECTIVE["port_usages"]["office"], "mac_limit": 20}},
    }
    other = {**deepcopy(SWITCH_A), "mac": "bb0000000001", "id": "dev-b"}
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["office"]["mac_limit"] = 10
    v = _run(_raw(devices=(dev, other)), _op({"port_usages": usages}))
    f = next(f for f in v.findings if f.code == "scope.effective_noop.partially_overridden")
    assert f.evidence["overridden_devices_by_path"] == {"port_usages.office.mac_limit": [DID]}
    assert f.evidence["applied_devices_by_path"] == {
        "port_usages.office.mac_limit": [device_id(other["mac"])]
    }


def test_masked_sibling_does_not_mask_effective_changed_leaf():
    dev = {
        **deepcopy(SWITCH_A),
        "port_usages": {"office": {**SITE_EFFECTIVE["port_usages"]["office"], "mac_limit": 20}},
    }
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["uplink"]["mac_limit"] = 10
    v = _run(_raw(devices=(dev,)), _op({"port_usages": usages}))
    assert v.decision is Decision.SAFE
    assert not any(f.code.startswith("scope.effective_noop") for f in v.findings)


def test_site_edit_and_device_override_removal_apply_final_intent():
    dev = {
        **deepcopy(SWITCH_A),
        "port_usages": {"office": {**SITE_EFFECTIVE["port_usages"]["office"], "mac_limit": 20}},
    }
    usages = deepcopy(SITE_EFFECTIVE["port_usages"])
    usages["office"]["mac_limit"] = 10
    v = _run(
        _raw(devices=(dev,)),
        _op({"port_usages": usages}),
        _op({"-port_usages": True}, kind="device", oid="dev-a"),
    )
    assert not any(f.code.startswith("scope.effective_noop") for f in v.findings)


def test_device_profile_gate_still_blocks_confident_override_conclusions():
    dev = {**deepcopy(SWITCH_A), "deviceprofile_id": "profile-a"}
    v = _run(_raw(devices=(dev,)), _op({"radius_config": {"auth_servers": [_server()]}}))
    assert v.decision is Decision.UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        {"radius_config": {"auth_servers": [_server()]}},
        {"mist_nac": {"enabled": True}},
    ],
)
def test_switch_dependency_fields_cannot_silently_certify_gateway_operation(payload):
    gw = {"id": "gw-a", "mac": "dd0000000001", "type": "gateway"}
    v = _run(_raw(devices=(deepcopy(SWITCH_A), gw)), _op(payload))
    assert v.decision is Decision.UNKNOWN


def test_gateway_static_routes_are_modeled_and_require_review():
    gw = {"id": "gw-a", "mac": "dd0000000001", "type": "gateway"}
    v = _run(
        _raw(devices=(deepcopy(SWITCH_A), gw)),
        _op({"extra_routes": {"0.0.0.0/0": {"via": "192.0.2.1"}}}),
    )
    assert v.decision is Decision.REVIEW
    assert any(
        f.code.startswith("wired.l3.static_route_reachability")
        and f.subject is not None
        and f.subject.id == "dd0000000001"
        for f in v.findings
    )


def test_forwarding_port_edit_warns_about_unchanged_route_control_dependencies():
    raw = _raw(
        setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": {"0.0.0.0/0": {"via": "10.0.10.254"}}}
    )
    v = _run(
        raw,
        _op(
            {
                "port_config_overwrite": {
                    "ge-0/0/0": {
                        "disabled": True,
                    }
                }
            },
            kind="device",
            oid="dev-a",
        ),
    )
    assert v.decision is Decision.REVIEW
    f = _result(v, CONTROL).findings[0]
    assert f.code.endswith(".path_dependency_changed")
    assert f.evidence["ports"] == [f"{DID}:ge-0/0/0"]
    assert any(c.ref.kind == "port" and "disabled" in c.fields for c in f.caused_by)


def test_unknown_storm_child_remains_out_of_scope():
    assert _run(_raw(), _storm_op({"future_knob": True})).decision is Decision.UNKNOWN


def test_dynamic_addressing_does_not_prove_connected_route_next_hop():
    raw = _raw(
        setting={**deepcopy(SITE_EFFECTIVE), "extra_routes": {"0.0.0.0/0": {"via": "10.0.10.254"}}}
    )
    v = _run(
        raw,
        _op(
            {
                "other_ip_configs": {
                    "corp": {
                        "type": "dhcp",
                        "ip": "10.0.10.1",
                        "netmask": "255.255.255.0",
                    }
                }
            },
            kind="device",
            oid="dev-a",
        ),
    )
    assert v.decision is Decision.REVIEW
    assert any(f.code.endswith(".recursive_resolution") for f in _result(v, ROUTE).findings)


def test_empty_device_inventory_does_not_invent_effective_override():
    raw = replace(_raw(), devices=())
    v = _run(raw, _op({"radius_config": {"auth_servers": [_server()]}}))
    assert not any(f.code.startswith("scope.effective_noop") for f in v.findings)
