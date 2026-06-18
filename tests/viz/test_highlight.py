# tests/viz/test_highlight.py
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import Confidence, ConfidenceLevel, IRBuilder
from digital_twin.ir.entities import Device, DeviceRole, Port, PortMode, Vlan
from digital_twin.viz.highlight import build_highlight

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _ir():
    b = IRBuilder()
    b.add_device(Device(id="aabb01", role=DeviceRole.SWITCH, site="s1"))
    b.add_device(Device(id="aabb02", role=DeviceRole.SWITCH, site="s1"))
    b.add_port(Port(id="aabb01:ge-0/0/1", device_id="aabb01", name="ge-0/0/1",
                    mode=PortMode.ACCESS))
    b.add_vlan(Vlan(vlan_id=30, name="voice"))
    return b.build()


def _f(**kw):
    base = dict(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK, code="t.x",
        severity=Severity.ERROR, confidence=_HIGH, message="m",
    )
    return Finding(**{**base, **kw})


def test_additive_vlan_subject_also_highlights_device_nodes():
    # subject is the vlan; component_nodes are the broken devices — BOTH highlight
    f = _f(subject=ObjectRef("vlan", "30"), evidence={"vlan": 30, "component_nodes": ["aabb01"]})
    hl = build_highlight((f,), _ir())
    assert 30 in hl.vlans
    assert "aabb01" in hl.nodes


def test_worst_severity_wins_per_node():
    warn = _f(severity=Severity.WARNING, affected_entities=("aabb01",))
    err = _f(severity=Severity.ERROR, affected_entities=("aabb01",))
    hl = build_highlight((warn, err), _ir())
    assert hl.nodes["aabb01"].severity is Severity.ERROR


def test_mixed_severity_keeps_per_label_severity():
    # the node's CLASS uses the worst severity, but each label keeps ITS OWN
    # severity (so a WARNING caption is not relabelled error)
    warn = _f(severity=Severity.WARNING, code="w.x", affected_entities=("aabb01",))
    err = _f(severity=Severity.ERROR, code="e.x", affected_entities=("aabb01",))
    hit = build_highlight((warn, err), _ir()).nodes["aabb01"]
    assert hit.severity is Severity.ERROR
    assert {sev for sev, _ in hit.labels} == {Severity.WARNING, Severity.ERROR}


def test_port_and_link_resolve_to_device_nodes():
    port_f = _f(affected_entities=("aabb01:ge-0/0/1",))
    link_f = _f(affected_entities=("aabb01:ge-0/0/1__aabb02:ge-0/0/2",))
    hl = build_highlight((port_f, link_f), _ir())
    assert "aabb01" in hl.nodes and "aabb02" in hl.nodes


def test_mist_device_id_is_normalized_to_mac():
    f = _f(subject=ObjectRef("device", "00000000-0000-0000-1000-aabb01"))
    hl = build_highlight((f,), _ir())
    assert "aabb01" in hl.nodes


def test_gateway_mist_id_2000_normalized():
    # gateway Mist ids use the 2000 type tag — normalize generically, not just 1000
    f = _f(subject=ObjectRef("device", "00000000-0000-0000-2000-aabb01"))
    hl = build_highlight((f,), _ir())
    assert "aabb01" in hl.nodes


def test_caused_by_is_a_cause_line_not_a_highlight():
    f = _f(
        affected_entities=("aabb01",),
        caused_by=(Cause(ref=ObjectRef("link", "aabb02:p__aabb01:q"), fields=("native_vlan",)),),
    )
    hl = build_highlight((f,), _ir())
    assert "aabb02" not in hl.nodes  # cause is NOT highlighted
    assert any("native_vlan" in c for c in hl.causes)


def test_unlocalized_finding_is_counted():
    f = _f(subject=ObjectRef("dhcp_scope", "site:corp"))
    hl = build_highlight((f,), _ir())
    assert hl.unlocalized == 1


def test_same_entity_two_channels_no_duplicate_caption():
    # subject=vlan 30 AND evidence={"vlan": 30} both reference the same entity;
    # the caption label must appear exactly once in Hit.labels.
    f = _f(subject=ObjectRef("vlan", "30"), evidence={"vlan": 30})
    hl = build_highlight((f,), _ir())
    assert len(hl.vlans[30].labels) == 1
