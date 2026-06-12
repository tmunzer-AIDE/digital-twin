"""wired.dhcp.scope_lint (GS25): overlapping scope ranges + scope facts outside
the owning network's subnet. WARNING/REVIEW tier (MVP). Violation-specific
parity: INFO only when the SAME violation existed in baseline with the SAME
values — touching the hazard forfeits demotion (native-mismatch precedent)."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.scope_lint import DhcpScopeLintCheck
from digital_twin.contracts import Severity
from digital_twin.ir import DhcpScope, IRBuilder, IRCapability, diff_ir
from tests.factories import sw


def _ir(*scopes):
    b = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2)
    for s in scopes:
        b.add_dhcp_scope(s)
    return b.build()


def _run(base, prop):
    return DhcpScopeLintCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


A = DhcpScope(provider="site", network="a", vlan=10, ip_start="10.0.0.10", ip_end="10.0.0.99")
B_CLEAR = DhcpScope(provider="site", network="b", vlan=20, ip_start="10.0.1.10", ip_end="10.0.1.99")
B_OVERLAP = DhcpScope(
    provider="site", network="b", vlan=20, ip_start="10.0.0.50", ip_end="10.0.1.10"
)


def test_introduced_overlap_is_warning():
    r = _run(_ir(A), _ir(A, B_OVERLAP))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.dhcp.scope_lint.overlap" and f.severity is Severity.WARNING
    assert sorted(f.evidence["scopes"]) == ["site:a", "site:b"]


def test_preexisting_unchanged_overlap_is_info_only():
    r = _run(_ir(A, B_OVERLAP), _ir(A, B_OVERLAP))
    assert r.status is Status.PASS
    assert [f.severity for f in r.findings] == [Severity.INFO]


def test_altered_still_overlapping_range_forfeits_demotion():
    # native-mismatch precedent: editing the hazard = introduced
    moved = DhcpScope(provider="site", network="b", vlan=20,
                      ip_start="10.0.0.60", ip_end="10.0.1.10")
    r = _run(_ir(A, B_OVERLAP), _ir(A, moved))
    assert r.findings[0].severity is Severity.WARNING


def test_unparseable_range_abstains_with_partial_when_delta_touches_scopes():
    from digital_twin.ir.entities import Device, DeviceRole

    blind = DhcpScope(provider="GW", network="x", ip_start=None, ip_end=None)
    base = _ir(A)
    b = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2)
    b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
    for s in (A, B_CLEAR, blind):
        b.add_dhcp_scope(s)
    prop = b.build()
    r = _run(base, prop)  # delta adds scopes -> the blind row is relevant
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("GW:x" in n for n in r.coverage.notes)


def test_unchanged_blind_scope_with_untouched_delta_stays_complete():
    # gateway_gap lesson: INFO/irrelevance never drags PARTIAL — if no scope
    # changed and nothing was concluded, the unevaluable row adds no note
    blind = DhcpScope(provider="site", network="x")
    r = _run(_ir(A, blind), _ir(A, blind))
    assert r.coverage.state is CoverageState.COMPLETE


def test_out_of_subnet_gateway_is_warning_and_same_violation_demotes():
    bad = DhcpScope(provider="site", network="a", vlan=10, subnet="10.0.0.0/24",
                    ip_start="10.0.0.10", ip_end="10.0.0.99", gateway="10.9.9.1")
    r = _run(_ir(A), _ir(bad))
    f = next(x for x in r.findings if x.code == "wired.dhcp.scope_lint.out_of_subnet")
    assert f.severity is Severity.WARNING
    r2 = _run(_ir(bad), _ir(bad))
    f2 = next(x for x in r2.findings if x.code == "wired.dhcp.scope_lint.out_of_subnet")
    assert f2.severity is Severity.INFO


def test_changed_bad_value_is_still_warning():
    bad1 = DhcpScope(provider="site", network="a", subnet="10.0.0.0/24", gateway="10.9.9.1")
    bad2 = DhcpScope(provider="site", network="a", subnet="10.0.0.0/24", gateway="10.8.8.1")
    r = _run(_ir(bad1), _ir(bad2))
    f = next(x for x in r.findings if x.code == "wired.dhcp.scope_lint.out_of_subnet")
    assert f.severity is Severity.WARNING


def test_no_subnet_intent_is_not_a_violation():
    s = DhcpScope(provider="site", network="a", gateway="10.9.9.1", subnet=None)
    r = _run(_ir(), _ir(s))
    assert not [f for f in r.findings if f.code.endswith("out_of_subnet")]


def test_touched_scope_with_unresolved_subnet_abstains_partial():
    # spec: subnet_unresolved (intent exists but unreadable/unknowable) ->
    # the scope is SKIPPED for .out_of_subnet and that silence is VISIBLE
    # when the delta touches scopes. Plain subnet=None without the flag is
    # "no intent" — nothing to verify, stays COMPLETE (next test).
    s = DhcpScope(provider="site", network="a", vlan=10, ip_start="10.0.0.10",
                  ip_end="10.0.0.99", subnet=None, subnet_unresolved=True)
    r = _run(_ir(), _ir(s))
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("subnet" in n and "site:a" in n for n in r.coverage.notes)


def test_no_subnet_intent_stays_complete():
    s = DhcpScope(provider="site", network="a", vlan=10,
                  ip_start="10.0.0.10", ip_end="10.0.0.99", subnet=None)
    r = _run(_ir(), _ir(s))
    assert r.coverage.state is CoverageState.COMPLETE


def test_unchanged_blind_scope_does_not_taint_unrelated_scope_edit():
    # review P2 r2: subnet blindness is per-scope — an UNCHANGED unresolved
    # gateway scope elsewhere must not PARTIAL-floor an unrelated, fully
    # readable scope addition (the blind subnet can only hide a violation of
    # ITSELF, and it was not touched)
    from digital_twin.ir.entities import Device, DeviceRole

    blind = DhcpScope(provider="GW", network="x", ip_start="10.5.0.10",
                      ip_end="10.5.0.99", subnet=None, subnet_unresolved=True)

    def ir_with(*extra):
        b = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2)
        b.add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
        for s in (blind, *extra):
            b.add_dhcp_scope(s)
        return b.build()

    r = _run(ir_with(), ir_with(B_CLEAR))
    assert r.coverage.state is CoverageState.COMPLETE
