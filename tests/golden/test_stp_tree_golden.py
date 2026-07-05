"""Golden: TM-LAB STP tree prediction vs Mist's own observed telemetry
(Spec-4 Task 8). Skipped until the finishing phase captures the redacted
fixture at tests/golden/fixtures/tmlab_stp.json (see tools/capture_replay.py
+ tools/stp_gate.py --replay-fixture for the live-verify procedure).

Fixture ids are REDACTED on capture — this test must never hardcode a
port/device id from the lab. The self-loop pair is instead found
STRUCTURALLY: scan the prediction's components for a port predicted
"backup" (only pseudo-edge same-bridge self-loops ever produce that role;
see analysis/stp_tree.py's pseudo-edge handling) and pull its reciprocal
"designated" partner out of the same component.

THE INVARIANT (analysis/stp_tree.py): prediction alone never earns SAFE;
every future verdict-facing consumer must call compare_to_observed and cap
confidence on component-level disagreement. This golden only exercises the
report-only pairing — it asserts zero mismatched_high, not a verdict.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.analysis.stp_agreement import compare_to_observed
from digital_twin.analysis.stp_tree import predict_stp_tree
from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.providers.base import FetchError, RawSiteState

_FIXTURE = Path(__file__).parent / "fixtures" / "tmlab_stp.json"

pytestmark = pytest.mark.skipif(not _FIXTURE.exists(), reason="captured at live-verify")


def _load_ir():
    provider = FixtureProvider(_FIXTURE)
    # strict=True default: fetch_site rejects a mismatched scope, so read the
    # fixture's OWN recorded scope back out rather than guessing an org/site id.
    fetched = provider.fetch_site(provider.fixture_scope)
    assert isinstance(fetched, RawSiteState), (
        f"fixture fetch failed: {fetched.failures if isinstance(fetched, FetchError) else fetched}"
    )
    outcome = MistAdapter().ingest(fetched)
    assert outcome.ir is not None, f"ingest failed: {outcome.report.failures}"
    return outcome.ir


def test_tmlab_stp_prediction_agrees_with_observed():
    ir = _load_ir()
    prediction = predict_stp_tree(ir)
    report = compare_to_observed(prediction, ir)

    # (a) zero mismatched_high anywhere: a HIGH-confidence prediction the
    # network telemetry contradicts is an engine bug, full stop.
    assert report.mismatched_high == 0

    # (b) non-vacuous: the fixture must actually exercise agreement, not
    # produce an all-unvalidatable pass that looks green for free.
    assert report.matched > 0

    # (c) at least one reciprocal self-loop pair predicted designated/backup
    # at LOW confidence with deciding_factor "port_id_tie". Found structurally
    # via the "backup" role (only same-bridge pseudo-edges ever assign it).
    backup_ports = [
        (comp, pred)
        for comp in prediction.components
        for pred in comp.ports.values()
        if pred.role == "backup"
    ]
    assert backup_ports, "expected at least one predicted backup-role port (self-loop pseudo-edge)"

    comp, backup_pred = backup_ports[0]
    assert backup_pred.confidence is ConfidenceLevel.LOW
    assert backup_pred.deciding_factor == "port_id_tie"

    designated_partners = [
        pred
        for pred in comp.ports.values()
        if pred.role == "designated" and pred.deciding_factor == "port_id_tie"
    ]
    assert designated_partners, (
        "expected the backup port's reciprocal designated partner in the same component"
    )
    assert any(p.confidence is ConfidenceLevel.LOW for p in designated_partners)
