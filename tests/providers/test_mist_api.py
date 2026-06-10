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
    ) -> None:
        self._host = "test"
        self._session = None  # type: ignore[assignment]  # privates below never touch it
        self._sites = sites
        self._ports = ports
        self._wired = wired
        self._device_stats_rows = device_stats or []
        self._templates = templates or {}
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

    def _derived(self, s: Any) -> dict[str, Any]:
        return {"_derived": s.site_id}

    def _networktemplate(self, s: Any, nt_id: str) -> dict[str, Any]:
        self.nt_calls.append(nt_id)
        return self._templates[nt_id]


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
