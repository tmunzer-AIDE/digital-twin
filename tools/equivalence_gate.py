"""Tier-2 live gate: validate the compiler against Mist's own reality, per site.

Two independent checks (both must pass for exit 0):

1. CONFIG EQUIVALENCE — our merge_only() vs getSiteSettingDerived, restricted to
   the M1 in-scope SITE fields (networks/port_usages/vars). (merge_only, NOT
   compile_site: derived does not resolve {{vars}}.) This proves the site-level
   inputs the compiler consumes match Mist's derivation.

2. PORT-USAGE-NAME CROSS-CHECK — our compiled per-port usage NAME (switch_matching
   rule base + device overlay) vs the OBSERVED `port_usage` in port_stats. This is
   the only live oracle for the device-level port projection: port_config is
   device-level and absent from getSiteSettingDerived, and there is no structured
   device-derived endpoint. Scoped to STATIC ports (dynamic_usage off per member,
   since their observed usage is runtime, not config). ZERO matches = zero
   evidence = FAIL (a vacuous pass must not look green).

   HONESTY LIMIT: this validates usage-name ASSIGNMENT only, not the VLAN
   projection — port_stats carry no per-port VLAN fields (verified live), so a
   port_network override that moves a port's VLAN within the same usage name is
   invisible here. The override->VLAN layering is covered by Tier-1 unit tests
   (test_ingest_ports.py).

Usage:  uv run python tools/equivalence_gate.py
Env:    MIST_HOST, MIST_APITOKEN, DT_GATE_ORG_ID, DT_GATE_SITE_IDS (comma-separated)

GATE RULE: do not build Plans 3-5 on top until this passes on the target orgs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from digital_twin.adapters.mist.compile.equivalence import (
    IN_SCOPE_FIELDS,
    attribute_coverage,
    compare_effective,
    restrict_schema_to_scope,
    restrict_to_scope,
)
from digital_twin.adapters.mist.compile.switch import compile_device, merge_only
from digital_twin.adapters.mist.ingest.ports import resolve_effective_ports, resolve_port_bases
from digital_twin.providers.base import FetchError, OrgScope, RawSiteState
from digital_twin.providers.mist_api import MistApiProvider

OAS = Path("src/digital_twin/adapters/mist/oas/site_setting.schema.json")

_Mismatch = tuple[str, str, str, str | None, str]  # (site, device, port, ours, observed)


def _port_usage_crosscheck(raw: RawSiteState) -> tuple[int, list[tuple[str, str, str | None, str]]]:
    """Compiled per-port usage vs OBSERVED port_usage, for STATIC ports only.

    Compares the FULL effective port_config (switch_matching rule base + device
    overlay), excluding dynamic_usage ports per member (runtime usage, not config)."""
    nt = dict(raw.networktemplate) if raw.networktemplate else None
    setting = dict(raw.setting)
    matched = 0
    mismatches: list[tuple[str, str, str | None, str]] = []
    for d in raw.devices:
        if d.get("type") != "switch" or not d.get("mac"):
            continue
        eff = compile_device(nt, setting, dict(d))
        static = {
            member
            for member, base in resolve_port_bases(eff).items()
            if base.get("dynamic_usage") != "dynamic"
        }
        compiled = {member: name for member, _usage, name, _res in resolve_effective_ports(eff)}
        observed = {
            str(p["port_id"]): str(p["port_usage"])
            for p in raw.port_stats
            if str(p.get("mac")) == str(d["mac"]) and p.get("port_usage")
        }
        for member in static & observed.keys():
            if compiled.get(member) == observed[member]:
                matched += 1
            else:
                mismatches.append(
                    (str(d.get("name")), member, compiled.get(member), observed[member])
                )
    return matched, mismatches


def main() -> None:
    org_id = os.environ["DT_GATE_ORG_ID"]
    site_ids = [s for s in os.environ["DT_GATE_SITE_IDS"].split(",") if s]
    provider = MistApiProvider()
    failures = 0
    derived_samples: list[dict] = []
    xc_matched = 0
    xc_mismatches: list[_Mismatch] = []

    # one org-batched fetch (ports/wired-clients/device-stats/site-list batched)
    states = provider.fetch_sites(OrgScope(org_id), site_ids, include_derived=True)

    for site_id in site_ids:
        raw = states.get(site_id)
        if not isinstance(raw, RawSiteState):
            errs = (
                [f.error for f in raw.failures] if isinstance(raw, FetchError) else ["not returned"]
            )
            print(f"[FAIL] {site_id}: baseline fetch failed: {errs}")
            failures += 1
            continue
        if raw.derived_setting is None:
            print(
                f"[FAIL] {site_id}: derived_setting fetch failed: "
                f"{[f.error for f in raw.meta.failures]}"
            )
            failures += 1
            continue
        # (1) config equivalence — derived does NOT resolve {{vars}} -> pre-vars merge,
        # restricted to the M1 in-scope fields (out-of-scope domains = Tier-1's job).
        ours = restrict_to_scope(
            merge_only(
                dict(raw.networktemplate) if raw.networktemplate else None, dict(raw.setting)
            )
        )
        derived = restrict_to_scope(dict(raw.derived_setting))
        derived_samples.append(derived)
        result = compare_effective(ours, derived)
        if result.passed:
            note = (
                f" ({len(result.catalogued_diffs)} catalogued)" if result.catalogued_diffs else ""
            )
            print(f"[ OK ] {site_id}  (in-scope: {', '.join(IN_SCOPE_FIELDS)}){note}")
        else:
            failures += 1
            print(f"[FAIL] {site_id}: {len(result.diffs)} uncatalogued in-scope diff(s):")
            for d in result.diffs[:25]:
                print(f"         {d.path}: ours={d.ours!r} derived={d.derived!r}")
        # (2) port-projection cross-check
        matched, mismatches = _port_usage_crosscheck(raw)
        xc_matched += matched
        xc_mismatches.extend((site_id, *m) for m in mismatches)

    if OAS.exists() and derived_samples:
        schema = restrict_schema_to_scope(json.loads(OAS.read_text()))
        cov = attribute_coverage(schema, derived_samples)
        total = len(cov.covered) + len(cov.uncovered)
        print(f"\nin-scope attribute coverage: {len(cov.covered)}/{total} schema leaves exercised")
        for leaf in sorted(cov.uncovered)[:40]:
            print(f"  uncovered: {leaf}   (validated by Tier-1 OAS tests only)")

    print(
        f"\nport-usage-NAME cross-check (compiled vs observed, static ports): "
        f"{xc_matched} matched, {len(xc_mismatches)} mismatched"
    )
    for site_id, name, member, ours_u, obs_u in xc_mismatches[:25]:
        print(f"  MISMATCH {site_id} {name} {member}: ours={ours_u!r} observed={obs_u!r}")
    if xc_mismatches:
        failures += 1
    if not xc_matched:
        failures += 1
        print("  [FAIL] zero matches -> the cross-check produced NO positive evidence")
    print(
        "  scope: STATIC ports only (switch_matching rule base + device overlay);\n"
        "         dynamic_usage ports are runtime and excluded. Validates usage-name\n"
        "         assignment ONLY — no per-port VLAN is observable (port_stats carry\n"
        "         none); the override->VLAN layering is Tier-1-tested."
    )

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
