# Switch port-config misc attrs — voip_network, mac_limit, + recognized→REVIEW knobs

**Status:** PROPOSED
**Date:** 2026-06-26
**Author:** brainstormed with the repo owner

The final sub-project (SP4) of the switch port-config attribute-modeling program.
Brings the last 5 unmodeled switch port attrs in-scope so changes simulate
instead of returning UNKNOWN, split by genuine tractability:

- **`voip_network`** — modeled as a real **voice VLAN** (full integration into the
  VLAN/reachability graph; reuses the existing L2 checks).
- **`mac_limit`** — modeled as a real **over-limit client-drop** check.
- **`inter_switch_link`, `storm_control`, `enable_qos`** — **recognized→REVIEW**
  (no reachability model; SP3-style policy-floor).

## Program context

SP1 (admin-disable), SP2 (L1 speed/duplex/autoneg), SP3 (wired-auth) are merged.
SP4 completes the program. Owner-approved decisions (2026-06-26): full VLAN
integration for `voip_network`; recognized→REVIEW for the three knobs.

## OAS placement (verified; the plan MUST re-assert via scope tests)

From the committed `device_switch` OAS. `port_config` carries NONE of the five.

| attr | port_config | port_config_overwrite | local_port_config | port_usages |
|---|---|---|---|---|
| `mac_limit` | — | int (`anyOf` int/str), default 0 | int, default 0 | int (`anyOf` int/str) |
| `storm_control` | — | — | object | object |
| `enable_qos` | — | — | bool, default false | bool, default false |
| `voip_network` | — | — | string | string \| null |
| `inter_switch_link` | — | — | bool, default false | bool, default false |

So: `mac_limit` is allowlisted on overwrite + local + usage; the other four on
local + usage only. `mac_limit` default `0` = **unlimited**.

## Design

### 1. `voip_network` → voice VLAN (full integration)

`voip_network` names a network in the same namespace as `port_network`, resolving
to a VLAN via the existing `vlan_of(networks[name]["vlan_id"])` (`ingest/ports.py`
`usage_vlans`). It is the **voice/auxiliary VLAN** carried alongside the data
VLAN — present even on ACCESS ports (data untagged on `port_network`, voice tagged
on `voip_network`).

- **IR:** add `Port.voice_vlan: int | None` (the resolved voice VLAN; None =
  none/unresolvable). `voip_network` is already carried (dormant) by the resolver
  (`_USAGE_OVERRIDE_ATTRS`); ingest now resolves it: `voice = vlan_of(usage.get
  ("voip_network"))`, store on `Port.voice_vlan`, and fold it into the port's
  tagged set when present and ≠ native.
- **VLAN-graph membership (the load-bearing change):** today the L2 carriage logic
  ignores an access port's tagged VLANs — `_tagged(port)` returns `set()` unless
  `mode is TRUNK` (`representations/l2_graph.py`), and `access_ports_by_vlan`
  (`ir/indexes.py`) keys access ports by `native_vlan` ONLY. So a voice VLAN folded
  into an access port's `tagged_vlans` would be silently dropped. **An
  ACCESS/host-facing port with a `voice_vlan` must become a MEMBER of that VLAN**
  so it participates in the VLAN reachability graph. Concretely: extend the member
  index so a port with `voice_vlan is not None` **AND `mode is PortMode.ACCESS`**
  is listed under its `voice_vlan` (in addition to access-native membership), so
  `build_vlan_graph` includes the switch node in the voice VLAN component.
  **Mode-gated to ACCESS on purpose:** a trunk-shaped usage can also carry
  `voip_network`, but a transit trunk is NOT an endpoint member — it carries the
  voice VLAN via normal tagged carriage (`_tagged` on trunks), so folding voice
  into `tagged_vlans` covers trunks while the member index covers access. Treating
  a transit trunk as a member would create false member/blackhole churn.
  Voice-VLAN reachability to its exit flows via the uplink trunk's tagged carriage
  + the voice IRB exit — existing machinery; `l2.blackhole` / vlan-reachability /
  `native_mismatch` react to the voice VLAN automatically.
- **`client.impact` extension (NOT automatic — required):** `ClientImpactCheck`
  (`checks/wired/client_impact.py`) detects only `disconnect` (attach port
  removed), `vlan_move` (NATIVE vlan changed), and `blackhole` (vlan loses exit).
  A phone on `client.vlan == base_port.voice_vlan` whose port stops offering that
  voice VLAN (`prop_port.voice_vlan != client.vlan`) is MISSED — the vlan may stay
  healthy elsewhere (blackhole passes), and `vlan_move` only checks native. So add
  an explicit branch to `_impact_of`: a wired client whose `vlan` was offered by
  its attach port in baseline (native OR `voice_vlan`) and is no longer offered in
  proposed → impact `vlan_removed` ("voice/membership vlan {v} no longer offered on
  this port"). This is a small modeled extension, not free integration.
- **Field gate:** allowlist `voip_network` on `port_usages` + `local_port_config`
  (via `_MODELED_USAGE_ATTRS`).
- **Tests (owner-required):** an ACCESS port with data VLAN 10 + voice VLAN 30
  proves **VLAN 30 appears in the VLAN/reachability graph** (the port is a member
  of VLAN 30); a `voip_network` removal that strands the voice VLAN is a detected
  delta; **an active phone on the voice VLAN whose port drops `voip_network` is
  flagged by `client.impact` (`vlan_removed`)** even when the VLAN stays healthy;
  **a TRUNK usage carrying `voip_network` is NOT made an endpoint member** (regression
  — voice rides its tagged carriage, no spurious member/blackhole).
- **Golden checkpoint:** folding voice into the VLAN graph can shift
  `vlan_components`; the suite is delta-based (voice present on both baseline and
  proposed cancels), but the plan must run the full golden suite and investigate
  any churn before re-pinning.

### 2. `mac_limit` → real over-limit drop check

- **IR:** `Port.mac_limit: int | str | None`:
  - `int` (>0) = a concrete enforceable cap;
  - `None` = unlimited / absent / `0`;
  - `str` (a stable `unresolved:`/raw token) = a **templated/unparseable** value —
    **NOT collapsed to None** (collapsing would hide an in-scope config change).
- **Normalizer `_mac_limit(v)`** (mirrors SP3 `_reauth`): `int`/numeric-string →
  `int` (and `0` → `None` = unlimited); `None`/`""` → `None`; bool → `None`;
  anything else (template string, object) → a stable token.
- **Resolver:** `mac_limit` to `_OVERWRITE_ATTRS` (overwrite) + `_LOCAL_ATTRS`
  (local); usage-level via `usage_definition`. **Not** `_USAGE_OVERRIDE_ATTRS`
  (absent from `port_config`).
- **Check `wired.port.mac_limit_exceeded`** — **`requires()` returns `{WIRED_L2}`
  ONLY** (like `AuthAccessChangeCheck`), then inspects `CLIENTS_ACTIVE` INTERNALLY.
  (If it required `CLIENTS_ACTIVE`, the registry would short-circuit to
  INSUFFICIENT_DATA when client data is absent — `checks/registry.py` — and the
  `.unverified` finding below would never emit.) Per port whose `mac_limit` the
  delta changed, with an explicit **client-data honesty boundary**. The check
  counts BASELINE (currently-connected) clients, so "active wired-client data
  present" requires the capability on BOTH sides:
  `IRCapability.CLIENTS_ACTIVE in base_ir.capabilities and ... in
  prop_ir.capabilities` (the `dhcp_path` / `ospf_withdrawal` pattern). A baseline
  lacking the capability is NOT "known zero clients" → treat as data-absent
  (`.unverified`), never a silent pass:
  - `prop.mac_limit` is `None` (unlimited), or merely RAISED vs baseline →
    **silent** (a looser/removed cap cannot drop anyone).
  - `prop.mac_limit` is an **unresolved token** (and changed) → **WARNING→REVIEW**
    (`.unresolved`): the limit is not evaluable.
  - `prop.mac_limit` is a concrete `int` that is newly-set or LOWERED:
    - active wired-client data present for the device AND `len(clients_by_port[pid])
      > limit` → **WARNING→REVIEW** (`.exceeded`): the excess currently-connected
      clients will be dropped (the *count* over-limit is certain; *which* MACs the
      switch evicts, and enforcement aging, are not — so REVIEW, never ERROR/UNSAFE).
    - active wired-client data present AND observed ≤ limit → **silent** (proven
      within the cap).
    - active wired-client data **absent** → **WARNING→REVIEW** (`.unverified`):
      "limit set/lowered to N; current client count is unobservable, cannot
      confirm safe" — a restrictive change with no visibility never silently passes.
  - Degrades gracefully; never ERROR/UNSAFE; never SAFE on a restrictive/unresolved
    change.
- **Field gate:** `mac_limit` on `port_usages`+`local` (`_MODELED_USAGE_ATTRS`) +
  `port_config_overwrite` (`_OVERWRITE_LEAVES`).

### 3. `inter_switch_link` / `storm_control` / `enable_qos` → recognized→REVIEW

These have no reachability/connectivity model the twin reasons about
(`inter_switch_link` enables `networks.*.isolation`, which is explicitly
unmodeled; `storm_control` is a runtime traffic-protection knob; `enable_qos` is
pure scheduling). So: recognize the change and floor REVIEW — never fake an impact.

- **IR:** `Port.misc: PortMisc | None` — a frozen value object:
  `inter_switch_link: bool`, `enable_qos: bool`, `storm_control: str | None`
  (a canonical digest of the storm_control object; None = default). `Port.misc is
  None` ⇔ the whole misc surface is default/absent (mirrors `Port.auth=None`;
  a change to any single knob wakes the check).
- **Resolver:** these to `_LOCAL_ATTRS` (local) + usage via `usage_definition`.
- **Check `wired.port.unmodeled_change`:** floors **WARNING→REVIEW** on any
  `Port.misc` change, naming which knob ("inter-switch-link / storm-control / QoS
  changed; impact not modeled"). Never SAFE, never ERROR/UNSAFE — the SP3
  policy-floor pattern.
- **Field gate:** `inter_switch_link`/`storm_control`/`enable_qos` via
  `_MODELED_USAGE_ATTRS` (usage site/device/networktemplate + local device-only).

### 4. Registration + L0

Register `MacLimitExceededCheck` and `PortUnmodeledChangeCheck`; bump
`len(ALL_WIRED_CHECKS) == 23 → 25` (verify it is 23 first). **No L0 change** — all
5 are documented in the OAS (the `anyOf` `mac_limit` and the `storm_control` object
are documented properties the unknown-attribute walker accepts).

## Non-goals

- Simulating storm-control triggers, QoS scheduling, or `networks.*.isolation`.
- Resolving the switch's full MAC table (only currently-connected wired clients
  are observable — the `mac_limit` check is conservative about that, per §2).

## Testing (highlights; full list in the plan)

- **OAS placement** re-asserted: the 5 leaves in/out of scope per map exactly as
  the §"OAS placement" table (esp. `mac_limit` on overwrite+local+usage; the other
  four on local+usage; NONE on `port_config`).
- **voip:** ACCESS port data=10 + voice=30 → VLAN 30 in the VLAN/reachability
  graph; voice-VLAN strand detected; resolver resolves `voip_network` and a
  `port_config` `voip_network` is ignored; **active phone on the voice VLAN whose
  port drops `voip_network` → `client.impact` `vlan_removed` even when the VLAN
  stays healthy**; **TRUNK usage with `voip_network` is NOT an endpoint member**
  (carried via tagged, no spurious blackhole).
- **mac_limit normalizer:** `5`=="5"→5; `0`→None; `""`/null→None; `"{{var}}"`/object
  → stable token (NOT None).
- **mac_limit_exceeded:** check `requires()` is `{WIRED_L2}` only (a unit asserts
  CLIENTS_ACTIVE is NOT required, so it isn't registry-short-circuited); concrete
  over-limit with CLIENTS_ACTIVE on both sides → REVIEW(.exceeded); observed ≤
  limit with CLIENTS_ACTIVE on both sides → silent; restrictive/new limit with
  CLIENTS_ACTIVE missing on EITHER side → REVIEW(.unverified) — **incl. a pin where
  baseline lacks the capability but proposed has it** (must NOT silently pass);
  unresolved value → REVIEW(.unresolved); raised/unlimited → silent; never
  ERROR/UNSAFE.
- **misc:** any of the 3 knobs changes → REVIEW(.unmodeled); `Port.misc=None`
  ⇔ all-default (a lone `enable_qos` flip wakes it); never SAFE.
- **Field-gate pins:** each of the 5 in scope on its OAS maps; `port_config.*` and
  (for the four non-mac_limit) `port_config_overwrite.*` stay UNKNOWN; `mac_limit`
  on overwrite IS in scope.
- **Public API:** `len(ALL_WIRED_CHECKS) == 23 → 25`.
- **e2e + goldens + ROADMAP.** Full gate each task:
  `uv run pytest -q && uv run ruff check . && uv run mypy src`.

## Risks / open points

- The voice-VLAN membership change touches the VLAN graph — the highest churn risk
  in the program. Mitigated by the delta-based golden run + explicit member-index
  test.
- `mac_limit` reflects currently-connected wired clients only (not the full MAC
  table); the check is honest about this (`.unverified` when client data absent).
- `storm_control` digest must be stable/order-independent so an equivalent object
  doesn't spuriously wake the check.
