from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.bgp_prefix_delta import BgpPrefixDeltaCheck
from digital_twin.contracts import Severity
from digital_twin.ir import (
    BgpNeighbor,
    BgpPeer,
    Device,
    DeviceRole,
    IRBuilder,
    IRCapability,
    diff_ir,
)


def _peer(prefixes=(), *, opaque=None, neighbor="192.0.2.1"):
    return BgpPeer(
        device_id="d1", role=DeviceRole.SWITCH, session_name="edge",
        neighbor_ip=neighbor, advertised_prefixes=tuple(prefixes),
        export_unresolved=opaque,
    )


def _ir(peers, *, established=False):
    builder = IRBuilder().add_device(Device(id="d1", role=DeviceRole.SWITCH, site="s1"))
    for peer in peers:
        builder.add_bgp_peer(peer)
    if established:
        builder.with_capability(IRCapability.BGP_TELEMETRY)
        builder.set_bgp_neighbors((BgpNeighbor(
            device_id="d1", peer_ip="192.0.2.1", state="Established"
        ),))
    return builder.build()


def _run(base, proposed):
    diff = diff_ir(base, proposed)
    return BgpPrefixDeltaCheck().run(CheckContext(
        AnalysisContext(base), AnalysisContext(proposed), diff
    ))


def test_structural_sole_withdrawal_is_review_without_telemetry():
    result = _run(_ir([_peer(("10.0.0.0/24",))]), _ir([_peer()]))
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("withdrawn_sole")


def test_established_sole_withdrawal_is_unsafe():
    result = _run(
        _ir([_peer(("10.0.0.0/24",))], established=True),
        _ir([_peer()], established=True),
    )
    assert result.status is Status.FAIL
    assert result.findings[0].severity is Severity.ERROR


def test_overlapping_advertisement_is_review():
    base = _ir([
        _peer(("10.0.0.0/24",), neighbor="192.0.2.1"),
        _peer((), neighbor="192.0.2.2"),
    ])
    proposed = _ir([
        _peer(("10.0.0.0/24",), neighbor="192.0.2.1"),
        _peer(("10.0.0.0/25",), neighbor="192.0.2.2"),
    ])
    result = _run(base, proposed)
    finding = next(f for f in result.findings if f.code.endswith("overlap_added"))
    assert finding.evidence["overlapping_prefixes"] == ["10.0.0.0/24"]


def test_opaque_export_policy_change_is_partial_review():
    result = _run(_ir([_peer(opaque="old")]), _ir([_peer(opaque="new")]))
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("policy_changed")
