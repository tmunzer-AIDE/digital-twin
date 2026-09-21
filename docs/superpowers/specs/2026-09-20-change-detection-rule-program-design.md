# Mist change-detection rule program

**Status:** Active — first slice implemented

## Goal

Improve change-plan precision with deterministic Mist configuration and telemetry
rules. No rule in this program interprets natural-language intent, and no rule
requires an LLM in the simulation loop.

The program keeps the cardinal verdict invariant: missing state, unsupported
semantics, or incomplete telemetry can never produce `SAFE`.

## Deliberate exclusions

- **Natural-language intent/postconditions.** The optional free-text `intent` field
  remains display-only. Existing topology and configuration checks evaluate the
  actual proposed delta. A deterministic intent rule would require a future,
  explicitly structured `expected_effects` contract.
- **A second same-SSID rule.** `wireless.wlan.duplicate_ssid` already detects enabled
  duplicate SSIDs on provably overlapping AP scopes. Future work should resolve
  WxTag membership and enrich the existing finding with auth/VLAN differences.
- **PoE budget prediction.** `wired.poe.disconnect` can prove loss of power to an
  existing device. Predicting a newly attached device's demand requires complete
  switch-budget, per-port draw, and priority telemetry and is deferred until those
  inputs exist.

## Rule 1 — effective overrides

### Contract

Check id: `scope.effective_noop`

A lower-precedence template or site attribute that changes in the proposed raw
configuration but does not change a device's final compiled effective value is
overridden by a higher-precedence layer. This is reported because the requested
configuration will not take effect on that device.

- `scope.effective_noop.fully_overridden`: the path is overridden on every affected
  device.
- `scope.effective_noop.partially_overridden`: the path is overridden on some
  affected devices and applied on others.

Both findings are `WARNING` / `REVIEW`. Neither is a `SAFE` no-op: both indicate
that part or all of the requested change will not be applied as expressed.

Evidence includes exact effective leaf paths plus overridden and applied device
ids. Comparisons are performed on the rolling final state so an override removed
by another operation in the same plan does not produce a false finding.

### Delivery slices

1. **Implemented:** site/template-to-switch comparison against device-level
   overrides.
2. **Next prerequisite:** fetch and compile assigned device profiles in the actual
   precedence stack (`template -> sitetemplate -> site -> device-profile -> device`).
   The same comparison then reports device-profile overrides. Until this lands, the
   existing device-profile gate remains an `UNKNOWN` coverage rail.
3. Extend the lower-layer artifact to gateway-template and gateway-profile paths.

## Prioritized rule backlog

### Implemented

#### `wireless.wlan.auth_transition` — 2026-09-20

- Model secured-to-open transitions without treating the companion secret deletion
  from an auth-root replacement as unrelated unsupported churn.
- Reuse `resolve_wlan_usage(..., window_days=7)`.
- Secure to open with recent sessions: `ERROR` / `UNSAFE`.
- Secure to open with no recent sessions: `WARNING` / `REVIEW`.
- Missing usage telemetry: partial coverage / `REVIEW`.
- Other auth transitions remain deferred until their field-level semantics are
  modeled explicitly.
- `wireless.wlan.open_guest` remains the stronger finding for open WLANs without
  client isolation.

The implementation deliberately admits only secret companions deleted by a
secured-to-open whole-root replacement. Pure secret edits and other auth-root
transitions stay behind the field gate until their semantics are modeled.

### P0 — implemented 2026-09-20

#### `wired.auth.radius_missing`

Evaluate assigned 802.1X/MAB port profiles only. At least one RADIUS server or
Mist NAC authenticator must remain. Missing/unresolved authenticator state is
`REVIEW`; removing the last authenticator with recent authenticated-client evidence
may escalate to `UNSAFE`.

Implemented as a delta-conditioned IR lint over concrete assigned ports. Backend
presence is secret-free; unresolved server rows produce partial coverage. Failed-auth
telemetry is not yet available, so the V1 conclusion is `REVIEW`.

#### `nac.rule.access_impact`

This complements, rather than duplicates, `nac.rule.shadowed`. Shadowing proves
that a later rule is unreachable; access impact evaluates changed effective policy.

- V1: apply seven-day usage evidence to updates, action changes, and deletes.
- V2: replay recent NAC client attributes through baseline and proposed ordered
  rules to detect allow-to-deny, deny-to-allow, broadened matches, narrowed matches,
  changed fall-through, and overlapping non-superset reorder effects.

V1 is implemented. A recently used allow rule that is deleted, disabled, or changed
to block is `UNSAFE`; other recently used semantic changes are `REVIEW`. V2 replay
remains future work.

#### `gateway.wan.redundancy`

Removing one of multiple viable WAN paths is `REVIEW`; removing the last viable
path is `UNSAFE`. Health, VPN use, routing preference, and available bandwidth are
evidence inputs. Missing health/usage evidence prevents `SAFE`.

V1 models configured gateway ports with `usage=wan` and admin state. Path health,
VPN use, preference, and bandwidth are not yet observed, so path additions and
redundancy reductions retain partial coverage.

#### `routing.bgp.prefix_delta`

Model advertised-prefix additions, withdrawals, overlaps, and VRF placement.
Structural changes are `REVIEW`; withdrawal of a confirmed sole advertisement or
an unintended confirmed leak may become `UNSAFE`.

V1 parses explicit literal CIDRs from the BGP export selector, normalizes additions,
withdrawals, and overlaps, and escalates a sole withdrawal only when the baseline
peer was established. Named export policies and VRF placement remain partial.

#### `config.referenced_update_impact`

Replace blanket referenced-object `REVIEW` outcomes by expanding dependents and
simulating their effective changes. Initial families: device profiles, services,
service policies, VPNs, RF templates, and security profiles. Incomplete reference
discovery remains `REVIEW`/partial and can never yield `SAFE`.

V1 expands exact dependents and reports stable paths/identities. The provider does
not yet return complete dependent bodies, so effective recompilation is explicitly
partial rather than implied.

#### `config.batch_integrity`

Evaluate cross-object invariants against the final rolling plan: references must
resolve, related objects must be present, one object must have one final mutation,
and create/update/delete combinations must not leave dangling dependencies.

Implemented across the envelope and configuration-policy paths: duplicate final
mutations are rejected, create/update payload references are checked against batch
deletes, and known inbound references are evaluated against the final plan.

### P1 — implemented 2026-09-20

#### `wired.dhcp.capacity`

Compare the proposed usable pool with observed leases/clients and reserved space.
Below current demand is `UNSAFE`; low headroom is `REVIEW`; `SAFE` requires complete
lease/demand evidence.

#### `routing.static_route_reachability`

Detect unreachable next hops, recursive loops, removal of a sole route, and
more-specific blackholes. Live RIB evidence may escalate structural findings.

#### `routing.vrf_leak`

Evaluate import/export and route-target changes for unintended inter-VRF reachability
or loss of intended reachability, particularly guest, corporate, management, and VPN
boundaries.

#### `security.service_policy_semantics`

Detect broad `any -> any` permits, unreachable/shadowed policy rules, conflicting
actions, missing referenced services, and order changes that alter effective policy.

#### `switch.lag_redundancy`

Differentiate removal of one healthy AE/LAG member from loss of the final forwarding
member. Also detect inconsistent LACP mode and aggregate membership across peers.

#### `wired.control_plane_reachability`

Detect loss of a device's effective path to Mist cloud, DNS, NTP, RADIUS, TACACS,
or syslog after VLAN, VRF, source-interface, gateway, or route changes.

#### `wireless.rf_coverage_regression`

Evaluate RF-template assignments, band disablement, channel width, transmit power,
and minimum basic rates against AP placement and observed client capabilities.
Incomplete RF/client evidence floors the result to `REVIEW`.

#### `wired.storm_control_policy`

Detect shutdown-on-trigger on AP/uplink ports and thresholds below observed normal
broadcast, multicast, or unknown-unicast levels. Without traffic telemetry the rule
is a policy-floor `REVIEW`, not a guessed `UNSAFE`.

## Common acceptance matrix

Every rule must test:

1. an introduced harmful condition;
2. a valid change that must not be flagged;
3. a pre-existing condition unaffected by the delta (`INFO` only);
4. incomplete or stale evidence (`REVIEW`/`UNKNOWN`, never `SAFE`);
5. a multi-operation plan where the final rolling state repairs or creates the issue;
6. exact `caused_by`, subject, affected entities, and stable evidence keys.
