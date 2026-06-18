from digital_twin.analysis.delta_cause import delta_index
from digital_twin.ir.diff import EntityRef, IRDiff, Modified


def _diff(added=(), removed=(), modified=()):
    return IRDiff(tuple(added), tuple(removed), tuple(modified))


def test_modified_entity_yields_cause_with_fields():
    di = delta_index(_diff(modified=(Modified(EntityRef("port", "p1"), ("native_vlan",)),)))
    c = di.cause("port", "p1")
    assert c is not None
    assert c.ref.kind == "port" and c.ref.id == "p1" and c.fields == ("native_vlan",)


def test_added_and_removed_have_empty_fields():
    di = delta_index(_diff(added=(EntityRef("l3intf", "x"),), removed=(EntityRef("link", "l9"),)))
    assert di.cause("l3intf", "x").fields == ()
    assert di.cause("link", "l9").fields == ()


def test_unchanged_entity_yields_none():
    di = delta_index(_diff())
    assert di.cause("port", "nope") is None


def test_kinds_query_helper():
    di = delta_index(_diff(modified=(Modified(EntityRef("port", "p1"), ("poe",)),)))
    assert di.in_delta("port", "p1") and not di.in_delta("device", "p1")


def test_causes_maps_iterable_filtering_to_delta():
    di = delta_index(_diff(modified=(Modified(EntityRef("port", "p1"), ("poe",)),)))
    out = di.causes("port", ["p1", "p2"])  # p2 not in delta -> dropped
    assert tuple(c.ref.id for c in out) == ("p1",)
