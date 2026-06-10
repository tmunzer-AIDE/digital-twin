from datetime import UTC, datetime, timedelta

from digital_twin.providers.base import FetchFailure, StateMeta
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta


def test_view_carries_freshness_and_failures():
    acquired = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    meta = StateMeta(
        acquired_at=acquired,
        host="api.mist.com",
        fetched=("site", "setting"),
        failures=(FetchFailure(object="wired_clients", error="503"),),
    )
    view = build_state_meta(meta, now=acquired + timedelta(seconds=90))
    assert isinstance(view, StateMetaView)
    assert view.age_seconds == 90
    assert view.fetch_failures == (("wired_clients", "503"),)
    assert view.host == "api.mist.com"


def test_verdict_carries_state_meta_and_trace_ref():
    from digital_twin.ir import IRDiff
    from digital_twin.verdict.decision import DecisionInputs
    from digital_twin.verdict.verdict import assemble

    v = assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=()
        ),
        ir_diff=IRDiff((), (), ()),
        state_meta=None,
        trace_ref="run-123",
    )
    assert v.state_meta is None and v.trace_ref == "run-123"
