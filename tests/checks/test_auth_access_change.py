"""wired.auth.access_change: any in-scope auth change floors REVIEW (admission
impact depends on RADIUS/NAC, not modeled). Observed connected clients escalate
detail/confidence when admission tightens — capped at REVIEW, never UNSAFE.
No enrichment -> still REVIEW (floor)."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.auth_change import AuthAccessChangeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.entities import ClientEnrichment, PortAuth
from tests.factories import sw, wired_client


def _ir(auth, *, client=None, enrich=None):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10, auth=auth))
    if client is not None:
        b.add_client(client)
        if enrich is not None:
            b.set_client_enrichment({client.id: enrich})
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return AuthAccessChangeCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def test_gaining_dot1x_floors_review():
    r = _run(_ir(None), _ir(PortAuth(port_auth="dot1x")))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.auth.access_change.policy_change"
    assert f.severity is Severity.WARNING


def test_persist_mac_only_change_floors_review():
    # the false-SAFE guard: persist_mac-only (no port_auth) still surfaces
    r = _run(_ir(None), _ir(PortAuth(persist_mac=True)))
    assert r.status is Status.WARN
    assert r.findings[0].code == "wired.auth.access_change.policy_change"


def test_no_change_is_silent():
    assert _run(_ir(PortAuth(port_auth="dot1x")), _ir(PortAuth(port_auth="dot1x"))).findings == ()
    assert _run(_ir(None), _ir(None)).findings == ()


def test_tightening_with_unauth_client_escalates_but_caps_at_review():
    c = wired_client("cc:01", "S:ge-0/0/1", vlan=10)
    enrich = ClientEnrichment(auth_state="unauthenticated")
    base = _ir(None, client=c, enrich=enrich)
    prop = _ir(PortAuth(port_auth="dot1x"), client=c, enrich=enrich)
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code == "wired.auth.access_change.clients_at_risk")
    assert f.severity is Severity.WARNING       # capped at REVIEW, never ERROR
    assert "cc:01" in f.affected_entities or any("cc:01" in str(v) for v in f.evidence.values())
    assert r.status is Status.WARN              # never FAIL/UNSAFE


def test_no_enrichment_still_reviews_floor_only():
    c = wired_client("cc:02", "S:ge-0/0/1", vlan=10)
    base = _ir(None, client=c)            # client present, NO enrichment
    prop = _ir(PortAuth(port_auth="dot1x"), client=c)
    r = _run(base, prop)
    assert r.status is Status.WARN
    # degrades to the floor; no clients_at_risk without enrichment evidence
    assert all(f.code == "wired.auth.access_change.policy_change" for f in r.findings)


def test_base_only_port_auth_loss_surfaces():
    # a port present ONLY in baseline (e.g. its local port_auth entry was deleted)
    # must still surface the auth LOSS — union iteration, missing side = None
    base = _ir(PortAuth(port_auth="dot1x"))
    prop = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2).build()
    r = _run(base, prop)
    assert r.status is Status.WARN
    assert r.findings[0].code == "wired.auth.access_change.policy_change"


def test_mac_auth_only_drops_dot1x_client():
    # dot1x -> mac-auth-only: a client authenticated via dot1x is no longer
    # admitted (method dropped) -> escalates, capped at REVIEW
    c = wired_client("dd:01", "S:ge-0/0/1", vlan=10)
    enrich = ClientEnrichment(auth_state="authenticated", auth_method="dot1x")
    base = _ir(PortAuth(port_auth="dot1x"), client=c, enrich=enrich)
    prop = _ir(PortAuth(port_auth="dot1x", mac_auth_only=True), client=c, enrich=enrich)
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code == "wired.auth.access_change.clients_at_risk")
    assert "dd:01" in f.affected_entities
    assert f.severity is Severity.WARNING and r.status is Status.WARN  # capped


def test_guest_removal_with_guest_client_escalates():
    # removing a guest fallback while a guest-state client is connected -> at risk
    c = wired_client("ee:01", "S:ge-0/0/1", vlan=10)
    enrich = ClientEnrichment(auth_state="guest")
    base = _ir(PortAuth(port_auth="dot1x", guest_network="guest"), client=c, enrich=enrich)
    prop = _ir(PortAuth(port_auth="dot1x"), client=c, enrich=enrich)
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code == "wired.auth.access_change.clients_at_risk")
    assert "ee:01" in f.affected_entities
