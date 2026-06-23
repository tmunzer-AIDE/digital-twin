from digital_twin.ir import IRBuilder, NacRule, diff_ir


def _ir(*rules: NacRule):
    b = IRBuilder()
    for r in rules:
        b.add_nacrule(r)
    return b.build()


def test_add_remove_modify_nacrule():
    base = _ir(NacRule(id="r1", order=1, action="allow"),
               NacRule(id="r2", order=2, action="allow"))
    prop = _ir(NacRule(id="r1", order=1, action="block"),    # modified
               NacRule(id="r3", order=3, action="allow"))    # added; r2 removed
    d = diff_ir(base, prop)
    assert ("nacrule", "r3") in [(e.kind, e.id) for e in d.added]
    assert ("nacrule", "r2") in [(e.kind, e.id) for e in d.removed]
    mod = next(m for m in d.modified if m.ref.id == "r1")
    assert mod.ref.kind == "nacrule" and "action" in mod.changed_fields
    assert d.touches("nacrule")


def test_opaque_digest_is_diff_bearing():
    base = _ir(NacRule(id="r1", order=1, opaque_digest="aaa"))
    prop = _ir(NacRule(id="r1", order=1, opaque_digest="bbb"))
    d = diff_ir(base, prop)
    mod = next(m for m in d.modified if m.ref.id == "r1")
    assert "opaque_digest" in mod.changed_fields
