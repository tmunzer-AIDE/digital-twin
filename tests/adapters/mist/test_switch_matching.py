from digital_twin.adapters.mist.compile.switch_matching import resolve_switch_matching


def _sm(*rules, enable=True):
    return {"enable": enable, "rules": list(rules)}


def test_disabled_switch_matching_yields_no_base():
    sm = _sm(
        {"match_model": "EX4100-48MP", "port_config": {"ge-0/0/0": {"usage": "ap"}}}, enable=False
    )
    assert resolve_switch_matching(sm, {"model": "EX4100-48MP"}) == {}


def test_exact_model_match_returns_that_rules_port_config():
    sm = _sm({"match_model": "EX4100-48MP", "port_config": {"ge-0/0/0": {"usage": "ap"}}})
    assert resolve_switch_matching(sm, {"model": "EX4100-48MP"}) == {"ge-0/0/0": {"usage": "ap"}}


def test_model_slice_match_uses_the_prefix():
    sm = _sm({"match_model[0:6]": "EX4400", "port_config": {"ge-0/0/1": {"usage": "iot"}}})
    assert resolve_switch_matching(sm, {"model": "EX4400-48MP"}) == {"ge-0/0/1": {"usage": "iot"}}


def test_role_match():
    sm = _sm({"match_role": "core", "port_config": {"ge-0/0/2": {"usage": "uplink"}}})
    assert resolve_switch_matching(sm, {"role": "core"}) == {"ge-0/0/2": {"usage": "uplink"}}


def test_first_matching_rule_wins_over_a_later_match():
    # device matches BOTH (prefix and exact); the FIRST in list order wins
    sm = _sm(
        {"match_model[0:6]": "EX4100", "port_config": {"ge-0/0/0": {"usage": "from_prefix"}}},
        {"match_model": "EX4100-48MP", "port_config": {"ge-0/0/0": {"usage": "from_exact"}}},
    )
    base = resolve_switch_matching(sm, {"model": "EX4100-48MP"})
    assert base["ge-0/0/0"]["usage"] == "from_prefix"


def test_all_criteria_in_a_rule_must_match():
    # rule needs model AND role; device satisfies only the model -> no match
    sm = _sm(
        {
            "match_model": "EX4100-24MP",
            "match_role": "access",
            "port_config": {"ge-0/0/0": {"usage": "x"}},
        }
    )
    assert resolve_switch_matching(sm, {"model": "EX4100-24MP", "role": ""}) == {}


def test_no_matching_rule_yields_empty_base():
    sm = _sm({"match_model": "EX9999", "port_config": {"ge-0/0/0": {"usage": "x"}}})
    assert resolve_switch_matching(sm, {"model": "EX4100-48MP"}) == {}


def test_unknown_match_criterion_does_not_match():
    # conservative: a criterion we don't understand means the rule does NOT apply
    sm = _sm({"match_something_new": "v", "port_config": {"ge-0/0/0": {"usage": "x"}}})
    assert resolve_switch_matching(sm, {"model": "EX4100-48MP"}) == {}


def test_catch_all_rule_with_no_criteria_matches_any_switch():
    # an explicit default/catch-all rule (no match_* keys) applies to everything
    sm = _sm({"name": "default", "port_config": {"ge-0/0/0": {"usage": "default"}}})
    assert resolve_switch_matching(sm, {"model": "EX4100-48MP"}) == {
        "ge-0/0/0": {"usage": "default"}
    }


def test_returns_a_copy_not_the_rule_object():
    rule_pc = {"ge-0/0/0": {"usage": "ap"}}
    sm = _sm({"match_model": "EX4100-48MP", "port_config": rule_pc})
    out = resolve_switch_matching(sm, {"model": "EX4100-48MP"})
    out["ge-0/0/0"]["usage"] = "mutated"
    assert rule_pc["ge-0/0/0"]["usage"] == "ap"  # caller cannot mutate the template
