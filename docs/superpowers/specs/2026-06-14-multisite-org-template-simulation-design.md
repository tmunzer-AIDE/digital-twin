# Multi-site / org-template simulation (design)

Date: 2026-06-14
Status: approved (networktemplate vertical slice; per-site verdicts + org rollup;
all assigned sites auto-resolved; separate `simulate_org_template` entry)
Roadmap mapping: starts ROADMAP §3 "multi-site / org-template simulation" +
"networktemplate / sitetemplate as first-class object_types" (the networktemplate
slice).

## Problem / goal

The twin is single-site: a `ChangePlan` scoped to one site is fetched, applied,
checked, and returned as one `Verdict`. But the highest-blast-radius change in
Mist is an **org-level `networktemplate` (switch template) edit**: it changes the
INHERITED config layer of every site assigned to it at once (networks,
port_usages, vars, STP, DHCP, snooping, OSPF). Today such a change is honestly
rejected → UNKNOWN (`object_gate`), so the twin can't tell an operator *what
breaks across sites* when they edit a shared template.

Goal: simulate a `networktemplate` edit across **all sites assigned to that
template**, returning each site's full `Verdict` plus an org-level rollup
decision that names where it breaks — reusing the entire existing per-site
pipeline.

## Scope (MVP)

- **`networktemplate` (switch template) only.** `gatewaytemplate` (gateways
  aren't a compile target — GS22 reads them raw) and `sitetemplate` are deferred.
- **All assigned sites, auto-resolved** by `site.networktemplate_id`. No subset
  selection (chosen for simplicity over a big-org perf knob).
- **Per-site `Verdict` + org rollup** under the existing
  `UNKNOWN > UNSAFE > REVIEW > SAFE` precedence.
- **Exactly one template per plan** (the single-template-id invariant below).
- Single-site `simulate()` is **unchanged**; every GS golden stays byte-identical.

## Architecture

### 1. Pipeline split (the reuse seam)

Stages 4–9 of `engine/pipeline.py:simulate` (ingest baseline → ingest proposed →
dynamic gate → derived gate → checks → verdict) are already pure per-site
state→verdict logic. Extract them:

```
_simulate_site_state(
    baseline_raw: RawSiteState,
    proposed_raw: RawSiteState,
    *, run, adapter, registry, adapter_findings: tuple[Finding, ...] = (),
) -> Verdict
```

Both entries call it; neither fetches inside it:

- **`simulate(plan, provider) -> Verdict`** — UNCHANGED behavior. `fetch_site` →
  apply ops (rolling, with per-op L0 + field gate + screen_op) → `proposed_raw` →
  `_simulate_site_state`.
- **`simulate_org_template(plan, provider) -> OrgVerdict`** — NEW (below).

### 2. Scope / plan model + object_gate mode classification

A plan is classified into exactly one mode by its ops:

- **SITE mode** — all ops `site_setting`/`device`; `scope.site_id` REQUIRED;
  today's rules unchanged.
- **ORG mode** — all ops `networktemplate`; `scope.org_id` required and
  `scope.site_id` ABSENT.

`networktemplate` goes in a NEW `ORG_OBJECT_TYPES = ("networktemplate",)`, kept
OUT of `SUPPORTED_OBJECT_TYPES` (so single-site still rejects templates).
Rejections (whole-plan UNKNOWN at the gate):

- **Mixing** SITE and ORG object_types in one plan.
- **ORG mode with `site_id` present**, or SITE mode with `site_id` absent.
- **(Guardrail #1) One template, one op** — the envelope already enforces
  one-op-per-`(object_type, object_id)` (`envelope.py`: "two ops target the same
  object — full replacement makes the earlier op dead"). Combined with the
  single-template rule, an ORG plan therefore carries EXACTLY ONE op: the one
  `networktemplate` edit. The object_gate additionally rejects a plan whose
  `networktemplate` ops carry more than one DISTINCT `object_id`
  ("one template per plan in M1"). (Multiple ops on one template are already an
  envelope rejection, so no rolling-apply over a template is possible or needed.)

The CLI/MCP driver dispatches to `simulate` or `simulate_org_template` by mode.

### 3. Template apply + the baseline-template snapshot rule (guardrail #3)

The `networktemplate` is an org object shared by every assigned site. The org
path uses ONE resolved snapshot so the per-site diff is EXACTLY the edit:

1. `resolve_org_template(scope, template_id)` returns the current template JSON
   (the **baseline snapshot**) + the assigned site ids.
2. `proposed_template = effective_update(snapshot, op.payload)` for the SINGLE
   template op (the existing root-level merge + `-key` deletion + identity
   preservation; `update_conflicts` rejects a set-AND-delete on the same
   attribute → UNKNOWN). One op, so this is a single application — no rolling.
3. Per assigned site, AFTER `fetch_sites`, OVERRIDE the template on both sides
   from the single snapshot — never the per-site-fetched copy:
   - `baseline_raw = replace(fetched_raw, networktemplate=snapshot)`
   - `proposed_raw = replace(fetched_raw, networktemplate=proposed_template)`

This eliminates the race where `resolve_org_template` and `fetch_sites` fetch the
template at different instants: the baseline and proposed differ ONLY by the edit,
against ONE snapshot. (`fetch_sites` still fetches the template per unique id —
deduped to one call by its `nt_cache` — and we discard it; an optional
`fetch_sites(..., skip_networktemplate=True)` is a later optimization, not MVP.)

`compile_site`/`compile_device` already take the template as their first arg, so
each site's effective config recomputes with the edit. **No compile changes.**

### 4. Gates — org-level once, per-site after

- **Org-level (run ONCE on the proposed template, before fan-out):**
  - **L0** validate `proposed_template` against `networktemplate.schema.json`
    (add it to the L0 schema map). FATAL → whole-plan UNKNOWN, recorded as a
    `Rejection(stage="l0")` in `org_rejections` — NOT as a `Finding` (single-site
    fatal L0 is a control-path condition: `DecisionInputs.l0_fatal`, never a
    normal adapter finding; the org path mirrors that by treating it as a
    short-circuit rejection). NON-fatal schema violations → operational `Finding`s
    on `OrgVerdict.template_findings` (guardrail #5) that floor the rollup to
    REVIEW (the existing operational-ERROR→REVIEW rule), NOT duplicated per site.
  - **Field gate** — the template edit's changed leaves vs a NEW
    `RAW_ALLOWLIST["networktemplate"]`. A `screen_op` template branch with NO
    device-role check (there is no device). Out-of-scope leaf → whole-plan
    UNKNOWN.
- **Per-site (inside `_simulate_site_state`, unchanged):** the dynamic gate and
  derived gate run on each site's effective config. A template edit that ripples
  into an out-of-scope EFFECTIVE leaf on some site correctly floors THAT site to
  UNKNOWN (per-site default-deny is preserved).

### 5. networktemplate allowlist scope (guardrail #4 — explicit)

`RAW_ALLOWLIST["networktemplate"]` = the **EXACT same leaf tuple** as
`RAW_ALLOWLIST["site_setting"]` (reuse the constant — do NOT hand-copy, and do
NOT broaden to subtree wildcards; the leaf-tightened doctrine forbids it). That
tuple is leaf-level — e.g. `networks.*.vlan_id`, `networks.*.subnet`,
`networks.*.gateway`, `port_usages.*.mode`, `stp_config.bridge_priority`,
`dhcpd_config.*.type`, `dhcp_snooping.enabled`, `ospf_config.enabled`,
`ospf_areas.*.networks.*.passive`, `vars.*`, … — NOT `networks.*`/`port_usages.*`
subtrees. `switch_matching` is ABSENT from that tuple (as for `site_setting`), so
a template edit changing switch-matching / port-assignment rules → UNKNOWN. The
MVP thus covers **shared networks / usages / STP / DHCP / snooping / OSPF / vars
only**; template-matching and port-assignment changes remain UNKNOWN (honest —
their device-port impact is not modeled as in-scope). An unmodeled leaf can never
simulate as SAFE.

### 6. Provider extension

One new protocol method:

```
resolve_org_template(scope: OrgScope, template_id: str)
    -> OrgTemplateContext | FetchError
# OrgTemplateContext(template: JsonObj, assigned_site_ids: tuple[str, ...])
```

Bundles `listOrgSites` (filter to `site.networktemplate_id == template_id`) +
fetch the template. **Guardrail #2:** if the lookup itself FAILS (listOrgSites or
the template fetch errors) → `FetchError` → whole-plan UNKNOWN. A SUCCESSFUL
lookup returning **0 assigned sites** → `OrgVerdict` **SAFE** with reason
"template assigned to no sites; no impact simulated" (the two cases are
distinct). The heavy per-site fetch reuses the existing
`fetch_sites(scope, site_ids=assigned)` (batched; per-site failures isolated).

**Contract change (finding):** today `FetchError.scope` is typed `SiteScope`, but
an org-level lookup failure must carry an `OrgScope`. Broaden it to
`FetchError.scope: SiteScope | OrgScope` (a one-line widening; the single-site
path and its tests are unaffected). The org engine treats a `resolve_org_template`
`FetchError` as the whole-plan short-circuit (UNKNOWN, no fan-out), recording it in
`OrgVerdict.org_rejections`/`decision_reasons`.

`MistApiProvider` already has the internals (`_org_sites`, `_networktemplate`);
`FixtureProvider` reads a multi-site fixture.

### 7. `OrgVerdict` (the result)

```python
@dataclass(frozen=True)
class OrgVerdict:
    decision: Decision                       # rollup (precedence; see below)
    decision_reasons: tuple[str, ...]        # names the driving sites / template
    template_id: str
    per_site: Mapping[str, Verdict]          # full Verdict per site (incl. fetch-fail UNKNOWNs)
    driving_sites: tuple[str, ...]           # sites whose Verdict == the rollup decision
    site_failures: Mapping[str, str]         # site_id -> fetch error (surfaced subset)
    template_findings: tuple[Finding, ...]   # org-level (template-edit) L0 Findings only
    org_rejections: tuple[Rejection, ...]    # field-gate/conflict/lookup/fatal-L0 Rejections
```
(Per-site freshness lives on each `per_site[*].state_meta`; no separate org
state_meta in MVP. `site_failures` surfaces the fetch errors.)

The two structured failure channels are kept distinct by TYPE, mirroring the
single-site split: `template_findings` holds `Finding`s — **non-fatal template L0
schema violations ONLY** (operational, REVIEW-driving); `org_rejections` holds
`Rejection`s — every short-circuit cause: the org field-gate rejection, the
`update_conflicts` set-AND-delete, the assignment-lookup `FetchError` rendered as
a Rejection, AND a **fatal template L0** as `Rejection(stage="l0")` (consumers
distinguish causes by `Rejection.stage`). `decision_reasons` is the
human-readable flattening of both (as in the single-site `unknown(rejection)`
path).

**Short-circuit before fan-out (whole-plan UNKNOWN, no per-site work):** an
assignment-lookup failure (guardrail #2), a fatal template L0, the set-AND-delete
conflict, or an org field-gate rejection each return an `OrgVerdict` with
`decision=UNKNOWN`, empty `per_site`, the structured cause on `org_rejections`,
and the human string on `decision_reasons`. (`template_findings` never carries a
short-circuit cause — only non-fatal L0.)

Otherwise fan out, then roll up. Rollup `decision` = the WORST under
`UNKNOWN > UNSAFE > REVIEW > SAFE` over BOTH:
- every `per_site` Verdict's decision (a fetch-failed site → UNKNOWN, assembled
  from its `FetchError` via the existing total-fetch-failure path — never
  dropped), AND
- `template_findings` (here only NON-fatal template L0 violations): an
  operational ERROR/CRITICAL → REVIEW floor (mirrors the single-site
  operational-finding rule).

`driving_sites` lists the sites at the rollup level so the operator sees where it
breaks. A valid template assigned to 0 sites → SAFE (above).

### 8. Drivers + tests

- **CLI/MCP**: detect ORG mode → `simulate_org_template` → render the org decision
  + a per-site table (decision, top finding, freshness); exit code from the org
  decision. New `org_verdict_to_dict` + `render_org_human`.
- **Multi-site fixture infra**: a small `FixtureProvider` multi-site fixture — a
  sites index (id + `networktemplate_id`) + per-site raw states sharing one
  `networktemplate` — with redaction applied (reuse the existing redactor).
- **Goldens** (org-level):
  - **MS-a** — template edit removes / re-VLANs a network that site A's IRB/exit
    depends on but site B doesn't use → site A UNSAFE (e.g. `gateway_gap.removed`
    / `l2.blackhole.exit_lost`), site B SAFE → org rollup **UNSAFE** naming A.
  - **MS-b** — one assigned site's fetch fails → that site UNKNOWN → rollup
    **UNKNOWN**; the others still evaluate.
  - **MS-c** — cosmetic template edit (e.g. add an unused vlan) → all sites SAFE →
    rollup **SAFE**.
  - **MS-d** — assignment lookup fails → whole-plan **UNKNOWN** (guardrail #2);
    and a separate 0-assigned-sites case → **SAFE**.
- **Unit tests**: object_gate ORG-mode classification + the single-template-id /
  mixing / site_id rejections; template apply + `update_conflicts`; assignment
  filtering; the snapshot-override rule (baseline/proposed differ only by the
  edit); aggregation precedence incl. `template_findings`; the networktemplate
  allowlist (shared leaves allowed, `switch_matching` denied) + L0 schema.
- **Live verification**: a read-only `simulate_org_template` against a real
  networktemplate assigned to ≥2 sites (the org has multi-site templates) — assert
  it runs end-to-end and the rollup is internally consistent with the per-site
  verdicts.

## Honesty rails (carried from the single-site doctrine)

| Situation | Behavior |
|---|---|
| Assignment lookup (listOrgSites / template fetch) fails | whole-plan **UNKNOWN** (guardrail #2) — never SAFE |
| Template valid, assigned to 0 sites | **SAFE** + "no impact simulated" reason |
| One site's fetch fails | that site → UNKNOWN (from its FetchError), contributes UNKNOWN to the rollup; other sites still evaluate |
| Template edit touches `switch_matching` / an out-of-scope leaf | whole-plan **UNKNOWN** (org field gate) |
| Template ripples into an out-of-scope EFFECTIVE leaf on one site | THAT site → UNKNOWN (per-site derived gate); other sites unaffected |
| Non-fatal template L0 violation | operational finding on `template_findings` → REVIEW floor; not duplicated per site |
| Fatal template L0 / set-AND-delete conflict | whole-plan **UNKNOWN** |
| Multiple template ids / mixed SITE+ORG ops | rejected at object_gate → UNKNOWN |

## Out of scope (recorded, not built)

- `gatewaytemplate` (no gateway compile path) and `sitetemplate` simulation.
- The `delete` action on a template (object deletion) — still rejected → UNKNOWN
  (ROADMAP §3 entry); this MVP is template UPDATE only.
- Multiple templates per plan; an explicit site-subset selector.
- `switch_matching` / port-assignment-rule impact modeling.
- `org_networks` and other org objects (WLAN templates, RF templates) as
  first-class change types.
- A `fetch_sites(skip_networktemplate=True)` fetch optimization (the snapshot
  override makes the per-site template fetch redundant but harmless; `nt_cache`
  caps it at one call).
