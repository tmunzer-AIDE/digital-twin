"""Central subject name-resolution (checks/subjects.py)."""

from digital_twin.checks.subjects import name_findings, resolve_subject
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import Device, DeviceRole, Port, PortMode, Vlan

_DID = "aabbccddee00"
_PID = f"{_DID}:ge-0/0/0"


def _ir():
    b = IRBuilder()
    b.add_device(Device(id=_DID, role=DeviceRole.SWITCH, site="s1", model="EX4100-48P"))
    b.add_port(Port(id=_PID, device_id=_DID, name="ge-0/0/0", mode=PortMode.ACCESS))
    b.add_vlan(Vlan(vlan_id=10, name="corp"))
    return b.build()


def test_resolve_fills_names_from_ir():
    ir = _ir()
    assert resolve_subject(ObjectRef("vlan", "10"), ir, ir).name == "corp"
    assert resolve_subject(ObjectRef("port", _PID), ir, ir).name == "ge-0/0/0"


def test_resolve_device_uses_device_name():
    # now that the IR has Device.name, device subjects resolve to it (not model)
    ir = _ir()  # _ir()'s device has model "EX4100-48P" and NO name yet
    assert resolve_subject(ObjectRef("device", _DID), ir, ir).name is None  # no name set


def test_resolve_device_name_when_present():
    from digital_twin.ir import IRBuilder
    from digital_twin.ir.entities import Device, DeviceRole

    ir = IRBuilder().add_device(
        Device(id=_DID, role=DeviceRole.SWITCH, site="s1", model="EX4100-48P", name="core-1")
    ).build()
    assert resolve_subject(ObjectRef("device", _DID), ir, ir).name == "core-1"


def test_resolve_unknown_or_nameless_stays_none():
    ir = _ir()
    assert resolve_subject(ObjectRef("dhcp_scope", "site:corp"), ir, ir).name is None
    assert resolve_subject(ObjectRef("vlan", "999"), ir, ir).name is None  # absent vlan
    assert resolve_subject(ObjectRef("vlan", "not-an-int"), ir, ir).name is None
    assert resolve_subject(None, ir, ir) is None


def test_resolve_already_named_is_left_unchanged():
    ir = _ir()
    ref = ObjectRef("vlan", "10", name="explicit")
    assert resolve_subject(ref, ir, ir) is ref


def test_resolve_falls_back_to_baseline_for_removed_entity():
    # entity gone from the proposed IR (removal) still resolves via baseline
    base, empty = _ir(), IRBuilder().build()
    assert resolve_subject(ObjectRef("vlan", "10"), empty, base).name == "corp"


def test_name_findings_stamps_each_finding():
    ir = _ir()
    f = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="t.x",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="m",
        subject=ObjectRef("vlan", "10"),
    )
    (named,) = name_findings((f,), ir, ir)
    assert named.subject is not None and named.subject.name == "corp"
