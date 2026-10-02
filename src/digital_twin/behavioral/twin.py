"""Importable comparison service; compilation and collection remain separate seams."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import combinations
from typing import Literal, Protocol

from .coverage import Coverage, Gap
from .program import Evaluation, Program, Query, Status
from .snapshot import Batch, ObjectKey, Snapshot


@dataclass(frozen=True)
class Compilation:
    program: Program
    coverage: Coverage


class Compiler(Protocol):
    def compile(self, snapshot: Snapshot) -> Compilation: ...


@dataclass(frozen=True)
class Assessment:
    snapshot_revision: str
    evaluations: tuple[Evaluation, ...]
    gaps: tuple[Gap, ...]

    @property
    def proven_violations(self) -> tuple[Evaluation, ...]:
        return tuple(e for e in self.evaluations if e.status is Status.VIOLATED)

    @property
    def unresolved_obligations(self) -> tuple[Evaluation, ...]:
        return tuple(e for e in self.evaluations if e.status is Status.UNKNOWN)


@dataclass(frozen=True)
class Stage:
    operation_indices: tuple[int, ...]
    assessment: Assessment


@dataclass(frozen=True)
class Comparison:
    baseline: Assessment
    proposed: Assessment
    changed_queries: tuple[str, ...]
    affected_objects: tuple[ObjectKey, ...]
    stages: tuple[Stage, ...] = ()
    deployment_gaps: tuple[str, ...] = ()
    # Baseline configuration is the rollback target. No runtime-state rollback claim.
    rollback_configuration_revision: str = ""


@dataclass(frozen=True)
class Twin:
    snapshot: Snapshot
    compiler: Compiler
    compilation: Compilation

    @classmethod
    def compile(cls, snapshot: Snapshot, compiler: Compiler) -> Twin:
        return cls(snapshot, compiler, compiler.compile(snapshot))

    def _assess(
        self,
        snapshot: Snapshot,
        compilation: Compilation,
        queries: tuple[Query, ...],
        max_states: int,
        at: datetime | None = None,
        max_age: timedelta | None = None,
    ) -> Assessment:
        # Start conservatively with complete captured configuration inventory.
        # Per-property exclusions need a future non-interference contract; a
        # fragment compiler cannot silently omit an unchanged source object.
        keys = tuple(record.key for record in snapshot.records)
        gaps = compilation.coverage.inspect(snapshot, keys, at=at, max_age=max_age)
        unknown_dependencies = set(
            key for query in queries for key in compilation.program.dependencies(query.entry)
        ) - set(keys)
        gaps = (
            *gaps,
            *(
                Gap("missing_object", key, (), "program source is not captured")
                for key in sorted(unknown_dependencies)
            ),
        )
        if not keys:
            gaps = (*gaps, Gap("empty_inventory", None, (), "no captured source configuration"))
        evaluations = tuple(compilation.program.evaluate(q, max_states=max_states) for q in queries)
        if gaps:
            # Traces remain diagnostic candidates; unsupported behavior cannot
            # establish either a positive assurance or a proven violation.
            evaluations = tuple(replace(e, status=Status.UNKNOWN) for e in evaluations)
        return Assessment(snapshot.revision, evaluations, gaps)

    def simulate(
        self,
        batch: Batch,
        *,
        queries: tuple[Query, ...],
        max_states: int = 10_000,
        rollout: Literal["none", "prefixes", "mixed"] = "none",
        max_stages: int = 64,
        at: datetime | None = None,
        max_age: timedelta | None = None,
    ) -> Comparison:
        if not queries or len({q.id for q in queries}) != len(queries):
            raise ValueError("simulation requires nonempty, uniquely identified obligations")
        if rollout not in ("none", "prefixes", "mixed"):
            raise ValueError("unsupported rollout mode")
        if type(max_stages) is not int or max_stages < 1:
            raise ValueError("max_stages must be a positive integer")
        proposed = batch.apply(self.snapshot)
        compiled = self.compiler.compile(proposed)
        baseline = self._assess(self.snapshot, self.compilation, queries, max_states, at, max_age)
        final = self._assess(proposed, compiled, queries, max_states, at, max_age)
        changed = tuple(
            before.query_id
            for before, after in zip(
                baseline.evaluations,
                final.evaluations,
                strict=True,
            )
            if before != after
        )
        sources = {op.key for op in batch.operations}
        # Baseline AND proposed dependencies are retained, including removed edges.
        for query in queries:
            sources.update(self.compilation.program.dependencies(query.entry))
            sources.update(compiled.program.dependencies(query.entry))
        seeds = tuple(sources)
        sources.update(self.compilation.coverage.dependency_closure(seeds))
        sources.update(compiled.coverage.dependency_closure(seeds))
        stages: list[Stage] = []
        deployment_gaps: list[str] = []
        count = len(batch.operations)
        selections: Iterator[tuple[int, ...]]
        if rollout == "prefixes":
            selections = (tuple(range(i)) for i in range(1, count))
            deployment_gaps.append("ordered prefixes do not cover asynchronous mixed activation")
        elif rollout == "mixed" and len({op.key for op in batch.operations}) != count:
            selections = iter(())
            deployment_gaps.append("mixed activation of repeated-object operations is unsupported")
        elif rollout == "mixed":
            selections = (
                indices for size in range(1, count) for indices in combinations(range(count), size)
            )
            deployment_gaps.append("configuration subsets exclude retained runtime/session state")
        else:
            selections = iter(())
        for indices in selections:
            if len(stages) >= max_stages:
                deployment_gaps.append("deployment exploration budget exhausted")
                break
            stage_snapshot = Batch(
                self.snapshot.revision,
                tuple(batch.operations[i] for i in indices),
            ).apply(self.snapshot)
            assessment = self._assess(
                stage_snapshot,
                self.compiler.compile(stage_snapshot),
                queries,
                max_states,
                at,
                max_age,
            )
            stages.append(Stage(indices, assessment))
        return Comparison(
            baseline,
            final,
            changed,
            tuple(sorted(sources)),
            tuple(stages),
            tuple(deployment_gaps),
            self.snapshot.revision,
        )
