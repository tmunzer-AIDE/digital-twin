"""Give every pinned inventory occurrence an explicit allowlist disposition.

This report is a scope review, not a certificate of full-stack behavior. Exact
cosmetic registrations are contextual; names and descriptions elsewhere do not
inherit their exception. Invocation: uv run python -m tools.review_mist_attributes
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.audit_oas_coverage import LEGACY_TYPES, _pointer

from digital_twin.scope.allowlist import (
    COSMETIC_RAW_ALLOWLIST,
    RAW_ALLOWLIST,
    ignored_raw_fields,
)
from digital_twin.scope.paths import allowed_tokens

# Exact direct update operations only. Assignment, imports, commands, deletes
# and sibling endpoints do not inherit update-body non-interference claims.
UPDATE_CONTEXTS = {
    "updateSiteSettings": "site_setting",
    "updateOrgNetworkTemplate": "networktemplate",
    "updateOrgGatewayTemplate": "gatewaytemplate",
    "updateOrgSiteTemplate": "sitetemplate",
    "updateOrgNacRule": "nacrule",
    "updateOrgWlan": "wlan",
    "updateSiteWlan": "wlan",
}
RATIONALES = {
    "cosmetic": "Registered scalar administrative/display metadata. Network-inert within "
    "the direct update contract; schema validation, companion edits and derived gates still run.",
    "identity": "Immutable identity/audit/status in this resource context; excluded from raw "
    "network deltas. Not a permission to mutate identities or reassign resources.",
    "checked": "Admitted to existing checks, not inherently safe. Coverage, confidence, "
    "effective dependencies, values and runtime evidence determine the verdict.",
    "atomic": "The diff admits the containing list atomically. Element/union grammar is "
    "not independently certified by that admission; needs contextual semantic validation.",
    "boundary": "Open, recursive, unconstrained or unresolved shape. New children cannot "
    "inherit a cosmetic exception or prove behavioral completeness.",
    "selector": "Identity, name, reference, assignment or inheritance can select or generate "
    "configuration. A display-like spelling is not a non-interference proof.",
    "operational": "Alarms, telemetry, schedules, power, upgrades, push policy or device "
    "operations have experience/operational effects outside a cosmetic contract.",
    "behavior": "Forwarding, routing, security, admission, services, RF or external state "
    "requires a validated mechanism/dependency model. Unmodeled leaves remain default-denied.",
    "unsupported_context": "No simulation contract for this object/platform/action. A "
    "harmless-looking leaf does not license the endpoint or its side effects.",
}


def disposition(row: Mapping[str, Any], object_type: str | None) -> tuple[str, str]:
    tokens = tuple(row["tokens"])
    if row["kind"] != "value":
        return "unvalidated_boundary", "boundary"
    if object_type is None:
        return "unsupported_object_or_action", "unsupported_context"
    if tokens and tokens[0] in ignored_raw_fields(object_type):
        return "server_metadata_in_context", "identity"
    # Atomic arrays are not cosmetic even if an element's leaf is a label.
    atomic = "[]" in tokens
    prefix = tokens[:tokens.index("[]")] if atomic else tokens
    probe = tuple("probe" if t == "{key}" else t for t in prefix)
    if not atomic and allowed_tokens(probe, COSMETIC_RAW_ALLOWLIST.get(object_type, ())):
        return "cosmetic_direct_update", "cosmetic"
    if allowed_tokens(probe, RAW_ALLOWLIST.get(object_type, ())):
        return (
            ("atomic_parent_requires_validation", "atomic") if atomic
            else ("admitted_requires_checks", "checked")
        )
    if tokens and (
        tokens[-1] in {"name", "id", "type", "model", "role", "map_id"}
        or tokens[-1].endswith(("_id", "_ids"))
        or tokens[0].endswith("matching")
    ):
        return "denied_without_semantic_model", "selector"
    if any(
        part in token.lower() for token in tokens
        for part in ("alarm", "critical", "upgrade", "poe", "schedule", "push_policy", "sle_",
                     "syslog", "telemetry", "threshold", "snmp", "pcap")
    ):
        return "denied_without_semantic_model", "operational"
    return "denied_without_semantic_model", "behavior"


def _receipt(row: Mapping[str, Any], object_type: str | None, index: int) -> dict[str, Any]:
    status, rationale = disposition(row, object_type)
    return {
        "source_field_index": index,
        "path": _pointer(tuple(row["tokens"])),
        "variants": row["variants"],
        "disposition": status,
        "rationale": rationale,
    }


def build_review(inventory: Mapping[str, Any], *, inventory_digest: str) -> dict[str, Any]:
    fields = [
        {"component": row["component"], **_receipt(row, LEGACY_TYPES.get(row["component"]), i)}
        for i, row in enumerate(inventory["fields"])
    ]
    operations = []
    for op in inventory["mutation_operations"]:
        context = UPDATE_CONTEXTS.get(op["operation_id"]) if op["method"] == "PUT" else None
        reviewed = []
        for i, row in enumerate(op["body_fields"]):
            row_context = context
            if op["operation_id"] == "updateSiteDevice" and op["method"] == "PUT":
                # Pinned 2609.1.0 mist_device.oneOf[1] = device_switch. AP and
                # gateway branches remain unsupported, including their labels.
                row_context = "device" if row["variants"][:1] == ["oneOf[1]"] else None
            reviewed.append({"media_type": row["media_type"], **_receipt(row, row_context, i)})
        operations.append({
            "operation_id": op["operation_id"], "method": op["method"], "path": op["path"],
            "action_contract": "existing_update_contract" if context is not None
            or op["operation_id"] == "updateSiteDevice" and op["method"] == "PUT"
            else "requires_separate_action_contract",
            "request_body_status": op["request_body_status"],
            "input_fields": reviewed,
        })
    return {
        "review_date": "2026-10-05",
        "spec_version": inventory["spec_version"],
        "spec_sha256": inventory["spec_sha256"],
        "inventory_sha256": inventory_digest,
        "scope": "Every occurrence in the pinned 34-root / 473 org-site mutation inventory. "
        "Descriptions/types remain in that inventory at source_field_index. No live calibration.",
        "limitations": "Cosmetic is network non-interference in direct updates, not a promise "
        "about external automation, push timing, full-stack completeness or all cloud behavior. "
        "Never use this ledger to authorize an unsupported object or action.",
        "rationales": RATIONALES,
        "summary": {
            "component_field_occurrences": len(fields),
            "mutation_operations": len(operations),
            "operation_field_occurrences": sum(len(op["input_fields"]) for op in operations),
            "component_dispositions": dict(Counter(r["disposition"] for r in fields)),
            "operation_dispositions": dict(Counter(
                r["disposition"] for op in operations for r in op["input_fields"]
            )),
            "per_component": {
                c: dict(Counter(r["disposition"] for r in fields if r["component"] == c))
                for c in inventory["component_scope"]
            },
        },
        "fields": fields,
        "mutation_operations": operations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", type=Path, default=Path("docs/evidence/mist-oas-coverage-audit.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/mist-attribute-review.json")
    )
    args = parser.parse_args()
    raw = args.inventory.read_bytes()
    inventory = json.loads(raw)
    if (
        inventory["spec_version"] != "2609.1.0" or inventory["missing_components"]
        or inventory["spec_sha256"]
        != "22f55432535ab38f6c0539392a729b8fd515a9ccae9df693fbd4ff23d40b8fac"
    ):
        raise SystemExit(
            "Reconcile the pinned schema/context contracts before reviewing a new inventory"
        )
    review = build_review(inventory, inventory_digest=hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(review, indent=2) + "\n")
    print(json.dumps(review["summary"], indent=2))


if __name__ == "__main__":
    main()
