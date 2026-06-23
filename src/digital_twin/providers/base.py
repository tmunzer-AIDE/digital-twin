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

from digital_twin.contracts.finding import Finding

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
    # site WLAN configs (GET /sites/{id}/wlans) — AP VLAN requirements. Defaulted
    # (and trailing) so existing constructors/fixtures predating it stay valid;
    # absence is "not fetched", which leaves the WLAN_CONFIG capability unearned.
    wlans: tuple[JsonObj, ...] = ()
    # ORG networks (GET /orgs/{org}/networks) — the GATEWAY's network
    # namespace: name -> (vlan_id, subnet). Gateways reference these by name in
    # port_config/ip_configs; site/template networks are the SWITCH namespace.
    # Defaulted: absence leaves gateway carriage vlan-blind (never config-empty).
    org_networks: tuple[JsonObj, ...] = ()
    # assigned sitetemplate / gatewaytemplate bodies (None = not assigned/not fetched).
    # Trailing + defaulted so every existing constructor/fixture stays valid.
    sitetemplate: JsonObj | None = None
    gatewaytemplate: JsonObj | None = None
    # observed NAC clients (GET /orgs/{org}/nac_clients/search, site-filtered) —
    # OBSERVATIONAL enrichment only (fingerprint + auth/NAC identity for the
    # client.impact report). Trailing + defaulted: absence is "not fetched" and
    # is NON-FATAL (best-effort enrichment, never earns/loses a capability).
    nac_clients: tuple[JsonObj, ...] = ()
    # observed OSPF neighbor stats (GET /sites/{id}/stats/ospf_peers/search) — the
    # GS27 telemetry layer. Trailing + defaulted: absence is "not fetched".
    ospf_neighbors: tuple[JsonObj, ...] = ()


@dataclass(frozen=True)
class OrgTemplateContext:
    """Resolution of a networktemplate change: the current template JSON (the
    baseline SNAPSHOT) + the ids of every site assigned to it."""

    template: JsonObj
    assigned_site_ids: tuple[str, ...]


@dataclass(frozen=True)
class NacFetch:
    """Org-level NAC fetch result: rule payloads + tag payloads (vendor-shaped).

    `tag_findings` carries OPERATIONAL/WARNING diagnostics when nactags could not
    be fetched (labels-only degradation — the rules themselves are still usable).
    A nacrules failure produces a FetchError instead (whole fetch is fatal).
    """

    rules: tuple[Mapping[str, Any], ...]
    tags: tuple[Mapping[str, Any], ...]
    tag_findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class FetchError:
    """Total fetch failure — no usable baseline (site/setting could not be read).

    A VALUE, not an exception: callers must narrow `RawSiteState | FetchError`,
    and Plan 3's pipeline maps this to decision UNKNOWN.
    """

    scope: SiteScope | OrgScope
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

    def resolve_org_template(
        self, scope: OrgScope, template_id: str, object_type: str
    ) -> OrgTemplateContext | FetchError:
        """List the org's sites, filter to those whose networktemplate_id ==
        template_id, and fetch the template. A lookup failure (sites or template)
        is a FetchError (whole-plan UNKNOWN). 0 assigned sites is a SUCCESS with
        an empty assigned_site_ids tuple."""
        ...

    def resolve_org_nac(self, scope: OrgScope) -> NacFetch | FetchError:
        """Fetch the org's NAC rules and tags. A nacrules failure is a total
        FetchError; a nactags failure yields NacFetch(rules, (), (tag_finding,))
        so downstream can still apply rules without label resolution."""
        ...
