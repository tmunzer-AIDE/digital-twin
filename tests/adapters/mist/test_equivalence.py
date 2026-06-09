import pytest

from digital_twin.adapters.mist.compile.equivalence import (
    attribute_coverage,
    compare_effective,
)


def test_identical_configs_have_no_diffs():
    cfg = {"networks": {"corp": {"vlan_id": 10}}}
    assert compare_effective(ours=cfg, derived=cfg).diffs == ()


def test_numeric_string_normalization():
    r = compare_effective(ours={"v": "100"}, derived={"v": 100})
    assert r.diffs == ()


def test_absent_vs_null_vs_empty_are_equal():
    r = compare_effective(ours={"a": None, "b": {}}, derived={})
    assert r.diffs == ()


def test_real_difference_reported_with_path():
    r = compare_effective(
        ours={"networks": {"corp": {"vlan_id": 10}}},
        derived={"networks": {"corp": {"vlan_id": 11}}},
    )
    assert [d.path for d in r.diffs] == ["networks.corp.vlan_id"]


def test_catalogued_divergence_is_separated_not_failed():
    r = compare_effective(ours={"x": 1}, derived={"x": 2}, catalogued=("x",))
    assert r.diffs == () and [d.path for d in r.catalogued_diffs] == ["x"]


def test_divergence_entries_without_reason_are_rejected(tmp_path, monkeypatch):
    import digital_twin.adapters.mist.compile.equivalence as eq

    bad = tmp_path / "divergences.json"
    bad.write_text('{"entries": [{"path": "x"}]}')
    monkeypatch.setattr(eq, "_DIVERGENCES", bad)

    with pytest.raises(ValueError, match="without a reason"):
        eq.load_catalogued()


def test_attribute_coverage_lists_exercised_and_missing_leaves():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "object", "properties": {"c": {"type": "integer"}}},
            "d": {"type": "string"},
        },
    }
    cov = attribute_coverage(schema, [{"a": "x", "b": {"c": 1}}])
    assert "a" in cov.covered and "b.c" in cov.covered
    assert "d" in cov.uncovered


def test_attribute_coverage_sees_leaves_behind_composition():
    # same normalization as the Tier-1 generator: anyOf/allOf leaves still count
    schema = {
        "type": "object",
        "properties": {
            "vlan": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            "extra": {"allOf": [{"type": "object", "properties": {"x": {"type": "string"}}}]},
        },
    }
    cov = attribute_coverage(schema, [{"vlan": 30}])
    assert "vlan" in cov.covered
    assert "extra.x" in cov.uncovered  # visible despite the allOf wrapper
