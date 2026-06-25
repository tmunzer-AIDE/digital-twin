# Switch port admin-disable + port-config precedence rework

**Status:** PROPOSED
**Date:** 2026-06-25
**Author:** brainstormed with the repo owner

A device PUT that administratively disables switch ports via
`port_config_overwrite` (or `local_port_config`) currently resolves to
**UNKNOWN** — `out-of-scope raw path changed: port_config_overwrite.*.disabled
(not in the M1 allowlist)`. That is *honest* today (the twin does not ingest the
inline `disabled` boolean, so it refuses rather than false-SAFE), but disabling
a port is a high-impact, common operation that the twin should *simulate*, not
punt on. This spec makes admin port-disable a first-class, modeled operation —
and, as a necessary foundation, fixes the port-config layer-precedence resolver
to match real Mist semantics.

## Program context (decomposition)

This is **Sub-project 1 of 4** in the broader "fully model the switch
port-config / port-config-overwrite / local-port-config attribute surface" effort
(owner-approved decomposition, 2026-06-25). Each sub-project is its own
spec → plan → implementation cycle:

1. **(this spec) Admin port-disable (`disabled`) + precedence foundation** — the
   reported bug; impact ripple already modeled; lays the resolver foundation the
   rest depend on.
2. L1 physical — `speed`, `duplex`, `disable_autoneg` (two-ended link-param
   mismatch check).
3. Wired auth — `port_auth`, `enable_mac_auth`, `mac_auth_*`,
   `allow_multiple_supplicants`, `bypass_auth_*`, `persist_mac`, `reauth_interval`,
   `server_fail/reject_network`, `dynamic_vlan_networks`, `guest_network` (heavy
   org-NAC overlap; gets its own decomposition).
4. Mixed / hard-to-model — `voip_network`, `inter_switch_link` (modelable);
   `mac_limit`, `storm_control`, `enable_qos` (likely recognized→REVIEW, no
   faithful reachability model). Model-vs-REVIEW decided per attr at that time.

Sub-projects 2–4 are out of scope here.

## Problem

`resolve_effective_ports` (`adapters/mist/ingest/ports.py`) layers a port's
effective config as: named `port_usages` profile ← inline `port_config` attrs ←
`local_port_config` (merged into the base in `resolve_port_bases`) ←
`port_config_overwrite` (applied last). This is wrong in three ways relative to
real Mist behavior, and one way relative to the reported bug:

1. **Precedence order.** Mist applies `local_port_config` as the **highest**
   precedence layer, *above* `port_config_overwrite` — not below it.
2. **The `no_local_overwrite` gate is ignored.** `local_port_config` overrides a
   port **only when** that port's `port_config` entry has
   `no_local_overwrite == false`. The OAS default is `true`, so by default
   `local_port_config` is **discarded**. The current resolver always merges it.
3. **Member-set drop.** The resolver iterates members from
   `port_config ∪ local_port_config` only. A port present **only** in
   `port_config_overwrite` (the reported case: `disabled: true` on
   `mge-0/0/0..3`, ports that need not be individual `port_config` keys) is
   silently dropped — so the naïve "add `disabled` to `_OVERWRITE_ATTRS`" would
   not even fix the bug.
4. **Inline `disabled` not ingested.** `Port.disabled` is sourced only from the
   `usage: "disabled"` system usage, never from the inline `disabled` boolean on
   `port_config_overwrite` / `local_port_config`.

The impact machinery already exists: `Port.disabled` ("admin-down: forwards
NOTHING") removes the port's link from the L2 graph
(`representations/l2_graph.py:112`), so a disabled trunk/uplink already strands
downstream segments via `wired.l2.blackhole`. We are wiring the *input*, not
inventing the consequence.

## Goals

- A device PUT that disables ports via `port_config_overwrite` or
  `local_port_config` is **simulated**, not UNKNOWN — including the
  overwrite-only-member case from the bug report.
- The port-config precedence resolver matches real Mist: `port_config` base →
  `port_config_overwrite` → `local_port_config` (highest, per-member, gated on
  `no_local_overwrite`).
- Admin port-disable is **surfaced** as a finding, weighted by blast radius:
  AP-connected and trunk / inter-switch ports and ports with active wired clients
  floor REVIEW (or UNSAFE); a bare edge port is INFO context.
- **No false-SAFE.** Every newly in-scope leaf is either modeled by a check or
  is not allowlisted. `no_local_overwrite` itself stays UNKNOWN for v1 (see §4).

## Non-goals

- Modeling `speed`/`duplex`/auth/qos/etc. (sub-projects 2–4).
- Surfacing port **re-enable** (`disabled: true → false`) — low value, deferred.
- A general "recognized-but-unmodeled → REVIEW" gate tier (considered, not built).

## Design

### 1. Resolver precedence rework (`ingest/ports.py`)

Restructure `resolve_effective_ports` into explicit layers, applied
lowest → highest precedence per member:

1. Named `port_usages` profile, resolved from the **effective usage name**.
2. Inline `port_config` override attrs.
3. `port_config_overwrite` attrs.
4. `local_port_config` attrs — applied **last (highest)**, per-member, **only if
   locally-overridable**.

- **Locally-overridable predicate** (per member): the member is **absent** from
  `port_config`, **or** `port_config[member].no_local_overwrite == false`.
  `no_local_overwrite` defaults to `true` (OAS) → local discarded by default. A
  standalone `local_port_config` entry (no `port_config` member to protect)
  applies. *(Documented assumption, owner-confirmed; narrow edge.)*
- **Effective usage name** = the highest-precedence layer that sets `usage`:
  `local_port_config` if it applies and sets `usage`, else `port_config`.
  `port_config_overwrite` carries no `usage` (OAS) and never reassigns usage.
- **Member set = `port_config ∪ port_config_overwrite ∪ local_port_config`.** An
  overwrite-only or local-only member yields a `Port`; when no usage resolves,
  `resolution = "none"` (existing case) and only the layer's inline attrs apply.
- **Per-layer attr eligibility.** `disabled` is honored on the
  `port_config_overwrite` and `local_port_config` layers only (not `port_config`,
  per OAS). Reorganize `_USAGE_OVERRIDE_ATTRS` / `_OVERWRITE_ATTRS` so each layer
  copies exactly its OAS-valid + IR-modeled attrs; `disabled` joins the
  overwrite + local layers.

### 2. `Port.disabled` from the inline boolean (`ingest/switch.py`)

Once the resolver places `disabled` into a port's effective attrs (from the
overwrite/local layers), switch ingest constructs `Port.disabled` from it (it
already reads resolved attrs). Net: `Port.disabled` reflects **any** path —
`usage: "disabled"` (today) or the inline boolean (new). No new IR field.

### 3. New check `wired.port.admin_disable`

Modeled on `wired.poe.disconnect` (same template: baseline-classified,
delta-attributed, confidence/severity rails, crash-isolated).

- `id = "wired.port.admin_disable"`, `domain = "wired.port"`,
  `requires() = {WIRED_L2}`, `applies_to(diff) = diff.touches("port")`.
- **Iterate proposed ports** (NOT `base_ir.ports` — the overwrite-only-member
  case may have *no* baseline `Port`, so a baseline-driven loop like
  poe_disconnect's would skip it and read SAFE/contextless, breaking the bug-fix
  promise). A port is **newly disabled** when `prop.disabled is True and
  (base_port is None or base_port.disabled is False)` (source-agnostic — catches
  the inline boolean *and* `usage:"disabled"` reassignment). Pre-existing-disabled
  (`base_port.disabled is True`) and re-enable: not flagged.
- Classify on the **baseline** IR; severity = max over applicable conditions:

  | Condition (baseline) | Detection | Severity → verdict |
  |---|---|---|
  | AP-connected | `_ap_uplink_ports(base_ir)` (peer is AP) | **ERROR only when the port↔AP uplink tie is HIGH-confidence** (config / bidirectional LLDP); a MEDIUM/one-sided tie → WARNING — observed wireless clients raise *consequence*, not the *tie* confidence → UNSAFE/REVIEW |
  | Non-AP managed LLDP peer (inter-switch/gateway) **or** `mode is TRUNK` | base links with a non-AP managed peer; `base_port.mode` | WARNING → REVIEW |
  | Active wired clients on the port | `clients_by_port(base_ir)[pid]` non-empty | WARNING → REVIEW |
  | Baseline `Port` missing (prop-only port) with **no** tied baseline evidence | `base_ir.ports[pid]` absent AND no baseline link/client for `pid` | INFO → context (blast radius unattributable) |
  | Bare edge (none of the above) | — | INFO → context |

  When the baseline `Port` is missing but baseline evidence *does* tie a
  device/client to `pid` (a baseline link endpoint or `clients_by_port`), classify
  by that evidence using the rows above rather than defaulting to INFO.
- Complementary to `wired.l2.blackhole`: this flags the **action** (port shut
  down); blackhole flags the **consequence** (downstream stranded). Both may
  fire on the same delta.
- Attribution via `ctx.delta_index.cause("port", pid)`. **Registration (full
  local pattern):** import the class in `checks/wired/__init__.py`, append
  `AdminDisableCheck()` to `ALL_WIRED_CHECKS`, add the class name to `__all__`,
  **and bump the hard-coded `len(ALL_WIRED_CHECKS) == 20 → 21` in
  `tests/test_public_api.py`** (else a trivial red gate).
- **Confidence rail (matches `decide()`):** `decide()` floors UNSAFE on any
  network ERROR *before* confidence is consulted, so ERROR is emitted **only when
  the exact impact evidence is HIGH** — the port↔peer tie is HIGH-confidence
  (config / bidirectional LLDP). A MEDIUM/one-sided tie → WARNING (→ REVIEW),
  never ERROR, even with observed wireless clients on the AP. Bare-edge and
  baseline-missing → INFO. Mirrors poe_disconnect's `_BLIND_INTENT` caps.

### 4. Field gate (`scope/allowlist.py`)

- Add `disabled` to `_OVERWRITE_LEAVES` (`port_config_overwrite.*.disabled`) and
  `_LOCAL_PORT_CONFIG_LEAVES` (`local_port_config.*.disabled`). **Not**
  `_PORT_CONFIG_LEAVES` (`port_config` has no `disabled` in the OAS — a
  `port_config.*.disabled` change stays correctly flagged). These flow through
  `_DEVICE_PORT_LEAVES` into the `device` raw allowlist, `EFFECTIVE_ALLOWLIST`,
  and `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"]` automatically.
- **`port_config.*.no_local_overwrite` is NOT allowlisted (stays UNKNOWN for
  v1).** A lone `no_local_overwrite: true → false` flip activates pre-existing
  `local_port_config` leaves; if any are unmodeled (e.g. `speed`), real Mist
  behavior changes something the **derived gate cannot see** (it compares only
  modeled effective leaves) → false-SAFE. So a `no_local_overwrite` change must
  remain UNKNOWN (human review). The resolver still **reads** the flag internally
  to gate local application (correctness) — reading ≠ allowlisting.
  - *Future enhancement (deferred):* allowlist it **only** behind a guard that
    proves every `local_port_config` leaf the flip would activate for that member
    is modeled.

### 5. L0 (no change)

`disabled` already exists on `local_port_config` / `port_config_overwrite` in the
committed `device_switch` OAS, and `no_local_overwrite` on `port_config`. The L0
unknown-attribute walker already accepts them. No OAS or L0 changes.

## Testing

- **Resolver unit** (`tests/adapters/mist/test_ports.py` or sibling): per-member
  precedence order (local above overwrite when allowed); `no_local_overwrite`
  default-discard; explicit-`false` applies; standalone-local applies;
  overwrite-only `disabled` member yields a disabled `Port` (the bug-report
  shape); usage reassignment via local respects the gate.
- **Check unit** (`tests/checks/wired/test_admin_disable.py`): AP-connected with
  HIGH tie → ERROR/UNSAFE; AP-connected with MEDIUM/one-sided tie → WARNING even
  with observed wireless clients (never ERROR); trunk and non-AP-peer →
  WARNING/REVIEW; active wired client → WARNING/REVIEW; bare edge → INFO/context;
  **prop-only port with no baseline `Port` and no tied baseline evidence → INFO**
  (the overwrite-only-member shape — must not be skipped); prop-only port WITH
  tied baseline evidence → classified by that evidence; pre-existing-disabled and
  re-enable not flagged; baseline classification (peer present in baseline, not
  proposed).
- **Public API** (`tests/test_public_api.py`): bump `len(ALL_WIRED_CHECKS) == 20
  → 21` as part of registration (currently exactly 20).
- **e2e pipeline** (`tests/engine/test_pipeline.py`): the exact reported payload
  (`port_config_overwrite.{mge-0/0/0..3}.disabled = true`) → no longer UNKNOWN;
  `admin_disable` fires (+ `l2.blackhole` if it strands anything); verdict
  REVIEW/UNSAFE per blast radius.
- **Goldens:** re-pin affected switch goldens; add a golden for the bug-report
  scenario if one fits the existing fixture set.
- **Full gate** after each task: `pytest && ruff check . && mypy src`.

## Risks / open points

- The standalone-local-applies assumption (§1) is a narrow Mist edge; documented
  and owner-confirmed. If it proves wrong in practice, it only affects local-only
  ports with no `port_config` entry.
- `admin_disable` and `l2.blackhole` both firing is intended (action vs
  consequence) — verify the rendering does not read as double-counting.
