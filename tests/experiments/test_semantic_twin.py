"""Verify composition and conservative treatment of uncertainty in the experiment."""

from dataclasses import replace

import pytest
from experiments.full_stack_semantics import nac, profile, program, run_examples, switch
from experiments.semantic_twin import (
    Disposition,
    Node,
    Result,
    Rule,
    evaluate,
    replace_nodes,
)


def test_complete_batch_compensates_individually_breaking_changes_without_mutating_baseline():
    base = program()
    assert evaluate(base, "wlan:a", profile("a"), "payroll").result is Result.SATISFIED
    for changes in ((nac(20),), (switch("a", (20,)),)):
        changed = replace_nodes(base, changes)
        assert evaluate(changed, "wlan:a", profile("a"), "payroll").result is Result.VIOLATED
    changed = replace_nodes(base, (nac(20), switch("a", (20,)), switch("b", (20,))))
    for entry, site, attachment in (
        ("wlan:a", "a", "wifi"),
        ("shared-nac", "a", "wired"),
        ("wlan:b", "b", "wifi"),
    ):
        assert (
            evaluate(changed, entry, profile(site, attachment), "payroll").result
            is Result.SATISFIED
        )
    assert base == program()


def test_partial_batch_affects_unchanged_remote_site():
    changed = replace_nodes(program(), (nac(20), switch("a", (20,))))
    assert evaluate(changed, "wlan:a", profile("a"), "payroll").result is Result.SATISFIED
    result = evaluate(changed, "wlan:b", profile("b"), "payroll")
    assert result.result is Result.VIOLATED
    assert result.traces[0].path[-1] == "switch:b"


def test_unknown_earlier_policy_cannot_fall_through_to_a_definite_allow():
    nodes = (
        Node(
            "policy",
            (
                Rule(matches=(("posture", "bad"),), terminal=Disposition.DROPPED),
                Rule(next_nodes=("service",)),
            ),
        ),
        Node("service", (Rule(terminal=Disposition.REACHED),)),
    )
    result = evaluate(nodes, "policy", {}, "service")
    assert result.result is Result.UNKNOWN
    assert {t.disposition for t in result.traces} == {Disposition.REACHED, Disposition.DROPPED}


def test_unknown_alternative_does_not_certify_delivery():
    nodes = (
        Node("entry", (Rule(next_nodes=("service", "unmodeled")),)),
        Node("service", (Rule(terminal=Disposition.REACHED),)),
    )
    assert evaluate(nodes, "entry", {}, "service").result is Result.UNKNOWN


def test_mistyped_capability_cannot_prove_compatibility():
    result = evaluate(program(), "wlan:a", profile("a") | {"supports_auth": 1}, "payroll")
    assert result.result is Result.UNKNOWN
    assert any(t.uncertainties for t in result.traces)


def test_one_known_bad_forwarding_branch_violates_delivery_even_with_other_unknowns():
    nodes = (Node("entry", (Rule(next_nodes=("drop", "missing")),)), Node("drop", ()))
    assert evaluate(nodes, "entry", {}, "service").result is Result.VIOLATED


def test_same_evaluator_detects_isolation_breach_and_loop():
    base = program()
    assert (
        evaluate(base, "wlan:a", profile("a"), "payroll", must_reach=False).result
        is Result.VIOLATED
    )
    closed = replace_nodes(base, (Node("hub-firewall", ()),))
    assert (
        evaluate(closed, "wlan:a", profile("a"), "payroll", must_reach=False).result
        is Result.SATISFIED
    )
    loop = (Node("loop", (Rule(next_nodes=("loop",)),)),)
    result = evaluate(loop, "loop", {}, "payroll")
    assert result.result is Result.VIOLATED
    assert result.traces[0].disposition is Disposition.LOOP


def test_exhausted_exploration_never_returns_satisfaction():
    assert (
        evaluate(program(), "wlan:a", profile("a"), "payroll", max_states=1).result
        is Result.UNKNOWN
    )


def test_duplicate_and_missing_batch_targets_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        replace_nodes(program(), (nac(20), nac(30)))
    with pytest.raises(ValueError, match="unknown"):
        replace_nodes(program(), (replace(nac(20), id="missing"),))


def test_all_examples_keep_wireless_and_wired_paths_distinct():
    examples = {row["scenario"]: row["results"] for row in run_examples()}
    tunneled = examples["tunneled_wlan_bypasses_local_client_vlan"]
    assert tunneled["a:wifi"]["result"] is Result.SATISFIED
    assert tunneled["a:wired"]["result"] is Result.VIOLATED
    edge = examples["shared_edge_egress_breaks_remote_wifi"]
    assert edge["a:wifi"]["result"] is Result.VIOLATED
    assert edge["b:wifi"]["result"] is Result.VIOLATED
    assert edge["a:wired"]["result"] is Result.SATISFIED
    unknown = examples["missing_wireless_client_capability"]
    assert unknown["a:wifi"]["result"] is Result.UNKNOWN
    assert unknown["a:wired"]["result"] is Result.SATISFIED
