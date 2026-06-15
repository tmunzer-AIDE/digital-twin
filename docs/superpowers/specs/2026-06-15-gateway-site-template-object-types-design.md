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
  absent; **fixes the latent baseline gap** when it is present.
- gateway site-effective → `fold_layers([gatewaytemplate, sitetemplate,
  site_setting], GATEWAY_POLICY)`, then the per-device PUT-root overlay
  (`effective_update`) → an **effective gateway device**. `GATEWAY_POLICY` starts
  equal to `SWITCH_POLICY` for the shared keys and is hardened by the live gate.

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
baseline and proposed snapshots, *before* ingest runs. The existing GS22 ingest
then consumes them **unchanged** (guardrail #5 — no second gateway analysis
path). Rejected alternative: teaching the ingest to read a separate
`ctx.gateway_device_effective` source — that forks the device read path for one
family and is exactly the parallel path #5 forbids.

### 2. Provider surface

- `resolve_org_template(scope, template_id, object_type)` — generalized: filter
  the org's sites by `<object_type>_id` and fetch the template of that type.
  Returns the existing `OrgTemplateContext(template, assigned_site_ids)`. Lookup
  failure (sites list or template) → `FetchError` → UNKNOWN (unchanged contract).
- `RawSiteState` gains `sitetemplate: JsonObj | None` and
  `gatewaytemplate: JsonObj | None` alongside today's `networktemplate`. The
  per-site fetch pulls the site's assigned ones (by `sitetemplate_id` /
  `gatewaytemplate_id`).
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
the edit and nothing else moves:

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
      safe whether the key is family-distinct or genuinely shared. **Test:** a
      sitetemplate edit to a gateway-only leaf moves only the gateway IR (switch
      verdict unchanged), and a switch-only leaf moves only the switch IR — no
      accidental cross-family behavior.
- Committed OAS L0 schemas: `gatewaytemplate.schema.json`,
  `sitetemplate.schema.json` (added to the schema registry, like
  networktemplate). Provenance recorded in the OAS `VERSION`/source notes.

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
it as a driving UNKNOWN site. The relevance-scoping and the per-site-UNKNOWN
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
- **sitetemplate role-projection** — a sitetemplate edit to a gateway-only leaf
  moves only the gateway IR (switch verdict unchanged); a switch-only leaf moves
  only the switch IR — no accidental cross-family behavior.
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

Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Out of scope (recorded, not built)

device-profile layer (modeled only as relevance-scoped UNKNOWN; roadmap item);
gateway routing/BGP/tunnels/security policy (→ UNKNOWN); **`gatewaytemplate.
networks` consumption** — the gateway namespace is `org_networks` and
`_gateway_ports_and_l3` / VLAN+DHCP-scope minting don't read a device's own
`networks`; until that path consumes the materialized gateway `networks`, a
`gatewaytemplate.networks` edit stays UNKNOWN (future work); `aptemplate`;
`switch_matching` (denied); template `delete`-ripple; multiple templates per
plan; other org objects (`org_networks`, WLAN/RF templates).
