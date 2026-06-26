# Gateway-loss → CRITICAL; VLAN-split → context — Design

**Status:** Approved (design); plan pending
**Date:** 2026-06-26
**Author:** Thomas Munzer (with Claude)

## Problem

On the live `mge`-disable delta, disabling a leaf AP's port made `wired.l2.vlan_segmentation.split` fire `WARNING` ("broadcast domain partitioned") for every VLAN the AP carried — flooding the per-VLAN topology charts. But a leaf shedding is not an interesting broadcast-domain split; it is the AP *losing its path to the gateway*, which `wired.l2.blackhole.exit_lost` already reports. Two corrections, one UX intent — **move the severity from the noisy symptom (segmentation) to the real harm (lost gateway path), and make that harm the top-severity event**:

1. `vlan_segmentation.split` is contextual, not a harm carrier → it should be `INFO`.
2. A previously-reachable populated segment that is now cut off from a **confidently-located** exit is the most severe wired outcome → `blackhole.exit_lost` should be `CRITICAL`.

`CRITICAL` here is about **operator salience**, not the verdict: `decision.py:64` already gates UNSAFE on NETWORK `ERROR` *or* `CRITICAL`, so the SAFE/REVIEW/UNSAFE result is unchanged. The point is that "gateway path lost" visually outranks a generic disruption (the renderer — mistmcp — already has a Critical tier).

## Design

### Locked contract

- `wired.l2.vlan_segmentation.split` → **contextual `INFO`**, and the check no longer sets `Status.WARN` (it becomes purely observational, `Status.PASS`).
- `wired.l2.blackhole.exit_lost` → **`CRITICAL` when `exit_confidence == HIGH`**, `WARNING` below HIGH.
- `wired.l2.blackhole.new_member_stranded` → **unchanged** (`ERROR if high else WARNING`). A newly-added member that *never* reached the exit is "misconfigured," not "lost the gateway" — a different event.
- `wired.l2.blackhole.exit_unlocatable` → **unchanged** (no confident exit, so no CRITICAL on a guess).
- `wired.l2.isolation.severed` → **unchanged in this spec** (see Deferred).

### Change 1 — `vlan_segmentation.split` → INFO

In `l2_vlan_segmentation.py`, the split branch ([l2_vlan_segmentation.py:47-60](src/digital_twin/checks/wired/l2_vlan_segmentation.py)) emits `severity=Severity.WARNING` and sets `status = Status.WARN`. Change the finding to `Severity.INFO` and **remove** the `status = Status.WARN` line. The `reshape` INFO path and the finding's `code`/`subject`/`evidence`/`caused_by` are unchanged, so the per-VLAN attribution (and visual-map context) is preserved — only the severity and the REVIEW floor are dropped.

**Why this is safe (never-false-SAFE).** A split means a baseline VLAN component fragmented. For each resulting piece: if it has members and *lost* its exit → `blackhole.exit_lost` (CRITICAL/WARNING) or, if the exit is unlocatable, `blackhole.exit_unlocatable` (INSUFFICIENT_DATA → REVIEW), or, in the exit-less case, `isolation.severed` (ERROR/WARNING) reports it — every one of these floors the verdict on its own. A piece that *keeps* its exit, or an empty piece, is genuinely harmless. So no harmful split can become SAFE by this demotion, and `segmentation` never needs to compute exit-reachability itself.

### Change 2 — `blackhole.exit_lost` → CRITICAL at HIGH confidence

In `l2_blackhole.py`, both codes currently share one severity line ([l2_blackhole.py:332](src/digital_twin/checks/wired/l2_blackhole.py)):

```python
severity=Severity.ERROR if high else Severity.WARNING,
```

Make it depend on the existing `lost_exit` boolean:

```python
severity=(
    (Severity.CRITICAL if high else Severity.WARNING)
    if lost_exit
    else (Severity.ERROR if high else Severity.WARNING)
),
```

- `lost_exit` is True exactly for `exit_lost` (a component that reached a located exit in baseline and no longer does); it has members by construction (`stranded = c.has_members and not c.reaches_exit`). `high = exit_conf.level is ConfidenceLevel.HIGH` is the located exit's confidence.
- So `exit_lost` + HIGH → CRITICAL; `exit_lost` below HIGH → WARNING (unchanged); `new_member_stranded` → ERROR/WARNING (unchanged).
- The `worst` status line (`Status.FAIL if high else Status.WARN`) is **unchanged**: CRITICAL and ERROR both map to FAIL → UNSAFE.

`CRITICAL` is a precise label: a previously-reachable, populated segment now cut off from a confidently-located exit. It does not change the verdict (`decision.py:64` treats ERROR and CRITICAL identically for UNSAFE).

## Files touched

- `src/digital_twin/checks/wired/l2_vlan_segmentation.py` — split finding → INFO; drop `Status.WARN`; update the module docstring's split line.
- `src/digital_twin/checks/wired/l2_blackhole.py` — `exit_lost` severity → CRITICAL/WARNING by `lost_exit`; update the module docstring's severity summary.
- Tests in `tests/checks/test_l2_vlan_segmentation*.py`, `tests/checks/test_l2_blackhole*.py`; any golden that asserted the old severities.

## Testing

- **blackhole exit_lost CRITICAL:** a member component that reached a HIGH-confidence located exit in baseline and is severed from it in proposed → finding `wired.l2.blackhole.exit_lost`, `severity is Severity.CRITICAL`, status FAIL.
- **blackhole exit_lost below HIGH → WARNING:** the same severance where the exit confidence is MEDIUM/LOW (e.g. boundary-uplink via one-sided LLDP) → `severity is Severity.WARNING` (unchanged).
- **new_member_stranded unchanged:** a newly-added member port with no path to a HIGH exit → `severity is Severity.ERROR` (NOT critical).
- **exit_unlocatable unchanged:** a member VLAN whose exit cannot be located → still `exit_unlocatable` / INSUFFICIENT_DATA, no severity change.
- **segmentation split is INFO:** a delta that fragments a VLAN's broadcast domain → finding `wired.l2.vlan_segmentation.split`, `severity is Severity.INFO`, and the check's `status is Status.PASS` (no WARN floor).
- **Verdict invariance for the harm:** a confident `exit_lost` still yields `UNSAFE` (CRITICAL gates UNSAFE exactly like ERROR) — escalation is label-only.
- **Existing goldens / check tests:** any test asserting the old `exit_lost` ERROR or `segmentation.split` WARNING legitimately changes (update to CRITICAL / INFO respectively, and note why); a confident-severance golden's verdict stays UNSAFE.

## Scope and deferred

In scope: `vlan_segmentation.split` → INFO and `blackhole.exit_lost` → CRITICAL only.

**Deferred — `l2_isolation.severed` → CRITICAL (tight condition).** A future spec may escalate `isolation.severed` to CRITICAL, but only under a *tightly defined* exit-anchor condition: the severed occupied fragment's baseline home component contained an exit anchor (`exit_anchor_nodes`) that the fragment no longer reaches, at HIGH confidence. It is deliberately out of this spec because the cases where `isolation` is the *only* report of a gateway loss are exactly those with **no** confident exit (the original no-modeled-L3-exit scenario) — where, per this design, we must not invent CRITICAL. Whenever the exit *is* confidently located, `blackhole.exit_lost` already carries the CRITICAL, so isolation escalation would add risk for no coverage gain.

Also still deferred (separate threads): `blackhole.exit_unlocatable`'s exit-location coverage gap, and the mistmcp VisualMap consumer that renders the corrected attribution/severity.
