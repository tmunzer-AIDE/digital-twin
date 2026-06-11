"""wired.l2.mtu_mismatch: ends of a link disagreeing on MTU silently drop
large frames. Numeric-vs-numeric introduced/altered -> ERROR (UNSAFE at HIGH);
identical live baseline pair -> INFO context; explicit-vs-platform-default ->
WARNING/MEDIUM (.vs_default — the default's value is unmodeled); blind peer ->
.unverified with the same baseline-uncertainty symmetry as native_mismatch.
Boundary selection (VC folding, disabled ends, AP transparency) is shared via
link_boundary.BoundaryView."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.mtu_mismatch import MtuMismatchCheck
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
    did, name = pid.split(":")
    return Port(
        id=pid,
        device_id=did,
        name=name,
        mode=PortMode.TRUNK,
        meta=fact_meta(Provenance.OBSERVED, ("port ensured from stats",)),
    )


def _ir(a_mtu, b_mtu, *, b_port=None):
    b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
    b.add_port(trunk_port("S", "ge-0/0/1", tagged=(20,), native=10, mtu=a_mtu))
    b.add_port(b_port or trunk_port("T", "ge-0/0/1", tagged=(20,), native=10, mtu=b_mtu))
    b.add_link(link("S:ge-0/0/1", "T:ge-0/0/1"))  # two-sided -> HIGH
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return MtuMismatchCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_introduced_numeric_mismatch_is_unsafe():
    result = _run(_ir(9200, 9200), _ir(9200, 1500))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.l2.mtu_mismatch.introduced"
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert {f.evidence["a_mtu"], f.evidence["b_mtu"]} == {9200, 1500}


def test_preexisting_numeric_mismatch_is_info_context():
    result = _run(_ir(9200, 1500), _ir(9200, 1500))
    assert result.status is Status.PASS
    f = result.findings[0]
    assert f.code == "wired.l2.mtu_mismatch.preexisting" and f.severity is Severity.INFO
    assert result.confidence.level is ConfidenceLevel.HIGH


def test_altering_an_existing_mismatch_is_attributed():
    result = _run(_ir(9200, 1500), _ir(9200, 9000))
    assert result.status is Status.FAIL
    assert result.findings[0].code == "wired.l2.mtu_mismatch.introduced"


def test_matching_or_both_default_is_silent():
    assert _run(_ir(9200, 9200), _ir(9200, 9200)).findings == ()
    assert _run(_ir(None, None), _ir(None, None)).findings == ()


def test_explicit_vs_platform_default_is_a_medium_warning():
    # the peer is a CONFIG statement with no explicit mtu: it runs the platform
    # default, whose value is unmodeled — almost certainly mismatched with an
    # explicit 9200, but the claim caps at MEDIUM -> WARNING (REVIEW)
    result = _run(_ir(None, None), _ir(9200, None))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.l2.mtu_mismatch.vs_default"
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM


def test_preexisting_vs_default_is_silent():
    # the soft explicit-vs-default state existed identically in the baseline:
    # not the delta's doing, and too weak a claim to surface as INFO context
    assert _run(_ir(9200, None), _ir(9200, None)).findings == ()


def test_mtu_change_against_a_blind_peer_is_unverifiable():
    blind = _blind_port("T:ge-0/0/1")
    result = _run(_ir(None, None, b_port=blind), _ir(9200, None, b_port=blind))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.l2.mtu_mismatch.unverified"
    assert f.severity is Severity.WARNING and f.confidence.level is ConfidenceLevel.MEDIUM


def test_unchanged_mtu_against_a_blind_peer_is_silent():
    blind = _blind_port("T:ge-0/0/1")
    assert _run(_ir(9200, None, b_port=blind), _ir(9200, None, b_port=blind)).findings == ()


def test_peer_going_blind_after_a_verified_match_is_unverifiable():
    base = _ir(9200, 9200)
    prop = _ir(9200, None, b_port=_blind_port("T:ge-0/0/1"))
    result = _run(base, prop)
    assert result.status is Status.WARN
    assert result.findings[0].code == "wired.l2.mtu_mismatch.unverified"


def test_ap_uplinks_never_fire():
    def ir(mtu):
        b = IRBuilder().add_device(sw("S")).add_device(ap("A"))
        b.add_port(trunk_port("S", "ge-0/0/1", tagged=(20,), native=10, mtu=mtu))
        b.add_port(Port(id="A:eth0", device_id="A", name="eth0", mode=PortMode.TRUNK))
        b.add_link(link("S:ge-0/0/1", "A:eth0"))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    assert _run(ir(None), ir(9200)).findings == ()
