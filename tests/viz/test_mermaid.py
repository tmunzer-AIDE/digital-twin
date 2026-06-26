# tests/viz/test_mermaid.py
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import (
    Device,
    DeviceRole,
    Link,
    LinkKind,
    Port,
    PortMode,
    Vlan,
    link_id,
)
from digital_twin.viz.mermaid import build_diagrams, safe_build_diagrams

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _ir():
    b = IRBuilder()
    b.add_device(Device(id="aabb01", role=DeviceRole.SWITCH, site="s1", name="core-1"))
    b.add_device(Device(id="aabb02", role=DeviceRole.SWITCH, site="s1", name="idf-3"))
    b.add_port(Port(id="aabb01:ge-0/0/1", device_id="aabb01", name="ge-0/0/1",
                    mode=PortMode.TRUNK, tagged_vlans=(30,)))
    b.add_port(Port(id="aabb02:ge-0/0/1", device_id="aabb02", name="ge-0/0/1",
                    mode=PortMode.TRUNK, tagged_vlans=(30,)))
    b.add_link(Link(id=link_id("aabb01:ge-0/0/1", "aabb02:ge-0/0/1"),
                    a_port="aabb01:ge-0/0/1", b_port="aabb02:ge-0/0/1", kind=LinkKind.PHYSICAL))
    b.add_vlan(Vlan(vlan_id=20, name="data"))   # unaffected
    b.add_vlan(Vlan(vlan_id=30, name="voice"))  # affected by the test finding
    b.add_vlan(Vlan(vlan_id=100, name="iot"))   # unaffected; numeric-sort guard
    return b.build()


def _two_switch_vlan_ir_for_mermaid():
    """s1 and s2 connected by a trunk carrying BOTH vlan 10 and vlan 20,
    PLUS each has an access port for their respective vlan.  s1 therefore
    appears in BOTH vlan 10 and vlan 20 mermaid graphs — required so a
    bleed bug (s1 mis-classed on vlan:20 when only vlan:10 was hit) is
    actually detectable."""
    b = IRBuilder()
    b.add_device(Device(id="s1", role=DeviceRole.SWITCH, site="site1", name="sw1"))
    b.add_device(Device(id="s2", role=DeviceRole.SWITCH, site="site1", name="sw2"))
    # trunk carrying both vlan 10 and vlan 20 between s1 and s2
    b.add_port(Port(id="s1:ge-0/0/0", device_id="s1", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10, 20)))
    b.add_port(Port(id="s2:ge-0/0/0", device_id="s2", name="ge-0/0/0",
                    mode=PortMode.TRUNK, tagged_vlans=(10, 20)))
    b.add_link(Link(id=link_id("s1:ge-0/0/0", "s2:ge-0/0/0"),
                    a_port="s1:ge-0/0/0", b_port="s2:ge-0/0/0", kind=LinkKind.PHYSICAL))
    # s1 access port for vlan 10
    b.add_port(Port(id="s1:ge-0/0/1", device_id="s1", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10))
    # s2 access port for vlan 20
    b.add_port(Port(id="s2:ge-0/0/1", device_id="s2", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=20))
    b.add_vlan(Vlan(vlan_id=10, name="data"))
    b.add_vlan(Vlan(vlan_id=20, name="voice"))
    return b.build()


def _f(**kw):
    base = dict(source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="t.x",
                severity=Severity.ERROR, confidence=_HIGH, message="boom")
    return Finding(**{**base, **kw})


def test_vlan_chart_does_not_inherit_other_vlans_node_hits():
    ir = _two_switch_vlan_ir_for_mermaid()
    # a finding scoped to vlan 10 hitting s1
    f = _f(subject=ObjectRef("vlan", "10"),
           evidence={"vlan": 10, "component_nodes": ["s1"]})
    diagrams = build_diagrams(ir, ir, (f,))
    v20 = next(d for d in diagrams if d.view == "vlan:20")
    # s1 must NOT be classed on the vlan:20 chart
    assert " class " not in v20.mermaid or "warn" not in v20.mermaid.split("class", 1)[1]
    v10 = next(d for d in diagrams if d.view == "vlan:10")
    assert "warn" in v10.mermaid or "origin" in v10.mermaid


def test_l2_chart_present_and_well_formed():
    diagrams = build_diagrams(_ir(), _ir(), ())
    l2 = next(d for d in diagrams if d.view == "l2")
    assert l2.mermaid.startswith("graph LR")
    assert "classDef" in l2.mermaid
    assert "core-1" in l2.mermaid  # device display name in a label


def test_l2_highlights_affected_device():
    diagrams = build_diagrams(_ir(), _ir(), (_f(affected_entities=("aabb01",)),))
    l2 = next(d for d in diagrams if d.view == "l2")
    assert "class " in l2.mermaid and ":::" not in l2.mermaid  # uses `class n crit;` form
    assert l2.severity is Severity.ERROR


def test_every_class_target_node_is_declared():
    # structural invariant: no `class nX` line references an undeclared node id
    diagrams = build_diagrams(_ir(), _ir(), (_f(affected_entities=("aabb01",)),))
    for d in diagrams:
        declared = {
            ln.split("[")[0].strip().rstrip("(")
            for ln in d.mermaid.splitlines() if "[" in ln
        }
        for ln in d.mermaid.splitlines():
            if ln.strip().startswith("class "):
                body = ln.strip()[len("class "):].rstrip(";")  # "n0,n1 crit"
                targets, _cls = body.rsplit(" ", 1)
                for nid in targets.split(","):
                    assert nid.strip() in declared, f"{nid} not declared in {d.view}"


def test_safe_build_diagrams_swallows_errors(monkeypatch):
    import digital_twin.viz.mermaid as m

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "build_diagrams", _boom)
    assert safe_build_diagrams(_ir(), _ir(), ()) == ()


def test_causes_appear_in_l2_notes():
    from digital_twin.contracts import Cause, ObjectRef
    f = _f(affected_entities=("aabb01",),
           caused_by=(Cause(ref=ObjectRef("link", "aabb02:p__aabb01:q"), fields=("native_vlan",)),))
    l2 = next(d for d in build_diagrams(_ir(), _ir(), (f,)) if d.view == "l2")
    assert any("native_vlan" in n for n in l2.notes)  # cause is a visible caption, not %%


def test_mixed_severity_labels_render_with_own_severity():
    warn = _f(severity=Severity.WARNING, code="w.x", affected_entities=("aabb01",))
    err = _f(severity=Severity.ERROR, code="e.x", affected_entities=("aabb01",))
    l2 = next(d for d in build_diagrams(_ir(), _ir(), (warn, err)) if d.view == "l2")
    assert any(n.startswith("warning: w.x") for n in l2.notes)  # warn label kept as warning
    assert any(n.startswith("error: e.x") for n in l2.notes)


def test_per_vlan_chart_emitted_and_affected_first():
    f = _f(evidence={"vlan": 30, "component_nodes": ["aabb01"]})
    diagrams = build_diagrams(_ir(), _ir(), (f,))
    vlan_order = [d.view for d in diagrams if d.view.startswith("vlan:")]
    assert {"vlan:20", "vlan:30", "vlan:100"} <= set(vlan_order)
    assert vlan_order[0] == "vlan:30"  # the affected VLAN sorts before ALL unaffected
    # unaffected VLANs follow in NUMERIC order (vlan:100 must NOT precede vlan:20)
    assert vlan_order.index("vlan:20") < vlan_order.index("vlan:100")
    v30 = next(d for d in diagrams if d.view == "vlan:30")
    assert v30.severity is Severity.ERROR


def test_vlan_subject_label_appears_in_chart_notes():
    # a pure vlan-subject finding (no node) must still show its code+reason caption
    v30 = next(
        d for d in build_diagrams(_ir(), _ir(), (_f(subject=ObjectRef("vlan", "30")),))
        if d.view == "vlan:30"
    )
    assert any("t.x" in n for n in v30.notes)


def test_l3_exits_chart_includes_gateway_role_interface():
    from digital_twin.ir.entities import L3Intf, L3Role, Vlan

    ir = (
        IRBuilder()
        .add_device(Device(id="gw01", role=DeviceRole.GATEWAY, site="s1", name="srx"))
        .add_vlan(Vlan(vlan_id=2, name="mgmt"))  # subnet-less, but has an l3intf
        .add_l3intf(L3Intf(device_id="gw01", role=L3Role.GATEWAY, vlan_id=2))
        .build()
    )
    diagrams = build_diagrams(ir, ir, ())
    l3 = next(d for d in diagrams if d.view == "l3_exits")
    assert "VLAN 2" in l3.mermaid
    assert "srx" in l3.mermaid  # gateway-role interface present


def test_l3_exits_highlights_affected_gateway_device():
    from digital_twin.ir.entities import L3Intf, L3Role, Vlan

    ir = (
        IRBuilder()
        .add_device(Device(id="gw01", role=DeviceRole.GATEWAY, site="s1", name="srx"))
        .add_vlan(Vlan(vlan_id=2, name="mgmt"))
        .add_l3intf(L3Intf(device_id="gw01", role=L3Role.GATEWAY, vlan_id=2))
        .build()
    )
    l3 = next(
        d for d in build_diagrams(ir, ir, (_f(affected_entities=("gw01",)),))
        if d.view == "l3_exits"
    )
    assert "class " in l3.mermaid  # the interface node is classed via its owning device
    assert l3.severity is Severity.ERROR
    assert any("t.x" in n for n in l3.notes)  # the device-finding caption shows on L3


def test_per_vlan_diagram_is_deterministic_across_hash_seeds():
    """_vlan_diagram must produce byte-identical output for PYTHONHASHSEED=1 vs =2.

    The test builds the mermaid string for vlan:30 in two subprocesses that each
    have a different PYTHONHASHSEED, then asserts the outputs are identical.  A
    graph with 3 devices on the VLAN (plus cross-links) makes node-ordering
    observable when iteration is over a raw Python ``set``.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    # repo root derived from this file (.../tests/viz/test_mermaid.py -> root) so the
    # subprocess imports the checkout under test, hermetically (no hard-coded path).
    root = Path(__file__).resolve().parents[2]

    # Snippet run in each subprocess: build a 3-device VLAN graph and print the
    # vlan:30 mermaid string to stdout.
    SNIPPET = """
import sys
sys.path.insert(0, "src")
from digital_twin.ir import IRBuilder
from digital_twin.ir.entities import (
    Device, DeviceRole, Link, LinkKind,
    Port, PortMode, Vlan, link_id,
)
from digital_twin.viz.mermaid import build_diagrams

b = IRBuilder()
b.add_device(Device(id="sw01", role=DeviceRole.SWITCH, site="s1", name="core-sw01"))
b.add_device(Device(id="sw02", role=DeviceRole.SWITCH, site="s1", name="idf-sw02"))
b.add_device(Device(id="sw03", role=DeviceRole.SWITCH, site="s1", name="idf-sw03"))
# VLAN 30 trunked on all three pairs of links — explicit ids keep lines short
links = [
    ("sw01:ge-0/0/1", "sw02:ge-0/0/1"),
    ("sw01:ge-0/0/2", "sw03:ge-0/0/1"),
    ("sw02:ge-0/0/2", "sw03:ge-0/0/2"),
]
for pa, pb in links:
    dev_a = pa.split(":")[0]
    dev_b = pb.split(":")[0]
    b.add_port(Port(
        id=pa, device_id=dev_a, name=pa.split(":")[1],
        mode=PortMode.TRUNK, tagged_vlans=(30,),
    ))
    b.add_port(Port(
        id=pb, device_id=dev_b, name=pb.split(":")[1],
        mode=PortMode.TRUNK, tagged_vlans=(30,),
    ))
    b.add_link(Link(
        id=link_id(pa, pb), a_port=pa, b_port=pb, kind=LinkKind.PHYSICAL,
    ))
b.add_vlan(Vlan(vlan_id=30, name="voice"))
ir = b.build()
diagrams = build_diagrams(ir, ir, ())
v30 = next(d for d in diagrams if d.view == "vlan:30")
print(v30.mermaid)
"""

    env_base = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}

    result1 = subprocess.run(
        [sys.executable, "-c", SNIPPET],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**env_base, "PYTHONHASHSEED": "1"},
    )
    result2 = subprocess.run(
        [sys.executable, "-c", SNIPPET],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**env_base, "PYTHONHASHSEED": "2"},
    )

    assert result1.returncode == 0, f"seed=1 subprocess failed:\n{result1.stderr}"
    assert result2.returncode == 0, f"seed=2 subprocess failed:\n{result2.stderr}"
    assert result1.stdout == result2.stdout, (
        "vlan:30 mermaid differs between PYTHONHASHSEED=1 and PYTHONHASHSEED=2\n"
        f"--- seed=1 ---\n{result1.stdout}\n--- seed=2 ---\n{result2.stdout}"
    )
