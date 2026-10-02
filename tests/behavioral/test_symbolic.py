from itertools import product

import pytest

from digital_twin.behavioral import (
    Copy,
    Domain,
    Match,
    Node,
    Outcome,
    Program,
    Query,
    Rule,
    Space,
    Status,
)


def terminal(name="done"):
    return Node(name, (Rule(outcome=Outcome.DELIVERED),))


def query(space, *, must_reach=True):
    return Query("q", "start", space, "done", must_reach)


def members(domain):
    return {i for i in range(-10, 11) if not Domain.value(i).intersect(domain).empty}


def test_interval_algebra_matches_concrete_enumeration():
    domains = [Domain.range(a, b) for a in range(-3, 4) for b in range(a, 4)]
    domains += [Domain("integer", ((-4, -2), (0, 2), (4, 4)))]
    for a, b in product(domains, repeat=2):
        assert members(a.intersect(b)) == members(a) & members(b)
        assert members(a.subtract(b)) == members(a) - members(b)
    assert Domain("integer", ((3, 4), (1, 2), (4, 6))) == Domain.range(1, 6)


def test_finite_cofinite_and_boolean_types():
    universe = {"a", "b", "c", "other"}
    domains = [
        Domain("string", symbols=("a", "b")),
        Domain.universe("string"),
        Domain("string", symbols=("a",), excluded=True),
    ]

    def values(domain):
        return {v for v in universe if not Domain.value(v).intersect(domain).empty}

    for a, b in product(domains, repeat=2):
        assert values(a.intersect(b)) == values(a) & values(b)
        assert values(a.subtract(b)) == values(a) - values(b)
    assert Domain.universe("boolean").symbols == (False, True)
    assert Domain.value(True).complement() == Domain.value(False)
    with pytest.raises(ValueError, match="kinds differ"):
        Domain.value(True).intersect(Domain.value(1))
    with pytest.raises(ValueError, match="typed interval"):
        Domain.range(True, 2)


def test_whole_port_class_finds_unsampled_boundary_and_disjoint_partition():
    program = Program(
        (
            Node(
                "start",
                (
                    Rule(
                        matches=(Match("dst_port", Domain.value(48731)),), outcome=Outcome.DROPPED
                    ),
                    Rule(destinations=("done",)),
                ),
            ),
            terminal(),
        )
    )
    evaluation = program.evaluate(query(Space.of(dst_port=Domain.range(1, 65535))))
    assert evaluation.status is Status.VIOLATED
    assert (
        next(
            t.space.witness()["dst_port"] for t in evaluation.traces if t.outcome is Outcome.DROPPED
        )
        == 48731
    )
    represented = [t.space.get("dst_port") for t in evaluation.traces]
    for port in (1, 48730, 48731, 48732, 65535):
        branches = [d for d in represented if not d.intersect(Domain.value(port)).empty]
        assert len(branches) == 1
        concrete = program.evaluate(query(Space.of(dst_port=port)))
        assert concrete.status is (Status.VIOLATED if port == 48731 else Status.SATISFIED)


def test_missing_fact_remains_correlated_across_repeated_matches():
    program = Program(
        (
            Node(
                "start",
                (
                    Rule(
                        matches=(Match("identity", Domain.value("employee")),),
                        destinations=("employee",),
                    ),
                    Rule(destinations=("done",)),
                ),
            ),
            Node(
                "employee",
                (
                    Rule(
                        matches=(Match("identity", Domain.value("guest")),), outcome=Outcome.DROPPED
                    ),
                    Rule(destinations=("done",)),
                ),
            ),
            terminal(),
        )
    )
    result = program.evaluate(query(Space.of(dst_port=443)))
    assert result.status is Status.UNKNOWN
    assert all(t.outcome is Outcome.DELIVERED for t in result.traces)
    assert all(t.uncertainties for t in result.traces)


def test_mistyped_boolean_and_integer_cannot_be_coerced():
    program = Program(
        (
            Node(
                "start",
                (Rule(matches=(Match("enabled", Domain.value(True)),), destinations=("done",)),),
            ),
            terminal(),
        )
    )
    assert program.evaluate(query(Space.of(enabled=1))).status is Status.UNKNOWN


def test_dual_stack_families_are_disjoint_known_facts():
    program = Program(
        (
            Node(
                "start",
                (
                    Rule(
                        matches=(Match("dst", Domain.ip("2001:db8::/32")),), outcome=Outcome.DROPPED
                    ),
                    Rule(
                        matches=(Match("dst", Domain.ip("192.0.2.0/24")),), destinations=("done",)
                    ),
                ),
            ),
            terminal(),
        )
    )
    assert program.evaluate(query(Space.of(dst=Domain.ip("192.0.2.3")))).status is Status.SATISFIED


def test_opaque_rule_cannot_prove_reachability_or_isolation():
    program = Program(
        (
            Node(
                "start",
                (
                    Rule(opaque=True, reason="new firewall match"),
                    Rule(destinations=("done",)),
                ),
            ),
            terminal(),
        )
    )
    for must_reach in (True, False):
        assert program.evaluate(query(Space.of(dst_port=443), must_reach=must_reach)).status is (
            Status.UNKNOWN
        )


def test_literal_copies_are_simultaneous_and_range_correlation_is_unknown():
    program = Program(
        (
            Node(
                "start",
                (Rule(writes=(("x", Copy("y")), ("y", Copy("x"))), destinations=("done",)),),
            ),
            terminal(),
        )
    )
    result = program.evaluate(query(Space.of(x=1, y=2)))
    assert result.status is Status.SATISFIED
    assert result.traces[0].space.witness() == {"x": 2, "y": 1}
    result = program.evaluate(query(Space.of(x=Domain.range(1, 2), y=2)))
    assert result.status is Status.UNKNOWN
    assert "copy correlation" in " ".join(result.traces[0].uncertainties)
    assert program.evaluate(query(Space.of(x=1))).status is Status.UNKNOWN


def test_loop_missing_destination_and_exploration_budget():
    loop = Program((Node("start", (Rule(destinations=("start",)),)),))
    assert loop.evaluate(query(Space.of(dst_port=443))).traces[0].outcome is Outcome.LOOP
    assert loop.evaluate(query(Space.of(dst_port=443))).status is Status.VIOLATED
    missing = Program((Node("start", (Rule(destinations=("gone",)),)),))
    assert missing.evaluate(query(Space.of(dst_port=443))).status is Status.UNKNOWN
    program = Program((Node("start", (Rule(destinations=("done",)),)), terminal()))
    assert program.evaluate(query(Space.of(dst_port=443)), max_states=1).status is Status.UNKNOWN
    with pytest.raises(ValueError):
        program.evaluate(query(Space.of(dst_port=443)), max_states=True)


def test_rule_partition_budget_bounds_expansion():
    rules = tuple(
        Rule(matches=(Match("dst_port", Domain.value(p)),), outcome=Outcome.DELIVERED)
        for p in range(10)
    )
    program = Program((Node("start", rules),))
    result = program.evaluate(query(Space.of(dst_port=Domain.range(0, 20))), max_states=3)
    assert any(t.reason == "partition budget exhausted" for t in result.traces)
    assert len(result.traces) <= 4


def test_large_single_rule_fanout_cannot_bypass_work_budget():
    program = Program((Node("start", (Rule(destinations=tuple(str(i) for i in range(1000))),)),))
    result = program.evaluate(query(Space.of(dst_port=443)), max_states=10)
    assert result.status is Status.UNKNOWN
    assert len(result.traces) == 1
    assert result.traces[0].reason == "partition budget exhausted"


def test_isolation_quantifies_over_every_possible_forwarding_branch():
    program = Program(
        (
            Node("start", (Rule(destinations=("done", "elsewhere")),)),
            terminal(),
            terminal("elsewhere"),
        )
    )
    assert (
        program.evaluate(query(Space.of(dst_port=443), must_reach=False)).status is Status.VIOLATED
    )
