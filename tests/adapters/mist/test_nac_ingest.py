from digital_twin.adapters.mist.ingest.nac import build_nac_ir
from digital_twin.contracts import FindingCategory, FindingSource, Severity


def _rule(**kw):
    base = {"id": "r1", "name": "n", "order": 1, "enabled": True, "action": "allow",
            "matching": {}, "apply_tags": []}
    base.update(kw)
    return base


def test_full_matching_surface_mapped():
    ir, findings = build_nac_ir([_rule(matching={
        "auth_type": "cert", "port_types": ["wireless"], "nactags": ["t1", "t2"],
        "family": ["x"]})], [])
    r = ir.nacrules[0]
    assert r.auth_types == frozenset({"cert"})
    assert r.port_types == frozenset({"wireless"})
    assert r.match_tags == frozenset({"t1", "t2"})
    assert r.family == frozenset({"x"})
    assert r.opaque_digest is None and findings == ()


def test_absent_enabled_defaults_true():
    rule = _rule()
    del rule["enabled"]
    ir, _ = build_nac_ir([rule], [])
    assert ir.nacrules[0].enabled is True


def test_non_bool_enabled_sets_opaque_and_warns():
    ir, findings = build_nac_ir([_rule(enabled="yes")], [])
    assert ir.nacrules[0].opaque_digest is not None
    f = findings[0]
    assert (f.source is FindingSource.ADAPTER and f.category is FindingCategory.OPERATIONAL
            and f.severity is Severity.WARNING)


def test_unparseable_proof_field_is_opaque_not_empty():
    ir, findings = build_nac_ir([_rule(matching={"auth_type": {"bad": "shape"}})], [])
    r = ir.nacrules[0]
    assert r.opaque_digest is not None        # NOT collapsed to ∅
    assert r.auth_types == frozenset()        # best-effort empty, but opaque guards it
    assert findings and findings[0].severity is Severity.WARNING


def test_falsy_malformed_matching_block_is_opaque_not_catch_all():
    # matching=[] is present-but-malformed, NOT absent — must NOT become a clean catch-all
    # (the old `matching or {}` masked it). Same for not_matching=[].
    ir, findings = build_nac_ir([_rule(matching=[])], [])
    assert ir.nacrules[0].opaque_digest is not None and findings
    ir2, findings2 = build_nac_ir([_rule(not_matching=[])], [])
    assert ir2.nacrules[0].opaque_digest is not None and findings2


def test_empty_matching_dict_is_a_real_catch_all():
    # an empty dict {} genuinely means "no constraints" → clean (not opaque)
    ir, findings = build_nac_ir([_rule(matching={})], [])
    assert ir.nacrules[0].opaque_digest is None and findings == ()


def test_malformed_list_elements_are_opaque_not_stringified():
    # a non-string list element (dict/list) must NOT be str()-ified into a clean value
    # that participates in shadow proofs — it mints an opaque row instead.
    for bad in ({"nactags": [{"bad": "shape"}]},
                {"port_types": [["nested"]]},
                {"auth_type": [{"bad": "shape"}]}):
        ir, findings = build_nac_ir([_rule(matching=bad)], [])
        assert ir.nacrules[0].opaque_digest is not None and findings, bad


def test_falsy_malformed_per_dimension_values_are_opaque():
    # present-but-falsy non-list dim values must reach _str_set and raise → opaque,
    # NOT collapse to a clean "any" (the `and m[dim]` truthiness bug).
    for bad in ({"port_types": 0}, {"nactags": ""}, {"site_ids": False}):
        ir, findings = build_nac_ir([_rule(matching=bad)], [])
        assert ir.nacrules[0].opaque_digest is not None and findings, bad


def test_empty_list_dimension_is_a_real_any():
    # an empty list [] is genuinely "no values in this dim" → clean ∅, not opaque
    ir, findings = build_nac_ir([_rule(matching={"port_types": []})], [])
    assert ir.nacrules[0].opaque_digest is None and findings == ()


def test_malformed_not_matching_values_are_opaque():
    for bad in ({"family": [{"bad": "shape"}]}, {"family": 5}):
        ir, findings = build_nac_ir([_rule(not_matching=bad)], [])
        assert ir.nacrules[0].opaque_digest is not None and findings, bad


def test_row_without_id_is_dropped_with_finding():
    ir, findings = build_nac_ir([{"name": "no-id", "action": "allow"}], [])
    assert ir.nacrules == ()
    assert findings and findings[0].severity is Severity.WARNING


def test_duplicate_rule_id_is_dropped_not_crash():
    # two rows sharing an id must NOT crash build_nac_ir (IRValidationError subclasses
    # ValueError — the old catch-then-re-add would raise again and escape). First wins;
    # the later is dropped with a WARNING.
    ir, findings = build_nac_ir([_rule(id="dup", order=1), _rule(id="dup", order=2)], [])
    assert [r.id for r in ir.nacrules] == ["dup"] and ir.nacrules[0].order == 1
    assert any(f.code == "nac.ingest.duplicate" and f.severity is Severity.WARNING
               for f in findings)


def test_two_different_malformed_values_differ_in_digest():
    a, _ = build_nac_ir([_rule(matching={"auth_type": {"k": 1}})], [])
    b, _ = build_nac_ir([_rule(matching={"auth_type": {"k": 2}})], [])
    assert a.nacrules[0].opaque_digest != b.nacrules[0].opaque_digest


def test_not_matching_normalized_to_pairs():
    ir, _ = build_nac_ir([_rule(not_matching={"auth_type": "cert", "family": ["x"]})], [])
    assert ("auth_type", "cert") in ir.nacrules[0].not_matching
    assert ("family", "x") in ir.nacrules[0].not_matching


def test_absent_order_is_none_not_opaque():
    rule = _rule()
    del rule["order"]
    ir, findings = build_nac_ir([rule], [])
    assert ir.nacrules[0].order is None and ir.nacrules[0].opaque_digest is None
    assert findings == ()                      # absent ≠ malformed


def test_malformed_order_is_opaque_and_warns():
    # a PRESENT-but-non-int order is proof-bearing → opaque (so a malformed-order change
    # stays diff-bearing via opaque_digest, not collapsed to None==None)
    ir, findings = build_nac_ir([_rule(order="abc")], [])
    r = ir.nacrules[0]
    assert r.order is None and r.opaque_digest is not None
    assert findings and findings[0].severity is Severity.WARNING


def test_nactag_mapped():
    ir, _ = build_nac_ir([], [{"id": "t1", "name": "mac.pc", "type": "match",
                               "match": "client_mac", "values": ["aabb"], "match_all": True}])
    t = ir.nactags[0]
    assert t.match == "client_mac" and t.match_all is True and t.values == frozenset({"aabb"})
