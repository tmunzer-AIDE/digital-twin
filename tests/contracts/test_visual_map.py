from digital_twin.contracts import (
    FindingRef,
    ObjectRef,
    Severity,
    VisualEntry,
    VisualMap,
    VisualTier,
    entity_key,
)


def test_entity_key_joins_kind_and_id():
    assert entity_key("device", "aabb01") == "device:aabb01"
    # id may itself contain colons; key is still split-on-first-colon recoverable
    assert entity_key("port", "aabb01:ge-0/0/1") == "port:aabb01:ge-0/0/1"


def test_visual_entry_is_frozen_and_carries_structured_kind_id():
    e = VisualEntry(
        kind="device", id="aabb01", tier=VisualTier.ORIGIN,
        severity=Severity.WARNING,
        findings=(FindingRef(index=0, code="t.x", subject=ObjectRef("vlan", "10")),),
    )
    assert e.tier is VisualTier.ORIGIN
    assert e.kind == "device" and e.id == "aabb01"
    assert e.findings[0].index == 0


def test_visual_map_alias_usable_as_nested_dict():
    m: VisualMap = {"l2": {"device:aabb01": VisualEntry(
        kind="device", id="aabb01", tier=VisualTier.AFFECTED,
        severity=Severity.ERROR, findings=(),
    )}}
    assert m["l2"]["device:aabb01"].severity is Severity.ERROR
