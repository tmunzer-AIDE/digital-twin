from digital_twin.adapters.mist.compile.fold import MergePolicy, fold_layers


def test_replace_default_last_layer_wins():
    out = fold_layers([{"a": 1, "b": 1}, {"b": 2}], {})
    assert out == {"a": 1, "b": 2}


def test_none_layers_skipped():
    out = fold_layers([None, {"a": 1}, None], {})
    assert out == {"a": 1}


def test_dict_merge_per_key_later_layer_wins_per_key():
    policy = {"networks": MergePolicy.DICT_MERGE}
    base = {"networks": {"corp": {"vlan_id": 10}, "guest": {"vlan_id": 20}}}
    top = {"networks": {"guest": {"vlan_id": 99}, "iot": {"vlan_id": 30}}}
    out = fold_layers([base, top], policy)
    assert out["networks"] == {
        "corp": {"vlan_id": 10},
        "guest": {"vlan_id": 99},
        "iot": {"vlan_id": 30},
    }


def test_replace_field_not_merged():
    # a field absent from policy replaces wholesale (a sitetemplate one-port
    # edit must not be merged when policy says REPLACE)
    out = fold_layers([{"x": {"a": 1}}, {"x": {"b": 2}}], {})
    assert out["x"] == {"b": 2}


def test_three_layer_fold_equals_left_fold_of_two():
    policy = {"networks": MergePolicy.DICT_MERGE}
    a = {"networks": {"n1": {"vlan_id": 1}}}
    b = {"networks": {"n2": {"vlan_id": 2}}}
    c = {"networks": {"n2": {"vlan_id": 22}, "n3": {"vlan_id": 3}}}
    assert fold_layers([a, b, c], policy)["networks"] == {
        "n1": {"vlan_id": 1}, "n2": {"vlan_id": 22}, "n3": {"vlan_id": 3},
    }
