"""wired.l3.gateway_gap (MVP: ROUTE-GW): a ROUTED network (subnet declared)
must have an L3 interface somewhere — switch IRB or gateway ip_config. The
delta removing the only modeled one -> ERROR (UNSAFE at HIGH); newly-declared
routed intent with no modeled interface -> WARNING/MEDIUM (could live on an
unmodeled box); the same gap already in the baseline -> INFO context."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.gateway_gap import GatewayGapCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import irb, sw


def _ir(*, routed=True, with_irb=True, blind_gateway=False):
    b = IRBuilder().add_device(sw("S"))
    if blind_gateway:
        from digital_twin.ir.entities import Device, DeviceRole

        b.add_device(
            Device(id="GW", role=DeviceRole.GATEWAY, site="s1", l3_unmodeled=True)
        )
    b.add_vlan(Vlan(vlan_id=10, name="corp", subnet="198.51.100.0/24" if routed else None))
    if with_irb:
        b.add_l3intf(irb("S", 10, subnet="198.51.100.0/24"))
    # the check's predicate consumes ir.l3intfs: it must demand the L3_EXITS
    # capability, not just WIRED_L2 (review on 9b4dbe7)
    b.with_capability(IRCapability.WIRED_L2)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_requires_the_l3_exits_capability():
    from digital_twin.ir import IRCapability as Cap

    assert GatewayGapCheck().requires() == frozenset({Cap.WIRED_L2, Cap.L3_EXITS})


def test_unmodeled_gateway_namespace_makes_coverage_partial():
    # review on 9b4dbe7: a gateway whose network namespace was not fetched has
    # UNKNOWN L3 interfaces — "no modeled device provides L3" must not claim
    # COMPLETE coverage over it (PARTIAL -> REVIEW floor, standalone-honest)
    from digital_twin.checks.base import CoverageState

    result = _run(
        _ir(routed=False, with_irb=False, blind_gateway=True),
        _ir(routed=True, with_irb=False, blind_gateway=True),
    )
    assert result.coverage.state is CoverageState.PARTIAL
    assert any("unmodeled" in n for n in result.coverage.notes)
    # the finding itself still ships (the uncertainty lives in the coverage)
    assert result.findings[0].code == "wired.l3.gateway_gap.unserved"


def _run(base, prop):
    return GatewayGapCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_removing_the_only_l3_interface_of_a_routed_network_is_unsafe():
    result = _run(_ir(with_irb=True), _ir(with_irb=False))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.removed"
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["vlan"] == 10


def test_newly_routed_network_without_modeled_l3_is_a_warning():
    # the interface could live on an unmodeled box -> MEDIUM, REVIEW not UNSAFE
    result = _run(_ir(routed=False, with_irb=False), _ir(routed=True, with_irb=False))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.unserved"
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM


def test_removed_with_a_blind_gateway_downgrades_to_warning():
    # review on ffd7670 (P1): the invisible replacement may live on the
    # unmodeled gateway — "removed the only modeled L3" cannot be a confident
    # ERROR/HIGH -> UNSAFE there; the claim caps at MEDIUM -> WARNING/REVIEW
    result = _run(
        _ir(with_irb=True, blind_gateway=True), _ir(with_irb=False, blind_gateway=True)
    )
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.removed"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_preexisting_context_alone_does_not_make_coverage_partial():
    # review on ffd7670 (P2): INFO .preexisting is context, excluded from
    # verdict floors by doctrine — it must not drag PARTIAL/REVIEW in via the
    # coverage side door when no real conclusion was emitted
    from digital_twin.checks.base import CoverageState

    result = _run(
        _ir(with_irb=False, blind_gateway=True), _ir(with_irb=False, blind_gateway=True)
    )
    assert result.status is Status.PASS
    assert [f.code for f in result.findings] == ["wired.l3.gateway_gap.preexisting"]
    assert result.coverage.state is CoverageState.COMPLETE
    assert result.coverage.notes == ()


def test_blind_gateway_does_not_taint_a_served_routed_network():
    # review on c804fe5: a routed vlan SERVED by a modeled IRB is a positive
    # conclusion — an unrelated unmodeled gateway must not floor it to
    # PARTIAL/REVIEW; the blind spot only degrades negative-existence claims
    from digital_twin.checks.base import CoverageState

    result = _run(
        _ir(routed=False, with_irb=True, blind_gateway=True),
        _ir(routed=True, with_irb=True, blind_gateway=True),
    )
    assert result.status is Status.PASS and result.findings == ()
    assert result.coverage.state is CoverageState.COMPLETE
    assert result.coverage.notes == ()


def test_preexisting_gap_is_info_context():
    result = _run(_ir(with_irb=False), _ir(with_irb=False))
    assert result.status is Status.PASS
    f = result.findings[0]
    assert f.code == "wired.l3.gateway_gap.preexisting" and f.severity is Severity.INFO


def test_served_routed_network_is_silent():
    assert _run(_ir(), _ir()).findings == ()


def test_unrouted_vlan_never_fires():
    assert _run(_ir(routed=False, with_irb=False), _ir(routed=False, with_irb=False)).findings == ()

# --- .gateway_unowned: when L3 interfaces EXIST on a vlan, the declared
# gateway IP must be OWNED by one of them (GS22-GW)


def _routed_ir(*, gateway="10.0.0.1", gw_unresolved=False, intf_ip="10.0.0.1",
               with_intf=True):
    from digital_twin.ir.entities import L3Intf, L3Role

    b = IRBuilder().add_device(sw("S"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", subnet="10.0.0.0/24",
                    gateway=gateway, gateway_unresolved=gw_unresolved))
    if with_intf:
        b.add_l3intf(L3Intf(device_id="S", role=L3Role.IRB, vlan_id=10,
                            ip=intf_ip))
    b.with_capability(IRCapability.WIRED_L2)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_owned_gateway_is_silent():
    r = _run(_routed_ir(), _routed_ir())
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]


def test_breaking_a_known_owner_is_error():
    # baseline owned (intf ip == declared G); delta moves G away -> the
    # known owner is gone: ERROR at the owning fact's confidence
    r = _run(_routed_ir(gateway="10.0.0.1"), _routed_ir(gateway="10.0.0.9"))
    f = next(x for x in r.findings if x.code.endswith("gateway_unowned"))
    assert f.severity is Severity.ERROR


def test_never_owned_is_warning_medium():
    # interfaces exist but none ever owned G (baseline G already different
    # from the intf and CHANGED by the delta -> introduced, no known owner)
    base = _routed_ir(gateway="10.0.0.8", intf_ip="10.0.0.250")
    prop = _routed_ir(gateway="10.0.0.9", intf_ip="10.0.0.250")
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code.endswith("gateway_unowned"))
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_preexisting_unowned_is_info():
    same = _routed_ir(gateway="10.0.0.9", intf_ip="10.0.0.250")
    r = _run(same, same)
    f = next(x for x in r.findings if x.code.endswith("gateway_unowned"))
    assert f.severity is Severity.INFO


def test_no_declared_gateway_is_silent():
    r = _run(_routed_ir(gateway=None), _routed_ir(gateway=None))
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]


def test_unresolved_gateway_abstains_with_note_when_vlan_touched():
    base = _routed_ir(gateway="10.0.0.1")
    prop = _routed_ir(gateway=None, gw_unresolved=True)  # delta touches vlan
    r = _run(base, prop)
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("10" in n and "gateway" in n.lower() for n in r.coverage.notes)


def test_unresolved_gateway_untouched_stays_complete():
    same = _routed_ir(gateway=None, gw_unresolved=True)
    r = _run(same, same)
    assert r.coverage.state is CoverageState.COMPLETE


def test_unknown_intf_ip_abstains_never_unowned():
    # an interface with ip=None may BE the owner: unknown never collapses
    # to a violation
    base = _routed_ir(gateway="10.0.0.1", intf_ip="10.0.0.1")
    prop = _routed_ir(gateway="10.0.0.1", intf_ip=None)
    r = _run(base, prop)
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]
    assert r.coverage.state is CoverageState.PARTIAL


def test_unparseable_declared_gateway_abstains():
    # "foo" passes _literal_ip (not templated) — same_ip returns None
    # against every interface -> ownership UNKNOWN, never .gateway_unowned
    base = _routed_ir(gateway="10.0.0.1")
    prop = _routed_ir(gateway="foo")
    r = _run(base, prop)
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]
    assert r.coverage.state is CoverageState.PARTIAL


def test_no_interfaces_stays_with_existence_codes():
    # strict precedence: the no-intf case is .removed/.unserved territory;
    # .gateway_unowned never fires there (no double-fire)
    base = _routed_ir(gateway="10.0.0.1", with_intf=True)
    prop = _routed_ir(gateway="10.0.0.1", with_intf=False)
    r = _run(base, prop)
    codes = [f.code for f in r.findings]
    assert any(c.endswith(".removed") for c in codes)
    assert not any(c.endswith("gateway_unowned") for c in codes)
