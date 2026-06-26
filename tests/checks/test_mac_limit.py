"""wired.port.mac_limit_exceeded: a lowered/new MAC cap that drops currently-
connected wired clients (or can't be confirmed safe). requires WIRED_L2 only;
client data = CLIENTS_ACTIVE on BOTH sides; capped at REVIEW."""
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.mac_limit import MacLimitExceededCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from tests.factories import sw, wired_client


def _ir(limit, *, n_clients=0, clients_active=True):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1",
                    mode=PortMode.ACCESS, native_vlan=10, mac_limit=limit))
    for i in range(n_clients):
        b.add_client(wired_client(f"cc:{i:02}", "S:ge-0/0/1", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    if clients_active:
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def _run(base, prop):
    return MacLimitExceededCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def test_requires_wired_l2_only():
    assert MacLimitExceededCheck().requires() == frozenset({IRCapability.WIRED_L2})


def test_lowered_below_observed_is_review_exceeded():
    base, prop = _ir(None, n_clients=3), _ir(2, n_clients=3)
    r = _run(base, prop)
    f = r.findings[0]
    assert f.code == "wired.port.mac_limit_exceeded.exceeded"
    assert f.severity is Severity.WARNING and r.status is Status.WARN


def test_within_limit_with_clients_is_silent():
    assert _run(_ir(None, n_clients=2), _ir(10, n_clients=2)).findings == ()


def test_restrictive_without_client_caps_is_unverified():
    base = _ir(None, n_clients=0, clients_active=False)
    prop = _ir(2, n_clients=0, clients_active=False)
    assert _run(base, prop).findings[0].code == "wired.port.mac_limit_exceeded.unverified"


def test_baseline_lacks_capability_is_unverified():
    base = _ir(None, n_clients=0, clients_active=False)      # baseline blind
    prop = _ir(2, n_clients=0, clients_active=True)          # proposed has it
    assert _run(base, prop).findings[0].code == "wired.port.mac_limit_exceeded.unverified"


def test_unresolved_limit_is_review():
    assert _run(_ir(None), _ir("unresolved:{{v}}")).findings[0].code == \
        "wired.port.mac_limit_exceeded.unresolved"


def test_raised_or_unlimited_is_silent():
    assert _run(_ir(2, n_clients=1), _ir(10, n_clients=1)).findings == ()
    assert _run(_ir(2, n_clients=1), _ir(None, n_clients=1)).findings == ()
