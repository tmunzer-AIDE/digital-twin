from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.wlan_open_guest import WlanOpenGuestCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRCapability, Wlan
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


_OPEN = dict(ssid="guest", enabled=True, auth_type="open", apply_to="site")


def test_introduced_open_no_isolation_is_warning():
    base = _ir(Wlan(id="w1", isolation=True, **_OPEN))          # was isolated
    prop = _ir(Wlan(id="w1", isolation=False, **_OPEN))         # isolation removed
    res = WlanOpenGuestCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN
    assert res.findings[0].code.endswith(".introduced")


def test_isolated_open_guest_is_clean():
    ir = _ir(Wlan(id="w1", isolation=True, **_OPEN))
    assert WlanOpenGuestCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_preexisting_open_no_isolation_is_info_not_warn():
    # present identically in baseline + proposed -> pre-existing INFO context, PASS
    ir = _ir(Wlan(id="w1", isolation=False, **_OPEN))
    f = WlanOpenGuestCheck().run(_ctx(ir, ir)).findings[0]
    assert f.severity is Severity.INFO and f.code.endswith(".preexisting")


def test_empty_explicit_scope_is_silent():
    ir = _ir(Wlan(id="w1", ssid="g", enabled=True, auth_type="open", apply_to="aps"))
    res = WlanOpenGuestCheck().run(_ctx(ir, ir))
    assert res.findings == () and res.coverage.state is CoverageState.COMPLETE


def test_wxtag_scope_INTRODUCED_is_partial_note_not_finding():
    # the wxtag WLAN is delta-touched (added) -> PARTIAL note, no WARNING
    wx = Wlan(id="w1", ssid="g", enabled=True, auth_type="open", isolation=False,
              apply_to="wxtags", wxtag_ids=("t1",))
    res = WlanOpenGuestCheck().run(_ctx(_ir(), _ir(wx)))
    assert all(f.severity is not Severity.WARNING for f in res.findings)
    assert res.coverage.state is CoverageState.PARTIAL


def test_unrelated_diff_leaves_old_wxtag_wlan_complete():
    # relevance-scoping: a pre-existing wxtag WLAN (untouched) + an UNRELATED wlan change
    # must NOT floor to PARTIAL/REVIEW.
    wx = Wlan(id="w1", ssid="g", enabled=True, auth_type="open", isolation=False,
              apply_to="wxtags", wxtag_ids=("t1",))
    base = _ir(wx, Wlan(id="w2", ssid="corp", enabled=True, apply_to="site"))
    prop = _ir(wx, Wlan(id="w2", ssid="corp2", enabled=True, apply_to="site"))  # only w2 changed
    res = WlanOpenGuestCheck().run(_ctx(base, prop))
    assert res.coverage.state is CoverageState.COMPLETE
