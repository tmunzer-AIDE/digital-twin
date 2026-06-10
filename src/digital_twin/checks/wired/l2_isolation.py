"""wired.l2.isolation — PHYSICAL severance of a member-bearing segment.

Found in real use (2026-06-10): disabling a switch's only uplink blackholes the
switch and everything on it, yet no per-vlan check could say so — the site had
no modeled L3 exits (no IRBs; L3 on an SRX the LLDP view never linked in), so
the exit-centric blackhole had nothing to anchor on. The severance itself needs
no exit: it is a structural fact on the PHYSICAL L2 multigraph.

Per baseline connected component: a proposed component that is a STRICT subset
of its baseline component (its reach shrank) and holds occupants — config
member access ports, observed clients (wired or wireless), or WLAN-requiring
APs — has been cut off from the rest of its former domain by the delta.

- Severity is terminal here (this layer only): ERROR at HIGH confidence,
  WARNING below — confidence = MIN over the baseline boundary links the delta
  severed (their existence is what proves the lost reach).
- A pre-existing island (proposed nodes == baseline nodes) is unchanged
  context, never a finding — e.g. an offline switch that was already alone.
- Redundancy is respected by construction: graph components, not "an uplink
  died" — one surviving physical path means no fragmentation, no finding.
"""

from __future__ import annotations

from collections import defaultdict

import networkx as nx

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import AttachKind, PortMode
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def _occupants(ir: IR) -> dict[str, dict[str, int]]:
    """node -> {member_ports, clients, wlan_aps} counts (who LIVES there)."""
    vc_root = vc_root_map(ir)
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for port in ir.ports.values():
        if port.mode is PortMode.ACCESS and port.native_vlan is not None and not port.disabled:
            out[node_for(vc_root, port.device_id)]["member_ports"] += 1
    for client in ir.clients:
        if client.attach_kind is AttachKind.PORT:
            attach_port = ir.ports.get(client.attach_id)
            if attach_port is None:
                continue
            out[node_for(vc_root, attach_port.device_id)]["clients"] += 1
        else:
            out[node_for(vc_root, client.attach_id)]["clients"] += 1
    for ap_id in ir.ap_wlan_vlans:
        out[node_for(vc_root, ap_id)]["wlan_aps"] += 1
    return out


class L2IsolationCheck:
    id = "wired.l2.isolation"
    title = "Physical severance of a member-bearing segment"
    domain = "wired.l2"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        base_l2 = ctx.baseline.l2_graph()
        base_comps = [frozenset(c) for c in nx.connected_components(base_l2)]
        prop_comps = [frozenset(c) for c in nx.connected_components(ctx.proposed.l2_graph())]
        occupants = _occupants(ctx.baseline.ir)
        vc_root = vc_root_map(ctx.baseline.ir)

        findings: list[Finding] = []
        worst = Status.PASS
        for fragment in prop_comps:
            baseline_home = next((b for b in base_comps if fragment & b), None)
            if baseline_home is None or not (fragment < baseline_home):
                continue  # new/unchanged/merged reach — nothing severed
            occupied = {n: occupants[n] for n in sorted(fragment) if occupants.get(n)}
            if not occupied:
                continue  # an empty segment going dark is not client impact
            # confidence rides the severed LINKS' existence (their provenance —
            # two-sided LLDP = HIGH), NOT the edges' carriage confidence, which
            # blind-peer carriage assumptions may cap at MEDIUM: severance does
            # not care what the link carried, only that it connected.
            severed_links = []
            for lnk in ctx.baseline.ir.links:
                na = node_for(vc_root, ctx.baseline.ir.port(lnk.a_port).device_id)
                nb = node_for(vc_root, ctx.baseline.ir.port(lnk.b_port).device_id)
                if (na in fragment) != (nb in fragment):
                    severed_links.append(lnk.meta.confidence)
            confidence = min_confidence(*severed_links) if severed_links else _HIGH
            high = confidence.level is ConfidenceLevel.HIGH
            totals = {
                key: sum(c.get(key, 0) for c in occupied.values())
                for key in ("member_ports", "clients", "wlan_aps")
            }
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code="wired.l2.isolation.severed",
                    severity=Severity.ERROR if high else Severity.WARNING,
                    confidence=confidence,
                    message=(
                        f"segment {sorted(fragment)} is physically severed from the rest "
                        f"of its L2 domain — {totals['clients']} observed client(s), "
                        f"{totals['member_ports']} member port(s), {totals['wlan_aps']} "
                        "WLAN-serving AP(s) lose all paths beyond it"
                    ),
                    affected_entities=tuple(sorted(fragment)),
                    evidence={
                        "fragment_nodes": sorted(fragment),
                        "lost_peers": sorted(baseline_home - fragment),
                        "occupants": {n: dict(c) for n, c in occupied.items()},
                    },
                )
            )
            worst = Status.FAIL if high else (worst if worst is Status.FAIL else Status.WARN)
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=min_confidence(*(f.confidence for f in findings)) if findings else _HIGH,
            reasoning="compared physical L2 connected components baseline vs proposed",
        )
