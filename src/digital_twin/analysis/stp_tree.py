"""Stable-state STP tree prediction (Spec-4). Pure — no I/O, no findings.

THE INVARIANT: prediction alone never earns SAFE; every future verdict-facing
consumer must call analysis/stp_agreement.compare_to_observed and cap
confidence on component-level disagreement.
"""
from __future__ import annotations

from digital_twin.ir.entities import DeviceRole
from digital_twin.ir.model import IR

DEFAULT_PRIORITY = 32768
ABSTAIN = "abstain"


def root_of(ir: IR, component: frozenset[str]) -> tuple[str, bool] | str | None:
    """(root device id, any-default-assumed) for the component's switches —
    None when fewer than two switches (no election to disturb), ABSTAIN when
    an uninterpretable priority makes the election unpredictable (the caller
    must surface that as PARTIAL coverage, never a clean pass)."""
    switches = [d for d in component if ir.devices[d].role is DeviceRole.SWITCH]
    if len(switches) < 2:
        return None
    if any(ir.devices[d].stp_priority_invalid for d in switches):
        return ABSTAIN
    assumed = any(ir.devices[d].stp_priority is None for d in switches)

    def election_key(d: str) -> tuple[int, str]:
        prio = ir.devices[d].stp_priority
        # explicit `is None`: 0 is a VALID priority — the strongest one
        return (DEFAULT_PRIORITY if prio is None else prio, d)

    return min(switches, key=election_key), assumed
