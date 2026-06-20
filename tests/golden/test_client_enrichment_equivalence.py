"""Enrichment is non-load-bearing: present / absent / BROKEN nac_clients must all
produce the same decision, severity multiset, and coverage. The broken arm is the
regression for the self-isolating-ingester guarantee (a malformed row never UNKNOWNs)."""
import copy

from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision
from tests.golden.builders import (
    EDGE,
    EDGE_ACCESS_PORT,
    GS_VLAN,
    WIRED_CLIENT_MAC,
    augmented_doc,
    device_op,
    plan_for,
    write_doc,
)

_NAC_PRESENT = [{
    "mac": WIRED_CLIENT_MAC, "last_family": "Surveillance Camera", "last_mfg": "Verkada Inc",
    "auth_type": "mab", "last_status": "permitted", "last_nacrule_name": "wired_camera_mab",
    "last_vlan": str(GS_VLAN), "vlan_source": "nactag",
}]
# rows a naive parser would choke on: a bare string, a mac-less dict, a None mac
_NAC_BROKEN = ["garbage", {"oops": 1}, {"mac": None}]


def _gs4_doc_and_plan():
    """GS4: move the wired client's access port off vlan 999 -> client.impact fires."""
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    doc["setting"]["port_usages"]["gs_access2"] = {
        "mode": "access",
        "port_network": next(
            name for name, net in doc["setting"]["networks"].items()
            if isinstance(net, dict) and net.get("vlan_id") not in (None, 999)
        ),
    }
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_ACCESS_PORT.replace("/", "__"): "gs_access2"})]
    )
    return doc, plan


def _signature(v):
    return (
        v.decision,
        tuple(sorted(f.severity.value for f in v.findings)),
        tuple(sorted((r.check_id, r.coverage.state.value) for r in v.check_results)),
    )


def _run(nac, tmp_path, tag):
    doc, plan = _gs4_doc_and_plan()
    doc["nac_clients"] = copy.deepcopy(nac)
    fixture = write_doc(doc, tmp_path / f"{tag}.json")
    return simulate(plan, provider=FixtureProvider(fixture))


def test_present_absent_broken_are_equivalent(tmp_path):
    present = _run(_NAC_PRESENT, tmp_path, "present")
    absent = _run([], tmp_path, "absent")
    broken = _run(_NAC_BROKEN, tmp_path, "broken")
    assert _signature(present) == _signature(absent) == _signature(broken)
    assert broken.decision is not Decision.UNKNOWN  # self-isolating ingester held


def test_present_arm_actually_enriches(tmp_path):
    present = _run(_NAC_PRESENT, tmp_path, "present2")
    impact = next(f for f in present.findings if f.code == "wired.client.impact.active_clients")
    entry = next(i for i in impact.evidence["impacts"] if i["mac"] == WIRED_CLIENT_MAC)
    assert entry["identity"]["family"] == "Surveillance Camera"
    assert entry["identity"]["nacrule"] == "wired_camera_mab"
