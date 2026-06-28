from digital_twin.config_diff import object_config_diff


def _d(action, before, after, ot="nacrule", oid="b", name="b"):
    return object_config_diff(object_type=ot, object_id=oid, name=name,
                              action=action, before=before, after=after)


def test_update_changed_only():
    d = _d("update", {"order": 2, "enabled": True}, {"order": 0, "enabled": True})
    by = {c.path: c for c in d.changes}
    assert set(by) == {"order"}
    assert by["order"].kind == "changed" and by["order"].before == 2 and by["order"].after == 0


def test_create_all_added():
    d = _d("create", {}, {"order": 0, "action": "allow"}, oid="z", name="z")
    assert {c.kind for c in d.changes} == {"added"}
    by = {c.path: c for c in d.changes}
    assert by["action"].before is None and by["action"].after == "allow"


def test_delete_all_removed():
    d = _d("delete", {"order": 2, "action": "allow"}, {})
    assert {c.kind for c in d.changes} == {"removed"}


def test_list_leaf_is_atomic():
    d = _d("update", {"tags": ["a", "b"]}, {"tags": ["a", "b", "c"]})
    by = {c.path: c for c in d.changes}
    assert "tags" in by and by["tags"].before == ["a", "b"] and by["tags"].after == ["a", "b", "c"]


def test_secret_leaf_masked_not_leaked():
    d = _d("update", {"psk": "OLDSECRET"}, {"psk": "NEWSECRET"})
    c = d.changes[0]
    assert c.path == "psk" and c.kind == "changed"
    assert c.before == "‹redacted›" and c.after == "‹redacted›"
    assert "OLDSECRET" not in repr(d.changes) and "NEWSECRET" not in repr(d.changes)


def test_secret_under_sensitive_parent_masked():
    # P1: generic child key "value" under sensitive parent "private_key"
    d = _d("update", {"private_key": {"value": "OLD"}}, {"private_key": {"value": "NEW"}})
    c = d.changes[0]
    assert c.path == "private_key.value"
    assert c.before == "‹redacted›" and c.after == "‹redacted›"
    assert "OLD" not in repr(d.changes) and "NEW" not in repr(d.changes)


def test_open_ended_wlantemplate_body_secrets_redacted():
    d = object_config_diff(
        object_type="wlantemplate",
        object_id="tmpl1",
        name="Guest template",
        action="delete",
        before={
            "id": "tmpl1",
            "name": "Guest template",
            "additional": {"portal_psk": "SUPERSECRET"},
            "vendorBlob": {"api_token": "eyJhbGciOiJI.eyJzdWIiOiIxMjMifQ.signature"},
        },
        after=None,
    )

    blob = repr(d)
    assert "SUPERSECRET" not in blob
    assert "eyJhbGciOiJI.eyJzdWIiOiIxMjMifQ.signature" not in blob
    by = {c.path: c for c in d.changes}
    assert by["additional.portal_psk"].before == "‹redacted›"
    assert by["vendorBlob.api_token"].before == "‹redacted›"


def test_object_identity_kept_raw():
    d = _d("update", {"order": 2}, {"order": 0}, oid="rule-42", name="guest")
    assert d.object_id == "rule-42" and d.name == "guest"
