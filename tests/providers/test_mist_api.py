"""Offline tests for MistApiProvider.fetch_sites org-batch + partition logic.

The network-touching privates are overridden with canned payloads; the live SDK
path is covered (gated) in test_mist_api_live.py. We assert the contract that
matters: org-wide rows are partitioned to the right site, one site's failure
never sinks the others, and shared network templates are fetched once.
"""

from __future__ import annotations

from typing import Any

from digital_twin.providers.base import FetchError, OrgScope, RawSiteState
from digital_twin.providers.mist_api import MistApiProvider, _group_by_site


class FakeProvider(MistApiProvider):
    """Constructs without env/SDK; canned org + per-site payloads."""

    def __init__(
        self,
        *,
        sites: list[dict[str, Any]],
        ports: list[dict[str, Any]],
        wired: list[dict[str, Any]],
        device_stats: list[dict[str, Any]] | None = None,
        templates: dict[str, dict[str, Any]] | None = None,
        gatewaytemplates: dict[str, dict[str, Any]] | None = None,
        sitetemplates: dict[str, dict[str, Any]] | None = None,
        org_wlans: dict[str, dict[str, Any]] | None = None,
        org_wlantemplates: dict[str, dict[str, Any]] | None = None,
        wlans_by_site: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._host = "test"
        self._session = None  # type: ignore[assignment]  # privates below never touch it
        self._sites = sites
        self._ports = ports
        self._wired = wired
        self._device_stats_rows = device_stats or []
        self._templates = templates or {}
        self._gatewaytemplates = gatewaytemplates or {}
        self._sitetemplates = sitetemplates or {}
        self._org_wlans = org_wlans or {}
        self._org_wlantemplates = org_wlantemplates or {}
        self._wlans_by_site = wlans_by_site or {}
        self.nt_calls: list[str] = []

    # org-batched
    def _org_sites(self, s: OrgScope) -> list[dict[str, Any]]:
        return self._sites

    def _org_port_stats(self, s: OrgScope) -> list[dict[str, Any]]:
        return self._ports

    def _org_wired_clients(self, s: OrgScope) -> list[dict[str, Any]]:
        return self._wired

    def _org_device_stats(self, s: OrgScope) -> list[dict[str, Any]]:
        return self._device_stats_rows

    # per-site
    def _setting(self, s: Any) -> dict[str, Any]:
        return {"networks": {}, "_site": s.site_id}

    def _devices(self, s: Any) -> list[dict[str, Any]]:
        return []

    def _device_stats(self, s: Any) -> list[dict[str, Any]]:
        return []

    def _wireless_clients(self, s: Any) -> list[dict[str, Any]]:
        return []

    def _wlans(self, s: Any) -> list[dict[str, Any]]:
        return self._wlans_by_site.get(s.site_id, [])

    def _derived(self, s: Any) -> dict[str, Any]:
        return {"_derived": s.site_id}

    def _networktemplate(self, s: Any, nt_id: str) -> dict[str, Any]:
        self.nt_calls.append(nt_id)
        return self._templates[nt_id]

    def _gatewaytemplate(self, s: Any, gt_id: str) -> dict[str, Any]:
        return self._gatewaytemplates[gt_id]

    def _sitetemplate(self, s: Any, st_id: str) -> dict[str, Any]:
        return self._sitetemplates[st_id]

    def _org_wlan(self, s: Any, wlan_id: str) -> dict[str, Any]:
        return self._org_wlans[wlan_id]

    def _org_wlan_template(self, s: Any, template_id: str) -> dict[str, Any]:
        return self._org_wlantemplates[template_id]


def _sites(*ids: str, nt: str | None = None) -> list[dict[str, Any]]:
    return [{"id": i, **({"networktemplate_id": nt} if nt else {})} for i in ids]


def test_group_by_site_drops_rows_without_site_id():
    rows = [{"site_id": "a", "x": 1}, {"x": 2}, {"site_id": "a", "x": 3}, {"site_id": "b"}]
    grouped = _group_by_site(rows)
    assert sorted(grouped) == ["a", "b"]
    assert len(grouped["a"]) == 2


def test_fetch_sites_partitions_org_rows_by_site():
    p = FakeProvider(
        sites=_sites("s1", "s2"),
        ports=[{"site_id": "s1", "port_id": "ge-0/0/0"}, {"site_id": "s2", "port_id": "ge-0/0/1"}],
        wired=[{"site_id": "s2", "mac": "aa"}],
        device_stats=[{"site_id": "s1", "mac": "d1"}, {"site_id": "s2", "mac": "d2"}],
    )
    out = p.fetch_sites(OrgScope("o1"), ["s1", "s2"])
    s1, s2 = out["s1"], out["s2"]
    assert isinstance(s1, RawSiteState) and isinstance(s2, RawSiteState)
    assert [r["port_id"] for r in s1.port_stats] == ["ge-0/0/0"]
    assert [r["port_id"] for r in s2.port_stats] == ["ge-0/0/1"]
    assert s1.wired_clients == ()  # s2's client must not leak into s1
    assert [r["mac"] for r in s2.wired_clients] == ["aa"]
    assert [r["mac"] for r in s1.device_stats] == ["d1"]  # device_stats batched too
    assert [r["mac"] for r in s2.device_stats] == ["d2"]


def test_fetch_sites_none_means_all_org_sites():
    p = FakeProvider(sites=_sites("s1", "s2", "s3"), ports=[], wired=[])
    out = p.fetch_sites(OrgScope("o1"))
    assert sorted(out) == ["s1", "s2", "s3"]
    assert all(isinstance(v, RawSiteState) for v in out.values())


def test_fetch_sites_unknown_site_is_fetch_error_not_a_crash():
    p = FakeProvider(sites=_sites("s1"), ports=[], wired=[])
    out = p.fetch_sites(OrgScope("o1"), ["s1", "ghost"])
    assert isinstance(out["s1"], RawSiteState)
    assert isinstance(out["ghost"], FetchError)  # no site object -> no baseline


def test_network_template_fetched_once_across_sites():
    p = FakeProvider(
        sites=_sites("s1", "s2", nt="nt-shared"),
        ports=[],
        wired=[],
        templates={"nt-shared": {"v": 1}},
    )
    out = p.fetch_sites(OrgScope("o1"), ["s1", "s2"])
    assert p.nt_calls == ["nt-shared"]  # fetched ONCE, reused for s2
    for sid in ("s1", "s2"):
        raw = out[sid]
        assert isinstance(raw, RawSiteState)
        assert raw.networktemplate == {"v": 1}
        assert "networktemplate" in raw.meta.fetched


def test_org_sites_failure_falls_back_to_per_site_fetch():
    # if the org site-list call fails, explicit site_ids must still resolve via
    # the per-site getSiteInfo fallback — NOT all become baseline failures
    class OrgSitesBoom(FakeProvider):
        def _org_sites(self, s: OrgScope) -> list[dict[str, Any]]:
            raise RuntimeError("org sites 503")

        def _site(self, s: Any) -> dict[str, Any]:
            return {"id": s.site_id, "via": "per-site-fallback"}

    out = OrgSitesBoom(sites=[], ports=[], wired=[]).fetch_sites(OrgScope("o1"), ["s1", "s2"])
    for sid in ("s1", "s2"):
        raw = out[sid]
        assert isinstance(raw, RawSiteState), f"{sid} unexpectedly failed: {raw}"
        assert raw.site["via"] == "per-site-fallback"


def test_org_batch_failure_records_per_site_but_keeps_baseline():
    class PortsBoom(FakeProvider):
        def _org_port_stats(self, s: OrgScope) -> list[dict[str, Any]]:
            raise RuntimeError("org ports 503")

    p = PortsBoom(sites=_sites("s1", "s2"), ports=[], wired=[])
    out = p.fetch_sites(OrgScope("o1"), ["s1", "s2"])
    for sid in ("s1", "s2"):
        raw = out[sid]
        assert isinstance(raw, RawSiteState)  # setting ok -> still a usable baseline
        assert raw.port_stats == ()
        assert any(f.object == "port_stats" for f in raw.meta.failures)
        assert raw.meta.is_complete is False


def test_include_derived_only_when_requested():
    p = FakeProvider(sites=_sites("s1"), ports=[], wired=[])
    without = p.fetch_sites(OrgScope("o1"), ["s1"])["s1"]
    with_d = p.fetch_sites(OrgScope("o1"), ["s1"], include_derived=True)["s1"]
    assert isinstance(without, RawSiteState) and without.derived_setting is None
    assert isinstance(with_d, RawSiteState) and with_d.derived_setting == {"_derived": "s1"}


def test_resolve_org_template_filters_assigned_sites():
    from digital_twin.providers.base import OrgScope, OrgTemplateContext
    p = FakeProvider(
        sites=[
            {"id": "s1", "networktemplate_id": "ntX"},
            {"id": "s2", "networktemplate_id": "ntY"},
            {"id": "s3", "networktemplate_id": "ntX"},
        ],
        ports=[],
        wired=[],
        templates={"ntX": {"id": "ntX", "networks": {}}},
    )
    ctx = p.resolve_org_template(OrgScope(org_id="o1"), "ntX", "networktemplate")
    assert isinstance(ctx, OrgTemplateContext)
    assert set(ctx.assigned_site_ids) == {"s1", "s3"}
    assert ctx.template["id"] == "ntX"


def test_resolve_org_wlan_uses_derived_rows_for_membership():
    from digital_twin.providers.base import OrgScope, OrgWlanContext

    p = FakeProvider(
        sites=_sites("s1", "s2", "s3"),
        ports=[],
        wired=[],
        org_wlans={"w1": {"id": "w1", "ssid": "corp", "enabled": False}},
        wlans_by_site={
            "s1": [{"id": "w1", "ssid": "corp", "enabled": True, "apply_to": "site"}],
            "s2": [{"id": "other", "ssid": "guest", "enabled": True}],
            "s3": [{"id": "w1", "ssid": "corp", "enabled": True, "ap_ids": ["ap3"]}],
        },
    )

    ctx = p.resolve_org_wlan(OrgScope("o1"), "w1")

    assert isinstance(ctx, OrgWlanContext)
    assert ctx.wlan == {"id": "w1", "ssid": "corp", "enabled": False}
    assert set(ctx.derived_rows_by_site) == {"s1", "s3"}
    assert ctx.derived_rows_by_site["s1"]["enabled"] is True
    assert ctx.derived_rows_by_site["s3"]["ap_ids"] == ["ap3"]


def test_resolve_org_wlan_missing_org_wlan_is_fetch_error():
    p = FakeProvider(sites=_sites("s1"), ports=[], wired=[], org_wlans={})

    result = p.resolve_org_wlan(OrgScope("o1"), "missing")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "org_wlan"


def test_resolve_org_wlan_membership_probe_failure_is_fetch_error():
    class WlanProbeBoom(FakeProvider):
        def _wlans(self, s: Any) -> list[dict[str, Any]]:
            raise RuntimeError(f"wlans unavailable for {s.site_id}")

    p = WlanProbeBoom(
        sites=_sites("s1"),
        ports=[],
        wired=[],
        org_wlans={"w1": {"id": "w1", "ssid": "corp"}},
    )

    result = p.resolve_org_wlan(OrgScope("o1"), "w1")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "org_wlan_membership"


def test_resolve_org_wlan_template_uses_derived_template_rows_for_membership():
    from digital_twin.providers.base import OrgScope, OrgWlanTemplateContext

    p = FakeProvider(
        sites=_sites("s1", "s2", "s3"),
        ports=[],
        wired=[],
        org_wlantemplates={"tmpl1": {"id": "tmpl1", "name": "guest-template"}},
        wlans_by_site={
            "s1": [
                {"id": "w2", "ssid": "iot", "template_id": "tmpl1"},
                {"id": "w1", "ssid": "guest", "template_id": "tmpl1"},
            ],
            "s2": [
                {"id": "other", "ssid": "guest", "template_id": "other-template"},
                {"id": "site-owned", "ssid": "corp"},
            ],
            "s3": [{"id": "w3", "ssid": "guest", "template_id": "tmpl1"}],
        },
    )

    ctx = p.resolve_org_wlan_template(OrgScope("o1"), "tmpl1")

    assert isinstance(ctx, OrgWlanTemplateContext)
    assert ctx.template == {"id": "tmpl1", "name": "guest-template"}
    assert set(ctx.derived_rows_by_site) == {"s1", "s3"}
    assert [row["id"] for row in ctx.derived_rows_by_site["s1"]] == ["w1", "w2"]
    assert [row["id"] for row in ctx.derived_rows_by_site["s3"]] == ["w3"]


def test_resolve_org_wlan_template_missing_template_is_fetch_error():
    p = FakeProvider(sites=_sites("s1"), ports=[], wired=[], org_wlantemplates={})

    result = p.resolve_org_wlan_template(OrgScope("o1"), "missing")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "org_wlantemplate"


def test_resolve_org_wlan_template_membership_probe_failure_is_fetch_error():
    class WlanProbeBoom(FakeProvider):
        def _wlans(self, s: Any) -> list[dict[str, Any]]:
            raise RuntimeError(f"wlans unavailable for {s.site_id}")

    p = WlanProbeBoom(
        sites=_sites("s1"),
        ports=[],
        wired=[],
        org_wlantemplates={"tmpl1": {"id": "tmpl1", "name": "guest-template"}},
    )

    result = p.resolve_org_wlan_template(OrgScope("o1"), "tmpl1")

    assert isinstance(result, FetchError)
    assert result.failures[0].object == "org_wlantemplate_membership"


# ---- Task 6: typed resolve_org_template + per-site gateway/site template tests ----

def test_resolve_org_template_gatewaytemplate_filters_by_gatewaytemplate_id():
    """resolve_org_template with object_type='gatewaytemplate' filters by
    gatewaytemplate_id and returns the fetched gatewaytemplate body."""
    from digital_twin.providers.base import OrgScope, OrgTemplateContext
    p = FakeProvider(
        sites=[
            {"id": "s1", "gatewaytemplate_id": "g1"},
            {"id": "s2", "gatewaytemplate_id": "g2"},
            {"id": "s3", "gatewaytemplate_id": "g1"},
        ],
        ports=[],
        wired=[],
        gatewaytemplates={"g1": {"id": "g1", "name": "gateway-one"}},
    )
    ctx = p.resolve_org_template(OrgScope(org_id="o1"), "g1", "gatewaytemplate")
    assert isinstance(ctx, OrgTemplateContext)
    assert set(ctx.assigned_site_ids) == {"s1", "s3"}
    assert ctx.template["id"] == "g1"
    assert ctx.template["name"] == "gateway-one"


def test_fetch_site_populates_sitetemplate_when_assigned():
    """A site with sitetemplate_id gets RawSiteState.sitetemplate populated."""
    p = FakeProvider(
        sites=[{"id": "s1", "sitetemplate_id": "st1"}],
        ports=[],
        wired=[],
        sitetemplates={"st1": {"id": "st1", "name": "site-tpl-one"}},
    )
    out = p.fetch_sites(OrgScope("o1"), ["s1"])
    raw = out["s1"]
    assert isinstance(raw, RawSiteState)
    assert raw.sitetemplate == {"id": "st1", "name": "site-tpl-one"}


def test_fetch_site_networktemplate_fetch_fail_returns_fetch_error():
    """REGRESSION (PR #4 review P1): a site assigned a networktemplate_id whose fetch
    raises → FetchError (UNKNOWN), not a RawSiteState with networktemplate=None. The
    networktemplate is a consumed layer of the switch IR (incl. in a gatewaytemplate/
    sitetemplate org run); silently dropping it compiles an incomplete IR that the
    verdict does not floor on (meta.failures alone never floors) → false-SAFE."""

    class FailingNetworktemplate(FakeProvider):
        def _networktemplate(self, s: Any, nt_id: str) -> dict[str, Any]:
            raise RuntimeError("network template 404")

    p = FailingNetworktemplate(sites=_sites("s1", nt="nt_missing"), ports=[], wired=[])
    out = p.fetch_sites(OrgScope("o1"), ["s1"])
    result = out["s1"]
    assert isinstance(result, FetchError), (
        f"Expected FetchError but got {type(result).__name__}: {result}"
    )
    assert any("networktemplate" in f.object for f in result.failures)


def test_fetch_site_unassigned_networktemplate_is_not_an_error():
    """A site with NO networktemplate_id is not floored — networktemplate stays None
    and the site is a normal RawSiteState (the guardrail only fires on a present id)."""
    p = FakeProvider(sites=_sites("s1"), ports=[], wired=[])
    raw = p.fetch_sites(OrgScope("o1"), ["s1"])["s1"]
    assert isinstance(raw, RawSiteState) and raw.networktemplate is None


def test_fetch_site_gatewaytemplate_fetch_fail_returns_fetch_error():
    """A site assigned a gatewaytemplate_id whose fetch raises → FetchError (UNKNOWN),
    not a RawSiteState with None gatewaytemplate."""

    class FailingGatewaytemplate(FakeProvider):
        def _gatewaytemplate(self, s: Any, gt_id: str) -> dict[str, Any]:
            raise RuntimeError("gateway template 404")

    p = FailingGatewaytemplate(
        sites=[{"id": "s1", "gatewaytemplate_id": "g_missing"}],
        ports=[],
        wired=[],
    )
    out = p.fetch_sites(OrgScope("o1"), ["s1"])
    result = out["s1"]
    assert isinstance(result, FetchError), (
        f"Expected FetchError but got {type(result).__name__}: {result}"
    )
    assert any("gatewaytemplate" in f.object for f in result.failures)
