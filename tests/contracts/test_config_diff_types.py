from digital_twin.contracts import FieldChange, ObjectConfigDiff


def test_field_change_is_frozen_and_holds_values():
    c = FieldChange(path="order", kind="changed", before=2, after=0)
    assert (c.path, c.kind, c.before, c.after) == ("order", "changed", 2, 0)


def test_object_config_diff_holds_changes():
    d = ObjectConfigDiff(
        object_type="nacrule", object_id="b", name="b", action="update",
        changes=(FieldChange("order", "changed", 2, 0),),
    )
    assert d.object_id == "b" and d.action == "update"
    assert d.changes[0].path == "order"
