from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from digital_twin.behavioral import (
    Action,
    Batch,
    Coverage,
    InputWindow,
    ObjectKey,
    Operation,
    Receipt,
    Record,
    Snapshot,
    Support,
)
from digital_twin.behavioral.coverage import paths


def record(body=None, *, site="a", identity="switch"):
    return Record.create(ObjectKey("org", "device_switch", identity, site), body or {"vlan": 10})


def receipt(source, path, **kwargs):
    return Receipt.bind(source, path, model="test-grammar", evidence=("unit-case",), **kwargs)


def test_snapshot_body_is_detached_and_revisions_are_canonical():
    body = {"port_config": {"ge-0/0/1": {"usage": "ap"}}, "id": "switch"}
    before = Record.create(ObjectKey("org", "device_switch", "switch", "a"), body)
    snapshot = Snapshot("org", [before])
    revision = snapshot.revision
    body["port_config"]["ge-0/0/1"]["usage"] = "different"
    detached = before.body()
    detached["port_config"].clear()
    assert before.body()["port_config"]["ge-0/0/1"]["usage"] == "ap"
    assert snapshot.revision == revision
    assert (
        Record.create(before.key, dict(reversed(before.body().items()))).revision == before.revision
    )
    assert replace(before, platform="EX4400").revision == before.revision
    assert Snapshot("org", (replace(before, platform="EX4400"),)).revision != revision


@pytest.mark.parametrize("field", ["source", "schema_version", "platform", "release"])
def test_mutable_provenance_cannot_enter_an_immutable_record(field):
    with pytest.raises(ValueError, match="immutable types"):
        replace(record(), **{field: ["mutable"]})


def test_root_replacement_deletion_and_null_are_distinct():
    before = record({"networks": {"old": {"vlan_id": 10}}, "keep": [], "delete": 42})
    baseline = Snapshot("org", (before,))
    op = Operation.update(before, {"networks": {"new": {"vlan_id": 20}}, "-delete": "", "n": None})
    result = Batch(baseline.revision, (op,)).apply(baseline).get(before.key)
    assert result.body() == {"networks": {"new": {"vlan_id": 20}}, "keep": [], "n": None}
    assert baseline.get(before.key) == before


@pytest.mark.parametrize("payload", [{"-vlan": 1}, {"-vlan": "", "vlan": 20}, {"-": ""}])
def test_bad_delete_markers_fail_atomically(payload):
    before = record()
    baseline = Snapshot("org", (before,))
    with pytest.raises(ValueError):
        Batch(baseline.revision, (Operation.update(before, payload),)).apply(baseline)
    assert baseline.get(before.key).body() == {"vlan": 10}


def test_repeated_updates_require_rolling_revisions():
    before = record()
    baseline = Snapshot("org", (before,))
    first = Operation.update(before, {"vlan": 20})
    middle = Batch(baseline.revision, (first,)).apply(baseline).get(before.key)
    second = Operation.update(middle, {"vlan": 30})
    assert Batch(baseline.revision, (first, second)).apply(baseline).get(before.key).body() == {
        "vlan": 30
    }
    with pytest.raises(ValueError, match="revision"):
        Batch(baseline.revision, (first, Operation.update(before, {"vlan": 30}))).apply(baseline)
    with pytest.raises(ValueError, match="snapshot revision"):
        Batch("outdated", (first,)).apply(baseline)


def test_create_delete_preserve_provenance_and_site_namespace():
    a, b = record(), record(site="b")
    baseline = Snapshot("org", (a,))
    new = replace(b, source="reference", schema_version="fixture-1", platform="EX4400")
    result = Batch(baseline.revision, (Operation.create(new),)).apply(baseline)
    assert result.get(a.key) == a
    assert result.get(b.key) == new
    result = Batch(result.revision, (Operation(a.key, Action.DELETE, a.revision),)).apply(result)
    with pytest.raises(KeyError):
        result.get(a.key)
    assert result.get(b.key) == new
    with pytest.raises(ValueError, match="already exists"):
        Batch(baseline.revision, (Operation.create(a),)).apply(baseline)
    with pytest.raises(ValueError, match="provenance"):
        Operation(b.key, Action.CREATE, None, b.body_json)


def test_duplicate_and_cross_org_objects_are_rejected():
    before = record()
    with pytest.raises(ValueError, match="duplicate"):
        Snapshot("org", (before, before))
    foreign = replace(before, key=replace(before.key, org_id="foreign"))
    with pytest.raises(ValueError, match="different organization"):
        Snapshot("org", (foreign,))
    baseline = Snapshot("org", (before,))
    with pytest.raises(ValueError, match="different organization"):
        Batch(baseline.revision, (Operation.create(foreign),)).apply(baseline)


def test_structural_paths_include_empty_and_null_nodes():
    assert set(paths({"a": {}, "b": [], "c": None, "d": [{"v": 2}, {}]})) == {
        ("a",),
        ("b",),
        ("c",),
        ("d", "0", "v"),
        ("d", "1"),
    }
    assert list(paths({})) == [()]
    before = record({"supported": 1, "new": {}})
    coverage = Coverage((receipt(before, ("supported",)),))
    assert [
        (g.code, g.path) for g in coverage.inspect(Snapshot("org", (before,)), (before.key,))
    ] == [("unconsumed", ("new",))]


@pytest.mark.parametrize(
    "field", ["source_revision", "source", "schema_version", "platform", "release"]
)
def test_receipts_are_bound_to_configuration_and_platform(field):
    before = record()
    bound = replace(receipt(before, ("vlan",)), **{field: "different"})
    assert Coverage((bound,)).inspect(Snapshot("org", (before,)), (before.key,))[0].code == (
        "invalid_receipt"
    )


def test_opaque_noninterference_and_ambiguity_cannot_establish_coverage():
    before = record()
    snapshot = Snapshot("org", (before,))
    opaque = receipt(before, ("vlan",), support=Support.OPAQUE, reason="unknown enum")
    assert Coverage((opaque,)).inspect(snapshot, (before.key,))[0].code == "opaque"
    unsupported_exclusion = receipt(before, ("vlan",), support=Support.NON_INTERFERING)
    assert Coverage((unsupported_exclusion,)).inspect(snapshot, (before.key,))[0].code == (
        "invalid_receipt"
    )
    valid = receipt(before, ("vlan",))
    assert Coverage((valid, valid)).inspect(snapshot, (before.key,))[0].code == "ambiguous_receipt"


def test_dependencies_close_across_sites_and_cycles_terminate():
    a, b = record(), record(site="b")
    ra = receipt(a, ("vlan",), dependencies=(b.key,))
    rb = receipt(b, ("vlan",), dependencies=(a.key,))
    assert not Coverage((ra, rb)).inspect(Snapshot("org", (a, b)), (a.key,))
    gaps = Coverage((ra,)).inspect(Snapshot("org", (a,)), (a.key,))
    assert [(g.code, g.key) for g in gaps] == [("missing_object", b.key)]
    assert Coverage((ra, rb)).dependency_closure((a.key,)) == tuple(sorted((a.key, b.key)))


def test_incomplete_stale_and_future_capture_windows():
    before = record()
    at = datetime(2026, 10, 1, tzinfo=UTC)
    window = InputWindow("site:a", at, at, False, "wlans")
    snapshot = Snapshot("org", (before,), (window,))
    coverage = Coverage((receipt(before, ("vlan",)),))
    assert coverage.inspect(snapshot, (before.key,))[0].code == "incomplete_input"
    for clock in (at - timedelta(seconds=1), at + timedelta(hours=1)):
        assert "stale_input" in {
            g.code
            for g in coverage.inspect(
                snapshot, (before.key,), at=clock, max_age=timedelta(minutes=1)
            )
        }
    with pytest.raises(ValueError, match="both"):
        coverage.inspect(snapshot, (before.key,), at=at)
    with pytest.raises(ValueError, match="aware"):
        InputWindow("site:a", at.replace(tzinfo=None), at, True)
