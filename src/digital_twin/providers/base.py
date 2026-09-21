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
    # observed BGP neighbor stats (GET /sites/{id}/stats/bgp_peers/search) — the
    # GS28 telemetry layer. Trailing + defaulted: absence is "not fetched".
    bgp_neighbors: tuple[JsonObj, ...] = ()


@dataclass(frozen=True)
class OrgTemplateContext:
    """Resolution of a networktemplate change: the current template JSON (the
    baseline SNAPSHOT) + the ids of every site assigned to it."""

    template: JsonObj
    assigned_site_ids: tuple[str, ...]


@dataclass(frozen=True)
class OrgWlanContext:
    """Resolution of an org WLAN change: the org-level WLAN snapshot plus each
    affected site's current derived WLAN row for that same WLAN id."""

    wlan: JsonObj
    derived_rows_by_site: Mapping[str, JsonObj]


@dataclass(frozen=True)
class OrgWlanTemplateContext:
    """Resolution of an org WLAN template delete: the current template snapshot
    plus each affected site's derived WLAN rows produced by that template."""

    template: JsonObj
    derived_rows_by_site: Mapping[str, tuple[JsonObj, ...]]
    # Org WLAN definitions that belong to this template, including when the
    # template is not currently assigned to any site.
    template_wlans: tuple[JsonObj, ...] = ()
    template_wlans_complete: bool = False


@dataclass(frozen=True)
class OrgSiteGroupContext:
    """Current site-group membership used to guard deletion."""

    assigned_site_ids: tuple[str, ...]


@dataclass(frozen=True)
class OrgNetworksContext:
    """Current organization gateway-network namespace."""

    networks: tuple[JsonObj, ...]


@dataclass(frozen=True)
class PskUsageContext:
    """Observed PSK sessions in a bounded lookback window.

    ``failures`` is intentionally retained alongside successful observations:
    one active site is enough to require review, while a clean SAFE conclusion
    requires every target site query to have succeeded.
    """

    active_site_ids: tuple[str, ...]
    checked_site_ids: tuple[str, ...]
    failures: tuple[FetchFailure, ...]
    window_days: int


@dataclass(frozen=True)
class WlanUsageContext:
    """Observed WLAN client sessions in a bounded lookback window."""

    active_site_ids: tuple[str, ...]
    checked_site_ids: tuple[str, ...]
    failures: tuple[FetchFailure, ...]
    window_days: int


@dataclass(frozen=True)
class NacRuleUsageContext:
    """Observed NAC client matches in a bounded lookback window."""

    active_site_ids: tuple[str, ...]
    checked_site_ids: tuple[str, ...]
    failures: tuple[FetchFailure, ...]
    window_days: int


@dataclass(frozen=True)
class ObjectReference:
    """One exact reference to a configuration object.

    ``path`` is the vendor-shaped JSON path where the target id/name was found;
    keeping it makes relationship decisions auditable without teaching the
    policy layer every Mist payload shape.
    """

    source_type: str
    source_id: str
    path: str
    site_id: str | None = None
    source_name: str | None = None


@dataclass(frozen=True)
class ObjectRelationshipContext:
    """Target object plus the configuration rows that reference it.

    A clean "unused" conclusion is valid only when ``failures`` is empty.
    References and failures may coexist: a known reference is still actionable,
    while the failures make the overall relationship coverage partial.
    """

    target: JsonObj
    references: tuple[ObjectReference, ...]
    checked_sources: tuple[str, ...]
    failures: tuple[FetchFailure, ...] = ()


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

    def resolve_org_wlan(self, scope: OrgScope, wlan_id: str) -> OrgWlanContext | FetchError:
        """Fetch the org WLAN snapshot and determine affected sites from their
        derived WLAN rows. A lookup or membership-probe failure is a FetchError."""
        ...

    def resolve_org_wlan_template(
        self, scope: OrgScope, template_id: str
    ) -> OrgWlanTemplateContext | FetchError:
        """Fetch the org WLAN template snapshot and determine affected sites from
        derived WLAN rows carrying that template_id. A lookup or membership-probe
        failure is a FetchError."""
        ...

    def resolve_org_sitegroup(
        self, scope: OrgScope, sitegroup_id: str
    ) -> OrgSiteGroupContext | FetchError:
        """Fetch a site group and return its currently assigned site ids."""
        ...

    def resolve_org_networks(
        self, scope: OrgScope
    ) -> OrgNetworksContext | FetchError:
        """Fetch the org gateway-network namespace for create validation."""
        ...

    def resolve_psk_usage(
        self, scope: OrgScope | SiteScope, psk_id: str, *, window_days: int = 7
    ) -> PskUsageContext | FetchError:
        """Find sites with sessions using ``psk_id`` in the lookback window."""
        ...

    def resolve_object_relationships(
        self, scope: OrgScope, object_type: str, object_id: str
    ) -> ObjectRelationshipContext | FetchError:
        """Resolve exact id/name references to one org configuration object.

        Implementations must report every failed relevant source in the returned
        context (or return FetchError when the target itself cannot be read), so
        callers never mistake incomplete discovery for an unused object.
        """
        ...

    def resolve_wlan_usage(
        self, scope: OrgScope | SiteScope, wlan_id: str, *, window_days: int = 7
    ) -> WlanUsageContext | FetchError:
        """Find sites with client sessions on ``wlan_id`` in the lookback window."""
        ...

    def resolve_nacrule_usage(
        self, scope: OrgScope, nacrule_id: str, *, window_days: int = 7
    ) -> NacRuleUsageContext | FetchError:
        """Find sites where ``nacrule_id`` matched clients in the lookback window."""
        ...

    def resolve_org_nac(self, scope: OrgScope) -> NacFetch | FetchError:
        """Fetch the org's NAC rules and tags. A nacrules failure is a total
        FetchError; a nactags failure yields NacFetch(rules, (), (tag_finding,))
        so downstream can still apply rules without label resolution."""
        ...
