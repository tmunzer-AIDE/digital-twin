from digital_twin.checks.base import Coverage, CoverageState, Status
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir.diff import EntityRef, IRDiff, Modified


def test_touched_ids_filters_by_kind():
    diff = IRDiff(added=(EntityRef("wlan", "w2"),), removed=(),
                  modified=(Modified(EntityRef("vlan", "10"), ("subnet",)),))
    assert touched_ids(diff, "wlan") == {"w2"}
    assert touched_ids(diff, "vlan") == {"10"}


def _v(key, summary):
    return Violation(key=key, subject=ObjectRef("vlan", "10"), affected=("10",),
                     evidence={"k": key}, summary=summary, caused_by=())


def test_introduced_is_warning_preexisting_is_info():
    res = run_delta_lint(
        check_id="wired.l2.vlan_collision",
        base=[_v("old", "old")],
        proposed=[_v("old", "old"), _v("new", "new")],
        coverage=Coverage(state=CoverageState.COMPLETE),
    )
    by_code = {f.code: f for f in res.findings}
    assert by_code["wired.l2.vlan_collision.introduced"].severity is Severity.WARNING
    assert by_code["wired.l2.vlan_collision.preexisting"].severity is Severity.INFO
    assert res.status is Status.WARN   # an introduced violation


def test_all_preexisting_is_pass():
    res = run_delta_lint(
        check_id="wired.l2.vlan_collision", base=[_v("k", "k")], proposed=[_v("k", "k")],
        coverage=Coverage(state=CoverageState.COMPLETE),
    )
    assert res.status is Status.PASS
    assert all(f.severity is Severity.INFO for f in res.findings)
