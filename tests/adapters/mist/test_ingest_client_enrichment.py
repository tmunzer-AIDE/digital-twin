from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.client_enrichment import ClientEnrichmentIngester
from digital_twin.ir.model import IRBuilder
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(*, nac=(), wired=(), wireless=()) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={}, setting={},
        networktemplate=None, devices=(), device_stats=(), port_stats=(),
        wireless_clients=tuple(wireless), wired_clients=tuple(wired), derived_setting=None,
        nac_clients=tuple(nac),
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
    )


def _ctx(raw: RawSiteState) -> IngestContext:
    return IngestContext(raw=raw, site_effective={}, device_effective={}, builder=IRBuilder())


def test_ingester_populates_enrichment():
    ctx = _ctx(_raw(nac=({"mac": "aabbcc000001", "last_family": "Surveillance Camera",
                          "last_mfg": "Verkada Inc"},)))
    assert ClientEnrichmentIngester().ingest(ctx) == frozenset()  # earns NO capability
    ce = ctx.builder.build().client_enrichment["aabbcc000001"]
    assert ce.family == "Surveillance Camera" and ce.mfg == "Verkada Inc"


def test_ingester_never_raises_on_garbage():
    ctx = _ctx(_raw(nac=("not-a-dict", 42, {"mac": None})))  # type: ignore[arg-type]
    assert ClientEnrichmentIngester().ingest(ctx) == frozenset()
    assert dict(ctx.builder.build().client_enrichment) == {}


def test_broken_nac_does_not_taint_report_ok_through_the_adapter():
    # the verdict-path guarantee: a malformed nac_clients row must NOT add to
    # IngestReport.failures (which would flip report.ok -> ir=None -> UNKNOWN)
    raw = _raw(nac=("garbage", {"oops": 1}))  # type: ignore[arg-type]
    outcome = MistAdapter().ingest(raw)
    assert outcome.report.ok is True
    assert outcome.ir is not None
    assert all(f.ingester != "client_enrichment" for f in outcome.report.failures)
