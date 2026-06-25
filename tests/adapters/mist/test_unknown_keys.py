from digital_twin.adapters.mist.validate.unknown_keys import (
    OAS_UNKNOWN_KEY_SKIP,
    unknown_attribute_findings,
)
from digital_twin.contracts import FindingCategory, FindingSource, Severity

CLOSED = {"type": "object", "properties": {"a": {"type": "string"}}}  # addl absent -> closed
MAP = {"type": "object", "additionalProperties": {"type": "object",
       "properties": {"x": {"type": "integer"}}}}
OPEN = {"type": "object", "properties": {"a": {}}, "additionalProperties": True}
MIXED = {"type": "object", "properties": {"a": {"type": "string"}},
         "additionalProperties": {"type": "object", "properties": {"x": {}}}}


def _paths(findings):
    return {f.evidence["path"] for f in findings}


def test_closed_node_flags_unknown_key():
    out = unknown_attribute_findings(CLOSED, {"a": "ok", "b": 1},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"b"}
    f = out[0]
    assert f.code == "l0.schema.unknown_attribute"
    assert f.source is FindingSource.ADAPTER
    assert f.category is FindingCategory.OPERATIONAL
    assert f.severity is Severity.WARNING
    assert f.evidence["object_type"] == "device"


def test_documented_keys_pass():
    out = unknown_attribute_findings(CLOSED, {"a": "ok"}, object_type="device", scope_roots=None)
    assert out == ()


def test_null_value_treated_as_absent():
    out = unknown_attribute_findings(CLOSED, {"a": "ok", "b": None},
                                     object_type="device", scope_roots=None)
    assert out == ()


def test_map_node_dynamic_keys_pass_and_recurse():
    out = unknown_attribute_findings(
        MAP, {"any-name": {"x": 1, "bogus": 2}}, object_type="device", scope_roots=None)
    assert _paths(out) == {"any-name.bogus"}


def test_open_node_true_allows_extra():
    out = unknown_attribute_findings(OPEN, {"a": 1, "whatever": 2},
                                     object_type="device", scope_roots=None)
    assert out == ()


def test_undocumented_object_node_not_flagged():
    # no properties AND no additionalProperties -> nothing to compare against
    out = unknown_attribute_findings({"type": "object"}, {"anything": 1, "x": 2},
                                     object_type="device", scope_roots=None)
    assert out == ()


def test_explicit_closed_empty_object_flags_keys():
    # additionalProperties: false with no properties -> NO keys allowed
    out = unknown_attribute_findings({"type": "object", "additionalProperties": False},
                                     {"x": 1}, object_type="device", scope_roots=None)
    assert _paths(out) == {"x"}


def test_mixed_node_props_and_additional():
    # 'a' matches properties; any other key is allowed by the map schema (recurses
    # into it); an undocumented key under such a value is flagged
    out = unknown_attribute_findings(
        MIXED, {"a": "ok", "extra": {"x": 1, "nope": 2}},
        object_type="device", scope_roots=None)
    assert _paths(out) == {"extra.nope"}


def test_composition_anyof_union_accepts_second_branch():
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {}}},
        {"type": "object", "properties": {"b": {}}},
    ]}
    out = unknown_attribute_findings(schema, {"b": 1}, object_type="device", scope_roots=None)
    assert out == ()


def test_composition_anyof_map_branch_allows_dynamic_keys():
    # a non-first anyOf branch with schema-valued additionalProperties -> node is MAP,
    # so dynamic keys it allows are NOT flagged, and a leaf inside the value IS checked
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {}}},
        {"type": "object",
         "additionalProperties": {"type": "object", "properties": {"x": {}}}},
    ]}
    assert unknown_attribute_findings(schema, {"dyn": {"x": 1}},
                                      object_type="device", scope_roots=None) == ()
    out = unknown_attribute_findings(schema, {"dyn": {"x": 1, "bad": 2}},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"dyn.bad"}


def test_composition_same_property_across_branches_unions_subschemas():
    # 'p' is documented in BOTH branches with different nested props -> recursion must
    # see the UNION, so a nested key from either branch is accepted (not overwritten).
    schema = {"anyOf": [
        {"type": "object", "properties": {"p": {"type": "object",
                                                "properties": {"a": {}}}}},
        {"type": "object", "properties": {"p": {"type": "object",
                                                "properties": {"b": {}}}}},
    ]}
    assert unknown_attribute_findings(schema, {"p": {"a": 1, "b": 2}},
                                      object_type="device", scope_roots=None) == ()
    out = unknown_attribute_findings(schema, {"p": {"a": 1, "c": 3}},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"p.c"}


def test_composition_two_map_branches_union_value_schemas():
    # two anyOf MAP branches with different value schemas -> a dynamic value's keys
    # from EITHER map are accepted (tied map schemas are combined, not dropped).
    schema = {"anyOf": [
        {"type": "object", "additionalProperties": {"type": "object",
                                                    "properties": {"a": {}}}},
        {"type": "object", "additionalProperties": {"type": "object",
                                                    "properties": {"b": {}}}},
    ]}
    assert unknown_attribute_findings(schema, {"k": {"a": 1, "b": 2}},
                                      object_type="device", scope_roots=None) == ()
    out = unknown_attribute_findings(schema, {"k": {"a": 1, "c": 3}},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"k.c"}


def test_composition_allof_merges_properties():
    schema = {"allOf": [
        {"type": "object", "properties": {"a": {}}},
        {"type": "object", "properties": {"b": {}}},
    ]}
    out = unknown_attribute_findings(schema, {"a": 1, "b": 2, "c": 3},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"c"}


def test_array_items_recursion():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {"k": {}}}}}}
    out = unknown_attribute_findings(
        schema, {"items": [{"k": 1}, {"k": 2, "bad": 3}]},
        object_type="device", scope_roots=None)
    assert _paths(out) == {"items.1.bad"}


def test_secret_path_suppressed():
    out = unknown_attribute_findings(
        CLOSED, {"a": "ok", "shared_secret": "zzz"}, object_type="device", scope_roots=None)
    assert out == ()  # 'secret' is a STRIP_KEY_PARTS token


def test_skip_listed_object_type_returns_empty():
    assert "wlan" in OAS_UNKNOWN_KEY_SKIP
    out = unknown_attribute_findings(CLOSED, {"b": 1}, object_type="wlan", scope_roots=None)
    assert out == ()


def test_cap_limits_findings():
    payload = {f"k{i}": 1 for i in range(120)}
    out = unknown_attribute_findings(CLOSED, payload, object_type="device", scope_roots=None)
    assert len(out) == 50


def test_scope_roots_limits_to_changed_roots():
    schema = {"type": "object", "properties": {
        "port_config": {"type": "object", "additionalProperties":
                        {"type": "object", "properties": {"usage": {}}}},
        "other": {"type": "object", "properties": {"ok": {}}}}}
    payload = {"port_config": {"ge-0/0/1": {"usage": "x", "disabled": True}},
               "other": {"ok": 1, "weird": 2}}
    scoped = unknown_attribute_findings(schema, payload, object_type="device",
                                        scope_roots={"port_config"})
    assert _paths(scoped) == {"port_config.ge-0/0/1.disabled"}  # 'other.weird' not in scope
    full = unknown_attribute_findings(schema, payload, object_type="device", scope_roots=None)
    assert _paths(full) == {"port_config.ge-0/0/1.disabled", "other.weird"}


def test_device_server_managed_roots_skipped_top_level_only():
    # device server-managed / GET-only roots (e.g. x_m/tag_id, device-scoped) are
    # skipped at the TOP LEVEL, but a same-named key NESTED under a documented map
    # still surfaces — the skip is root-level only, never segment-wide.
    schema = {"type": "object", "properties": {
        "networks": {"type": "object", "additionalProperties":
                     {"type": "object", "properties": {"vlan_id": {}}}}}}
    payload = {"x_m": 12.5, "tag_id": "t", "bogus": 1,
               "networks": {"corp": {"vlan_id": 10, "x_m": 9}}}
    out = unknown_attribute_findings(schema, payload, object_type="device", scope_roots=None)
    # x_m/tag_id skipped at root; bogus (root, not server-managed) flagged;
    # networks.corp.x_m (nested) flagged — the skip is root-level only
    assert _paths(out) == {"bogus", "networks.corp.x_m"}


def test_device_get_only_roots_not_flagged_against_real_schema():
    # full-object device payload carrying the real GET-only roots the closed PUT
    # schema omits -> ZERO unknown-attribute findings (device-scoped skip).
    from digital_twin.adapters.mist.oas import load_schema
    from digital_twin.adapters.mist.validate.unknown_keys import _DEVICE_GET_ONLY_ROOTS
    payload: dict = {"type": "switch", "port_config": {"ge-0/0/0": {"usage": "office"}}}
    payload.update({r: {} for r in _DEVICE_GET_ONLY_ROOTS})
    out = unknown_attribute_findings(load_schema("device_switch.schema.json"), payload,
                                     object_type="device", scope_roots=None)
    assert out == ()
