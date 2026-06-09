"""Tier-2 equivalence gate: our merge_only() vs getSiteSettingDerived, per real site.

(merge_only, NOT compile_site: derived does not resolve {{vars}} — confirmed.)

Usage:  uv run python tools/equivalence_gate.py
Env:    MIST_HOST, MIST_APITOKEN, DT_GATE_ORG_ID, DT_GATE_SITE_IDS (comma-separated)

Per site: fetch raw + derived, merge ours, compare. Exit 0 only if EVERY site
has zero uncatalogued diffs. Always prints the attribute-coverage report — a
green gate explicitly states what real data did NOT exercise (Tier 1 covers it).

GATE RULE: do not build Plans 3-5 on top until this passes on the target orgs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from digital_twin.adapters.mist.compile.equivalence import attribute_coverage, compare_effective
from digital_twin.adapters.mist.compile.switch import merge_only
from digital_twin.providers.base import FetchError, SiteScope
from digital_twin.providers.mist_api import MistApiProvider

OAS = Path("src/digital_twin/adapters/mist/oas/site_setting.schema.json")


def main() -> None:
    org_id = os.environ["DT_GATE_ORG_ID"]
    site_ids = [s for s in os.environ["DT_GATE_SITE_IDS"].split(",") if s]
    provider = MistApiProvider()
    failures = 0
    derived_samples: list[dict] = []

    for site_id in site_ids:
        raw = provider.fetch_site(SiteScope(org_id, site_id), include_derived=True)
        if isinstance(raw, FetchError):
            print(f"[FAIL] {site_id}: baseline fetch failed: {[f.error for f in raw.failures]}")
            failures += 1
            continue
        if raw.derived_setting is None:
            print(
                f"[FAIL] {site_id}: derived_setting fetch failed: "
                f"{[f.error for f in raw.meta.failures]}"
            )
            failures += 1
            continue
        # derived does NOT resolve {{vars}} (confirmed) -> compare the pre-vars merge
        ours = merge_only(
            dict(raw.networktemplate) if raw.networktemplate else None, dict(raw.setting)
        )
        derived = dict(raw.derived_setting)
        derived_samples.append(derived)
        result = compare_effective(ours, derived)
        if result.passed:
            note = (
                f" ({len(result.catalogued_diffs)} catalogued)" if result.catalogued_diffs else ""
            )
            print(f"[ OK ] {site_id}{note}")
        else:
            failures += 1
            print(f"[FAIL] {site_id}: {len(result.diffs)} uncatalogued diff(s):")
            for d in result.diffs[:25]:
                print(f"         {d.path}: ours={d.ours!r} derived={d.derived!r}")

    if OAS.exists() and derived_samples:
        cov = attribute_coverage(json.loads(OAS.read_text()), derived_samples)
        total = len(cov.covered) + len(cov.uncovered)
        print(f"\nattribute coverage: {len(cov.covered)}/{total} schema leaves exercised")
        for leaf in sorted(cov.uncovered)[:40]:
            print(f"  uncovered: {leaf}   (validated by Tier-1 OAS tests only)")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
