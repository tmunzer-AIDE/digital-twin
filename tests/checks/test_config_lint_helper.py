from digital_twin.checks.base import Coverage, CoverageState, Status
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import Cause, ObjectRef, Severity
from digital_twin.ir.diff import EntityRef, IRDiff, Modified


def test_touched_ids_filters_by_kind():
    diff = IRDiff(added=(EntityRef("wlan", "w2"),), removed=(),
                  modified=(Modified(EntityRef("vlan", "10"), ("subnet",)),))
    assert touched_ids(diff, "wlan") == {"w2"}
    assert touched_ids(diff, "vlan") == {"10"}


def test_touched_ids_empty_diff():
    assert touched_ids(IRDiff((), (), ()), "vlan") == set()


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


def test_changed_facts_same_subject_reads_as_introduced():
    # the defining invariant: the KEY carries the facts, so a changed violation on the
    # same subject has a NEW key and reads as introduced (not pre-existing INFO).
    res = run_delta_lint(
        check_id="x", base=[_v("k_old", "old fact")], proposed=[_v("k_new", "new fact")],
        coverage=Coverage(state=CoverageState.COMPLETE),
    )
    assert res.findings[0].code == "x.introduced"


def test_caused_by_suppressed_on_preexisting():
    # honesty guarantee: an INFO context finding never carries causation
    cb = (Cause(ref=ObjectRef("vlan", "10"), fields=("vlan_id",)),)
    v = Violation(key="k", subject=ObjectRef("vlan", "10"), affected=("10",),
                  summary="s", caused_by=cb)
    res = run_delta_lint(check_id="x", base=[v], proposed=[v],
                         coverage=Coverage(state=CoverageState.COMPLETE))
    assert res.findings[0].severity is Severity.INFO and res.findings[0].caused_by == ()
