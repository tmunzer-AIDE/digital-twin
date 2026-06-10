"""CLI driver: ChangePlan JSON in -> verdict out, decision-coded exit status.

Exit codes (spec): SAFE=0, REVIEW=10, UNSAFE=20, UNKNOWN=30 — only SAFE is
success, because everything else means "do not apply automatically".
Providers: live Mist (env MIST_HOST/MIST_APITOKEN) or --replay-fixture (offline).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from digital_twin.drivers.render import render_human, verdict_to_dict
from digital_twin.engine.pipeline import simulate
from digital_twin.engine.run_context import RunContext
from digital_twin.observability.replay.store import FixtureProvider, ReplayStore
from digital_twin.providers.base import RawSiteState, SiteScope, StateProvider
from digital_twin.verdict.decision import Decision

EXIT_CODES = {Decision.SAFE: 0, Decision.REVIEW: 10, Decision.UNSAFE: 20, Decision.UNKNOWN: 30}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digital-twin", description="simulate a ChangePlan")
    parser.add_argument("--plan", required=True, help="ChangePlan JSON file (or '-' for stdin)")
    parser.add_argument("--json", action="store_true", help="print verdict as JSON")
    parser.add_argument("--replay-fixture", help="run against a saved fixture instead of live")
    parser.add_argument("--replay-store", help="directory to capture (raw, plan, verdict, trace)")
    args = parser.parse_args(argv)

    plan_text = sys.stdin.read() if args.plan == "-" else Path(args.plan).read_text()
    plan_data = json.loads(plan_text)

    provider: StateProvider
    if args.replay_fixture:
        provider = FixtureProvider(args.replay_fixture)
    else:
        from digital_twin.providers.mist_api import MistApiProvider

        provider = MistApiProvider()

    run = RunContext()
    verdict = simulate(plan_data, provider=provider, run=run)

    if args.replay_store:
        scope = plan_data.get("scope", {}) if isinstance(plan_data, dict) else {}
        raw = provider.fetch_site(  # the same state the run used (fixture or on-demand)
            SiteScope(str(scope.get("org_id", "")), str(scope.get("site_id", "")))
        )
        if isinstance(raw, RawSiteState):
            assert run.trace is not None
            ReplayStore(args.replay_store).save_run(
                run.run_id,
                raw=raw,
                plan=plan_data,
                verdict_doc=verdict_to_dict(verdict),
                trace=run.trace,
            )

    print(json.dumps(verdict_to_dict(verdict), indent=1) if args.json else render_human(verdict))
    return EXIT_CODES[verdict.decision]


def script() -> None:
    raise SystemExit(main())
