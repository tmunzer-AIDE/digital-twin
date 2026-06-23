"""File-based replay store: (redacted raw, ChangePlan, verdict, trace) per run.

Debug/test artifact, NOT product state and NOT the deferred SnapshotProvider.
Redaction happens ON WRITE — there is no API to store un-redacted data.
FixtureProvider serves a saved fixture as a StateProvider for offline replay
and the golden-scenario suite.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digital_twin.observability.trace import Trace
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    NacFetch,
    OrgScope,
    OrgTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
)

from .redaction import REDACTION_VERSION, redact

_RAW_FIELDS = (
    "site",
    "setting",
    "networktemplate",
    "devices",
    "device_stats",
    "port_stats",
    "wireless_clients",
    "wired_clients",
    "wlans",
    "org_networks",
    "derived_setting",
    "sitetemplate",
    "gatewaytemplate",
    "nac_clients",
)


class ReplayStore:
    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def save_raw(self, run_id: str, raw: RawSiteState) -> Path:
        return self._write(run_id, self._raw_doc(raw))

    def save_run(
        self,
        run_id: str,
        *,
        raw: RawSiteState,
        plan: dict[str, Any],
        verdict_doc: dict[str, Any],
        trace: Trace,
    ) -> Path:
        doc = self._raw_doc(raw)
        doc["plan"] = redact(plan)
        doc["verdict"] = redact(verdict_doc)
        doc["trace"] = trace.to_dict()
        return self._write(run_id, doc)

    def _raw_doc(self, raw: RawSiteState) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "redaction_version": REDACTION_VERSION,
            "scope": redact({"org_id": raw.scope.org_id, "site_id": raw.scope.site_id}),
            "meta": {
                "acquired_at": raw.meta.acquired_at.isoformat(),
                "host": raw.meta.host,
                "fetched": list(raw.meta.fetched),
                "failures": [[f.object, f.error] for f in raw.meta.failures],
            },
        }
        for field in _RAW_FIELDS:
            payload[field] = redact(getattr(raw, field))
        return payload

    def _write(self, run_id: str, doc: dict[str, Any]) -> Path:
        path = self._dir / f"{run_id}.json"
        path.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
        return path


def load_fixture_doc(data: dict[str, Any]) -> RawSiteState:
    """Build a RawSiteState from one single-site fixture doc (already parsed)."""
    meta = data["meta"]
    return RawSiteState(
        scope=SiteScope(org_id=data["scope"]["org_id"], site_id=data["scope"]["site_id"]),
        site=data["site"],
        setting=data["setting"],
        networktemplate=data["networktemplate"],
        devices=tuple(data["devices"]),
        device_stats=tuple(data["device_stats"]),
        port_stats=tuple(data["port_stats"]),
        wireless_clients=tuple(data["wireless_clients"]),
        wired_clients=tuple(data["wired_clients"]),
        wlans=tuple(data.get("wlans", ())),  # .get: fixtures predating WLAN support
        org_networks=tuple(data.get("org_networks", ())),  # .get: pre-GS22 fixtures
        derived_setting=data["derived_setting"],
        sitetemplate=data.get("sitetemplate"),  # .get: pre-gateway-site-templates fixtures
        gatewaytemplate=data.get("gatewaytemplate"),  # .get: pre-gateway-site-templates fixtures
        nac_clients=tuple(data.get("nac_clients", ())),  # .get: pre-enrichment fixtures
        meta=StateMeta(
            acquired_at=datetime.fromisoformat(meta["acquired_at"]).astimezone(UTC),
            host=meta["host"],
            fetched=tuple(meta["fetched"]),
            failures=tuple(FetchFailure(object=o, error=e) for o, e in meta["failures"]),
        ),
    )


def load_fixture_raw(path: Path | str) -> RawSiteState:
    return load_fixture_doc(json.loads(Path(path).read_text()))


class FixtureProvider:
    """StateProvider over a saved fixture (offline replay / golden scenarios).

    SINGLE-SITE: the doc carries one site's raw fields directly. STRICT by
    default — asking for a different org/site than the fixture holds is a
    FetchError (-> UNKNOWN), never a silently-wrong verdict. strict=False is the
    test-only escape hatch.

    MULTI-SITE: the doc carries {"template": {<networktemplate>}, "sites":
    {"<sid>": {<single-site doc>}, ...}} where each site doc's site.
    networktemplate_id names the template it is assigned to. Optional
    "fetch_failures": ["<sid>", ...] marks sites whose fetch_sites entry is a
    FetchError (the org rollup -> UNKNOWN site_failures path). This shape drives
    resolve_org_template + fetch_sites for the org-template goldens; the
    single-site path stays untouched.
    """

    def __init__(self, path: Path | str, *, strict: bool = True) -> None:
        data = json.loads(Path(path).read_text())
        self._strict = strict
        self._data: dict[str, Any] = data  # raw parsed doc (for optional top-level sections)
        self._sites: dict[str, RawSiteState] = {}
        self._site_docs: dict[str, dict[str, Any]] = {}
        self._template: dict[str, Any] | None = None
        self._fetch_failures: frozenset[str] = frozenset()
        self._raw: RawSiteState | None = None  # set only for single-site fixtures
        self._multisite = "sites" in data
        if self._multisite:
            self._template = data.get("template")
            self._fetch_failures = frozenset(data.get("fetch_failures", ()))
            for sid, site_doc in data["sites"].items():
                self._site_docs[str(sid)] = site_doc
                self._sites[str(sid)] = load_fixture_doc(site_doc)
            # a representative host/org/time for FetchError construction (the
            # multi-site fixture's sites share one org — the replay authority)
            first = next(iter(self._sites.values()), None)
            self._host = first.meta.host if first is not None else ""
            self._org_id = first.scope.org_id if first is not None else ""
            self._acquired_at = first.meta.acquired_at if first is not None else datetime.now(UTC)
            # Build typed templates map: object_type -> {template_id -> body}
            # Start from the new "templates" key (typed shape), then fold in the
            # legacy single "template" key as "networktemplate" for back-compat.
            self._templates: dict[str, dict[str, dict[str, Any]]] = {}
            typed: dict[str, Any] = data.get("templates") or {}
            for obj_type, by_id in typed.items():
                self._templates[str(obj_type)] = {str(k): v for k, v in by_id.items()}
            if self._template is not None:
                template = self._template
                nt_map = self._templates.setdefault("networktemplate", {})
                nt_map.setdefault(str(template["id"]), template)
        else:  # single-site fixture (unchanged)
            self._raw = load_fixture_doc(data)
            self._host = self._raw.meta.host
            self._org_id = self._raw.scope.org_id
            self._acquired_at = self._raw.meta.acquired_at

    @property
    def _single(self) -> RawSiteState:
        if self._raw is None:
            raise ValueError("single-site API called on a multi-site fixture")
        return self._raw

    @property
    def fixture_scope(self) -> SiteScope:
        return self._single.scope

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        raw = self._single
        if self._strict and (
            scope.org_id != raw.scope.org_id or scope.site_id != raw.scope.site_id
        ):
            return FetchError(
                scope=scope,
                failures=(
                    FetchFailure(
                        object="fixture",
                        error=(
                            f"fixture holds {raw.scope.org_id}/"
                            f"{raw.scope.site_id}, not the requested scope"
                        ),
                    ),
                ),
                acquired_at=raw.meta.acquired_at,
                host=raw.meta.host,
            )
        return raw

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        if self._multisite:
            targets = list(site_ids) if site_ids is not None else list(self._sites)
            return {sid: self._fetch_multisite(scope, sid) for sid in targets}
        # the batched path applies the SAME strict guard as fetch_site —
        # no silent wrong-scope hole for offline replay callers
        targets = list(site_ids) if site_ids is not None else [self._single.scope.site_id]
        return {
            sid: self.fetch_site(SiteScope(org_id=scope.org_id, site_id=sid)) for sid in targets
        }

    def _wrong_org(self, scope: OrgScope) -> bool:
        # STRICT (like single-site fetch_site): replaying a fixture against a
        # different org than it captured must NOT silently succeed
        return self._strict and scope.org_id != self._org_id

    def _fetch_multisite(self, scope: OrgScope, sid: str) -> RawSiteState | FetchError:
        if self._wrong_org(scope):
            return FetchError(
                scope=SiteScope(org_id=scope.org_id, site_id=sid),
                failures=(
                    FetchFailure(
                        object="fixture",
                        error=f"fixture holds org {self._org_id}, not the requested {scope.org_id}",
                    ),
                ),
                acquired_at=self._acquired_at,
                host=self._host,
            )
        is_failure = sid in self._fetch_failures
        if is_failure or sid not in self._sites:
            error = (
                "site marked as a fetch failure in the fixture"
                if is_failure
                else "site not present in the multi-site fixture"
            )
            # keep the fixture's own timestamp when the site IS present (a marked
            # failure) so replay stays deterministic; only a truly-absent site
            # has no captured time to borrow
            acquired_at = (
                self._sites[sid].meta.acquired_at if sid in self._sites else datetime.now(UTC)
            )
            return FetchError(
                scope=SiteScope(org_id=scope.org_id, site_id=sid),
                failures=(FetchFailure(object="fixture", error=error),),
                acquired_at=acquired_at,
                host=self._host,
            )
        return self._sites[sid]

    def resolve_org_template(
        self, scope: OrgScope, template_id: str, object_type: str
    ) -> OrgTemplateContext | FetchError:
        """Multi-site: filter the fixture's sites to those whose site.
        <object_type>_id == template_id and return the shared template + their
        ids (0 assigned is a SUCCESS, per the contract — but only when the
        template EXISTS). Single-site fixtures do not carry any templates ->
        FetchError (-> UNKNOWN).

        Supports both the new typed 'templates' key and the legacy 'template'
        key (folded in as 'networktemplate' at construction time).
        """
        # Single-site fixtures carry no templates at all — always FetchError
        if not self._multisite or not self._templates:
            return FetchError(
                scope=scope,
                failures=(
                    FetchFailure(
                        object="org_template",
                        error="resolve_org_template not supported for single-site fixtures",
                    ),
                ),
                acquired_at=datetime.now(UTC),
                host=self._host,
            )
        if self._wrong_org(scope):
            return FetchError(
                scope=scope,
                failures=(
                    FetchFailure(
                        object="fixture",
                        error=f"fixture holds org {self._org_id}, not the requested {scope.org_id}",
                    ),
                ),
                acquired_at=self._acquired_at,
                host=self._host,
            )
        # template-not-found is a FetchError (-> UNKNOWN), NOT a 0-assigned SUCCESS:
        # a typo'd/missing template_id must never resolve SAFE — per type
        template = self._templates.get(object_type, {}).get(template_id)
        if template is None:
            return FetchError(
                scope=scope,
                failures=(
                    FetchFailure(
                        object=object_type,
                        error=f"{template_id} not found in the multi-site fixture",
                    ),
                ),
                acquired_at=self._acquired_at,
                host=self._host,
            )
        # filter assigned sites by the per-type id field
        id_field = f"{object_type}_id"
        assigned = tuple(
            sid
            for sid, doc in self._site_docs.items()
            if str((doc.get("site") or {}).get(id_field) or "") == template_id
        )
        return OrgTemplateContext(template=dict(template), assigned_site_ids=assigned)

    def resolve_org_nac(self, scope: OrgScope) -> NacFetch | FetchError:
        if self._wrong_org(scope):
            return FetchError(
                scope=scope,
                failures=(FetchFailure(object="org_nac",
                                       error=f"fixture holds org {self._org_id}, "
                                             f"not the requested {scope.org_id}"),),
                acquired_at=datetime.now(UTC), host=self._host)
        nac = self._data.get("nac") if isinstance(self._data, dict) else None
        if not nac:
            return FetchError(
                scope=scope,
                failures=(FetchFailure(object="org_nac",
                                       error="fixture carries no 'nac' section"),),
                acquired_at=datetime.now(UTC), host=self._host)
        return NacFetch(rules=tuple(nac.get("rules", ())),
                        tags=tuple(nac.get("tags", ())), tag_findings=())
