from digital_twin.adapters.mist.ingest.bgp_neighbors import (
    BgpNeighborIngester,
    build_bgp_neighbors,
)
from digital_twin.ir import IRCapability


def test_parses_good_rows_and_counts_bad():
    rows = (
        {"mac": "aa:bb:cc:dd:ee:01", "peer_ip": "10.0.0.2", "neighbor_as": 65001,
         "state": "Established", "vrf_name": "default", "up": True},
        {"mac": "aa:bb:cc:dd:ee:01", "neighbor": "10.0.0.3", "state": "Idle"},  # fallback key
        {"mac": "aa:bb:cc:dd:ee:01"},  # no peer ip -> unparsed
        {"peer_ip": "10.0.0.9"},  # no mac -> unparsed
    )
    neighbors, unparsed = build_bgp_neighbors(rows)
    assert unparsed == 2
    by_ip = {n.peer_ip: n for n in neighbors}
    assert by_ip["10.0.0.2"].neighbor_as == 65001 and by_ip["10.0.0.2"].up is True
    assert by_ip["10.0.0.3"].state == "Idle"


def test_one_exploding_row_never_drops_the_batch():
    class Boom(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")
    neighbors, unparsed = build_bgp_neighbors(({"mac": "aa", "peer_ip": "10.0.0.2"}, Boom()))
    assert len(neighbors) == 1 and unparsed == 1


class _Builder:
    def __init__(self):
        self.calls = []
    def set_bgp_neighbors(self, neighbors, unparsed):
        self.calls.append((list(neighbors), unparsed))


class _Raw:
    def __init__(self, fetched, rows):
        self.meta = type("M", (), {"fetched": fetched})()
        self.bgp_neighbors = rows


class _Ctx:
    def __init__(self, fetched, rows):
        self.raw = _Raw(fetched, rows)
        self.builder = _Builder()


def test_earns_capability_only_when_fetched():
    ing = BgpNeighborIngester()
    # not fetched -> no claim, no publish
    ctx = _Ctx((), ())
    assert ing.ingest(ctx) == frozenset()
    assert ctx.builder.calls == []
    # fetched (even empty) -> earns BGP_TELEMETRY
    ctx2 = _Ctx(("bgp_neighbors",), ())
    assert ing.ingest(ctx2) == frozenset({IRCapability.BGP_TELEMETRY})
    assert ctx2.builder.calls == [([], 0)]
