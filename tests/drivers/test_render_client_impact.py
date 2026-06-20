import dataclasses

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.drivers.render import render_human, verdict_to_dict
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import Verdict, assemble


def _verdict(impacts) -> Verdict:
    f = Finding(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK,
        code="wired.client.impact.active_clients", severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message=f"{len(impacts)} currently-connected client(s) affected by the delta",
        affected_entities=tuple(i["mac"] for i in impacts), evidence={"impacts": impacts},
    )
    base = assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=(),
        ),
        ir_diff=IRDiff((), (), ()),
    )
    return dataclasses.replace(base, findings=(f,), overall_severity=Severity.WARNING,
                               decision=Decision.REVIEW)


def test_human_expands_each_client_line():
    impacts = [{"mac": "aabbcc000001", "vlan": 30, "attachment": "sw1:mge-0/0/1",
                "impact": "disconnect", "detail": "attach port removed", "caused_by": (),
                "subnet": None, "dhcp_vlan_touched": False,
                "identity": {"hostname": "LiveDemo-CD51", "family": "Surveillance Camera",
                             "mfg": "Verkada Inc", "auth_type": "mab", "status": "permitted",
                             "nacrule": "wired_camera_mab"}}]
    out = render_human(_verdict(impacts))
    assert "LiveDemo-CD51" in out and "Surveillance Camera" in out
    assert "disconnect" in out and "mab" in out


def test_human_caps_at_20_with_more_note():
    impacts = [{"mac": f"aa{i:010x}", "vlan": 10, "attachment": "sw1:ge-0/0/1",
                "impact": "blackhole", "detail": "x", "caused_by": (),
                "subnet": None, "dhcp_vlan_touched": False} for i in range(25)]
    out = render_human(_verdict(impacts))
    assert "and 5 more" in out


def test_dict_carries_full_identity():
    impacts = [{"mac": "aabbcc000001", "vlan": 10, "attachment": "sw1:ge-0/0/1",
                "impact": "vlan_move", "detail": "access vlan 1 -> 20", "caused_by": (),
                "subnet": "10.0.0.0/24", "dhcp_vlan_touched": True,
                "identity": {"hostname": "LD_Kitchen", "mfg": "Mist Systems, Inc."}}]
    d = verdict_to_dict(_verdict(impacts))
    got = d["findings"][0]["evidence"]["impacts"][0]
    assert got["identity"]["hostname"] == "LD_Kitchen" and got["dhcp_vlan_touched"] is True
