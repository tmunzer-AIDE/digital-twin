"""wired.l1.link_param_mismatch: speed/duplex/autoneg incompatibility across a
link. forced-vs-forced different speed/duplex -> ERROR; forced-vs-autonegotiating
-> WARNING (.autoneg_mismatch); forced-vs-no-config-peer -> WARNING (.unverified);
both-auto/forced-identical -> silent. Observed enrichment is pre-existing-only:
clean negotiation suppresses, half-duplex annotates INFO; baseline observation
never upgrades an introduced mismatch."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l1_param_mismatch import L1ParamMismatchCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.provenance import Provenance, fact_meta
from tests.factories import link, sw


def _p(pid, *, speed=None, duplex=None, autoneg_disabled=False,
       observed_speed=None, observed_duplex=None, blind=False):
    did, name = pid.split(":")
    meta = fact_meta(Provenance.OBSERVED, ("ensured from stats",)) if blind else None
    kw = {"meta": meta} if meta else {}
    return Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK, speed=speed,
                duplex=duplex, autoneg_disabled=autoneg_disabled,
                observed_speed=observed_speed, observed_duplex=observed_duplex, **kw)


def _ir(pa, pb):
    b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
    b.add_port(pa)
    b.add_port(pb)
    b.add_link(link(pa.id, pb.id))  # two-sided -> HIGH
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return L1ParamMismatchCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def _forced(pid, speed="1g", duplex="full"):
    return _p(pid, speed=speed, duplex=duplex, autoneg_disabled=True)


def test_forced_vs_forced_different_speed_is_error():
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1"))
    prop = _ir(_forced("S:ge-0/0/1", "1g"), _forced("T:ge-0/0/1", "10g"))
    r = _run(base, prop)
    assert r.status is Status.FAIL
    assert r.findings[0].code == "wired.l1.link_param_mismatch.speed_conflict"
    assert r.findings[0].severity is Severity.ERROR


def test_forced_vs_forced_different_duplex_is_error():
    prop = _ir(_forced("S:ge-0/0/1", "1g", "full"), _forced("T:ge-0/0/1", "1g", "half"))
    r = _run(_ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1")), prop)
    assert r.status is Status.FAIL
    assert r.findings[0].code == "wired.l1.link_param_mismatch.duplex_conflict"


def test_forced_vs_autonegotiating_is_warning():
    # peer is config-stated and not forced -> autonegotiating
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1"))
    r = _run(_ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1")), prop)
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.l1.link_param_mismatch.autoneg_mismatch"
    assert f.severity is Severity.WARNING


def test_forced_vs_no_config_peer_is_unverified_not_autoneg():
    # blind peer (no config facts) -> .unverified, NOT .autoneg_mismatch
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1", blind=True))
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1", blind=True))
    r = _run(base, prop)
    assert r.findings[0].code == "wired.l1.link_param_mismatch.unverified"
    assert r.findings[0].severity is Severity.WARNING


def test_both_autonegotiating_is_silent():
    assert _run(_ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1")),
                _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1"))).findings == ()


def test_forced_identical_is_silent():
    # both ends forced to the SAME speed+duplex -> compatible -> silent
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1"))
    prop = _ir(_forced("S:ge-0/0/1", "1g", "full"), _forced("T:ge-0/0/1", "1g", "full"))
    assert _run(base, prop).findings == ()


def test_introduced_mismatch_not_upgraded_by_baseline_observed_half():
    # baseline peer observed half — must NOT make the INTRODUCED mismatch HIGH/ERROR
    # beyond what config provenance gives (time-honesty). Here forced-vs-auto stays WARNING.
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1", observed_duplex="half"))
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1", observed_duplex="half"))
    r = _run(base, prop)
    assert r.findings[0].severity is Severity.WARNING  # not escalated


def test_preexisting_conflict_clean_negotiation_suppressed():
    # same forced-vs-auto config in baseline AND both observed full at the same
    # speed -> hardware negotiated a working link -> suppressed
    base = _ir(_p("S:ge-0/0/1", speed="1g", duplex="full", autoneg_disabled=True,
                  observed_speed="1g", observed_duplex="full"),
               _p("T:ge-0/0/1", observed_speed="1g", observed_duplex="full"))
    r = _run(base, base)  # unchanged
    assert r.findings == ()  # pre-existing autoneg_mismatch + clean obs -> suppressed


def test_preexisting_conflict_no_clean_obs_is_info():
    forced_obs = _p("S:ge-0/0/1", speed="1g", duplex="full", autoneg_disabled=True)
    auto_half = _p("T:ge-0/0/1", observed_duplex="half")
    base = _ir(forced_obs, auto_half)
    r = _run(base, base)  # unchanged
    f = r.findings[0]
    assert f.code == "wired.l1.link_param_mismatch.preexisting" and f.severity is Severity.INFO
    assert r.status is Status.PASS  # INFO does not floor


def test_preexisting_unverified_suppressed():
    forced = _forced("S:ge-0/0/1")
    blind = _p("T:ge-0/0/1", blind=True)
    base = _ir(forced, blind)
    assert _run(base, base).findings == ()  # baseline-parity suppression


def test_unknown_peer_becoming_config_stated_auto_is_not_demoted():
    # baseline: forced vs NO-CONFIG peer (.unverified). proposed: same L1 tuple
    # (None/None/False) but the peer is now CONFIG-STATED -> .autoneg_mismatch.
    # The endpoint-class change means this is NOT pre-existing: it must surface as
    # WARNING, not be demoted to INFO/suppressed by the parity check.
    base = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1", blind=True))
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1"))  # peer now config-stated
    r = _run(base, prop)
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.l1.link_param_mismatch.autoneg_mismatch"
    assert f.severity is Severity.WARNING  # NOT preexisting/INFO
