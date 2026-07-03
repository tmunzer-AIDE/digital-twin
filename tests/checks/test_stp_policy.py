"""wired.stp.policy: the four StpPolicy knobs (stp_required, stp_no_root_port,
stp_p2p, use_vstp) NEVER resolve SAFE in this slice — a changed policy always
floors REVIEW via `.policy_change` (WARNING/MEDIUM). Precise codes are a later
task; v1 emits only the floor + `unresolved:` token coverage notes.
applies_to is changed_fields-precise: an unrelated port edit must not wake it."""

import dataclasses

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.stp_policy import StpPolicyCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.entities import StpPolicy
from tests.factories import sw


def _base_port(**kw):
    return Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.ACCESS,
                native_vlan=10, **kw)


def _ir_with_port(port):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(port)
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _ir_no_port():
    b = IRBuilder().add_device(sw("S"))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return StpPolicyCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def _run_flip(knob, value):
    base_port = _base_port()
    prop_port = dataclasses.replace(base_port, stp_policy=StpPolicy(**{knob: value}))
    return _run(_ir_with_port(base_port), _ir_with_port(prop_port))


def test_any_stp_policy_change_floors_review_via_policy_change():
    result = _run_flip("stp_p2p", True)
    f = result.findings[0]
    assert f.code == "wired.stp.policy.policy_change"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM
    assert result.status is Status.WARN


def test_unresolved_token_lands_on_the_floor_with_a_note():
    result = _run_flip("use_vstp", "unresolved:{{vstp}}")
    assert result.findings[0].code == "wired.stp.policy.policy_change"
    assert any("unresolved" in n for n in result.coverage.notes)
    assert result.coverage.state is CoverageState.PARTIAL


def _diff_with_description_only_change():
    # an unrelated port-field edit (mtu) that leaves stp_policy untouched and
    # does not affect port identity (unlike `name`, which is id-derived)
    base_port = _base_port()
    prop_port = dataclasses.replace(base_port, mtu=1500)
    return diff_ir(_ir_with_port(base_port), _ir_with_port(prop_port))


def _diff_with_port_added_carrying_policy():
    prop_port = _base_port(stp_policy=StpPolicy(stp_p2p=True))
    return diff_ir(_ir_no_port(), _ir_with_port(prop_port))


def test_unrelated_port_change_does_not_wake_the_check():
    check = StpPolicyCheck()
    assert check.applies_to(_diff_with_description_only_change()) is False


def test_port_add_and_remove_wake_the_check():
    check = StpPolicyCheck()
    assert check.applies_to(_diff_with_port_added_carrying_policy()) is True
    # remove is the mirror case
    remove_diff = diff_ir(_ir_with_port(_base_port(stp_policy=StpPolicy(stp_p2p=True))),
                           _ir_no_port())
    assert check.applies_to(remove_diff) is True


def test_no_stp_policy_fixture_can_resolve_safe():
    # structural guard: every fixture in this module that changes stp_policy
    # must yield >=1 finding from this check (the floor makes SAFE impossible)
    for knob in ("stp_required", "stp_no_root_port", "stp_p2p", "use_vstp"):
        assert _run_flip(knob, True).findings, knob


def test_run_names_the_changed_knobs():
    result = _run_flip("stp_required", True)
    f = result.findings[0]
    assert "stp_required" in f.evidence["knobs"]
    assert "stp_required" in f.message
    assert f.subject is not None and f.subject.kind == "port" and f.subject.id == "S:ge-0/0/1"


def test_multiple_knobs_changed_together_are_all_named():
    base_port = _base_port()
    prop_port = dataclasses.replace(
        base_port, stp_policy=StpPolicy(stp_required=True, use_vstp=True))
    result = _run(_ir_with_port(base_port), _ir_with_port(prop_port))
    f = result.findings[0]
    assert set(f.evidence["knobs"]) == {"stp_required", "use_vstp"}


def test_no_change_is_silent():
    port = _base_port()
    assert _run(_ir_with_port(port), _ir_with_port(port)).findings == ()


def test_caused_by_points_at_the_changed_port():
    result = _run_flip("stp_p2p", True)
    f = result.findings[0]
    assert len(f.caused_by) > 0
    assert f.caused_by[0].ref.kind == "port"
    assert f.caused_by[0].ref.id == "S:ge-0/0/1"
