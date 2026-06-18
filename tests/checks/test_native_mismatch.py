"""wired.l2.native_mismatch: a link whose two ends disagree on the native VLAN
leaks untagged traffic between the two vlans — invisible to reachability
analysis (the graph simply doesn't carry a mismatched native), so it needs its
own check. Introduced by the delta -> ERROR; pre-existing -> INFO context;
native changed against a vlan-blind peer -> WARNING (mismatch unverifiable);
AP uplinks are vlan-transparent and never fire."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.native_mismatch import NativeVlanMismatchCheck
from digital_twin.contracts import Severity
from digital_twin.ir import (
    ConfidenceLevel,
    IRBuilder,
    IRCapability,
    Port,
    PortMode,
    diff_ir,
)
from digital_twin.ir.provenance import Provenance, fact_meta
from tests.factories import ap, link, sw, trunk_port


def _blind_port(pid):
    # stat-ensured: OBSERVED, no vlan facts -> carriage unknown
    did, name = pid.split(":")
    return Port(
        id=pid,
        device_id=did,
        name=name,
        mode=PortMode.TRUNK,
        meta=fact_meta(Provenance.OBSERVED, ("port ensured from stats",)),
    )


def _two_switch_ir(a_native, b_native, *, b_port=None, a_disabled=False):
    b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
    b.add_port(
        Port(
            id="S:ge-0/0/1",
            device_id="S",
            name="ge-0/0/1",
            mode=PortMode.TRUNK,
            native_vlan=a_native,
            tagged_vlans=(20,),
            disabled=a_disabled,
        )
    )
    b.add_port(b_port or trunk_port("T", "ge-0/0/1", tagged=(20,), native=b_native))
    b.add_link(link("S:ge-0/0/1", "T:ge-0/0/1"))  # two-sided -> HIGH
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return NativeVlanMismatchCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_introduced_mismatch_is_unsafe():
    result = _run(_two_switch_ir(10, 10), _two_switch_ir(10, 30))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l2.native_mismatch.introduced"
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["a_native"] == 10 and f.evidence["b_native"] == 30
    # the finding names its object: the link (id matches the evidence link id)
    assert f.subject is not None
    assert f.subject.kind == "link" and f.subject.id == f.evidence["link"]


def test_preexisting_mismatch_is_info_context_only():
    result = _run(_two_switch_ir(10, 30), _two_switch_ir(10, 30))
    assert result.status is Status.PASS
    f = result.findings[0]
    assert f.code == "wired.l2.native_mismatch.preexisting"
    assert f.severity is Severity.INFO
    # context must not drag the check-result confidence floor down
    assert result.confidence.level is ConfidenceLevel.HIGH


def test_changing_an_already_mismatched_pair_is_still_attributed():
    # (10,30) -> (10,40): the delta did not create the mismatch but it ALTERED
    # it — the new leak pair is the delta's doing
    result = _run(_two_switch_ir(10, 30), _two_switch_ir(10, 40))
    assert result.status is Status.FAIL
    assert result.findings[0].code == "wired.l2.native_mismatch.introduced"


def test_enabling_a_disabled_mismatched_link_is_attributed():
    # review regression (74b78c7): natives 10 vs 30 exist in the baseline but
    # one end is admin-disabled — the leak is INERT. The delta enabling the
    # port ACTIVATES it; that is the delta's doing, not pre-existing context.
    result = _run(
        _two_switch_ir(10, 30, a_disabled=True), _two_switch_ir(10, 30, a_disabled=False)
    )
    assert result.status is Status.FAIL
    assert result.findings[0].code == "wired.l2.native_mismatch.introduced"


def test_activating_a_link_against_a_blind_peer_is_unverifiable():
    # same activation principle on the blind-peer path: the native is unchanged
    # but the link only comes alive with the delta -> the (unverifiable)
    # mismatch becomes active -> WARNING, not silence
    blind = _blind_port("T:ge-0/0/1")
    result = _run(
        _two_switch_ir(10, None, b_port=blind, a_disabled=True),
        _two_switch_ir(10, None, b_port=blind, a_disabled=False),
    )
    assert result.status is Status.WARN
    assert result.findings[0].code == "wired.l2.native_mismatch.unverified"


def test_adding_a_link_between_mismatched_ports_is_attributed():
    # review regression (6a24baf): both ports pre-exist enabled with natives
    # 10/30 but the LINK only appears in the proposed IR. A link-only diff must
    # (a) make the check applicable at the registry and (b) read as introduced,
    # not pre-existing — the baseline hazard needs the baseline LINK to exist.
    def ir(with_link):
        b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
        b.add_port(trunk_port("S", "ge-0/0/1", tagged=(20,), native=10))
        b.add_port(trunk_port("T", "ge-0/0/1", tagged=(20,), native=30))
        if with_link:
            b.add_link(link("S:ge-0/0/1", "T:ge-0/0/1"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    base, prop = ir(False), ir(True)
    assert NativeVlanMismatchCheck().applies_to(diff_ir(base, prop))
    result = _run(base, prop)
    assert result.status is Status.FAIL
    assert result.findings[0].code == "wired.l2.native_mismatch.introduced"


def test_unfolding_a_vc_activates_the_mismatch_and_is_attributed():
    # the check consumes DEVICE facts too (vc_members folding, AP role): a
    # device-only diff that turns a chassis-internal link into an external
    # boundary must reach the check (applies_to) and read as introduced
    def ir(vc):
        b = IRBuilder().add_device(sw("S", vc_members=("S2",) if vc else ())).add_device(sw("S2"))
        b.add_port(trunk_port("S", "ge-0/0/1", tagged=(20,), native=10))
        b.add_port(trunk_port("S2", "ge-0/0/1", tagged=(20,), native=30))
        b.add_link(link("S:ge-0/0/1", "S2:ge-0/0/1"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    base, prop = ir(True), ir(False)
    assert NativeVlanMismatchCheck().applies_to(diff_ir(base, prop))
    result = _run(base, prop)
    assert result.status is Status.FAIL
    assert result.findings[0].code == "wired.l2.native_mismatch.introduced"


def test_ap_to_switch_role_change_activates_the_blind_peer_path():
    # review regression (46a508d): in the baseline the link was AP-transparent
    # (never a native-mismatch surface); the delta re-roles the AP as a switch,
    # making it a real L2 boundary with a vlan-blind peer. The unchanged native
    # must NOT be suppressed as already-live — the boundary itself is new.
    def ir(role_ap):
        b = IRBuilder().add_device(sw("S")).add_device(ap("A") if role_ap else sw("A"))
        b.add_port(trunk_port("S", "ge-0/0/1", tagged=(20,), native=10))
        b.add_port(_blind_port("A:eth0"))
        b.add_link(link("S:ge-0/0/1", "A:eth0"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(ir(True), ir(False))
    assert result.status is Status.WARN
    assert result.findings[0].code == "wired.l2.native_mismatch.unverified"


def test_peer_going_blind_after_a_verified_match_is_unverifiable():
    # review regression (1b935a9): baseline had BOTH ends known and MATCHING —
    # a verified no-mismatch. The proposed IR makes the peer vlan-blind, so
    # that guarantee is gone: the cannot-rule-out condition is the delta's
    # doing and must WARN, not be suppressed as "native unchanged"
    base = _two_switch_ir(10, 10)
    prop = _two_switch_ir(10, None, b_port=_blind_port("T:ge-0/0/1"))
    result = _run(base, prop)
    assert result.status is Status.WARN
    assert result.findings[0].code == "wired.l2.native_mismatch.unverified"


def test_matching_natives_and_removed_native_are_silent():
    assert _run(_two_switch_ir(10, 10), _two_switch_ir(10, 10)).findings == ()
    # removing the native (None) means no untagged traffic -> nothing to leak
    assert _run(_two_switch_ir(10, 10), _two_switch_ir(None, 10)).findings == ()


def test_inferred_end_caps_severity_and_confidence():
    # one end's native comes from an INFERRED usage (e.g. system-defined) ->
    # the mismatch claim cannot exceed MEDIUM -> WARNING, not ERROR
    inferred = Port(
        id="T:ge-0/0/1",
        device_id="T",
        name="ge-0/0/1",
        mode=PortMode.TRUNK,
        native_vlan=30,
        tagged_vlans=(20,),
        meta=fact_meta(Provenance.INFERRED, ("system-defined usage",)),
    )
    result = _run(_two_switch_ir(10, 10), _two_switch_ir(10, None, b_port=inferred))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM


def test_native_change_against_a_blind_peer_is_unverifiable():
    blind = _blind_port("T:ge-0/0/1")
    result = _run(
        _two_switch_ir(10, None, b_port=blind), _two_switch_ir(30, None, b_port=blind)
    )
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.l2.native_mismatch.unverified"
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM


def test_unchanged_native_against_a_blind_peer_is_silent():
    blind = _blind_port("T:ge-0/0/1")
    result = _run(
        _two_switch_ir(10, None, b_port=blind), _two_switch_ir(10, None, b_port=blind)
    )
    assert result.findings == ()


def test_vc_internal_links_never_fire():
    # review regression (96b9a7c): a VC member-to-member link is chassis
    # backplane, not an external L2 boundary — the graph folds members into
    # their root and drops these links (l2_graph); so must this check
    from digital_twin.ir import LinkKind

    def ir(b_native):
        b = IRBuilder().add_device(sw("S", vc_members=("S2",))).add_device(sw("S2"))
        b.add_port(trunk_port("S", "vcp0", tagged=(20,), native=10))
        b.add_port(trunk_port("S2", "vcp1", tagged=(20,), native=b_native))
        b.add_link(link("S:vcp0", "S2:vcp1", LinkKind.VC))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(ir(10), ir(30))
    assert result.status is Status.PASS and result.findings == ()


def test_ap_uplinks_are_vlan_transparent_and_never_fire():
    def ir(native):
        b = IRBuilder().add_device(sw("S")).add_device(ap("A"))
        b.add_port(trunk_port("S", "ge-0/0/1", tagged=(20,), native=native))
        b.add_port(Port(id="A:eth0", device_id="A", name="eth0", mode=PortMode.TRUNK))
        b.add_link(link("S:ge-0/0/1", "A:eth0"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    assert _run(ir(10), ir(30)).findings == ()


# ── caused_by attribution ──────────────────────────────────────────────────────

def test_introduced_mismatch_caused_by_is_non_empty():
    # delta changes T:ge-0/0/1 native_vlan -> it appears in caused_by
    result = _run(_two_switch_ir(10, 10), _two_switch_ir(10, 30))
    f = result.findings[0]
    assert f.severity is not Severity.INFO
    assert len(f.caused_by) > 0
    ids = {c.ref.id for c in f.caused_by}
    assert "T:ge-0/0/1" in ids
    assert all(c.ref.kind == "port" for c in f.caused_by)


def test_preexisting_mismatch_caused_by_is_empty():
    # same mismatch in baseline and proposed -> INFO row -> caused_by must be ()
    result = _run(_two_switch_ir(10, 30), _two_switch_ir(10, 30))
    f = result.findings[0]
    assert f.severity is Severity.INFO
    assert f.caused_by == ()
