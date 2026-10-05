from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.radius_missing import RadiusMissingCheck
from digital_twin.contracts import Severity
from digital_twin.ir import Device, DeviceRole, IRBuilder, Port, PortMode, diff_ir
from digital_twin.ir.entities import PortAuth


def _ir(
    *, backends: int, unresolved: bool = False, auth: bool = True, disabled=False, spare=False
):
    builder = IRBuilder().add_device(Device(
        id="d1", role=DeviceRole.SWITCH, site="s1",
        authenticator_count=backends,
        authenticator_unresolved=unresolved,
    ))
    builder.add_port(Port(
        id="d1:ge-0/0/1", device_id="d1", name="ge-0/0/1",
        mode=PortMode.ACCESS, disabled=disabled,
        auth=PortAuth(port_auth="dot1x") if auth else None,
    ))
    if spare:
        builder.add_port(Port(
            id="d1:ge-0/0/2", device_id="d1", name="ge-0/0/2", mode=PortMode.ACCESS,
        ))
    return builder.build()


def _run(base, proposed):
    diff = diff_ir(base, proposed)
    return RadiusMissingCheck().run(CheckContext(
        AnalysisContext(base), AnalysisContext(proposed), diff
    ))


def test_removing_last_authenticator_with_assigned_dot1x_is_review():
    result = _run(_ir(backends=1), _ir(backends=0))
    finding = next(f for f in result.findings if f.severity is Severity.WARNING)
    assert finding.code == "wired.auth.radius_missing.introduced"
    assert finding.evidence == {"port": "d1:ge-0/0/1", "configured_backends": 0}
    assert result.status is Status.WARN


def test_unassigned_auth_profile_does_not_fire():
    result = _run(_ir(backends=1, auth=False), _ir(backends=0, auth=False))
    assert not result.findings


def test_unresolved_backend_state_is_partial_not_missing_claim():
    result = _run(_ir(backends=1), _ir(backends=0, unresolved=True))
    assert not result.findings
    assert result.coverage.state is CoverageState.PARTIAL


def test_disabling_the_only_auth_port_clears_the_missing_backend():
    result = _run(_ir(backends=0), _ir(backends=0, disabled=True))
    assert result.status is Status.PASS
    assert all(f.severity is Severity.INFO for f in result.findings)


def test_preexisting_missing_backend_is_info_only():
    result = _run(_ir(backends=0), _ir(backends=0, spare=True))
    assert result.status is Status.PASS
    assert result.findings[0].severity is Severity.INFO
