from digital_twin.adapters.mist.ingest.base import IngestContext, Ingester
from digital_twin.adapters.mist.ingest.registry import IngesterRegistry
from digital_twin.ir import IRBuilder, IRCapability
from digital_twin.providers.base import RawSiteState


class FakeIngester:
    """Earns its capability only when its source data is actually present."""

    name = "fake"

    def produces(self) -> frozenset[str]:  # POTENTIAL supply (for capability_check)
        return frozenset({IRCapability.WIRED_L2})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:  # ACTUALLY produced
        if "devices" not in ctx.raw.meta.fetched:
            return frozenset()
        return frozenset({IRCapability.WIRED_L2})


def _raw(fetched: tuple[str, ...] = ()) -> RawSiteState:  # minimal stub
    from datetime import UTC, datetime

    from digital_twin.providers.base import SiteScope, StateMeta

    return RawSiteState(
        scope=SiteScope(org_id="o", site_id="s"),
        site={},
        setting={},
        networktemplate=None,
        devices=(),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=fetched, failures=()),
    )


def test_registry_collects_capabilities_actually_earned():
    reg = IngesterRegistry([FakeIngester()])
    builder = IRBuilder()
    report = reg.run(
        IngestContext(
            raw=_raw(fetched=("devices",)), site_effective={}, device_effective={}, builder=builder
        )
    )
    assert report.ok
    assert IRCapability.WIRED_L2 in report.produced
    assert builder.build().has(IRCapability.WIRED_L2)


def test_capability_not_claimed_when_source_data_missing():
    # the fetch failed -> the ingester earns nothing -> the IR must NOT claim it
    reg = IngesterRegistry([FakeIngester()])
    builder = IRBuilder()
    report = reg.run(
        IngestContext(raw=_raw(fetched=()), site_effective={}, device_effective={}, builder=builder)
    )
    assert report.produced == frozenset()
    assert not builder.build().has(IRCapability.WIRED_L2)


def test_crashing_ingester_becomes_a_named_failure_value_not_an_exception():
    class Crasher:
        name = "crasher"

        def produces(self) -> frozenset[str]:
            return frozenset({IRCapability.STP_STATE})

        def ingest(self, ctx: IngestContext) -> frozenset[str]:
            raise RuntimeError("boom")

    reg = IngesterRegistry([Crasher(), FakeIngester()])
    builder = IRBuilder()
    report = reg.run(
        IngestContext(
            raw=_raw(fetched=("devices",)), site_effective={}, device_effective={}, builder=builder
        )
    )
    assert not report.ok
    assert report.failures[0].ingester == "crasher" and "boom" in report.failures[0].error
    # the crash is isolated: the other ingester still ran, the crasher earned nothing
    assert IRCapability.WIRED_L2 in report.produced
    assert IRCapability.STP_STATE not in report.produced


def test_ingester_satisfies_protocol():
    ingester: Ingester = FakeIngester()
    assert ingester.name == "fake"
