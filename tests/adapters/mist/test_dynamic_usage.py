"""Dynamic port profile rule evaluation (Mist `mode: "dynamic"` port usages).

Semantics pinned from a real template (2026-06-10): rules evaluate IN ORDER,
`expression` "[a:b]" slices the source value, `equals` exact-matches, first
match wins; a port with nothing connected keeps its static usage. Honesty: a
rule whose source we cannot observe (e.g. lldp_system_description — not in any
fetched stat) makes the outcome INCONCLUSIVE when reached before a match —
the twin never guesses a runtime profile.
"""

from __future__ import annotations

from digital_twin.adapters.mist.ingest.dynamic_usage import (
    RuleOutcome,
    evaluate_rules,
    unresolved_dynamic_findings,
)

# the real template's rules, verbatim shape
RULES = [
    {"equals": "IPCLOS", "expression": "[0:6]", "src": "lldp_system_name", "usage": "uplink"},
    {"equals": "ld-cup-idf", "expression": "[0:10]", "src": "lldp_system_name", "usage": "uplink"},
    {"equals": "LD_", "expression": "[0:3]", "src": "lldp_system_name", "usage": "ap"},
    {"equals": "Mist", "expression": "[0:4]", "src": "lldp_system_description", "usage": "ap"},
]


def test_first_matching_rule_wins():
    out = evaluate_rules(RULES, {"lldp_system_name": "LD_PLM1_AP"})
    assert out == RuleOutcome(kind="matched", usage="ap", rule_index=2)


def test_slice_expression_applies_to_the_source():
    out = evaluate_rules(RULES, {"lldp_system_name": "ld-cup-idf-b"})
    assert out.kind == "matched" and out.usage == "uplink"


def test_no_match_with_unevaluable_rule_is_inconclusive():
    # name rules all miss; the description rule CANNOT be evaluated (source
    # unobserved) -> the twin cannot rule out a match -> inconclusive
    out = evaluate_rules(RULES, {"lldp_system_name": "iDRAC-DNR-LD"})
    assert out.kind == "inconclusive"


def test_unevaluable_rule_after_a_match_does_not_matter():
    # LD_ matches at index 2; the unevaluable description rule is AFTER it
    out = evaluate_rules(RULES, {"lldp_system_name": "LD_Kitchen"})
    assert out.kind == "matched" and out.usage == "ap"


def test_all_rules_evaluable_and_missed_is_static():
    name_only = [r for r in RULES if r["src"] == "lldp_system_name"]
    out = evaluate_rules(name_only, {"lldp_system_name": "printer-7"})
    assert out.kind == "static"


def test_known_absent_source_is_a_conclusive_miss():
    # source key present with None = KNOWN absent (no lldp neighbor at all):
    # the rule conclusively cannot match. A MISSING key = unobservable by the
    # twin -> inconclusive (next test).
    name_only = [r for r in RULES if r["src"] == "lldp_system_name"]
    assert evaluate_rules(name_only, {"lldp_system_name": None}).kind == "static"


def test_missing_source_key_is_unobservable_hence_inconclusive():
    name_only = [r for r in RULES if r["src"] == "lldp_system_name"]
    assert evaluate_rules(name_only, {}).kind == "inconclusive"


def test_missing_expression_compares_the_whole_value():
    rules = [{"equals": "ap-7", "src": "lldp_system_name", "usage": "ap"}]
    assert evaluate_rules(rules, {"lldp_system_name": "ap-7"}).kind == "matched"
    assert evaluate_rules(rules, {"lldp_system_name": "ap-77"}).kind == "static"


def test_malformed_rule_is_unevaluable_not_a_crash():
    rules = [{"src": "lldp_system_name", "usage": "ap"}]  # no equals
    assert evaluate_rules(rules, {"lldp_system_name": "x"}).kind == "inconclusive"


# -- the honesty gate: flag only UNRESOLVED dynamic ports on definition changes --

_EFF = {
    "networks": {"corp": {"vlan_id": 10}},
    "port_usages": {
        "aps": {"mode": "trunk", "all_networks": True},
        "dynamic": {
            "mode": "dynamic",
            "rules": [
                {"src": "lldp_system_name", "expression": "[0:3]", "equals": "AP_", "usage": "aps"}
            ],
        },
    },
    "port_config": {
        "ge-0/0/1": {"usage": "default", "dynamic_usage": "dynamic"},  # resolvable
        "ge-0/0/9": {"usage": "default", "dynamic_usage": "dynamic"},  # no stats row
    },
}
_STATS = [
    {"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": True, "neighbor_system_name": "AP_1"},
]


def _changed_eff():
    return {**_EFF, "port_usages": {**_EFF["port_usages"], "aps": {"mode": "access"}}}


def test_definition_change_flags_only_unresolved_dynamic_ports():
    findings = unresolved_dynamic_findings(
        {"aa0000000001": _EFF}, {"aa0000000001": _changed_eff()}, _STATS
    )
    assert len(findings) == 1
    blob = str(findings[0].evidence)
    assert "ge-0/0/9" in blob and "ge-0/0/1" not in blob
    # the finding names its object: the device it's about
    assert findings[0].subject is not None
    assert findings[0].subject.kind == "device"
    assert findings[0].subject.id == "aa0000000001"


def test_all_dynamic_ports_resolved_means_no_finding():
    eff = {
        **_EFF,
        "port_config": {"ge-0/0/1": {"usage": "default", "dynamic_usage": "dynamic"}},
    }
    changed = {**eff, "port_usages": {**eff["port_usages"], "aps": {"mode": "access"}}}
    out = unresolved_dynamic_findings({"aa0000000001": eff}, {"aa0000000001": changed}, _STATS)
    assert out == ()


def test_unchanged_definitions_mean_no_finding():
    assert unresolved_dynamic_findings({"aa0000000001": _EFF}, {"aa0000000001": _EFF}, _STATS) == ()


# -- full OAS rule grammar: equals_any + split()/index/slice expressions --------


def test_equals_any_matches_any_listed_value():
    rules = [
        {"src": "lldp_system_name", "expression": "[0:3]", "equals_any": ["AP_", "ap-"],
         "usage": "ap"}
    ]
    assert evaluate_rules(rules, {"lldp_system_name": "ap-7"}).usage == "ap"
    assert evaluate_rules(rules, {"lldp_system_name": "AP_9"}).usage == "ap"
    assert evaluate_rules(rules, {"lldp_system_name": "sw-1"}).kind == "static"


def test_split_expression_selects_the_field():
    # schema: "split(.)[1]": "a.b.c" -> "b"
    rules = [{"src": "lldp_system_name", "expression": "split(.)[1]", "equals": "b",
              "usage": "ap"}]
    assert evaluate_rules(rules, {"lldp_system_name": "a.b.c"}).kind == "matched"


def test_split_then_slice_expression():
    # schema: "split(-)[1][0:3]": "a1234-b5678-c90" -> "b56"
    rules = [{"src": "lldp_system_name", "expression": "split(-)[1][0:3]", "equals": "b56",
              "usage": "ap"}]
    assert evaluate_rules(rules, {"lldp_system_name": "a1234-b5678-c90"}).kind == "matched"


def test_split_index_out_of_range_is_a_conclusive_miss():
    rules = [{"src": "lldp_system_name", "expression": "split(.)[5]", "equals": "x",
              "usage": "ap"}]
    assert evaluate_rules(rules, {"lldp_system_name": "a.b"}).kind == "static"


def test_unparseable_expression_is_inconclusive():
    rules = [{"src": "lldp_system_name", "expression": "regex(foo)", "equals": "x",
              "usage": "ap"}]
    assert evaluate_rules(rules, {"lldp_system_name": "a"}).kind == "inconclusive"


def test_baseline_unresolved_dynamic_port_also_gates():
    # reviewer case (W1): the rule matches usage "aps" but the BASELINE has no
    # "aps" definition (unresolved -> the CURRENT carriage is unknown); the
    # proposal adds the definition. The transition FROM an unknown state cannot
    # be verified -> gate, with side-specific evidence.
    base = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "dynamic": {
                "mode": "dynamic",
                "rules": [
                    {"src": "lldp_system_name", "expression": "[0:3]", "equals": "AP_",
                     "usage": "aps"}
                ],
            },
        },
        "port_config": {"ge-0/0/1": {"usage": "default", "dynamic_usage": "dynamic"}},
    }
    prop = {**base, "port_usages": {**base["port_usages"], "aps": {"mode": "trunk"}}}
    findings = unresolved_dynamic_findings(
        {"aa0000000001": base}, {"aa0000000001": prop}, _STATS
    )
    assert len(findings) == 1
    assert findings[0].evidence["unresolved_dynamic_ports"] == {
        "baseline": ["ge-0/0/1"],
        "proposed": [],
    }


def test_reset_default_when_none_makes_a_down_port_unresolved():
    # OAS: reset_default_when "none" keeps the LAST dynamic usage on link-down —
    # unknowable from config+stats, so a down port is NOT conclusively static
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "aps": {"mode": "trunk", "all_networks": True},
            "dynamic": {
                "mode": "dynamic",
                "reset_default_when": "none",
                "rules": [
                    {"src": "lldp_system_name", "expression": "[0:3]", "equals": "AP_",
                     "usage": "aps"}
                ],
            },
        },
        "port_config": {"ge-0/0/1": {"usage": "default", "dynamic_usage": "dynamic"}},
    }
    changed = {**eff, "port_usages": {**eff["port_usages"], "aps": {"mode": "access"}}}
    down = [{"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": False}]
    findings = unresolved_dynamic_findings(
        {"aa0000000001": eff}, {"aa0000000001": changed}, down
    )
    assert len(findings) == 1 and "ge-0/0/1" in str(findings[0].evidence)
