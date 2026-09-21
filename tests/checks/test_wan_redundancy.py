from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.wan_redundancy import GatewayWanRedundancyCheck
from digital_twin.contracts import Severity
from digital_twin.ir import Device, DeviceRole, IRBuilder, Port, PortMode, diff_ir


def _ir(wan0=True, wan1=True):
    builder = IRBuilder().add_device(Device(id="g1", role=DeviceRole.GATEWAY, site="s1"))
    for name, active in (("wan0", wan0), ("wan1", wan1)):
        builder.add_port(Port(
            id=f"g1:{name}", device_id="g1", name=name, mode=PortMode.ACCESS,
            profile="wan", disabled=not active,
        ))
    return builder.build()


def _run(base, proposed):
    diff = diff_ir(base, proposed)
    return GatewayWanRedundancyCheck().run(CheckContext(
        AnalysisContext(base), AnalysisContext(proposed), diff
    ))


def test_removing_one_of_two_wan_paths_is_review():
    result = _run(_ir(), _ir(wan0=False))
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("redundancy_reduced")


def test_removing_last_wan_path_is_unsafe():
    result = _run(_ir(wan1=False), _ir(wan0=False, wan1=False))
    assert result.status is Status.FAIL
    assert result.findings[0].severity is Severity.ERROR
    assert result.findings[0].code.endswith("last_path_removed")


def test_adding_wan_path_requires_health_review():
    result = _run(_ir(wan1=False), _ir())
    assert result.status is Status.WARN
    assert result.findings[0].code.endswith("path_added_unverified")
