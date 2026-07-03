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
family.

.root_protect_risk fires ONLY when the delta enables stp_no_root_port
(False/absent -> True; unresolved: tokens never reach this code) on a port
that is the device's ONLY graph path to the component's elected root (drop
the port's edge on the proposed L2 component graph; if the elected root
becomes unreachable from the port's device, it was the only path). Election
reuses stp_root.py:_root_of (same cross-check reuse rule as .blocking_risk):
ERROR/HIGH requires the root known at HIGH confidence (_root_of returned a
tuple AND any_default_assumed is False -- _root_of already folds
stp_priority_invalid into its _ABSTAIN sentinel, so a tuple result is never
riding on an uninterpretable priority). An only-path port whose election is
NOT HIGH (default-assumed priority, or _root_of abstained) degrades to
WARNING with a coverage note -- never guess at ERROR. A redundant path, or
the device itself being the elected root, is no risk code at all (the
.policy_change floor covers it). A fire suppresses that port's
.policy_change (same most-specific precedence as .blocking_risk).

.link_mismatch fires when both ends of a MODELED link disagree on the
EFFECTIVE (default False when absent) value of use_vstp or stp_p2p, and the
delta introduced or changed that disagreement on either end. Keyed by
(link, knob): use_vstp and stp_p2p mismatched on the same link are two
separate findings, each carrying both ends' values and (when observed) each
end's live Port.stp_mode as corroborating evidence -- never a gate. WARNING
always (a link-level degradation, not an outage); confidence follows the
LINK's own tie confidence via the same _tie_confidence used by .blocking_risk
(HIGH two-sided, MEDIUM below). A disagreement already identical in the
baseline, merely touched by some OTHER knob changing on one of the ports, is
INFO context instead. A token end is excluded from the comparison entirely
(tokens never produce a mismatch claim -- see .policy_change). Per-link
findings COEXIST with per-port findings and never suppress the .policy_change
floor -- only .blocking_risk/.root_protect_risk (port-level, WARNING-or-above)
do that; an INFO .link_mismatch in particular must never be mistaken for
floor satisfaction (spec P2 round 2)."""

from __future__ import annotations

import dataclasses

import networkx as nx

from digital_twin.checks.base import (
    CheckContext,
    CheckResult,
    Coverage,
    CoverageState,
    status_from_findings,
)
from digital_twin.checks.wired.stp_root import _root_of
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
from digital_twin.ir.entities import Client, DeviceRole, Port, PortMode, StpMode, StpPolicy
from digital_twin.ir.indexes import clients_by_ap, clients_by_port, node_for, vc_root_map
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
_UNPROVABLE_ELECTION = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the elected root is not known at HIGH confidence (a default-assumed "
             "priority or an uninterpretable priority is present in the component)",),
)

# .link_mismatch only ever compares these two knobs: stp_required and
# stp_no_root_port are per-port BPDU-handling/root-eligibility switches with
# no "the two ends must agree" semantics (a root-protect port and its peer are
# expected to differ). use_vstp/stp_p2p are link-level protocol/handshake
# settings where the two ends disagreeing is itself the anomaly.
_LINK_MISMATCH_KNOBS = ("use_vstp", "stp_p2p")


def _changed_knobs(old: StpPolicy | None, new: StpPolicy | None) -> list[str]:
    o, n = old or StpPolicy(), new or StpPolicy()
    return [
        f.name for f in dataclasses.fields(StpPolicy)
        if getattr(o, f.name) != getattr(n, f.name)
    ]


def _is_unresolved(value: object) -> bool:
    return isinstance(value, str) and value.startswith("unresolved:")


def _effective_knob(policy: StpPolicy | None, knob: str) -> bool | str:
    """A port's effective value for one StpPolicy knob; default False when
    the port carries no StpPolicy at all (Port.stp_policy is None)."""
    return getattr(policy, knob) if policy is not None else False


# --- .blocking_risk peer classification --------------------------------------
#
# Cloned (small idiom, not imported) from checks/wired/admin_disable.py:
# _ap_ports-style AP ties and _nonap_peer_links(base_ir) returning a
# port -> Link map so classification can read the LINK's own tie confidence.
#
# Each per-IR helper is unioned across baseline AND proposed (see _union_ties
# below) rather than read from a single state, per sibling-check convention
# (admin_disable.py, poe_disconnect.py, l2_isolation.py all read baseline) —
# defense-in-depth against baseline/proposed ingestion divergence. Today the
# two states never actually differ for these delta shapes (apply_plan has no
# device/link-add path), so the union is a no-op in practice; it exists so a
# future ingestion path that DOES let baseline and proposed disagree on peer
# evidence can't silently under-escalate an ERROR to the floor.

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


def _union_ties[Peer](
    base: dict[str, tuple[Peer, Link]], prop: dict[str, tuple[Peer, Link]]
) -> dict[str, tuple[Peer, Link]]:
    """Union two port -> (peer, Link) maps (AP or bpdu_filter peers). A port
    tied in either state qualifies; if BOTH provide a tie, keep the one with
    the higher tie confidence (HIGH beats one-sided) so the finding reports
    the strongest evidence actually available."""
    out = dict(base)
    for pid, (peer, lk) in prop.items():
        if pid not in out:
            out[pid] = (peer, lk)
            continue
        _, base_lk = out[pid]
        if _tie_confidence(lk).level is ConfidenceLevel.HIGH and (
            _tie_confidence(base_lk).level is not ConfidenceLevel.HIGH
        ):
            out[pid] = (peer, lk)
    return out


def _union_wired_clients(
    base: dict[str, list[Client]], prop: dict[str, list[Client]]
) -> dict[str, list[Client]]:
    """Union two port -> observed-wired-clients maps, de-duplicated by MAC —
    a client observed in EITHER state counts as evidence on that port."""
    out: dict[str, list[Client]] = {}
    for pid in base.keys() | prop.keys():
        by_mac = {c.mac: c for c in base.get(pid, ())}
        by_mac.update({c.mac: c for c in prop.get(pid, ())})
        out[pid] = list(by_mac.values())
    return out


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
    if device_id in ir.devices and ir.devices[device_id].role is DeviceRole.AP:
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
        # peer/occupancy evidence unions baseline AND proposed (see the
        # _union_ties docstring above): a port ADD (whose peer only exists in
        # `prop_ir`) still classifies correctly from the proposed side, while
        # a port whose peer evidence only exists in `base_ir` (defense against
        # future ingestion divergence between the two states) is not missed.
        ap_peers = _union_ties(_ap_peer_links(base_ir), _ap_peer_links(prop_ir))
        bpdu_filter_peers = _union_ties(
            _bpdu_filter_peer_links(base_ir), _bpdu_filter_peer_links(prop_ir)
        )
        wired = _union_wired_clients(clients_by_port(base_ir), clients_by_port(prop_ir))
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

            risk_findings: list[Finding] = []
            if "stp_required" in knobs and new_policy.stp_required is True:
                # False/absent -> True only; an unresolved: token never reaches
                # here (it is filtered into unresolved_knobs above, and a token
                # is never `is True`).
                blocking_finding, note = self._blocking_risk(
                    ctx, pid, ap_peers, bpdu_filter_peers, wired
                )
                if blocking_finding is not None:
                    risk_findings.append(blocking_finding)
                if note is not None:
                    notes.append(note)

            if "stp_no_root_port" in knobs and new_policy.stp_no_root_port is True:
                # False/absent -> True only; same unresolved-token exclusion.
                root_protect_finding, note = self._root_protect_risk(ctx, pid)
                if root_protect_finding is not None:
                    risk_findings.append(root_protect_finding)
                if note is not None:
                    notes.append(note)

            if risk_findings:
                findings.extend(risk_findings)
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
        findings.extend(self._link_mismatch(ctx))
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
        prop_ir, base_ir = ctx.proposed.ir, ctx.baseline.ir
        port_ref = ObjectRef("port", pid)

        def occupants_ir(device_id: str) -> IR:
            # occupant counts read whichever state actually models the peer
            # device — proposed preferred (the union's baseline-only ties are
            # the rare/defensive case), falling back to baseline so a
            # peer that exists only there doesn't KeyError/zero out.
            return prop_ir if device_id in prop_ir.devices else base_ir

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
                        "occupants_behind": _occupants_behind(occupants_ir(ap_id), ap_id),
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
                        "occupants_behind": _occupants_behind(
                            occupants_ir(peer_port.device_id), peer_port.device_id
                        ),
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

    def _root_protect_risk(
        self, ctx: CheckContext, pid: str,
    ) -> tuple[Finding | None, str | None]:
        """Classify a port whose stp_no_root_port just went True: is it the
        device's ONLY graph path to the component's elected root? Election
        reuses stp_root.py:_root_of over the PROPOSED component (the state
        this port's new policy applies to). ERROR/HIGH requires the root
        known at HIGH confidence; only-path with a lower-confidence election
        degrades to WARNING + note; a redundant path or the device itself
        being the root is no risk code (falls through to the .policy_change
        floor, no note needed -- there is nothing unprovable to flag)."""
        prop_ir = ctx.proposed.ir
        port = prop_ir.ports[pid]
        vc_root = vc_root_map(prop_ir)
        device_node = node_for(vc_root, port.device_id)

        graph = ctx.proposed.l2_graph()
        if device_node not in graph:
            return None, None  # nothing to elect a root over

        component = frozenset(nx.node_connected_component(graph, device_node))
        elected = _root_of(prop_ir, component)
        if elected is None or not isinstance(elected, tuple):
            # no election to disturb (fewer than two switches), or the
            # election itself abstained (uninterpretable priority in the
            # component) -- root-protect risk cannot even be framed without
            # a candidate root, so this falls through to the floor.
            return None, None
        root_id, any_default_assumed = elected
        root_node = node_for(vc_root, root_id)
        if root_node == device_node:
            # the device IS the elected root -- no root to lose the path to.
            return None, None

        # only-path mechanics: drop this port's edge(s) on a working copy of
        # the proposed component graph; if the root becomes unreachable from
        # the device, this port was the only path.
        subgraph = graph.subgraph(component).copy()
        edges_to_drop = [
            (u, v, k)
            for u, v, k, data in subgraph.edges(keys=True, data="data")
            if pid in data.member_ports
        ]
        subgraph.remove_edges_from(edges_to_drop)
        only_path = not nx.has_path(subgraph, device_node, root_node)
        if not only_path:
            # a redundant path survives -- the .policy_change floor covers
            # this port; no risk code, nothing unprovable to note either.
            return None, None

        port_ref = ObjectRef("port", pid)
        election_high = not any_default_assumed
        if election_high:
            severity = Severity.ERROR
            confidence = _HIGH
            confidence_label = "high"
            reason = (
                "only graph path to the elected root, root known at HIGH "
                "confidence — the port can never accept its root port and "
                "the device blocks toward the root"
            )
            note = None
        else:
            severity = Severity.WARNING
            confidence = _UNPROVABLE_ELECTION
            confidence_label = "unprovable"
            reason = (
                "only graph path to a candidate root, but the root election "
                "itself is not provable at HIGH confidence — never assert "
                "ERROR on a guessed root"
            )
            note = (
                f"port {pid}: root election not provable — root-protect risk "
                f"assessed at reduced confidence"
            )
        return (
            Finding(
                source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code=f"{self.id}.root_protect_risk", severity=severity, confidence=confidence,
                message=f"port {pid}: stp_no_root_port enabled — this is the device's "
                        f"only path to the elected root {root_id}; the port can never "
                        f"become root port and the device may black-hole toward the root",
                affected_entities=(pid, root_id), subject=port_ref,
                evidence={
                    "port": pid, "elected_root": root_id, "only_path": True,
                    "election_confidence": confidence_label,
                    "severity_reason": reason,
                },
                caused_by=ctx.delta_index.causes("port", [pid]),
            ),
            note,
        )

    def _link_mismatch(self, ctx: CheckContext) -> list[Finding]:
        """For every MODELED link present in the proposed IR, compare each
        end's EFFECTIVE use_vstp/stp_p2p value (tokens excluded — a token
        never produces a mismatch claim, it stays on the .policy_change floor
        path). A disagreement introduced or changed by the delta on either
        end -> one WARNING finding per (link, knob), confidence = the link's
        own tie confidence (HIGH two-sided, MEDIUM below, same _tie_confidence
        as .blocking_risk). A disagreement that already existed identically in
        the baseline, merely touched by some other knob changing on one of the
        ports -> INFO context. These findings COEXIST with port-level findings
        and never suppress the .policy_change floor for the same port."""
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        out: list[Finding] = []
        for lnk in prop_ir.links:
            pa = prop_ir.ports.get(lnk.a_port)
            pb = prop_ir.ports.get(lnk.b_port)
            if pa is None or pb is None:
                continue  # not a fully modeled link
            base_pa = base_ir.ports.get(lnk.a_port)
            base_pb = base_ir.ports.get(lnk.b_port)
            for knob in _LINK_MISMATCH_KNOBS:
                a_new, b_new = _effective_knob(pa.stp_policy, knob), _effective_knob(
                    pb.stp_policy, knob
                )
                if _is_unresolved(a_new) or _is_unresolved(b_new):
                    continue  # token end -> floor path, never a mismatch claim
                if a_new == b_new:
                    continue  # both ends agree in the proposed state
                changed_here = (
                    base_pa is None or base_pb is None
                    or _effective_knob(base_pa.stp_policy, knob) != a_new
                    or _effective_knob(base_pb.stp_policy, knob) != b_new
                )
                conf = _tie_confidence(lnk)
                observed_modes = {
                    p.id: p.stp_mode
                    for p in (pa, pb)
                    if p.stp_mode is not StpMode.NONE
                }
                evidence = {
                    "link": lnk.id, "knob": knob,
                    "values": {pa.id: a_new, pb.id: b_new},
                    "observed_modes": observed_modes,
                }
                if changed_here:
                    out.append(
                        Finding(
                            source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                            code=f"{self.id}.link_mismatch", severity=Severity.WARNING,
                            confidence=conf,
                            message=f"link {pa.id} <-> {pb.id}: {knob} disagreement "
                                    f"({a_new} vs {b_new}) introduced or changed by "
                                    f"this delta",
                            affected_entities=(pa.id, pb.id), subject=ObjectRef("link", lnk.id),
                            evidence=evidence,
                            caused_by=tuple(
                                c for c in (
                                    ctx.delta_index.cause("port", pa.id),
                                    ctx.delta_index.cause("port", pb.id),
                                    ctx.delta_index.cause("link", lnk.id),
                                ) if c is not None
                            ),
                        )
                    )
                else:
                    out.append(
                        Finding(
                            source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                            code=f"{self.id}.link_mismatch", severity=Severity.INFO,
                            confidence=conf,
                            message=f"link {pa.id} <-> {pb.id}: pre-existing {knob} "
                                    f"disagreement ({a_new} vs {b_new}), unchanged by "
                                    f"this delta (context)",
                            affected_entities=(pa.id, pb.id), subject=ObjectRef("link", lnk.id),
                            evidence=evidence,
                            caused_by=(),
                        )
                    )
        return out
