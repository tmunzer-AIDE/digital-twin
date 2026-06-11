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
    OrgScope,
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


def load_fixture_raw(path: Path | str) -> RawSiteState:
    data = json.loads(Path(path).read_text())
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
        meta=StateMeta(
            acquired_at=datetime.fromisoformat(meta["acquired_at"]).astimezone(UTC),
            host=meta["host"],
            fetched=tuple(meta["fetched"]),
            failures=tuple(FetchFailure(object=o, error=e) for o, e in meta["failures"]),
        ),
    )


class FixtureProvider:
    """StateProvider over ONE saved fixture (offline replay / golden scenarios).

    STRICT by default: scope is explicit in the contract, so asking for a
    different org/site than the fixture holds is a FetchError (-> UNKNOWN),
    never a silently-wrong verdict. strict=False is the test-only escape hatch.
    """

    def __init__(self, path: Path | str, *, strict: bool = True) -> None:
        self._raw = load_fixture_raw(path)
        self._strict = strict

    @property
    def fixture_scope(self) -> SiteScope:
        return self._raw.scope

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        if self._strict and (
            scope.org_id != self._raw.scope.org_id or scope.site_id != self._raw.scope.site_id
        ):
            return FetchError(
                scope=scope,
                failures=(
                    FetchFailure(
                        object="fixture",
                        error=(
                            f"fixture holds {self._raw.scope.org_id}/"
                            f"{self._raw.scope.site_id}, not the requested scope"
                        ),
                    ),
                ),
                acquired_at=self._raw.meta.acquired_at,
                host=self._raw.meta.host,
            )
        return self._raw

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        # the batched path applies the SAME strict guard as fetch_site —
        # no silent wrong-scope hole for offline replay callers
        targets = list(site_ids) if site_ids is not None else [self._raw.scope.site_id]
        return {
            sid: self.fetch_site(SiteScope(org_id=scope.org_id, site_id=sid)) for sid in targets
        }
