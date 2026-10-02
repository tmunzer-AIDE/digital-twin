"""Prove the inventory retains schema branches and non-PUT mutation surfaces."""

from tools.audit_oas_coverage import build_report, legacy_disposition, schema_fields


def test_schema_inventory_keeps_union_map_array_and_open_boundaries():
    spec = {
        "components": {
            "schemas": {
                "Root": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "neighbors": {
                                    "type": "object",
                                    "additionalProperties": {"$ref": "#/components/schemas/Peer"},
                                }
                            },
                            "additionalProperties": False,
                        },
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"future_field": {"type": "boolean"}},
                            },
                        },
                    ]
                },
                "Peer": {
                    "type": "object",
                    "properties": {"auth_key": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        }
    }
    rows = list(schema_fields(spec, {"$ref": "#/components/schemas/Root"}))
    assert any(r["tokens"] == ["neighbors", "{key}", "auth_key"] for r in rows)
    assert any(r["tokens"] == ["[]", "future_field"] for r in rows)
    assert any(
        r["tokens"] == ["[]", "{unknown}"] and r["kind"] == "open_object_boundary" for r in rows
    )
    assert {r["variants"][0] for r in rows} == {"oneOf[0]", "oneOf[1]"}


def test_recursive_and_unresolved_schema_never_disappear():
    spec = {
        "components": {
            "schemas": {
                "Recursive": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/components/schemas/Recursive"},
                        "external": {"$ref": "https://example.test/schema"},
                    },
                }
            }
        }
    }
    rows = list(schema_fields(spec, {"$ref": "#/components/schemas/Recursive"}))
    assert any(r["kind"] == "recursive_boundary" for r in rows)
    assert any(r["kind"] == "unresolved_reference" for r in rows)


def test_all_org_site_mutation_verbs_and_empty_bodies_are_inventoried():
    spec = {
        "info": {"version": "synthetic"},
        "components": {"schemas": {}},
        "paths": {
            "/api/v1/orgs/{org_id}/devices/assign": {"post": {"operationId": "assign"}},
            "/api/v1/sites/{site_id}/devices/{id}/upgrade": {"post": {"operationId": "upgrade"}},
            "/api/v1/sites/{site_id}/evpn_topologies/{id}": {
                "put": {"operationId": "update"},
                "delete": {"operationId": "remove"},
                "get": {"operationId": "read"},
            },
        },
    }
    report = build_report(spec, digest="synthetic")
    operations = report["mutation_operations"]
    assert {r["operation_id"] for r in operations} == {"assign", "upgrade", "update", "remove"}
    assert all(r["admission_status"].startswith("requires_explicit") for r in operations)
    assert report["missing_components"]  # missing roots are not an empty successful audit


def test_admitted_atomic_list_is_not_claimed_to_have_semantic_coverage():
    assert (
        legacy_disposition("site_setting", ["port_usages", "{key}", "rules", "[]", "new"])
        == "legacy_admitted_not_semantic_proof"
    )
    assert legacy_disposition("evpn_topology", ["overwrite"]) == "no_existing_simulation_path"


def test_free_form_and_object_without_type_are_not_hidden_as_typed_values():
    assert list(schema_fields({}, {}))[0]["kind"] == "unconstrained_boundary"
    rows = list(schema_fields({}, {"additionalProperties": True}))
    assert rows[0]["kind"] == "open_object_boundary"


def test_non_json_mutation_inputs_are_also_inventoried():
    spec = {
        "info": {"version": "synthetic"},
        "components": {"schemas": {}},
        "paths": {
            "/api/v1/orgs/{org_id}/psks/import": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"type": "string"}}
                            },
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"file": {"type": "string", "format": "binary"}},
                                    "additionalProperties": False,
                                }
                            },
                        }
                    }
                }
            },
        },
    }
    operation = build_report(spec, digest="synthetic")["mutation_operations"][0]
    assert operation["request_media"] == ["application/json", "multipart/form-data"]
    assert any(
        r["tokens"] == ["file"] and r["format"] == "binary" for r in operation["body_fields"]
    )
