"""The review ledger must not spread cosmetic exceptions across contexts."""

import json
from pathlib import Path

from tools.review_mist_attributes import build_review, disposition

from digital_twin.scope.allowlist import COSMETIC_RAW_ALLOWLIST, RAW_ALLOWLIST
from digital_twin.scope.paths import allowed_tokens, leaf_changes


def _field(tokens, variants=()):
    return {"tokens": list(tokens), "variants": list(variants), "kind": "value"}


def test_identical_display_paths_keep_distinct_authorization_tokens():
    deltas = leaf_changes({}, {"a.b": 1, "a": {"b": 2}})
    assert [d.path for d in deltas] == ["a.b", "a.b"]
    assert {d.tokens for d in deltas} == {("a.b",), ("a", "b")}
    assert allowed_tokens(("a", "b"), ("a.b",))
    assert not allowed_tokens(("a.b",), ("a.b",))
    assert allowed_tokens(("networks", "corp.example", "vlan_id"), ("networks.*.vlan_id",))


def test_bgp_double_star_is_one_original_ip_key_in_authorization():
    patterns = ("bgp_config.*.neighbors.**.neighbor_as",)
    assert allowed_tokens(("bgp_config", "wan", "neighbors", "10.0.0.2", "neighbor_as"), patterns)
    assert not allowed_tokens(
        ("bgp_config", "wan", "neighbors", "10.0.0.2", "nested", "neighbor_as"), patterns,
    )


def test_names_labels_and_open_children_do_not_inherit_cosmetic_permissions():
    assert disposition(_field(("name",)), "device")[0] == "admitted_requires_checks"
    assert disposition(_field(("name",)), "gatewaytemplate")[0] == "cosmetic_direct_update"
    assert disposition(_field(("port_config", "{key}", "name")), "gatewaytemplate")[0] \
        == "denied_without_semantic_model"
    assert disposition(_field(("image1_url",)), "device")[0] == "cosmetic_direct_update"
    assert disposition(_field(("type",)), "gatewaytemplate")[0] == "denied_without_semantic_model"
    assert disposition(_field(("name",)), None)[0] == "unsupported_object_or_action"
    row = {**_field(("vars", "{unknown}")), "kind": "open_object_boundary"}
    assert disposition(row, "site_setting")[0] == "unvalidated_boundary"


def test_atomic_rule_children_are_not_independently_certified():
    row = _field(("port_usages", "{key}", "rules", "[]", "future_filter"))
    assert disposition(row, "site_setting")[0] == "atomic_parent_requires_validation"


def test_cosmetic_review_registry_is_admitted_and_never_licenses_a_whole_tree():
    for object_type, paths in COSMETIC_RAW_ALLOWLIST.items():
        assert set(paths).issubset(RAW_ALLOWLIST[object_type])
        for path in paths:
            assert not path.endswith(".*")
            tokens = tuple("probe" if p == "*" else p for p in path.split("."))
            assert not allowed_tokens((*tokens, "future_child"), paths)


def test_device_platform_branches_and_nonupdate_actions_remain_separate():
    fields = [{**_field(("image2_url",), (f"oneOf[{i}]",)), "media_type": "application/json"}
              for i in range(3)]
    inventory = {
        "spec_version": "synthetic", "spec_sha256": "synthetic", "component_scope": [],
        "fields": [], "mutation_operations": [
            {"method": method, "operation_id": "updateSiteDevice", "path": "/devices/id",
             "request_body_status": "documented", "body_fields": fields}
            for method in ("PUT", "POST")
        ],
    }
    review = build_review(inventory, inventory_digest="synthetic")
    assert [f["disposition"] for f in review["mutation_operations"][0]["input_fields"]] == [
        "unsupported_object_or_action", "cosmetic_direct_update", "unsupported_object_or_action",
    ]
    assert all(f["disposition"] == "unsupported_object_or_action"
               for f in review["mutation_operations"][1]["input_fields"])


def test_committed_review_has_a_disposition_for_every_inventory_occurrence():
    inventory = json.loads(Path("docs/evidence/mist-oas-coverage-audit.json").read_text())
    report = json.loads(Path("docs/evidence/mist-attribute-review.json").read_text())
    assert len(report["fields"]) == len(inventory["fields"]) == 7240
    assert len(report["mutation_operations"]) == len(inventory["mutation_operations"]) == 473
    for i, field in enumerate(report["fields"]):
        assert field["source_field_index"] == i
        assert field["rationale"] in report["rationales"]
    for op, original in zip(
        report["mutation_operations"], inventory["mutation_operations"], strict=True
    ):
        assert (op["method"], op["path"]) == (original["method"], original["path"])
        assert len(op["input_fields"]) == len(original["body_fields"])
        for i, field in enumerate(op["input_fields"]):
            assert field["source_field_index"] == i
            assert field["rationale"] in report["rationales"]
