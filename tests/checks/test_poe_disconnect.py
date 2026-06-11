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


def _switch_port(pid, *, poe, poe_draw=False):
    did, name = pid.split(":")
    return Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK, poe=poe, poe_draw=poe_draw)


def _ap_uplink_ir(*, poe, poe_draw=False, with_client=True):
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


def test_already_disabled_is_not_attributed_to_the_delta():
    # base already poe=False -> cutting nothing -> no finding
    assert _run(_ap_uplink_ir(poe=False), _ap_uplink_ir(poe=False)).status is Status.PASS
