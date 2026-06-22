from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.ir import IRCapability
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(neighbors, *, fetched=("site", "setting", "devices", "ospf_neighbors")):
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"},
        setting={"networks": {}}, networktemplate=None,
        devices=({"mac": "001122334455", "type": "switch", "name": "sw"},),
        device_stats=(), port_stats=(), wireless_clients=(), wired_clients=(),
        derived_setting=None, ospf_neighbors=tuple(neighbors),
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=fetched, failures=()),
    )


def test_neighbors_parsed_and_capability_earned():
    out = MistAdapter().ingest(_raw([
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "area": "0", "state": "Full"}]))
    assert out.report.ok and out.ir is not None
    assert IRCapability.OSPF_TELEMETRY in out.ir.capabilities
    assert len(out.ir.ospf_neighbors) == 1 and out.ir.ospf_telemetry_unparsed_count == 0


def test_partial_unparsed_rows_counted_not_fatal():
    out = MistAdapter().ingest(_raw([
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "state": "Full"},  # ok
        {"garbage": True},                                                # no peer_ip
    ]))
    assert out.report.ok                              # self-isolating: never fatal
    assert IRCapability.OSPF_TELEMETRY in out.ir.capabilities
    assert len(out.ir.ospf_neighbors) == 1 and out.ir.ospf_telemetry_unparsed_count == 1


def test_not_fetched_earns_nothing():
    out = MistAdapter().ingest(_raw([], fetched=("site", "setting", "devices")))
    assert IRCapability.OSPF_TELEMETRY not in out.ir.capabilities
    assert out.ir.ospf_neighbors == ()


def test_empty_but_fetched_earns_capability_zero_neighbors():
    # genuinely-zero (shape known) is distinct from not-fetched (blind): capability earned.
    out = MistAdapter().ingest(_raw([]))
    assert IRCapability.OSPF_TELEMETRY in out.ir.capabilities
    assert out.ir.ospf_neighbors == () and out.ir.ospf_telemetry_unparsed_count == 0


def test_field_fallbacks_status_and_neighbor_ip():
    out = MistAdapter().ingest(_raw([
        {"mac": "001122334455", "neighbor_ip": "10.0.0.9", "status": "Full"}]))  # fallback keys
    n = out.ir.ospf_neighbors[0]
    assert n.peer_ip == "10.0.0.9" and n.state == "Full"


def test_per_row_exception_is_isolated_and_counted():
    # a value that raises inside _clean(str(...)) must be caught per-row -> counted unparsed,
    # never fatal (exercises the per-row try/except, not just the missing-field path).
    class _Boom:
        def __str__(self) -> str:
            raise ValueError("boom")

    out = MistAdapter().ingest(_raw([
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "state": "Full"},   # ok
        {"mac": _Boom(), "peer_ip": "10.0.0.6"},                           # raises in _clean
    ]))
    assert out.report.ok                              # never fatal
    assert len(out.ir.ospf_neighbors) == 1 and out.ir.ospf_telemetry_unparsed_count == 1
