"""A compile/ingest CRASH on one site must be that site's UNKNOWN — never a hard
crash, never a false-SAFE. Live-caught: an org multi-op delete fanned out to a site
whose gateway carried an unresolvable {{var}}; compile_gateway_device raised
UnresolvedVars, which (before the fix) propagated and crashed the whole org run."""

from __future__ import annotations

from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.checks.registry import CheckRegistry
from digital_twin.engine.pipeline import _simulate_site_state
from digital_twin.engine.run_context import RunContext
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.verdict.decision import Decision


def _raw() -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"}, setting={},
        networktemplate=None, gatewaytemplate=None, sitetemplate=None, devices=(),
        device_stats=(), port_stats=(), wireless_clients=(), wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
    )


class _CrashAdapter:
    """An adapter whose ingest raises (e.g. an unresolvable {{var}} at compile)."""

    def ingest(self, raw: RawSiteState) -> object:
        raise RuntimeError("unresolved vars: guest_end at dhcpd_config.guest.ip_end")


def test_ingest_crash_is_unknown_not_a_hard_crash() -> None:
    verdict = _simulate_site_state(
        _raw(), _raw(),
        adapter=_CrashAdapter(),  # type: ignore[arg-type]  # duck-typed crash adapter
        registry=CheckRegistry([]), run=RunContext(), state_meta=None,
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any("ingest crashed" in r for r in verdict.decision_reasons), verdict.decision_reasons


class _SoftFailOutcome:
    """Duck-typed IngestOutcome for a NON-crash ingest failure (report not ok)."""

    ir = None


class _SoftFailAdapter:
    def ingest(self, raw: RawSiteState) -> object:
        return _SoftFailOutcome()


def test_baseline_soft_ingest_failure_is_unknown_baseline_unavailable() -> None:
    # `baseline.ir is None` (ingest returned, but produced no IR) is the SOFT
    # failure twin of the crash above: UNKNOWN via baseline_unavailable, never
    # a false-SAFE run on a phantom baseline.
    verdict = _simulate_site_state(
        _raw(), _raw(),
        adapter=_SoftFailAdapter(),  # type: ignore[arg-type]  # duck-typed soft-fail adapter
        registry=CheckRegistry([]), run=RunContext(), state_meta=None,
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any(
        "no usable baseline state" in r for r in verdict.decision_reasons
    ), verdict.decision_reasons


class _ProposedCrashAdapter:
    """Baseline ingests fine (real adapter); the PROPOSED ingest crashes."""

    def __init__(self) -> None:
        self._real = MistAdapter()
        self._calls = 0

    def ingest(self, raw: RawSiteState) -> object:
        self._calls += 1
        if self._calls == 1:
            return self._real.ingest(raw)
        raise RuntimeError("unresolved vars: guest_end at dhcpd_config.guest.ip_end")


def test_proposed_ingest_crash_is_unknown_not_a_hard_crash() -> None:
    verdict = _simulate_site_state(
        _raw(), _raw(),
        adapter=_ProposedCrashAdapter(),  # type: ignore[arg-type]  # duck-typed crash adapter
        registry=CheckRegistry([]), run=RunContext(), state_meta=None,
    )
    assert verdict.decision is Decision.UNKNOWN
    assert any(
        "proposed ingest crashed" in r for r in verdict.decision_reasons
    ), verdict.decision_reasons
