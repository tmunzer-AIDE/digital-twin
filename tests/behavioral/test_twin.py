from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from digital_twin.behavioral import (
    Action,
    Batch,
    Compilation,
    Coverage,
    InputWindow,
    Node,
    ObjectKey,
    Operation,
    Outcome,
    Program,
    Rule,
    Status,
    Twin,
)
from digital_twin.behavioral.demo import ReferenceCompiler, fixture, scenario


def key(kind, site=""):
    return ObjectKey("demo", kind, "shared" if kind == "admission" else kind, site)


def statuses(assessment):
    return tuple(e.status for e in assessment.evaluations)


def test_wireless_and_wired_batch_final_is_working_but_both_mixed_stages_break_site_a():
    result = scenario()
    assert statuses(result.baseline) == (Status.SATISFIED, Status.SATISFIED)
    assert statuses(result.proposed) == (Status.SATISFIED, Status.SATISFIED)
    assert {stage.operation_indices for stage in result.stages} == {(0,), (1,)}
    assert all(statuses(s.assessment) == (Status.VIOLATED, Status.SATISFIED) for s in result.stages)
    assert all(s.assessment.proven_violations for s in result.stages)
    assert result.deployment_gaps  # existing state/activation timing is explicitly excluded
    assert key("wlan", "a") in result.affected_objects
    assert key("admission") in result.affected_objects
    assert result.rollback_configuration_revision == result.baseline.snapshot_revision


def test_shared_org_change_affects_both_sites_and_baseline_is_immutable():
    snapshot, queries = fixture()
    gate = snapshot.get(key("admission"))
    batch = Batch(snapshot.revision, (Operation.update(gate, {"enabled": False}),))
    twin = Twin.compile(snapshot, ReferenceCompiler())
    result = twin.simulate(batch, queries=queries)
    assert statuses(result.proposed) == (Status.VIOLATED, Status.VIOLATED)
    assert result.changed_queries == ("carriage:a", "carriage:b")
    assert snapshot.get(gate.key).body() == {"enabled": True}
    assert twin.snapshot == snapshot
    for site in ("a", "b"):
        assert key("wlan", site) in result.affected_objects
        assert key("ex_port", site) in result.affected_objects


def test_affected_sources_include_receipt_dependencies_from_baseline_and_proposed():
    snapshot, queries = fixture()
    gate = snapshot.get(key("admission"))

    class DependencyCompiler:
        def compile(self, snapshot):
            compilation = ReferenceCompiler().compile(snapshot)
            dependency = key("ex_port", "a" if snapshot.get(gate.key).body()["enabled"] else "b")
            receipts = tuple(
                replace(r, dependencies=(dependency,)) if r.key == gate.key else r
                for r in compilation.coverage.receipts
            )
            # Restrict the graph to a single query path to ensure the other site's
            # source is reached through receipts rather than graph traversal.
            nodes = tuple(n for n in compilation.program.nodes if not n.id.endswith(":b"))
            return Compilation(Program(nodes), Coverage(receipts))

    result = Twin.compile(snapshot, DependencyCompiler()).simulate(
        Batch(snapshot.revision, (Operation.update(gate, {"enabled": False}),)),
        queries=(queries[0],),
    )
    assert key("ex_port", "a") in result.affected_objects
    assert key("ex_port", "b") in result.affected_objects


@pytest.mark.parametrize("extra", [None, {}, [], "future-value"])
def test_unchanged_unknown_fields_block_positive_and_failure_proofs(extra):
    snapshot, queries = fixture()
    wlan = snapshot.get(key("wlan", "a"))
    changed = Batch(snapshot.revision, (Operation.update(wlan, {"new_feature": extra}),)).apply(
        snapshot
    )
    twin = Twin.compile(changed, ReferenceCompiler())
    gate = changed.get(key("admission"))
    result = twin.simulate(
        Batch(changed.revision, (Operation.update(gate, {"enabled": False}),)), queries=queries
    )
    for assessment in (result.baseline, result.proposed):
        assert statuses(assessment) == (Status.UNKNOWN, Status.UNKNOWN)
        assert any(g.key == wlan.key and g.path == ("new_feature",) for g in assessment.gaps)
        assert not assessment.proven_violations
        assert assessment.unresolved_obligations
    assert result.proposed.evaluations[0].traces[0].outcome is Outcome.DROPPED


def test_no_obligations_or_duplicate_query_ids_cannot_produce_a_safe_empty_report():
    snapshot, queries = fixture()
    twin = Twin.compile(snapshot, ReferenceCompiler())
    for obligations in ((), (queries[0], queries[0])):
        with pytest.raises(ValueError, match="obligations"):
            twin.simulate(Batch(snapshot.revision, ()), queries=obligations)


def test_deleted_source_and_missing_graph_dependency_remain_unknown():
    snapshot, queries = fixture()
    port = snapshot.get(key("ex_port", "a"))
    batch = Batch(snapshot.revision, (Operation(port.key, Action.DELETE, port.revision),))
    result = Twin.compile(snapshot, ReferenceCompiler()).simulate(batch, queries=queries)
    assert result.proposed.evaluations[0].status is Status.UNKNOWN

    class MissingSourceCompiler:
        def compile(self, snapshot):
            reference = ReferenceCompiler().compile(snapshot)
            nodes = tuple(
                replace(n, sources=(*n.sources, key("uncaptured"))) if n.id == "admission:a" else n
                for n in reference.program.nodes
            )
            return Compilation(Program(nodes), reference.coverage)

    twin = Twin.compile(snapshot, MissingSourceCompiler())
    result = twin.simulate(Batch(snapshot.revision, ()), queries=queries)
    assert all(e.status is Status.UNKNOWN for e in result.baseline.evaluations)
    assert any(g.code == "missing_object" for g in result.baseline.gaps)


def test_stale_input_is_a_public_simulation_gap():
    snapshot, queries = fixture()
    at = datetime(2026, 10, 1, tzinfo=UTC)
    snapshot = replace(snapshot, inputs=(InputWindow("source", at, at, True),))
    result = Twin.compile(snapshot, ReferenceCompiler()).simulate(
        Batch(snapshot.revision, ()),
        queries=queries,
        at=at + timedelta(minutes=10),
        max_age=timedelta(minutes=1),
    )
    assert statuses(result.proposed) == (Status.UNKNOWN, Status.UNKNOWN)
    assert result.proposed.gaps[0].code == "stale_input"


def test_rollout_budget_is_not_silently_treated_as_exhaustive():
    snapshot, queries = fixture()
    operations = tuple(Operation.update(record, record.body()) for record in snapshot.records)
    result = Twin.compile(snapshot, ReferenceCompiler()).simulate(
        Batch(snapshot.revision, operations),
        queries=queries,
        rollout="mixed",
        max_stages=2,
    )
    assert len(result.stages) == 2
    assert "deployment exploration budget exhausted" in result.deployment_gaps


def test_prefixes_and_repeated_object_mixed_activation_have_explicit_limits():
    snapshot, queries = fixture()
    gate = snapshot.get(key("admission"))
    first = Operation.update(gate, {"enabled": False})
    middle = Batch(snapshot.revision, (first,)).apply(snapshot)
    second = Operation.update(middle.get(gate.key), {"enabled": True})
    batch = Batch(snapshot.revision, (first, second))
    twin = Twin.compile(snapshot, ReferenceCompiler())
    prefix = twin.simulate(batch, queries=queries, rollout="prefixes")
    assert statuses(prefix.stages[0].assessment) == (Status.VIOLATED, Status.VIOLATED)
    mixed = twin.simulate(batch, queries=queries, rollout="mixed")
    assert not mixed.stages
    assert "mixed activation of repeated-object operations is unsupported" in mixed.deployment_gaps


def test_empty_inventory_cannot_certify_unconditional_forwarding():
    snapshot, queries = fixture()

    class EmptyCompiler:
        def compile(self, snapshot):
            return Compilation(
                Program(
                    (
                        Node("admission:a", (Rule(destinations=("carried:a",)),)),
                        Node("carried:a", (Rule(outcome=Outcome.DELIVERED),)),
                    )
                ),
                Coverage(),
            )

    snapshot = replace(snapshot, records=())
    result = Twin.compile(snapshot, EmptyCompiler()).simulate(
        Batch(snapshot.revision, ()), queries=(queries[0],)
    )
    assert statuses(result.proposed) == (Status.UNKNOWN,)
    assert result.proposed.gaps[0].code == "empty_inventory"
