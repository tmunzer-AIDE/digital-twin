# tests/viz/test_mermaid.py
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import (
    Device, DeviceRole, Link, LinkKind, Port, PortMode, Vlan, link_id,
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


def _f(**kw):
    base = dict(source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="t.x",
                severity=Severity.ERROR, confidence=_HIGH, message="boom")
    return Finding(**{**base, **kw})


def test_l2_chart_present_and_well_formed():
    diagrams = build_diagrams(_ir(), ())
    l2 = next(d for d in diagrams if d.view == "l2")
    assert l2.mermaid.startswith("graph LR")
    assert "classDef" in l2.mermaid
    assert "core-1" in l2.mermaid  # device display name in a label


def test_l2_highlights_affected_device():
    diagrams = build_diagrams(_ir(), (_f(affected_entities=("aabb01",)),))
    l2 = next(d for d in diagrams if d.view == "l2")
    assert "class " in l2.mermaid and ":::" not in l2.mermaid  # uses `class n crit;` form
    assert l2.severity is Severity.ERROR


def test_every_class_target_node_is_declared():
    # structural invariant: no `class nX` line references an undeclared node id
    diagrams = build_diagrams(_ir(), (_f(affected_entities=("aabb01",)),))
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
    assert safe_build_diagrams(_ir(), ()) == ()


def test_causes_appear_in_l2_notes():
    from digital_twin.contracts import Cause, ObjectRef
    f = _f(affected_entities=("aabb01",),
           caused_by=(Cause(ref=ObjectRef("link", "aabb02:p__aabb01:q"), fields=("native_vlan",)),))
    l2 = next(d for d in build_diagrams(_ir(), (f,)) if d.view == "l2")
    assert any("native_vlan" in n for n in l2.notes)  # cause is a visible caption, not %%


def test_mixed_severity_labels_render_with_own_severity():
    warn = _f(severity=Severity.WARNING, code="w.x", affected_entities=("aabb01",))
    err = _f(severity=Severity.ERROR, code="e.x", affected_entities=("aabb01",))
    l2 = next(d for d in build_diagrams(_ir(), (warn, err)) if d.view == "l2")
    assert any(n.startswith("warning: w.x") for n in l2.notes)  # warn label kept as warning
    assert any(n.startswith("error: e.x") for n in l2.notes)


def test_per_vlan_chart_emitted_and_affected_first():
    diagrams = build_diagrams(_ir(), (_f(evidence={"vlan": 30, "component_nodes": ["aabb01"]}),))
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
        d for d in build_diagrams(_ir(), (_f(subject=ObjectRef("vlan", "30")),))
        if d.view == "vlan:30"
    )
    assert any("t.x" in n for n in v30.notes)
