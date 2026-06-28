# Site WLAN Coverage Loss Implementation Plan

## Goal

Implement SP1: simulate site-local WLAN removal/disable/scope loss and report active wireless clients that lose SSID coverage.

This plan implements the approved spec:

- `docs/superpowers/specs/2026-06-27-site-wlan-coverage-loss-design.md`

The feature is site-path only:

- `wlan` delete becomes supported for site-local WLAN objects.
- `site_setting` and `device` deletes remain unsupported.
- Inherited WLAN deletion remains unsupported in the site path and must be simulated at the org/template layer in later SP2/SP3 work.
- Wireless auth/PSK/security changes remain out of scope.

## Implementation Baseline

Before coding, make sure this worktree is based on `origin/main` at or after PR #27:

- Required baseline commit: `df397987b291c520333f878e4f316e9e0f575676`
- Reason: the spec depends on the merged coverage-gap precedence in `decision.py`, where `coverage_lost` FAIL is not masked by check-local coverage UNKNOWN, but hard UNKNOWN still wins globally.

Suggested preflight:

```bash
git fetch origin
git merge-base --is-ancestor df397987b291c520333f878e4f316e9e0f575676 HEAD
```

If the command fails, rebase or merge `origin/main` before starting implementation. Do not start from the older pre-#27 worktree state.

## Design Summary

The implementation has four moving parts:

1. Site simulation accepts `delete` for `wlan` only and removes the WLAN row from proposed raw state.
2. `Client` gains an observational, diff-ignored `ssid` field for wireless clients.
3. A new `wireless.wlan.client_impact` check reports coverage loss when active wireless clients on an affected SSID have no provable survivor WLAN on their AP.
4. Delete/update verdicts retain `config_diffs`, including UNKNOWN cases where the diff is computable.

The new check is fail-closed only for the coverage question:

- A same-SSID survivor proves safety only when it is enabled and its AP coverage is provable.
- `wxtag` or otherwise unverifiable survivor scope does not clear the impact.
- Missing client telemetry yields `.unverified` REVIEW, never SAFE, when a WLAN coverage change exists.

## Task 0 - Rebase and Spec Check

### Objective

Start implementation from the correct mainline and confirm the spec is the version being implemented.

### Steps

1. Fetch `origin`.
2. Rebase or merge this feature branch onto `origin/main` at or after `df397987b291c520333f878e4f316e9e0f575676`.
3. Re-read:
   - `docs/superpowers/specs/2026-06-27-site-wlan-coverage-loss-design.md`
   - `src/digital_twin/verdict/decision.py`
   - `src/digital_twin/engine/pipeline.py`

### Acceptance

- `git merge-base --is-ancestor df397987b291c520333f878e4f316e9e0f575676 HEAD` succeeds.
- No code changes yet, unless rebasing creates mechanical conflict resolutions.

## Task 1 - Site WLAN Delete Plumbing

### Objective

Allow site `wlan` delete operations, reject all other site deletes, remove the WLAN from proposed raw state, and surface config diffs for computable delete attempts.

### Files

- `src/digital_twin/scope/object_gate.py`
- `src/digital_twin/adapters/mist/apply/objects.py`
- `src/digital_twin/adapters/mist/apply/__init__.py` if exports are needed
- `src/digital_twin/engine/pipeline.py`
- `tests/scope/test_object_gate.py`
- `tests/scope/test_wlan_object.py`
- `tests/engine/test_pipeline.py` or the existing site simulation integration test file

### RED Tests

Add or update object-gate tests:

- `wlan` delete with `site_id`, `object_id`, and an empty payload is accepted.
- `wlan` delete with a non-empty payload is rejected.
- `site_setting` delete is still rejected.
- `device` delete is still rejected.
- Other unsupported actions remain rejected.

Add apply-object tests:

- Deleting a WLAN removes exactly the row whose provider `id` matches `object_id`.
- Deleting a missing WLAN is not silently treated as success in the pipeline; `get_object` remains the ownership/existence gate before deletion.
- The helper does not introduce generic site-object delete support beyond the WLAN path used by the pipeline.

Add site simulation tests:

- Site-local WLAN delete produces a proposed IR where the baseline WLAN is removed and `diff.touches("wlan")` is true.
- The verdict contains a `config_diff` with `action="delete"`, `before=<wlan>`, `after=None`.
- Inherited WLAN delete is rejected after fetch by the same ownership doctrine as inherited WLAN update, with a useful rejection reason.

If the inherited delete rejection occurs after the delete diff is computable, keep the diff attached to the UNKNOWN verdict. This follows the PR #26 rule that computable config diffs survive UNKNOWN.

### Implementation Notes

In `object_gate.py`, keep site-mode action handling narrow:

```python
if op.action == "delete":
    if op.object_type != "wlan":
        return rejected(...)
    if op.payload:
        return rejected(...)
elif op.action != "update":
    return rejected(...)
```

In `apply/objects.py`, add a small raw-state helper, for example:

```python
def delete_object(raw: RawSiteState, object_type: str, object_id: str) -> RawSiteState:
    if object_type == "wlan":
        return dc_replace(
            raw,
            wlans=tuple(row for row in raw.wlans if str(row.get("id")) != object_id),
        )
    raise ValueError(...)
```

Teach `apply_plan` to dispatch on `op.action` (`delete -> delete_object`, update -> `replace_object`). This is needed not only for the main site loop but also for the below-profile replay path in `simulate()`, which re-applies non-device ops against the baseline before the device-profile gate. Without the dispatch, a WLAN delete would pass the main loop and then be replayed as a malformed update.

In `pipeline.py`, branch per op before `effective_update`:

1. `current = get_object(proposed_raw, op.object_type, op.object_id)`.
2. If not found, return UNKNOWN with already-built diffs.
3. For `op.action == "delete"`:
   - Build `object_config_diff(object_type=op.object_type, object_id=op.object_id, name=current.get("name"), action="delete", before=current, after=None)`.
   - If `op.object_type == "wlan"` and `wlan_is_inherited(current)`, return UNKNOWN with the diff attached.
   - Apply through `adapter.apply(proposed_raw, (op,))`, which now dispatches the delete to `delete_object`.
   - Skip L0 and field-gate because there is no proposed object.
   - Continue to the next op.
4. Leave the update path unchanged.

### Acceptance

- Site WLAN delete reaches IR diffing.
- Unsupported deletes still stop at object gate.
- Delete config diffs are visible, including computable UNKNOWN cases.
- Existing update behavior is unchanged.

## Task 2 - `Client.ssid` IR Field, Ingest, and Diff Isolation

### Objective

Carry wireless-client SSID as observational evidence for the new check without making SSID telemetry load-bearing.

### Files

- `src/digital_twin/ir/entities.py`
- `src/digital_twin/ir/diff.py`
- `src/digital_twin/adapters/mist/ingest/clients.py`
- `tests/adapters/mist/test_ingest_clients.py`
- `tests/ir/test_diff.py`

### RED Tests

Add ingest tests:

- A wireless client row with `ssid: "Corp"` mints `Client.ssid == "Corp"`.
- Missing `ssid` mints `Client.ssid is None`.
- Empty or whitespace-only `ssid` mints `Client.ssid is None`.
- Wired clients always mint `Client.ssid is None`, even if a raw row happens to carry an `ssid` key.
- Before relying on the key, verify the raw wireless-client fixture/API shape really uses `ssid`; the current ingester only reads `mac`, `ap_mac`, `vlan_id`, and `ip`.

Add diff isolation test:

- Two otherwise-identical IRs whose only difference is `Client.ssid` produce an empty diff.
- The new field must not wake any check by itself.

### Implementation Notes

Add the field near the existing observational client attributes:

```python
ssid: str | None = None
```

Normalize from raw wireless-client data only:

```python
def _ssid(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
```

Add `ssid` to the client ignored field set in `diff.py`, for example:

```python
_IGNORED_BY_KIND["client"] = frozenset({"ssid", ...})
```

If `client` has no existing ignored set, create one. Preserve existing ignored-field behavior for all entity kinds.

### Acceptance

- Wireless clients can be joined to WLAN SSID coverage.
- Unknown/blank SSID routes to `None`, so it can drive `.unverified` instead of being mistaken for a known non-match.
- SSID telemetry cannot change the verdict by creating an IR diff.

## Task 3 - `wireless.wlan.client_impact` Check

### Objective

Add a delta-conditioned check that reports active wireless clients losing WLAN SSID coverage after delete/disable/rename/scope shrink.

### Files

- `src/digital_twin/checks/wired/wlan_client_impact.py`
- `src/digital_twin/checks/wired/__init__.py`
- `tests/checks/test_wlan_client_impact.py`
- `tests/test_public_api.py`

### Check Contract

Register a new check:

- id: `wireless.wlan.client_impact`
- domain: `wireless.wlan`
- `requires() = frozenset({IRCapability.WLAN_CONFIG})`
- `applies_to(diff) = diff.touches("wlan")`
- `CLIENTS_ACTIVE` is inspected inside `run()` and is not a hard registry requirement.

Update `ALL_WIRED_CHECKS` and the public API count.

The concrete public API assertion is currently `len(ALL_WIRED_CHECKS) == 25`; it must become `26`.

### Detection Model

Compute affected SSIDs from delta-touched baseline WLANs:

- Include only baseline WLANs with `enabled is True`.
- Include removals.
- Include modifications whose changed fields intersect:
  - `ssid`
  - `enabled`
  - `apply_to`
  - `ap_ids`
  - `wxtag_ids`
- Exclude already-disabled baseline WLANs.
- Exclude added-only WLANs from `affected_ssids`, though added WLANs can act as survivors.

For each affected SSID:

1. Find baseline clients where `client.active is True`, `client.kind is ClientKind.WIRELESS`, and `Client.ssid` equals the affected SSID.
2. A client is safe only when proposed IR contains another enabled WLAN with the same SSID that provably covers the client's AP.
3. If no such survivor exists, the client is impacted.

Inspect client telemetry with the same pattern as existing client-aware checks:

```python
clients_known = (
    IRCapability.CLIENTS_ACTIVE in ctx.baseline.capabilities
    and IRCapability.CLIENTS_ACTIVE in ctx.proposed.capabilities
)
```

Coverage helper:

```python
def _covers(wlan: Wlan, ap_id: str | None) -> Literal["yes", "unknown"]:
    if wlan.apply_to == "site":
        return "yes"
    if wlan.apply_to == "aps":
        if ap_id is None:
            return "unknown"
        return "yes" if ap_id in wlan.ap_ids else "unknown"
    return "unknown"
```

Do not treat `wxtag` scope as provable coverage in SP1.

### Findings

`coverage_lost`:

- Code: `wireless.wlan.client_impact.coverage_lost`
- Category: NETWORK
- Severity: ERROR
- Confidence: HIGH
- Status: FAIL
- One finding per affected SSID.
- `subject = ObjectRef("wlan", headline_wlan_id, ssid)`, where `headline_wlan_id` is the lowest sorted changed WLAN id for that SSID.
- `affected_entities` is the impacted client ids.
- `caused_by = ctx.delta_index.causes("wlan", changed_wlan_ids_for_ssid)`.
- Evidence contains per-client records:
  - `mac`
  - `ap`
  - `ssid`

`unverified`:

- Code: `wireless.wlan.client_impact.unverified`
- Category: OPERATIONAL
- Severity: WARNING
- Confidence: HIGH
- Status: WARN
- Emitted when `affected_ssids` is non-empty and client impact cannot be verified:
  - client telemetry not fetched on either side; or
  - no `coverage_lost` finding exists, but baseline has active wireless clients with `ssid is None`.

PASS rows:

- If `affected_ssids` is empty, return PASS, COMPLETE, HIGH.
- If client telemetry is fetched, affected SSIDs exist, and zero clients are impacted, return PASS, COMPLETE, HIGH with an observation-only coverage note.
- Do not return sub-HIGH confidence on PASS rows, or the post-#27 decision layer will floor to REVIEW.

The `run()` outcome-selection order is load-bearing. Implement it exactly:

```python
if coverage_lost_findings:
    return CheckResult(status=Status.FAIL, coverage=COMPLETE, confidence=HIGH, ...)
if unverified_findings:
    return CheckResult(status=Status.WARN, coverage=COMPLETE, confidence=HIGH, ...)
return CheckResult(status=Status.PASS, coverage=COMPLETE, confidence=HIGH, ...)
```

`unverified` is emitted only when there is no `coverage_lost` finding. A known active impacted client must always win over missing/unknown client-identity uncertainty.

### RED Tests

Use focused unit tests with small IR builders. Cover:

1. Delete WLAN with active wireless client and no survivor -> FAIL + `coverage_lost`.
2. Disable `enabled: true -> false` with active client -> FAIL.
3. Rename SSID with active client on old SSID -> FAIL.
4. Scope shrink `site -> aps` excluding the client's AP -> FAIL.
5. Delete with enabled site-scope same-SSID survivor -> PASS, COMPLETE, HIGH.
6. Same-SSID survivor scoped only by `wxtag` -> FAIL, fail-closed.
7. Client telemetry not fetched and `affected_ssids` non-empty -> WARN `.unverified`.
8. Client telemetry not fetched and `affected_ssids` empty, for example added-only WLAN -> PASS, COMPLETE, HIGH.
9. Telemetry fetched, zero active clients on affected SSID -> PASS, COMPLETE, HIGH with observation note.
10. Two changed WLANs for the same SSID and one impacted client -> one finding, deterministic headline WLAN id, both causes in `caused_by`.
11. Disabled baseline WLAN deleted/renamed while an active client has the same SSID -> no impact from that baseline WLAN.
12. Active wireless client with `ssid is None`, affected SSIDs exist, and no provable-loss client exists -> `.unverified` REVIEW.

Also pin finding shape:

- `affected_entities` lists client ids, not AP ids.
- Evidence includes `mac`, `ap`, and `ssid`.
- `caused_by` points to the changed WLAN ids.

Pin the safety-critical tests with explicit assertions, not only scenario prose:

```python
# Provable site-scope survivor reaches a real PASS, not REVIEW.
assert res.status is Status.PASS
assert res.coverage.state is CoverageState.COMPLETE
assert res.confidence is not None
assert res.confidence.level is ConfidenceLevel.HIGH

# Wxtag-only survivor fails closed.
assert res.status is Status.FAIL
assert any(f.code == "wireless.wlan.client_impact.coverage_lost" for f in res.findings)

# Added-only WLAN / no affected SSID remains PASS even without client telemetry.
assert res.status is Status.PASS
assert res.coverage.state is CoverageState.COMPLETE
assert res.confidence is not None
assert res.confidence.level is ConfidenceLevel.HIGH

# Already-disabled baseline WLAN cannot create coverage loss.
assert res.status is Status.PASS
assert not any(f.code.endswith(".coverage_lost") for f in res.findings)

# Unknown-SSID clients with an affected SSID produce unverified REVIEW, never PASS.
assert res.status is Status.WARN
assert any(f.code == "wireless.wlan.client_impact.unverified" for f in res.findings)
```

### Acceptance

- The check never runs for non-WLAN diffs.
- Missing client telemetry does not become `INSUFFICIENT_DATA` at the registry; it becomes a check-level `.unverified` result.
- Any active client with known affected SSID and no provable survivor makes the verdict UNSAFE.
- PASS cases are HIGH confidence with COMPLETE coverage.

## Task 4 - End-to-End Site Simulation and Config Diffs

### Objective

Prove the full site simulate path: object gate -> raw delete/update -> IR diff -> check -> decision -> config diff rendering.

### Files

- Existing site pipeline/integration test file, likely under `tests/engine/`
- `tests/golden/` only if the repo already has SP-style golden fixtures for this surface

### RED Tests

Add end-to-end simulations:

1. Site-local WLAN delete with active wireless client:
   - Decision UNSAFE.
   - Finding code `wireless.wlan.client_impact.coverage_lost`.
   - `config_diffs` contains the WLAN delete with `after=None`.

2. Site-local WLAN disable update with active wireless client:
   - Decision UNSAFE.
   - Existing update path still works.
   - `config_diffs` contains the before/after update.

3. WLAN delete with same-SSID site-scope survivor and no impacted clients:
   - Decision SAFE unless another independent check fires.
   - Check result is PASS, COMPLETE, HIGH.
   - Config diff is still present.

4. Inherited WLAN delete:
   - Decision UNKNOWN.
   - Rejection explains that inherited WLANs must be simulated at the org/template layer.
   - If the delete diff was already computable, the config diff remains attached.

5. Delete of a missing WLAN:
   - Decision UNKNOWN.
   - Rejection is object lookup / missing object.
   - No fabricated diff.

### Notes

Use real `RawSiteState.wlans` rows where possible, keeping the raw shape aligned with existing WLAN update tests. Keep secrets out of fixtures; WLAN entities are secret-free, but raw rows can contain portal URLs in real captures.

### Acceptance

- The new behavior is proven through the public simulation API, not only check units.
- `config_diffs` survive all computable delete/update outcomes.
- Unsupported or unowned objects do not slip into proposed IR.

## Task 5 - Roadmap, Documentation, and Full Gate

### Objective

Mark SP1 complete, document the remaining SP2/SP3 boundaries, and verify the whole repo.

### Files

- `docs/ROADMAP.md` or the current roadmap tracker
- Any memory/status file used by the project, if current conventions require it

### Steps

1. Update roadmap/status:
   - SP1 site WLAN coverage loss implemented.
   - SP2 org/template WLAN delete is still deferred.
   - SP3 NAC / role-profile interaction is still deferred.
   - Wireless auth/PSK disruption remains deferred as a separate check.

2. Run the full gate:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
```

3. If goldens change, review the diff leaf-by-leaf. Do not blindly regenerate.

### Acceptance

- Full pytest, ruff, and mypy pass.
- Public check count is updated.
- Roadmap accurately separates SP1 from later SP2/SP3 work.

## Expected Commit Shape

Prefer one commit per task after its tests pass:

1. `feat(wlan): support site wlan delete simulation`
2. `feat(ir): carry wireless client ssid evidence`
3. `feat(checks): detect wlan coverage loss for active clients`
4. `test(engine): cover wlan delete impact end to end`
5. `docs: record site wlan coverage loss completion`

If implementation discovers a conflict with `origin/main`, pause and update the plan/spec rather than smuggling in broader behavior.
