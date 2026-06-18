"""Central resolution of a `Finding.subject`'s human name from the IR.

Checks set only the headline object's `kind` + `id` (what they know locally);
the registry calls `name_findings()` to fill `ObjectRef.name` once, here, with
the IR in hand. The IR carries names for vlans (`Vlan.name`) and ports
(`Port.name`); a link's label is composed from its two port names. Devices
resolve via `Device.name` (a display name; `model` is never used). Unknown/absent
names stay None and the renderer falls back to the id. Proposed IR is consulted
first, then baseline — so a finding about a REMOVED entity (gone from the proposed
IR) still resolves. The full set of involved entities still rides in
`Finding.affected_entities`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from digital_twin.contracts import Cause, Finding, ObjectRef
from digital_twin.ir import IR


def _name_for(kind: str, oid: str, ir: IR) -> str | None:
    if kind == "vlan":
        try:
            v = ir.vlans.get(int(oid))
        except ValueError:
            return None
        return v.name if v else None
    if kind == "port":
        p = ir.ports.get(oid)
        return p.name if p else None
    if kind == "link":
        # link id is `{port_a}__{port_b}` (see ir.entities.link_id); port ids
        # carry no "__", so splitting recovers the two endpoints
        parts = oid.split("__")
        if len(parts) != 2:
            return None
        a, b = ir.ports.get(parts[0]), ir.ports.get(parts[1])
        return f"{a.name} <-> {b.name}" if a and b else None
    if kind == "device":
        d = ir.devices.get(oid)
        return d.name if d else None  # Device.name only — model is NOT an identity
    return None  # dhcp_scope / site_setting / etc. have no IR name source


def resolve_subject(subject: ObjectRef | None, prop_ir: IR, base_ir: IR) -> ObjectRef | None:
    """Fill `name` from the IR (proposed first, then baseline). No-op when the
    subject is absent or already named."""
    if subject is None or subject.name is not None:
        return subject
    name = _name_for(subject.kind, subject.id, prop_ir) or _name_for(
        subject.kind, subject.id, base_ir
    )
    return replace(subject, name=name) if name else subject


def _resolve_caused_by(
    causes: tuple[Cause, ...], prop_ir: IR, base_ir: IR
) -> tuple[Cause, ...]:
    return tuple(
        replace(c, ref=resolve_subject(c.ref, prop_ir, base_ir))  # type: ignore[arg-type]
        for c in causes
    )


def _resolve_nested_impacts(
    evidence: Mapping[str, Any], prop_ir: IR, base_ir: IR
) -> Mapping[str, Any]:
    impacts = evidence.get("impacts")
    if not isinstance(impacts, list):
        return evidence
    new_impacts = []
    for imp in impacts:
        if isinstance(imp, dict) and imp.get("caused_by"):
            imp = {
                **imp,
                "caused_by": list(
                    _resolve_caused_by(tuple(imp["caused_by"]), prop_ir, base_ir)
                ),
            }
        new_impacts.append(imp)
    return {**evidence, "impacts": new_impacts}


def name_findings(
    findings: tuple[Finding, ...], prop_ir: IR, base_ir: IR
) -> tuple[Finding, ...]:
    return tuple(
        replace(
            f,
            subject=resolve_subject(f.subject, prop_ir, base_ir),
            caused_by=_resolve_caused_by(f.caused_by, prop_ir, base_ir),
            evidence=_resolve_nested_impacts(f.evidence, prop_ir, base_ir),
        )
        for f in findings
    )
