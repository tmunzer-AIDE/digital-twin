"""name_findings resolves Cause.ref names for top-level and nested impact causes."""

from digital_twin.checks.subjects import name_findings
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import Device, DeviceRole, Port, PortMode


def _ir_with_named_port(port_id: str, name: str):
    """Minimal IR with one device and one named port.

    The IR requires the canonical port id form ``{device_id}:{name}``, so the
    caller must pass a port_id whose suffix matches `name`.
    """
    b = IRBuilder()
    b.add_device(Device(id="dev1", role=DeviceRole.SWITCH, site="s1"))
    b.add_port(Port(id=port_id, device_id="dev1", name=name, mode=PortMode.TRUNK))
    return b.build()


_PORT_NAME = "uplink-1"
_PORT_ID = f"dev1:{_PORT_NAME}"


def _f(**kw):
    base = dict(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="x",
        severity=Severity.WARNING, confidence=Confidence(level=ConfidenceLevel.HIGH), message="m",
    )
    base.update(kw)
    return Finding(**base)


def test_top_level_cause_ref_gets_named():
    ir = _ir_with_named_port(_PORT_ID, _PORT_NAME)
    f = _f(caused_by=(Cause(ref=ObjectRef("port", _PORT_ID)),))
    named = name_findings((f,), ir, ir)
    assert named[0].caused_by[0].ref.name == _PORT_NAME


def test_nested_impacts_cause_ref_gets_named():
    ir = _ir_with_named_port(_PORT_ID, _PORT_NAME)
    cause = Cause(ref=ObjectRef("port", _PORT_ID))
    f = _f(evidence={"impacts": [{"mac": "aa", "caused_by": [cause]}]})
    named = name_findings((f,), ir, ir)
    assert named[0].evidence["impacts"][0]["caused_by"][0].ref.name == _PORT_NAME


def test_device_cause_ref_stays_unnamed():
    ir = _ir_with_named_port(_PORT_ID, _PORT_NAME)
    f = _f(caused_by=(Cause(ref=ObjectRef("device", "dev1")),))
    named = name_findings((f,), ir, ir)
    assert named[0].caused_by[0].ref.name is None
