"""wired.l3.bgp_adjacency — a switch/gateway BGP peering removed/disabled/added or
a retained peering's session-breaking attribute changed (GS28).

The twin has no RIB: structural codes are config-certain but reachability-unconfirmed
-> WARNING/REVIEW. Live telemetry (baseline-established peers) escalates a session-
breaking change to ERROR/UNSAFE (Task 8). Identity is (device, neighbor_ip); an
'active peering' = present AND not disabled. Codes:
- .peering_removed / .peering_disabled: active in baseline, gone/disabled in proposed.
- .peering_added: not-active in baseline, active in proposed (no escalation).
- .as_changed / .session_type_changed / .transport_changed: retained active peering
  whose local_as/neighbor_as / type / via changed.
Ambiguous, unresolved-IP, and templated-token (AS/type/via/admin-state) cases become
relevance-scoped PARTIAL coverage notes, never confident findings (never false-SAFE)."""

from __future__ import annotations

from typing import Any

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Cause,
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import (
    BgpNeighbor,
    BgpPeer,
    Capability,
    Confidence,
    ConfidenceLevel,
    DeviceRole,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=(
        "BGP reachability is not computed — a redundant peering or redistribution "
        "the twin does not model may still carry these routes",
    ),
)

# Session-breaking sub-codes: the structural change is certain to drop the session.
# .peering_added is deliberately excluded — a new peering cannot be session-BREAKING
# (it doesn't break an existing session), so telemetry escalation never applies to it.
_SESSION_BREAKING: frozenset[str] = frozenset({
    "peering_removed",
    "peering_disabled",
    "as_changed",
    "session_type_changed",
    "transport_changed",
})


def is_established(n: BgpNeighbor) -> bool:
    """Return True if the BgpNeighbor was established in the observed baseline."""
    return n.up is True or n.state.strip().lower() == "established"


def _active(p: BgpPeer) -> bool:
    return not p.disabled


def _by_key(ir: IR) -> dict[tuple[str, str], BgpPeer]:
    # identity (device, neighbor_ip); ingest guarantees one row per key (ambiguous flag set)
    return {(p.device_id, p.neighbor_ip): p for p in ir.bgp_peers}


class BgpAdjacencyCheck:
    id = "wired.l3.bgp_adjacency"
    title = "BGP peering withdrawn or session-breaking change"
    domain = "wired.l3"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("bgp_peer")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        base, prop = _by_key(base_ir), _by_key(prop_ir)
        findings: list[Finding] = []
        notes: list[str] = []

        def _caused_by(p: BgpPeer) -> tuple[Cause, ...]:
            return ctx.delta_index.causes("bgp_peer", [p.id])

        def _mk(code: str, p: BgpPeer, message: str, extra: dict[str, Any]) -> Finding:
            return Finding(
                source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code=f"{self.id}.{code}", subject=ObjectRef("device", p.device_id),
                severity=Severity.WARNING, confidence=_UNVERIFIED, message=message,
                affected_entities=(p.neighbor_ip,),
                evidence={"device": p.device_id, "neighbor_ip": p.neighbor_ip, **extra},
                caused_by=_caused_by(p),
            )

        def _note_if_fuzzy(did: str, nip: str, b: BgpPeer | None, p: BgpPeer | None) -> bool:
            """Emit a coverage note if EITHER present side is ambiguous / unresolved-IP /
            templated-admin-state, and return True so the caller skips confident compare,
            ACTIVE-STATE classification, AND telemetry escalation on this peer. Checking
            both sides is load-bearing: a baseline-ambiguous (or templated-disabled) peer
            that becomes clean — or vice-versa — must still abstain (spec §2). The
            admin-state check MUST run before add/remove classification: `disabled` parses
            to False when templated, so without this guard `_active()` would wrongly read a
            templated-disabled peer as active and emit a confident .peering_added/_removed."""
            if (b is not None and b.ambiguous) or (p is not None and p.ambiguous):
                notes.append(
                    f"BGP peer {nip} on {did} is claimed by multiple sessions with "
                    "differing attributes — change detection skipped"
                )
                return True
            if (b is not None and b.unresolved) or (p is not None and p.unresolved):
                notes.append(
                    f"BGP peer key {nip!r} on {did} is not a literal IP — peering "
                    "change impact cannot be verified"
                )
                return True
            if (b is not None and b.disabled_unresolved is not None) or \
                    (p is not None and p.disabled_unresolved is not None):
                notes.append(
                    f"BGP peer {nip} on {did} has a templated/non-boolean admin "
                    "state — active-ness is unknown, change detection skipped"
                )
                return True
            return False

        # Relevance scope: process ONLY peers whose own bgp_peer entity was delta-touched
        # (added / removed / modified). A STABLE pre-existing fuzzy peer (ambiguous /
        # unresolved / templated-disabled, unchanged) must NEVER emit a note and floor an
        # unrelated BGP change to REVIEW (spec §2: notes are relevance-scoped). Confident
        # findings are already implicitly scoped — an unchanged peer is not in the diff and
        # its base==prop attributes produce no add/remove/change — but the fuzzy notes are
        # presence-based, so they need this explicit guard.
        touched_ids = {
            r.id
            for r in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if r.kind == "bgp_peer"
        }
        all_keys = sorted(set(base) | set(prop))
        for key in all_keys:
            b, p = base.get(key), prop.get(key)
            did, nip = key
            cur = b if b is not None else p
            assert cur is not None
            if cur.id not in touched_ids:
                continue  # stable peer, not part of this change -> no compare, no note
            if _note_if_fuzzy(did, nip, b, p):
                continue
            b_active = b is not None and _active(b)
            p_active = p is not None and _active(p)

            # removed / disabled (active -> not active)
            if b_active and not p_active:
                assert b is not None  # b_active => b is not None
                if p is None:
                    findings.append(_mk(
                        "peering_removed", b,
                        f"BGP peering to {nip} on {did} is removed — the session is "
                        "withdrawn; routes learned/advertised over it are lost",
                        {},
                    ))
                else:
                    findings.append(_mk(
                        "peering_disabled", p,
                        f"BGP peering to {nip} on {did} is administratively disabled — "
                        "the session goes down",
                        {},
                    ))
                continue
            # added / enabled (not active -> active)
            if not b_active and p_active:
                assert p is not None
                # require literal identity + resolved ASN (BOTH local and neighbor) for a
                # confident add — else a coverage note, never a confident .peering_added
                if p.neighbor_as_unresolved is not None or p.local_as_unresolved is not None:
                    notes.append(
                        f"BGP peer {nip} on {did} added with a templated AS "
                        "— new-peering details unverifiable"
                    )
                    continue
                findings.append(_mk(
                    "peering_added", p,
                    f"BGP peering to {nip} on {did} is newly added — a new session shifts "
                    "advertised/learned routes; review intended scope",
                    {},
                ))
                continue
            # retained active peering: compare session-breaking attributes
            if b_active and p_active:
                assert b is not None and p is not None
                # AS (with unresolved-token guard)
                if b.local_as_unresolved != p.local_as_unresolved or \
                        b.neighbor_as_unresolved != p.neighbor_as_unresolved:
                    notes.append(
                        f"BGP peer {nip} on {did} has a templated AS on one side "
                        "— AS-change impact unverifiable"
                    )
                else:
                    local_changed = b.local_as != p.local_as
                    neighbor_changed = b.neighbor_as != p.neighbor_as
                    if local_changed or neighbor_changed:
                        findings.append(_mk(
                            "as_changed", p,
                            f"BGP peering to {nip} on {did} changed AS (local "
                            f"{b.local_as}->{p.local_as}, neighbor {b.neighbor_as}->"
                            f"{p.neighbor_as}) — the session must re-establish",
                            {
                                "local_as_changed": local_changed,
                                "neighbor_as_changed": neighbor_changed,
                                "base_local_as": b.local_as,
                                "proposed_local_as": p.local_as,
                                "base_neighbor_as": b.neighbor_as,
                                "proposed_neighbor_as": p.neighbor_as,
                            },
                        ))
                # session type
                if b.session_type_unresolved != p.session_type_unresolved:
                    notes.append(
                        f"BGP peer {nip} on {did} has a templated session type on "
                        "one side — type-change impact unverifiable"
                    )
                elif b.session_type != p.session_type:
                    findings.append(_mk(
                        "session_type_changed", p,
                        f"BGP peering to {nip} on {did} changed type {b.session_type}->"
                        f"{p.session_type} (iBGP/eBGP) — the session must re-establish",
                        {"base_type": b.session_type, "proposed_type": p.session_type},
                    ))
                # transport (gateway via) — role-gated: switches are implicitly LAN and
                # have no transport dimension; never rely on field-gate invariants in a
                # pure check (p.role == b.role, same device).
                if p.role is DeviceRole.GATEWAY:
                    if b.via_unresolved != p.via_unresolved:
                        notes.append(
                            f"BGP peer {nip} on {did} has a templated transport on "
                            "one side — transport-change impact unverifiable"
                        )
                    elif b.via != p.via:
                        findings.append(_mk(
                            "transport_changed", p,
                            f"BGP peering to {nip} on {did} changed transport "
                            f"{b.via}->{p.via} — the session path changed",
                            {"base_via": b.via, "proposed_via": p.via},
                        ))
                # (admin-state-unresolved is handled UP FRONT by _note_if_fuzzy, which
                # abstains before active-state classification — so a retained peer that
                # reaches here has resolved admin-state on both sides.)

        # Telemetry escalation (escalate-only; BASELINE telemetry). The structural
        # findings ARE the breaks; baseline telemetry confirms which were live.
        telemetry_known = IRCapability.BGP_TELEMETRY in base_ir.capabilities
        has_unparsed = base_ir.bgp_telemetry_unparsed_count > 0
        session_breaking_codes = frozenset(f"{self.id}.{c}" for c in _SESSION_BREAKING)
        has_session_breaking = any(f.code in session_breaking_codes for f in findings)

        if telemetry_known:
            established = {
                (n.device_id, n.peer_ip): n
                for n in base_ir.bgp_neighbors
                if is_established(n)
            }
            for i, f in enumerate(findings):
                if f.code not in session_breaking_codes:
                    continue
                ev = f.evidence or {}
                dev = ev.get("device")
                nip_key = ev.get("neighbor_ip")
                if not isinstance(dev, str) or not isinstance(nip_key, str):
                    continue
                n = established.get((dev, nip_key))
                if n is None:
                    continue
                findings[i] = Finding(
                    source=f.source, category=f.category, code=f.code, subject=f.subject,
                    severity=Severity.ERROR, confidence=_HIGH,
                    message=(
                        f"{f.message} | telemetry: this peer was ESTABLISHED in baseline "
                        "— this change is session-breaking, so the peering would drop"
                    ),
                    affected_entities=f.affected_entities,
                    evidence={
                        **ev,
                        "broken_peers": [n.peer_ip],
                        "baseline_state": n.state,
                        "baseline_neighbor_as": n.neighbor_as,
                        "vrf": n.vrf,
                    },
                    caused_by=f.caused_by,
                )
            # established live peer with no config BgpPeer, on a delta-touched device -> note
            touched_devices = {
                r.id.split(":")[0]
                for r in (
                    *ctx.diff.added,
                    *ctx.diff.removed,
                    *(m.ref for m in ctx.diff.modified),
                )
                if r.kind == "bgp_peer"
            }
            config_keys = set(base) | set(prop)
            for (did, pip), _n in established.items():
                if did in touched_devices and (did, pip) not in config_keys:
                    notes.append(
                        f"BGP peer {pip} on {did} is established in telemetry but "
                        "not found in the modeled config — the twin is blind for it"
                    )

        # telemetry-blind note: only when a session-breaking finding exists
        if (not telemetry_known or has_unparsed) and has_session_breaking:
            notes.append(
                "BGP neighbor telemetry unavailable/partial — confirmed-break "
                "detection is blind for the changed peering(s)"
            )

        return self._finish(findings, notes)

    def _finish(self, findings: list[Finding], notes: list[str]) -> CheckResult:
        worst = Status.PASS
        for f in findings:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id, status=worst, findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(min_confidence(*(f.confidence for f in findings)) if findings else _HIGH),
            reasoning="compared per-(device, neighbor_ip) BGP peerings, baseline vs proposed",
        )
