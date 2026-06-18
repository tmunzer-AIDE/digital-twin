"""CA-T15: client_impact per-impact causes + top-level union.

Three targeted cause-attribution cases:

1. Two clients whose own access ports changed (vlan_move) → each impact's
   ``caused_by`` names its own port; the top-level ``Finding.caused_by`` is
   the deduped union of both.

2. A client blackholed by an upstream TRUNK change (its own access port is
   unchanged) → its nested ``caused_by`` names the upstream trunk port, not
   empty.  This is the key discriminator: if the check attributed only the
   access port it would return ().

3. A client blackholed by a removed IRB l3intf (carriage unchanged) → its
   nested ``caused_by`` names the removed l3intf via ``causes_for_blackhole``.

All assertions are on ``c.ref.id`` (not ``.name``) because ``name_findings``
runs in the registry, not here.
"""

from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.client_impact import ClientImpactCheck
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from tests.factories import access_port, irb, link, sw, trunk_port, wired_client


def _ctx(baseline, proposed):
    return CheckContext(
        baseline=AnalysisContext(baseline),
        proposed=AnalysisContext(proposed),
        diff=diff_ir(baseline, proposed),
    )


def _ids(causes):
    return sorted((c.ref.kind, c.ref.id) for c in causes)


# ---------------------------------------------------------------------------
# Case 1: two clients whose access ports changed native vlan (vlan_move)
# ---------------------------------------------------------------------------

def _two_vlan_move_ir(*, moved: bool):
    """Switch A with two access ports.  Client aa:aa on A:acc1 (vlan 10),
    client bb:bb on A:acc2 (vlan 20).  In the ``moved`` variant BOTH ports
    flip to the opposite vlan — so both clients see a vlan_move and each
    impact's caused_by must point to its own port."""
    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    for vid in (10, 20):
        b.add_vlan(Vlan(vlan_id=vid, name=f"v{vid}", scope="s1"))
    acc1_vlan = 20 if moved else 10
    acc2_vlan = 10 if moved else 20
    b.add_port(access_port("A", "acc1", acc1_vlan))
    b.add_port(access_port("A", "acc2", acc2_vlan))
    b.add_port(trunk_port("A", "up", tagged=(10, 20)))
    b.add_port(trunk_port("B", "down", tagged=(10, 20)))
    b.add_link(link("A:up", "B:down"))
    b.add_l3intf(irb("B", 10))
    b.add_l3intf(irb("B", 20))
    b.add_client(wired_client("aa:aa", "A:acc1", vlan=10))
    b.add_client(wired_client("bb:bb", "A:acc2", vlan=20))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_two_vlan_move_clients_each_name_own_port_and_union():
    base, prop = _two_vlan_move_ir(moved=False), _two_vlan_move_ir(moved=True)
    result = ClientImpactCheck().run(_ctx(base, prop))
    assert result.status is Status.WARN
    f = result.findings[0]
    impacts = f.evidence["impacts"]

    # Each client impact names its own access port
    by_mac = {i["mac"]: i for i in impacts}
    assert set(by_mac.keys()) == {"aa:aa", "bb:bb"}
    assert _ids(by_mac["aa:aa"]["caused_by"]) == [("port", "A:acc1")]
    assert _ids(by_mac["bb:bb"]["caused_by"]) == [("port", "A:acc2")]

    # Top-level union = both ports (order-insensitive)
    assert _ids(f.caused_by) == [("port", "A:acc1"), ("port", "A:acc2")]


# ---------------------------------------------------------------------------
# Case 2: client blackholed by an upstream trunk change (access port UNCHANGED)
# ---------------------------------------------------------------------------

def _upstream_trunk_cut_ir(*, cut: bool):
    """Three-switch chain: A -- B -- C, vlan 10 everywhere, exit IRB on A.
    Client cc:cc sits on C (access port C:acc, vlan 10).

    In the ``cut`` variant B's port to C drops vlan 10, stranding {C} — the
    client's own access port (C:acc) is UNCHANGED.  The correct cause is the
    trunk port B:to-C (the boundary cut), not C:acc.
    """
    b = IRBuilder()
    for d in ("A", "B", "C"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(trunk_port("B", "to-C", tagged=() if cut else (10,)))
    b.add_port(trunk_port("C", "to-B", tagged=(10,)))
    b.add_link(link("B:to-C", "C:to-B"))
    b.add_port(access_port("C", "acc", 10))
    b.add_l3intf(irb("A", 10))
    b.add_client(wired_client("cc:cc", "C:acc", vlan=10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_upstream_trunk_cut_names_trunk_not_access_port():
    result = ClientImpactCheck().run(
        _ctx(_upstream_trunk_cut_ir(cut=False), _upstream_trunk_cut_ir(cut=True))
    )
    assert result.status is Status.WARN
    f = result.findings[0]
    impacts = f.evidence["impacts"]
    bh = next(i for i in impacts if i["mac"] == "cc:cc" and i["impact"] == "blackhole")

    # The upstream trunk port B:to-C is named — NOT empty, NOT only the access port
    cause_ids = _ids(bh["caused_by"])
    assert ("port", "B:to-C") in cause_ids
    # access port C:acc is not in the delta -> should not appear
    assert ("port", "C:acc") not in cause_ids

    # Top-level union must include the trunk port
    assert ("port", "B:to-C") in _ids(f.caused_by)


# ---------------------------------------------------------------------------
# Case 3: client blackholed by a removed IRB l3intf (carriage unchanged)
# ---------------------------------------------------------------------------

def _irb_removal_ir(*, rm_irb: bool):
    """A -- B connected on vlan 10 (carriage UNCHANGED); the only exit IRB on A
    is removed in the ``rm_irb`` variant.  Client dd:dd sits on B (access port
    B:acc, vlan 10) and gets blackholed by the exit removal.  The correct cause
    is the removed l3intf A:l3:irb:10."""
    b = IRBuilder()
    for d in ("A", "B"):
        b.add_device(sw(d))
    b.add_vlan(Vlan(vlan_id=10, name="corp", scope="s1"))
    b.add_port(trunk_port("A", "to-B", tagged=(10,)))
    b.add_port(trunk_port("B", "to-A", tagged=(10,)))
    b.add_link(link("A:to-B", "B:to-A"))
    b.add_port(access_port("B", "acc", 10))
    if not rm_irb:
        b.add_l3intf(irb("A", 10))
    b.add_client(wired_client("dd:dd", "B:acc", vlan=10))
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def test_removed_irb_names_l3intf():
    result = ClientImpactCheck().run(
        _ctx(_irb_removal_ir(rm_irb=False), _irb_removal_ir(rm_irb=True))
    )
    assert result.status is Status.WARN
    f = result.findings[0]
    impacts = f.evidence["impacts"]
    bh = next(i for i in impacts if i["mac"] == "dd:dd" and i["impact"] == "blackhole")

    # The removed IRB is named as the cause
    assert ("l3intf", "A:l3:irb:10") in _ids(bh["caused_by"])

    # Top-level union also carries the l3intf
    assert ("l3intf", "A:l3:irb:10") in _ids(f.caused_by)
