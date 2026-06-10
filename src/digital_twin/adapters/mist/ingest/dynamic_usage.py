"""Dynamic port profile rule evaluation (Mist `mode: "dynamic"` port usages).

A `port_config` entry with `dynamic_usage: "<profile>"` takes its RUNTIME usage
from the named profile's match rules when a device connects. Semantics pinned
from a real template (2026-06-10): rules evaluate IN ORDER, `expression`
"[a:b]" slices the source value, `equals` exact-matches (case-sensitive),
first match wins; nothing connected -> the static usage stands.

Honesty contract for `sources` (what the twin observed about the port's
neighbor): key present with a string = evaluable; key present with None =
KNOWN absent (no LLDP neighbor — the rule conclusively misses); key MISSING =
UNOBSERVABLE by the twin (e.g. lldp_system_description is in no fetched stat)
— a rule on such a source, reached before any match, makes the whole outcome
INCONCLUSIVE: the twin never guesses a runtime profile. Malformed rules are
unevaluable, not crashes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, device_id

_SLICE = re.compile(r"^\[(\d*):(\d*)\]$")
_VLAN_DEFINING_ROOTS = ("port_usages", "networks")


@dataclass(frozen=True)
class RuleOutcome:
    kind: str  # "matched" | "static" | "inconclusive"
    usage: str | None = None  # the matched profile name (kind == "matched")
    rule_index: int | None = None


def _apply_expression(value: str, expression: Any) -> str | None:
    """The slice of `value` the rule compares; None = malformed expression."""
    if expression in (None, ""):
        return value
    m = _SLICE.match(str(expression))
    if not m:
        return None
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else len(value)
    return value[start:end]


def evaluate_rules(
    rules: Sequence[Mapping[str, Any]],
    sources: Mapping[str, str | None],
) -> RuleOutcome:
    for index, rule in enumerate(rules):
        src, equals, usage = rule.get("src"), rule.get("equals"), rule.get("usage")
        if not src or equals is None or not usage:
            return RuleOutcome(kind="inconclusive")  # malformed = unevaluable
        if str(src) not in sources:
            return RuleOutcome(kind="inconclusive")  # unobservable source
        value = sources[str(src)]
        if value is None:
            continue  # known absent -> conclusive miss
        sliced = _apply_expression(str(value), rule.get("expression"))
        if sliced is None:
            return RuleOutcome(kind="inconclusive")  # malformed expression
        if sliced == str(equals):
            return RuleOutcome(kind="matched", usage=str(usage), rule_index=index)
    return RuleOutcome(kind="static")


def _without_nulls(obj: Any) -> Any:
    """null == absent (project canon) — for definition-change comparison."""
    if isinstance(obj, Mapping):
        return {k: _without_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_without_nulls(v) for v in obj]
    return obj


def unresolved_dynamic_ports(
    eff: dict[str, Any], stat_rows: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Members whose RUNTIME usage is unknowable: dynamically profiled but the
    rules/observations cannot resolve them (mirrors the ingester's outcomes —
    down ports and conclusive rule misses keep their static usage and are NOT
    unresolved)."""
    from .ports import resolve_port_bases, usage_definition  # local: avoid cycle

    out: list[str] = []
    for member, attrs in resolve_port_bases(eff).items():
        profile = attrs.get("dynamic_usage")
        if not profile:
            continue
        rules = ((eff.get("port_usages") or {}).get(str(profile)) or {}).get("rules")
        if not isinstance(rules, list):
            out.append(member)
            continue
        row = stat_rows.get(member)
        if row is None:
            out.append(member)
            continue
        if not row.get("up"):
            continue  # nothing connected -> static usage stands, conclusive
        outcome = evaluate_rules(rules, {"lldp_system_name": row.get("neighbor_system_name")})
        if outcome.kind == "inconclusive":
            out.append(member)
        elif outcome.kind == "matched" and outcome.usage is not None:
            if usage_definition(eff, outcome.usage)[1] == "unresolved":
                out.append(member)
    return sorted(out)


def unresolved_dynamic_findings(
    baseline_effective: Mapping[str, dict[str, Any]],
    proposed_effective: Mapping[str, dict[str, Any]],
    port_stats: Iterable[Mapping[str, Any]],
) -> tuple[Finding, ...]:
    """One WARNING finding (-> REVIEW) when vlan-defining DEFINITIONS changed on
    a device that has dynamic ports whose runtime usage could NOT be resolved.
    Resolved dynamic ports need no gate — their impact flows into the IR diff
    and the checks reason about it for real. Compiled effective configs are
    compared, so site/template-level ripples are caught per device."""
    rows_by_device: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row in port_stats:
        if row.get("mac") and row.get("port_id"):
            rows_by_device.setdefault(device_id(str(row["mac"])), {})[str(row["port_id"])] = row

    findings: list[Finding] = []
    for did in sorted(set(baseline_effective) | set(proposed_effective)):
        base, prop = baseline_effective.get(did, {}), proposed_effective.get(did, {})
        changed = tuple(
            root
            for root in _VLAN_DEFINING_ROOTS
            if _without_nulls(base.get(root)) != _without_nulls(prop.get(root))
        )
        if not changed:
            continue
        unresolved = unresolved_dynamic_ports(prop, rows_by_device.get(did, {}))
        if not unresolved:
            continue
        findings.append(
            Finding(
                source=FindingSource.ADAPTER,
                category=FindingCategory.OPERATIONAL,
                code="scope.dynamic_ports.unverifiable",
                severity=Severity.WARNING,
                confidence=Confidence(level=ConfidenceLevel.HIGH),
                message=(
                    f"{' and '.join(changed)} redefined while device {did} has "
                    f"dynamically-profiled port(s) {unresolved} whose runtime usage "
                    "could not be resolved from observed LLDP — the impact on them "
                    "(and the devices attached) cannot be verified"
                ),
                evidence={
                    "device": did,
                    "changed_roots": list(changed),
                    "unresolved_dynamic_ports": unresolved,
                },
            )
        )
    return tuple(findings)
