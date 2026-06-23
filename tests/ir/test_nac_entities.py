from digital_twin.ir import CONFIG_META, NacRule, NacTag


def test_nacrule_is_frozen_with_defaults():
    r = NacRule(id="r1", name="rule one", order=1, enabled=True, action="allow",
                auth_types=frozenset({"cert"}), port_types=frozenset({"wireless"}),
                match_tags=frozenset({"t1"}))
    assert r.id == "r1"
    assert r.not_matching == frozenset() and r.opaque_digest is None
    assert r.site_ids == frozenset() and r.apply_tags == frozenset()
    assert r.meta is CONFIG_META


def test_nactag_carries_predicate_fields():
    t = NacTag(id="t1", name="mac.pc", type="match",
               match="client_mac", values=frozenset({"aabb"}), match_all=False)
    assert t.id == "t1" and t.match == "client_mac" and t.match_all is False
