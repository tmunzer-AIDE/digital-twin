# GS28 — BGP adjacency break

**Status:** Implemented — 2026-06-23 (11 tasks, subagent-driven, full gate green).
Live regression verify: the test org has zero live BGP — `searchSiteBgpStats`
returns cleanly EMPTY (no 404, unlike OSPF's `ospf_peers/search`), so
`bgp_neighbors` lands in `state_meta.fetched`, `BGP_TELEMETRY` is earned on a
genuine empty fetch, and no spurious BGP findings arise. The neighbor-record
field mapping is grounded by `test_gs28_bgp_neighbor_field_mapping_real_shape`
(re-confirm against a live paste when a BGP-bearing org is reachable).
Two cross-cutting fixes landed during implementation: `scope/paths.py` gained a
`**` allowlist token (one-or-more segments) used ONLY in the two BGP neighbor
patterns (IP-keyed neighbor paths contain literal dots); `*` stays EXACTLY-one so
no other domain over-matches — the final whole-branch review caught that a global
greedy `*` had over-matched real gatewaytemplate leaves
(`dhcpd_config.*.options.*.type`, `port_config.*.wan_source_nat.disabled`) →
false-SAFE; fixed + regression-tested off the committed OAS. `compile/switch.py`
`_DEVICE_OWN_FIELDS` gained `bgp_config` so a device-level switch BGP edit is
simulable (was a latent device-op false-SAFE), symmetric with OSPF.
**Date:** 2026-06-23
**Author:** brainstormed with the repo owner
**Builds on:** the GS27 OSPF pattern (`wired.l3.ospf_withdrawal`, `OspfNeighbor`,
escalate-only telemetry) — see `docs/superpowers/specs/2026-06-22-gs27-ospf-transit-changes-design.md`.

## Goal

Make a **BGP config change simulable** on switches **and** gateways, and surface a
peering break before it ships. Two halves, exactly mirroring GS27's doctrine:

1. **Structural** (config-delta, no RIB): a removed / disabled / AS-changed /
   type-changed / transport-changed BGP peering → **REVIEW**; a newly added peering
   → REVIEW (additions are never silently SAFE).
2. **Telemetry** (`org_bgp`/`site_bgp` peers): a session-breaking change whose
   neighbor IP was an **established** BGP peer in **baseline** → **UNSAFE**.
   **Escalate-only** — telemetry only adds/raises findings, never produces SAFE.

**Why simpler than GS27:** a BGP neighbor is keyed by its **IP literally in config**
(`bgp_config.<session>.neighbors.<neighborIP>`), so the telemetry join is a *direct
established-peer-set membership* — no subnet prediction, no reachability module, no
area gate. GS27's hardest/blindest part has no BGP analogue.

## Scope

- **Switch + gateway BGP** (both roles). Switch BGP flows via `site_setting` /
  `networktemplate`; gateway BGP via `gatewaytemplate`. A direct **device op on a
  gateway** stays UNKNOWN (M1 field-gate switch-only role check — out of scope; GS28
  does not change it). Gateway BGP is reached via gatewaytemplate.
- **Peer/session safety only.** Advertised-prefix (`networks`) checking is deferred
  → `networks` is **denied** (an advertised-route edit → UNKNOWN), so it can never
  resolve SAFE without a check.
- `auth_key` (the one BGP secret) is **denied** — a secret edit → UNKNOWN, never
  simulated.

## Grounded facts (live Mist, 2026-06-23)

- **`bgp_config` shape** (switch `site_setting`, gateway `gatewaytemplate`): a map
  `bgp_config.<sessionName>.{ type (external|internal), local_as, networks[],
  import/export_policy, hold_time, bfd_*, auth_key(SECRET),
  neighbors.<neighborIP>.{ neighbor_as(req), disabled, import/export_policy,
  hold_time, multihop_ttl } }`. Switch required: `type`,`local_as`; per-neighbor:
  `neighbor_as`. **No `vrf` inside `bgp_config`** (VRF is sibling `vrf_instances`).
- **Gateway = strict superset:** adds `via` (lan|tunnel|vpn|wan — the defining
  gateway field), `tunnel_name`/`vpn_name`/`wan_name`, `no_readvertise_to_overlay`,
  neighbor `tunnel_via`. `disabled` exists on neighbors of **both** roles.
- **`bgp_config` is already in the committed OAS** (`site_setting.schema.json`,
  `gatewaytemplate.schema.json`) → switch-via-site_setting and gateway-via-
  gatewaytemplate edits pass L0. (Roadmap's "no bgp_config in the OAS" worry was the
  per-device switch schema only — verified + thin-schema'd at build if needed.)
- **No live BGP** in the reachable org (`org_bgp`/`site_bgp` empty) → telemetry is
  regression-only; the neighbor-record field shape is grounded from a pasted live
  record (the GS27 approach). The OSPF record precedent: lowercase `state`, `mac`,
  `peer_ip`, `vrf_name`, `up`, no `area`.

## Architecture (Approach A — GS27-parity, BGP-specific)

One role-agnostic check over role-aware `BgpPeer` entities + a `BgpNeighbor`
telemetry entity. No shared "routing core" abstraction (premature over two
instances; the joins genuinely differ).

```
ir/entities.py        BgpPeer (role-aware) + BgpNeighbor (observational)
ir/model.py           IR.bgp_peers / bgp_neighbors / bgp_telemetry_unparsed_count + setters
ir/diff.py            bgp_peer in _ENTITY_KINDS; _IGNORED_BY_KIND["bgp_peer"]={"session_name"};
                      bgp_neighbor NOT in _ENTITY_KINDS
ir/capabilities.py    BGP_TELEMETRY
adapters/mist/ingest/switch.py          role-aware _bgp pass (switch + gateway_effective) + _as_int
adapters/mist/ingest/bgp_neighbors.py   NEW self-isolating BgpNeighborIngester
adapters/mist/adapter.py                register BgpNeighborIngester
scope/allowlist.py    bgp leaves on switch (site_setting/networktemplate/device) + _GATEWAY_LEAVES (+via)
providers/base.py     RawSiteState.bgp_neighbors
providers/mist_api.py _bgp_neighbors (org_bgp/site_bgp, fail-soft)
observability/replay/store.py + FixtureProvider   bgp_neighbors save/load
checks/wired/bgp_adjacency.py            NEW wired.l3.bgp_adjacency
```

## Section 1 — IR, ingest, allowlist

### `BgpPeer` (config, diff-bearing)
Grain: one per `(device, session, neighbor_ip)`; **identity = `(device_id, neighbor_ip)`**
(`session_name` is grouping, not identity — a session rename / neighbor-moves-session-
with-same-attrs is silent).
```python
@dataclass(frozen=True)
class BgpPeer:
    device_id: str
    role: DeviceRole                  # SWITCH | GATEWAY (role-aware messaging/scope)
    session_name: str                 # bgp_config map key — display only (ignored in diff)
    neighbor_ip: str                  # peer IP — identity + the direct telemetry join key
    local_as: int | None = None
    neighbor_as: int | None = None
    session_type: str | None = None   # "external" | "internal" (None if absent OR unparseable)
    disabled: bool = False            # per-neighbor admin shutdown (schema default False)
    via: str | None = None            # gateway transport lan|tunnel|vpn|wan (None if absent OR unparseable)
    # Raw tokens when a value is PRESENT-but-unparseable (templated {{var}} / non-enum / non-bool),
    # carried DIFF-BEARING so `absent (None) -> "{{x}}"` does NOT collapse and the check can emit a
    # coverage note instead of misclassifying (the GS27 metric false-SAFE scar tissue). Every
    # modeled value-leaf gets one:
    local_as_unresolved: str | None = None
    neighbor_as_unresolved: str | None = None
    session_type_unresolved: str | None = None   # raw token if `type` not in {external,internal}
    via_unresolved: str | None = None            # raw token if `via` not in {lan,tunnel,vpn,wan}
    disabled_unresolved: str | None = None       # raw token if `disabled` is non-boolean/templated
    unresolved: bool = False          # True when the neighbor-IP map key is NOT a literal IP
    ambiguous: bool = False           # True when 2+ sessions defined this (device, neighbor_ip)
    meta: FactMeta = CONFIG_META
    id: str = ""                      # f"{device_id}:bgp:{neighbor_ip}"
```
**Identity collision guard (collision-preserving, like `Vlan.collisions`):** because
`IR.bgp_peers`/`diff_ir` index by `BgpPeer.id` (= `device:bgp:neighbor_ip`), a naive
last-win would *hide* a duplicate before the check sees it. So the **ingest/dedup
step detects the collision**: if the same `(device, neighbor_ip)` is produced by 2+
sessions whose **modeled attributes differ** — the comparison key is the full set
`(local_as, local_as_unresolved, neighbor_as, neighbor_as_unresolved, session_type,
session_type_unresolved, via, via_unresolved, disabled, disabled_unresolved)`,
i.e. admin-state and every unresolved token included — they collapse into **one**
`BgpPeer` with `ambiguous=True` (never last-win). `ambiguous` is a diff-bearing fact
(a config becoming/ceasing-to-be ambiguous is a change). The check, seeing a
delta-touched `ambiguous` peer, emits a PARTIAL note and skips precise compare. (No
`vrf` in `bgp_config`, so VRF-scoped identity `(device, vrf, neighbor_ip)` is a noted
deferred refinement if real configs reuse a peer IP across VRFs.)

### Ingest — role-aware `_bgp`
For each device (switch **or** gateway) with effective `bgp_config`, walk
`bgp_config.<session>.neighbors.<ip>` → mint one `BgpPeer` per neighbor, tagged with
`Device.role`. A `disabled` neighbor is still minted (so a disabled↔enabled flip is
detectable). ASNs parsed by `_as_int` (int or, when present-but-unparseable, the raw
token into `*_as_unresolved`; absent → both None). `type`/`via` are validated against
their enum sets: a valid value → `session_type`/`via`; a present-but-non-enum value
(templated `{{...}}` / garbage) → the raw token into `session_type_unresolved`/
`via_unresolved` with the parsed field left `None`; absent → both `None` (so
`absent → "{{type}}"` produces a diff, not a collapse). A non-literal-IP neighbor key →
`unresolved=True` (`neighbor_ip` = the raw key), never dropped.
**IMPLEMENTATION REQUIREMENT (build-verified):** gateway peers MUST be minted from
the gateway's true **`gateway_effective`** config (materialized onto the gateway raw
device), never from `site_effective`. A dedicated gateway golden proves this E2E.

### Allowlist (leaf-tightened — model exactly the break-relevant leaves)
- Switch (shared across `site_setting`/`networktemplate`/`device`):
  `bgp_config.*.local_as`, `bgp_config.*.type`,
  `bgp_config.*.neighbors.*.neighbor_as`, `bgp_config.*.neighbors.*.disabled`.
- Gateway (added to `_GATEWAY_LEAVES` → gatewaytemplate / GATEWAY_EFFECTIVE_ALLOWLIST /
  device-profile-gateway slice): the same four **+ `bgp_config.*.via`**.
- **DENIED** (→ UNKNOWN): `auth_key` (secret), `networks` (advertised prefixes — no
  v1 check), and all timers/policies/bfd/multihop/graceful_restart/etc. Honest
  corner: removing a neighbor that carried `auth_key` → the auth_key removal is a
  denied leaf → UNKNOWN (conservative, never false-SAFE).

## Section 2 — structural codes

An **active peering** = present AND `disabled=False`. For each `(device, neighbor_ip)`
(skipping ambiguous/unresolved → coverage notes), all codes **WARNING → REVIEW,
MEDIUM** (`_UNVERIFIED` — no RIB; config-certain change, impact unconfirmed):

| code | trigger | escalates? |
|---|---|---|
| `.peering_removed` | active in baseline, entry **gone** in proposed | yes |
| `.peering_disabled` | active in baseline, present `disabled:true` in proposed | yes |
| `.peering_added` | not-active in baseline (absent or disabled), active in proposed | **no** |
| `.as_changed` | retained active peering whose `local_as` or `neighbor_as` changed | yes |
| `.session_type_changed` | retained active peering, `type` external↔internal (both sides resolved) | yes |
| `.transport_changed` | retained active **gateway** peering, `via` changed (both sides resolved) | yes |

- `.peering_added` fires only with **literal identity** (neighbor IP a literal IP AND
  ASN not templated-unresolved); a fuzzy-identity add → the unresolved/AS-unresolved
  note instead of a confident added finding. Additions never escalate.
- `.session_type_changed` / `.transport_changed` fire only when **both** sides'
  `type`/`via` are resolved; if either side carries `session_type_unresolved` /
  `via_unresolved` and the token changed, emit the type/transport-unresolved note
  (below) instead of a confident change finding (don't misclassify a templated value).
- `.as_changed` evidence carries `local_as_changed` / `neighbor_as_changed` booleans +
  base/proposed values (so an agent knows which ASN moved). `.as_changed` and
  `.session_type_changed` **co-fire** on the same peer (separate break causes).
- `caused_by` names the changed `bgp_peer` via `delta_index`.

Relevance-scoped **coverage notes** (→ REVIEW, never silent, never UNSAFE) — one per
unresolved value-leaf, so a templated value can never collapse to a confident SAFE/
classification:
- **ambiguous** `(device, neighbor_ip)` (`ambiguous=True` — 2+ sessions, differing attrs);
- **unresolved** neighbor-IP key (templated/non-literal) when delta-touched;
- **AS-unresolved**: a retained peering whose `local_as`/`neighbor_as` token is
  templated and the token changed → can't compare (the GS27 metric-unresolved guard);
- **type/transport-unresolved**: a retained peering whose `session_type`/`via` token
  is templated/non-enum (`session_type_unresolved`/`via_unresolved` set) and changed →
  can't decide → note;
- **admin-state-unresolved**: a `disabled` value that is templated/non-boolean
  (`disabled_unresolved` set) and changed → can't decide active-ness → note.

## Section 3 — telemetry: `BgpNeighbor` + escalate-only direct-IP escalation

### `BgpNeighbor` (observational, GS27-`OspfNeighbor` parity)
```python
@dataclass(frozen=True)
class BgpNeighbor:
    device_id: str
    peer_ip: str
    state: str = ""                   # raw BGP state, e.g. "Established"
    up: bool | None = None            # raw liveness flag when the payload carries one
    neighbor_as: int | None = None
    vrf: str | None = None
    meta: FactMeta = OBSERVED_META
    id: str = ""                      # f"{device_id}:bgpnbr:{peer_ip}"
```
Minted from `org_bgp`/`site_bgp` (exact fields confirmed against the Mist OAS at
build — likely `mac`→device, `peer_ip`/`neighbor`, `state`, `neighbor_as`,
`vrf_name`, `up`). **Non-diff-bearing**, **non-load-bearing** (no validation),
**self-isolating `BgpNeighborIngester`** (never flips `report.ok`); earns
**`BGP_TELEMETRY`** only on fetch-success; carries `bgp_telemetry_unparsed_count`.
**`is_established(n) = (n.up is True) or (n.state.strip().lower() == "established")`** —
both `up` and `state` are represented so a payload that conveys liveness via the
boolean (not the string) still escalates; a missed `up` would *under*-escalate a
live session. Exact up-state string confirmed at build (GS27's lowercase `"full"`
surprise).

### Escalation (escalate-only; baseline telemetry)
**No reachability computation, no pure module.** The structural findings *are* the
breaks; baseline telemetry confirms which were live:
`established_baseline = {(n.device_id, n.peer_ip) for n in base_ir.bgp_neighbors if
is_established(n)}` (only when **baseline** carries `BGP_TELEMETRY`). For each
**session-breaking** finding (removed / disabled / as_changed / session_type_changed /
transport_changed) on `(device, neighbor_ip)`, if that pair ∈ `established_baseline`
→ build it **ERROR / UNSAFE / HIGH**, naming the peer; **aggregate** all confirmed
peers per owning finding (`evidence["broken_peers"]` list + `baseline_state`,
`baseline_neighbor_as`, `vrf`). Message wording: *"BGP peer X (established in
baseline) — this change is session-breaking; the peering would drop."* (telemetry
confirms baseline liveness; the model predicts the break — never claims post-change
observation). `.peering_added` never escalates.

**No `.peer_unreachable` backstop** (unlike GS27): every BGP break has a structural
config owner by construction — there is no ownerless-break case.

### Relevance-scoped notes (→ REVIEW, never UNSAFE)
- **telemetry-blind**: `BGP_TELEMETRY` absent/unparsed AND a **session-breaking**
  finding exists (NOT `.peering_added` — telemetry absence doesn't lower add
  confidence) → PARTIAL note "BGP telemetry unavailable — confirmed-break detection
  blind for the changed peering(s)";
- **established peer not in modeled config**: a live `BgpNeighbor` with no config
  `BgpPeer`, on a **delta-touched device** → coverage note (the twin is blind for it),
  relevance-scoped to delta-touched devices only.

### Never-false-SAFE spine (identical doctrine to GS27)
Structural REVIEW floor is telemetry-independent (`requires()=frozenset()`,
`applies_to("bgp_peer")` is the sole gate; `BGP_TELEMETRY` never required); telemetry
is escalate-only (adds/raises, never SAFE, never removes); absent/unparsed → REVIEW
floor + note. Since the escalation is a direct established-set membership (no
prediction), there is essentially no blind-build risk in it.

## Section 4 — applicability, verdict matrix, edge cases

- **`applies_to = diff.touches("bgp_peer")`** — no vlan/subnet broadening.
- **`requires() = frozenset()`** — applies_to gates; the IR is `ok` or the run is
  UNKNOWN before the check, so the check never sees an incomplete IR.

| condition | sev | conf | status |
|---|---|---|---|
| session-breaking code, no live peer | WARNING | MED | REVIEW |
| session-breaking code, **baseline-established** peer | ERROR | HIGH | UNSAFE |
| `.peering_added` | WARNING | MED | REVIEW |
| ambiguous / unresolved-IP / AS- / type-transport- / admin-state-unresolved (delta-touched) | — | — | PARTIAL note |
| telemetry-blind **and** a session-breaking finding | — | — | PARTIAL note |
| established peer not in modeled config (delta-touched device) | — | — | PARTIAL note |
| `auth_key` / `networks` / timer edit | — | — | **UNKNOWN** (field gate denies) |

**Edge cases:** `disabled` absent → False (schema default); `disabled`/`type`/`via`
templated → the matching `*_unresolved` token + coverage note (no collapse, no
misclassification); `absent → "{{type}}"`/`"{{transport}}"` yields a diff (not None==None);
`.as_changed` + `.session_type_changed` co-fire; switch `via`=None (LAN implicit → no
`.transport_changed` for switches); gateway-only peering minted from `gateway_effective`
(not `site_effective`); telemetry liveness via `up==True` with empty/odd `state` still
escalates.

## Testing

- **Check unit tests:** each of the 6 codes; ambiguity guard (2 sessions, same IP,
  differing attrs incl. admin-state → one `ambiguous` peer + note, no last-win);
  unresolved neighbor-IP note; AS-unresolved templated-change note; **type/transport-
  unresolved note** (`absent → "{{type}}"` and `via → "{{transport}}"` both yield a
  diff + note, never collapse/misclassify); admin-state-unresolved note; per-code
  telemetry escalation (removed/disabled/as/type/transport → UNSAFE with established
  peer); **`up==True` with empty `state` escalates**; aggregation (2 broken peers →
  both named); `.peering_added` does NOT escalate; telemetry-blind note (session-
  breaking only, not added); established-peer-not-in-config note;
  `applies_to("bgp_peer")` precision; `disabled` default-False.
- **Self-isolating ingester test:** malformed telemetry → `report.ok` stays True,
  `BGP_TELEMETRY` earned, `bgp_telemetry_unparsed_count` reflects dropped rows
  (incl. partial).
- **Ingest tests:** switch BGP minted from site_setting; **gateway BGP minted from
  gateway_effective**; templated ASN → `*_as_unresolved` token; non-literal-IP key →
  `unresolved`.
- **Goldens** (end-to-end via `simulate`): switch peering removed → REVIEW + telemetry
  UNSAFE; AS change → REVIEW; peering added → REVIEW; **a dedicated gateway-only BGP
  golden** (gatewaytemplate edit → BgpPeer on the gateway → finding) proving the
  `gateway_effective` path E2E; `auth_key` edit → UNKNOWN; `networks` edit → UNKNOWN.
- **Real `org_bgp`-shape grounding test** from a pasted live BGP record (the GS27
  approach), pinning the field mapping (`state`/`peer_ip`/`neighbor_as`/`vrf_name`/`up`).

## Live-verify (honest about the constraint)

The reachable org (TM-LAB) has **no live BGP peers** → telemetry is regression-only:
the new `org_bgp`/`site_bgp` fetch is clean (empty → `BGP_TELEMETRY` earned, zero
peers), ingest stays `ok=True`, existing live plans unchanged aside from
`state_meta.fetched` gaining `bgp_neighbors`. If TM-LAB carries switch/gateway
`bgp_config`, confirm `BgpPeer` minting on real config. The neighbor-record field
shape is grounded from the pasted live record.

## Out of scope / deferred

- Advertised-prefix BGP checking (`networks`) — denied → UNKNOWN for now.
- `auth_key`-bearing neighbor removal sharpness (→ UNKNOWN today; same class as the
  GS27 deferred auth-root case).
- VRF-scoped peer identity `(device, vrf, neighbor_ip)` (bgp_config carries no vrf).
- Gateway **device-op** BGP (stays UNKNOWN — M1 field-gate switch-only role check).
- Full live simulate against a real BGP-bearing org (telemetry record shape grounded
  from a pasted record; full live-verify pending an accessible BGP org).
