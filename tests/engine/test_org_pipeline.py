from dataclasses import dataclass
from datetime import UTC, datetime

from digital_twin.engine.pipeline import simulate_org_template
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    OrgTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
)
from digital_twin.verdict.decision import Decision


def _meta():
    return StateMeta(
        acquired_at=datetime.now(UTC), host="h",
        # client fetches succeeded (empty == "no clients") so clients.active is
        # EARNED -> wired.client.impact is HIGH, not INSUFFICIENT_DATA -> SAFE
        fetched=("site", "setting", "devices", "wireless_clients", "wired_clients"),
        failures=(),
    )


def _site(sid, *, setting, devices, nt):
    return RawSiteState(
        scope=SiteScope("o1", sid), site={"id": sid, "networktemplate_id": "nt1"},
        setting=setting, networktemplate=nt, devices=tuple(devices), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None, meta=_meta(),
    )


@dataclass
class _FakeProvider:
    sites: dict
    template: dict

    def resolve_org_template(self, scope, template_id, object_type="networktemplate"):
        return OrgTemplateContext(template=self.template, assigned_site_ids=tuple(self.sites))

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        ids = list(site_ids) if site_ids is not None else list(self.sites)
        return {sid: self.sites[sid] for sid in ids}

    def fetch_site(self, scope, *, include_derived=False):  # unused here
        raise NotImplementedError


def _plan(payload):
    return {"source": "mist", "scope": {"org_id": "o1"},
            "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                     "object_id": "nt1", "payload": payload}]}


def test_org_template_rejects_site_plan_with_unknown():
    prov = _FakeProvider(sites={}, template={})
    site_plan = {"source": "mist", "scope": {"org_id": "o1", "site_id": "s1"},
                 "ops": [{"action": "update", "order": 0, "object_type": "device",
                          "object_id": "d1", "payload": {}}]}
    ov = simulate_org_template(site_plan, provider=prov)
    assert ov.decision is Decision.UNKNOWN
    assert any("simulate" in r and "org" in r.lower() for r in ov.decision_reasons)


def test_org_template_zero_sites_is_safe():
    prov = _FakeProvider(sites={}, template={"id": "nt1", "networks": {}})
    ov = simulate_org_template(_plan({"networks": {}}), provider=prov)
    assert ov.decision is Decision.SAFE and ov.per_site == {}


def test_org_template_out_of_scope_leaf_is_unknown():
    prov = _FakeProvider(
        sites={"s1": _site("s1", setting={"id": "s1"}, devices=(), nt={"id": "nt1"})},
        template={"id": "nt1", "switch_matching": {"enable": True}},
    )
    ov = simulate_org_template(_plan({"switch_matching": {"enable": False}}), provider=prov)
    assert ov.decision is Decision.UNKNOWN and ov.per_site == {}
    assert ov.org_rejections


def test_org_template_per_site_and_rollup():
    tmpl = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    s1 = _site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    s2 = _site("s2", setting={"id": "s2"}, devices=(), nt=tmpl)
    prov = _FakeProvider(sites={"s1": s1, "s2": s2}, template=tmpl)
    ov = simulate_org_template(
        _plan({"networks": {"corp": {"vlan_id": 10}, "extra": {"vlan_id": 11}}}), provider=prov
    )
    assert set(ov.per_site) == {"s1", "s2"}
    assert ov.decision is Decision.SAFE


def test_org_template_fetch_failed_site_is_unknown():
    tmpl = {"id": "nt1", "networks": {}}
    s1 = _site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    prov = _FakeProvider(sites={"s1": s1, "s2": None}, template=tmpl)
    prov.sites["s2"] = FetchError(
        scope=SiteScope("o1", "s2"), failures=(FetchFailure("site", "503"),),
        acquired_at=datetime.now(UTC), host="h",
    )
    ov = simulate_org_template(_plan({"networks": {}}), provider=prov)
    assert ov.per_site["s2"].decision is Decision.UNKNOWN
    assert ov.decision is Decision.UNKNOWN
    assert "s2" in ov.site_failures
