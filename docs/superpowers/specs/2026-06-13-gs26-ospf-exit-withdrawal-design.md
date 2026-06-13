# GS26 — OSPF exit withdrawal (design)

Date: 2026-06-13
Status: approved (both-unified detection; dedicated `OspfIntf`; tiered floors;
leaf-tightened allowlist with a `.transit_mutation` REVIEW backstop)
Roadmap mapping: closes ROADMAP §2 "OSPF exit withdrawal (GS26)".
Foundation for GS27 (OSPF transit changes) and GS28 (BGP).

## Problem

A routed segment's reachability *beyond its L3 device* depends on route
distribution (OSPF), which `wired.l3.gateway_gap` does not model:
`gateway_gap.removed` fires only when the **IRB itself** is deleted. Withdraw
the segment from OSPF and the IRB stays — local inter-VLAN routing still works —
but its prefix stops being advertised, and/or the device loses the OSPF-learned
default it used to egress. The segment becomes an island beyond the box while
every existence check stays green: a false-SAFE.

The twin has **no RIB**. It cannot prove OSPF was the only path (a static
default, redistribution, a second area, BGP — all invisible). So GS26 detects
the **structural removal of modeled OSPF participation**, never real
reachability, and floors verdicts to match that uncertainty (the same epistemic
stance as `gateway_gap` and `wired.dhcp.path`).

### Boundary vs GS27

GS26 owns **withdrawal**: an OSPF interface leaves OSPF (network removed from
all areas, area removed) or OSPF is disabled (`ospf_config.enabled` → false).
GS27 will own **mutation of retained transit config** (metric, area type,
timers, live adjacency telemetry). The one overlap — an `active→passive` flip
that *collapses a device's last adjacency* — is functionally a withdrawal
(the adjacency is gone), so GS26 claims it. Every other retained-interface
mutation that GS26 allowlists but does not yet model is held at REVIEW by the
`.transit_mutation` floor (below) so nothing GS27 owns can resolve SAFE.

## OSPF config shape (Mist / Junos)

Participation is expressed by a network **name** appearing under an area, gated
by a device/site master switch:

```
ospf_config: { enabled: bool, areas: {...}, import_policy, export_policy, ... }
ospf_areas: {
  "<area>": {                       # area key = number 0-255 or IP
    type: "default|nssa|stub",
    include_loopback: bool,
    networks: {
      "<network_name>": {           # KEY joins to networks.<name> -> vlan_id/subnet
        passive: bool,              # default false; true = advertise-only, no adjacency
        metric, auth_type, auth_*, interface_type, hello/dead/bfd intervals, ...
      }
    }
  }
}
```

- `ospf_config.enabled` defaults **false** — absent/false ⇒ the areas are inert,
  there is no participation.
- `passive: false` (default) ⇒ the interface forms adjacencies = adjacency-bearing
  (transit/uplink). `passive: true` ⇒ stub advertisement only.
- Configurable at **device** and **networktemplate/site_setting** level; both
  merge into the effective config (`test_merge` confirms `ospf_areas` survives the
  site merge).
- The **live org carries `ospf_areas: {}`** (empty) — GS26 goldens are synthetic
  and all eight live plans are unaffected (expected for the routing tier).

## IR addition — `OspfIntf` (new entity)

`src/digital_twin/ir/entities.py`, mirroring `L3Intf`:

```python
@dataclass(frozen=True)
class OspfIntf:
    device_id: str
    vlan_id: int | None          # None when the network name does not resolve
    area: str
    network_name: str
    passive: bool = False        # default false = adjacency-bearing
    unresolved: bool = False     # name did not resolve to a vlan -> blind
    meta: FactMeta = CONFIG_META
    id: str = ""                 # f"{device_id}:ospf:{area}:{network_name}"
```

`active` is a derived predicate: `not passive`. `unresolved=True` is the OSPF
analog of vlan-blind port carriage — the participation exists but cannot be tied
to a segment. Added to `IRBuilder` (`add_ospf_intf`, `ospf_intfs` tuple) and
registered as IR diff entity kind `"ospf_intf"`.

**Validation** (`_validate_ospf_intfs`, mirroring the role-aware precedents
`_validate_dhcp_scopes`/`_validate_wlan_reqs`, not merely `_validate_l3intfs`'s
device-existence — the check trusts these fields for collapse, clients, and
affected-segment computation):
- `device_id` must reference a real device whose role is **SWITCH** (GS26 is
  switch-scoped — see device scope below);
- a non-`None` `vlan_id` must reference a **minted `Vlan`** (a resolvable OSPF
  network name is always minted as a `Vlan` by the same `networks` ingest; a
  miss here is an ingest bug worth surfacing);
- the `unresolved ⇔ vlan_id is None` invariant (unresolved rows carry no vlan;
  resolved rows carry one).

`OspfIntf.id` carries `area`+`network_name` for stable identity and messaging,
**but the check never compares by `id`** — see the participation tuple below.

## Device scope — switches only (M1 boundary)

The post-fetch field gate (`scope/field_gate.py:screen_op`) rejects any `device`
op whose fetched `type != "switch"` ("AP/gateway devices are out of scope" in
M1). So a **device-level gateway** OSPF change cannot reach a check — it is
rejected at the gate as UNKNOWN regardless of any allowlist. GS26 therefore
models OSPF on **switch devices only**: ingest mints `OspfIntf` in the switch L3
pass, validation requires the SWITCH role, and the goldens use switch `device`
ops (and/or `site_setting` ops, which are not role-gated). Gateways run OSPF too,
but modeling gateway OSPF would imply a device-op path the M1 boundary forbids;
it is deferred (see out of scope). The live org has empty `ospf_areas`, so this
scope costs no live coverage.

## Ingest — mint from effective config

A new `_ospf` pass in `adapters/mist/ingest/switch.py`, run for **switch
devices only** (in the switch L3 branch). Reuses the existing `net_of`/`vlan_of`
network-name namespace resolution; a name that does not resolve to a vlan mints
an `unresolved=True` row (the only switch-side blindness here — `l3_unmodeled` is
set on **gateways** only, so it never applies to a switch-scoped `OspfIntf`). No
new capability — `SwitchIngester.ingest` already returns `{WIRED_L2, L3_EXITS}`
whenever device data is fetched.

```
ospf_cfg = eff.get("ospf_config") or {}
if not ospf_cfg.get("enabled"):        # absent/false/falsey -> no participation (silent)
    return
for area, area_cfg in (eff.get("ospf_areas") or {}).items():
    for name, net_cfg in ((area_cfg or {}).get("networks") or {}).items():
        passive = bool((net_cfg or {}).get("passive", False))
        vid = vlan_of(name)            # org/site namespace; None if unresolvable
        add_ospf_intf(OspfIntf(
            device_id=did, vlan_id=vid, area=str(area), network_name=str(name),
            passive=passive, unresolved=(vid is None)))
```

- `enabled` truthy is required; OSPF is optional, so its absence is silent, never
  a blind spot (the never-served-vlan doctrine).
- Unresolvable name ⇒ `unresolved=True` participation (vlan_id None) so the check
  can ABSTAIN if a withdrawal touches it — never a silent miss, never a guess.

## Compile carry-through (the GS21 `_DEVICE_OWN_FIELDS` gotcha — required)

`compile/switch.py` merges template+site, then applies device overlays only for
`_DEVICE_DICT_MERGE_FIELDS` and `_DEVICE_OWN_FIELDS`. `ospf_config`/`ospf_areas`
are in **neither**, so a **device-level** OSPF block is silently dropped today
(exactly the bug that bit `stp_config.bridge_priority` in GS21). Site/template-
level OSPF already survives via `merge_only`.

Fix: add `"ospf_config"` and `"ospf_areas"` to `_DEVICE_OWN_FIELDS` (wholesale
device override, matching `stp_config`). Documented limitation: a device that
defines OSPF overrides the inherited OSPF wholesale (no per-area merge) — fine
for the synthetic goldens; the live org has no OSPF to inherit. An ingest test
pins that device-level OSPF survives compile.

## Allowlist — leaf-tightened, two leaves only

The allowlist's contract (`allowlist.py` docstring, `paths.changed_leaf_paths`):
removed/added subtrees are **descended into**, gating each leaf on its own.
Allowlisting a leaf the IR does not act on lets an out-of-scope change simulate
as falsely in-scope → SAFE. So GS26 allowlists **only** the two leaves it models
and acts on:

```python
_OSPF_LEAVES = (
    "ospf_config.enabled",
    "ospf_areas.*.networks.*.passive",
)
```

Added to `RAW_ALLOWLIST["device"]`, `RAW_ALLOWLIST["site_setting"]`, and
`EFFECTIVE_ALLOWLIST`. Everything else stays **denied → UNKNOWN**:

- `metric`, `interface_type`, `hello/dead/bfd` timers, `import/export_policy`,
  `no_readvertise_to_overlay` — unmodeled; a change touching them is honest
  out-of-scope. GS27 can model `metric` later **without inheriting a false-SAFE
  hole**.
- `ospf_areas.*.type`, `include_loopback` — area semantics unmodeled.
- `auth_type`/`auth_keys`/`auth_password` — unmodeled (and sensitive).

Consequence (pinned, accepted): withdrawing an OSPF network that **also** carries
any denied leaf (e.g. `metric`) surfaces that denied leaf as a removed path →
the whole plan resolves **UNKNOWN**, not REVIEW. So goldens withdraw entries with
**no denied leaves** — either a passive stub (`{"passive": true}`) or a **bare
`{}` active/transit** entry (the default-active shape, since `passive` defaults
false).

**Bare-`{}` active withdrawal is fully in-scope — not a gap.** Such a removal
surfaces **zero** raw changed leaves (`changed_leaf_paths` finds none), so the
field gate raises no offense and *passes* — but the engine still applies the op
(`effective_update`/`apply` drop the network from the proposed object), proposed
ingest drops the `OspfIntf`, and the IR diff registers the removal, so the check
fires. **Detection rides the IR diff, never the raw-leaf count** (`pipeline.py`
has no empty-diff short-circuit). Because `passive` defaults false, real active
transit entries *are* bare `{}`, so a REQUIRED test pins this end-to-end:
default-active participation must never false-SAFE.

## Check — `wired.l3.ospf_withdrawal` (domain `wired.l3`)

- `requires() = {WIRED_L2, L3_EXITS}` — the core consumes `ir.l3intfs` (to know a
  segment is routed) and `ir.ospf_intfs`; without them, INSUFFICIENT_DATA.
- `applies_to(diff) = diff.touches("ospf_intf")` — no OSPF entities anywhere ⇒
  NOT_APPLICABLE.

### Semantic participation (the comparison key — NOT `OspfIntf.id`)

Reduce each side's `ospf_intfs` to per-segment participation, comparing by the
**semantic tuple**, never by `id`:

```
# membership for withdrawal/collapse:
participates[(device_id, vlan_id)] = OR of (active over its OspfIntf rows)
# identity for mutation:
tuple = (device_id, vlan_id, frozenset(areas for that device+vlan), active_status)
```

`active_status` normalizes absent==`passive:false` (both active). A pure
**network-name rename** (same `device`, `vlan`, `area`, `active_status`) leaves
the tuple unchanged ⇒ **silent** (its real impact is owned by the name-aware
checks). An **area-move** changes the area set ⇒ caught by `.transit_mutation`,
never a false withdrawal.

A "routed segment" = a vlan with `subnet`/an IRB `L3Intf` (the same routed-intent
signal `gateway_gap` uses).

### Findings (three codes)

1. **`.egress_lost`** — device D's adjacency-bearing set went **non-empty →
   empty** in proposed (last adjacency removed, OSPF disabled, **or** an
   `active→passive` flip that collapses it). D has no modeled dynamic egress.
   Affected = routed segments that had OSPF participation on D in baseline (the
   collapsed transit interface **and** the now-islanded stubs at D). One finding
   per collapsed device.
   - any affected segment has **observed clients** → Severity.ERROR / HIGH →
     **UNSAFE** (the only modeled dynamic egress removed, with evidence of
     impact — the `dhcp.path`/`poe` escalation pattern).
   - otherwise → Severity.WARNING / MEDIUM → **REVIEW**.

2. **`.advertised_removed`** — a routed segment's `(device, vlan)` participation
   is **fully withdrawn** (present in baseline, absent in proposed) while its
   device keeps adjacency (or it was a passive stub). Prefix no longer
   distributed → reachability-to-segment concern. Severity.WARNING / MEDIUM →
   **REVIEW**. Suppressed for any segment already named by `.egress_lost` (no
   double-report).

3. **`.transit_mutation`** (the deferred-mutation REVIEW floor) — a `(device,
   vlan)` present in **both** baseline and proposed whose semantic tuple changed
   in `active_status` **or** `area` set, when no `.egress_lost`/
   `.advertised_removed` already owns it. Severity.WARNING / MEDIUM → **REVIEW**.
   Message: "OSPF participation for vlan V on D changed (passive/area); transit &
   area-semantics impact deferred to GS27." GS27 replaces this coarse backstop
   with precise modeling. Pure rename ⇒ no tuple change ⇒ not emitted.

## Honesty rails

| Situation | Behavior |
|---|---|
| Segment never in OSPF (baseline), unchanged | **silent** — OSPF is not the only route mechanism; static/BGP/external invisible (never-served-vlan doctrine) |
| Network added to OSPF | **no finding** — additions are not breakage |
| Pure network-name rename (tuple unchanged) | **silent** — owned by name-aware checks |
| Retained participation, `active_status`/area changed, no withdrawal | **`.transit_mutation` → REVIEW** — never SAFE for a GS27-owned mutation |
| OSPF network name `unresolved` and relevant to the delta (the only switch-side blindness) | cap finding to MEDIUM + **PARTIAL** abstain note; never silent, never a guessed UNSAFE |
| Clients unfetched (no `CLIENTS_ACTIVE` in both IRs) | `.egress_lost` stays **REVIEW** + PARTIAL note "client census unavailable — egress impact unconfirmed"; never a confident UNSAFE over missing facts (the `dhcp.path` gate) |
| Unchanged OSPF elsewhere | never taints — diff-gated on `ospf_intf` / the enable flip |
| Denied OSPF leaf changed (metric/type/auth/timers) | **UNKNOWN** — honest out-of-scope (field gate), never reaches the check |

Confidence is config-sourced (HIGH) for the UNSAFE `.egress_lost`-with-clients
case; the REVIEW codes carry MEDIUM. Unresolved-name and clients-unfetched caps
are applied as PARTIAL coverage, mirroring `gateway_gap` and `dhcp.path`. (A
gateway `l3_unmodeled` cap does not arise here — GS26 is switch-only; it returns
with gateway OSPF, see out of scope.)

## Goldens (filed under GS26) + ingest tests

- **GS26-a** — passive stub removed from its area; device keeps another active
  adjacency → `.advertised_removed` **REVIEW**.
- **GS26-b** — a **bare `{}` active** transit interface (default-active, no
  leaves) removed = device's last adjacency; an islanded stub at the device has
  observed clients → `.egress_lost` **UNSAFE**. This is the in-scope bare-`{}`
  active-withdrawal case (zero raw leaves, detected via the IR diff).
- **GS26-c** — `ospf_config.enabled` true→false with observed clients on a routed
  segment → `.egress_lost` **UNSAFE**.
- **GS26-d** (control) — a network *added* to OSPF + an untouched never-in-OSPF
  segment → **SAFE** (additions/unrelated never fire).
- **GS26-e** (mutation/blind) — (i) `active→passive` flip that does **not**
  collapse the last adjacency → `.transit_mutation` **REVIEW**; (ii) a withdrawal
  whose OSPF network name is `unresolved` (does not resolve to a vlan) → **REVIEW
  + PARTIAL**, never silent/UNSAFE.
- **Ingest units** — `enabled` gates participation (false ⇒ none); `passive`
  parsed; unresolvable name ⇒ `unresolved=True`; an `active→passive` flip
  collapsing the last adjacency is detected via proposed-IR active-status (not
  only removals); device-level OSPF survives compile (the carry-through fix).
- **Field-gate / engine units** — `metric`-only change ⇒ denied/UNKNOWN;
  withdrawing a `metric`-bearing entry ⇒ UNKNOWN; withdrawing a `passive: true`
  stub ⇒ passes the gate; **a bare-`{}` active withdrawal ⇒ zero raw changed
  leaves, the field gate passes, and end-to-end the `OspfIntf` is dropped and the
  check fires (default-active must not false-SAFE).**
- **Diff unit** — an `active→passive` flip alone marks the `ospf_intf` modified;
  a rename changes `id` but not the `(device, vlan)` membership.
- **Live verification** — all eight plans hold their verdicts (live `ospf_areas`
  is empty; no OSPF participation exists).

## Out of scope (recorded, not built)

- GS27 OSPF transit precision: `metric`/area-`type`/timer modeling, transit-
  interface identification, live adjacency telemetry — `.transit_mutation` holds
  these at REVIEW until then. `metric` stays denied so GS27 adopts it cleanly.
- **Gateway OSPF** — gateways run OSPF, but device-level gateway ops are
  rejected by the M1 field-gate role check (`screen_op`); modeling gateway OSPF
  would imply a device-op path that does not exist. Deferred until the M1
  device-role boundary is revisited (or via `site_setting`-only withdrawals).
- GS28 BGP (`bgp_config` is absent from the committed `device_switch` OAS
  snapshot — refresh when GS28 lands).
- Redaction network-name joins (ROADMAP §5, still 🟡 — deferred this round).
- Per-area device OSPF merge (device OSPF overrides inherited OSPF wholesale).
