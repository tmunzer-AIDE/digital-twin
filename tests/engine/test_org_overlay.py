from datetime import UTC, datetime

from digital_twin.contracts import ObjectRef
from digital_twin.engine.org_overlay import OrgOverlay, affected_sites, apply_overlays
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
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


# ---------------------------------------------------------------------------
# apply_overlays — Task 2 (OD-T2)
# ---------------------------------------------------------------------------

def _raw(nt=None, gt=None, st=None):
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1"},
        setting={},
        networktemplate=nt,
        devices=(),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
        gatewaytemplate=gt,
        sitetemplate=st,
    )


def test_apply_overlays_delete_pins_proposed_none():
    fetched = _raw(nt={"id": "nt1", "live": True})
    o = _ov(otype="networktemplate", oid="nt1", sites=("s1",), proposed=None, action="delete")
    base, prop = apply_overlays(fetched, "s1", (o,))
    assert base.networktemplate == {"id": "nt1"}     # baseline pinned to the snapshot
    assert prop.networktemplate is None              # proposed = layer ABSENT


def test_apply_overlays_only_pins_assigned_overlays():
    fetched = _raw(nt={"id": "ntX"}, gt={"id": "gtX"})
    nt_op = _ov(otype="networktemplate", oid="nt1", sites=("s1",), action="delete")
    gt_op = _ov(otype="gatewaytemplate", oid="gt1", sites=("s2",), action="delete")  # NOT s1
    base, prop = apply_overlays(fetched, "s1", (nt_op, gt_op))
    assert prop.networktemplate is None              # s1 IS assigned nt1 -> pinned
    assert prop.gatewaytemplate == {"id": "gtX"}     # s1 NOT assigned gt1 -> untouched


def test_apply_overlays_combines_two_overlays_on_one_site():
    fetched = _raw(nt={"id": "ntX"}, gt={"id": "gtX"})
    nt_op = _ov(otype="networktemplate", oid="nt1", sites=("s1",), action="delete")
    gt_op = _ov(otype="gatewaytemplate", oid="gt1", sites=("s1",),
                proposed={"id": "gt1", "edited": True}, action="update")
    base, prop = apply_overlays(fetched, "s1", (nt_op, gt_op))
    assert prop.networktemplate is None and prop.gatewaytemplate == {"id": "gt1", "edited": True}
