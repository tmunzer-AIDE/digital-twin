"""wired.stp.policy — precise STP policy attribution under a REVIEW floor.

The four StpPolicy knobs are modeled but the bridge domain is not provable
(unmanaged switches, invisible BPDU sources, off-fabric roots, convergence),
so a policy change NEVER resolves SAFE in this slice: concrete predicted harm
escalates (.blocking_risk / .root_protect_risk, ERROR only at HIGH evidence);
everything else floors REVIEW via .policy_change. SAFE is deferred to a
future STP tree engine validated against live stp_state (see the 2026-07-03
spec).

.blocking_risk fires ONLY when the delta enables stp_required (False/absent
-> True; an unresolved: token never reaches this code) AND the port has a
CANDIDATE non-BPDU peer: (1) an LLDP-tied AP (two-sided tie -> ERROR/HIGH,
one-sided -> WARNING/MEDIUM); (2) an observed wired client on the port with
no modeled bridge peer -> ERROR/HIGH; (3) a modeled peer port with
bpdu_filter=True (two-sided tie -> ERROR/HIGH, else WARNING/MEDIUM). Unknown/
no peer evidence does NOT qualify (spec P2) -- the model cannot claim "this
peer won't send BPDUs" about a peer it cannot see, so that case falls through
to the .policy_change floor with a coverage note. A fire suppresses that
port's .policy_change (most-specific precedence wins). Disabling
stp_required (True -> False) is never a risk -- floor only. A pre-existing
True left untouched by the delta (some OTHER knob on the port changed) is
INFO context, per the .preexisting convention used elsewhere in this check
family."""

from __future__ import annotations

import dataclasses

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    Link,
    min_confidence,
)
from digital_twin.ir.entities import Client, DeviceRole, Port, PortMode, StpPolicy
from digital_twin.ir.indexes import clients_by_ap, clients_by_port
from digital_twin.ir.model import IR

_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the bridge domain (unmanaged switches, off-fabric roots, convergence) "
             "is not provable",),
)
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_ONE_SIDED_TIE = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the peer tie is one-sided LLDP, not corroborated by both ends",),
)


def _changed_knobs(old: StpPolicy | None, new: StpPolicy | None) -> list[str]:
    o, n = old or StpPolicy(), new or StpPolicy()
    return [
        f.name for f in dataclasses.fields(StpPolicy)
        if getattr(o, f.name) != getattr(n, f.name)
    ]


def _is_unresolved(value: object) -> bool:
    return isinstance(value, str) and value.startswith("unresolved:")


# --- .blocking_risk peer classification --------------------------------------
#
# Cloned (small idiom, not imported) from checks/wired/admin_disable.py:
# _ap_ports-style AP ties and _nonap_peer_links(base_ir) returning a
# port -> Link map so classification can read the LINK's own tie confidence.

def _ap_peer_links(ir: IR) -> dict[str, tuple[str, Link]]:
    """switch-port id -> (ap_device_id, the LLDP link) for every AP peer."""
    out: dict[str, tuple[str, Link]] = {}
    for lk in ir.links:
        pa, pb = ir.ports.get(lk.a_port), ir.ports.get(lk.b_port)
        if pa is None or pb is None:
            continue
        a_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        if a_ap == b_ap:
            continue  # not an AP-peer link (both or neither are APs)
        ap_port, sw_port = (pa, pb) if a_ap else (pb, pa)
        out[sw_port.id] = (ap_port.device_id, lk)
    return out


def _bpdu_filter_peer_links(ir: IR) -> dict[str, tuple[Port, Link]]:
    """switch-port id -> (peer_port, the link) where the peer port has
    bpdu_filter=True. Neither side may be an AP (handled by _ap_peer_links)."""
    out: dict[str, tuple[Port, Link]] = {}
    for lk in ir.links:
        pa, pb = ir.ports.get(lk.a_port), ir.ports.get(lk.b_port)
        if pa is None or pb is None:
            continue
        a_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        if a_ap or b_ap:
            continue
        if pb.bpdu_filter:
            out[pa.id] = (pb, lk)
        if pa.bpdu_filter:
            out[pb.id] = (pa, lk)
    return out


def _tie_confidence(lk: Link) -> Confidence:
    """HIGH only for a genuinely two-sided (HIGH-level) provenance link;
    anything weaker (one-sided LLDP, inferred) caps the tie at MEDIUM."""
    return _HIGH if lk.meta.confidence.level is ConfidenceLevel.HIGH else _ONE_SIDED_TIE


# Cloned (small idiom) from checks/wired/l2_isolation.py:_occupants — here
# scoped to a single node (the device behind the changed port) rather than
# every node in a component: who is BEHIND this blocking port.
def _occupants_behind(ir: IR, device_id: str) -> dict[str, int]:
    out = {"member_ports": 0, "clients": 0, "wlan_aps": 0}
    wired = clients_by_port(ir)
    ap_clients = clients_by_ap(ir)
    for port in ir.ports.values():
        if port.device_id != device_id:
            continue
        if port.mode is PortMode.ACCESS and port.native_vlan is not None and not port.disabled:
            out["member_ports"] += 1
        out["clients"] += len(wired.get(port.id, []))
    if ir.devices[device_id].role is DeviceRole.AP:
        out["wlan_aps"] += len(ap_clients.get(device_id, []))
    return out


class StpPolicyCheck:
    id = "wired.stp.policy"
    title = "STP policy change — blocking/root-protect/mismatch attribution"
    domain = "wired.stp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # precise: a port entry added/removed, or stp_policy among its
        # changed fields — an unrelated port edit must not wake this check
        added_or_removed = any(r.kind == "port" for r in (*diff.added, *diff.removed))
        changed = any(
            m.ref.kind == "port" and "stp_policy" in m.changed_fields
            for m in diff.modified
        )
        return added_or_removed or changed

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        # peer/occupancy evidence reads the PROPOSED topology: the question is
        # "will the port's peer, as it will exist, send BPDUs" — this also
        # covers a port ADD, where the tie/peer only exists in `prop_ir`.
        ap_peers = _ap_peer_links(prop_ir)
        bpdu_filter_peers = _bpdu_filter_peer_links(prop_ir)
        wired = clients_by_port(prop_ir)
        findings: list[Finding] = []
        notes: list[str] = []
        for pid in sorted(base_ir.ports.keys() | prop_ir.ports.keys()):
            old = base_ir.ports[pid].stp_policy if pid in base_ir.ports else None
            new = prop_ir.ports[pid].stp_policy if pid in prop_ir.ports else None
            if old == new:
                continue
            knobs = _changed_knobs(old, new)
            if not knobs:
                continue
            new_policy = new or StpPolicy()
            old_policy = old or StpPolicy()
            unresolved_knobs = [
                k for k in knobs if _is_unresolved(getattr(new_policy, k))
            ]
            for k in unresolved_knobs:
                notes.append(
                    f"port {pid}: {k} is an unresolved: token — no precise "
                    f"prediction is possible, floored to REVIEW"
                )

            risk_finding: Finding | None = None
            if "stp_required" in knobs and new_policy.stp_required is True:
                # False/absent -> True only; an unresolved: token never reaches
                # here (it is filtered into unresolved_knobs above, and a token
                # is never `is True`).
                risk_finding, note = self._blocking_risk(
                    ctx, pid, ap_peers, bpdu_filter_peers, wired
                )
                if note is not None:
                    notes.append(note)

            if risk_finding is not None:
                findings.append(risk_finding)
            else:
                findings.append(
                    Finding(
                        source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                        code=f"{self.id}.policy_change", severity=Severity.WARNING,
                        confidence=_MEDIUM,
                        message=f"port {pid}: STP policy changed ({', '.join(knobs)}) — "
                                f"impact not provable in this slice (review)",
                        affected_entities=(pid,), subject=ObjectRef("port", pid),
                        evidence={"port": pid, "knobs": knobs},
                        caused_by=ctx.delta_index.causes("port", [pid]),
                    )
                )

            # pre-existing stp_required=True, untouched by THIS delta (some
            # OTHER knob on the port changed) -> INFO context, never re-flagged.
            if (
                "stp_required" not in knobs
                and old_policy.stp_required is True
                and new_policy.stp_required is True
            ):
                findings.append(
                    Finding(
                        source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                        code=f"{self.id}.preexisting", severity=Severity.INFO,
                        confidence=_HIGH,
                        message=f"port {pid}: stp_required is pre-existing (unchanged by "
                                f"the delta) — context only",
                        affected_entities=(pid,), subject=ObjectRef("port", pid),
                        evidence={"port": pid, "knob": "stp_required"},
                        caused_by=(),
                    )
                )
        coverage = (
            Coverage(state=CoverageState.PARTIAL, notes=tuple(notes))
            if notes
            else Coverage(state=CoverageState.COMPLETE)
        )
        confidences = [f.confidence for f in findings if f.severity is not Severity.INFO]
        return CheckResult(
            check_id=self.id,
            status=status_from_findings(findings),
            findings=tuple(findings),
            coverage=coverage,
            confidence=min_confidence(*confidences) if confidences else _HIGH,
            reasoning="compared per-port StpPolicy baseline vs proposed; every "
                      "change floors REVIEW (bridge domain not provable) unless "
                      "concrete no-BPDU-peer harm escalates it",
        )

    def _blocking_risk(
        self,
        ctx: CheckContext,
        pid: str,
        ap_peers: dict[str, tuple[str, Link]],
        bpdu_filter_peers: dict[str, tuple[Port, Link]],
        wired: dict[str, list[Client]],
    ) -> tuple[Finding | None, str | None]:
        """Classify the no-BPDU-peer candidate for a port whose stp_required
        just went True. Order: (1) LLDP-tied AP, (2) observed wired client with
        no modeled bridge peer, (3) modeled bpdu_filter peer, (4) unknown ->
        None (falls through to the .policy_change floor) + a coverage note."""
        prop_ir = ctx.proposed.ir
        port_ref = ObjectRef("port", pid)

        ap = ap_peers.get(pid)
        if ap is not None:
            ap_id, lk = ap
            conf = _tie_confidence(lk)
            severity = Severity.ERROR if conf.level is ConfidenceLevel.HIGH else Severity.WARNING
            reason = (
                "LLDP-tied AP peer, two-sided tie — the AP will not source BPDUs"
                if conf.level is ConfidenceLevel.HIGH
                else "LLDP-tied AP peer, one-sided tie — weaker evidence caps at WARNING"
            )
            return (
                Finding(
                    source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                    code=f"{self.id}.blocking_risk", severity=severity, confidence=conf,
                    message=f"port {pid}: stp_required enabled — AP {ap_id} peer will not "
                            f"send BPDUs, the port may end up blocking",
                    affected_entities=(pid, ap_id), subject=port_ref,
                    evidence={
                        "port": pid, "peer": ap_id, "peer_kind": "ap",
                        "tie_provenance": lk.meta.provenance.value,
                        "occupants_behind": _occupants_behind(prop_ir, ap_id),
                        "severity_reason": reason,
                    },
                    caused_by=ctx.delta_index.causes("port", [pid]),
                ),
                None,
            )

        n_wired = len(wired.get(pid, []))
        peer_link = bpdu_filter_peers.get(pid)
        if n_wired and peer_link is None:
            reason = "observed wired client on the port, no modeled bridge peer"
            return (
                Finding(
                    source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                    code=f"{self.id}.blocking_risk", severity=Severity.ERROR, confidence=_HIGH,
                    message=f"port {pid}: stp_required enabled — {n_wired} observed wired "
                            f"client(s), no modeled bridge peer to send BPDUs",
                    affected_entities=(pid,), subject=port_ref,
                    evidence={
                        "port": pid, "peer": None, "peer_kind": "client",
                        "tie_provenance": "observed",
                        "occupants_behind": {"member_ports": 0, "clients": n_wired,
                                              "wlan_aps": 0},
                        "severity_reason": reason,
                    },
                    caused_by=ctx.delta_index.causes("port", [pid]),
                ),
                None,
            )

        if peer_link is not None:
            peer_port, lk = peer_link
            conf = _tie_confidence(lk)
            severity = Severity.ERROR if conf.level is ConfidenceLevel.HIGH else Severity.WARNING
            reason = (
                "modeled peer port has bpdu_filter set, two-sided tie — the peer "
                "drops BPDUs entirely"
                if conf.level is ConfidenceLevel.HIGH
                else "modeled bpdu_filter peer, one-sided tie — weaker evidence caps "
                     "at WARNING"
            )
            return (
                Finding(
                    source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                    code=f"{self.id}.blocking_risk", severity=severity, confidence=conf,
                    message=f"port {pid}: stp_required enabled — peer port "
                            f"{peer_port.id} has bpdu_filter set and will not "
                            f"forward BPDUs",
                    affected_entities=(pid, peer_port.id), subject=port_ref,
                    evidence={
                        "port": pid, "peer": peer_port.id, "peer_kind": "bpdu_filter",
                        "tie_provenance": lk.meta.provenance.value,
                        "occupants_behind": _occupants_behind(prop_ir, peer_port.device_id),
                        "severity_reason": reason,
                    },
                    caused_by=ctx.delta_index.causes("port", [pid]),
                ),
                None,
            )

        # unknown/no-peer evidence: does NOT qualify (spec P2) -- falls
        # through to the .policy_change floor, plus a coverage note.
        return (
            None,
            f"stp_required enabled on {pid}: peer unobserved — blocking outcome "
            f"not assessable",
        )
