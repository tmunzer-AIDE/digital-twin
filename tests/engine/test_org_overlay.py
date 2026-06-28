from datetime import UTC, datetime

import pytest

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

def _raw(nt=None, gt=None, st=None, wlans=()):
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
        wlans=wlans,
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


def _wlan_ov(
    *,
    sites=("s1",),
    baseline_by_site=None,
    proposed_by_site=None,
    baseline=None,
    proposed=None,
):
    if baseline_by_site is None:
        baseline_by_site = {"s1": {"id": "w1", "enabled": True}}
    if proposed_by_site is None:
        proposed_by_site = {"s1": proposed}
    return OrgOverlay(
        object_type="wlan",
        object_id="w1",
        name="corp",
        action="delete" if proposed is None else "update",
        assigned_site_ids=frozenset(sites),
        baseline=baseline or {"id": "w1", "enabled": False},
        proposed=proposed,
        wlan_baseline_by_site=baseline_by_site,
        wlan_proposed_by_site=proposed_by_site,
    )


def test_wlan_overlay_site_maps_must_match_assigned_sites():
    with pytest.raises(ValueError, match="baseline sites"):
        _wlan_ov(sites=("s1", "s2"), baseline_by_site={"s1": {"id": "w1"}})
    with pytest.raises(ValueError, match="proposed sites"):
        _wlan_ov(proposed_by_site={})


def test_apply_overlays_wlan_delete_uses_derived_baseline_row():
    fetched = _raw(wlans=({"id": "w1", "enabled": "fetched"}, {"id": "w2", "enabled": True}))
    o = _wlan_ov(
        baseline={"id": "w1", "enabled": False},  # org snapshot, not authoritative per-site
        baseline_by_site={"s1": {"id": "w1", "enabled": True, "apply_to": "site"}},
        proposed_by_site={"s1": None},
    )

    base, prop = apply_overlays(fetched, "s1", (o,))

    assert tuple(row["id"] for row in base.wlans) == ("w1", "w2")
    assert next(row for row in base.wlans if row["id"] == "w1")["enabled"] is True
    assert tuple(row["id"] for row in prop.wlans) == ("w2",)


def test_apply_overlays_wlan_update_uses_per_site_proposed_row():
    fetched = _raw(wlans=({"id": "w1", "enabled": True}, {"id": "w2", "enabled": True}))
    proposed = {"id": "w1", "enabled": False}
    o = _wlan_ov(
        proposed=proposed,
        baseline_by_site={"s1": {"id": "w1", "enabled": True}},
        proposed_by_site={"s1": proposed},
    )

    base, prop = apply_overlays(fetched, "s1", (o,))

    assert next(row for row in base.wlans if row["id"] == "w1")["enabled"] is True
    assert next(row for row in prop.wlans if row["id"] == "w1")["enabled"] is False
    assert next(row for row in prop.wlans if row["id"] == "w2")["enabled"] is True


def test_apply_overlays_wlan_skips_unassigned_site():
    fetched = _raw(wlans=({"id": "w1", "enabled": True},))
    o = _wlan_ov(
        sites=("s2",),
        baseline_by_site={"s2": {"id": "w1"}},
        proposed_by_site={"s2": None},
    )

    base, prop = apply_overlays(fetched, "s1", (o,))

    assert base.wlans == fetched.wlans
    assert prop.wlans == fetched.wlans
