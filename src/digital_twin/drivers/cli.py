"""CLI driver: ChangePlan JSON in -> verdict out, decision-coded exit status.

Exit codes (spec): SAFE=0, REVIEW=10, UNSAFE=20, UNKNOWN=30 — only SAFE is
success, because everything else means "do not apply automatically".
Providers: live Mist (env MIST_HOST/MIST_APITOKEN) or --replay-fixture (offline).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from digital_twin.drivers.composite import (
    needs_composite_evaluation,
    simulate_composite,
)
from digital_twin.drivers.render import (
    org_nac_verdict_to_dict,
    org_verdict_to_dict,
    render_human,
    render_org_human,
    render_org_nac_human,
    verdict_to_dict,
)
from digital_twin.engine.pipeline import (
    simulate,
    simulate_name_change,
    simulate_org_nac,
    simulate_org_template,
)
from digital_twin.engine.run_context import RunContext
from digital_twin.observability.replay.store import FixtureProvider, ReplayStore
from digital_twin.providers.base import (
    FetchError,
    NacFetch,
    NacRuleUsageContext,
    ObjectRelationshipContext,
    OrgNetworksContext,
    OrgScope,
    OrgSiteGroupContext,
    OrgTemplateContext,
    OrgWlanContext,
    OrgWlanTemplateContext,
    PskUsageContext,
    RawSiteState,
    SiteScope,
    StateProvider,
    WlanUsageContext,
)
from digital_twin.scope.allowlist import NAC_OBJECT_TYPES, ORG_OBJECT_TYPES
from digital_twin.verdict.decision import Decision

EXIT_CODES = {Decision.SAFE: 0, Decision.REVIEW: 10, Decision.UNSAFE: 20, Decision.UNKNOWN: 30}


class _RecordingProvider:
    """Records the run's OWN fetch so --replay-store saves the exact state the
    verdict judged — never a second fetch that could differ on a live source."""

    def __init__(self, inner: StateProvider) -> None:
        self._inner = inner
        self.recorded: RawSiteState | None = None

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        result = self._inner.fetch_site(scope, include_derived=include_derived)
        if isinstance(result, RawSiteState):
            self.recorded = result
        return result

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        return self._inner.fetch_sites(scope, site_ids, include_derived=include_derived)

    def resolve_org_template(
        self, scope: OrgScope, template_id: str, object_type: str
    ) -> OrgTemplateContext | FetchError:
        return self._inner.resolve_org_template(scope, template_id, object_type)

    def resolve_org_wlan(self, scope: OrgScope, wlan_id: str) -> OrgWlanContext | FetchError:
        return self._inner.resolve_org_wlan(scope, wlan_id)

    def resolve_org_wlan_template(
        self, scope: OrgScope, template_id: str
    ) -> OrgWlanTemplateContext | FetchError:
        return self._inner.resolve_org_wlan_template(scope, template_id)

    def resolve_org_sitegroup(
        self, scope: OrgScope, sitegroup_id: str
    ) -> OrgSiteGroupContext | FetchError:
        return self._inner.resolve_org_sitegroup(scope, sitegroup_id)

    def resolve_org_networks(
        self, scope: OrgScope
    ) -> OrgNetworksContext | FetchError:
        return self._inner.resolve_org_networks(scope)

    def resolve_psk_usage(
        self, scope: OrgScope | SiteScope, psk_id: str, *, window_days: int = 7
    ) -> PskUsageContext | FetchError:
        return self._inner.resolve_psk_usage(scope, psk_id, window_days=window_days)

    def resolve_object_relationships(
        self, scope: OrgScope, object_type: str, object_id: str
    ) -> ObjectRelationshipContext | FetchError:
        return self._inner.resolve_object_relationships(scope, object_type, object_id)

    def resolve_wlan_usage(
        self, scope: OrgScope | SiteScope, wlan_id: str, *, window_days: int = 7
    ) -> WlanUsageContext | FetchError:
        return self._inner.resolve_wlan_usage(scope, wlan_id, window_days=window_days)

    def resolve_nacrule_usage(
        self, scope: OrgScope, nacrule_id: str, *, window_days: int = 7
    ) -> NacRuleUsageContext | FetchError:
        return self._inner.resolve_nacrule_usage(
            scope, nacrule_id, window_days=window_days
        )

    def resolve_org_nac(self, scope: OrgScope) -> NacFetch | FetchError:
        return self._inner.resolve_org_nac(scope)


def _is_org_nac_plan(plan_data: object) -> bool:
    if not isinstance(plan_data, dict):
        return False
    ops = plan_data.get("ops")
    scope = plan_data.get("scope")
    return (
        isinstance(ops, list) and bool(ops)
        and all(isinstance(o, dict) and o.get("object_type") in NAC_OBJECT_TYPES for o in ops)
        and isinstance(scope, dict) and not scope.get("site_id")
    )


def _is_org_plan(plan_data: object) -> bool:
    """Return True if plan_data looks like an ORG fan-out plan.

    Defensive: any malformed plan (missing/wrong-typed fields) returns False so
    it falls through to the SITE path, which will envelope-reject it properly.
    """
    if not isinstance(plan_data, dict):
        return False
    ops = plan_data.get("ops")
    scope = plan_data.get("scope")
    return (
        isinstance(ops, list) and bool(ops)
        and all(isinstance(o, dict) and o.get("object_type") in ORG_OBJECT_TYPES for o in ops)
        and isinstance(scope, dict) and not scope.get("site_id")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digital-twin", description="simulate a ChangePlan")
    parser.add_argument("--plan", required=True, help="ChangePlan JSON file (or '-' for stdin)")
    parser.add_argument("--json", action="store_true", help="print verdict as JSON")
    parser.add_argument("--replay-fixture", help="run against a saved fixture instead of live")
    parser.add_argument(
        "--replay-store",
        help="directory to capture (raw, plan, verdict, trace) — single-site runs only",
    )
    parser.add_argument(
        "--l0-full-object",
        action="store_true",
        help="validate the WHOLE effective object against the OAS (default: only the "
        "roots the change touches — Mist persists/keeps omitted roots unchanged)",
    )
    args = parser.parse_args(argv)

    plan_text = sys.stdin.read() if args.plan == "-" else Path(args.plan).read_text()
    plan_data = json.loads(plan_text)

    run = RunContext()
    name_change_verdict = simulate_name_change(plan_data, run=run)
    if name_change_verdict is not None:
        print(
            json.dumps(verdict_to_dict(name_change_verdict), indent=1)
            if args.json
            else render_human(name_change_verdict)
        )
        return EXIT_CODES[name_change_verdict.decision]

    provider: StateProvider
    if args.replay_fixture:
        provider = FixtureProvider(args.replay_fixture)
    else:
        from digital_twin.providers.mist_api import MistApiProvider

        provider = MistApiProvider()
    recording = _RecordingProvider(provider)

    if needs_composite_evaluation(plan_data):
        document = simulate_composite(
            plan_data,
            provider=recording,
            run=run,
            l0_full_object=args.l0_full_object,
        )
        print(
            json.dumps(document, indent=1)
            if args.json
            else _render_composite_human(document)
        )
        return EXIT_CODES[Decision(str(document["decision"]))]

    if _is_org_nac_plan(plan_data):
        nac_verdict = simulate_org_nac(
            plan_data, provider=recording, run=run, l0_full_object=args.l0_full_object)
        print(json.dumps(org_nac_verdict_to_dict(nac_verdict), indent=1)
              if args.json else render_org_nac_human(nac_verdict))
        return EXIT_CODES[nac_verdict.decision]

    if _is_org_plan(plan_data):
        # ORG (template) path — fan-out across all assigned sites
        org_verdict = simulate_org_template(
            plan_data, provider=recording, run=run, l0_full_object=args.l0_full_object
        )
        # --replay-store is single-site only; recording.recorded stays None for org runs
        # so the guard below naturally skips saving (no site state was captured here)
        print(
            json.dumps(org_verdict_to_dict(org_verdict), indent=1)
            if args.json
            else render_org_human(org_verdict)
        )
        return EXIT_CODES[org_verdict.decision]

    # SITE path (unchanged)
    verdict = simulate(plan_data, provider=recording, run=run, l0_full_object=args.l0_full_object)

    if args.replay_store and recording.recorded is not None:
        assert run.trace is not None
        ReplayStore(args.replay_store).save_run(
            run.run_id,
            raw=recording.recorded,  # the EXACT state the verdict judged
            plan=plan_data,
            verdict_doc=verdict_to_dict(verdict),
            trace=run.trace,
        )

    print(json.dumps(verdict_to_dict(verdict), indent=1) if args.json else render_human(verdict))
    return EXIT_CODES[verdict.decision]


def script() -> None:
    raise SystemExit(main())


def _render_composite_human(document: dict[str, object]) -> str:
    lines = [f"batch decision: {str(document.get('decision', 'unknown')).upper()}"]
    reasons = document.get("decision_reasons", [])
    if isinstance(reasons, list):
        lines.extend(f"  reason: {reason}" for reason in reasons[:20])
    return "\n".join(lines)
