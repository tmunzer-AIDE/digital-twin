"""Tier-2 live gate: validate the STP tree prediction engine against Mist's
own observed telemetry, per site (Spec-4 Task 7).

Per site: fetch -> MistAdapter.ingest -> predict_stp_tree(ir) ->
compare_to_observed(prediction, ir). Two independent failure rules (either
fails the whole run):

1. **Any `mismatched_high` anywhere** — a HIGH-confidence prediction the
   network telemetry contradicts is an engine bug, full stop.
2. **Zero participating (non-`unvalidatable`) predicted ports org-wide** — a
   vacuous pass produces no positive evidence and must not look green.

`mismatched_medium` / `mismatched_low` are report-only: MEDIUM predictions
rest on a declared assumption (assumed-default priority, degraded link,
speed disagreement) and LOW are declared tie-break guesses. Tightening either
into the gate is a later decision, taken on real agreement data, not now.

Usage:  uv run python tools/stp_gate.py
        uv run python tools/stp_gate.py --replay-fixture path/to/fixture.json
Env:    MIST_HOST, MIST_APITOKEN, DT_GATE_ORG_ID, DT_GATE_SITE_IDS (comma-separated)
        (not required in --replay-fixture mode: fixtures are REDACTED on write,
        so env ids would never match a live org/site — the fixture's OWN
        recorded scope is read instead, from the fixture file directly.)

GATE RULE: no verdict-facing consumer of stp_tree() may treat a prediction as
SAFE without running it through compare_to_observed first (see
analysis/stp_agreement.py's module docstring for the full invariant). This
tool is the live evidence that the pure comparator's buckets hold up against
reality.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.analysis.stp_agreement import (
    PortAgreement,
    StpAgreementReport,
    compare_to_observed,
)
from digital_twin.analysis.stp_tree import predict_stp_tree
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.providers.base import FetchError, OrgScope, RawSiteState, StateProvider
from digital_twin.providers.mist_api import MistApiProvider

_MISMATCH_ROW_BUCKETS = frozenset(
    {"mismatched_high", "mismatched_medium", "mismatched_low", "bpdu_inconsistent"}
)


def _print_mismatch_row(row: PortAgreement) -> None:
    pred = row.predicted
    print(
        f"    [{row.bucket}] port={row.port_id} "
        f"predicted=({pred.role}/{pred.state}, {pred.confidence.name}, {pred.deciding_factor}) "
        f"observed=({row.observed_role}/{row.observed_state})"
    )


def _print_site_report(site_id: str, report: StpAgreementReport) -> None:
    print(
        f"[{site_id}] matched={report.matched} "
        f"mismatched_high={report.mismatched_high} "
        f"mismatched_medium={report.mismatched_medium} "
        f"mismatched_low={report.mismatched_low} "
        f"unvalidatable={report.unvalidatable} "
        f"bpdu_inconsistent={report.bpdu_inconsistent}"
    )
    for row in report.ports:
        if row.bucket in _MISMATCH_ROW_BUCKETS:
            _print_mismatch_row(row)


def _run_site(site_id: str, raw: RawSiteState | FetchError | None) -> StpAgreementReport | None:
    if not isinstance(raw, RawSiteState):
        errs = [f.error for f in raw.failures] if isinstance(raw, FetchError) else ["not returned"]
        print(f"[FAIL] {site_id}: fetch failed: {errs}")
        return None
    try:
        outcome = MistAdapter().ingest(raw)
        if outcome.ir is None:
            print(f"[FAIL] {site_id}: ingest failed: {outcome.report.failures}")
            return None
        prediction = predict_stp_tree(outcome.ir)
        return compare_to_observed(prediction, outcome.ir)
    except Exception as exc:
        print(f"[FAIL] {site_id}: ingest/predict raised: {exc!r}")
        return None


def _fixture_scope(path: str) -> tuple[str, list[str]]:
    """Read (org_id, site_ids) FROM THE FIXTURE DOC's own recorded scope —
    fixtures are redacted on write, so DT_GATE_* env ids can never match a
    replayed fixture; the fixture is the sole source of truth in this mode."""
    doc = json.loads(Path(path).read_text())
    if "sites" in doc:
        site_ids = list(doc["sites"])
        org_id = next(iter(doc["sites"].values()))["scope"]["org_id"]
    else:
        org_id = doc["scope"]["org_id"]
        site_ids = [doc["scope"]["site_id"]]
    return org_id, site_ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="live STP prediction agreement gate")
    parser.add_argument(
        "--replay-fixture", help="run against a saved fixture instead of the live provider"
    )
    args = parser.parse_args(argv)

    provider: StateProvider
    if args.replay_fixture:
        org_id, site_ids = _fixture_scope(args.replay_fixture)
        provider = FixtureProvider(args.replay_fixture)
    else:
        org_id = os.environ["DT_GATE_ORG_ID"]
        site_ids = [s for s in os.environ["DT_GATE_SITE_IDS"].split(",") if s]
        provider = MistApiProvider()

    states = provider.fetch_sites(OrgScope(org_id), site_ids)

    failures = 0
    total_participating = 0
    for site_id in site_ids:
        report = _run_site(site_id, states.get(site_id))
        if report is None:
            failures += 1
            continue
        _print_site_report(site_id, report)
        if report.mismatched_high:
            failures += 1
        total_participating += (
            report.matched
            + report.mismatched_high
            + report.mismatched_medium
            + report.mismatched_low
            + report.bpdu_inconsistent
        )

    print(
        f"\ntotal participating (non-unvalidatable) ports across all sites: {total_participating}"
    )
    if not total_participating:
        failures += 1
        print("  [FAIL] zero participating ports -> the gate produced NO positive evidence")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
