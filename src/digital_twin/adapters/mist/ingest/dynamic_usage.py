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

from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, device_id

# expression grammar (per the OAS): an optional `split(<delim>)` followed by
# any chain of `[n]` (index) / `[a:b]` (slice) operations — e.g. "[0:3]",
# "split(.)[1]", "split(-)[1][0:3]"
_EXPR_TOKEN = re.compile(r"split\(([^)]*)\)|\[(-?\d*):(-?\d*)\]|\[(-?\d+)\]")
_VLAN_DEFINING_ROOTS = ("port_usages", "networks")


@dataclass(frozen=True)
class RuleOutcome:
    kind: str  # "matched" | "static" | "inconclusive"
    usage: str | None = None  # the matched profile name (kind == "matched")
    rule_index: int | None = None


class _Unparseable(Exception):
    """The expression uses grammar the twin does not model."""


class _NoValue(Exception):
    """The expression conclusively yields nothing (e.g. split index OOR)."""


def _apply_expression(value: str, expression: Any) -> str:
    """The transform of `value` the rule compares (OAS grammar: optional
    split(<delim>), then [n] index / [a:b] slice chains). Raises _Unparseable
    for unknown grammar (-> inconclusive) and _NoValue when the expression
    conclusively selects nothing (e.g. out-of-range split index -> miss)."""
    if expression in (None, ""):
        return value
    expr = str(expression)
    pos = 0
    current: str | list[str] = value
    for m in _EXPR_TOKEN.finditer(expr):
        if m.start() != pos:
            raise _Unparseable(expr)
        pos = m.end()
        delim, sl_start, sl_end, index = m.group(1), m.group(2), m.group(3), m.group(4)
        if delim is not None:
            if not isinstance(current, str) or not delim:
                raise _Unparseable(expr)
            current = current.split(delim)
        elif index is not None:
            try:
                current = current[int(index)]
            except IndexError:
                raise _NoValue(expr) from None
        else:
            start = int(sl_start) if sl_start else None
            end = int(sl_end) if sl_end else None
            sliced = current[start:end]
            if isinstance(sliced, list):  # slicing a list yields a list — not
                raise _Unparseable(expr)  # a comparable string; unknown intent
            current = sliced
    if pos != len(expr) or not isinstance(current, str):
        raise _Unparseable(expr)
    return current


def evaluate_rules(
    rules: Sequence[Mapping[str, Any]],
    sources: Mapping[str, str | None],
) -> RuleOutcome:
    for index, rule in enumerate(rules):
        src, usage = rule.get("src"), rule.get("usage")
        equals, equals_any = rule.get("equals"), rule.get("equals_any")
        wanted = [str(equals)] if equals is not None else [str(x) for x in equals_any or ()]
        if not src or not wanted or not usage:
            return RuleOutcome(kind="inconclusive")  # malformed = unevaluable
        if str(src) not in sources:
            return RuleOutcome(kind="inconclusive")  # unobservable source
        value = sources[str(src)]
        if value is None:
            continue  # known absent -> conclusive miss
        try:
            transformed = _apply_expression(str(value), rule.get("expression"))
        except _Unparseable:
            return RuleOutcome(kind="inconclusive")  # unknown grammar
        except _NoValue:
            continue  # the expression conclusively selects nothing -> miss
        if transformed in wanted:
            return RuleOutcome(kind="matched", usage=str(usage), rule_index=index)
    return RuleOutcome(kind="static")


def _without_nulls(obj: Any) -> Any:
    """null == absent (project canon) — for definition-change comparison."""
    if isinstance(obj, Mapping):
        return {k: _without_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_without_nulls(v) for v in obj]
    return obj


def classify_dynamic_port(
    eff: dict[str, Any], profile: str, row: Mapping[str, Any] | None
) -> tuple[str, str | None]:
    """One dynamically-profiled port's runtime resolution — the SINGLE source of
    truth shared by the ingester and the honesty gate:
    ("matched", usage_name) — a rule matched and the usage has a definition;
    ("static", None)        — nothing connected / conclusive rule miss: the
                              static usage stands (Mist semantics);
    ("unresolved", reason)  — the runtime usage is UNKNOWABLE (no rules, no
                              stats row, inconclusive rules, matched usage
                              undefined, or reset_default_when='none' on a
                              down port: it keeps the LAST dynamic usage).
    """
    from .ports import usage_definition  # local: avoid import cycle

    spec = (eff.get("port_usages") or {}).get(str(profile)) or {}
    rules = spec.get("rules")
    if not isinstance(rules, list):
        return "unresolved", f"dynamic profile {profile!r} has no rules in the modeled config"
    if row is None:
        return "unresolved", "no port stats for the dynamically-profiled port"
    if not row.get("up"):
        if spec.get("reset_default_when") == "none":
            return "unresolved", (
                "link down with reset_default_when='none' — the port keeps its "
                "LAST dynamic usage, unknowable from config+stats"
            )
        return "static", None  # nothing connected -> static usage stands
    outcome = evaluate_rules(rules, {"lldp_system_name": row.get("neighbor_system_name")})
    if outcome.kind == "static":
        return "static", None
    if outcome.kind == "matched" and outcome.usage is not None:
        if usage_definition(eff, outcome.usage)[1] == "unresolved":
            return "unresolved", (
                f"runtime usage {outcome.usage!r} (dynamic rule) has no definition "
                "in the modeled config"
            )
        return "matched", outcome.usage
    return "unresolved", "dynamic rules not evaluable from observed LLDP"


def unresolved_dynamic_ports(
    eff: dict[str, Any], stat_rows: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Members whose RUNTIME usage is unknowable (see classify_dynamic_port)."""
    from .ports import resolve_port_bases  # local: avoid import cycle

    out: list[str] = []
    for member, attrs in resolve_port_bases(eff).items():
        profile = attrs.get("dynamic_usage")
        if not profile:
            continue
        kind, _ = classify_dynamic_port(eff, str(profile), stat_rows.get(member))
        if kind == "unresolved":
            out.append(member)
    return sorted(out)


def unresolved_dynamic_findings(
    baseline_effective: Mapping[str, dict[str, Any]],
    proposed_effective: Mapping[str, dict[str, Any]],
    port_stats: Iterable[Mapping[str, Any]],
) -> tuple[Finding, ...]:
    """One WARNING finding (-> REVIEW) when vlan-defining DEFINITIONS changed on
    a device that has dynamic ports whose runtime usage could NOT be resolved
    on EITHER side: an unresolved baseline means the CURRENT state being
    transitioned from is unknown — a delta from an unknown state cannot be
    verified even when the proposed side resolves (e.g. the proposal ADDS the
    previously-missing usage definition). Resolved-both-sides dynamic ports
    need no gate — their impact flows into the IR diff and the checks reason
    about it for real. Compiled effective configs are compared, so
    site/template-level ripples are caught per device."""
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
        rows = rows_by_device.get(did, {})
        base_unresolved = unresolved_dynamic_ports(base, rows)
        prop_unresolved = unresolved_dynamic_ports(prop, rows)
        if not base_unresolved and not prop_unresolved:
            continue
        affected = sorted(set(base_unresolved) | set(prop_unresolved))
        findings.append(
            Finding(
                source=FindingSource.ADAPTER,
                category=FindingCategory.OPERATIONAL,
                code="scope.dynamic_ports.unverifiable",
                subject=ObjectRef("device", did, name=prop.get("name") or base.get("name")),
                severity=Severity.WARNING,
                confidence=Confidence(level=ConfidenceLevel.HIGH),
                message=(
                    f"{' and '.join(changed)} redefined while device {did} has "
                    f"dynamically-profiled port(s) {affected} whose runtime usage "
                    "could not be resolved from observed LLDP — the impact on them "
                    "(and the devices attached) cannot be verified"
                ),
                evidence={
                    "device": did,
                    "changed_roots": list(changed),
                    "unresolved_dynamic_ports": {
                        "baseline": base_unresolved,
                        "proposed": prop_unresolved,
                    },
                },
            )
        )
    return tuple(findings)
