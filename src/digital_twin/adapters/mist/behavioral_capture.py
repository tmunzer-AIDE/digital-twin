"""Capture immutable configuration records through the existing Mist provider.

The caller owns the provider/session and supplies the requested sites explicitly.
No SDK session, credentials, SSH, renderer, or live request is created on import.
Raw statistics remain in the existing provider/replay boundary; this first capture
contract records configuration and acquisition failures, not their completeness
as behavioral observations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from digital_twin.behavioral import InputWindow, ObjectKey, Record, Snapshot
from digital_twin.providers.base import FetchError, OrgScope, StateProvider


def capture(
    provider: StateProvider,
    *,
    org_id: str,
    site_ids: Sequence[str],
    include_nac: bool = False,
) -> Snapshot:
    if not org_id or not site_ids or len(set(site_ids)) != len(site_ids):
        raise ValueError("capture requires an organization and unique explicit site identities")
    if any(not isinstance(site, str) or not site for site in site_ids):
        raise ValueError("site identities must be nonempty strings")
    if type(include_nac) is not bool:
        raise ValueError("NAC collection selection must be boolean")
    started = datetime.now(UTC)
    scope = OrgScope(org_id)
    results = provider.fetch_sites(scope, site_ids)
    records: dict[ObjectKey, Record] = {}
    windows: list[InputWindow] = []
    consistency: list[str] = []

    def add(
        kind: str,
        body: Mapping[str, Any],
        site: str = "",
        identity: str = "",
        *,
        platform: str = "",
        release: str = "",
    ) -> None:
        identifier = identity or body.get("id")
        if not isinstance(identifier, str) or not identifier:
            consistency.append(f"{kind}: missing object identity")
            return
        if body.get("id") and body["id"] != identifier:
            consistency.append(f"{kind}: object identity differs")
        if body.get("org_id") and body["org_id"] != org_id:
            consistency.append(f"{kind}: organization identity differs")
        if site and body.get("site_id") and body["site_id"] != site:
            consistency.append(f"{kind}: site identity differs")
        key = ObjectKey(org_id, kind, identifier, site)
        record = Record.create(key, body, platform=platform, release=release)
        if key in records and records[key] != record:
            consistency.append(f"{kind}: inconsistent duplicate object")
        else:
            records[key] = record

    for site_id in site_ids:
        raw = results.get(site_id)
        if raw is None or isinstance(raw, FetchError):
            windows.append(
                InputWindow(
                    f"site:{site_id}", started, datetime.now(UTC), False, "site capture unavailable"
                )
            )
            continue
        if raw.scope.org_id != org_id or raw.scope.site_id != site_id:
            windows.append(
                InputWindow(
                    f"site:{site_id}",
                    started,
                    datetime.now(UTC),
                    False,
                    "provider returned a different scope",
                )
            )
            continue
        required = {"site", "setting", "devices", "wlans", "org_networks"}
        missing = required - set(raw.meta.fetched)
        windows.append(
            InputWindow(
                f"site:{site_id}",
                min(started, raw.meta.acquired_at),
                raw.meta.acquired_at,
                raw.meta.is_complete and not missing,
                "; ".join((*[f.object for f in raw.meta.failures], *sorted(missing))),
            )
        )
        add("site", raw.site, site_id, site_id)
        add("site_setting", raw.setting, site_id, site_id)
        for kind, body, assignment in (
            ("network_template", raw.networktemplate, "networktemplate_id"),
            ("gateway_template", raw.gatewaytemplate, "gatewaytemplate_id"),
            ("site_template", raw.sitetemplate, "sitetemplate_id"),
        ):
            if body is not None:
                identity = raw.site.get(assignment) or body.get("id")
                if not isinstance(identity, str) or not identity:
                    consistency.append(f"{kind}: missing assignment identity")
                    continue
                if body.get("id") and body["id"] != identity:
                    consistency.append(f"{kind}: assignment identity differs")
                add(kind, body, identity=identity)
            elif raw.site.get(assignment):
                consistency.append(f"{kind}: assigned template is missing")
        stats = {d.get("mac"): d for d in raw.device_stats if d.get("mac")}
        for device in raw.devices:
            model = device.get("model", "")
            stat = stats.get(device.get("mac"), {})
            add(
                "device_" + str(device.get("type", "unknown")),
                device,
                site_id,
                platform=model if isinstance(model, str) else "",
                release=str(stat.get("version") or device.get("version") or ""),
            )
        for wlan in raw.wlans:
            add("wlan", wlan, site_id)
        for network in raw.org_networks:
            add("network", network)
    if include_nac:
        nac = provider.resolve_org_nac(scope)
        if isinstance(nac, FetchError):
            windows.append(
                InputWindow("nac", started, datetime.now(UTC), False, "NAC capture unavailable")
            )
        else:
            windows.append(
                InputWindow(
                    "nac",
                    started,
                    datetime.now(UTC),
                    not nac.tag_findings,
                    "NAC tag capture incomplete" if nac.tag_findings else "",
                )
            )
            for rule in nac.rules:
                add("nac_rule", rule)
            for tag in nac.tags:
                add("nac_tag", tag)
    windows.append(
        InputWindow(
            "capture_consistency",
            started,
            datetime.now(UTC),
            not consistency,
            "; ".join(consistency),
        )
    )
    windows.append(
        InputWindow(
            "behavioral_inventory",
            started,
            datetime.now(UTC),
            False,
            "selected-site configuration capture excludes complete organization membership, "
            "Mist Edge configuration, effective policy and behavioral observations",
        )
    )
    return Snapshot(org_id, tuple(records.values()), tuple(windows))
