from digital_twin.contracts import ObjectRef
from digital_twin.engine.org_overlay import OrgOverlay, affected_sites
from digital_twin.verdict.org_verdict import OrgChange


def _ov(otype="networktemplate", oid="nt1", sites=("s1",), proposed=None, action="delete"):
    return OrgOverlay(
        object_type=otype, object_id=oid, name=oid, action=action,
        assigned_site_ids=frozenset(sites), baseline={"id": oid}, proposed=proposed,
    )


def test_overlay_delete_has_none_proposed():
    o = _ov()
    assert o.proposed is None and o.action == "delete"


def test_overlay_update_carries_proposed():
    o = _ov(proposed={"id": "nt1", "x": 1}, action="update")
    assert o.proposed == {"id": "nt1", "x": 1} and o.action == "update"


def test_affected_sites_is_sorted_union():
    a = _ov(oid="nt1", sites=("s2", "s1"))
    b = _ov(oid="gt1", otype="gatewaytemplate", sites=("s2", "s3"))
    assert affected_sites((a, b)) == ("s1", "s2", "s3")


def test_org_change_holds_ref_and_action():
    c = OrgChange(ref=ObjectRef("networktemplate", "nt1", "name"), action="delete")
    assert c.ref.id == "nt1" and c.action == "delete"
