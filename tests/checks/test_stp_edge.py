"""wired.stp.edge_on_uplink: stp_disable (BPDU drop) or stp_edge configured on
a switch-to-switch link breaks/weakens loop protection exactly where it
matters. AP uplinks are SKIPPED — stp_edge there is correct practice. Hard
hazard (bpdu_filter) introduced -> ERROR; soft (edge_port, self-healing on
BPDU receipt) -> WARNING; pre-existing hard -> INFO context, soft -> silent."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.stp_edge import StpEdgeOnUplinkCheck
from digital_twin.contracts import Severity
from digital_twin.ir import (
    ConfidenceLevel,
    IRBuilder,
    IRCapability,
    Port,
    PortMode,
    diff_ir,
)
from tests.factories import ap, link, sw


def _port(pid, *, stp_edge=False, bpdu_filter=False):
    did, name = pid.split(":")
    return Port(
        id=pid,
        device_id=did,
        name=name,
        mode=PortMode.TRUNK,
        native_vlan=10,
        tagged_vlans=(20,),
        stp_edge=stp_edge,
        bpdu_filter=bpdu_filter,
    )


def _ir(*, a_edge=False, a_filter=False, peer_ap=False):
    b = IRBuilder().add_device(sw("S")).add_device(ap("A") if peer_ap else sw("T"))
    b.add_port(_port("S:ge-0/0/1", stp_edge=a_edge, bpdu_filter=a_filter))
    peer = "A:eth0" if peer_ap else "T:ge-0/0/1"
    b.add_port(_port(peer))
    b.add_link(link("S:ge-0/0/1", peer))  # two-sided -> HIGH
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return StpEdgeOnUplinkCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_introduced_bpdu_filter_on_an_uplink_is_unsafe():
    result = _run(_ir(), _ir(a_filter=True))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.code == "wired.stp.edge_on_uplink.bpdu_filter"
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH


def test_preexisting_bpdu_filter_is_info_context():
    result = _run(_ir(a_filter=True), _ir(a_filter=True))
    assert result.status is Status.PASS
    f = result.findings[0]
    assert f.code == "wired.stp.edge_on_uplink.bpdu_filter" and f.severity is Severity.INFO


def test_introduced_stp_edge_on_an_uplink_is_a_warning():
    result = _run(_ir(), _ir(a_edge=True))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.stp.edge_on_uplink.edge_port" and f.severity is Severity.WARNING


def test_preexisting_stp_edge_is_silent():
    assert _run(_ir(a_edge=True), _ir(a_edge=True)).findings == ()


def test_stp_edge_on_an_ap_uplink_is_correct_practice():
    assert _run(_ir(peer_ap=True), _ir(a_edge=True, peer_ap=True)).findings == ()
    assert _run(_ir(peer_ap=True), _ir(a_filter=True, peer_ap=True)).findings == ()


# ── caused_by attribution ──────────────────────────────────────────────────────

def test_introduced_bpdu_filter_caused_by_is_non_empty():
    # delta sets bpdu_filter on S:ge-0/0/1 -> it appears in caused_by
    result = _run(_ir(), _ir(a_filter=True))
    f = result.findings[0]
    assert f.severity is not Severity.INFO
    assert len(f.caused_by) > 0
    assert f.caused_by[0].ref.kind == "port"
    assert f.caused_by[0].ref.id == "S:ge-0/0/1"


def test_preexisting_bpdu_filter_caused_by_is_empty():
    # bpdu_filter unchanged in delta -> INFO row -> caused_by must be ()
    result = _run(_ir(a_filter=True), _ir(a_filter=True))
    f = result.findings[0]
    assert f.severity is Severity.INFO
    assert f.caused_by == ()
