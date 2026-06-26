"""wired.port.unmodeled_change: inter_switch_link/storm_control/enable_qos changes
are recognized and floored to REVIEW (impact not modeled). Never SAFE/UNSAFE."""
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.unmodeled_change import PortUnmodeledChangeCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
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


def test_enable_qos_change_is_review():
    r = _run(_ir(None), _ir(PortMisc(enable_qos=True)))
    assert r.status is Status.WARN
    assert r.findings[0].code == "wired.port.unmodeled_change.recognized"
    assert r.findings[0].severity is Severity.WARNING


def test_no_change_is_silent():
    assert _run(_ir(None), _ir(None)).findings == ()
    assert _run(_ir(PortMisc(enable_qos=True)), _ir(PortMisc(enable_qos=True))).findings == ()
