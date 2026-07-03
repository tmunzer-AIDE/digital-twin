# Port-profile attribute completion — curated SAFE + REVIEW coverage

**Status:** PROPOSED
**Date:** 2026-06-29
**Author:** brainstormed with the repo owner

## Summary

After #31-#34, the twin can already distinguish hard-UNKNOWN from coverage gaps,
and common cosmetic fields such as `description` no longer block a simulation.
This spec is the next small usability pass: finish the remaining switch
port-profile attributes so common port-profile edits can produce **SAFE**, while
non-cosmetic knobs produce a specific **REVIEW** reason instead of an unhelpful
field-gate gap.

The design is intentionally MVP-biased:

- **Curated SAFE** for fields we choose to treat as non-connectivity metadata or
  timing labels in M1.
- **Recognized→REVIEW** for admission, PoE-priority, STP, PVLAN, and runtime
  protection knobs whose real impact is not modeled yet.
- **No broad wildcard.** Every added leaf is explicit and map-scoped.

## Implementation baseline

Build on `origin/main` at or after:

```text
515693c fix(snooping): scope PARTIAL coverage to delta-introduced abstentions (#34)
```

The baseline already includes:

- #31: `description` is allowlisted as cosmetic on inline port maps.
- #32: `critical` is allowlisted as inert metadata; `no_local_overwrite` is
  modeled and guarded by `_local_overwrite_ripple`.
- #33: field-gate leaf gaps are coverage gaps in the single-site path.
- Existing `PortAuth` + `wired.auth.access_change`.
- Existing `PortMisc` + `wired.port.unmodeled_change` for
  `inter_switch_link`, `storm_control`, and currently `enable_qos`.

## Goals

- A common port-profile edit that changes only curated benign leaves can return
  **SAFE**: checks run, no coverage gap is emitted, no IR diff wakes a check.
- No known switch port-profile attribute falls into an unclassified UNKNOWN/gap
  bucket merely because the field is recognized but not yet modeled.
- Preserve the no-false-SAFE boundary for potentially impactful knobs by routing
  them to explicit REVIEW findings.
- Keep the design small: no PVLAN graph, no STP topology model, no RADIUS outage
  state machine, no PoE budget calculation.

## Non-goals

- Predicting PVLAN isolation, VSTP/STP convergence, or storm-control drops.
- Predicting RADIUS server outage state or auth retry timing.
- Predicting PoE budget shedding from priority ordering.
- Turning all unknown OAS leaves into SAFE. Unknown or unclassified leaves still
  remain coverage gaps.

## Attribute classification

### Existing benign leaves

These already exist on `main` and stay benign:

| attribute | current treatment |
|---|---|
| `description` | SAFE/cosmetic on `port_config`, `local_port_config`, `port_config_overwrite` |
| `critical` | SAFE/inert alarm label on `port_config` |

### New or reclassified SAFE leaves

These leaves are explicitly allowed by the raw and effective gates, but do **not**
enter the IR and do **not** wake any check. A delta containing only these leaves
can therefore resolve SAFE.

| attribute | maps | MVP rationale |
|---|---|---|
| `ui_evpntopo_id` | `port_usages` | UI/topology presentation id only |
| `enable_qos` | `port_usages`, `local_port_config` | Scheduling/classification knob; for M1 operator usability, not treated as reachability risk |
| `poe_keep_state_when_reboot` | `port_usages`, `port_config_overwrite` | Reboot behavior only; not a live config-change connectivity prediction |
| `server_fail_retry_interval` | `port_usages` | RADIUS retry timing only; M1 does not model outage-time behavior |

**Validation gate for `ui_evpntopo_id`:** the current OAS inventory describes it
as a UI helper for selecting a port profile as the ESI-LAG between distribution
and access switches. That is only SAFE if it is truly a presentation/selection id
and the actual EVPN/ESI-LAG forwarding config lives in other modeled or reviewed
fields. Implementation task 1 must validate this against the refreshed OAS/docs
or a live-derived config shape. If `ui_evpntopo_id` materially changes EVPN/ESI-LAG
membership, it moves from the SAFE table to the REVIEW table before coding.

**Important product choice:** `server_fail_retry_interval` can affect timing
during a RADIUS outage, but it does not select who is admitted or which VLAN they
land on. For M1, classifying it SAFE is an explicit usability tradeoff so
routine port-profile edits do not floor REVIEW. If a future RADIUS-outage model
exists, this can move into the auth REVIEW surface.

**Implementation consequence for `enable_qos`:** today it lives in `PortMisc` and
`wired.port.unmodeled_change`, which makes it REVIEW. Spec 1 moves it out of
that IR/check path. Keep it allowlisted as benign, but remove it from:

- `PortMisc`
- `_port_misc`
- `_MISC_ATTRS`
- `_MODELED_USAGE_ATTRS` (then re-add it through the benign allowlist group)
- `wired.port.unmodeled_change` evidence/tests

### REVIEW leaves

These become or remain explicit REVIEW, not SAFE.

| attribute | route | rationale |
|---|---|---|
| `bypass_auth_when_server_down_for_voip` | add to `PortAuth`; surfaced by `wired.auth.access_change` | admission behavior during RADIUS outage for voice clients |
| `poe_priority` | recognized by `wired.port.unmodeled_change` | power-shed policy without a PoE budget model |
| `community_vlan_id` | recognized by `wired.port.unmodeled_change` | PVLAN membership/isolation not modeled |
| `inter_isolation_network_link` | recognized by `wired.port.unmodeled_change` | PVLAN/inter-isolation semantics not modeled |
| `stp_required` | recognized by `wired.port.unmodeled_change` | STP policy/topology impact not modeled |
| `stp_no_root_port` | recognized by `wired.port.unmodeled_change` | STP root/path constraints not modeled |
| `stp_p2p` | recognized by `wired.port.unmodeled_change` | STP link-type behavior not modeled |
| `use_vstp` | recognized by `wired.port.unmodeled_change` | VSTP behavior not modeled |
| `inter_switch_link` | already in `PortMisc`; keep REVIEW | isolation/trunk semantics not fully modeled |
| `storm_control` | already in `PortMisc`; keep REVIEW | runtime traffic protection/drop behavior not modeled |

`bypass_auth_when_server_down_for_voip` does not need a new check class. It is an
auth admission-policy knob. Adding it to `PortAuth` makes an effective change
surface through `wired.auth.access_change.policy_change`. A future enhancement may
add VOIP-specific evidence when the port has `voice_vlan` clients, but Spec 1 only
requires the generic REVIEW floor. Preserve the existing `PortAuth` invariant:
`Port.auth is None` only when the whole auth surface is default. Therefore
`bypass_auth_when_server_down_for_voip` defaults to `False`, and a lone
`False → True` flip must produce a non-default `PortAuth` so the auth check wakes.

`poe_priority` intentionally starts as flat REVIEW. A real check would require
switch PoE budget, observed draw, all powered ports, and priority ordering. That
is follow-up work, not Spec 1.

## OAS refresh and map placement

The current committed schemas are inconsistent for the newest leaves: the switch
device schema exposes more attributes than `site_setting` / `networktemplate`.
The **first implementation task** is to refresh the committed OAS extracts from
the latest source used by the attribute inventory, then derive the allowlist
placement from that refreshed OAS. The table below is the expected inventory,
not an authority that overrides the schema.

Expected placement after refresh:

| attribute | `port_config` | `port_config_overwrite` | `local_port_config` | `port_usages` |
|---|---:|---:|---:|---:|
| `ui_evpntopo_id` | no | no | no | yes |
| `enable_qos` | no | no | yes | yes |
| `poe_keep_state_when_reboot` | no | yes | no | yes |
| `server_fail_retry_interval` | no | no | no | yes |
| `bypass_auth_when_server_down_for_voip` | no | no | no | yes |
| `poe_priority` | no | no | no | yes |
| `community_vlan_id` | no | no | no | yes |
| `inter_isolation_network_link` | no | no | no | yes |
| `stp_required` | no | no | no | yes |
| `stp_no_root_port` | no | no | yes | yes |
| `stp_p2p` | no | no | yes | yes |
| `use_vstp` | no | no | yes | yes |

If the refreshed OAS disproves any row, stop and update this spec before coding
the gate. The placement tests must assert the refreshed OAS-derived facts, not
freeze a stale guess from this draft.

## Field and derived gate design

The allowlist needs two distinct concepts:

- **Modeled/reviewed leaves** — enter IR and wake checks.
- **Benign SAFE leaves** — are allowed by the gates but deliberately ignored by
  IR and checks.

Do not put benign leaves into a tuple whose documentation says "IR consumes
these leaves". Introduce a small explicit group such as:

```python
_BENIGN_PROFILE_USAGE_ATTRS = (
    "ui_evpntopo_id",
    "enable_qos",
    "poe_keep_state_when_reboot",
    "server_fail_retry_interval",
)
```

and corresponding map-scoped leaves:

- `port_usages.*.<benign>`
- `local_port_config.*.enable_qos`
- `port_config_overwrite.*.poe_keep_state_when_reboot`

These benign leaves must be present in both:

- raw field gate allowlists (`RAW_ALLOWLIST[...]`)
- effective derived allowlist (`EFFECTIVE_ALLOWLIST`)

so they do not become coverage gaps after #33. They must not be read by ingest.
In particular, `enable_qos` must move from `_MODELED_USAGE_ATTRS` to this benign
group. Leaving it in `_MODELED_USAGE_ATTRS` makes that tuple's "IR consumes these"
contract false; dropping it without re-adding it here turns the change back into a
coverage gap instead of SAFE.

Reviewed usage leaves have a **third gate**: the device-profile override gate.
The current code builds `_USAGE_LEAVES` from `_MODELED_USAGE_ATTRS` and uses that
same `_USAGE_LEAVES` inside
`DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"]`. That coupling is
load-bearing: if a profile can override a reviewed leaf but the device-profile
gate does not know that leaf, a below-profile edit on a profiled switch can
resolve REVIEW or SAFE when the honest answer is UNKNOWN. Therefore:

- Put every reviewed `port_usages` leaf that enters `PortMisc` into the modeled
  usage surface that feeds `_USAGE_LEAVES` and the switch device-profile
  overridable list. In the current code this is `_MODELED_USAGE_ATTRS`, exactly
  like `inter_switch_link` and `storm_control`.
- `bypass_auth_when_server_down_for_voip` enters through `_AUTH_ATTRS`, which is
  already folded into `_MODELED_USAGE_ATTRS`.
- If the refreshed OAS forces a split between usage and local placement, the
  invariant is unchanged: reviewed usage leaves must be present in raw
  allowlist, effective allowlist, and the device-profile overridable list; benign
  SAFE leaves stay out of the device-profile modeled-leaf surface.

For REVIEW leaves, keep using explicit IR surfaces:

- Extend `_AUTH_ATTRS` / `PortAuth` / `_port_auth` with
  `bypass_auth_when_server_down_for_voip`.
- Extend the existing `PortMisc` value object with the new recognized→REVIEW
  knobs. Keep the public check id `wired.port.unmodeled_change`; this is the
  same "recognized but not impact-modeled" surface, just with more fields.
  Extend `adapters/mist/ingest/switch.py:_port_misc` to read every reviewed misc
  leaf from the resolved usage dict; that is the usage-level path that actually
  creates a `PortMisc` diff. Separately, add only the local-capable reviewed
  leaves from the refreshed OAS (currently `use_vstp`, `stp_p2p`, and
  `stp_no_root_port`) to `adapters/mist/ingest/ports.py:_MISC_ATTRS`, so
  `local_port_config` can contribute those fields to the effective usage. Do not
  add usage-only leaves such as `poe_priority`, `community_vlan_id`,
  `inter_isolation_network_link`, or `stp_required` to `_MISC_ATTRS` unless the
  refreshed OAS says they exist on `local_port_config`. `storm_control` still
  needs the existing digest; scalar additions such as `community_vlan_id`,
  `poe_priority`, and STP booleans keep their values with stable None/default
  handling, not blanket `bool()` coercion. `_port_misc` must continue returning
  `None` when all misc fields are default, so benign-only or absent-misc deltas
  do not wake `wired.port.unmodeled_change`.
- Keep `inter_switch_link` and `storm_control` in the same REVIEW path.
- Remove `enable_qos` from the REVIEW path.

### `no_local_overwrite` ripple coupling

#32 added `_local_overwrite_ripple` because a `no_local_overwrite` flip can
activate or deactivate an existing `local_port_config` entry wholesale. The
current comment names exactly three soon-to-be-reviewed local leaves:
`use_vstp`, `stp_p2p`, and `stp_no_root_port`. Spec 1 changes their treatment:

- Update the stale comment so it no longer calls those three leaves unmodeled.
- A `no_local_overwrite` flip over a local entry containing only newly reviewed
  STP leaves must yield REVIEW via the `PortMisc` diff, not SAFE and not a
  coverage gap.
- If the refreshed OAS still exposes any remaining truly unmodeled
  `local_port_config` leaf, a `no_local_overwrite` flip over that leaf must still
  produce a field-gate coverage gap. If none remain after Spec 1, the ripple
  guard becomes dormant but stays in place for future local leaves; do not delete
  it just because the current concrete examples moved to `PortMisc`.

## Check behavior

### SAFE leaves

For a delta that changes only SAFE leaves:

- field gate passes;
- derived gate emits no coverage gap;
- IR diff is empty or contains no check-relevant change;
- all applicable checks pass or are not applicable;
- verdict can be SAFE with normal high-confidence complete-coverage wording.

### Auth REVIEW

`bypass_auth_when_server_down_for_voip` changes produce
`wired.auth.access_change.policy_change` with WARNING/MEDIUM-style REVIEW, matching
the existing auth policy floor. No new check is required.

### Port-profile REVIEW

`wired.port.unmodeled_change` remains the generic REVIEW carrier for recognized
but unmodeled port-profile knobs. Update its evidence/message from the current
"inter_switch_link / storm_control / enable_qos" wording to the broader group,
for example:

```text
port <pid>: poe_priority, use_vstp changed — port-profile behavior not modeled (review)
```

It remains WARNING-only. It never returns ERROR/UNSAFE.
Also update the check confidence reason from "the changed knob has no modeled
connectivity impact" to "the changed knob's impact is not modeled"; STP/PVLAN
knobs can have real connectivity impact, the twin simply does not model it yet.

## Testing requirements

### Allowlist / OAS placement

- Assert every SAFE and REVIEW leaf is allowlisted only on the maps documented in
  the refreshed OAS-derived placement table.
- Assert leaves are not accidentally allowed on `port_config` when the OAS says
  they do not exist there.
- Assert OAS unknown-key validation accepts the refreshed leaves.
- Assert `ui_evpntopo_id` is either proven cosmetic and SAFE, or moved to REVIEW
  before implementation proceeds.
- Assert reviewed usage leaves appear in the switch device-profile overridable
  surface. Pin at least one concrete regression: a below-profile `poe_priority`
  edit on a device-profiled switch resolves UNKNOWN, not REVIEW or SAFE.

### SAFE behavior

End-to-end site simulations should prove each SAFE bucket can produce SAFE when
changed alone:

- `port_usages.*.ui_evpntopo_id`
- `port_usages.*.enable_qos`
- `local_port_config.*.enable_qos`
- `port_usages.*.poe_keep_state_when_reboot`
- `port_config_overwrite.*.poe_keep_state_when_reboot`
- `port_usages.*.server_fail_retry_interval`

Assertions must include:

- `verdict.decision is Decision.SAFE`
- no `coverage.gap` finding
- no `wired.port.unmodeled_change` finding for `enable_qos`
- `config_diffs` still show the changed leaf

### REVIEW behavior

End-to-end site simulations should prove:

- `bypass_auth_when_server_down_for_voip` changes produce
  `wired.auth.access_change.policy_change` and REVIEW.
- `poe_priority` changes produce `wired.port.unmodeled_change.recognized` and
  REVIEW.
- PVLAN and STP leaves produce `wired.port.unmodeled_change.recognized` and
  REVIEW.
- Existing `inter_switch_link` and `storm_control` REVIEW tests still pass.

### Regression pins

- A truly unknown/unclassified port leaf still becomes a field-gate coverage gap,
  not SAFE.
- If a concrete unmodeled local leaf remains after the OAS refresh, a
  `no_local_overwrite` flip over it still produces a coverage gap; do not hide it
  behind the new benign list. If no such leaf remains, keep this as a
  dormant-backstop unit around `_local_overwrite_ripple` rather than inventing a
  fake OAS leaf.
- A `no_local_overwrite` flip over local `use_vstp` / `stp_p2p` /
  `stp_no_root_port` now produces REVIEW through `wired.port.unmodeled_change`,
  proving the old ripple backstop was replaced by an explicit reviewed surface.
- Mixed deltas obey precedence: a SAFE leaf plus a REVIEW leaf returns REVIEW; a
  SAFE leaf plus a modeled UNSAFE returns UNSAFE.

## Follow-up specs

- **PVLAN impact model:** `community_vlan_id` and
  `inter_isolation_network_link` can move from REVIEW to modeled SAFE/UNSAFE once
  the twin has a PVLAN graph.
- **STP/VSTP policy model:** `stp_required`, `stp_no_root_port`, `stp_p2p`, and
  `use_vstp` can move from REVIEW to precise STP risk findings.
- **RADIUS outage behavior:** `server_fail_retry_interval` and
  `bypass_auth_when_server_down_for_voip` can become more precise if the twin
  models RADIUS availability/fail-open state.
- **PoE budget model:** `poe_priority` can become a real risk check when switch
  budget and all powered draw/priority inputs are available.
