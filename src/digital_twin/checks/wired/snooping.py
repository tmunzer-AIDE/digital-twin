"""wired.dhcp.snooping — snooping turned on with no trusted egress toward the
vlan's modeled DHCP source (GS25).

The path rule: a snooping switch drops DHCP offers arriving on UNTRUSTED
ports, so for each vlan the switch snoops, SOME egress that can reach the
vlan's modeled DHCP source must be trusted — ONE trusted path is enough.

Honesty rails:
- unknown trust (tri-state None) or an edge whose carriage is not config-
  certain is NEVER read as untrusted: the evaluation abstains (PARTIAL note),
  never a dropped-offer conclusion from blindness;
- "site" sources are unlocatable BY DESIGN (the site dhcpd_config does not
  say which switch hosts the service) — abstention note, and any conclusion
  against a co-listed gateway source is hedged in its message;
- introduction is ACTIVITY-based (native-mismatch lesson): a blockage that
  existed identically in baseline demotes to INFO, but the baseline probe
  folds node ids with the BASELINE vc map — VC membership may differ;
- a blind gateway source (l3_unmodeled / dhcp_unresolved) caps the claim at
  MEDIUM — its placement is uncertain.
"""

from __future__ import annotations

import networkx as nx

from digital_twin.analysis.context import AnalysisContext
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
from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.indexes import node_for, vc_root_map
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_BLIND_SOURCE = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the source gateway's namespace is unmodeled — placement uncertain",),
)


def _snooped_vlans(ir: IR, did: str) -> tuple[int, ...]:
    """The vlan ids the device snoops: only vlans WITH a modeled DHCP source
    matter (without one there is no path to verify)."""
    dev = ir.devices.get(did)
    if dev is None or dev.dhcp_snooping is None:
        return ()
    if dev.dhcp_snooping == ("*",):
        return tuple(sorted(v.vlan_id for v in ir.vlans.values() if v.dhcp_sources))
    names = set(dev.dhcp_snooping)
    return tuple(
        sorted(
            v.vlan_id for v in ir.vlans.values() if v.name in names and v.dhcp_sources
        )
    )


def _egress_trust(
    actx: AnalysisContext,
    ir: IR,
    vc_root: dict[str, str],
    did: str,
    vlan: int,
    source: str,
) -> tuple[str, tuple[str, ...]]:
    """("ok" | "blocked" | "unknown" | "unreachable", untrusted port ids).

    Examines every vlan-graph edge at `did` that can still reach `source`
    once `did` itself is removed (its OWN ports are the snooping boundary).
    Any trusted local port -> ok; any unknown trust (or carriage-uncertain
    edge) -> unknown; all known-untrusted -> blocked; no path -> unreachable.
    """
    g = actx.vlan_graph(vlan)
    if source not in g or did not in g:
        return ("unreachable", ())
    pruned = nx.restricted_view(g, [did], [])
    candidates: list[tuple[bool | None, str]] = []
    for _, neighbor, data in g.edges(did, data=True):
        edge = data["data"]
        reaches = neighbor == source or (
            neighbor in pruned and source in pruned and nx.has_path(pruned, neighbor, source)
        )
        if not reaches:
            continue
        edge_certain = edge.confidence.level is ConfidenceLevel.HIGH
        for pid in edge.member_ports:
            port = ir.ports.get(pid)
            if port is None or port.disabled:
                continue
            if node_for(vc_root, port.device_id) != did:
                continue  # the peer side's trust is not this switch's boundary
            trust = port.dhcp_trusted if edge_certain else None
            candidates.append((trust, pid))
    if not candidates:
        return ("unreachable", ())
    if any(trust is True for trust, _ in candidates):
        return ("ok", ())
    if any(trust is None for trust, _ in candidates):
        return ("unknown", ())
    return ("blocked", tuple(sorted(pid for _, pid in candidates)))


class DhcpSnoopingCheck:
    id = "wired.dhcp.snooping"
    title = "DHCP snooping with no trusted path to the vlan's DHCP source"
    domain = "wired.dhcp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("device", "port", "dhcp_scope", "vlan", "link"))

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        prop_root, base_root = vc_root_map(prop_ir), vc_root_map(base_ir)
        findings: list[Finding] = []
        notes: list[str] = []
        for did in sorted(prop_ir.devices):
            if prop_ir.devices[did].role is not DeviceRole.SWITCH:
                continue
            node = node_for(prop_root, did)
            for vlan in _snooped_vlans(prop_ir, did):
                sources = prop_ir.vlans[vlan].dhcp_sources
                has_site = "site" in sources
                if has_site:
                    notes.append(
                        f"{did} vlan {vlan}: a \"site\" DHCP source is unlocatable — "
                        "site-hosted service placement is unmodeled, snooping trust "
                        "toward it cannot be verified"
                    )
                for source in sources:
                    if source == "site":
                        continue
                    src_node = node_for(prop_root, source)
                    if src_node == node:
                        continue  # locally hosted — never crosses an egress port
                    state, blocked = _egress_trust(
                        ctx.proposed, prop_ir, prop_root, node, vlan, src_node
                    )
                    if state == "unknown":
                        notes.append(
                            f"{did} vlan {vlan}: trust or carriage toward DHCP source "
                            f"{source} is unknown — cannot conclude offers drop"
                        )
                        continue
                    if state == "unreachable":
                        notes.append(
                            f"{did} vlan {vlan}: DHCP source {source} is not locatable "
                            "in the modeled topology for this vlan — snooping trust "
                            "unverifiable"
                        )
                        continue
                    if state == "ok":
                        continue
                    pre = (
                        vlan in _snooped_vlans(base_ir, did)
                        and (bv := base_ir.vlans.get(vlan)) is not None
                        and source in bv.dhcp_sources
                        and _egress_trust(
                            ctx.baseline,
                            base_ir,
                            base_root,
                            node_for(base_root, did),
                            vlan,
                            node_for(base_root, source),
                        )[0]
                        == "blocked"
                    )
                    src_dev = prop_ir.devices.get(source)
                    blind = src_dev is not None and (
                        src_dev.l3_unmodeled or src_dev.dhcp_unresolved
                    )
                    confidence = _BLIND_SOURCE if blind else _HIGH
                    if blind and not pre:
                        # GS24 rail: the cap lives on the finding, this note
                        # makes the blind spot visible — same condition, the
                        # layers cannot disagree. NOT on INFO-demoted
                        # pre-existing context: PARTIAL keys off CONCLUSIONS
                        # (GS22 rule) or the coverage side door floors an
                        # unrelated change to REVIEW
                        notes.append(
                            f"gateway {source}: namespace unmodeled — its DHCP "
                            "service placement is (partly) invisible; the "
                            "dropped-offer conclusion is capped at MEDIUM"
                        )
                    findings.append(
                        Finding(
                            source=FindingSource.CHECK,
                            category=FindingCategory.NETWORK,
                            code=f"{self.id}.untrusted_path",
                            severity=Severity.INFO if pre else Severity.WARNING,
                            confidence=confidence,
                            message=(
                                f"dhcp snooping on {did} vlan {vlan}: every egress "
                                f"toward DHCP source {source} is untrusted — offers "
                                "are dropped at lease renewal"
                                + (
                                    " — an unmodeled site-hosted service may still "
                                    "serve this vlan"
                                    if has_site
                                    else ""
                                )
                                + (" (pre-existing, unchanged)" if pre else "")
                            ),
                            affected_entities=(did, str(vlan)),
                            evidence={
                                "device": did,
                                "vlan": vlan,
                                "source": source,
                                "untrusted_egress": list(blocked),
                            },
                        )
                    )
        conclusions = [f for f in findings if f.severity is not Severity.INFO]
        # abstention notes stay attached even without findings: an ACTIVE
        # snooping evaluation that could not conclude must be visible
        # (stp_root precedent); they only arise for vlans snooped in PROPOSED
        return CheckResult(
            check_id=self.id,
            status=Status.WARN if conclusions else Status.PASS,
            findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(
                min_confidence(*(f.confidence for f in conclusions)) if conclusions else _HIGH
            ),
            reasoning="probed each snooped vlan's trusted egress toward its modeled "
            "DHCP sources on the per-vlan graph, baseline-parity by activity",
        )
