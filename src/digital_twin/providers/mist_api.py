"""MistApiProvider: on-demand single-site fetch via the mistapi SDK.

Every endpoint call is isolated in a small private method so the probe script
(tools/probe_fetch.py) can validate exact SDK call names / response shapes per
SDK release, and a fix is a one-liner. Partial failures are RECORDED in
StateMeta; a failed BASELINE fetch (site/setting) returns a FetchError VALUE —
this provider never raises for fetch problems.

NOTE: the SDK call names below are best-effort and MUST be validated by running
tools/probe_fetch.py against a real org before trusting the equivalence gate.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import mistapi

from .base import FetchError, FetchFailure, RawSiteState, SiteScope, StateMeta, StateProvider

_Json = dict[str, Any]


class MistApiProvider(StateProvider):
    def __init__(self, host: str | None = None, apitoken: str | None = None) -> None:
        self._host = host or os.environ["MIST_HOST"]
        self._session = mistapi.APISession(
            host=self._host, apitoken=apitoken or os.environ["MIST_APITOKEN"]
        )

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
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
        site = attempt("site", lambda: self._site(scope), None)
        setting = attempt("setting", lambda: self._setting(scope), None)
        if site is None or setting is None:
            return FetchError(
                scope=scope,
                failures=tuple(failures),
                acquired_at=datetime.now(UTC),
                host=self._host,
            )

        nt_id = site.get("networktemplate_id")
        networktemplate = (
            attempt("networktemplate", lambda: self._networktemplate(scope, str(nt_id)), None)
            if nt_id
            else None
        )
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
            device_stats=tuple(attempt("device_stats", lambda: self._device_stats(scope), [])),
            port_stats=tuple(attempt("port_stats", lambda: self._port_stats(scope), [])),
            wireless_clients=tuple(
                attempt("wireless_clients", lambda: self._wireless_clients(scope), [])
            ),
            wired_clients=tuple(attempt("wired_clients", lambda: self._wired_clients(scope), [])),
            derived_setting=derived,
            meta=StateMeta(
                acquired_at=datetime.now(UTC),
                host=self._host,
                fetched=tuple(fetched),
                failures=tuple(failures),
            ),
        )

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
        resp = mistapi.api.v1.sites.stats.searchSiteSwOrGwPorts(
            self._session, s.site_id, limit=1000
        )
        return [dict(d) for d in (resp.data or {}).get("results", [])]

    def _wireless_clients(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.stats.listSiteWirelessClientsStats(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _wired_clients(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.wired_clients.searchSiteWiredClients(
            self._session, s.site_id, limit=1000
        )
        return [dict(d) for d in (resp.data or {}).get("results", [])]
