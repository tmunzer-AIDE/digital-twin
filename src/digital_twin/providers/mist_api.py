"""MistApiProvider: on-demand fetch via the mistapi SDK (single site or whole org).

Every endpoint call is isolated in a small private method so the probe script
(tools/probe_fetch.py) can validate exact SDK call names / response shapes per
SDK release, and a fix is a one-liner. Partial failures are RECORDED in
StateMeta; a failed BASELINE fetch (site/setting) returns a FetchError VALUE —
this provider never raises for fetch problems.

`fetch_sites` (multi-site) batches the org-level endpoints whose payload matches
the per-site call — `searchOrgSwOrGwPorts`, `searchOrgWiredClients`, `listOrgSites`
and `listOrgDevicesStats` (the last REQUIRES fields="*" or the rows are lean and
drop lldp_stat) — into one paged call each and partitions by `site_id`. Two stay
per-site ON PURPOSE: `listOrgDevices` is inventory-only (no port_config), and
`searchOrgWirelessClients` has different shape/semantics than the per-site stats.
Network templates are fetched once per unique id and reused across sites.

NOTE: SDK call names below are probe-validated (tools/probe_fetch.py); re-run the
probe after an SDK bump before trusting the equivalence gate.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import mistapi

from .base import (
    FetchError,
    FetchFailure,
    OrgScope,
    OrgTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
    StateProvider,
)

_Json = dict[str, Any]
_Attempt = Callable[[str, Callable[[], Any], Any], Any]


def _now() -> datetime:
    return datetime.now(UTC)


def _group_by_site(rows: list[_Json]) -> dict[str, list[_Json]]:
    """Index org-wide rows by their `site_id` (rows lacking one are dropped)."""
    grouped: dict[str, list[_Json]] = {}
    for row in rows:
        sid = row.get("site_id")
        if sid is not None:
            grouped.setdefault(str(sid), []).append(row)
    return grouped


def _site_thunk(
    sites: dict[str, _Json], scope: SiteScope, fetch: Callable[[SiteScope], _Json]
) -> Callable[[], _Json | None]:
    """Serve the site object from the org-batched list; fall back to the per-site
    getSiteInfo when the org list failed or lacks the site (a single org-call
    failure must degrade, not turn every site into a baseline failure)."""
    return lambda: sites.get(scope.site_id) or fetch(scope)


def _slice_thunk(index: Callable[[str], list[_Json]], sid: str) -> Callable[[], list[_Json]]:
    return lambda: index(sid)


class MistApiProvider(StateProvider):
    def __init__(self, host: str | None = None, apitoken: str | None = None) -> None:
        self._host = host or os.environ["MIST_HOST"]
        self._session = mistapi.APISession(
            host=self._host, apitoken=apitoken or os.environ["MIST_APITOKEN"]
        )

    # -- public seam -----------------------------------------------------------
    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        return self._fetch_one(
            scope,
            site_fn=lambda: self._site(scope),
            port_stats_fn=lambda: self._port_stats(scope),
            wired_clients_fn=lambda: self._wired_clients(scope),
            device_stats_fn=lambda: self._device_stats(scope),
            nt_cache={},
            include_derived=include_derived,
        )

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        # one org-level call per batchable domain, then partition by site_id
        try:
            sites = {str(s["id"]): s for s in self._org_sites(scope) if s.get("id")}
        except Exception:  # noqa: BLE001 — degrades to per-site baseline failure below
            sites = {}
        targets = [str(s) for s in site_ids] if site_ids is not None else list(sites)
        port_slice = self._org_slice(lambda: self._org_port_stats(scope))
        wired_slice = self._org_slice(lambda: self._org_wired_clients(scope))
        device_slice = self._org_slice(lambda: self._org_device_stats(scope))
        nt_cache: dict[str, _Json | None] = {}
        out: dict[str, RawSiteState | FetchError] = {}
        for sid in targets:
            out[sid] = self._fetch_one(
                SiteScope(scope.org_id, sid),
                site_fn=_site_thunk(sites, SiteScope(scope.org_id, sid), self._site),
                port_stats_fn=_slice_thunk(port_slice, sid),
                wired_clients_fn=_slice_thunk(wired_slice, sid),
                device_stats_fn=_slice_thunk(device_slice, sid),
                nt_cache=nt_cache,
                include_derived=include_derived,
            )
        return out

    def resolve_org_template(
        self, scope: OrgScope, template_id: str
    ) -> OrgTemplateContext | FetchError:
        try:
            sites = self._org_sites(scope)
            template = self._networktemplate(SiteScope(scope.org_id, ""), template_id)
        except Exception as exc:  # noqa: BLE001 — total lookup failure is a VALUE
            return FetchError(
                scope=scope,
                failures=(FetchFailure(object="org_template", error=str(exc)),),
                acquired_at=_now(),
                host=self._host,
            )
        # defensive: the live `_networktemplate` raises (-> the except above) on a
        # missing id, but a subclass/SDK change could return None instead of raising
        if template is None:
            return FetchError(
                scope=scope,
                failures=(
                    FetchFailure(object="networktemplate", error=f"{template_id} not found"),
                ),
                acquired_at=_now(),
                host=self._host,
            )
        assigned = tuple(
            str(s["id"]) for s in sites
            if s.get("id") and str(s.get("networktemplate_id") or "") == template_id
        )
        return OrgTemplateContext(template=dict(template), assigned_site_ids=assigned)

    # -- shared per-site assembly ---------------------------------------------
    def _fetch_one(
        self,
        scope: SiteScope,
        *,
        site_fn: Callable[[], _Json | None],
        port_stats_fn: Callable[[], list[_Json]],
        wired_clients_fn: Callable[[], list[_Json]],
        device_stats_fn: Callable[[], list[_Json]],
        nt_cache: dict[str, _Json | None],
        include_derived: bool,
    ) -> RawSiteState | FetchError:
        """Assemble one site's RawSiteState. `site`, `port_stats`, `wired_clients`
        and `device_stats` are supplied as thunks so the per-site and org-batched
        callers share this path; everything else is fetched here per-site."""
        fetched: list[str] = []
        failures: list[FetchFailure] = []

        def attempt(name: str, fn: Callable[[], Any], default: Any) -> Any:
            try:
                result = fn()
                fetched.append(name)
                return result
            except Exception as e:  # noqa: BLE001 — recorded, surfaced via StateMeta
                failures.append(FetchFailure(object=name, error=str(e)))
                return default

        # baseline: without site+setting there is nothing to simulate against
        site = attempt("site", site_fn, None)
        setting = attempt("setting", lambda: self._setting(scope), None)
        if site is None or setting is None:
            return FetchError(
                scope=scope, failures=tuple(failures), acquired_at=_now(), host=self._host
            )

        networktemplate = self._templatecached(scope, site, nt_cache, fetched, attempt)
        derived = (
            attempt("derived_setting", lambda: self._derived(scope), None)
            if include_derived
            else None
        )
        return RawSiteState(
            scope=scope,
            site=site,
            setting=setting,
            networktemplate=networktemplate,
            devices=tuple(attempt("devices", lambda: self._devices(scope), [])),
            device_stats=tuple(attempt("device_stats", device_stats_fn, [])),
            port_stats=tuple(attempt("port_stats", port_stats_fn, [])),
            wireless_clients=tuple(
                attempt("wireless_clients", lambda: self._wireless_clients(scope), [])
            ),
            wired_clients=tuple(attempt("wired_clients", wired_clients_fn, [])),
            wlans=tuple(attempt("wlans", lambda: self._wlans(scope), [])),
            org_networks=tuple(attempt("org_networks", lambda: self._org_networks(scope), [])),
            derived_setting=derived,
            meta=StateMeta(
                acquired_at=_now(),
                host=self._host,
                fetched=tuple(fetched),
                failures=tuple(failures),
            ),
        )

    def _templatecached(
        self,
        scope: SiteScope,
        site: _Json,
        nt_cache: dict[str, _Json | None],
        fetched: list[str],
        attempt: _Attempt,
    ) -> _Json | None:
        """Network templates are org-level and shared: fetch each unique id once,
        reuse across sites. Only successful fetches are cached (a failure retries
        so its gap stays recorded per site)."""
        nt_id = site.get("networktemplate_id")
        if not nt_id:
            return None
        nt_id = str(nt_id)
        if nt_id in nt_cache:
            fetched.append("networktemplate")  # reused from an earlier site this batch
            return nt_cache[nt_id]
        result: _Json | None = attempt(
            "networktemplate", lambda: self._networktemplate(scope, nt_id), None
        )
        if result is not None:
            nt_cache[nt_id] = result
        return result

    def _org_slice(self, fetch: Callable[[], list[_Json]]) -> Callable[[str], list[_Json]]:
        """Fetch an org-wide list ONCE and index by site_id. On fetch failure the
        returned indexer RAISES, so each site records the gap in its own meta."""
        try:
            grouped = _group_by_site(fetch())
        except Exception as e:  # noqa: BLE001 — replayed per site via the indexer

            def _raise(_sid: str, _e: Exception = e) -> list[_Json]:
                raise _e

            return _raise
        return lambda sid: grouped.get(sid, [])

    # -- one private helper per endpoint (probe-validated names) ---------------
    def _site(self, s: SiteScope) -> _Json:
        return dict(mistapi.api.v1.sites.sites.getSiteInfo(self._session, s.site_id).data)

    def _setting(self, s: SiteScope) -> _Json:
        return dict(mistapi.api.v1.sites.setting.getSiteSetting(self._session, s.site_id).data)

    def _derived(self, s: SiteScope) -> _Json:
        return dict(
            mistapi.api.v1.sites.setting.getSiteSettingDerived(self._session, s.site_id).data
        )

    def _networktemplate(self, s: SiteScope, nt_id: str) -> _Json:
        return dict(
            mistapi.api.v1.orgs.networktemplates.getOrgNetworkTemplate(
                self._session, s.org_id, nt_id
            ).data
        )

    def _devices(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.devices.listSiteDevices(self._session, s.site_id, type="all")
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _device_stats(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.stats.listSiteDevicesStats(self._session, s.site_id, type="all")
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _port_stats(self, s: SiteScope) -> list[_Json]:
        # get_all pages the search response — never just the first 1000 results
        resp = mistapi.api.v1.sites.stats.searchSiteSwOrGwPorts(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _wireless_clients(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.stats.listSiteWirelessClientsStats(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _wired_clients(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.wired_clients.searchSiteWiredClients(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _wlans(self, s: SiteScope) -> list[_Json]:
        # site WLAN config (AP VLAN requirements) — the DERIVED list, which
        # merges org-template WLANs into the site's effective set. Real orgs
        # commonly define every SSID at org level via wlantemplates, leaving
        # the plain per-site list EMPTY (found in real use, 2026-06-10) — the
        # twin would then falsely "know" there are no WLAN requirements.
        resp = mistapi.api.v1.sites.wlans.listSiteWlansDerived(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _org_networks(self, s: SiteScope) -> list[_Json]:
        # the GATEWAY's network namespace: org networks carry name + vlan_id +
        # subnet; gateway port_config/ip_configs reference them BY NAME (the
        # site/template networks are the switch namespace — different names,
        # found in real use 2026-06-11). NOTE: the list endpoint omits unset
        # fields; vlan_id is present on the full objects where configured.
        resp = mistapi.api.v1.orgs.networks.listOrgNetworks(self._session, s.org_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    # -- org-batched endpoints (payload identical to the per-site call) --------
    def _org_sites(self, s: OrgScope) -> list[_Json]:
        resp = mistapi.api.v1.orgs.sites.listOrgSites(self._session, s.org_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _org_device_stats(self, s: OrgScope) -> list[_Json]:
        # fields="*" is REQUIRED at org scope — without it the rows are lean and
        # drop lldp_stat (AP-uplink detection); the per-site call returns it by default.
        resp = mistapi.api.v1.orgs.stats.listOrgDevicesStats(
            self._session, s.org_id, type="all", fields="*"
        )
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _org_port_stats(self, s: OrgScope) -> list[_Json]:
        resp = mistapi.api.v1.orgs.stats.searchOrgSwOrGwPorts(self._session, s.org_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _org_wired_clients(self, s: OrgScope) -> list[_Json]:
        resp = mistapi.api.v1.orgs.wired_clients.searchOrgWiredClients(self._session, s.org_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]
