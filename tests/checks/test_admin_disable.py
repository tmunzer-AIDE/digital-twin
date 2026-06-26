"""wired.port.admin_disable: administratively disabling a switch port. ERROR
(UNSAFE) when a HIGH-confidence AP uplink is cut; WARNING (REVIEW) for a
MEDIUM AP tie, a trunk/inter-switch link, or a port with active wired clients;
INFO (context) for a bare edge port or a prop-only port with no baseline state.
Pre-existing-disabled and re-enable are not flagged."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.admin_disable import AdminDisableCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.provenance import Provenance
from tests.factories import ap, link, sw, wired_client


def _run(base, prop):
    return AdminDisableCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def _ap_ir(*, disabled, prov=Provenance.LLDP_TWO_SIDED):
    b = IRBuilder().add_device(sw("S")).add_device(ap("A"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.TRUNK,
                    disabled=disabled))
    b.add_port(Port(id="A:eth0", device_id="A", name="eth0", mode=PortMode.TRUNK))
    b.add_link(link("S:ge-0/0/1", "A:eth0", prov=prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_disabling_a_high_confidence_ap_uplink_is_unsafe():
    r = _run(_ap_ir(disabled=False), _ap_ir(disabled=True))
    assert r.status is Status.FAIL
    f = r.findings[0]
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert "A" in f.affected_entities


def test_medium_ap_tie_is_warning_not_unsafe():
    # one-sided/inferred tie -> WARNING even though it's an AP (decide() floors
    # UNSAFE on any network ERROR before confidence, so ERROR needs a HIGH tie)
    r = _run(_ap_ir(disabled=False, prov=Provenance.INFERRED),
             _ap_ir(disabled=True, prov=Provenance.INFERRED))
    assert r.status is Status.WARN
    assert r.findings[0].severity is Severity.WARNING


def _edge_ir(*, disabled, mode=PortMode.ACCESS, with_client=False):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/2", device_id="S", name="ge-0/0/2", mode=mode, disabled=disabled))
    if with_client:
        b.add_client(wired_client("cc:01", "S:ge-0/0/2", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_disabling_a_bare_edge_port_is_info_context():
    r = _run(_edge_ir(disabled=False), _edge_ir(disabled=True))
    assert r.status is Status.PASS  # INFO does not floor
    assert r.findings[0].severity is Severity.INFO


def test_disabling_a_trunk_edge_port_is_review():
    r = _run(_edge_ir(disabled=False, mode=PortMode.TRUNK),
             _edge_ir(disabled=True, mode=PortMode.TRUNK))
    assert r.status is Status.WARN
    assert r.findings[0].severity is Severity.WARNING


def test_nonap_peer_uses_link_confidence_not_high():
    # an inter-switch ACCESS port with a one-sided LLDP peer -> WARNING, but the
    # finding's confidence is the LINK's (LOW), not overstated HIGH (P3)
    def ir(disabled):
        b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
        b.add_port(Port(id="S:ge-0/0/3", device_id="S", name="ge-0/0/3",
                        mode=PortMode.ACCESS, disabled=disabled))
        b.add_port(Port(id="T:ge-0/0/3", device_id="T", name="ge-0/0/3", mode=PortMode.ACCESS))
        b.add_link(link("S:ge-0/0/3", "T:ge-0/0/3", prov=Provenance.LLDP_ONE_SIDED))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    r = _run(ir(False), ir(True))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.LOW  # link confidence, not HIGH


def test_disabling_a_port_with_active_wired_clients_is_review():
    r = _run(_edge_ir(disabled=False, with_client=True),
             _edge_ir(disabled=True, with_client=True))
    assert r.status is Status.WARN
    assert r.findings[0].severity is Severity.WARNING


def test_prop_only_disabled_port_is_info_unattributable():
    # no baseline Port for this pid -> INFO (blast radius unattributable), NOT skipped
    base = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2).build()
    prop = IRBuilder().add_device(sw("S"))
    prop.add_port(Port(id="S:mge-0/0/0", device_id="S", name="mge-0/0/0",
                       mode=PortMode.ACCESS, disabled=True))
    prop.with_capability(IRCapability.WIRED_L2)
    r = _run(base, prop.build())
    assert r.status is Status.PASS
    assert len(r.findings) == 1
    assert r.findings[0].severity is Severity.INFO
    assert r.findings[0].code == "wired.port.admin_disable.unattributable"


def test_already_disabled_is_not_flagged():
    assert _run(_edge_ir(disabled=True), _edge_ir(disabled=True)).findings == ()


def test_re_enable_is_not_flagged():
    assert _run(_edge_ir(disabled=True), _edge_ir(disabled=False)).findings == ()


def test_caused_by_points_at_the_port():
    r = _run(_ap_ir(disabled=False), _ap_ir(disabled=True))
    f = r.findings[0]
    assert f.caused_by and f.caused_by[0].ref.kind == "port"
    assert f.caused_by[0].ref.id == "S:ge-0/0/1"


def _trunk_ir(*, disabled, is_uplink, with_peer_link, peer_prov=Provenance.LLDP_TWO_SIDED):
    """S:up is a TRUNK with no AP and no wired clients. Optionally a peer link to
    a 2nd switch (at `peer_prov`'s confidence), and an observed is_uplink bit."""
    b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
    up = Port(id="S:up", device_id="S", name="up", mode=PortMode.TRUNK,
              disabled=disabled, is_uplink=is_uplink)
    b.add_port(up)
    if with_peer_link:
        b.add_port(Port(id="T:down", device_id="T", name="down", mode=PortMode.TRUNK))
        b.add_link(link("S:up", "T:down", prov=peer_prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_unconnected_non_uplink_trunk_is_info():
    # is_uplink False, no peer link, no AP, no clients -> demoted to INFO
    res = _run(_trunk_ir(disabled=False, is_uplink=False, with_peer_link=False),
               _trunk_ir(disabled=True, is_uplink=False, with_peer_link=False))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.INFO
    assert f.code == "wired.port.admin_disable.edge"


def test_uplink_true_trunk_stays_warning():
    res = _run(_trunk_ir(disabled=False, is_uplink=True, with_peer_link=False),
               _trunk_ir(disabled=True, is_uplink=True, with_peer_link=False))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.WARNING


def test_unknown_uplink_trunk_stays_warning_conservative():
    # is_uplink None (absent bit) -> conservative WARNING, never demoted
    res = _run(_trunk_ir(disabled=False, is_uplink=None, with_peer_link=False),
               _trunk_ir(disabled=True, is_uplink=None, with_peer_link=False))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.WARNING


def test_linked_trunk_warns_at_link_confidence_even_if_not_uplink():
    # a modeled ONE-SIDED peer link -> WARNING at the LINK's confidence (LOW),
    # NOT demoted, even though is_uplink is False. The LOW assertion is what proves
    # the peer-link branch runs FIRST: the old blanket trunk branch returned _HIGH,
    # so a two-sided (HIGH) link could not distinguish the two implementations.
    res = _run(
        _trunk_ir(disabled=False, is_uplink=False, with_peer_link=True,
                  peer_prov=Provenance.LLDP_ONE_SIDED),
        _trunk_ir(disabled=True, is_uplink=False, with_peer_link=True,
                  peer_prov=Provenance.LLDP_ONE_SIDED),
    )
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.LOW
