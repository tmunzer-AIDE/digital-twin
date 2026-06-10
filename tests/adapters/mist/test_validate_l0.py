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


def test_non_object_payload_is_fatal():
    res = validate_payload("site_setting", "just-a-string")  # type: ignore[arg-type]
    assert res.fatal is True and len(res.findings) == 1


def test_unknown_object_type_is_fatal():
    res = validate_payload("wlan", {})
    assert res.fatal is True
    assert "wlan" in res.findings[0].message
