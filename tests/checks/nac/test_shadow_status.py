from digital_twin.checks.nac.shadowing import (
    A_covers_B,
    ShadowStatus,
    is_provable,
    shadow_status,
)
from digital_twin.ir import NacRule


def R(id, order=1, enabled=True, **kw):
    return NacRule(id=id, order=order, enabled=enabled, action="allow", **kw)


def _state(*rules):
    return {r.id: r for r in rules}


def test_catch_all_covers_everything():
    a = R("a")                                   # ∅ everything = catch-all
    b = R("b", auth_types=frozenset({"cert"}), port_types=frozenset({"wireless"}))
    assert A_covers_B(a, b) is True


def test_choice_direction_auth_port():
    a = R("a", auth_types=frozenset({"cert", "mab"}))
    b = R("b", auth_types=frozenset({"cert"}))    # b ⊆ a → a covers b
    assert A_covers_B(a, b) is True
    assert A_covers_B(b, a) is False              # a not ⊆ b


def test_tag_conservatism():
    a = R("a", match_tags=frozenset({"X"}))
    b = R("b", match_tags=frozenset({"X", "Y"}))
    assert A_covers_B(a, b) is False              # strict subset must NOT cover
    c = R("c", match_tags=frozenset({"X"}))
    assert A_covers_B(a, c) is True               # identical → covers


def test_provability_excludes_unmodeled_and_opaque():
    assert is_provable(R("a")) is True
    assert is_provable(R("a", site_ids=frozenset({"s"}))) is False
    assert is_provable(R("a", not_matching=frozenset({("auth_type", "cert")}))) is False
    assert is_provable(NacRule(id="a", order=1, opaque_digest="x")) is False
    assert is_provable(NacRule(id="a", order=None)) is False


def test_shadow_status_tristate():
    a, b = R("a", order=1), R("b", order=2)
    assert shadow_status("a", "b", _state(a, b)) is ShadowStatus.TRUE
    assert shadow_status("a", "x", _state(a)) is ShadowStatus.FALSE   # absent
    dis = R("a", order=1, enabled=False)
    assert shadow_status("a", "b", _state(dis, b)) is ShadowStatus.FALSE  # disabled
    later = R("a", order=5)
    assert shadow_status("a", "b", _state(later, b)) is ShadowStatus.FALSE  # not earlier
    opa = NacRule(id="a", order=1, enabled=True, opaque_digest="x")
    assert shadow_status("a", "b", _state(opa, b)) is ShadowStatus.INDETERMINATE
