from dataclasses import replace
from datetime import UTC, datetime

import pytest

from digital_twin.adapters.mist.behavioral_capture import capture
from digital_twin.behavioral import Coverage, ObjectKey
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    NacFetch,
    RawSiteState,
    SiteScope,
    StateMeta,
)

AT = datetime(2026, 10, 1, tzinfo=UTC)


def raw(site, *, network_vlan=10):
    return RawSiteState(
        SiteScope("org", site),
        {"id": site, "networktemplate_id": "shared"},
        {},
        {"id": "shared", "networks": {"employees": {"vlan_id": network_vlan}}},
        ({"id": "sw-" + site, "type": "switch", "model": "EX4400", "mac": "mac-" + site},),
        ({"mac": "mac-" + site, "version": "24.4R2"},),
        (),
        (),
        (),
        None,
        StateMeta(AT, "api.mist.com", ("site", "setting", "devices", "wlans", "org_networks"), ()),
        wlans=({"id": "wlan-" + site, "vlan_id": 10},),
        org_networks=({"id": "net", "name": "employees", "vlan_id": network_vlan},),
    )


class Provider:
    def __init__(self, results, nac=None):
        self.results = results
        self.nac = (
            nac
            if nac is not None
            else NacFetch(
                ({"id": "rule", "action": "allow"},),
                ({"id": "tag", "type": "usermac"},),
            )
        )
        self.requested = None

    def fetch_sites(self, scope, site_ids, *, include_derived=False):
        assert scope.org_id == "org"
        assert not include_derived
        self.requested = tuple(site_ids)
        return self.results

    def resolve_org_nac(self, scope):
        assert scope.org_id == "org"
        return self.nac


def windows(snapshot):
    return {window.name: window for window in snapshot.inputs}


def test_capture_preserves_multisite_scope_shared_objects_platform_and_raw_structure():
    provider = Provider({"a": raw("a"), "b": raw("b")})
    snapshot = capture(provider, org_id="org", site_ids=("a", "b"), include_nac=True)
    assert provider.requested == ("a", "b")
    templates = [r for r in snapshot.records if r.key.kind == "network_template"]
    assert len(templates) == 1
    assert templates[0].body()["networks"]["employees"]["vlan_id"] == 10
    switches = [r for r in snapshot.records if r.key.kind == "device_switch"]
    assert {r.key.site_id for r in switches} == {"a", "b"}
    assert all((r.platform, r.release) == ("EX4400", "24.4R2") for r in switches)
    assert windows(snapshot)["site:a"].completed_at == AT
    assert windows(snapshot)["site:a"].complete
    assert windows(snapshot)["capture_consistency"].complete
    assert not windows(snapshot)["behavioral_inventory"].complete
    assert {r.key.kind for r in snapshot.records} >= {"nac_rule", "nac_tag"}
    provider.results["a"].networktemplate["networks"]["employees"]["vlan_id"] = 20
    assert templates[0].body()["networks"]["employees"]["vlan_id"] == 10


def test_partial_failed_missing_and_wrong_scope_sites_are_not_authoritative_empty():
    partial = replace(
        raw("a"),
        meta=replace(
            raw("a").meta,
            failures=(FetchFailure("wlans", "sensitive raw error must not be copied"),),
        ),
    )
    failed = FetchError(
        SiteScope("org", "b"), (FetchFailure("setting", "secret"),), AT, "api.mist.com"
    )
    wrong = replace(raw("d"), scope=SiteScope("other-org", "d"))
    snapshot = capture(
        Provider({"a": partial, "b": failed, "d": wrong}),
        org_id="org",
        site_ids=("a", "b", "c", "d"),
    )
    assert all(not windows(snapshot)["site:" + s].complete for s in ("a", "b", "c", "d"))
    assert all("secret" not in w.failure and "sensitive" not in w.failure for w in snapshot.inputs)
    assert {r.key.site_id for r in snapshot.records if r.key.site_id} == {"a"}


def test_absent_required_fetch_markers_and_missing_assigned_template_are_gaps():
    a = replace(
        raw("a"),
        networktemplate=None,
        meta=replace(raw("a").meta, fetched=("site", "setting", "devices")),
    )
    snapshot = capture(Provider({"a": a}), org_id="org", site_ids=("a",))
    assert not windows(snapshot)["site:a"].complete
    assert "wlans" in windows(snapshot)["site:a"].failure
    assert not windows(snapshot)["capture_consistency"].complete


def test_inconsistent_shared_configurations_and_cross_scope_body_are_visible():
    a, b = raw("a"), raw("b", network_vlan=20)
    a = replace(a, devices=({**a.devices[0], "site_id": "wrong"},))
    snapshot = capture(Provider({"a": a, "b": b}), org_id="org", site_ids=("a", "b"))
    failure = windows(snapshot)["capture_consistency"].failure
    assert "inconsistent duplicate" in failure
    assert "site identity differs" in failure
    assert snapshot.get(ObjectKey("org", "network_template", "shared")).body() == a.networktemplate


def test_nac_failure_and_missing_device_identity_are_explicit():
    a = replace(raw("a"), devices=({"type": "switch", "mac": "mac-a"},))
    nac = FetchError(
        SiteScope("org", "a"), (FetchFailure("nacrules", "secret"),), AT, "api.mist.com"
    )
    snapshot = capture(Provider({"a": a}, nac), org_id="org", site_ids=("a",), include_nac=True)
    assert not windows(snapshot)["nac"].complete
    assert not windows(snapshot)["capture_consistency"].complete
    gaps = Coverage().inspect(snapshot, tuple(r.key for r in snapshot.records))
    assert any(g.code == "incomplete_input" and g.path == ("nac",) for g in gaps)


@pytest.mark.parametrize("sites", [(), ("a", "a"), ("",)])
def test_invalid_capture_scope_rejected_before_fetch(sites):
    provider = Provider({})
    with pytest.raises(ValueError):
        capture(provider, org_id="org", site_ids=sites)
    assert provider.requested is None
