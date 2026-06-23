import pytest

from digital_twin.ir import IRBuilder, IRValidationError, NacRule, NacTag


def test_builder_collects_nac_entities_in_order():
    ir = (IRBuilder()
          .add_nacrule(NacRule(id="r2", order=2))
          .add_nacrule(NacRule(id="r1", order=1))
          .add_nactag(NacTag(id="t1", name="vlan.srv"))
          .build())
    assert [r.id for r in ir.nacrules] == ["r2", "r1"]   # insertion order preserved
    assert [t.id for t in ir.nactags] == ["t1"]


def test_empty_ir_has_no_nac_entities():
    ir = IRBuilder().build()
    assert ir.nacrules == () and ir.nactags == ()


def test_duplicate_nacrule_id_raises():
    b = IRBuilder().add_nacrule(NacRule(id="r1"))
    with pytest.raises(IRValidationError):
        b.add_nacrule(NacRule(id="r1"))
