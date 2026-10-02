"""Ordered symbolic transfer programs with explicit uncertainty and resource bounds."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from .snapshot import ObjectKey
from .space import Domain, Space


class Outcome(StrEnum):
    DELIVERED = "delivered"
    DROPPED = "dropped"
    LOOP = "loop"
    UNKNOWN = "unknown"


class Status(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Match:
    field: str
    domain: Domain

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field or not isinstance(self.domain, Domain):
            raise ValueError("a match requires a named field and typed domain")


@dataclass(frozen=True)
class Copy:
    field: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field:
            raise ValueError("a copy requires a named source field")


@dataclass(frozen=True)
class Rule:
    matches: tuple[Match, ...] = ()
    writes: tuple[tuple[str, Domain | Copy], ...] = ()
    destinations: tuple[str, ...] = ()
    outcome: Outcome | None = None
    reason: str = ""
    opaque: bool = False

    def __post_init__(self) -> None:
        if self.outcome is not None and self.destinations:
            raise ValueError("a rule cannot both terminate and forward")
        if len({key for key, _ in self.writes}) != len(self.writes):
            raise ValueError("a transfer writes the same field twice")
        if type(self.opaque) is not bool:
            raise ValueError("opaque status must be boolean")
        if self.outcome is not None and not isinstance(self.outcome, Outcome):
            raise ValueError("rule outcome must be a recognized Outcome")
        if any(not isinstance(d, str) or not d for d in self.destinations):
            raise ValueError("forwarding destinations must be named nodes")
        if any(
            not isinstance(k, str)
            or not k
            or not isinstance(v, (Domain, Copy))
            or (isinstance(v, Domain) and v.empty)
            for k, v in self.writes
        ):
            raise ValueError("writes require named fields and nonempty domains or copies")
        object.__setattr__(self, "matches", tuple(self.matches))
        object.__setattr__(self, "writes", tuple(self.writes))
        object.__setattr__(self, "destinations", tuple(self.destinations))


@dataclass(frozen=True)
class Node:
    id: str
    rules: tuple[Rule, ...]
    sources: tuple[ObjectKey, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules))
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class Trace:
    outcome: Outcome
    path: tuple[str, ...]
    space: Space
    reason: str
    uncertainties: tuple[str, ...] = ()


@dataclass(frozen=True)
class Query:
    id: str
    entry: str
    space: Space
    target: str
    must_reach: bool = True
    # A query names its population. A configured potential cohort need not be observed active.
    population: str = "specified packet/client class"

    def __post_init__(self) -> None:
        if not all((self.id, self.entry, self.target, self.population)):
            raise ValueError("a query requires identity, endpoints and population")
        if type(self.must_reach) is not bool:
            raise ValueError("query reachability intent must be boolean")


@dataclass(frozen=True)
class Evaluation:
    query_id: str
    status: Status
    traces: tuple[Trace, ...]


type Branch = tuple[Space, tuple[str, ...]]
type Frontier = tuple[str, Space, tuple[str, ...], frozenset[tuple[str, Space]], tuple[str, ...]]


def partition(space: Space, matches: tuple[Match, ...]) -> tuple[Branch | None, tuple[Branch, ...]]:
    """Disjoint exact partition by a conjunction; absent facts remain correlated."""
    candidate: Branch = (space, ())
    remainder: list[Branch] = []
    for match in matches:
        current, uncertain = candidate
        actual = current.get(match.field)
        if actual is None:
            actual = Domain.universe(match.domain.kind)
            uncertain = (*uncertain, f"missing fact: {match.field}")
        elif actual.kind != match.domain.kind:
            if {actual.kind, match.domain.kind} == {"ipv4", "ipv6"}:
                # Known IP families are disjoint, not malformed input.
                remainder.append(candidate)
                return None, tuple(remainder)
            # Mistyped facts cannot be coerced into a match or a definite mismatch.
            actual = Domain.universe(match.domain.kind)
            uncertain = (*uncertain, f"mistyped fact: {match.field}")
        yes = actual.intersect(match.domain)
        no = actual.subtract(match.domain)
        if not no.empty:
            remainder.append((current.with_field(match.field, no), uncertain))
        if yes.empty:
            return None, tuple(remainder)
        candidate = (current.with_field(match.field, yes), uncertain)
    return candidate, tuple(remainder)


@dataclass(frozen=True)
class Program:
    nodes: tuple[Node, ...]

    def __post_init__(self) -> None:
        if any(not node.id for node in self.nodes) or len({n.id for n in self.nodes}) != len(
            self.nodes
        ):
            raise ValueError("program node identities must be unique and nonempty")
        object.__setattr__(self, "nodes", tuple(self.nodes))

    def dependencies(self, entry: str) -> tuple[ObjectKey, ...]:
        index = {n.id: n for n in self.nodes}
        pending = [entry]
        seen: set[str] = set()
        sources: set[ObjectKey] = set()
        while pending:
            location = pending.pop()
            if location in seen:
                continue
            seen.add(location)
            node = index.get(location)
            if node is not None:
                sources.update(node.sources)
                pending.extend(d for rule in node.rules for d in rule.destinations)
        return tuple(sorted(sources))

    def explore(self, query: Query, *, max_states: int = 10_000) -> tuple[Trace, ...]:
        if type(max_states) is not int or max_states < 1:
            raise ValueError("max_states must be a positive integer")
        index = {node.id: node for node in self.nodes}
        pending: deque[Frontier] = deque([(query.entry, query.space, (), frozenset(), ())])
        traces: list[Trace] = []
        states = 0
        partitions = 0
        while pending:
            location, space, path, ancestors, uncertain = pending.popleft()
            states += 1
            path = (*path, location)
            if states > max_states:
                traces.append(Trace(Outcome.UNKNOWN, path, space, "exploration budget exhausted"))
                break
            state = (location, space)
            if state in ancestors:
                traces.append(
                    Trace(Outcome.LOOP, path, space, "repeated forwarding state", uncertain)
                )
                continue
            node = index.get(location)
            if node is None:
                traces.append(
                    Trace(Outcome.UNKNOWN, path, space, "missing node semantics", uncertain)
                )
                continue
            remaining: tuple[Branch, ...] = ((space, uncertain),)
            for rule in node.rules:
                next_remaining: list[Branch] = []
                for branch, reasons in remaining:
                    # Bound predicate/transfer work and queued fan-out as well
                    # as visited nodes; one enormous rule cannot bypass the cap.
                    partitions += (
                        max(1, len(rule.matches)) + len(rule.writes) + len(rule.destinations)
                    )
                    if partitions > max_states:
                        return (
                            *traces,
                            Trace(
                                Outcome.UNKNOWN,
                                (query.entry,),
                                query.space,
                                "partition budget exhausted",
                            ),
                        )
                    matched, unmatched = partition(branch, rule.matches)
                    next_remaining.extend((s, (*reasons, *u)) for s, u in unmatched)
                    if matched is None:
                        continue
                    selected, missing = matched
                    branch_uncertainty = (*reasons, *missing)
                    if rule.opaque:
                        traces.append(
                            Trace(
                                Outcome.UNKNOWN,
                                path,
                                selected,
                                rule.reason or "opaque rule semantics",
                                branch_uncertainty,
                            )
                        )
                        # Unknown enforced precedence includes the possibility of fall-through.
                        next_remaining.append(
                            (selected, (*branch_uncertainty, "opaque earlier rule"))
                        )
                        continue
                    rewritten = selected
                    for field, value in rule.writes:
                        domain = selected.get(value.field) if isinstance(value, Copy) else value
                        if domain is None:
                            branch_uncertainty = (*branch_uncertainty, f"missing copy: {field}")
                            break
                        if (
                            isinstance(value, Copy)
                            and value.field != field
                            and not domain.singleton
                        ):
                            # This Cartesian representation cannot retain x=y
                            # after copying a range. Its overapproximation is
                            # diagnostic, never an exact counterexample/proof.
                            branch_uncertainty = (
                                *branch_uncertainty,
                                f"unmodeled copy correlation: {value.field} -> {field}",
                            )
                        rewritten = rewritten.with_field(field, domain)
                    else:
                        if rule.outcome is not None:
                            traces.append(
                                Trace(
                                    rule.outcome, path, rewritten, rule.reason, branch_uncertainty
                                )
                            )
                        elif rule.destinations:
                            for destination in rule.destinations:
                                pending.append(
                                    (
                                        destination,
                                        rewritten,
                                        path,
                                        ancestors | {state},
                                        branch_uncertainty,
                                    )
                                )
                        else:
                            traces.append(
                                Trace(
                                    Outcome.UNKNOWN,
                                    path,
                                    rewritten,
                                    "rule has no terminal or next step",
                                    branch_uncertainty,
                                )
                            )
                        continue
                    traces.append(
                        Trace(
                            Outcome.UNKNOWN,
                            path,
                            selected,
                            "transfer needs an absent source field",
                            branch_uncertainty,
                        )
                    )
                remaining = tuple(next_remaining)
                if not remaining:
                    break
            traces.extend(
                Trace(Outcome.DROPPED, path, s, "no matching rule", u) for s, u in remaining
            )
        return tuple(traces)

    def evaluate(self, query: Query, *, max_states: int = 10_000) -> Evaluation:
        traces = self.explore(query, max_states=max_states)
        unknown = False
        violation = False
        for trace in traces:
            if trace.uncertainties or trace.outcome is Outcome.UNKNOWN:
                unknown = True
                continue
            reached = trace.outcome is Outcome.DELIVERED and trace.path[-1] == query.target
            if reached != query.must_reach:
                violation = True
        status = (
            Status.VIOLATED
            if violation
            else (Status.UNKNOWN if unknown or not traces else Status.SATISFIED)
        )
        return Evaluation(query.id, status, traces)
