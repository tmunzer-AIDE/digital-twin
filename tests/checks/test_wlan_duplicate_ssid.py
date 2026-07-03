from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.wlan_duplicate_ssid import WlanDuplicateSsidCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRCapability, Wlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(*wlans):
    b = IRBuilder().with_capability(IRCapability.WLAN_CONFIG)
    for w in wlans:
        b.add_wlan(w)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_two_site_scoped_same_ssid_introduced_warns():
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"),
               Wlan(id="w2", ssid="corp", enabled=True, apply_to="site"))
    res = WlanDuplicateSsidCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN and res.findings[0].code.endswith(".introduced")


def test_disabled_duplicate_not_flagged():
    ir = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"),
             Wlan(id="w2", ssid="corp", enabled=False, apply_to="site"))
    assert WlanDuplicateSsidCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_provably_disjoint_aps_is_silent_pass():
    # same SSID but explicit DISJOINT AP scopes -> _overlap "no" -> neither finding nor note
    ir = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="aps", ap_ids=("apX",)),
             Wlan(id="w2", ssid="corp", enabled=True, apply_to="aps", ap_ids=("apY",)))
    res = WlanDuplicateSsidCheck().run(_ctx(ir, ir))
    assert res.status is Status.PASS and res.coverage.state is CoverageState.COMPLETE


def test_site_and_explicit_ap_scope_overlap_warns():
    # site x aps-with-ids: the site WLAN provably covers the explicit AP
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"),
               Wlan(id="w2", ssid="corp", enabled=True, apply_to="aps", ap_ids=("ap1",)))
    res = WlanDuplicateSsidCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN
    f = res.findings[0]
    assert f.code == "wireless.wlan.duplicate_ssid.introduced"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert res.coverage.state is CoverageState.COMPLETE


def test_explicit_ap_and_site_scope_overlap_warns():
    # the symmetric arm (a=aps, b=site under the id sort)
    base = _ir(Wlan(id="w2", ssid="corp", enabled=True, apply_to="site"))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="aps", ap_ids=("ap1",)),
               Wlan(id="w2", ssid="corp", enabled=True, apply_to="site"))
    res = WlanDuplicateSsidCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN
    f = res.findings[0]
    assert f.code == "wireless.wlan.duplicate_ssid.introduced"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.HIGH


def test_site_and_empty_ap_scope_is_provably_disjoint():
    # aps scope with NO explicit ap_ids covers nothing -> "no" overlap: neither
    # a finding nor an unverifiable note
    ir = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"),
             Wlan(id="w2", ssid="corp", enabled=True, apply_to="aps", ap_ids=()))
    res = WlanDuplicateSsidCheck().run(_ctx(_ir(), ir))
    assert res.status is Status.PASS
    assert res.findings == ()
    assert res.coverage.state is CoverageState.COMPLETE


def test_unrecognized_scope_falls_back_to_unknown_partial_not_finding():
    # an apply_to value outside site/aps/wxtags/None hits the "unknown" fallback:
    # unverifiable -> PARTIAL note, no WARNING — and the lint tier's HIGH result
    # confidence is unchanged (PARTIAL coverage, not confidence, carries the doubt)
    w1 = Wlan(id="w1", ssid="corp", enabled=True, apply_to="site")
    w2 = Wlan(id="w2", ssid="corp", enabled=True, apply_to="mystery")
    res = WlanDuplicateSsidCheck().run(_ctx(_ir(w1), _ir(w1, w2)))
    assert res.status is Status.PASS
    assert all(f.severity is not Severity.WARNING for f in res.findings)
    assert res.coverage.state is CoverageState.PARTIAL
    assert any("unverifiable" in n for n in res.coverage.notes)
    assert res.confidence.level is ConfidenceLevel.HIGH


def test_wxtag_scoped_duplicate_INTRODUCED_is_note_not_finding():
    # introducing the second wxtag WLAN touches it -> PARTIAL note, no WARNING
    w1 = Wlan(id="w1", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t1",))
    w2 = Wlan(id="w2", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t2",))
    res = WlanDuplicateSsidCheck().run(_ctx(_ir(w1), _ir(w1, w2)))
    assert all(f.severity is not Severity.WARNING for f in res.findings)
    assert res.coverage.state is CoverageState.PARTIAL


def test_unrelated_diff_leaves_old_wxtag_duplicate_complete():
    w1 = Wlan(id="w1", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t1",))
    w2 = Wlan(id="w2", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t2",))
    base = _ir(w1, w2, Wlan(id="w3", ssid="iot", enabled=True, apply_to="site"))
    prop = _ir(w1, w2, Wlan(id="w3", ssid="iot2", enabled=True, apply_to="site"))  # only w3
    assert WlanDuplicateSsidCheck().run(_ctx(base, prop)).coverage.state is CoverageState.COMPLETE
