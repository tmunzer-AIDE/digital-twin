# Switch L1 physical link-parameter mismatch (speed / duplex / autoneg)

**Status:** PROPOSED
**Date:** 2026-06-25
**Author:** brainstormed with the repo owner

A device PUT that pins switch port `speed` / `duplex` / `disable_autoneg`
currently resolves to **UNKNOWN** (those leaves are unmodeled / not allowlisted).
This spec models L1 physical-layer link parameters and adds a check that flags
the classic, high-impact misconfiguration: a **duplex / autoneg mismatch across a
link** — one end hard-forced while the peer autonegotiates (the auto side falls
back to half-duplex → late collisions and throughput collapse; at 1g+ the link
may not come up at all), or two ends forced to incompatible speed/duplex.

## Program context

Sub-project 2 of 4 in the switch port-config attribute-modeling program
(owner-approved decomposition, 2026-06-25). SP1 (admin port-disable + precedence
rework) is merged. This is SP2 — L1 physical. SP3 (wired auth) and SP4 (mixed /
hard-to-model) follow, each its own spec → plan → implementation cycle.

## Problem

`speed` / `duplex` / `disable_autoneg` are valid on switch ports per the OAS but
the twin models none of them, so any change to them returns UNKNOWN. The
real-world failure they cause is a **duplex mismatch**: it is invisible to
reachability analysis (the link still carries every VLAN and pings succeed) yet
silently destroys throughput — the same class of "looks fine, isn't" bug the MTU
check was built for. The IR has a dormant, unused, mistyped `Port.speed: int`
(the OAS `speed` is a string enum), and no `duplex` / autoneg modeling.

OAS facts (refreshed `device_switch`):
- `port_config` & `local_port_config`: `speed` + `duplex` + `disable_autoneg`.
- `port_config_overwrite`: `speed` + `duplex` only (**no** `disable_autoneg`).
- `port_usages`: `speed` + `duplex` + `disable_autoneg`.
- `speed` enum: `10m,100m,1g,2.5g,5g,10g,25g,40g,100g,auto` (default `auto`).
  `duplex` enum: `auto,full,half` (default `auto`). `disable_autoneg` bool (default
  `false`); meaningful only when `speed` and `duplex` are specified.

## Goals

- A change to `speed` / `duplex` / `disable_autoneg` on a switch port is
  **simulated**, not UNKNOWN.
- A new check `wired.l1.link_param_mismatch` flags incompatible L1 settings
  across a live link boundary, weighted by type and confidence.
- Observed negotiated state (from `port_stats`) **enriches** the config-driven
  conclusion — confirming live symptoms and suppressing pre-existing predictions
  the hardware negotiated around — but never serves as proof of a future
  (post-change) state.
- **No false-SAFE** and **no observed-only noise**: every flagged leaf is modeled
  by the check; observed half-duplex with no delta-attributable config mismatch
  stays silent in v1.

## Non-goals

- Auth / qos / storm-control / mac-limit (SP3, SP4).
- A standalone "observed half-duplex" lint independent of a config-predicted,
  delta-attributable mismatch (deferred — see §6).
- Modeling speed as a routing/throughput quantity (bandwidth-aware checks); this
  is purely a link-establishment / duplex-correctness check.

## Design

### 1. IR modeling (`ir/entities.py`)

Replace the dormant `Port.speed: int | None` and add (mirroring the
config-intent / observed split already used for PoE — `poe` vs `poe_draw`):

- **Config intent** (resolved through the port-config layering):
  - `speed: str | None` — a **concrete** speed enum (`10m…100g`); `None` ⇒ unset
    or `auto`. **IR invariant: `"auto"` is never stored** — ingest normalizes
    config `"auto"` (and absent) to `None`, so `None` is the single
    "not-pinned-to-a-concrete-speed" value.
  - `duplex: str | None` — strictly `"full"` / `"half"`; `None` ⇒ unset or `auto`
    (same normalization — `"auto"` is never stored).
  - `autoneg_disabled: bool = False` — from `disable_autoneg`.
- **Observed** (baseline `port_stats`, canonicalized — see §2):
  - `observed_speed: str | None`, `observed_duplex: str | None` (`full`/`half`).

Because `"auto"`/absent normalize to `None`, the predicates are simply:
- **forced** ⇔ `autoneg_disabled and speed is not None and duplex is not None`
  (per the OAS "meaningful only when speed+duplex set").
- **autonegotiating** ⇔ `config_stated(port) and not forced(port)` — i.e. the
  port has CONFIG facts and is not forced. A port with **no config facts**
  (an AP / LLDP / stat-ensured end — `config_stated(port) is False`) is neither
  forced nor "autonegotiating"; it is an **unknown peer** (handled by the
  `.unverified` path in §3, NOT `.autoneg_mismatch`). This split matters because
  the new fields default to `None`/`False`, so a no-facts peer would otherwise be
  misread as "autonegotiating" and draw the wrong (too-specific) code.

### 2. Ingest (`ingest/ports.py` + `ingest/switch.py`)

- Thread the three config attrs through the resolver layering (as SP1 did for
  `disabled`): add `speed` / `duplex` / `disable_autoneg` to `_USAGE_OVERRIDE_ATTRS`
  (so they apply as inline `port_config` overrides and, via the usage profile,
  from `port_usages`); `_OVERWRITE_ATTRS` gains `speed` + `duplex` **only**;
  `_LOCAL_ATTRS` (= `*_USAGE_OVERRIDE_ATTRS, "disabled"`) picks them up
  automatically. `port_config_overwrite` never carries `disable_autoneg`.
- `ingest/switch.py` builds `Port.speed` / `duplex` / `autoneg_disabled` from the
  resolved effective attrs — **normalizing config `"auto"` and absent to `None`**
  for both `speed` and `duplex` (the IR invariant from §1; `"auto"` is never
  stored), and `observed_speed` / `observed_duplex` from the stat row via a new
  `_l1_observed(row)` helper (sibling to `_poe_draw`):
  - **Speed canonicalizer**: numeric Mbps → config enum string
    (`10→"10m"`, `100→"100m"`, `1000→"1g"`, `2500→"2.5g"`, `5000→"5g"`,
    `10000→"10g"`, `25000→"25g"`, `40000→"40g"`, `100000→"100g"`). Unknown /
    `0` / `None` → `None`.
  - **Up-gating**: observed fields are populated **only when `row["up"] is True`**.
    A down port (`up` false, `speed` 0/None, `full_duplex` false/None) yields
    `observed_speed = observed_duplex = None` — never a spurious `half`.
  - `observed_duplex` = `"full"`/`"half"` from `full_duplex` (bool) when up; else
    `None`.

### 3. New check `wired.l1.link_param_mismatch` (template: `mtu_mismatch`)

Two-ended boundary walk via `BoundaryView(ir, ap_transparent=False)` — L1 exists
on every Ethernet link, so AP uplinks are evaluated and the AP end is an unknown
(no config facts); VC-internal links never fire; baseline parity via the same
`BoundaryView` on the baseline IR.

**Config compatibility** per evaluable boundary `(pa, pb)`. Classify each end as
**forced**, **autonegotiating** (`config_stated and not forced`), or **unknown
peer** (`not config_stated` — no config facts), per §1:

| Ends (config) | Verdict | Code |
|---|---|---|
| both forced, **different speed** | ERROR — no common speed, link won't establish | `.speed_conflict` |
| both forced, same speed, **different duplex** | ERROR — duplex conflict | `.duplex_conflict` |
| one **forced** / one **autonegotiating** | WARNING — duplex-mismatch risk (auto side → half-duplex; 1g+ may not link) | `.autoneg_mismatch` |
| one **forced** / one **unknown peer** (no config facts) | WARNING/MEDIUM — cannot rule out a mismatch | `.unverified` |
| both autonegotiating, both unknown, or forced-identical | silent | — |

The **forced-vs-unknown-peer** row is distinct from forced-vs-autonegotiating:
because the new fields default to `None`/`False`, a no-facts AP/LLDP/stat-ensured
end would naively look "autonegotiating" — the `config_stated` predicate (§1)
routes it to the honest `.unverified` code instead of the over-specific
`.autoneg_mismatch`. (Mirrors `mtu_mismatch`'s explicit-vs-`config_stated` split.)

Severity downgrades to WARNING when confidence is below HIGH (per the
`min_confidence(pa, pb, link)` rail), exactly as `mtu_mismatch`.

**Attribution (delta-conditioned, honest about time):**

- **Introduced / changed mismatch** (the boundary's config mismatch is new, or
  the forced settings were altered by the delta): severity + confidence come from
  **config + link provenance only**. Baseline observed state is **context only** —
  it can neither prove (the new config's negotiated outcome is unobservable
  pre-apply) nor suppress the finding. Observed peer state MAY appear in
  `evidence` as annotation, never in the verdict math.
- **Pre-existing mismatch** (the *same* config mismatch was already live on the
  *same* baseline boundary):
  - if baseline shows **clean negotiation** (both ends `observed_duplex == "full"`
    and matching `observed_speed`) → **suppress** (the hardware negotiated a
    working link; predicted-but-not-real, and unchanged by the delta).
  - else → **INFO context** (pre-existing, not caused by the delta — same as
    `mtu_mismatch.preexisting`); if a baseline end is `observed_duplex == "half"`,
    annotate it as a live symptom in the message/evidence, but it stays INFO
    (does not floor — it is not this delta's doing).
- **Pre-existing `.unverified` suppression** (the `forced`-vs-unknown-peer case):
  when the *same* uncertainty was already live on the *same* baseline boundary —
  same forced end with the same `speed`/`duplex`, same no-config peer — the
  `.unverified` finding is **suppressed entirely** (not even INFO). Mirrors
  `mtu_mismatch.unverified`'s baseline-parity guard; without it, an unrelated
  delta would re-surface stale AP/no-facts uncertainty on every run.

### 4. Field gate (`scope/allowlist.py`)

Allowlist (now safe — the check models them):
- `speed`, `duplex`, `disable_autoneg` on `port_config`, `local_port_config`,
  and `port_usages`.
- `speed`, `duplex` on `port_config_overwrite` (no `disable_autoneg` per OAS).

These propagate through the existing `_DEVICE_PORT_LEAVES` / `_USAGE_LEAVES` /
`EFFECTIVE_ALLOWLIST` / `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"]`
composition. **Force the gate change only after the check exists** (no-false-SAFE
ordering, as SP1).

### 5. L0 (no change)

All three attrs are already in the committed `device_switch` OAS on their
respective maps; the L0 unknown-attribute walker already accepts them.

### 6. Out of scope (deferred)

- **Standalone observed-only finding.** A port `observed_duplex == "half"` with no
  config-predicted, delta-attributable mismatch is **silent** in v1. Real
  fixtures carry half-duplex telemetry on internal/legacy ports (e.g. `cbp0` at
  10m/half); firing on observation alone would surface old quirks during
  unrelated changes. A dedicated observed-health lint can come later.
- Bandwidth/throughput modeling; speed as a capacity quantity.

## Testing

- **Speed canonicalizer / observed up-gating unit**: numeric→enum mapping;
  `up:false` → `observed_* = None`; `up:true, full_duplex:false` → `"half"`;
  unknown speed → `None`.
- **Resolver / IR-normalization unit**: `speed`/`duplex`/`disable_autoneg` resolve
  through port_config / overwrite (speed+duplex only) / local (gated) /
  port_usages, with SP1 precedence intact; **explicit config `"auto"` (and absent)
  normalize to `Port.speed/duplex == None`** — `"auto"` is never stored (guards the
  §1 invariant against a future regression to a concrete value).
- **Check unit** (`tests/checks/test_l1_param_mismatch.py`): forced-vs-forced
  different speed → ERROR; forced same-speed different duplex → ERROR;
  forced-vs-auto → WARNING; both-auto / forced-identical → silent; one-forced
  vs no-facts peer → `.unverified` WARNING (and **forced-vs-no-facts-peer is
  `.unverified`, NOT `.autoneg_mismatch`** — the `config_stated` split);
  confidence downgrade; VC-internal silent; **introduced mismatch with baseline
  observed half does NOT upgrade to HIGH** (time-honesty); **pre-existing mismatch
  + baseline clean negotiation → suppressed**; **pre-existing mismatch (no clean
  obs) → INFO**, half annotated; **pre-existing `.unverified` (same forced end,
  same no-facts peer in baseline) → suppressed** (baseline-parity guard, mirrors
  `mtu_mismatch`).
- **e2e pipeline**: a device PUT pinning a forced speed/duplex on one end of a
  trunk uplink whose peer autonegotiates → not UNKNOWN; `l1.link_param_mismatch.
  autoneg_mismatch` present; verdict REVIEW.
- **Goldens**: run full golden suite; `site.json` has internal half-duplex
  telemetry (`cbp0`) — confirm the no-standalone-observed rule keeps it silent and
  no golden churns. Re-pin only with justification.
- **Full gate** after each task: `uv run pytest -q && uv run ruff check . &&
  uv run mypy src`.

## Risks / open points

- The speed canonicalizer must stay in lockstep with the OAS `speed` enum; an
  unmapped numeric value degrades to `observed_speed = None` (honest, never a
  wrong string).
- Observed enrichment is deliberately conservative (suppress-only for
  pre-existing, context-only for introduced) to avoid both false-HIGH (treating
  stale telemetry as future proof) and observed-only noise.
