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
M_BAD = DhcpScope(provider="site", network="m", vlan=40, gateway="10.4.0.254",
                  network_gateway="10.4.0.1")
M_OK = DhcpScope(provider="site", network="m", vlan=40, gateway="10.4.0.1/24",
                 network_gateway="10.4.0.1")


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


def test_v4_and_v6_scopes_never_overlap():
    # int(IPv4Address("0.0.0.255")) == int(IPv6Address("::ff")) — bare-int
    # comparison across families must not produce a spurious overlap
    v6 = DhcpScope(provider="site", network="b", ip_start="::aa", ip_end="::ff")
    v4 = DhcpScope(provider="site", network="a", ip_start="0.0.0.170", ip_end="0.0.0.255")
    r = _run(_ir(), _ir(v4, v6))
    assert not [f for f in r.findings if f.code.endswith("overlap")]


def test_mixed_family_range_is_unevaluable_not_compared():
    weird = DhcpScope(provider="site", network="w", ip_start="10.0.0.1", ip_end="::ff")
    r = _run(_ir(), _ir(weird, A))
    assert not [f for f in r.findings if f.code.endswith("overlap")]
    assert r.coverage.state is CoverageState.PARTIAL  # unevaluable + scope delta


def test_v6_gateway_on_v4_subnet_is_a_violation():
    # decision: a value whose family differs from the subnet's lies outside it
    s = DhcpScope(provider="site", network="a", subnet="10.0.0.0/24", gateway="::1")
    r = _run(_ir(), _ir(s))
    f = next(x for x in r.findings if x.code.endswith("out_of_subnet"))
    assert "gateway" in " ".join(f.evidence["violations"])


def test_inverted_range_is_unevaluable_not_normalized():
    # sibling idiom: anomalous = unevaluable, never silently swapped
    inv = DhcpScope(provider="site", network="i", ip_start="10.0.0.99", ip_end="10.0.0.10")
    r = _run(_ir(), _ir(inv, A))
    assert not [f for f in r.findings if f.code.endswith("overlap")]
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("site:i" in n for n in r.coverage.notes)


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


def test_introduced_gateway_mismatch_is_warning():
    r = _run(_ir(), _ir(M_BAD))
    f = next(x for x in r.findings if x.code.endswith("gateway_mismatch"))
    assert f.severity is Severity.WARNING
    assert f.evidence["handed"] == "10.4.0.254"
    assert f.evidence["declared"] == "10.4.0.1"


def test_prefix_equal_gateways_do_not_mismatch():
    r = _run(_ir(), _ir(M_OK))
    assert not [f for f in r.findings if f.code.endswith("gateway_mismatch")]


def test_preexisting_mismatch_is_info_and_any_value_change_forfeits():
    r = _run(_ir(M_BAD), _ir(M_BAD))
    f = next(x for x in r.findings if x.code.endswith("gateway_mismatch"))
    assert f.severity is Severity.INFO
    moved = DhcpScope(provider="site", network="m", vlan=40,
                      gateway="10.4.0.253", network_gateway="10.4.0.1")
    r2 = _run(_ir(M_BAD), _ir(moved))
    f2 = next(x for x in r2.findings if x.code.endswith("gateway_mismatch"))
    assert f2.severity is Severity.WARNING


def test_missing_either_side_is_silent():
    a = DhcpScope(provider="site", network="m", gateway="10.4.0.254")
    b = DhcpScope(provider="site", network="n", network_gateway="10.4.0.1")
    r = _run(_ir(), _ir(a, b))
    assert not [f for f in r.findings if f.code.endswith("gateway_mismatch")]


def test_network_gateway_unresolved_touched_scope_notes_partial():
    s = DhcpScope(provider="site", network="m", gateway="10.4.0.254",
                  network_gateway=None, network_gateway_unresolved=True)
    r = _run(_ir(), _ir(s))
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("site:m" in n and "gateway" in n.lower() for n in r.coverage.notes)


def test_unparseable_present_values_abstain_with_note():
    s = DhcpScope(provider="site", network="m", gateway="bogus",
                  network_gateway="10.4.0.1")
    r = _run(_ir(), _ir(s))
    assert not [f for f in r.findings if f.code.endswith("gateway_mismatch")]
    assert r.coverage.state is CoverageState.PARTIAL


# --- caused_by attribution tests ---


def test_overlap_conclusion_caused_by_names_added_scope():
    # B_OVERLAP (site:b) is ADDED in proposed → it IS in the delta.
    # The overlap WARNING's caused_by must name at least one dhcp_scope cause.
    r = _run(_ir(A), _ir(A, B_OVERLAP))
    f = next(x for x in r.findings if x.code.endswith("overlap") and x.severity is Severity.WARNING)
    assert len(f.caused_by) >= 1
    assert all(c.ref.kind == "dhcp_scope" for c in f.caused_by)
    ids = {c.ref.id for c in f.caused_by}
    assert "site:b" in ids  # the newly added scope is the delta entity


def test_overlap_preexisting_info_finding_has_empty_caused_by():
    # Same overlap on both sides → INFO → caused_by must be ().
    r = _run(_ir(A, B_OVERLAP), _ir(A, B_OVERLAP))
    f = next(x for x in r.findings if x.code.endswith("overlap") and x.severity is Severity.INFO)
    assert f.caused_by == ()


def test_out_of_subnet_conclusion_caused_by_names_modified_scope():
    # scope site:a changes from A (no subnet) to bad (has subnet + bad gateway)
    # → site:a is MODIFIED in the delta. The WARNING finding's caused_by names it.
    bad = DhcpScope(provider="site", network="a", vlan=10, subnet="10.0.0.0/24",
                    ip_start="10.0.0.10", ip_end="10.0.0.99", gateway="10.9.9.1")
    r = _run(_ir(A), _ir(bad))
    f = next(
        x for x in r.findings
        if x.code.endswith("out_of_subnet") and x.severity is Severity.WARNING
    )
    assert len(f.caused_by) == 1
    assert f.caused_by[0].ref.kind == "dhcp_scope"
    assert f.caused_by[0].ref.id == "site:a"


def test_out_of_subnet_preexisting_info_finding_has_empty_caused_by():
    # Same out-of-subnet violation on both sides → INFO → caused_by must be ().
    bad = DhcpScope(provider="site", network="a", vlan=10, subnet="10.0.0.0/24",
                    ip_start="10.0.0.10", ip_end="10.0.0.99", gateway="10.9.9.1")
    r = _run(_ir(bad), _ir(bad))
    f = next(
        x for x in r.findings
        if x.code.endswith("out_of_subnet") and x.severity is Severity.INFO
    )
    assert f.caused_by == ()


def test_gateway_mismatch_conclusion_caused_by_names_delta_scope():
    # M_BAD is ADDED to proposed (not in baseline) → it IS in the delta.
    # The gateway_mismatch WARNING's caused_by must name the dhcp_scope.
    r = _run(_ir(), _ir(M_BAD))
    f = next(
        x for x in r.findings
        if x.code.endswith("gateway_mismatch") and x.severity is Severity.WARNING
    )
    assert len(f.caused_by) == 1
    assert f.caused_by[0].ref.kind == "dhcp_scope"
    assert f.caused_by[0].ref.id == "site:m"


def test_gateway_mismatch_preexisting_info_finding_has_empty_caused_by():
    # Same mismatch on both sides (preexisting=True) → INFO → caused_by must be ().
    r = _run(_ir(M_BAD), _ir(M_BAD))
    f = next(
        x for x in r.findings
        if x.code.endswith("gateway_mismatch") and x.severity is Severity.INFO
    )
    assert f.caused_by == ()
