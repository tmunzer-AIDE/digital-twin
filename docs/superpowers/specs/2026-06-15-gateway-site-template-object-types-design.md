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
`RawSiteState.devices`** (replacing only the modeled gateway leaves —
`port_config`, `ip_configs`, `dhcpd_config`, and any modeled `networks` — on
`type == "gateway"` devices; switch/AP devices untouched) for **both** the
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
- **The edited layer is not re-fetched per site (was a P2 over-requirement).**
  The edited layer's baseline snapshot already comes from `resolve_org_template`
  (one org-level fetch) and is **pinned** into every assigned site (§3). The
  per-site fetch therefore requires only the **non-edited assigned layers** that
  the affected compile actually consumes (e.g. a `gatewaytemplate` edit fetches
  per site `site_setting` + `sitetemplate` + devices, **not** the gatewaytemplate
  again). This avoids a duplicate per-site template fetch that could manufacture
  a false UNKNOWN even though the exact edited snapshot is already in hand.
- **Fetch-miss rule (guardrail #4):** for a **non-edited assigned layer the
  simulation consumes**, a site assigned that layer which fails to fetch must
  **not** compile without it — it is a recorded per-site fetch failure → that
  site is UNKNOWN (the existing `site_failures` → org rollup UNKNOWN path), never
  a silent SAFE.

### 3. Org fan-out — pin exactly one edited layer (guardrail #2)

`apply_template` / `override_template` are parametrized by the edited
`object_type`. For an edit to layer **X**, baseline vs proposed differ **only**
at layer X; every other fetched layer (the other templates, `sitetemplate`,
`site_setting`) stays **pinned** per site to a single snapshot (the fetch-race
guardrail, carried from the networktemplate doc). So each site's diff is exactly
the edit and nothing else moves:

- `networktemplate` edit → pins networktemplate, holds sitetemplate +
  site_setting; gateway effective is identical baseline/proposed → no gateway
  findings.
- `sitetemplate` edit → pins sitetemplate, holds both org templates +
  site_setting; re-derives **both** switch and gateway effective (sitetemplate is
  in both stacks).
- `gatewaytemplate` edit → pins gatewaytemplate, holds sitetemplate +
  site_setting; switch effective identical → no switch findings.

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
  - `gatewaytemplate` = only the **modeled** gateway leaves that feed GS22 IR:
    `networks`, `port_config`, `ip_configs`, `dhcpd_config`. Everything else
    (routing, BGP, tunnels, security policy) is **not allowlisted → field gate →
    UNKNOWN** (fail-safe).
  - `sitetemplate` = the **union of the modeled switch/site leaves and the
    modeled gateway leaves**, because sitetemplate sits in *both* stacks. This
    union MUST be **verified against the committed `sitetemplate` OAS / live
    shape**: keep a modeled gateway-affecting leaf in the allowlist only if the
    sitetemplate schema can actually carry it, and narrow the set only where the
    schema proves a leaf cannot appear. Do **not** assume "sitetemplate = the
    switch/site surface" — under-allowlisting would reject a sitetemplate change
    the MVP can actually analyze as UNKNOWN.
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
> that **site** cannot return SAFE — the MVP returns **UNKNOWN** with a
> device-profile coverage rejection/note. Unrelated **AP** profiles and devices
> **not** affected by the edit do **not** taint the site or org verdict.

This avoids a noisy "any device-profile → UNKNOWN" (the current fixtures carry
many AP `deviceprofile_id`s).

**"Could override" is a concrete, conservative leaf set (was a P3 gap).** Define
`DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` — the modeled leaves a device-profile
can override, keyed by device role (`switch` / `gateway`). The relevance test is
exactly: *the edit changes a leaf in that role's set, for an in-scope modeled
device of that role carrying a `deviceprofile_id`.* Start it conservatively from
the modeled leaves each role actually consumes (e.g. gateway:
`port_config` / `ip_configs` / `dhcpd_config` / `networks`; switch: the modeled
switch/site leaves) and verify against the device-profile OAS shape, so the set
neither over-taints every template edit nor under-taints a genuinely
profile-overridable leaf (which would reintroduce a false SAFE). Mechanism: an
analysis-context flag set when an in-scope modeled device has a profile id,
consulted only against this leaf set for the leaves the edit actually changes.
The relevance-scoping and the "cannot return SAFE for that site" outcome are
locked. The full fix (model the layer) is the roadmap item added 2026-06-15.

### 6. Checks & verdict

No new analysis path. Switch checks consume the now-sitetemplate-aware switch
effective; the existing gateway checks (`wired.l3.gateway_gap.same_ip` /
`.gateway_unowned`, `wired.dhcp.scope_lint.gateway_mismatch`) consume the gateway
IR built from the new gateway effective device. `OrgVerdict` /
`org_verdict_to_dict` / `render_org_human` are reused; CLI/MCP dispatch by mode
is already object_type-agnostic (defensive — malformed → SITE path → UNKNOWN).

## Data flow — a gatewaytemplate edit

1. Plan: `gatewaytemplate` update, no `site_id`, one template id.
2. object_gate → ORG mode, `object_type = gatewaytemplate`.
3. `resolve_org_template(scope, id, "gatewaytemplate")` → template + assigned
   site ids (`site.gatewaytemplate_id == id`).
4. Org-level L0 (`gatewaytemplate.schema.json`) + field gate once: modeled
   gateway leaves allowed; any unmodeled field → UNKNOWN.
5. Apply the edit to one gatewaytemplate snapshot.
6. Per assigned site: fetch only the **non-edited** consumed layers —
   `site_setting`, `sitetemplate`, devices (fetch-miss on `sitetemplate` → site
   UNKNOWN). The gatewaytemplate is **not** re-fetched: its baseline/proposed
   snapshots are pinned from step 3/5. Build gateway site-effective baseline &
   proposed (`fold_layers`), overlay each gateway device (`effective_update`),
   and **materialize the result back into the gateway-type entries of
   `RawSiteState.devices`** for both snapshots. Run the unchanged GS22 gateway
   ingest → gateway IR → gateway checks. Device-profile rule applied per the
   relevance-scoped leaf-set test.
7. `decide_org` rollup → `OrgVerdict`.

## Error / honesty rails

- assignment / template / sitetemplate / gatewaytemplate fetch failure → UNKNOWN.
- unmodeled gateway field → field gate → UNKNOWN.
- relevant device-profile detected → that site UNKNOWN (relevance-scoped).
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
- device-profile rule — leaf in `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` for a
  relevant modeled device → site UNKNOWN; edit hitting a non-overridable leaf, an
  unrelated AP profile, or an unaffected device → no taint.
- **edited layer not re-fetched per site**; a **non-edited** assigned layer
  (e.g. sitetemplate) fetch-miss → UNKNOWN.

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
gateway routing/BGP/tunnels/security policy (→ UNKNOWN); `aptemplate`;
`switch_matching` (denied); template `delete`-ripple; multiple templates per
plan; other org objects (`org_networks`, WLAN/RF templates).
