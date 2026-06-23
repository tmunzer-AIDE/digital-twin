"""wired.l3.bgp_adjacency structural codes + coverage notes — Task 7.

Task 8 will extend this file with telemetry-escalation tests; the structural
tests here must remain passing after that extension.
"""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.bgp_adjacency import BgpAdjacencyCheck
from digital_twin.contracts import Severity
from digital_twin.ir import BgpPeer, DeviceRole, IRBuilder, IRCapability, diff_ir
from digital_twin.ir.entities import Device


def _peer(nip: str = "10.0.0.2", **kw: object) -> BgpPeer:
    return BgpPeer(device_id="d1", role=DeviceRole.SWITCH, session_name="s",
                   neighbor_ip=nip, **kw)  # type: ignore[arg-type]


def _ir(peers: list[BgpPeer], caps: tuple[IRCapability, ...] = (IRCapability.WIRED_L2,)):
    b = IRBuilder().add_device(Device(id="d1", role=DeviceRole.SWITCH, site="x"))
    for c in caps:
        b.with_capability(c)
    for p in peers:
        b.add_bgp_peer(p)
    return b.build()


def _run(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    ctx = CheckContext(baseline=AnalysisContext(base_ir), proposed=AnalysisContext(prop_ir),
                      diff=diff)
    return BgpAdjacencyCheck().run(ctx)


def _codes(res):
    return {f.code for f in res.findings}


def test_applies_only_to_bgp_peer_diff():
    base, prop = _ir([_peer()]), _ir([])
    assert BgpAdjacencyCheck().applies_to(diff_ir(base, prop)) is True
    same = _ir([_peer()])
    assert BgpAdjacencyCheck().applies_to(diff_ir(same, _ir([_peer()]))) is False


def test_peering_removed_is_review():
    res = _run(_ir([_peer()]), _ir([]))
    assert "wired.l3.bgp_adjacency.peering_removed" in _codes(res)
    assert res.status is Status.WARN
    assert all(f.severity is Severity.WARNING for f in res.findings)


def test_peering_disabled_is_review():
    res = _run(_ir([_peer(disabled=False)]), _ir([_peer(disabled=True)]))
    assert "wired.l3.bgp_adjacency.peering_disabled" in _codes(res)


def test_peering_added_is_review():
    res = _run(_ir([]), _ir([_peer()]))
    assert "wired.l3.bgp_adjacency.peering_added" in _codes(res)


def test_as_changed_carries_side_evidence_and_cofires_with_type():
    res = _run(_ir([_peer(neighbor_as=65001, session_type="external")]),
               _ir([_peer(neighbor_as=65002, session_type="internal")]))
    codes = _codes(res)
    assert "wired.l3.bgp_adjacency.as_changed" in codes
    assert "wired.l3.bgp_adjacency.session_type_changed" in codes
    as_f = next(f for f in res.findings if f.code.endswith(".as_changed"))
    assert as_f.evidence["neighbor_as_changed"] is True
    assert as_f.evidence["local_as_changed"] is False


def test_transport_changed_for_gateway():
    def g(**kw: object) -> BgpPeer:
        return BgpPeer(device_id="g1", role=DeviceRole.GATEWAY, session_name="s",
                       neighbor_ip="203.0.113.1", **kw)  # type: ignore[arg-type]

    base = (IRBuilder().add_device(Device(id="g1", role=DeviceRole.GATEWAY, site="x"))
            .with_capability(IRCapability.WIRED_L2).add_bgp_peer(g(via="wan")).build())
    prop = (IRBuilder().add_device(Device(id="g1", role=DeviceRole.GATEWAY, site="x"))
            .with_capability(IRCapability.WIRED_L2).add_bgp_peer(g(via="tunnel")).build())
    assert "wired.l3.bgp_adjacency.transport_changed" in _codes(_run(base, prop))


def test_ambiguous_peer_is_a_note_not_a_finding():
    res = _run(_ir([_peer(ambiguous=True, neighbor_as=1)]),
               _ir([_peer(ambiguous=True, neighbor_as=2)]))
    assert not res.findings
    assert res.coverage.notes


def test_unresolved_type_change_is_note_not_confident_change():
    res = _run(_ir([_peer(session_type=None, session_type_unresolved=None)]),
               _ir([_peer(session_type=None, session_type_unresolved="{{t}}")]))
    assert "wired.l3.bgp_adjacency.session_type_changed" not in _codes(res)
    assert res.coverage.notes


def test_ambiguous_on_either_side_abstains():
    # baseline ambiguous -> proposed clean: must STILL abstain (no confident as_changed)
    res1 = _run(_ir([_peer(ambiguous=True, neighbor_as=65001)]),
                _ir([_peer(ambiguous=False, neighbor_as=65002)]))
    assert "wired.l3.bgp_adjacency.as_changed" not in _codes(res1)
    assert res1.coverage.notes
    # baseline clean -> proposed ambiguous: also abstain
    res2 = _run(_ir([_peer(ambiguous=False, neighbor_as=65001)]),
                _ir([_peer(ambiguous=True, neighbor_as=65002)]))
    assert "wired.l3.bgp_adjacency.as_changed" not in _codes(res2)
    assert res2.coverage.notes


def test_added_with_templated_local_as_is_note_not_confident_add():
    res = _run(_ir([]),
               _ir([_peer(local_as=None, local_as_unresolved="{{asn}}", neighbor_as=65001)]))
    assert "wired.l3.bgp_adjacency.peering_added" not in _codes(res)
    assert res.coverage.notes


def test_switch_via_diff_is_silent():
    # a via difference on a SWITCH peer must NOT emit .transport_changed (switches are LAN)
    res = _run(_ir([_peer(via="lan")]), _ir([_peer(via="wan")]))
    assert "wired.l3.bgp_adjacency.transport_changed" not in _codes(res)
    assert not res.findings


def test_absent_to_templated_disabled_is_note_not_added():
    # active-ness of the proposed peer is UNKNOWN (templated disabled) -> abstain, not add
    res = _run(_ir([]),
               _ir([_peer(disabled=False, disabled_unresolved="{{flag}}", neighbor_as=65001)]))
    assert "wired.l3.bgp_adjacency.peering_added" not in _codes(res)
    assert res.coverage.notes


def test_templated_disabled_baseline_removed_is_note_not_removed():
    # baseline active-ness UNKNOWN (templated disabled); peer gone -> abstain, not remove
    res = _run(_ir([_peer(disabled=False, disabled_unresolved="{{flag}}", neighbor_as=65001)]),
               _ir([]))
    assert "wired.l3.bgp_adjacency.peering_removed" not in _codes(res)
    assert res.coverage.notes


def test_stable_fuzzy_peer_does_not_floor_unrelated_change():
    # peer A is ambiguous in BOTH base and prop (identical -> NOT delta-touched);
    # peer B changes its AS. Only B's finding fires; A emits NO note (relevance scope).
    a = _peer("10.0.0.2", ambiguous=True, neighbor_as=65001)
    res = _run(_ir([a, _peer("10.0.0.3", neighbor_as=65001)]),
               _ir([a, _peer("10.0.0.3", neighbor_as=65002)]))
    assert "wired.l3.bgp_adjacency.as_changed" in _codes(res)
    assert not res.coverage.notes  # the stable ambiguous A did NOT floor to PARTIAL/REVIEW
