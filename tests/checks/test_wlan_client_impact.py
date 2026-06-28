from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.wlan_client_impact import WlanClientImpactCheck
from digital_twin.contracts import FindingCategory, Severity
from digital_twin.ir import ConfidenceLevel, IRCapability, Wlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder
from tests.factories import ap, wireless_client


def _wlan(
    wid: str = "w1",
    *,
    ssid: str = "corp",
    enabled: bool = True,
    apply_to: str | None = "site",
    ap_ids: tuple[str, ...] = (),
    wxtag_ids: tuple[str, ...] = (),
) -> Wlan:
    return Wlan(
        id=wid,
        ssid=ssid,
        enabled=enabled,
        apply_to=apply_to,
        ap_ids=ap_ids,
        wxtag_ids=wxtag_ids,
    )


def _ir(*wlans: Wlan, clients=(), clients_known: bool = True):
    b = IRBuilder().with_capability(IRCapability.WLAN_CONFIG)
    if clients_known:
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
    aps = {c.attach_id for c in clients}
    for ap_id in sorted(aps):
        b.add_device(ap(ap_id))
    for wlan in wlans:
        b.add_wlan(wlan)
    for client in clients:
        b.add_client(client)
    return b.build()


def _ctx(base, prop):
    return CheckContext(
        baseline=AnalysisContext(base),
        proposed=AnalysisContext(prop),
        diff=diff_ir(base, prop),
    )


def _run(base, prop):
    return WlanClientImpactCheck().run(_ctx(base, prop))


def _client(mac="001122334455", *, ssid="corp", ap_id="ap1"):
    return wireless_client(mac, ap_id, ssid=ssid)


def test_delete_wlan_with_active_client_no_survivor_fails():
    client = _client()
    res = _run(_ir(_wlan(), clients=(client,)), _ir(clients=(client,)))
    assert res.status is Status.FAIL
    f = res.findings[0]
    assert f.code == "wireless.wlan.client_impact.coverage_lost"
    assert f.category is FindingCategory.NETWORK
    assert f.severity is Severity.ERROR
    assert f.confidence.level is ConfidenceLevel.HIGH
    assert f.affected_entities == (client.id,)
    assert f.evidence["clients"] == [{"mac": client.mac, "ap": "ap1", "ssid": "corp"}]


def test_disable_wlan_with_active_client_fails():
    client = _client()
    res = _run(
        _ir(_wlan(enabled=True), clients=(client,)),
        _ir(_wlan(enabled=False), clients=(client,)),
    )
    assert res.status is Status.FAIL
    assert res.findings[0].code.endswith(".coverage_lost")


def test_rename_old_ssid_with_active_client_fails():
    client = _client(ssid="corp")
    res = _run(
        _ir(_wlan(ssid="corp"), clients=(client,)),
        _ir(_wlan(ssid="guest"), clients=(client,)),
    )
    assert res.status is Status.FAIL
    assert res.findings[0].evidence["ssid"] == "corp"


def test_scope_shrink_excluding_client_ap_fails():
    client = _client(ap_id="ap1")
    res = _run(
        _ir(_wlan(apply_to="site"), clients=(client,)),
        _ir(_wlan(apply_to="aps", ap_ids=("ap2",)), clients=(client,)),
    )
    assert res.status is Status.FAIL
    assert res.findings[0].code.endswith(".coverage_lost")


def test_site_scope_survivor_reaches_high_complete_pass():
    client = _client()
    res = _run(
        _ir(_wlan("w1"), clients=(client,)),
        _ir(_wlan("w2"), clients=(client,)),
    )
    assert res.status is Status.PASS
    assert res.coverage.state is CoverageState.COMPLETE
    assert res.confidence is not None
    assert res.confidence.level is ConfidenceLevel.HIGH


def test_wxtag_only_survivor_fails_closed():
    client = _client()
    res = _run(
        _ir(_wlan("w1"), clients=(client,)),
        _ir(_wlan("w2", apply_to="wxtags", wxtag_ids=("tag1",)), clients=(client,)),
    )
    assert res.status is Status.FAIL
    assert any(f.code == "wireless.wlan.client_impact.coverage_lost" for f in res.findings)


def test_missing_client_telemetry_with_affected_ssid_warns_unverified():
    res = _run(
        _ir(_wlan(), clients_known=False),
        _ir(clients_known=False),
    )
    assert res.status is Status.WARN
    assert res.findings[0].code == "wireless.wlan.client_impact.unverified"
    assert res.findings[0].category is FindingCategory.OPERATIONAL
    assert res.findings[0].severity is Severity.WARNING


def test_missing_client_telemetry_added_only_wlan_stays_high_complete_pass():
    res = _run(_ir(clients_known=False), _ir(_wlan(), clients_known=False))
    assert res.status is Status.PASS
    assert res.coverage.state is CoverageState.COMPLETE
    assert res.confidence is not None
    assert res.confidence.level is ConfidenceLevel.HIGH


def test_zero_clients_on_affected_ssid_stays_high_complete_pass_with_note():
    res = _run(_ir(_wlan()), _ir())
    assert res.status is Status.PASS
    assert res.coverage.state is CoverageState.COMPLETE
    assert res.confidence is not None
    assert res.confidence.level is ConfidenceLevel.HIGH
    assert res.coverage.notes


def test_two_changed_wlans_same_ssid_aggregate_to_one_finding():
    client = _client()
    res = _run(
        _ir(_wlan("w2"), _wlan("w1"), clients=(client,)),
        _ir(clients=(client,)),
    )
    assert res.status is Status.FAIL
    assert len(res.findings) == 1
    f = res.findings[0]
    assert f.subject is not None and f.subject.id == "w1"
    assert [c.ref.id for c in f.caused_by] == ["w1", "w2"]


def test_disabled_baseline_wlan_delete_does_not_create_coverage_loss():
    client = _client()
    res = _run(_ir(_wlan(enabled=False), clients=(client,)), _ir(clients=(client,)))
    assert res.status is Status.PASS
    assert not any(f.code.endswith(".coverage_lost") for f in res.findings)


def test_unknown_ssid_client_with_affected_ssid_warns_unverified_not_pass():
    client = _client(ssid=None)
    res = _run(_ir(_wlan(), clients=(client,)), _ir(clients=(client,)))
    assert res.status is Status.WARN
    assert any(f.code == "wireless.wlan.client_impact.unverified" for f in res.findings)
