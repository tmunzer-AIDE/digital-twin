"""Inventory configuration surfaces without claiming semantic implementation.

Run with the pinned Mist JSON specification and an output path. The report
enumerates selected full-stack schemas and every org/site mutation endpoint,
including assignments, diagnostic commands, firmware and other operational
actions. New and open-ended shapes remain explicitly unvalidated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS, RAW_ALLOWLIST
from digital_twin.scope.paths import allowed

# This is an inventory scope, not a support or exemption list. Operation bodies
# below are inventoried independently, so an omitted component stays visible in
# the mutation-endpoint catalog instead of becoming implicitly supported.
COMPONENTS = (
    "site",
    "sitegroup",
    "org_setting",
    "site_setting",
    "device_ap",
    "device_switch",
    "device_gateway",
    "deviceprofile_ap",
    "deviceprofile_switch",
    "deviceprofile_gateway",
    "network_template",
    "gateway_template",
    "site_template",
    "ap_template",
    "rf_template",
    "template",
    "wlan",
    "wxlan_rule",
    "wxlan_tag",
    "wxlan_tunnel",
    "nac_rule",
    "nac_tag",
    "nac_portal",
    "psk",
    "secpolicy",
    "network",
    "service",
    "service_policy",
    "vpn",
    "mxedge",
    "mxcluster",
    "mxtunnel",
    "evpn_topology",
    "idp_profile",
)
LEGACY_TYPES = {
    "device_switch": "device",
    "site_setting": "site_setting",
    "wlan": "wlan",
    "network_template": "networktemplate",
    "gateway_template": "gatewaytemplate",
    "site_template": "sitetemplate",
    "nac_rule": "nacrule",
}


def _pointer(tokens: tuple[str, ...]) -> str:
    return "/" + "/".join(t.replace("~", "~0").replace("/", "~1") for t in tokens)


def _resolve(spec: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"external reference not resolved: {ref}")
    node: Any = spec
    for token in ref[2:].split("/"):
        node = node[token.replace("~1", "/").replace("~0", "~")]
    if not isinstance(node, Mapping):
        raise ValueError(f"reference does not resolve to an object: {ref}")
    return node


def schema_fields(
    spec: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    tokens: tuple[str, ...] = (),
    references: tuple[str, ...] = (),
    variants: tuple[str, ...] = (),
) -> Iterator[dict[str, Any]]:
    """Keep map keys, list boundaries, unions, cycles and open objects explicit."""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in references:
            yield {
                "tokens": list(tokens),
                "kind": "recursive_boundary",
                "reference": ref,
                "variants": list(variants),
            }
            return
        try:
            resolved = dict(_resolve(spec, ref))
        except (KeyError, ValueError) as error:
            yield {
                "tokens": list(tokens),
                "kind": "unresolved_reference",
                "reference": ref,
                "error": str(error),
                "variants": list(variants),
            }
            return
        resolved.update({k: v for k, v in schema.items() if k != "$ref"})
        yield from schema_fields(
            spec, resolved, tokens=tokens, references=(*references, ref), variants=variants
        )
        return
    branches = False
    for combinator in ("allOf", "oneOf", "anyOf"):
        for index, child in enumerate(schema.get(combinator, ())):
            branches = True
            yield from schema_fields(
                spec,
                child,
                tokens=tokens,
                references=references,
                variants=(*variants, f"{combinator}[{index}]"),
            )
    properties = schema.get("properties", {})
    for key, child in properties.items():
        yield from schema_fields(
            spec, child, tokens=(*tokens, key), references=references, variants=variants
        )
    items = schema.get("items")
    if isinstance(items, Mapping):
        yield from schema_fields(
            spec, items, tokens=(*tokens, "[]"), references=references, variants=variants
        )
    additional = schema.get("additionalProperties")
    if isinstance(additional, Mapping):
        yield from schema_fields(
            spec, additional, tokens=(*tokens, "{key}"), references=references, variants=variants
        )
    declared_type = schema.get("type")
    is_object = (
        declared_type == "object"
        or isinstance(declared_type, list)
        and "object" in declared_type
        or "properties" in schema
        or "additionalProperties" in schema
    )
    if is_object and additional is not False and not isinstance(additional, Mapping):
        yield {
            "tokens": [*tokens, "{unknown}"],
            "kind": "open_object_boundary",
            "variants": list(variants),
        }
    has_children = bool(properties) or isinstance(items, Mapping) or isinstance(additional, Mapping)
    if not has_children and not branches and not is_object:
        yield {
            "tokens": list(tokens),
            "kind": "value"
            if any(k in schema for k in ("type", "enum", "const"))
            else "unconstrained_boundary",
            "variants": list(variants),
            **{
                k: schema[k]
                for k in (
                    "type",
                    "enum",
                    "const",
                    "default",
                    "format",
                    "pattern",
                    "minLength",
                    "maxLength",
                    "minItems",
                    "maxItems",
                    "readOnly",
                    "writeOnly",
                    "nullable",
                    "minimum",
                    "maximum",
                    "description",
                )
                if k in schema
            },
        }
    elif is_object and not has_children and additional is False:
        yield {"tokens": list(tokens), "kind": "closed_empty_object", "variants": list(variants)}


def legacy_disposition(component: str, tokens: list[str]) -> str:
    object_type = LEGACY_TYPES.get(component)
    if object_type is None:
        return "no_existing_simulation_path"
    if tokens and tokens[0] in IGNORED_RAW_FIELDS:
        return "legacy_ignored_not_semantic_proof"
    # The existing diff treats lists atomically. Admitting a list does not prove
    # that every union, element, or new child grammar in it is understood.
    concrete = []
    for token in tokens:
        if token == "[]":
            break
        concrete.append("probe" if token in ("{key}", "{unknown}") else token)
    return (
        "legacy_admitted_not_semantic_proof"
        if allowed(".".join(concrete), RAW_ALLOWLIST.get(object_type, ()))
        else "legacy_rejects"
    )


def _request_media(spec: Mapping[str, Any], operation: Mapping[str, Any]) -> Mapping[str, Any]:
    body = operation.get("requestBody", {})
    if "$ref" in body:
        body = _resolve(spec, body["$ref"])
    media = body.get("content", {})
    if not isinstance(media, Mapping):
        raise ValueError("request body content is not an object")
    return media


def build_report(spec: Mapping[str, Any], *, digest: str) -> dict[str, Any]:
    fields = []
    missing = []
    schemas = spec["components"]["schemas"]
    for component in COMPONENTS:
        if component not in schemas:
            missing.append(component)
            continue
        for row in schema_fields(spec, {"$ref": f"#/components/schemas/{component}"}):
            fields.append(
                {
                    "component": component,
                    "path": _pointer(tuple(row["tokens"])),
                    "legacy_disposition": legacy_disposition(component, row["tokens"]),
                    "semantic_status": "unvalidated",
                    **row,
                }
            )
    operations = []
    for path, item in sorted(spec["paths"].items()):
        if not path.startswith(("/api/v1/orgs/", "/api/v1/sites/")):
            continue
        for method, operation in item.items():
            if method not in ("post", "put", "patch", "delete"):
                continue
            media = _request_media(spec, operation)
            body_fields = [
                {"media_type": media_type, **row}
                for media_type, description in media.items()
                for row in schema_fields(spec, description.get("schema", {}))
            ]
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation.get("operationId"),
                    "request_media": list(media),
                    "request_body_status": "documented" if media else "none_documented",
                    "body_fields": body_fields,
                    "admission_status": "requires_explicit_action_and_semantic_validation",
                    "note": "HTTP verbs alone cannot distinguish config, assignment, reorder, "
                    "upgrade, diagnostic or destructive operational actions.",
                }
            )
    return {
        "purpose": "Exhaustive inventory within the declared schema scope, plus all org/site "
        "mutation endpoints. No field or endpoint is certified by this inventory.",
        "spec_version": spec["info"]["version"],
        "spec_sha256": digest,
        "component_scope": list(COMPONENTS),
        "missing_components": missing,
        "summary": {
            "fields": len(fields),
            "mutation_operations": len(operations),
            "operation_input_fields": sum(len(op["body_fields"]) for op in operations),
            "operation_field_kinds": dict(
                Counter(row["kind"] for op in operations for row in op["body_fields"])
            ),
            "field_kinds": dict(Counter(row["kind"] for row in fields)),
            "legacy_dispositions": dict(Counter(row["legacy_disposition"] for row in fields)),
        },
        "fields": fields,
        "mutation_operations": operations,
        "new_field_policy": "Unregistered fields, enum values, object contexts, open trees and "
        "operational actions cannot establish complete simulation coverage.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.spec.read_bytes()
    report = build_report(json.loads(raw), digest=hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    if report["missing_components"]:
        raise SystemExit(
            "Missing configuration schemas: " + ", ".join(report["missing_components"])
        )
    unresolved = [r for r in report["fields"] if r["kind"] == "unresolved_reference"]
    unresolved += [
        r
        for op in report["mutation_operations"]
        for r in op["body_fields"]
        if r["kind"] == "unresolved_reference"
    ]
    if unresolved:
        raise SystemExit(f"Unresolved schema references: {len(unresolved)}")


if __name__ == "__main__":
    main()
