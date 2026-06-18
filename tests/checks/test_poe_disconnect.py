"""wired.poe.disconnect: cutting PoE to a port that powers a device is a
disconnect. UNSAFE (ERROR/HIGH) when the powered device is observed drawing
power or is an LLDP-confirmed AP; harmless (PASS) on an unpowered port;
pre-existing (already disabled) is not attributed to the delta."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.poe_disconnect import PoeDisconnectCheck
from digital_twin.contracts import Severity
from digital_twin.ir import (
    ConfidenceLevel,
    IRBuilder,
    IRCapability,
    Port,
    PortMode,
    diff_ir,
)
from tests.factories import ap, sw, wireless_client


def _switch_port(pid, *, poe, poe_draw=None):
    did, name = pid.split(":")
    return Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK, poe=poe, poe_draw=poe_draw)


def _ap_uplink_ir(*, poe, poe_draw=None, with_client=True):
    from tests.factories import link

    b = IRBuilder().add_device(sw("S")).add_device(ap("A"))
    b.add_port(_switch_port("S:ge-0/0/1", poe=poe, poe_draw=poe_draw))
    b.add_port(Port(id="A:eth0", device_id="A", name="eth0", mode=PortMode.TRUNK))
    b.add_link(link("S:ge-0/0/1", "A:eth0"))  # two-sided -> HIGH
    if with_client:
        b.add_client(wireless_client("ww:01", "A", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return PoeDisconnectCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def test_cutting_poe_to_an_ap_is_unsafe():
    result = _run(_ap_uplink_ir(poe=True), _ap_uplink_ir(poe=False))
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert "A" in f.affected_entities  # the AP
    assert f.evidence["affected_wireless_clients"] == 1


def test_observed_power_draw_cut_is_unsafe_even_without_an_ap():
    def ir(poe):
        b = IRBuilder().add_device(sw("S"))
        b.add_port(_switch_port("S:ge-0/0/9", poe=poe, poe_draw=True))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(ir(True), ir(False))
    assert result.status is Status.FAIL
    assert result.findings[0].confidence.level is ConfidenceLevel.HIGH


def test_cutting_poe_on_an_unpowered_port_is_harmless():
    def ir(poe):
        b = IRBuilder().add_device(sw("S"))
        b.add_port(_switch_port("S:ge-0/0/2", poe=poe, poe_draw=False))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    assert _run(ir(True), ir(False)).status is Status.PASS


def test_ap_uplink_observed_not_drawing_is_silent():
    # review regression (ca1b474): observed poe_on=False is DIRECT evidence the
    # port powers nothing — the AP must be on aux power. The AP-link inference
    # must not override the observation back into power_loss (let alone
    # ERROR/HIGH on a two-sided link).
    result = _run(
        _ap_uplink_ir(poe=True, poe_draw=False), _ap_uplink_ir(poe=False, poe_draw=False)
    )
    assert result.status is Status.PASS
    assert result.findings == ()


def test_already_disabled_is_not_attributed_to_the_delta():
    # base already poe=False -> cutting nothing -> no finding
    assert _run(_ap_uplink_ir(poe=False), _ap_uplink_ir(poe=False)).status is Status.PASS


def test_unknown_powered_state_is_a_warning_not_silence():
    # poe_draw=None (no/blind telemetry): cutting PoE on a port whose powered
    # state is unobservable can never silently PASS — a camera/phone could be
    # on it. WARNING (-> REVIEW) at MEDIUM, distinct code.
    def ir(poe):
        b = IRBuilder().add_device(sw("S"))
        b.add_port(_switch_port("S:ge-0/0/3", poe=poe, poe_draw=None))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    result = _run(ir(True), ir(False))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.code == "wired.poe.disconnect.unverified"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_blind_baseline_intent_with_an_ap_downgrades_to_warning():
    # baseline poe=None (usage blind): the AP is LLDP-confirmed but whether the
    # baseline even delivered power is unknown -> WARNING/MEDIUM, not ERROR/HIGH
    result = _run(_ap_uplink_ir(poe=None), _ap_uplink_ir(poe=False))
    assert result.status is Status.WARN
    f = result.findings[0]
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_observed_draw_trumps_blind_baseline_intent():
    # direct evidence: the port IS delivering power -> UNSAFE regardless of
    # whether the baseline config intent resolved
    result = _run(
        _ap_uplink_ir(poe=None, poe_draw=True), _ap_uplink_ir(poe=False, poe_draw=True)
    )
    assert result.status is Status.FAIL
    f = result.findings[0]
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH


def test_blind_baseline_with_nothing_observed_is_silent():
    # poe=None AND poe_draw=None AND no AP: nothing says power was ever
    # delivered — the usage blindness itself is gated elsewhere (dynamic gate)
    def ir(poe):
        b = IRBuilder().add_device(sw("S"))
        b.add_port(_switch_port("S:ge-0/0/4", poe=poe, poe_draw=None))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    assert _run(ir(None), ir(False)).status is Status.PASS


# ── caused_by attribution ──────────────────────────────────────────────────────

def test_poe_cut_caused_by_is_non_empty():
    # delta disables PoE on S:ge-0/0/1 -> it appears in caused_by
    result = _run(_ap_uplink_ir(poe=True), _ap_uplink_ir(poe=False))
    f = result.findings[0]
    assert f.severity is not Severity.INFO
    assert len(f.caused_by) > 0
    assert f.caused_by[0].ref.kind == "port"
    assert f.caused_by[0].ref.id == "S:ge-0/0/1"


def test_already_disabled_poe_emits_no_finding():
    # poe_disconnect has no INFO path — an already-disabled port emits no finding
    # (verified here to document the absence of a preexisting INFO row)
    result = _run(_ap_uplink_ir(poe=False), _ap_uplink_ir(poe=False))
    assert result.findings == ()
