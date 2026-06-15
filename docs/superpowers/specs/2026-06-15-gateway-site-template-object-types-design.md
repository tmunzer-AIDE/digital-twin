# gatewaytemplate / sitetemplate as first-class object_types (design)

Status: approved 2026-06-15, ready for writing-plans.
Builds directly on the shipped networktemplate org-template fan-out
(docs/superpowers/specs/2026-06-14-multisite-org-template-simulation-design.md).

## Problem / goal

The networktemplate slice simulates a **switch**-template edit across every site
assigned to it. Two more org-assigned template types remain unmodeled:

- **`gatewaytemplate`** — the WAN/gateway (SRX/SSR) template. Today gateways are
  not a compile target at all: GS22 reads gateway facts off the **raw device
  object**, so there is no template-merge path on that side.
- **`sitetemplate`** — a layer that sits between the org template and the site's
  own setting, carrying the same config surface. The twin skips it entirely
  today, so any site already assigned a sitetemplate is compiled slightly wrong.

Goal: make both first-class `object_type`s for org-template simulation, on a
**unified layered effective-config compiler**, reusing the existing fan-out
(`simulate_org_template`, `decide_org`, `OrgVerdict`) and the existing checks —
never a parallel analysis path, never a false-SAFE.

## The vendor model (confirmed with the domain owner)

The Mist derivation is **one uniform layer stack** for every device family
(ap / switch / gateway), base → winner:

```
<type>template  →  sitetemplate  →  site_setting  →  device-profile  →  device
   (base)                                                                (wins)
```

- switch: `networktemplate → sitetemplate → site_setting → device-profile → device`
- gateway: `gatewaytemplate → sitetemplate → site_setting → device-profile → device`

Each template is bound to a site by its own id field in `/sites/{id}`
(`networktemplate_id`, `gatewaytemplate_id`, `sitetemplate_id`, …). The
gatewaytemplate uses the **same field names** the gateway device exposes (some
device-level fields may be absent), and per-device overrides follow the same
Mist PUT-root model the twin already handles (present roots replace wholesale,
`{"-attr":""}` deletes, omitted persists → `effective_update`/`update_conflicts`).
The switch side additionally has `switch_matching` assignment rules (already
denied → UNKNOWN); gateway has no such complication.

## Scope (MVP)

In:
- Generalize the org-template fan-out to a **typed** set
  `{networktemplate, gatewaytemplate, sitetemplate}`.
- One **`fold_layers(layers, policy)`** primitive for the whole stack.
- The **sitetemplate** compile layer (switch and gateway sides) — also fixes the
  latent baseline gap.
- A **gateway compile**: gatewaytemplate folded under the device → the existing
  GS22 gateway IR/checks run on the merged effective device, unchanged.
- Committed OAS schemas for L0; goldens; read-only live verification.

Out (documented, not built):
- The **device-profile** layer — modeled only as a relevance-scoped UNKNOWN
  (see "Device-profile honesty"). Roadmap item to model it for real.
- Gateway **routing / BGP / tunnels / security policy** — not allowlisted →
  field gate → UNKNOWN (fail-safe).
- `aptemplate` (APs are observation-only in the twin); `switch_matching`
  (already denied); template `delete`-ripple (separate roadmap item).

## Architecture

### 1. The fold primitive (the crux)

New primitive in `adapters/mist/compile/`:

```
fold_layers(layers: Sequence[JsonObj | None], policy: PolicyTable) -> Effective
```

- `layers` ordered **base → winner**; `None` layers are skipped.
- per-field merge policy comes from the **`policy` parameter** (guardrail #1):
  `REPLACE` (default) or `DICT_MERGE` (keyed collections merged per key, later
  layer wins per key). Switch and gateway each pass their own table so gateway
  can diverge later without a refactor.
- keeps lightweight **provenance** (which layer set each top-level field) for
  diagnostics where useful.

Reimplementations on top of the primitive:
- `merge_site_effective` → `fold_layers([networktemplate, sitetemplate,
  site_setting], SWITCH_POLICY)`. Behavior-identical when `sitetemplate` is
  absent; **fixes the latent baseline gap** when it is present. The existing
  `merge_only(networktemplate, site_setting)` / `merge_site_effective` **2-arg
  signatures must be preserved** (the offline Tier-2 `tools/equivalence_gate.py`
  calls `merge_only` with two args) — reimplement their *bodies* on `fold_layers`,
  don't change their arities.
- gateway site-effective → `fold_layers([gatewaytemplate, sitetemplate,
  site_setting], GATEWAY_POLICY)`, then the per-device PUT-root overlay
  (`effective_update`), then **`_resolve` `{{vars}}` LAST** → an **effective
  gateway device**. **Order matters — `_resolve` runs AFTER the device overlay,
  matching `compile_device` (`compile/switch.py:95` resolves last, after the
  per-device merge) (was a P2 order divergence).** Resolving before the overlay
  would leave any device-supplied `{{var}}` unsubstituted; resolving last matches
  the switch contract. **And `_resolve` is mandatory (was a P2 gap):** a
  fold/overlay *without* `_resolve` leaves `{{var}}` refs literal, so a `vars`
  edit never reaches the effective gateway `dhcpd_config`/`ip_configs` and the
  vars-ripple DHCP screens (§4) silently no-op on gateways.
- **`GATEWAY_POLICY` must DICT_MERGE the gateway keyed maps `port_config` /
  `ip_configs` / `dhcpd_config` — NOT inherit the default REPLACE (was a P2 false-
  diff).** `_POLICY` (`compile/merge.py:29-35`) lists no `port_config`/`ip_configs`
  → they default to **REPLACE**; for switches that's fine because their per-port
  merge happens at the *device-overlay* layer (`_DEVICE_DICT_MERGE_FIELDS`), not
  the site-fold. But a `gatewaytemplate` carries `port_config`/`ip_configs` at the
  **template** layer, so folding `[gatewaytemplate, sitetemplate, site_setting]`
  under REPLACE would let a sitetemplate that defines **one** port range **wipe the
  gatewaytemplate's other ports** — on **both** baseline and proposed, silently
  corrupting the diff (→ false verdict). `GATEWAY_POLICY` therefore DICT_MERGEs
  these keyed maps per key — the same principle `merge.py:35` already states
  ("device defining one port range must not wipe the rest"). The exact per-layer
  policy for these gateway maps is **Tier-2 live-verified** (Mist's real layering;
  flagged below), starting from DICT_MERGE. Test: a sitetemplate defining one
  gateway port range does **not** erase the gatewaytemplate's other ports.

**Gateway effective → ingest handoff (the explicit contract — was a P1 gap).**
Today the gateway ingest (`_gateway_ports_and_l3`, gateway dhcp) reads its facts
straight off `ctx.raw.devices` (the raw device dict — `dev["port_config"]`,
`dev["ip_configs"]`, `dev["dhcpd_config"]`), exactly like the switch ingest at
`ingest/switch.py:319-324`. There is **no** effective-device source today, so a
gatewaytemplate edit would compile correctly yet never reach the IR unless the
handoff is explicit. The contract: the **compile stage materializes the folded
effective gateway config back into the gateway-type entries of
`RawSiteState.devices`** (replacing only the modeled gateway leaves the ingest
consumes from the device — `port_config`, `ip_configs`, `dhcpd_config`; **not**
`networks`, whose namespace is `org_networks`, see §4 — on `type == "gateway"`
devices; switch/AP devices untouched) for **both** the
baseline and proposed snapshots, *before* ingest runs. **`RawSiteState` is a
**frozen** dataclass (`providers/base.py:48`) — "materialize back into" means
**derive a NEW state via `dataclasses.replace(raw, devices=…)`** (like
`apply/objects.py`'s `replace_object`), not an in-place mutation. This also keeps
the *recorded* single-site fixture clean: `save_run` captures the pre-
materialization fetched state, so replay re-folds from the recorded layers. **This is one of TWO
required destinations for the folded gateway effective** — `RawSiteState.devices`
feeds the *ingest* (raw reads); the *derived gate* needs its own copy (next
paragraph). Materializing into devices alone leaves the derived gate blind. The existing GS22 ingest
then consumes them **unchanged** (guardrail #5 — no second gateway analysis
path). Rejected alternative: teaching the ingest to read a separate
`ctx.gateway_device_effective` source — that forks the device read path for one
family and is exactly the parallel path #5 forbids.

**Gateway effective MUST also enter the derived-gate set (was a P1 false-SAFE —
the materialization feeds ingest but bypasses the derived gate).** Today
`adapter.ingest` builds `device_effective` **only for `type == "switch"`**
(`adapters/mist/adapter.py:55-58`), and the derived gate iterates exactly that
set (`pipeline.py:145` over `set(baseline.device_effective) |
set(proposed.device_effective)`). So a gateway has **no** compiled-effective
artifact the derived gate ever diffs — which means **every** out-of-scope or
value-inert gateway effective ripple (a `vars`/sitetemplate/override change
compiling into the gateway's effective `dhcpd_config`, `ip_configs`, …) is
**unscreened** and can resolve SAFE. The IR ingesting the materialized device is
**not** enough: the IR is a projection of in-scope leaves only, so an
out-of-scope or value-inert effective ripple never enters the IR and the *IR*
diff can't see it — the derived gate is the layer that catches exactly those, and
it is gateway-blind today. Contract: the gateway compile must **also publish the
folded effective gateway device (baseline + proposed) into a map the derived gate
iterates** — extend `device_effective` to gateway device ids (or a sibling
`gateway_effective` map fed into the same `check_derived` loop). **Key the gateway
effective the SAME way ingest keys devices (`device_id(mac)`,
`ingest/switch.py:332`): a gateway lacking `mac` is silently dropped from ingest,
so the materialization/derived-gate publishing MUST drop it too — uniformly absent
from both, a consistent blind spot (no IR facts either, so no false-SAFE), never
an asymmetry where one stage sees the gateway and the other doesn't.**
`check_derived` then screens gateway effective **against a GATEWAY effective
allowlist — NOT the
switch `EFFECTIVE_ALLOWLIST` (was a P1 false-UNKNOWN that contradicts our own
goldens).** Today `EFFECTIVE_ALLOWLIST` (`scope/allowlist.py:172`) is the *switch*
leaf set: it carries `poe_disabled` but **not** plain `port_config.*.disabled`,
and `other_ip_configs.*.ip` (switch IRB) but **not** gateway `ip_configs.*.ip`. So
screening gateway effective against it would reject a `gatewaytemplate.port_config.
*.disabled` flip or an `ip_configs.*.ip` edit → UNKNOWN, **defeating the feature
and contradicting the `disabled`-drift and `same_ip` goldens** (those leaves must
reach the gateway checks). The derived gate must therefore be **role-keyed**:
`check_derived` takes the allowlist to use, and the gateway-effective diff is
screened against a new `GATEWAY_EFFECTIVE_ALLOWLIST` = the §4 gateway leaf set
(`port_config.*.{networks,port_network,disabled}`, `ip_configs.*.ip`,
`dhcpd_config.*.{type,servers,ip_start,ip_end,gateway}`) **plus `vars.*` (was a P2
omission).** `_resolve` (`compile/switch.py:61`) substitutes `{{vars}}` but
**preserves the `vars` root**, so a `vars.*` edit appears in the gateway effective
diff as a residual `vars.*` leaf change. The switch `EFFECTIVE_ALLOWLIST` already
allowlists `vars.*` as a whole subtree for exactly this reason (`allowlist.py:181`,
comment at `:117/:170`) — the derived gate catches the *ripple* into modeled
leaves, so the `vars.*` leaf itself must be allowed or **every** gateway `vars`
edit trips a generic out-of-scope `derived_gate` rejection → UNKNOWN, preempting
the DHCP row helper / the real modeled-leaf screen. `GATEWAY_EFFECTIVE_ALLOWLIST`
must mirror that (`+ vars.*`). The DHCP value-aware screens (§4) run on the gateway
effective there too. Without this, the "screens run on the effective diff in the
derived gate, so ripples are caught" guarantee is gateway-blind and false; and a
benign gateway `vars` edit (rippling only into in-scope modeled leaves) would
wrongly UNKNOWN on the `vars.*` path alone.

### 2. Provider surface

- `resolve_org_template(scope, template_id, object_type)` — generalized: filter
  the org's sites by `<object_type>_id` and fetch the template of that type.
  Returns the existing `OrgTemplateContext(template, assigned_site_ids)`. Lookup
  failure (sites list or template) → `FetchError` → UNKNOWN (unchanged contract).
- `RawSiteState` gains `sitetemplate: JsonObj | None` and
  `gatewaytemplate: JsonObj | None` alongside today's `networktemplate`. The
  per-site fetch pulls the site's assigned ones (by `sitetemplate_id` /
  `gatewaytemplate_id`).
- **Single-site replay surface must add the new fields too (was a P2 gap, distinct
  from the org-fixture shape below).** The new `RawSiteState` fields must be added
  to `observability/replay/store.py`'s `_RAW_FIELDS` (the write enumeration) **and**
  to `load_fixture_doc` using the **`.get(..., None)` back-compat pattern** (like
  `wlans`/`org_networks` today), so older fixtures load as `None` and current runs
  round-trip `sitetemplate`/`gatewaytemplate`. Without this they are silently
  dropped on replay → compile-without-sitetemplate → an optimistic (potentially
  false-SAFE) replayed verdict, and `save_run` would omit them from saved fixtures.
- **Replay-fixture shape must carry typed templates (was a P2 gap).** Today the
  multi-site fixture (`observability/replay/store.py`) holds a single top-level
  `"template"` and `resolve_org_template` filters only on `networktemplate_id`.
  Generalize the doc to **typed templates keyed by `(object_type, id)`** —
  e.g. `"templates": {"networktemplate": {<id>: {...}}, "gatewaytemplate":
  {<id>: {...}}, "sitetemplate": {<id>: {...}}}` — and have each site doc carry
  its `networktemplate_id` / `gatewaytemplate_id` / `sitetemplate_id` plus the
  corresponding raw template bodies, so the typed `resolve_org_template` filters
  by `site.<object_type>_id` and the per-site cross-stack fetches resolve. Keep
  back-compat: the legacy single `"template"` key is read as a `networktemplate`
  so the existing MS-a..d goldens stay valid. The `FixtureProvider`
  multi-site/wrong-org/missing-template strictness rules carry over per type.
- **Fetch the full IR's layers; pin only the edited one (corrects an earlier
  over-narrowing).** `_simulate_site_state` builds the **whole** IR + check suite
  every run, and the checks are cross-cutting — gateway exits/DHCP depend on
  switch-side VLANs/carried-networks/client-attachment/L2 graph and vice versa.
  So the per-site fetch must pull **every assigned layer needed to build the full
  baseline/proposed IR**, not just the edited stack:
  - the **edited** layer is supplied from `resolve_org_template`'s org-level
    snapshot and **pinned** into every site (§3) — **not** re-fetched per site
    (avoids a duplicate fetch that could manufacture a false UNKNOWN).
  - **every other assigned layer is fetched per site:** `site_setting`,
    `sitetemplate`, **and the other org template** — i.e. a `gatewaytemplate`
    edit still fetches the assigned `networktemplate` (so the switch IR is built
    correctly), and a `networktemplate` edit still fetches the assigned
    `gatewaytemplate` (so gateway exits/DHCP context is built correctly), plus
    devices.
- **Fetch-miss rule (guardrail #4):** for **any assigned layer the simulation
  consumes to build the IR** (edited or not), a site assigned that layer which
  fails to fetch must **not** compile without it — it is a recorded per-site
  fetch failure → that site is UNKNOWN (the existing `site_failures` → org rollup
  UNKNOWN path), never a silent SAFE.

### 3. Org fan-out — pin exactly one edited layer (guardrail #2)

`apply_template` / `override_template` are parametrized by the edited
`object_type`. For an edit to layer **X**, baseline vs proposed differ **only**
at layer X; every other fetched layer (the other templates, `sitetemplate`,
`site_setting`) stays **pinned** per site to a single snapshot (the fetch-race
guardrail, carried from the networktemplate doc). So each site's diff is exactly
the edit and nothing else moves.

**`override_template` must generalize from one hard-wired field to a typed
field-set (was a P2 under-spec).** Today `override_template`
(`engine/org_template.py`) sets only `networktemplate=` on the `RawSiteState`,
and `RawSiteState` has no `sitetemplate`/`gatewaytemplate` fields
(`providers/base.py`); `simulate_org_template` hard-codes
`resolve_org_template(scope, template_id)` (no `object_type`) and
`screen_op("networktemplate", …)` (`pipeline.py:355,382`). Generalize: set the
**typed** field for `object_type` on **both** the baseline and proposed raws,
leaving every other layer at the single fetched snapshot. The `sitetemplate` case
is the one a single-field override cannot express — the proposed `sitetemplate`
must replace that field in the **one** fetched raw that feeds **both** the switch
**and** gateway compiles (sitetemplate is in both stacks), so the typed setter
writes one field consumed by two fold chains; a test must assert a sitetemplate
edit re-derives both stacks from the single pinned sitetemplate.

In all three cases the **full** IR is still built from **all** assigned layers
(§2) — "identical" / "no findings" on the non-edited side means it is *accurately
built and unchanged*, not skipped:

- `networktemplate` edit → pins networktemplate, holds sitetemplate +
  site_setting + the assigned **gatewaytemplate**; the gateway effective is built
  accurately from that gatewaytemplate and is identical baseline/proposed → no
  gateway findings (but gateway exits/DHCP context the switch checks rely on is
  correct).
- `sitetemplate` edit → pins sitetemplate, holds both org templates +
  site_setting; re-derives **both** switch and gateway effective (sitetemplate is
  in both stacks).
- `gatewaytemplate` edit → pins gatewaytemplate, holds sitetemplate +
  site_setting + the assigned **networktemplate**; the switch effective is built
  accurately from that networktemplate and is identical baseline/proposed → no
  switch findings (but switch/L2/VLAN/client context the gateway checks rely on is
  correct).

`simulate_org_template` dispatches by `object_type`, resolves assignment, fans
out, and per site builds the effective config(s) with the edited layer pinned,
then runs the existing `_simulate_site_state` core. `decide_org` rollup is
unchanged (worst-of `UNKNOWN>UNSAFE>REVIEW>SAFE` + template-findings floor +
0-sites SAFE).

### 4. Typed gates

- `ORG_OBJECT_TYPES = ("networktemplate", "gatewaytemplate", "sitetemplate")`.
  object_gate ORG-mode recognizes all three; the single-template-id-per-plan
  invariant is kept (one template id, no `site_id`, all ops that type).
  **Two ORG-detection sites must be aligned (was a P3 hardcode):**
  `object_gate.check_objects` hardcodes `op.object_type == "networktemplate"`
  (`scope/object_gate.py:31`) and the single-id message says "multiple
  networktemplate ids" (`:41`) — change the predicate to `in ORG_OBJECT_TYPES` and
  generalize the message; `pipeline.py:347` *already* uses `ORG_OBJECT_TYPES`, so
  the two sites are inconsistent today and must agree.
- **Allowlists per type:**
  - `gatewaytemplate` — **leaf-pattern entries, NOT root keys (was a P1
    false-allow).** The field gate is leaf-based (`scope/paths.py` descends added/
    removed subtrees and gates every leaf on its own), so a root-shaped entry like
    `port_config` / `ip_configs` / `dhcpd_config` — or copying the switch *device*
    allowlist — would bless leaves the gateway ingest never reads (e.g.
    `port_config.*.mtu`, `ip_configs.*.netmask`), yielding **unchanged IR that can
    resolve SAFE** despite a real config change. The allowlist is **exactly** the
    leaves `_gateway_ports_and_l3` / gateway-dhcp actually consume, in the existing
    `networks.*.vlan_id` entry style — only leaves the ingest **consumes AND acts
    on** (i.e. the value reaches an IR field some check / representation / analysis
    actually reasons about):
    ```
    port_config.*.networks        port_config.*.port_network
    port_config.*.disabled
    ip_configs.*.ip
    dhcpd_config.*.type           dhcpd_config.*.servers
    dhcpd_config.*.ip_start       dhcpd_config.*.ip_end
    dhcpd_config.*.gateway
    ```
    (`networks`/`port_network` → `Port.native/tagged_vlan`; `disabled` →
    `Port.disabled`, read by l2_isolation/snooping/link_boundary/l2_graph;
    `ip_configs.*.ip` → `L3Intf.ip` → `gateway_gap.same_ip`; the `dhcpd` leaves →
    `DhcpScope` + `_dhcp_active` source-crediting → `scope_lint`/`dhcp_path`.)
    *Documented coverage boundary (not a defect): an `ip_configs.*.ip` edit on a
    VLAN with **no routed intent** (no declared subnet in `org_networks`) mints/
    drops an `L3Intf` that `gateway_gap` skips (`gateway_gap.py:108-116`), so it can
    resolve SAFE — the same modeled-SAFE boundary as the existing switch
    `other_ip_configs` path (GS22). The leaf is allowlisted (it DOES drive
    `same_ip` on routed VLANs); the drift assertion should note this is partial
    coverage, not mistake the allowlist entry for full L3 coverage.*
    - **`dhcpd_config.*.servers` needs a value-aware screen (was a P1 false-allow).**
      The IR models `servers` only as a **boolean** — `_dhcp_active` treats *any*
      non-empty relay list as "active", and `Vlan.dhcp_sources` stores the provider
      id, **not** the relay target IPs; `dhcp_path` reasons only about provider
      gain/loss. So a both-non-empty **target change** (`["10.1.1.1"] →
      ["10.2.2.2"]`, or adding a second server) keeps the boolean and the provider
      set identical → no finding → false SAFE. **`servers` is meaningful ONLY on a
      `relay` row** — `_dhcp_active` (`ingest/switch.py:91-99`) returns active for
      `local`/`server` (serving) rows *regardless of* `servers`, and only toggles
      on `servers` for `relay`. So the screen is **row-type-aware**:
      - **`relay` row:** an **empty↔non-empty** `servers` change is the modeled
        activation/deactivation (real provider gain/loss) → **allowed**; a
        **both-non-empty** change is an unmodeled target/set change →
        `Rejection(stage="dhcp_relay_target")` → **UNKNOWN**.
      - **row serving on BOTH sides (S→S):** `servers` is **inert** (read by
        nothing on a serving row) → a `servers` change is like `usage` → not
        modeled → `Rejection(stage="dhcp_inert_servers")` → **UNKNOWN**. (Only when
        serving is *stable* across the transition — if `type` also changes, e.g.
        `local→relay`, the **participation subrule** governs, not this screen; that
        is how the empty-`servers` exemption `local,["x"]→relay,[]` correctly stays
        a `dhcp_path` REVIEW rather than being preempted here.)
      This screen
      attaches to the shared `dhcpd_config.*.servers` leaf **wherever it is in
      scope** — gateway here *and* the pre-existing switch/site path, which carries
      the identical limitation today — so it is a deliberate, test-pinned safety
      tightening (SAFE→UNKNOWN only for the unmodeled target-change case), not a
      gateway-only divergence. Modeling relay target IPs in the IR (to resolve such
      edits to SAFE/REVIEW precisely) is recorded as future work.
    - **`dhcpd_config.*.{ip_start,ip_end,gateway}` need the same serving-row screen
      (was a P1 false-allow, same class).** `DhcpScope` is minted **only for
      serving rows** (`_dhcp_serves_scope`: `type ∈ {local, server}`, absent →
      `local`); **relay/none rows are skipped** and never become a scope. So a
      relay/none row changing only `ip_start`/`ip_end`/`gateway` passes the gate,
      mints no `DhcpScope`, leaves `dhcp_sources` unchanged → no finding → false
      SAFE. Screen: a change to one of these range/gateway leaves is allowed only
      when **at least one side (baseline or proposed) is a serving row** — the
      serving↔non-serving transition is modeled as a `DhcpScope` add/remove (real
      signal), and a both-serving edit changes real scope facts the checks read.
      When **both sides are non-serving** (relay/none) → `Rejection(stage=
      "dhcp_scope_field")` → **UNKNOWN**. Like the `servers` screen, this attaches
      to the shared leaves wherever in scope — gateway **and** the switch/site path
      (identical serving-only minting) — as a uniform, test-pinned safety
      tightening.
    - **`dhcpd_config.*.type` is the participation selector — it needs a
      transition screen, and it forces the helper to be ROW-level (was a P1
      false-SAFE).** A `type`-only change silently **re-interprets other unchanged
      leaves**: `local → relay` with an unchanged non-empty `servers` keeps
      `_dhcp_active` true (so `dhcp_path` sees the *same* provider — no loss),
      **removes** the serving `DhcpScope` (relay owns no scope), and `scope_lint`
      only lints *proposed* scopes and does **not** treat healthy scope removal as a
      finding — so DHCP silently switches from serving leases locally to relaying to
      an **unmodeled target**, and it resolves **SAFE**. No *screened leaf* changed,
      so the per-leaf screens miss it. Therefore the three screens are **one shared
      row-level "dhcp-row relevance" helper** that examines **both sides' full row**,
      and its rule is **purely row-local — expressed via the two pure predicates the
      ingest already uses, NOT via any check's output** (the derived gate runs
      **before** checks, `pipeline.py:139`, so it cannot ask whether `dhcp_path`
      emitted a finding — was a P2 stage-order bug). Define
      `serves(row) = _dhcp_serves_scope(row)` (type ∈ {local,server,absent}) and
      `active(row) = _dhcp_active(row)` (serving, or relay with non-empty `servers`),
      and `active_relay(row) = active(row) ∧ type == "relay"` (a relay with non-empty
      `servers`). The only DHCP fact the IR/checks do **not** capture is an **active
      relay's target** (the `servers` IPs of an active relay) — an active-serving row
      has **no** relay target, so its "target identity" is a fixed sentinel. This
      sub-bullet defines the **participation/relay-target subrule** (one of the
      helper's three screens — the inert-`servers` and inert-range/gateway screens
      in the two sub-bullets above are ALSO part of the helper; see "Complete
      rejection set" below). When **at least one side is INACTIVE**, the active flip
      itself is the modeled signal (`dhcp_path` provider gain/loss) → no
      participation rejection. When **both sides are active**, this subrule rejects
      → **UNKNOWN** iff the two sides' relay-target identity differs — i.e.
      (symmetric, catches **both** directions):
      - **exactly one side is an `active_relay`** (serving↔active-relay, **either**
        direction — `local/server → relay+servers` *and* `relay+servers →
        local/server`) → `Rejection(stage="dhcp_mode_transition")`; or
      - **both sides are `active_relay`** and their `servers` differ →
        `Rejection(stage="dhcp_relay_target")`.

      Everything else is **participation-allowed** (defers to the modeled checks),
      because its whole participation effect is captured by `active` (→ `dhcp_path`
      provider gain/loss) and `DhcpScope` presence/facts (→ `scope_lint`): any change
      with ≥1 inactive side (pure relay activation/deactivation, serving↔`none`), a
      both-active-serving scope-fact edit, `local↔server`, or two identical active
      relays — **subject still to the two inert-leaf screens** (an inert `servers` or
      range/gateway edit within such a change is independently UNKNOWN). Crucially
      the empty-`servers` case —
      **serving→relay with EMPTY/absent `servers`** (relay becomes inactive, last
      provider lost) — has `active(proposed)=false` (≥1 inactive side), so it is **not
      rejected**; it stays a modeled `dhcp_path` provider-loss verdict (REVIEW), not
      preempted to
      UNKNOWN. Same shared leaves, same switch/site + gateway coverage, same
      derived-gate placement as below.

      **Exhaustive participation matrix (the participation axis's whole state space
      — every (S/R/I)×(S/R/I) transition classified; the two inert-leaf screens
      compose on top).** Each side collapses to one of three
      participation states: **S** = serving (`local`/`server`/absent → mints
      `DhcpScope`, `active`, no relay target), **R** = active relay (`relay` +
      non-empty `servers` → `active`, no scope, **unmodeled** target), **I** =
      inactive (`none`, or `relay` with empty/absent `servers` → not `active`, no
      scope, no provider). **This matrix is the participation/relay-target subrule ONLY
      (one axis); the two inert-leaf screens are orthogonal and compose on top —
      "allowed" below means "no *participation* rejection," and a cell can still be
      UNKNOWN if an inert leaf changed within it (noted per cell).** The
      participation subrule fires in **three** differing-target transitions —
      `S→R`, `R→S`, and the `R→R`-with-differing-`servers` **subcase** (the four
      both-`active` cells are S→S, S→R, R→S, R→R; S→S and R→R-same are allowed, so
      three transitions reject); every cell with ≥1 `I` side defers to the modeled
      `dhcp_path`/`scope_lint` signal:

      | base ↓ \ prop → | **S** (serving) | **R** (active relay) | **I** (inactive) |
      |---|---|---|---|
      | **S** | participation-allowed → `scope_lint` reads the scope-fact delta · *(a `servers` edit here → UNKNOWN, inert-servers screen)* | **UNKNOWN** `dhcp_mode_transition` (scope lost silently; `active` stays true → no `dhcp_path` loss) | allowed → `dhcp_path` provider-loss (REVIEW) |
      | **R** | **UNKNOWN** `dhcp_mode_transition` (relay target silently gone; `active` stays true) | same `servers`: participation-allowed *(a range/gateway edit → UNKNOWN, scope-field screen — R is non-serving)* · differing: **UNKNOWN** `dhcp_relay_target` | allowed → `dhcp_path` provider-loss (REVIEW) |
      | **I** | allowed → provider gain + `scope_lint` on the new scope | allowed → provider gain (additive; no modeled service replaced) | participation-allowed *(an inert range/gateway edit on these both-non-serving rows → UNKNOWN, scope-field screen)* |

      The three UNKNOWN transitions above (`S→R`, `R→S`, `R→R`-differing) are the
      participation/target rejections; the inert-leaf screens add UNKNOWN within
      otherwise-allowed cells. **Complete rejection
      set (the helper rejects → UNKNOWN if ANY holds — four distinct stages):** (1)
      **inert `servers`** — `servers` changed while the row is serving
      (`local`/`server`/absent) on **BOTH** sides (S→S, serving stable) →
      `dhcp_inert_servers`; (2) **participation/target** — the matrix's **three**
      differing-target rejection cases (`S→R`, `R→S`, `R→R`-differing) →
      `dhcp_mode_transition` (serving↔active-relay) / `dhcp_relay_target` (both
      active relays, differing `servers`); (3)
      **inert scope-fact** — `ip_start`/`ip_end`/`gateway` changed while **both**
      sides non-serving (relay/none) → `dhcp_scope_field`. Everything else defers to
      the modeled `dhcp_path`/`scope_lint` verdict. **(1) requires serving on BOTH
      sides** — a `servers` change that *crosses* participation (e.g.
      `local,["x"]→relay,[]`) is governed by (2)/`dhcp_path`, not (1), preserving the
      empty-`servers` REVIEW exemption.
    - **The row-level helper evaluates the COMPILED EFFECTIVE diff, in the derived
      gate — not just the raw field gate (was a P1 placement gap).** The derived
      gate (`scope/derived_gate.py`) diffs the full effective baseline/proposed at
      leaf granularity but today only checks path membership in
      `EFFECTIVE_ALLOWLIST` — so an effective `dhcpd_config.*` change (`servers`,
      range/gateway, **or `type`**) that **ripples in from an in-scope leaf** (e.g.
      a `vars.*` edit compiling through DICT_MERGE into the effective
      `dhcpd_config`) is an allowed *path* and slips through → false SAFE. The
      row-level helper must therefore run **on the effective rows inside the
      derived gate** (it compares each `dhcpd_config.*` row's both-sides effective
      value), catching **both** the direct template edit *and* the `vars`/override
      ripple. **The helper lives INSIDE `check_derived` itself (was a P3 wiring
      gap) — NOT bolted onto the gateway iteration only.** `check_derived` is
      called for the site-effective (switch/site `dhcpd_config`, `pipeline.py:140`),
      the per-switch-device effective (`:146`), and the new gateway-effective
      iteration; the DHCP row screen must fire on **all three** (a `dhcpd_config.*`
      row appearing in any of them), so it belongs in `check_derived` after the
      path-membership check, not in a gateway-only branch — otherwise the switch/
      site `dhcpd_config` ripple the spec promises to tighten would be missed. The
      raw field gate keeps allowing the `dhcpd_config.*` leaf *paths* (so direct
      edits proceed to compile); the derived gate's value-aware screen is the
      authoritative UNKNOWN. **For the gateway family this REQUIRES the §1
      contract** — gateway effective must be published into the derived-gate
      iteration set (extend `device_effective` to gateways or a sibling map);
      without it the derived gate never diffs gateway effective and this screen
      cannot fire on a gateway. Tests must pin the ripple path explicitly (a
      `vars` edit producing effective relay-target `["10.1.1.1"] → ["10.2.2.2"]`,
      and a `vars` edit moving a non-serving row's `gateway`, each → UNKNOWN).
    **`port_config.*.usage` is deliberately EXCLUDED (was a P1 false-allow):** the
    gateway ingest copies it only into `Port.profile`, an **inert** IR field no
    check/representation/analysis reads — so a usage-only edit would pass the gate,
    change nothing the checks reason about, and could resolve SAFE. It stays **not
    allowlisted → UNKNOWN** until a check or gateway usage-resolution gives it
    meaning. Any other leaf — `port_config.*.mtu`, `ip_configs.*.netmask`, routing /
    BGP / tunnels / security policy, etc. — is likewise **not allowlisted → field
    gate → UNKNOWN** (fail-safe), never allowed-but-ignored. **`networks.*` is
    deliberately absent:** the gateway namespace is the **org networks list**
    (`raw.org_networks`, then `site_effective`), not the device's own `networks`,
    so a materialized `dev["networks"]` would be silently ignored — a
    `gatewaytemplate.networks.*` edit stays UNKNOWN. **Drift assertion (plan task):
    every allowlisted leaf is ingest-consumed AND its value influences a
    check/representation/analysis, and no such acted-on leaf is missing** — the
    standard is "consumed and acted on," not merely "read by ingest" (that's what
    catches `usage`). Consuming materialized gateway `networks` in namespace
    resolution + VLAN/DHCP minting is future work below.
  - `sitetemplate` = the **union of the modeled switch/site leaves and the
    modeled gateway leaves**, because sitetemplate sits in *both* stacks. This
    union MUST be **verified against the committed `sitetemplate` OAS / live
    shape**: keep a modeled gateway-affecting leaf in the allowlist only if the
    sitetemplate schema can actually carry it, and narrow the set only where the
    schema proves a leaf cannot appear. Do **not** assume "sitetemplate = the
    switch/site surface" — under-allowlisting would reject a sitetemplate change
    the MVP can actually analyze as UNKNOWN.
    - **Folding is role-projected — the union gates allow/deny only, it does NOT
      cross families (resolves the open question).** A sitetemplate is folded into
      *both* stacks, but each family's ingest consumes **only its own modeled
      leaves** from that stack's effective: the switch ingest reads switch leaves,
      the gateway ingest reads gateway leaves, and the inert keys for the other
      family sit unread in the effective dict. So a gateway-shaped sitetemplate
      leaf drives gateway IR (real signal) without manufacturing a phantom
      switch-side change, and vice versa. Assumption (flag if Mist differs): a
      sitetemplate leaf affects a family **iff** that family models it; this is
      safe whether the key is family-distinct or genuinely shared. **Test (use a
      genuinely family-distinct leaf):** a sitetemplate edit to `ip_configs.*.ip`
      (gateway-only — the switch path never reads it) moves only the gateway IR
      (switch verdict unchanged), and a switch-only leaf (e.g. a `port_usages`
      profile leaf) moves only the switch IR — no accidental cross-family
      behavior. **Do NOT use `dhcpd_config.*` for this test:** it is a genuinely
      *shared* leaf — the switch/site path reads `site_effective.dhcpd_config`
      (`ingest/switch.py:450,484`), so a `dhcpd_config` change in a sitetemplate
      legitimately moves the switch/site `dhcp_sources` too; that is shared-leaf
      behavior, not cross-family contamination, and using it would make the test
      misleadingly green.
- Committed OAS L0 schemas: `gatewaytemplate.schema.json`,
  `sitetemplate.schema.json` (added to the schema registry, like
  networktemplate). Provenance recorded in the OAS `VERSION`/source notes.
  **These files do not exist yet** — `oas/` holds only `device_switch`,
  `networktemplate`, `site_setting`. **Ordering dependency (was a P3):** the L0
  step (data-flow step 4) is *not* a given — committing + registering the two
  schemas is a prerequisite plan task, and a **missing/unregistered schema must
  fail closed → UNKNOWN**, never silently skip validation (assert this).
- **`IGNORED_RAW_FIELDS` audit (was a P3).** `scope/allowlist.py`'s
  `IGNORED_RAW_FIELDS` strips `id`/`org_id`/`site_id` but not template
  server-managed roots (assignment back-refs, `*_template_id`, etc.). Audit the
  committed `gatewaytemplate`/`sitetemplate` OAS for server-managed roots and add
  them, so a server-managed field on a fetched template object can't trip a
  spurious out-of-scope rejection (fail-safe direction → UNKNOWN, but noisy).
  **`deviceprofile_id` is dual-purpose — keep the stages distinct:** it must be
  ignorable by the *field gate* (so a device op carrying it doesn't trip a spurious
  rejection) yet still **readable** by the post-ingest *device-profile gate* (§5,
  conjunct 1). Those are different stages over different inputs (field gate over the
  raw op diff; device-profile gate over the retained `dev[...]`), so adding
  `deviceprofile_id` to `IGNORED_RAW_FIELDS` does not hide it from the
  device-profile gate — but the plan must verify the gate reads the raw dict, not a
  gate-filtered view.

### 5. Device-profile honesty (guardrail #3 — one deterministic, relevance-scoped rule)

The device-profile layer is **not modeled** (pre-existing gap since M1). Because
it *wins* over the template/sitetemplate/site_setting layers, silently ignoring
it can make an upper-layer template edit look more or less impactful than
reality. The MVP rule is deterministic and **relevance-scoped**:

> If a **modeled switch/gateway** device has a `deviceprofile_id` **and** the
> edited layer changes a modeled leaf that the unknown profile could override,
> that **site** cannot return SAFE — the MVP returns **UNKNOWN** via a device-
> profile **gate rejection**. Unrelated **AP** profiles and devices **not**
> affected by the edit do **not** taint the site or org verdict.

This avoids a noisy "any device-profile → UNKNOWN" (the current fixtures carry
many AP `deviceprofile_id`s).

**"Could override" is a concrete leaf set AND an affected-device test (was a P3
+ P2 gap).** Define `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` — the modeled
leaves a device-profile can override, keyed by device role (`switch` /
`gateway`). Start it conservatively from the modeled leaves each role actually
consumes from the device (gateway: `port_config` / `ip_configs` / `dhcpd_config`
— **not** `networks`, which the gateway path doesn't consume from the device,
§4; switch: the modeled switch/site leaves) and verify against the device-profile
OAS shape, so the set neither over-taints every template edit nor under-taints a
genuinely profile-overridable leaf (which would reintroduce a false SAFE).

The taint test has **two** conjuncts — both required:
1. the edit changes a leaf in that role's overridable set, **and**
2. the changed leaf is **affected for that specific device** — it participates in
   that device's effective config or its referenced network/usage path (e.g. the
   device's effective `port_config` references the changed `port_usages`/network
   key, or the changed leaf resolves onto that device). A change to an
   overridable-typed leaf the device does **not** reference (an unused or purely
   cosmetic change) does **not** taint.

This keeps the device-profile rule consistent with the cosmetic-edit → SAFE
golden: a no-op/unused edit affects no device's path, so it neither produces a
finding nor a profile taint.

**Mechanism — a gate Rejection, NOT a finding (was a P2 verdict-path gap).** In
the current engine, `UNKNOWN` is produced **only** by `rejections` / `l0_fatal` /
`baseline_unavailable` (`verdict/decision.py`); any *finding* — including an
operational ERROR/CRITICAL — floors at **REVIEW**, never UNKNOWN. So the
device-profile taint must **not** be expressed as a check/adapter finding or a
mere coverage note (that would yield REVIEW and contradict the honesty rail). It
is a per-site **`Rejection(stage="device_profile_gate", reasons=(…,))`** raised
when an in-scope modeled switch/gateway device with a `deviceprofile_id` is
**affected** (both conjuncts) by the edit. That rejection flows into the site's
`DecisionInputs.rejections` (exactly like the field/scope gates) →
`decide(...) → UNKNOWN` for that site → the existing `decide_org` rollup surfaces
it as a driving UNKNOWN site.

**Stage placement — POST-ingest, not the pre-ingest field gate (was a P2 timing
gap).** Conjunct 2 needs the device's **compiled effective** config / the IR
(does the device's effective `port_config` reference the changed network/usage
key?), which does **not** exist at the raw `screen_op` field-gate stage
(`pipeline.py:287`, before `adapter.ingest` at `:110/:117`); and gateways have no
`device_effective` at all (P1) — their reference data is the post-materialization
raw `dev[...]` / gateway effective. So the device-profile relevance test runs as a
**post-ingest stage inside `_simulate_site_state`** (after ingest, where
`device_effective`, the materialized gateway devices, and the IR are all
available), and injects its `Rejection` into the site's `DecisionInputs.rejections`
before `decide(...)`. The engine has no such hook today between ingest and verdict
— adding it is an explicit plan task (a `rejections` channel threaded from the
post-ingest stage into `decide`). **Conjunct-1 (`deviceprofile_id` present) reads
from the raw/materialized device dict, NOT the IR (was a P2 gap):** the `Device`
IR entity (`ir/entities.py:83`) does **not** carry `deviceprofile_id` (ingest
never copies it), so the gate reads it from the retained raw `dev[...]`
(`IGNORED_RAW_FIELDS`/identity stripping do not remove it). Conjunct-2 (the
changed leaf participates in that device's effective config) uses the
`device_effective` / materialized gateway dict + the IR. The relevance-scoping and
the per-site-UNKNOWN
outcome are locked. The full fix (model the layer) is the roadmap item added
2026-06-15.

### 6. Checks & verdict

No new analysis path. Switch checks consume the now-sitetemplate-aware switch
effective; the existing gateway checks (`wired.l3.gateway_gap.same_ip` /
`.gateway_unowned`, `wired.dhcp.scope_lint.gateway_mismatch`) consume the gateway
IR built from the new gateway effective device. `OrgVerdict` /
`org_verdict_to_dict` / `render_org_human` are reused.

**Driver mode-detection must become typed (was a P1 inaccuracy).** The CLI's
`_is_org_plan` (`drivers/cli.py`) currently hard-codes
`object_type == "networktemplate"`, and the MCP server reuses that same helper
(`drivers/mcp_server.py`). Both must dispatch on **`ORG_OBJECT_TYPES`** (all-ops
of any org type + no `site_id`) so `gatewaytemplate` / `sitetemplate` plans route
to `simulate_org_template`; otherwise they fall to the SITE path and return
UNKNOWN. The defensiveness is kept (malformed → SITE path → UNKNOWN, never a
crash). The `_RecordingProvider.resolve_org_template` delegate (and the
`StateProvider` protocol / `FixtureProvider` / `mist_api` impls) must adopt the
new `(scope, template_id, object_type)` signature. Tests: a `gatewaytemplate`
plan and a `sitetemplate` plan each route to the org path (not SITE/UNKNOWN) via
both CLI and MCP.

## Data flow — a gatewaytemplate edit

1. Plan: `gatewaytemplate` update, no `site_id`, one template id.
2. object_gate → ORG mode, `object_type = gatewaytemplate`.
3. `resolve_org_template(scope, id, "gatewaytemplate")` → template + assigned
   site ids (`site.gatewaytemplate_id == id`).
4. Org-level L0 (`gatewaytemplate.schema.json`) + field gate once: modeled
   gateway leaves allowed; any unmodeled field → UNKNOWN.
5. Apply the edit to one gatewaytemplate snapshot.
6. Per assigned site: fetch **every assigned layer needed for the full IR except
   the edited one** — `site_setting`, `sitetemplate`, the assigned
   `networktemplate` (for the switch-side context the gateway checks rely on),
   and devices (fetch-miss on any consumed layer → site UNKNOWN). The
   gatewaytemplate is **not** re-fetched: its baseline/proposed snapshots are
   pinned from step 3/5. Build the switch effective (from networktemplate +
   sitetemplate + site_setting) and the gateway site-effective baseline & proposed
   (`fold_layers`), overlay each gateway device (`effective_update`), and
   **materialize the gateway result back into the gateway-type entries of
   `RawSiteState.devices`** for both snapshots. Run the unchanged GS22 ingest →
   full IR → checks. Device-profile rule applied per the relevance-scoped
   leaf-set + affected-device test.
7. `decide_org` rollup → `OrgVerdict`.

## Error / honesty rails

- assignment / template / sitetemplate / gatewaytemplate fetch failure → UNKNOWN.
- unmodeled gateway field → field gate → UNKNOWN.
- `dhcpd_config.*.servers` both-non-empty relay-target change →
  `Rejection(stage="dhcp_relay_target")` → UNKNOWN (only the active/inactive
  boolean is modeled, not the target IPs). Evaluated on the effective diff in the
  derived gate, so a `vars`/override ripple into the relay target is caught too.
- `dhcpd_config.*.servers` changed on a row serving on **both** sides (S→S) →
  `Rejection(stage="dhcp_inert_servers")` → UNKNOWN (`servers` is inert on a
  serving row; a participation-crossing `servers` change is handled by the
  participation subrule / `dhcp_path` instead).
- `dhcpd_config.*.{ip_start,ip_end,gateway}` change on a row that is non-serving
  (relay/none) on **both** sides → `Rejection(stage="dhcp_scope_field")` →
  UNKNOWN (no `DhcpScope` is minted, so the change is invisible to the checks).
  Also evaluated on the effective diff in the derived gate (ripple-safe).
- `dhcpd_config.*` row where **both sides are active** and their relay-target
  identity differs → UNKNOWN: exactly one side an active relay (serving↔active-relay,
  **either** direction → `dhcp_mode_transition`), or both active relays with
  differing `servers` (`dhcp_relay_target`). Pure row-local predicate
  (`_dhcp_active` / `_dhcp_serves_scope`), no check-output dependency; any change
  with ≥1 inactive side (e.g. serving→relay with empty `servers`) is NOT rejected
  (stays a modeled `dhcp_path` provider gain/loss verdict).
- relevant device-profile detected → `Rejection(stage="device_profile_gate")` →
  that site UNKNOWN (relevance-scoped; a gate rejection, not a REVIEW finding).
- 0 assigned sites → SAFE (existing contract).
- wrong plan-mode (site-scoped plan to org path or vice versa) → UNKNOWN
  (existing symmetric guards).
- a blind spot never resolves SAFE; a guessed POSITIVE fact is worse than a
  missed one (carried doctrine).

## Testing

Unit:
- `fold_layers` — layer order, per-field policy, provenance, `None`-layer skip.
- typed `resolve_org_template` — assignment by each id field.
- sitetemplate compile layer (switch + gateway), baseline-gap fix.
- gateway compile → **materialize into `RawSiteState.devices`** → GS22 IR
  equivalence (a gatewaytemplate edit actually moves the gateway IR; switch/AP
  device entries untouched).
- **gateway effective is in the derived-gate set** — a `vars`/sitetemplate ripple
  into an effective gateway leaf **outside `GATEWAY_EFFECTIVE_ALLOWLIST`** (use an
  actually-unmodeled gateway leaf, e.g. `ip_configs.*.netmask`) → `check_derived`
  → UNKNOWN (today the derived gate iterates switch-only `device_effective` and
  would miss it).
- **`vars.*` allowlisted on the gateway effective** — a benign gateway `vars` edit
  whose ripple lands ONLY in in-scope modeled leaves is **not** rejected on the
  residual `vars.*` path itself (the `vars` root survives `_resolve`); it resolves
  per the modeled ripple (SAFE/REVIEW), not a generic `derived_gate` UNKNOWN.
- **gateway-specific `disabled` drift** — a gatewaytemplate `port_config.*.disabled`
  flip **reaches the gateway check and moves a verdict** (NOT a derived-gate
  UNKNOWN), proving the gateway effective is screened against the gateway effective
  allowlist (which includes `port_config.*.disabled` / `ip_configs.*.ip`), not the
  switch `EFFECTIVE_ALLOWLIST` which omits them (Finding-1 regression guard).
- **single-site replay round-trips the new fields** — a fixture saved with
  `sitetemplate`/`gatewaytemplate` reloads them (and a legacy fixture lacking the
  keys loads them as `None`), so a replayed verdict compiles the same layers and
  is not optimistically SAFE.
- **gateway fold resolves `{{vars}}`** — a gatewaytemplate/sitetemplate `{{var}}`
  in `dhcpd_config`/`ip_configs` is substituted in the effective gateway device
  (so the vars-ripple DHCP screens actually fire on gateways).
- device-profile rule — **both** conjuncts (leaf in
  `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` **and** affected for that device's
  path) → `Rejection(stage="device_profile_gate")` → site **UNKNOWN** (assert
  UNKNOWN, **not** REVIEW — the verdict-path that the engine actually produces);
  a non-overridable leaf, an unrelated AP profile, an unaffected device, or an
  unused/cosmetic overridable-typed edit → no taint (the last pins consistency
  with the cosmetic-SAFE golden).
- `gatewaytemplate.networks.*` edit → field gate → UNKNOWN (not allowlisted;
  gateway namespace is `org_networks`, not consumed from the device in MVP).
- **gatewaytemplate ignored-leaf false-allow guard** — an edit to a leaf the
  ingest never reads (`port_config.*.mtu`, `ip_configs.*.netmask`) **and** to a
  read-but-inert leaf (`port_config.*.usage` → `Port.profile`) → field gate →
  UNKNOWN, **not** SAFE; plus the drift assertion (every allowlisted leaf is
  consumed **and acted on** by a check/representation/analysis, and every such
  acted-on leaf is allowlisted).
- **`dhcpd_config.*.servers` value-aware screen** — on a row that stays a `relay`
  both sides: both-non-empty target change (`["10.1.1.1"] → ["10.2.2.2"]`) →
  `dhcp_relay_target` UNKNOWN, an empty↔non-empty activation/deactivation → allowed
  (modeled provider gain/loss via `dhcp_path`); on a row **serving on BOTH sides
  (S→S)**: any `servers` change → `dhcp_inert_servers` UNKNOWN (inert);
  **participation-crossing** `servers` change (e.g. `local,["x"]→relay,[]`) → NOT
  this screen — governed by the participation subrule / `dhcp_path` (REVIEW). Assert
  the screen fires on the shared leaf for the switch/site path too (not gateway-only).
- **`dhcpd_config.*.type` mode-transition screen (row-local, BOTH directions)** —
  `type: local → relay` with **unchanged non-empty** `servers` → UNKNOWN (silent
  serving→active-relay to an unmodeled target); **the reverse `relay` (non-empty
  `servers`) → `local`/`server` with the same `servers`** → **UNKNOWN** too (active
  on both sides, relay target silently disappears — the symmetric case); **`type:
  local → relay` with empty/absent `servers`** → **NOT preempted** (≥1 inactive
  side) — stays a modeled `dhcp_path` provider-loss verdict (REVIEW), proving the
  exemption is row-local, not check-output-dependent; serving→`none` → allowed
  (provider loss via `dhcp_path`); `local→server` → no-op SAFE.
- **full 3×3 participation matrix + the two inert-leaf screens** — parametrize all
  nine (S/R/I)×(S/R/I) baseline→proposed transitions (R→R covers same *and*
  differing `servers` as two subcases); assert the **three** participation-UNKNOWN
  transitions (S→R, R→S, R→R-differing) and
  that the rest are SAFE/REVIEW via `dhcp_path`/`scope_lint`. **Plus the orthogonal
  inert-leaf cases that compose ON TOP within allowed cells:** a `servers` edit on
  an S→S row → UNKNOWN (inert-servers), and an `ip_start`/`gateway` edit on a
  both-non-serving (R/I) row → UNKNOWN (scope-field). Pins the complete rejection
  set — no transition AND no inert-leaf edit unscreened.
- **DHCP screens run on the effective (derived) diff, not only the raw gate** —
  a `vars.*` edit that compiles into an effective relay-target change
  (`["10.1.1.1"] → ["10.2.2.2"]`) → UNKNOWN; a `vars.*` edit that compiles into a
  non-serving row's `gateway`/range change → UNKNOWN; **a `vars.*` edit that
  compiles `type: local → relay` with unchanged non-empty `servers`** → UNKNOWN
  (pins the effective-`type`-ripple placement guarantee). (The raw-leaf direct-edit
  cases above must hold via the same derived-gate screen, so direct + ripple are
  covered by one path.)
- **sitetemplate role-projection** — a sitetemplate edit to a **family-distinct**
  gateway-only leaf (`ip_configs.*.ip`) moves only the gateway IR (switch verdict
  unchanged); a switch-only leaf moves only the switch IR — no accidental
  cross-family behavior. (NOT `dhcpd_config.*`, which is a genuinely shared leaf.)
- **edited layer not re-fetched per site**; **every other assigned layer needed
  for the full IR is fetched** — incl. the cross-stack one (a `gatewaytemplate`
  edit fetches the assigned `networktemplate`; a `networktemplate` edit fetches
  the assigned `gatewaytemplate`) — and any consumed-layer fetch-miss → UNKNOWN.
- **driver mode-detection** — a `gatewaytemplate` plan and a `sitetemplate` plan
  each route to the org path (not SITE/UNKNOWN) via **both** CLI and MCP;
  malformed → SITE path → UNKNOWN (no crash).
- **typed replay shape** — `resolve_org_template` filters the fixture's sites by
  `site.<object_type>_id` per type; the legacy single-`"template"` doc still
  loads as a `networktemplate` (back-compat); wrong-org / missing-template
  strictness holds per type.

Goldens:
- sitetemplate edit breaks a switch leaf at one site → org UNSAFE naming it.
- gatewaytemplate edit → gateway `same_ip` / `gateway_unowned` → org UNSAFE.
- gatewaytemplate edit on an unmodeled field → UNKNOWN.
- sitetemplate fetch-fail site → UNKNOWN.
- cosmetic edit → SAFE.
- device-profile-present, edit hits an overridable leaf → UNKNOWN; AP-profile-
  only site → unaffected.

Live (read-only / simulate-only):
- fan-out on a real `gatewaytemplate` and a real `sitetemplate` assigned to
  sites; the 8 single-site plans unchanged.
- **verify `GATEWAY_POLICY` against Mist's real layering** — confirm whether
  gateway `port_config` / `ip_configs` / `dhcpd_config` merge per-key (DICT_MERGE)
  or REPLACE across the `gatewaytemplate → sitetemplate → site_setting` fold, and
  whether gateway *device* objects can carry `{{vars}}` (which would force
  `_resolve`-last, already adopted). Harden `GATEWAY_POLICY`/`_POLICY` from the
  observed `derived_setting` like the existing Tier-2 gate.

Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Out of scope (recorded, not built)

device-profile layer (modeled only as relevance-scoped UNKNOWN; roadmap item);
gateway routing/BGP/tunnels/security policy (→ UNKNOWN); **DHCP relay target-IP
modeling** — the IR models `servers` only as an active/inactive boolean, so a
both-non-empty relay-target change is gated to UNKNOWN rather than analyzed;
modeling relay target IPs (in `Vlan.dhcp_sources` or a new field) + a check would
let such edits resolve SAFE/REVIEW (applies to switch/site DHCP too); **`gatewaytemplate.
networks` consumption** — the gateway namespace is `org_networks` and
`_gateway_ports_and_l3` / VLAN+DHCP-scope minting don't read a device's own
`networks`; until that path consumes the materialized gateway `networks`, a
`gatewaytemplate.networks` edit stays UNKNOWN (future work); `aptemplate`;
`switch_matching` (denied); template `delete`-ripple; multiple templates per
plan; other org objects (`org_networks`, WLAN/RF templates).
