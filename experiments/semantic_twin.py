"""Executable feasibility model, not a Mist adapter or production digital twin.

Ordered packet transfer functions compose across domains. A single evaluator
checks reachability and isolation; missing facts and exploration limits remain
unknown. Concrete packets keep this experiment small. Production would require
symbolic packet sets, onboarding state, and validated vendor semantics.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

type Value = str | int | bool | None
type Packet = tuple[tuple[str, Value], ...]
type State = tuple[str, Packet]
type Frontier = tuple[str, Packet, tuple[str, ...], frozenset[State], tuple[str, ...]]


class Disposition(StrEnum):
    REACHED = "reached"
    DROPPED = "dropped"
    UNKNOWN = "unknown"
    LOOP = "loop"


class Result(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Ref:
    field: str


@dataclass(frozen=True)
class Rule:
    matches: tuple[tuple[str, Value], ...] = ()
    writes: tuple[tuple[str, Value | Ref], ...] = ()
    next_nodes: tuple[str, ...] = ()
    terminal: Disposition | None = None
    reason: str = ""


@dataclass(frozen=True)
class Node:
    id: str
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Trace:
    disposition: Disposition
    path: tuple[str, ...]
    packet: Packet
    reason: str
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evaluation:
    result: Result
    traces: tuple[Trace, ...]


def _match(rule: Rule, packet: Packet) -> bool | None:
    facts = dict(packet)
    unknown = False
    for key, expected in rule.matches:
        actual = facts.get(key)
        if actual is None or expected is None:
            unknown = True
        elif type(actual) is not type(expected):
            # A caller's type annotation does not validate runtime input.
            # In particular, integer 1 is not proof of a boolean capability.
            unknown = True
        elif actual != expected:
            return False
    return None if unknown else True


def _choices(node: Node, packet: Packet) -> tuple[tuple[Rule | None, tuple[str, ...]], ...]:
    choices: list[tuple[Rule | None, tuple[str, ...]]] = []
    uncertain: tuple[str, ...] = ()
    for index, rule in enumerate(node.rules):
        matched = _match(rule, packet)
        if matched is False:
            continue
        if matched is None:
            # Both match and fall-through remain possible. An unknown earlier
            # rule must never be skipped to manufacture a definite allow.
            uncertain = (*uncertain, f"{node.id}: missing or mistyped fact in rule {index}")
            choices.append((rule, uncertain))
            continue
        choices.append((rule, uncertain))
        return tuple(choices)
    choices.append((None, uncertain))
    return tuple(choices)


def explore(
    nodes: tuple[Node, ...],
    entry: str,
    facts: Mapping[str, Value],
    *,
    max_states: int = 10_000,
) -> tuple[Trace, ...]:
    """Explore ordered transfers, preserving every possible forwarding branch."""
    if max_states < 1:
        raise ValueError("max_states must be positive")
    index = {node.id: node for node in nodes}
    if len(index) != len(nodes):
        raise ValueError("duplicate node identity")
    initial: Packet = tuple(sorted(facts.items()))
    pending: deque[Frontier] = deque([(entry, initial, (), frozenset(), ())])
    traces: list[Trace] = []
    visited = 0
    while pending:
        location, packet, path, ancestors, uncertain = pending.popleft()
        visited += 1
        if visited > max_states:
            traces.append(
                Trace(
                    Disposition.UNKNOWN,
                    (*path, location),
                    packet,
                    "exploration budget exhausted",
                    uncertain,
                )
            )
            # One terminal records that the remaining frontier is unexplored.
            # This blocks satisfaction even if another branch already reached.
            break
        state = (location, packet)
        next_path = (*path, location)
        if state in ancestors:
            traces.append(Trace(Disposition.LOOP, next_path, packet, "repeated state", uncertain))
            continue
        node = index.get(location)
        if node is None:
            traces.append(
                Trace(
                    Disposition.UNKNOWN,
                    next_path,
                    packet,
                    "missing node semantics",
                    uncertain,
                )
            )
            continue
        for rule, missing in _choices(node, packet):
            branch_uncertain = (*uncertain, *missing)
            if rule is None:
                traces.append(
                    Trace(
                        Disposition.DROPPED,
                        next_path,
                        packet,
                        "no matching rule",
                        branch_uncertain,
                    )
                )
                continue
            original = dict(packet)
            proposed = dict(original)
            for field, value in rule.writes:
                proposed[field] = original.get(value.field) if isinstance(value, Ref) else value
                if isinstance(value, Ref) and proposed[field] is None:
                    branch_uncertain = (*branch_uncertain, f"{location}: missing {value.field}")
            updated: Packet = tuple(sorted(proposed.items()))
            if rule.terminal is not None:
                traces.append(
                    Trace(
                        rule.terminal,
                        next_path,
                        updated,
                        rule.reason,
                        branch_uncertain,
                    )
                )
            elif rule.next_nodes:
                for destination in rule.next_nodes:
                    pending.append(
                        (
                            destination,
                            updated,
                            next_path,
                            ancestors | {state},
                            branch_uncertain,
                        )
                    )
            else:
                traces.append(
                    Trace(
                        Disposition.UNKNOWN,
                        next_path,
                        updated,
                        "rule has no effect",
                        branch_uncertain,
                    )
                )
    return tuple(traces)


def evaluate(
    nodes: tuple[Node, ...],
    entry: str,
    facts: Mapping[str, Value],
    target: str,
    *,
    must_reach: bool = True,
    max_states: int = 10_000,
) -> Evaluation:
    """Check delivery on every branch, or isolation on every branch.

    A concrete bad branch proves a violation even if others remain unknown.
    Unknown facts can never establish satisfaction in this conservative model.
    """
    traces = explore(nodes, entry, facts, max_states=max_states)
    unknown = False
    for trace in traces:
        if trace.uncertainties or trace.disposition is Disposition.UNKNOWN:
            unknown = True
            continue
        reached = trace.disposition is Disposition.REACHED and trace.path[-1] == target
        if reached != must_reach:
            return Evaluation(Result.VIOLATED, traces)
    return Evaluation(Result.UNKNOWN if unknown or not traces else Result.SATISFIED, traces)


def replace_nodes(nodes: tuple[Node, ...], changes: tuple[Node, ...]) -> tuple[Node, ...]:
    """Apply a whole batch to an independent proposed state before evaluation."""
    baseline = {node.id: node for node in nodes}
    overlay = {node.id: node for node in changes}
    if len(baseline) != len(nodes) or len(overlay) != len(changes):
        raise ValueError("duplicate node identity")
    if not overlay.keys() <= baseline.keys():
        raise ValueError("unknown batch target")
    return tuple(overlay.get(node.id, node) for node in nodes)
