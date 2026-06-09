"""StateProvider seam: raw vendor state for a scope, plus freshness metadata.

RawSiteState holds VENDOR-SHAPED payloads (dicts as returned by the API) — the
adapter standardizes them; nothing else may interpret them. `derived_setting` is
fetched ONLY for the equivalence gate (the oracle); the live pipeline never uses it.
Total fetch failure is a VALUE (FetchError), never an exception.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

JsonObj = Mapping[str, Any]


@dataclass(frozen=True)
class SiteScope:
    org_id: str
    site_id: str


@dataclass(frozen=True)
class OrgScope:
    org_id: str


@dataclass(frozen=True)
class FetchFailure:
    object: str  # which fetch failed, e.g. "port_stats"
    error: str


@dataclass(frozen=True)
class StateMeta:
    acquired_at: datetime
    host: str
    fetched: tuple[str, ...]  # which objects were fetched successfully
    failures: tuple[FetchFailure, ...]

    @property
    def is_complete(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class RawSiteState:
    scope: SiteScope
    site: JsonObj  # GET /sites/{id} — carries networktemplate_id etc.
    setting: JsonObj  # GET /sites/{id}/setting (raw, pre-derive)
    networktemplate: JsonObj | None  # GET /orgs/{org}/networktemplates/{id}
    devices: tuple[JsonObj, ...]  # device configs (switches + aps)
    device_stats: tuple[JsonObj, ...]  # per-device stats (AP lldp_stat lives here)
    port_stats: tuple[JsonObj, ...]  # switch port stats (LLDP neighbors, STP, LAG)
    wireless_clients: tuple[JsonObj, ...]
    wired_clients: tuple[JsonObj, ...]
    derived_setting: JsonObj | None  # ORACLE ONLY (equivalence gate)
    meta: StateMeta


@dataclass(frozen=True)
class FetchError:
    """Total fetch failure — no usable baseline (site/setting could not be read).

    A VALUE, not an exception: callers must narrow `RawSiteState | FetchError`,
    and Plan 3's pipeline maps this to decision UNKNOWN.
    """

    scope: SiteScope
    failures: tuple[FetchFailure, ...]
    acquired_at: datetime
    host: str


class StateProvider(Protocol):
    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError: ...

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        """Fetch many sites of an org. `site_ids=None` means all sites in the org.
        Returns a per-site result map; one site's failure never sinks the others.
        Implementations SHOULD batch org-level endpoints where the payload is
        identical to the per-site call (see MistApiProvider)."""
        ...
