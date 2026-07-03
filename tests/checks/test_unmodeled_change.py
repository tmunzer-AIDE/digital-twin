"""wired.port.unmodeled_change: inter_switch_link/storm_control/the Spec-1
reviewed knobs changed are recognized and floored to REVIEW (impact not
modeled). Never SAFE/UNSAFE. enable_qos left this surface in Spec 1 (benign)."""
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.unmodeled_change import PortUnmodeledChangeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.entities import PortMisc
from tests.factories import sw


def _ir(misc):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10, misc=misc))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return PortUnmodeledChangeCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def _run_with_misc_flip(knob, value):
    return _run(_ir(None), _ir(PortMisc(**{knob: value})))


def test_each_new_reviewed_knob_is_review():
    # poe_priority (str), community_vlan_id (int), and the PVLAN boolean each
    # wake the recognized-but-unmodeled REVIEW carrier (Spec 1). The four STP
    # knobs (stp_required, stp_no_root_port, stp_p2p, use_vstp) graduated to
    # StpPolicy in Spec 2 — they no longer flow through PortMisc/this check.
    cases = [
        ("poe_priority", "high"),
        ("community_vlan_id", 811),
        ("inter_isolation_network_link", True),
    ]
    for knob, value in cases:
        result = _run_with_misc_flip(knob, value)
        f = result.findings[0]
        assert f.code == "wired.port.unmodeled_change.recognized", knob
        assert f.severity is Severity.WARNING, knob
        assert f.confidence.level is ConfidenceLevel.MEDIUM, knob
        assert knob in f.evidence["knobs"], knob


def test_enable_qos_no_longer_wakes_the_check():
    # Spec 1 moved enable_qos to the benign SAFE group: it must not enter
    # PortMisc, so an enable_qos-only delta produces NO unmodeled_change
    # finding. (Replaces test_enable_qos_change_is_review; the SAFE end-to-end
    # lives in the pipeline suite.)
    from digital_twin.adapters.mist.ingest.switch import _port_misc

    result = _run(_ir(None), _ir(_port_misc({"enable_qos": True})))
    assert result.status is Status.PASS and not result.findings


def test_inter_switch_link_change_is_review():
    r = _run(_ir(None), _ir(PortMisc(inter_switch_link=True)))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.port.unmodeled_change.recognized"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM  # unmodeled impact, never HIGH
    assert f.evidence["knobs"] == ["inter_switch_link"]


def test_storm_control_change_is_review():
    r = _run(_ir(PortMisc(storm_control="pct:80")), _ir(PortMisc(storm_control="pct:50")))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.port.unmodeled_change.recognized"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM
    assert f.evidence["knobs"] == ["storm_control"]


def test_misc_object_flip_without_recognized_knob_is_silent():
    # None vs all-default PortMisc(): the misc OBJECTS differ, but no recognized
    # knob changed -> the `continue` guard keeps the check silent (no phantom
    # REVIEW off a representation-only difference)
    r = _run(_ir(None), _ir(PortMisc()))
    assert r.status is Status.PASS
    assert r.findings == ()
    assert r.confidence.level is ConfidenceLevel.HIGH


def test_no_change_is_silent():
    assert _run(_ir(None), _ir(None)).findings == ()
    assert _run(
        _ir(PortMisc(inter_switch_link=True)), _ir(PortMisc(inter_switch_link=True))
    ).findings == ()
