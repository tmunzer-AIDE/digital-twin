from digital_twin.adapters.mist.validate import L0Result, validate_payload
from digital_twin.contracts import FindingCategory, FindingSource, Severity


def test_clean_site_setting_payload_yields_no_findings():
    res = validate_payload(
        "site_setting",
        {
            "networks": {"corp": {"vlan_id": 10}},
            "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
        },
    )
    assert isinstance(res, L0Result)
    assert res.findings == () and res.fatal is False


def test_type_violation_yields_error_finding_with_path():
    res = validate_payload("site_setting", {"networks": "not-an-object"})
    assert res.fatal is False
    assert len(res.findings) >= 1
    f = res.findings[0]
    assert f.severity is Severity.ERROR
    assert f.source is FindingSource.ADAPTER
    assert f.category is FindingCategory.OPERATIONAL  # payload trouble, not net breakage
    assert "networks" in str(f.evidence.get("path"))


def test_enum_violation_detected_on_device_payload():
    # duplex is enum-constrained (auto|full|half) in the committed schema; the
    # partial payload also triggers the schema's required 'type' — hence any()
    res = validate_payload(
        "device", {"port_config": {"ge-0/0/0": {"usage": "office", "duplex": "warp-speed"}}}
    )
    assert any("duplex" in str(f.evidence.get("path")) for f in res.findings)


def test_nullable_string_field_accepts_explicit_null():
    # OAS `nullable: true` is not a JSON-Schema keyword; without normalization
    # `type: string` would falsely reject a valid explicit null
    res = validate_payload(
        "site_setting", {"port_usages": {"office": {"mode": "access", "guest_network": None}}}
    )
    assert res.findings == ()


def test_nullable_enum_field_accepts_explicit_null():
    # port_auth is nullable AND enum-constrained — null must join the enum too
    res = validate_payload("site_setting", {"port_usages": {"office": {"port_auth": None}}})
    assert res.findings == ()


def test_null_values_validate_as_absent():
    # the established canon: Mist GETs return null for unset optional fields,
    # and null == absent everywhere (compiler equivalence, field gate). The
    # EFFECTIVE object L0 validates inherits such nulls from the current state.
    res = validate_payload(
        "device",
        {"type": "switch", "notes": None, "deviceprofile_id": None, "port_config": {}},
    )
    assert res.findings == ()


def test_secret_key_violations_are_suppressed():
    # replay fixtures strip secrets (None/absent by design) and the twin never
    # simulates them — schema noise about secret-manifest keys adds nothing
    # (Mist's own API still validates real payloads on actual apply)
    res = validate_payload(
        "device",
        {
            "type": "switch",
            "radius_config": {"auth_servers": [{"host": "h", "port": 1812}]},  # no secret
        },
    )
    assert not any("secret" in str(f.evidence.get("path", "")) + f.message for f in res.findings)


def test_scope_roots_suppresses_violations_on_untouched_roots():
    # The EFFECTIVE object carries PERSISTED roots Mist already accepted. A real L0
    # type violation on a root the op never touched (here extra_routes.*.via is an
    # int, but the OAS types it as an array of next-hops) must NOT surface — Mist
    # re-validates only the roots in the change (root-level-merge PUT).
    effective = {
        "type": "switch",
        "extra_routes": {"1.2.3.4/32": {"via": 123, "discard": False}},
        "port_config": {"ge-0/0/10": {"usage": "srv"}},
    }
    res = validate_payload("device", effective, scope_roots={"port_config"})
    assert res.findings == ()


def test_scope_roots_keeps_violations_on_changed_roots():
    effective = {
        "type": "switch",
        "extra_routes": {"1.2.3.4/32": {"via": 123}},
    }
    res = validate_payload("device", effective, scope_roots={"extra_routes"})
    assert any("extra_routes" in str(f.evidence.get("path")) for f in res.findings)


def test_scope_roots_keeps_object_level_violations():
    # an empty-path (root-level) violation — e.g. the schema-required 'type' is
    # missing — is NOT tied to one root and must survive any scoping
    res = validate_payload("device", {"port_config": {}}, scope_roots={"port_config"})
    assert any("required" in f.message for f in res.findings)


def test_scope_roots_none_validates_whole_object():
    # the opt-in "extend to the whole object" mode (the legacy behavior): every
    # violation surfaces, including on untouched persisted roots
    effective = {
        "type": "switch",
        "extra_routes": {"1.2.3.4/32": {"via": 123}},
        "port_config": {"ge-0/0/10": {"usage": "srv"}},
    }
    res = validate_payload("device", effective)  # default: no scoping
    assert any("extra_routes" in str(f.evidence.get("path")) for f in res.findings)


def test_non_object_payload_is_fatal():
    res = validate_payload("site_setting", "just-a-string")  # type: ignore[arg-type]
    assert res.fatal is True and len(res.findings) == 1


def test_wlan_schema_validates_not_fatal():
    ok = validate_payload("wlan", {"isolation": True})       # modeled leaf
    assert ok.fatal is False and ok.findings == ()
    bad = validate_payload("wlan", {"enabled": "yes"})       # wrong type
    assert bad.fatal is False and any("enabled" in f.evidence.get("path", "") or
                                      "enabled" in f.message for f in bad.findings)


def test_networktemplate_l0_schema_registered():
    from digital_twin.adapters.mist.validate import validate_payload
    res = validate_payload("networktemplate", {"id": "nt1", "ospf_config": {"enabled": True}})
    assert res.fatal is False  # a valid template body validates
    # device-only v1 skip-lists networktemplate, so the walker emits nothing here
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_gatewaytemplate_schema_registered_and_validates():
    # a structurally-valid gatewaytemplate payload yields no L0 findings
    res = validate_payload("gatewaytemplate", {"name": "gw1", "port_config": {}})
    assert isinstance(res, L0Result) and not res.fatal


def test_gatewaytemplate_type_violation_detected():
    # port_config must be an object -> a string is an L0 violation
    res = validate_payload("gatewaytemplate", {"port_config": "nope"})
    assert any(f.severity.value in ("error", "critical") for f in res.findings)


def test_sitetemplate_schema_registered_permissive():
    # the thin OAS sitetemplate schema is permissive (no additionalProperties:false)
    # -> the rich fields it omits do NOT false-reject; vars validates structurally
    res = validate_payload("sitetemplate", {"name": "st1", "vars": {"X": "1"},
                                            "networks": {"corp": {"vlan_id": 10}}})
    assert isinstance(res, L0Result) and not res.fatal


def test_unregistered_org_type_fails_closed():
    # an object_type with no committed schema must FAIL CLOSED (fatal -> UNKNOWN)
    res = validate_payload("rftemplate", {"x": 1})
    assert res.fatal


def test_nacrule_schema_registered_and_validates():
    res = validate_payload("nacrule", {"name": "r1", "action": "allow", "order": 1,
                                       "enabled": True, "matching": {}})
    assert isinstance(res, L0Result) and not res.fatal and res.findings == ()


def test_nacrule_type_violations_detected():
    # enabled must be bool, order int, action enum
    res = validate_payload("nacrule", {"name": "r1", "action": "nope",
                                       "enabled": "yes", "order": "x"})
    paths = " ".join(str(f.evidence.get("path")) + f.message for f in res.findings)
    assert not res.fatal
    assert "enabled" in paths and "order" in paths and "action" in paths


def test_nacrule_unmodeled_field_passes_l0_permissive():
    # OAS-valid-but-unmodeled fields are NOT rejected at L0 (the field gate owns them)
    res = validate_payload("nacrule", {"name": "r", "action": "allow",
                                       "guest_auth_state": "whatever"})
    assert not res.fatal and res.findings == ()


# --- unknown-attribute walker integration (device-only v1) ---

def test_unknown_attribute_flagged_on_device_port_config():
    res = validate_payload(
        "device",
        {"type": "switch", "port_config": {"ge-0/0/10": {"usage": "srv", "disabled": True}}},
    )
    hits = [f for f in res.findings if f.code == "l0.schema.unknown_attribute"]
    assert len(hits) == 1
    assert hits[0].severity is Severity.WARNING
    assert hits[0].evidence["path"] == "port_config.ge-0/0/10.disabled"


def test_unknown_attribute_skipped_for_thin_schema():
    res = validate_payload("wlan", {"isolation": True, "totally_made_up": 1})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_unknown_attribute_respects_unknown_scope_roots():
    # the walker is scoped by unknown_scope_roots (the changed-value roots), NOT by
    # the jsonschema scope_roots
    payload = {"type": "switch",
               "port_config": {"ge-0/0/10": {"usage": "srv", "disabled": True}}}
    flagged = validate_payload("device", payload, unknown_scope_roots={"port_config"})
    assert any(f.code == "l0.schema.unknown_attribute" for f in flagged.findings)
    ignored = validate_payload("device", payload, unknown_scope_roots={"name"})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in ignored.findings)


def test_unknown_scope_roots_omitted_reuses_scope_roots():
    # back-compat: OMITTING unknown_scope_roots reuses scope_roots, so a direct
    # scoped-L0 caller does NOT get whole-object unknown findings on untouched roots.
    payload = {"type": "switch",
               "port_config": {"ge-0/0/10": {"usage": "srv", "disabled": True}},
               "totally_unknown_root": {"x": 1}}
    scoped = validate_payload("device", payload, scope_roots={"port_config"})
    paths = {f.evidence["path"] for f in scoped.findings
             if f.code == "l0.schema.unknown_attribute"}
    assert paths == {"port_config.ge-0/0/10.disabled"}  # NOT totally_unknown_root
    # explicit scope_roots=None (unknown omitted -> reuse -> None) audits everything
    whole = validate_payload("device", payload)
    wpaths = {f.evidence["path"] for f in whole.findings
              if f.code == "l0.schema.unknown_attribute"}
    assert {"totally_unknown_root", "port_config.ge-0/0/10.disabled"} <= wpaths


def test_clean_device_payload_has_no_unknown_attribute_findings():
    res = validate_payload(
        "device", {"type": "switch", "port_config": {"ge-0/0/0": {"usage": "office"}}})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_refreshed_oas_recognizes_real_switch_fields():
    # post-refresh: bgp_config + port_config.*.ae_lacp_force_up are real switch
    # fields the OAS now documents -> NOT flagged as unknown attributes.
    res = validate_payload("device", {
        "type": "switch",
        "bgp_config": {"sess": {"local_as": 65000}},
        "port_config": {"ge-0/0/0": {"usage": "office", "ae_lacp_force_up": True}},
    })
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_refreshed_oas_recognizes_template_modeled_roots():
    # device-only v1: templates are skip-listed, so they never produce unknown-attr
    # findings (the walker short-circuits) — the skip-set governs scope.
    nt = validate_payload("networktemplate", {"ospf_config": {"enabled": True}})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in nt.findings)
    ss = validate_payload("site_setting", {"networks": {"corp": {"vlan_id": 10}},
                                           "dhcpd_config": {"corp": {"type": "local"}}})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in ss.findings)


def test_no_modeled_allowlist_leaf_is_flagged():
    # leaf-level coverage gate (map-aware, via the real walker): every modeled
    # (allowlisted) leaf at its real nesting must be documented -> ZERO unknown
    # findings per ENFORCED (non-skip) type.
    from digital_twin.adapters.mist.validate.unknown_keys import OAS_UNKNOWN_KEY_SKIP
    from digital_twin.scope.allowlist import RAW_ALLOWLIST

    def payload_from(patterns):
        d: dict = {}
        for pat in patterns:
            cur = d
            segs = pat.split(".")
            for i, s in enumerate(segs):
                key = "k" if s == "*" else "10.0.0.1" if s == "**" else s
                if i == len(segs) - 1:
                    cur[key] = 1
                else:
                    cur = cur.setdefault(key, {})
        return d

    for ot in ("device", "networktemplate", "site_setting", "gatewaytemplate"):
        if ot in OAS_UNKNOWN_KEY_SKIP:
            continue
        res = validate_payload(ot, payload_from(RAW_ALLOWLIST[ot]))
        bad = sorted(f.evidence["path"] for f in res.findings
                     if f.code == "l0.schema.unknown_attribute")
        assert not bad, f"{ot}: modeled leaves flagged as unknown: {bad}"
