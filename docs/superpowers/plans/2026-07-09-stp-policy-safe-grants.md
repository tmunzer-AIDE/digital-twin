# STP Policy SAFE Grants (Spec-6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let provably-inert `stp_no_root_port`/`stp_required` changes earn SAFE in `wired.stp.policy`, under a telemetry-validated license, replacing the blanket REVIEW floor only where live agreement proves the stable state unchanged.

**Architecture:** A new pure pair-aware `analysis/stp_inertness.py` (`StpInertness.decide(pid, knob, old, new) -> InertnessDecision`) evaluates a 4-clause license (baseline existence; port row `matched`; component `agreement_clean`; identical HIGH baseline/proposed prediction) then a knob-specific rule. `wired.stp.policy` consumes it: all-knobs-inert ports emit a new INFO `.inert_change` instead of the `.policy_change` WARNING floor; risk codes always win; a WARNING+ naming the port suppresses the grant back to the floor. The baseline `StpAgreementReport` becomes a shared `CheckContext.stp_agreement` memo consumed by both `StpReachability` and `StpInertness`.

**Tech Stack:** Python 3.14, dataclasses, existing Spec-4/5 analysis machinery. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-09-stp-policy-safe-grants-design.md` (read it for rationale; this plan is self-contained for execution).

## Global Constraints

- **CARDINAL RULE: never false-SAFE.** Every grant path requires the full license + a knob rule; anything unprovable lands on the unchanged REVIEW floor. This slice may only REMOVE a WARNING on provably-inert changes — never demote a risk code, a mismatch, or an unproven port.
- **THE INVARIANT (Spec-4):** prediction alone never earns SAFE — grants require the port's own baseline agreement row `matched` AND its component `agreement_clean` AND identical HIGH baseline/proposed predictions.
- **SAFE-eligible knobs: `stp_no_root_port`, `stp_required` ONLY.** `stp_p2p`/`use_vstp` always floor. Unresolved-token values never grant.
- **The SAFE claim is stable-state-only** — "no current dataplane change", never "no future protection posture change". This wording goes in the module docstring and finding `severity_reason`.
- **Amended floor invariant:** every port with a changed `stp_policy` yields ≥1 delta-caused finding — WARNING-or-above OR a fully-licensed `.inert_change` INFO. Never zero. Link-level findings never satisfy (nor replace) the per-port floor.
- New finding code: `wired.stp.policy.inert_change` (INFO/HIGH). **No new check id.**
- All commands run from the worktree root: `/Users/tmunzer/4_dev/digital-twin/.claude/worktrees/stp-safe-grants`. All git commands use `git -C /Users/tmunzer/4_dev/digital-twin/.claude/worktrees/stp-safe-grants` (or run after `cd` to it) on branch `feat/stp-safe-grants` — NEVER commit in the main checkout.
- Full gate before each commit: `uv run pytest tests -q && uv run ruff check . && uv run mypy src` (pytest -q prints no summary line; all dots = pass).
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- INFO findings are excluded from `status_from_findings` and the check's confidence roll-up (existing convention — do not re-implement).
- `analysis/` must never import from `checks/` (layering). Small peer-scan idiom duplication with `stp_policy.py` is the family's deliberate "cloned idiom" convention.

## File Structure

- `src/digital_twin/checks/base.py` — add memoized `CheckContext.stp_agreement` + `stp_inertness`; thread agreement into `stp_reachability`.
- `src/digital_twin/analysis/stp_reachability.py` — optional `agreement` ctor param.
- `src/digital_twin/analysis/stp_inertness.py` — NEW: `InertnessDecision`, `StpInertness` (license + knob rules).
- `src/digital_twin/checks/wired/stp_policy.py` — grant/floor emission, suppression, provisional coverage note.
- `tests/analysis/test_stp_inertness.py` — NEW: license + knob-rule unit tests, fully-observed fixture helpers (exported for T5/T6).
- `tests/analysis/test_stp_reachability.py`, `tests/checks/test_base_context.py` (NEW) — T1 threading tests.
- `tests/checks/test_stp_policy.py` — check-level grant tests + amended never-SAFE guard.
- `tests/golden/test_stp_policy_safe_scenarios.py` — NEW: e2e SAFE/REVIEW goldens.
- `docs/ROADMAP.md`, `README.md`, spec Status — T6.

---

### Task 1: Shared `CheckContext.stp_agreement` memo, threaded into `StpReachability`

**Files:**
- Modify: `src/digital_twin/checks/base.py`
- Modify: `src/digital_twin/analysis/stp_reachability.py` (lines 43–50)
- Test: `tests/checks/test_base_context.py` (create)

**Interfaces:**
- Consumes: `compare_to_observed(prediction, ir) -> StpAgreementReport` from `digital_twin.analysis.stp_agreement`; `AnalysisContext.stp_tree()`.
- Produces: `CheckContext.stp_agreement -> StpAgreementReport` (memoized property, baseline-side); `StpReachability.__init__(baseline, proposed, agreement: StpAgreementReport | None = None)`. Task 2 consumes both.

- [ ] **Step 1: Write the failing tests**

Create `tests/checks/test_base_context.py`:

```python
"""CheckContext.stp_agreement (Spec-6 Task 1): ONE shared baseline
StpAgreementReport memo, passed into StpReachability (and later StpInertness)
so cache behavior lives in exactly one place."""
from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import compare_to_observed
from digital_twin.checks.base import CheckContext
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from tests.factories import link, make_port, sw


def _ctx() -> CheckContext:
    b = IRBuilder().add_device(sw("aa01", stp_priority=0)).add_device(sw("bb02"))
    b.add_port(make_port("aa01", "ge-0/0/1", observed_speed="1g"))
    b.add_port(make_port("bb02", "ge-0/0/1", observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1"))
    ir = b.with_capability(IRCapability.WIRED_L2).build()
    return CheckContext(
        baseline=AnalysisContext(ir), proposed=AnalysisContext(ir), diff=diff_ir(ir, ir)
    )


def test_stp_agreement_is_memoized_same_object():
    ctx = _ctx()
    assert ctx.stp_agreement is ctx.stp_agreement


def test_stp_agreement_equals_direct_comparator_run():
    ctx = _ctx()
    direct = compare_to_observed(ctx.baseline.stp_tree(), ctx.baseline.ir)
    assert ctx.stp_agreement == direct


def test_stp_reachability_receives_the_shared_report():
    ctx = _ctx()
    report = ctx.stp_agreement
    # the memoized StpReachability must hold the SAME object, not a recompute
    assert ctx.stp_reachability._base_agreements is report.components
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_base_context.py -v`
Expected: FAIL — `AttributeError: 'CheckContext' object has no attribute 'stp_agreement'`.

- [ ] **Step 3: Implement**

In `src/digital_twin/analysis/stp_reachability.py`, change `__init__` (currently lines 43–50):

```python
    def __init__(
        self,
        baseline: AnalysisContext,
        proposed: AnalysisContext,
        agreement: StpAgreementReport | None = None,
    ) -> None:
        self._baseline = baseline
        self._proposed = proposed
        # baseline licence, computed once (or shared via CheckContext.stp_agreement)
        self._base_pred = _predictions(baseline)
        self._prop_pred = _predictions(proposed)
        report = (
            agreement
            if agreement is not None
            else compare_to_observed(baseline.stp_tree(), baseline.ir)
        )
        self._base_agreements: tuple[ComponentAgreement, ...] = report.components
        self._base_comp_keys: dict[int, set[frozenset[str]]] = {}
        self._hard_cache: dict[tuple[str, int], tuple[VlanComponent, ...]] = {}
```

and extend the existing import (line 14) to include the report type:

```python
from digital_twin.analysis.stp_agreement import (
    ComponentAgreement,
    StpAgreementReport,
    compare_to_observed,
)
```

In `src/digital_twin/checks/base.py`, add below the `stp_reachability` property (keep the TYPE_CHECKING import block, extending it):

```python
if TYPE_CHECKING:
    from digital_twin.analysis.stp_agreement import StpAgreementReport
    from digital_twin.analysis.stp_reachability import StpReachability
```

```python
    @property
    def stp_agreement(self) -> StpAgreementReport:
        """Baseline-side StpAgreementReport, computed once per CheckContext and
        shared by every STP consumer (StpReachability, StpInertness) so cache
        behavior lives in exactly one place (Spec-6)."""
        cached = getattr(self, "_stp_agreement", None)
        if cached is None:
            from digital_twin.analysis.stp_agreement import compare_to_observed

            cached = compare_to_observed(self.baseline.stp_tree(), self.baseline.ir)
            object.__setattr__(self, "_stp_agreement", cached)
        return cached
```

and change the `stp_reachability` property body to pass it:

```python
            cached = StpReachability(self.baseline, self.proposed, agreement=self.stp_agreement)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/checks/test_base_context.py tests/analysis/test_stp_reachability.py tests/checks/test_l2_blackhole.py -q`
Expected: all pass (the default-`None` path keeps every existing StpReachability test byte-identical).

- [ ] **Step 5: Full gate, then commit**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

```bash
git add src/digital_twin/checks/base.py src/digital_twin/analysis/stp_reachability.py tests/checks/test_base_context.py
git commit -m "feat(checks): shared CheckContext.stp_agreement memo threaded into StpReachability

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `StpInertness` — contract + the 4-clause license

**Files:**
- Create: `src/digital_twin/analysis/stp_inertness.py`
- Test: `tests/analysis/test_stp_inertness.py` (create)

**Interfaces:**
- Consumes: `StpAgreementReport`/`ComponentAgreement`/`PortAgreement` and `compare_to_observed` from `digital_twin.analysis.stp_agreement`; `PortPrediction` from `digital_twin.analysis.stp_tree`; `AnalysisContext.stp_tree()`; `ConfidenceLevel` from `digital_twin.ir.confidence`.
- Produces (Tasks 3–6 rely on these EXACT names):
  - `InertnessDecision(inert: bool, reasons: tuple[str, ...], evidence: dict[str, object])`
  - `StpInertness(baseline: AnalysisContext, proposed: AnalysisContext, agreement: StpAgreementReport | None = None)`
  - `StpInertness.decide(pid: str, knob: str, old_value: bool | str, new_value: bool | str) -> InertnessDecision`
  - Test helpers exported from `tests/analysis/test_stp_inertness.py`: `_EXPECTED_ROLES: dict[str, tuple[str, str]]`, `_fully_observed(ir)`, `_with_policy(ir, pid, **knobs)`.

The license clauses (spec, verbatim intent):
(a) `pid` exists in the baseline IR; (b) the port's own baseline agreement row bucket is `matched`; (c) the port's baseline `ComponentAgreement.agreement_clean` is True; (d) baseline and proposed `PortPrediction` exist for `pid`, identical in `(role, state)`, both `ConfidenceLevel.HIGH`.

- [ ] **Step 1: Write the failing tests**

Create `tests/analysis/test_stp_inertness.py`:

```python
"""StpInertness (Spec-6 Tasks 2-3): licensed per-knob inertness decisions.

Reuses the Spec-5 bridge-id topology (root aa01 prio 0; leaf bb02 prio 4096;
transits cc03 8192 / dd04 12288; all links 1g two-sided) whose full prediction
is known: every port designated/forwarding except cc03:ge-0/0/1, dd04:ge-0/0/1,
bb02:ge-0/0/1 (root/forwarding) and bb02:ge-0/0/2 (alternate/blocking).
`_fully_observed` stamps observed telemetry AGREEING with all 8 predictions so
the single component is agreement_clean and every row matched."""
from __future__ import annotations

import dataclasses

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_inertness import InertnessDecision, StpInertness
from digital_twin.ir import IRBuilder, IRCapability
from digital_twin.ir.entities import StpPolicy
from tests.analysis.test_stp_reachability import _bridge_id_topology, _set_observed
from tests.factories import sw, make_port, link

# port -> (observed role, observed state) agreeing with the Spec-4 prediction
_EXPECTED_ROLES: dict[str, tuple[str, str]] = {
    "aa01:ge-0/0/1": ("designated", "forwarding"),
    "aa01:ge-0/0/2": ("designated", "forwarding"),
    "cc03:ge-0/0/1": ("root", "forwarding"),
    "cc03:ge-0/0/2": ("designated", "forwarding"),
    "dd04:ge-0/0/1": ("root", "forwarding"),
    "dd04:ge-0/0/2": ("designated", "forwarding"),
    "bb02:ge-0/0/1": ("root", "forwarding"),
    "bb02:ge-0/0/2": ("alternate", "blocking"),
}


def _bridge_ir(*, with_vlan: bool = False):
    b = IRBuilder()
    _bridge_id_topology(b, prune_vlan10=with_vlan, carry_both_paths=with_vlan)
    return b.with_capability(IRCapability.WIRED_L2).build()


def _fully_observed(ir, *, skip: frozenset[str] = frozenset()):
    for pid, (role, state) in _EXPECTED_ROLES.items():
        if pid in skip:
            continue
        ir = _set_observed(ir, pid, role=role, state=state)
    return ir


def _with_policy(ir, pid: str, **knobs):
    port = ir.ports[pid]
    new_port = dataclasses.replace(port, stp_policy=StpPolicy(**knobs))
    new_ports = dict(ir.ports)
    new_ports[pid] = new_port
    return dataclasses.replace(ir, ports=new_ports)


def _with_priority(ir, did: str, prio: int):
    dev = ir.devices[did]
    new_devices = dict(ir.devices)
    new_devices[did] = dataclasses.replace(dev, stp_priority=prio)
    return dataclasses.replace(ir, devices=new_devices)


def _lag_pair_ir():
    """Two switches joined by a 2-member LAG: the Spec-4 engine caps LAG
    member predictions at MEDIUM — the license-(d) confidence fixture."""
    b = IRBuilder().add_device(sw("aa01", stp_priority=0)).add_device(
        sw("bb02", stp_priority=4096)
    )
    for name in ("ge-0/0/1", "ge-0/0/2"):
        b.add_port(make_port("aa01", name, observed_speed="1g"))
        b.add_port(make_port("bb02", name, observed_speed="1g"))
    b.add_link(link("aa01:ge-0/0/1", "bb02:ge-0/0/1", bundle="ae0"))
    b.add_link(link("aa01:ge-0/0/2", "bb02:ge-0/0/2", bundle="ae0"))
    return b.with_capability(IRCapability.WIRED_L2).build()


def _inertness(base_ir, prop_ir) -> StpInertness:
    return StpInertness(AnalysisContext(base_ir), AnalysisContext(prop_ir))


def test_fixture_sanity_all_rows_matched_component_clean():
    # pins the fixture the whole suite rests on: 8 predicted ports, all
    # matched, one clean component — if the engine's prediction ever shifts,
    # THIS test names the drift rather than a license test failing obscurely
    from digital_twin.analysis.stp_agreement import compare_to_observed

    ir = _fully_observed(_bridge_ir())
    actx = AnalysisContext(ir)
    report = compare_to_observed(actx.stp_tree(), actx.ir)
    assert report.matched == 8 and report.mismatched_high == 0
    assert len(report.components) == 1 and report.components[0].agreement_clean


def _decide_root_protect(base_ir, prop_ir, pid="cc03:ge-0/0/2") -> InertnessDecision:
    return _inertness(base_ir, prop_ir).decide(pid, "stp_no_root_port", False, True)


def test_license_a_port_missing_from_baseline_floors():
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("zz99:ge-0/0/1", "stp_no_root_port", False, True)
    assert not d.inert and any("baseline" in r for r in d.reasons)


def test_license_b_unvalidatable_target_row_floors():
    # target port telemetry-dark (no observed role) -> row unvalidatable
    base = _fully_observed(_bridge_ir(), skip=frozenset({"cc03:ge-0/0/2"}))
    d = _decide_root_protect(base, base)
    assert not d.inert and any("matched" in r for r in d.reasons)


def test_license_b_non_tree_access_port_floors_even_if_observed_designated():
    # R1-P1-2: an access port with NO PortPrediction can never be licensed,
    # even when its observed role is designated
    base = _fully_observed(_bridge_ir(with_vlan=True))
    base = _set_observed(base, "bb02:acc", role="designated", state="forwarding")
    d = _decide_root_protect(base, base, pid="bb02:acc")
    assert not d.inert and any("matched" in r for r in d.reasons)


def test_license_c_component_dirty_mismatch_floors_matched_target():
    # adjustment 5: target row matched, but ANOTHER port in the component
    # mismatches -> agreement_clean False -> floor
    base = _fully_observed(_bridge_ir())
    base = _set_observed(base, "bb02:ge-0/0/2", role="root", state="forwarding")
    d = _decide_root_protect(base, base)
    assert not d.inert and any("clean" in r for r in d.reasons)


def test_license_c_component_dirty_bpdu_inconsistent_floors_matched_target():
    base = _fully_observed(_bridge_ir())
    base = _set_observed(
        base, "bb02:ge-0/0/2", role="disabled-bpdu-inconsistent", state="blocking"
    )
    d = _decide_root_protect(base, base)
    assert not d.inert and any("clean" in r for r in d.reasons)


def test_license_d_medium_confidence_lag_position_floors():
    # plan-review P1: clause (d)'s HIGH requirement, isolated — the row IS
    # matched (b holds), the component IS clean (c holds), the position IS
    # identical (same IR both sides), but the LAG cap makes it MEDIUM
    from digital_twin.analysis.stp_agreement import compare_to_observed
    from digital_twin.ir.confidence import ConfidenceLevel

    ir = _lag_pair_ir()
    for pid in ("aa01:ge-0/0/1", "aa01:ge-0/0/2"):
        ir = _set_observed(ir, pid, role="designated", state="forwarding")
    actx = AnalysisContext(ir)
    report = compare_to_observed(actx.stp_tree(), actx.ir)
    rows = {r.port_id: r for r in report.ports}
    assert rows["aa01:ge-0/0/1"].bucket == "matched"  # sanity: (b) holds
    assert rows["aa01:ge-0/0/1"].predicted.confidence is ConfidenceLevel.MEDIUM
    d = _inertness(ir, ir).decide("aa01:ge-0/0/1", "stp_no_root_port", False, True)
    assert not d.inert and any("license (d)" in r for r in d.reasons)


def test_license_d_delta_moving_tree_position_floors():
    # proposed disables the cc03<->bb02 edge -> bb02 re-roots via dd04 ->
    # bb02:ge-0/0/2 flips alternate->root: position changed -> floor
    base = _fully_observed(_bridge_ir())
    prop = base
    for pid in ("cc03:ge-0/0/2", "bb02:ge-0/0/1"):
        port = prop.ports[pid]
        new_ports = dict(prop.ports)
        new_ports[pid] = dataclasses.replace(port, disabled=True)
        prop = dataclasses.replace(prop, ports=new_ports)
    d = _inertness(base, prop).decide("bb02:ge-0/0/2", "stp_no_root_port", False, True)
    assert not d.inert and any("position" in r for r in d.reasons)


def test_non_eligible_knob_floors():
    base = _fully_observed(_bridge_ir())
    for knob in ("stp_p2p", "use_vstp"):
        d = _inertness(base, base).decide("cc03:ge-0/0/2", knob, False, True)
        assert not d.inert and any("eligible" in r for r in d.reasons)


def test_unresolved_token_floors():
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide(
        "cc03:ge-0/0/2", "stp_no_root_port", False, "unresolved:{{rp}}"
    )
    assert not d.inert and any("token" in r for r in d.reasons)


def test_shared_agreement_param_is_used_verbatim():
    from digital_twin.analysis.stp_agreement import compare_to_observed

    base = _fully_observed(_bridge_ir())
    actx = AnalysisContext(base)
    report = compare_to_observed(actx.stp_tree(), actx.ir)
    si = StpInertness(actx, AnalysisContext(base), agreement=report)
    assert si._agreement is report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/test_stp_inertness.py -v`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'digital_twin.analysis.stp_inertness'`.

- [ ] **Step 3: Implement the module (license only; knob rules land in Task 3)**

Create `src/digital_twin/analysis/stp_inertness.py`:

```python
"""STP policy inertness (Spec-6). Pure — no I/O, no findings, no checks import.

Decides whether ONE changed StpPolicy knob on ONE port is PROVABLY INERT in
the current stable state, against the Spec-4 tree prediction validated by
live telemetry. The SAFE claim is STABLE-STATE-ONLY: "no current dataplane
change", never "no future protection posture change" — enabling root-protect
on a designated port deliberately alters future-failure behavior; that is the
operator hardening, not a defect, and it is out of this module's scope.

THE INVARIANT (Spec-4) is discharged by construction: every grant requires
the port's own baseline agreement row `matched` AND its component
`agreement_clean` AND identical HIGH baseline/proposed predictions — a port
the engine does not model (client/AP-facing, unlinked) has no prediction and
no row and can never pass the license. The tree engine reads NONE of the four
StpPolicy knobs, so tree identity is vacuous for a pure policy change: the
license proves trust in the tree POSITION, and the per-knob rule proves the
knob is inert AT that position.
"""
from __future__ import annotations

from dataclasses import dataclass

from digital_twin.analysis.context import AnalysisContext
from digital_twin.analysis.stp_agreement import (
    ComponentAgreement,
    PortAgreement,
    StpAgreementReport,
    compare_to_observed,
)
from digital_twin.analysis.stp_tree import PortPrediction
from digital_twin.ir.confidence import ConfidenceLevel

_ELIGIBLE = frozenset({"stp_no_root_port", "stp_required"})


@dataclass(frozen=True)
class InertnessDecision:
    inert: bool
    reasons: tuple[str, ...]  # failing clause names, or the granting facts
    evidence: dict[str, object]  # license + knob facts, finding-ready


def _no(reason: str, evidence: dict[str, object]) -> InertnessDecision:
    return InertnessDecision(inert=False, reasons=(reason,), evidence=evidence)


def _predictions(actx: AnalysisContext) -> dict[str, PortPrediction]:
    """port_id -> PortPrediction for this side's tree (cloned from
    stp_reachability — 5-line idiom, not a shared dependency)."""
    out: dict[str, PortPrediction] = {}
    for comp in actx.stp_tree().components:
        out.update(comp.ports)
    return out


class StpInertness:
    def __init__(
        self,
        baseline: AnalysisContext,
        proposed: AnalysisContext,
        agreement: StpAgreementReport | None = None,
    ) -> None:
        self._baseline = baseline
        self._proposed = proposed
        self._agreement = (
            agreement
            if agreement is not None
            else compare_to_observed(baseline.stp_tree(), baseline.ir)
        )
        self._base_pred = _predictions(baseline)
        self._prop_pred = _predictions(proposed)
        self._rows: dict[str, PortAgreement] = {
            r.port_id: r for r in self._agreement.ports
        }

    # -- license ---------------------------------------------------------------
    def _component_agreement_for(self, pid: str) -> ComponentAgreement | None:
        """The port's baseline ComponentAgreement. Report components are built
        1:1 in prediction-component order (stp_agreement.compare_to_observed),
        so the strict zip is a positional join, not a heuristic."""
        components = self._baseline.stp_tree().components
        for tree_comp, agree_comp in zip(components, self._agreement.components, strict=True):
            if pid in tree_comp.ports:
                return agree_comp
        return None

    def _license(self, pid: str) -> tuple[str | None, dict[str, object]]:
        """None when every clause holds, else the failing reason. Evidence
        accumulates the facts checked so far either way."""
        evidence: dict[str, object] = {"port": pid}
        if pid not in self._baseline.ir.ports:
            return "license (a): port absent from baseline — no earned trust", evidence
        row = self._rows.get(pid)
        if row is None or row.bucket != "matched":
            evidence["agreement_bucket"] = row.bucket if row else None
            return (
                "license (b): the port's own baseline agreement row is not matched",
                evidence,
            )
        evidence["agreement_bucket"] = "matched"
        evidence["observed_role"] = row.observed_role
        evidence["observed_state"] = row.observed_state
        comp = self._component_agreement_for(pid)
        if comp is None or not comp.agreement_clean:
            return "license (c): baseline component agreement is not clean", evidence
        evidence["component_matched_count"] = comp.matched_count
        base_p, prop_p = self._base_pred.get(pid), self._prop_pred.get(pid)
        if (
            base_p is None
            or prop_p is None
            or (base_p.role, base_p.state) != (prop_p.role, prop_p.state)
            or base_p.confidence is not ConfidenceLevel.HIGH
            or prop_p.confidence is not ConfidenceLevel.HIGH
        ):
            evidence["baseline_prediction"] = (
                (base_p.role, base_p.state, base_p.confidence.value) if base_p else None
            )
            evidence["proposed_prediction"] = (
                (prop_p.role, prop_p.state, prop_p.confidence.value) if prop_p else None
            )
            return (
                "license (d): tree position not identical at HIGH confidence "
                "across baseline and proposed",
                evidence,
            )
        evidence["predicted_role"] = base_p.role
        evidence["predicted_state"] = base_p.state
        return None, evidence

    # -- entry point -------------------------------------------------------------
    def decide(
        self, pid: str, knob: str, old_value: bool | str, new_value: bool | str
    ) -> InertnessDecision:
        evidence: dict[str, object] = {"port": pid, "knob": knob}
        if knob not in _ELIGIBLE:
            return _no(f"knob {knob} is not SAFE-eligible in this slice", evidence)
        if not isinstance(old_value, bool) or not isinstance(new_value, bool):
            return _no("unresolved token value — never provable", evidence)
        failure, license_ev = self._license(pid)
        evidence.update(license_ev)
        if failure is not None:
            return _no(failure, evidence)
        if knob == "stp_no_root_port":
            return self._rule_no_root_port(pid, evidence)
        return self._rule_required(pid, new_value, evidence)

    # -- knob rules (Task 3) -------------------------------------------------
    def _rule_no_root_port(
        self, pid: str, evidence: dict[str, object]
    ) -> InertnessDecision:
        raise NotImplementedError  # Task 3

    def _rule_required(
        self, pid: str, enabling: bool, evidence: dict[str, object]
    ) -> InertnessDecision:
        raise NotImplementedError  # Task 3
```

- [ ] **Step 4: Run the Task-2 tests**

Run: `uv run pytest tests/analysis/test_stp_inertness.py -v`
Expected: every `license_*`, `non_eligible`, `token`, `sanity`, and `shared_agreement` test PASSES (they all fail before reaching a knob rule or never reach one). No test in this task exercises the `NotImplementedError` paths.

- [ ] **Step 5: Full gate, then commit**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

```bash
git add src/digital_twin/analysis/stp_inertness.py tests/analysis/test_stp_inertness.py
git commit -m "feat(analysis): StpInertness contract + 4-clause telemetry license (Spec-6)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Knob rules — designated-only root-protect, positively-evidenced `stp_required`

**Files:**
- Modify: `src/digital_twin/analysis/stp_inertness.py` (replace both `NotImplementedError` bodies; add the peer scan)
- Test: `tests/analysis/test_stp_inertness.py` (append)

**Interfaces:**
- Consumes: Task 2's license/evidence plumbing; `DeviceRole` from `digital_twin.ir.entities`; `Link.meta.confidence.level`.
- Produces: working `decide()` for both eligible knobs — Task 4 consumes it via `CheckContext`.

- [ ] **Step 1: Write the failing tests (append to `tests/analysis/test_stp_inertness.py`)**

```python
# --- knob rules (Task 3) -----------------------------------------------------


def test_root_protect_on_designated_port_is_inert_both_directions():
    base = _fully_observed(_bridge_ir())
    si = _inertness(base, base)
    enable = si.decide("cc03:ge-0/0/2", "stp_no_root_port", False, True)
    disable = si.decide("cc03:ge-0/0/2", "stp_no_root_port", True, False)
    assert enable.inert and disable.inert
    assert enable.evidence["predicted_role"] == "designated"


def test_root_protect_on_root_bridge_ports_is_inert():
    # every aa01 (root bridge) port is designated -> inert
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("aa01:ge-0/0/2", "stp_no_root_port", False, True)
    assert d.inert


def test_root_protect_on_alternate_port_floors():
    # bb02:ge-0/0/2 is alternate/blocking: root-protect would go
    # root-inconsistent on superior BPDUs — resilience change, REVIEW
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("bb02:ge-0/0/2", "stp_no_root_port", False, True)
    assert not d.inert and any("designated" in r for r in d.reasons)


def test_root_protect_on_root_port_floors_at_the_rule():
    # cc03:ge-0/0/1 is the observed+predicted root port; the CHECK's
    # observed-root ERROR route wins in practice, but the module itself must
    # also refuse (defense in depth — never rely on caller ordering)
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("cc03:ge-0/0/1", "stp_no_root_port", False, True)
    assert not d.inert


def test_required_enable_with_validated_switch_peer_is_inert():
    # cc03:ge-0/0/2 <-> bb02:ge-0/0/1: two-sided link, both switches, no
    # bpdu_filter, peer row matched (root/forwarding), peer position HIGH-stable
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("cc03:ge-0/0/2", "stp_required", False, True)
    assert d.inert
    assert d.evidence["peer"] == "bb02:ge-0/0/1"


def test_required_enable_with_telemetry_dark_peer_floors():
    # R1-P1: peer row unvalidatable — switch + no-filter is NOT positive
    # evidence that BPDUs flow
    base = _fully_observed(_bridge_ir(), skip=frozenset({"bb02:ge-0/0/1"}))
    d = _inertness(base, base).decide("cc03:ge-0/0/2", "stp_required", False, True)
    assert not d.inert and any("peer" in r for r in d.reasons)


def test_required_enable_with_bpdu_filter_peer_floors():
    base = _fully_observed(_bridge_ir())
    port = base.ports["bb02:ge-0/0/1"]
    new_ports = dict(base.ports)
    new_ports["bb02:ge-0/0/1"] = dataclasses.replace(port, bpdu_filter=True)
    base = dataclasses.replace(base, ports=new_ports)
    d = _inertness(base, base).decide("cc03:ge-0/0/2", "stp_required", False, True)
    assert not d.inert


def test_required_enable_with_no_modeled_link_floors():
    # bb02:acc (with_vlan fixture) has no link at all
    base = _fully_observed(_bridge_ir(with_vlan=True))
    base = _set_observed(base, "bb02:acc", role="designated", state="forwarding")
    d = _inertness(base, base).decide("bb02:acc", "stp_required", False, True)
    assert not d.inert


def test_required_enable_peer_moved_by_delta_floors_peer_clause():
    # plan-review P1, isolating: the TARGET cc03:ge-0/0/2 keeps an identical
    # HIGH designated position in BOTH states; ONLY the peer moves. Proposed
    # drops dd04's priority to 4096 (< cc03's 8192): bb02's equal-cost root
    # tiebreak flips from cc03 to dd04, so peer bb02:ge-0/0/1 goes
    # root->alternate while cc03's own root path (direct to aa01) and its
    # designated claim on the cc03-bb02 segment are untouched. The failure
    # MUST name the peer-position clause — proving the peer validation is
    # load-bearing, not shadowed by license (d) on the target.
    base = _fully_observed(_bridge_ir())
    prop = _with_priority(base, "dd04", 4096)
    si = _inertness(base, prop)
    # sanity: the target's OWN license fully holds across this delta (a
    # designated-rule grant succeeds), so any stp_required failure below is
    # attributable to the peer clauses alone
    assert si.decide("cc03:ge-0/0/2", "stp_no_root_port", False, True).inert
    d = si.decide("cc03:ge-0/0/2", "stp_required", False, True)
    assert not d.inert
    assert any("peer tree position" in r for r in d.reasons)


def test_required_disable_on_observed_forwarding_is_inert():
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("cc03:ge-0/0/2", "stp_required", True, False)
    assert d.inert


def test_required_disable_on_observed_blocking_floors():
    # bb02:ge-0/0/2 observed blocking: if the requirement were the operative
    # hold, removing it could unblock into a loop — never assumed benign.
    # (Also floors at the license? No: the row IS matched. The RULE floors it.)
    base = _fully_observed(_bridge_ir())
    d = _inertness(base, base).decide("bb02:ge-0/0/2", "stp_required", True, False)
    assert not d.inert and any("forwarding" in r for r in d.reasons)
```

Note for the implementer: `test_required_enable_peer_moved_by_delta_floors_peer_clause` asserts the exact reason string `"peer tree position"` — if you word the peer-clause reason differently, the TEST is the contract: keep the phrase `peer tree position` in the reason.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/analysis/test_stp_inertness.py -v -k "root_protect or required"`
Expected: FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement the rules**

In `src/digital_twin/analysis/stp_inertness.py`, add the imports (plan-review
P2: `peer_of` is fully typed — `IR`/`Link` are real imports, no `object` +
attribute access, which strict mypy rejects):

```python
from digital_twin.ir.entities import DeviceRole, Link
from digital_twin.ir.model import IR
```

(If `Link` does not re-export from `digital_twin.ir.entities`, import it from
where `stp_agreement.py`/`stp_policy.py` do — `from digital_twin.ir import Link` —
and keep `IR` from `digital_twin.ir.model`, mirroring `stp_agreement.py`.)

Replace the two placeholder methods with:

```python
    def _rule_no_root_port(
        self, pid: str, evidence: dict[str, object]
    ) -> InertnessDecision:
        """Both directions: inert iff the validated tree position is
        `designated`. Deliberately NOT `role != "root"`: root-protect on an
        ALTERNATE port (which receives superior BPDUs by definition) goes
        root-inconsistent — dataplane unchanged now, failover silently
        removed; that deserves REVIEW. A designated port never receives
        superior BPDUs in the validated stable state, so protect provably
        never triggers; this also covers every root-bridge port."""
        role = str(evidence["predicted_role"])
        if role != "designated":
            return _no(
                f"predicted role {role!r} is not designated — root-protect is "
                f"not provably inert outside the designated role",
                evidence,
            )
        return InertnessDecision(
            inert=True,
            reasons=(
                "validated designated port: superior BPDUs provably absent in "
                "the stable state (stable-state claim only)",
            ),
            evidence=evidence,
        )

    def _rule_required(
        self, pid: str, enabling: bool, evidence: dict[str, object]
    ) -> InertnessDecision:
        if enabling:
            failure = self._validated_switch_peer(pid, evidence)
            if failure is not None:
                return _no(failure, evidence)
            return InertnessDecision(
                inert=True,
                reasons=(
                    "peer positively validated as an STP participant — BPDUs "
                    "demonstrably flow, the requirement is already satisfied",
                ),
                evidence=evidence,
            )
        # disabling: only provable when the requirement is demonstrably not
        # the operative constraint — the port is observed FORWARDING. An
        # observed-blocking port might be held down BY the requirement;
        # removing it could unblock into a loop. Never assumed benign.
        if evidence.get("observed_state") != "forwarding":
            return _no(
                "observed stp_state is not 'forwarding' — removing the BPDU "
                "requirement from a non-forwarding port is not provably inert",
                evidence,
            )
        return InertnessDecision(
            inert=True,
            reasons=(
                "port observed forwarding: the requirement is demonstrably not "
                "the operative constraint (stable-state claim only)",
            ),
            evidence=evidence,
        )

    def _validated_switch_peer(
        self, pid: str, evidence: dict[str, object]
    ) -> str | None:
        """R1-P1 'effectively STP-participating peer': EVERY clause required.
        Returns the failing reason, or None when the peer is fully validated
        (evidence gains peer facts). Cloned peer-scan idiom (analysis/ must
        not import from checks/)."""
        base_ir, prop_ir = self._baseline.ir, self._proposed.ir

        def peer_of(ir: IR) -> tuple[str, Link] | None:
            hits: list[tuple[str, Link]] = []
            for lk in ir.links:
                if lk.a_port == pid:
                    hits.append((lk.b_port, lk))
                elif lk.b_port == pid:
                    hits.append((lk.a_port, lk))
            return hits[0] if len(hits) == 1 else None

        base_hit, prop_hit = peer_of(base_ir), peer_of(prop_ir)
        if base_hit is None or prop_hit is None or base_hit[0] != prop_hit[0]:
            return "no single stable modeled link across baseline and proposed"
        peer_pid = base_hit[0]
        evidence["peer"] = peer_pid
        for _, lk in (base_hit, prop_hit):
            if lk.meta.confidence.level is not ConfidenceLevel.HIGH:
                return "peer tie is not two-sided HIGH in both states"
        for ir in (base_ir, prop_ir):
            peer_port = ir.ports.get(peer_pid)
            if peer_port is None:
                return "peer port not modeled in both states"
            device = ir.devices.get(peer_port.device_id)
            if device is None or device.role is not DeviceRole.SWITCH:
                return "peer device is not a switch — never an STP participant claim"
            if peer_port.bpdu_filter:
                return "peer port has bpdu_filter set in at least one state"
        peer_row = self._rows.get(peer_pid)
        if peer_row is None or peer_row.bucket != "matched":
            return (
                "peer row is not matched — a switch that SHOULD run STP is not "
                "positive evidence that BPDUs flow (R1-P1)"
            )
        base_p, prop_p = self._base_pred.get(peer_pid), self._prop_pred.get(peer_pid)
        if (
            base_p is None
            or prop_p is None
            or (base_p.role, base_p.state) != (prop_p.role, prop_p.state)
            or base_p.confidence is not ConfidenceLevel.HIGH
            or prop_p.confidence is not ConfidenceLevel.HIGH
        ):
            return "peer tree position not identical at HIGH across both states"
        evidence["peer_observed_role"] = peer_row.observed_role
        evidence["peer_predicted_role"] = base_p.role
        return None
```

- [ ] **Step 4: Run the full module suite**

Run: `uv run pytest tests/analysis/test_stp_inertness.py -v`
Expected: ALL pass.

- [ ] **Step 5: Full gate, then commit**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

```bash
git add src/digital_twin/analysis/stp_inertness.py tests/analysis/test_stp_inertness.py
git commit -m "feat(analysis): StpInertness knob rules — designated-only root-protect, evidenced stp_required

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Check wiring — `.inert_change`, suppression, provisional coverage note

**Files:**
- Modify: `src/digital_twin/checks/base.py` (add `stp_inertness` property)
- Modify: `src/digital_twin/checks/wired/stp_policy.py` (`run()` + `_blocking_risk` call site + module docstring)
- Test: `tests/checks/test_stp_policy.py` (append + amend guard + module docstring)

**Interfaces:**
- Consumes: `StpInertness.decide(pid, knob, old_value, new_value) -> InertnessDecision` (Task 3); `CheckContext.stp_agreement` (Task 1); helpers `_EXPECTED_ROLES`, `_fully_observed`, `_with_policy` from `tests.analysis.test_stp_inertness`.
- Produces: finding code `wired.stp.policy.inert_change` (INFO/HIGH); the amended floor invariant. Task 5 drives it e2e.

Emission rules being implemented (spec §Emission, verbatim intent):
1. Risk codes first, byte-identical; any fire → grant never consulted.
2. Else decide EVERY changed knob; all inert → provisional `.inert_change` (INFO/HIGH, license+knob evidence, `caused_by`).
3. Any knob unproven → `.policy_change` floor exactly as today + `evidence["inertness"]` = the failure reasons.
4. Provisional grant emitted ONLY if no WARNING+ finding of THIS check names the port in `affected_entities` (cross-end `link_mismatch` included); a suppressed grant falls back to the `.policy_change` floor (link findings never satisfy the per-port floor). INFO never suppresses.
5. `.preexisting` unchanged.
6. The `"peer unobserved"` blocking note is discarded iff the port's `stp_required`-enable decision was inert — keyed on the PROOF, not on grant emission.

- [ ] **Step 1: Add `CheckContext.stp_inertness` (base.py)**

Extend the TYPE_CHECKING block and add below `stp_agreement`:

```python
if TYPE_CHECKING:
    from digital_twin.analysis.stp_agreement import StpAgreementReport
    from digital_twin.analysis.stp_inertness import StpInertness
    from digital_twin.analysis.stp_reachability import StpReachability
```

```python
    @property
    def stp_inertness(self) -> StpInertness:
        cached = getattr(self, "_stp_inertness", None)
        if cached is None:
            from digital_twin.analysis.stp_inertness import StpInertness

            cached = StpInertness(
                self.baseline, self.proposed, agreement=self.stp_agreement
            )
            object.__setattr__(self, "_stp_inertness", cached)
        return cached
```

- [ ] **Step 2: Write the failing check-level tests (append to `tests/checks/test_stp_policy.py`)**

```python
# --- Spec-6: licensed SAFE grants ---------------------------------------------

from digital_twin.ir import IRCapability, diff_ir  # noqa: E402  (extend existing imports instead if already present)
from tests.analysis.test_stp_inertness import (  # noqa: E402
    _bridge_ir,
    _fully_observed,
    _with_policy,
)


def _validated_pair(pid: str, **knobs):
    base = _fully_observed(_bridge_ir())
    return base, _with_policy(base, pid, **knobs)


def _run_pair(base, prop) -> CheckResult:
    return StpPolicyCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
        diff=diff_ir(base, prop)))


def test_licensed_root_protect_grant_emits_inert_change_info():
    base, prop = _validated_pair("cc03:ge-0/0/2", stp_no_root_port=True)
    result = _run_pair(base, prop)
    codes = [f.code for f in result.findings]
    assert "wired.stp.policy.inert_change" in codes
    assert "wired.stp.policy.policy_change" not in codes
    grant = next(f for f in result.findings if f.code.endswith("inert_change"))
    assert grant.severity is Severity.INFO
    assert grant.confidence.level is ConfidenceLevel.HIGH
    assert grant.caused_by  # delta-caused, auditable
    assert "stable-state" in str(grant.evidence["severity_reason"])
    assert result.status is Status.PASS
    assert result.coverage.state is CoverageState.COMPLETE


def test_unlicensed_port_floors_with_inertness_reasons_in_evidence():
    # telemetry-dark fixture (no observed roles at all): license (b) fails
    base = _bridge_ir()
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_no_root_port=True)
    result = _run_pair(base, prop)
    floor = next(f for f in result.findings if f.code.endswith("policy_change"))
    assert floor.severity is Severity.WARNING
    assert "inertness" in floor.evidence


def test_risk_wins_over_grant_observed_root_port():
    # enabling root-protect on the OBSERVED root port cc03:ge-0/0/1 must fire
    # .root_protect_risk ERROR and emit NO grant, even though telemetry is clean
    base, prop = _validated_pair("cc03:ge-0/0/1", stp_no_root_port=True)
    result = _run_pair(base, prop)
    codes = [f.code for f in result.findings]
    assert "wired.stp.policy.root_protect_risk" in codes
    assert "wired.stp.policy.inert_change" not in codes


def test_multi_knob_one_uneligible_floors_whole_port():
    base, prop = _validated_pair(
        "cc03:ge-0/0/2", stp_no_root_port=True, stp_p2p=True
    )
    result = _run_pair(base, prop)
    codes = [f.code for f in result.findings]
    assert "wired.stp.policy.inert_change" not in codes
    assert "wired.stp.policy.policy_change" in codes


def test_required_enable_grant_discards_blocking_note_coverage_complete():
    # R2-P1 reconciliation: validated switch peer -> proof succeeds -> the
    # "peer unobserved" note is DISCARDED -> COMPLETE/PASS
    base, prop = _validated_pair("cc03:ge-0/0/2", stp_required=True)
    result = _run_pair(base, prop)
    assert not any("peer unobserved" in n for n in result.coverage.notes)
    assert result.coverage.state is CoverageState.COMPLETE
    assert result.status is Status.PASS


def test_required_enable_dark_peer_keeps_note_and_floors():
    from tests.analysis.test_stp_inertness import _bridge_ir as _b
    base = _fully_observed(_b(), skip=frozenset({"bb02:ge-0/0/1"}))
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_required=True)
    result = _run_pair(base, prop)
    assert any("peer unobserved" in n for n in result.coverage.notes)
    assert result.coverage.state is CoverageState.PARTIAL
    assert any(f.code.endswith("policy_change") for f in result.findings)


def test_cross_end_link_mismatch_warning_suppresses_grant():
    # adjustment 4: the PEER end flips use_vstp (WARNING link_mismatch on the
    # shared link names both ports) while THIS port's own change is licensed
    # inert -> grant suppressed, port falls back to the floor
    base = _fully_observed(_bridge_ir())
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_no_root_port=True)
    prop = _with_policy(prop, "bb02:ge-0/0/1", use_vstp=True)
    result = _run_pair(base, prop)
    by_code = {}
    for f in result.findings:
        by_code.setdefault(f.code.rsplit(".", 1)[-1], []).append(f)
    assert "link_mismatch" in by_code
    assert not by_code.get("inert_change")
    floored_ports = {f.subject.id for f in by_code["policy_change"]}
    assert "cc03:ge-0/0/2" in floored_ports  # suppressed back to the floor


def test_info_link_mismatch_does_not_suppress_grant():
    # pre-existing identical vstp disagreement on the SAME link (INFO context)
    # must not block the licensed grant on this port
    base = _fully_observed(_bridge_ir())
    base = _with_policy(base, "bb02:ge-0/0/1", use_vstp=True)  # both states
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_no_root_port=True)
    result = _run_pair(base, prop)
    info_mismatch = [
        f for f in result.findings
        if f.code.endswith("link_mismatch") and f.severity is Severity.INFO
    ]
    assert info_mismatch  # the pre-existing context finding IS emitted
    assert any(f.code.endswith("inert_change") for f in result.findings)
```

Amend the existing guard `test_no_stp_policy_fixture_can_resolve_safe` to the new invariant, and update the module docstring's first paragraph:

```python
def test_floor_invariant_every_changed_port_yields_a_finding():
    # Spec-6 amended invariant: every changed port yields >=1 delta-caused
    # finding — WARNING-or-above OR a fully-licensed .inert_change INFO.
    # These telemetry-dark flips must all stay on the WARNING floor:
    for knob in ("stp_required", "stp_no_root_port", "stp_p2p", "use_vstp"):
        findings = _run_flip(knob, True).findings
        assert findings, knob
        assert any(f.severity is not Severity.INFO for f in findings), knob
    # and the SAFE path is exercised (non-vacuous guard): a fully-licensed
    # change yields the INFO grant and NOTHING at WARNING+
    base, prop = _validated_pair("cc03:ge-0/0/2", stp_no_root_port=True)
    findings = _run_pair(base, prop).findings
    assert any(f.code.endswith("inert_change") for f in findings)
    assert all(f.severity is Severity.INFO for f in findings)
```

(Delete the old `test_no_stp_policy_fixture_can_resolve_safe`. Docstring: replace "NEVER resolve SAFE in this slice — a changed policy always floors REVIEW" with "floor REVIEW unless a telemetry-licensed inertness proof grants `.inert_change` INFO (Spec-6); `stp_p2p`/`use_vstp` and every unproven change still always floor".)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_stp_policy.py -v -k "inert or suppress or floor_invariant or dark_peer or blocking_note"`
Expected: FAIL — no `.inert_change` code exists yet; note not discarded.

- [ ] **Step 4: Implement in `stp_policy.py`**

4a. In `run()`, restructure the per-port loop and finalization. Replace the block from `risk_findings: list[Finding] = []` through `findings.extend(self._link_mismatch(ctx))` with:

```python
            risk_findings: list[Finding] = []
            blocking_note: str | None = None
            if "stp_required" in knobs and new_policy.stp_required is True:
                # False/absent -> True only; an unresolved: token never reaches
                # here (it is filtered into unresolved_knobs above, and a token
                # is never `is True`).
                blocking_finding, blocking_note = self._blocking_risk(
                    ctx, pid, ap_peers, bpdu_filter_peers, nonap_bridge_peers, wired
                )
                if blocking_finding is not None:
                    risk_findings.append(blocking_finding)

            if "stp_no_root_port" in knobs and new_policy.stp_no_root_port is True:
                # False/absent -> True only; same unresolved-token exclusion.
                root_protect_finding, note = self._root_protect_risk(ctx, pid)
                if root_protect_finding is not None:
                    risk_findings.append(root_protect_finding)
                if note is not None:
                    notes.append(note)

            if risk_findings:
                # rule 1: risks win, the grant is never consulted; the
                # blocking note (if any) keeps today's behavior verbatim
                findings.extend(risk_findings)
                if blocking_note is not None:
                    notes.append(blocking_note)
            else:
                decisions = {
                    k: ctx.stp_inertness.decide(
                        pid, k, getattr(old_policy, k), getattr(new_policy, k)
                    )
                    for k in knobs
                }
                # rule 6 (R2-P1): the "peer unobserved" note is PROVISIONAL —
                # discarded iff the stp_required-enable PROOF succeeded (the
                # peer is positively identified and matched, so the note text
                # would be factually false). Keyed on the proof, NOT on grant
                # emission: a rule-4-suppressed grant keeps coverage truthful
                # while the port still floors via the suppressing WARNING path.
                required_proof = decisions.get("stp_required")
                if blocking_note is not None and not (
                    required_proof is not None and required_proof.inert
                ):
                    notes.append(blocking_note)
                if all(d.inert for d in decisions.values()):
                    provisional[pid] = Finding(
                        source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                        code=f"{self.id}.inert_change", severity=Severity.INFO,
                        confidence=_HIGH,
                        message=f"port {pid}: STP policy changed ({', '.join(knobs)}) — "
                                f"provably inert against the telemetry-validated tree "
                                f"(stable-state claim only)",
                        affected_entities=(pid,), subject=ObjectRef("port", pid),
                        evidence={
                            "port": pid, "knobs": knobs,
                            "inertness": {k: d.evidence for k, d in decisions.items()},
                            "severity_reason": (
                                "stable-state dataplane provably unchanged under the "
                                "telemetry-validated tree; future protection posture "
                                "out of scope"
                            ),
                        },
                        caused_by=ctx.delta_index.causes("port", [pid]),
                    )
                else:
                    findings.append(self._floor_finding(ctx, pid, knobs, decisions))

            # pre-existing stp_required=True, untouched by THIS delta (some
            # OTHER knob on the port changed) -> INFO context, never re-flagged.
```

(keep the existing `.preexisting` block below unchanged; `provisional: dict[str, Finding] = {}` is initialized next to `findings`/`notes` before the port loop.)

4b. After the port loop, replace `findings.extend(self._link_mismatch(ctx))` with the rule-4 finalization:

```python
        link_findings = self._link_mismatch(ctx)
        # rule 4: a provisional grant is emitted ONLY if no WARNING-or-higher
        # finding of THIS check names the port (cross-end link_mismatch
        # included). INFO never suppresses. A suppressed grant falls back to
        # the .policy_change floor — link findings never satisfy the per-port
        # floor (Spec-2), so the port must still carry its own WARNING.
        warning_entities = {
            e
            for f in (*findings, *link_findings)
            if f.severity is not Severity.INFO
            for e in f.affected_entities
        }
        for pid in sorted(provisional):
            grant = provisional[pid]
            if pid in warning_entities:
                knobs = list(grant.evidence["knobs"])  # type: ignore[arg-type]
                findings.append(
                    self._floor_finding(
                        ctx, pid, knobs, None,
                        suppressed_by="a WARNING-or-higher finding names this port",
                    )
                )
            else:
                findings.append(grant)
        findings.extend(link_findings)
```

4c. Add the floor-finding helper (used by both paths above) as a method:

```python
    def _floor_finding(
        self,
        ctx: CheckContext,
        pid: str,
        knobs: list[str],
        decisions: dict[str, "InertnessDecision"] | None,
        suppressed_by: str | None = None,
    ) -> Finding:
        """The unchanged Spec-2 `.policy_change` WARNING/MEDIUM floor, with the
        inertness near-miss reasons (or the suppression cause) folded into
        evidence for diagnosability — never a new severity or code."""
        evidence: dict[str, object] = {"port": pid, "knobs": knobs}
        if decisions is not None:
            evidence["inertness"] = {
                k: d.reasons for k, d in decisions.items() if not d.inert
            }
        if suppressed_by is not None:
            evidence["inertness"] = {"suppressed": suppressed_by}
        return Finding(
            source=FindingSource.CHECK, category=FindingCategory.NETWORK,
            code=f"{self.id}.policy_change", severity=Severity.WARNING,
            confidence=_MEDIUM,
            message=f"port {pid}: STP policy changed ({', '.join(knobs)}) — "
                    f"impact not provable in this slice (review)",
            affected_entities=(pid,), subject=ObjectRef("port", pid),
            evidence=evidence,
            caused_by=ctx.delta_index.causes("port", [pid]),
        )
```

with the import (TYPE_CHECKING to keep runtime layering explicit):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digital_twin.analysis.stp_inertness import InertnessDecision
```

4d. Update the module docstring: replace the sentence "so a policy change NEVER resolves SAFE in this slice" (and the trailing "SAFE is deferred to a future STP tree engine ..." sentence) with:

```
so a policy change floors REVIEW via .policy_change UNLESS a Spec-6
telemetry-licensed inertness proof (analysis/stp_inertness.py) grants
.inert_change INFO: eligible knobs stp_no_root_port/stp_required only, full
license (port row matched, component agreement_clean, identical HIGH tree
position both states) plus a knob rule, risk codes always win, and any
WARNING-or-higher finding naming the port suppresses the grant back to the
floor. The SAFE claim is stable-state-only. (2026-07-09 spec.)
```

4e. `reasoning=` string in the returned `CheckResult`: replace "every change floors REVIEW (bridge domain not provable) unless concrete no-BPDU-peer harm escalates it" with "every change floors REVIEW unless concrete harm escalates it or a telemetry-licensed inertness proof grants INFO".

- [ ] **Step 5: Run the check suite**

Run: `uv run pytest tests/checks/test_stp_policy.py tests/analysis/test_stp_inertness.py -v`
Expected: ALL pass, including every pre-existing test (the risk paths and the dark-fixture floor are byte-identical).

- [ ] **Step 6: Full gate, then commit**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

```bash
git add src/digital_twin/checks/base.py src/digital_twin/checks/wired/stp_policy.py tests/checks/test_stp_policy.py
git commit -m "feat(checks): wired.stp.policy licensed SAFE grants — .inert_change INFO under the Spec-6 license

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: End-to-end scenario goldens

**Files:**
- Create: `tests/golden/test_stp_policy_safe_scenarios.py`

**Interfaces:**
- Consumes: `_bridge_ir`, `_fully_observed`, `_with_policy` from `tests.analysis.test_stp_inertness`; the golden harness pattern from `tests/golden/test_stp_reachability_scenarios.py` (`CheckRegistry(ALL_WIRED_CHECKS).run_all` → `assemble(DecisionInputs(...))`).
- Produces: the headline SAFE proof + REVIEW boundaries, pinned at full-verdict level.

- [ ] **Step 1: Write the failing goldens**

```python
"""Spec-6 e2e goldens: STP policy SAFE grants through the FULL verdict
(CheckRegistry.run_all -> assemble/decide):

  bulk root-protect on validated inter-switch designated downlinks -> SAFE
  stp_required enable on a validated pair -> SAFE, coverage COMPLETE (R2-P1:
      the provisional "peer unobserved" note is DISCARDED, not outvoted)
  same bulk plan + one telemetry-dark port -> REVIEW
  observed-designated NON-TREE access port -> REVIEW (R1-P1-2 boundary)
  stp_p2p change -> REVIEW, byte-identical to Spec-2

Fixture: the fully-observed bridge-id topology (all 8 rows matched, one clean
component); VLAN 10 redundantly carried on both paths so no blackhole/loop
finding interferes with the SAFE assertions.
"""
from __future__ import annotations

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.ir import IRBuilder, IRCapability, diff_ir
from digital_twin.verdict.decision import Decision, DecisionInputs
from digital_twin.verdict.verdict import assemble
from tests.analysis.test_stp_inertness import _fully_observed, _with_policy
from tests.analysis.test_stp_reachability import _bridge_id_topology, _set_observed

_DESIGNATED_DOWNLINKS = ("aa01:ge-0/0/1", "aa01:ge-0/0/2", "cc03:ge-0/0/2", "dd04:ge-0/0/2")


def _validated_ir():
    # CLIENTS_ACTIVE models a SUCCESSFUL zero-client fetch (plan-review P1:
    # without it, wired.client.impact returns INSUFFICIENT_DATA on any port
    # diff and blackhole adds a missing-client note — both force REVIEW and
    # would make the SAFE assertions unreachable)
    b = IRBuilder()
    _bridge_id_topology(b, prune_vlan10=True, carry_both_paths=True)
    ir = (
        b.with_capability(IRCapability.WIRED_L2)
        .with_capability(IRCapability.L3_EXITS)
        .with_capability(IRCapability.CLIENTS_ACTIVE)
        .build()
    )
    return _fully_observed(ir)


def _verdict(base, prop):
    # exact harness shape from tests/golden/test_stp_reachability_scenarios.py:_run
    diff = diff_ir(base, prop)
    ctx = CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff,
    )
    results = CheckRegistry(ALL_WIRED_CHECKS).run_all(ctx)
    verdict = assemble(
        inputs=DecisionInputs(
            rejections=(),
            l0_fatal=False,
            baseline_unavailable=False,
            check_results=results,
        ),
        ir_diff=diff,
    )
    return verdict, results


def test_bulk_root_protect_on_designated_downlinks_is_safe():
    base = _validated_ir()
    prop = base
    for pid in _DESIGNATED_DOWNLINKS:
        prop = _with_policy(prop, pid, stp_no_root_port=True)
    verdict, results = _verdict(base, prop)
    assert verdict.decision is Decision.SAFE
    policy = next(r for r in results if r.check_id == "wired.stp.policy")
    grants = [f for f in policy.findings if f.code.endswith("inert_change")]
    assert {f.subject.id for f in grants} == set(_DESIGNATED_DOWNLINKS)


def test_required_enable_on_validated_pair_is_safe_with_complete_coverage():
    base = _validated_ir()
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_required=True)
    verdict, results = _verdict(base, prop)
    assert verdict.decision is Decision.SAFE
    policy = next(r for r in results if r.check_id == "wired.stp.policy")
    assert policy.coverage.state is CoverageState.COMPLETE
    assert not any("peer unobserved" in n for n in policy.coverage.notes)


def test_bulk_plan_with_one_dark_port_is_review():
    base_all = _validated_ir()
    base = _set_observed(base_all, "dd04:ge-0/0/2", role=None, state=None)
    prop = base
    for pid in _DESIGNATED_DOWNLINKS:
        prop = _with_policy(prop, pid, stp_no_root_port=True)
    verdict, _ = _verdict(base, prop)
    assert verdict.decision is Decision.REVIEW


def test_non_tree_access_port_is_review_even_observed_designated():
    base = _validated_ir()
    base = _set_observed(base, "bb02:acc", role="designated", state="forwarding")
    prop = _with_policy(base, "bb02:acc", stp_no_root_port=True)
    verdict, _ = _verdict(base, prop)
    assert verdict.decision is Decision.REVIEW


def test_stp_p2p_change_stays_review():
    base = _validated_ir()
    prop = _with_policy(base, "cc03:ge-0/0/2", stp_p2p=True)
    verdict, _ = _verdict(base, prop)
    assert verdict.decision is Decision.REVIEW
```

- [ ] **Step 2: Run to verify current state**

Run: `uv run pytest tests/golden/test_stp_policy_safe_scenarios.py -v`
Expected: the two SAFE tests and coverage assertions PASS only if Tasks 1–4 landed correctly; any failure here is an integration defect to fix in THIS task (e.g., another check flooring the verdict on these fixtures — investigate which `CheckResult` is non-PASS/COMPLETE and report it; do NOT weaken the assertion to REVIEW).

- [ ] **Step 3: Full gate, then commit**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

```bash
git add tests/golden/test_stp_policy_safe_scenarios.py
git commit -m "test(golden): Spec-6 e2e SAFE/REVIEW scenarios for STP policy grants

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Docs wrap

**Files:**
- Modify: `docs/ROADMAP.md` — mark the "SAFE grants in wired.stp.policy" item done (2026-07-09) with a summary mirroring prior done-entries (license clauses, eligible knobs, `.inert_change`, tree-representation scope); update the Spec-2 entry's "(still never SAFE)" phrasing to point at Spec-6.
- Modify: `README.md` — the check-inventory row / prose for `wired.stp.policy`: change any "never SAFE" wording to "SAFE only under the Spec-6 telemetry-validated inertness license"; add `stp_inertness` to the `analysis/` layout line ("… + STP-aware reachability taint + policy inertness license (memoized)").
- Modify: `docs/superpowers/specs/2026-07-09-stp-policy-safe-grants-design.md` — Status → `Implemented (branch feat/stp-safe-grants; live verify pending)`.

- [ ] **Step 1: Make the three edits** (grep `README.md` and `docs/ROADMAP.md` for `stp.policy`, `never SAFE`, and the `analysis/` layout line; keep each edit to the minimal phrase change described above — README's exact current wording may have drifted, adapt in place).

- [ ] **Step 2: Full gate** (docs-only, but run it anyway): `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

- [ ] **Step 3: Commit**

```bash
git add docs/ROADMAP.md README.md docs/superpowers/specs/2026-07-09-stp-policy-safe-grants-design.md
git commit -m "docs: Spec-6 wrap — roadmap, README check inventory, spec status

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

*(Live verification — replaying a bulk root-protect plan against the production-validated site and re-running the full golden suite — is performed by the session controller after the final review, per the established Spec-2..5 routine; it is not a subagent task.)*

---

## Self-Review (performed at plan-writing time)

- **Spec coverage:** decisions 1–6 → T2/T3 (knobs, license, scope), T4 (grant shape, emission rules 1–6), T1 (shared memo); R1-P1 peer evidence → T3; R1-P1-2 non-tree boundary → T2 unit + T5 golden; R1-P2 telemetry-dark-by-role → T2/T4 dark fixtures; R2-P1 note reconciliation → T4 rules + tests + T5 COMPLETE golden; adjustment 5 component-dirty → T2 (both variants); amended Spec-2 guard → T4; docs → T6.
- **Type consistency:** `decide(pid, knob, old_value, new_value)` and `InertnessDecision(inert, reasons, evidence)` used identically in T2/T3/T4; `agreement: StpAgreementReport | None = None` identical in T1 (`StpReachability`) and T2 (`StpInertness`); helper names `_bridge_ir`/`_fully_observed`/`_with_policy` exported by T2's test module and imported by T4/T5.
- **Known judgment points for implementers:** T3's peer-moved test is ISOLATING (plan-review R1): it first proves the target's own license holds across the delta, then demands the `peer tree position` reason verbatim. T5 SAFE goldens may surface an unrelated check flooring the fixture — that is an integration finding to resolve, not an assertion to weaken (CLIENTS_ACTIVE is already supplied for `wired.client.impact`).
- **Plan-review R1 fixes baked in:** T5 harness copies `_run` from the Spec-5 golden verbatim (`DecisionInputs(rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=...)` + `assemble(..., ir_diff=diff)`) and the fixture carries `CLIENTS_ACTIVE`; clause (d)'s HIGH bar has its own MEDIUM-LAG regression (`test_license_d_medium_confidence_lag_position_floors`); the peer helper is strictly typed.
