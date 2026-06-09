import pytest

from digital_twin.adapters.mist.compile.vars import UnresolvedVars, resolve_vars


def test_substitutes_string_leaves_recursively():
    cfg = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}", "name": "corp"}}, "list": ["{{a}}", 1]}
    out = resolve_vars(cfg, {"corp_vlan": "30", "a": "x"})
    assert out["networks"]["corp"]["vlan_id"] == "30"
    assert out["list"] == ["x", 1]


def test_partial_substitution_inside_strings():
    out = resolve_vars({"desc": "vlan-{{id}}-prod"}, {"id": "7"})
    assert out["desc"] == "vlan-7-prod"


def test_unresolved_var_raises_with_paths():
    with pytest.raises(UnresolvedVars) as e:
        resolve_vars({"a": {"b": "{{missing}}"}}, {})
    assert "missing" in str(e.value)
    assert "a.b" in str(e.value)


def test_non_strings_pass_through_and_input_not_mutated():
    cfg = {"n": 5, "b": True, "s": "{{v}}"}
    out = resolve_vars(cfg, {"v": "ok"})
    assert out["n"] == 5 and out["b"] is True and out["s"] == "ok"
    assert cfg["s"] == "{{v}}"
