# OAS unknown-attribute check — design

**Date:** 2026-06-24
**Status:** approved (brainstorm), pre-plan — gated on the OAS-refresh prerequisite (§1)
**Component:** L0 / adapter validation (`adapters/mist/validate`)

## 1. Problem

L0 validation today is **permissive about extra keys**. The committed Mist OAS
schemas do not set `additionalProperties: false` anywhere, so an attribute that is
**not documented in the OAS** passes L0 silently. Concrete case that motivated
this: a switch `port_config.<port>.disabled` field. `disabled` is **not a valid
switch `port_config` attribute** (confirmed by the OAS owner; it is a *gateway*
port_config field, not a switch one — 0 of 47 real switch ports in the captured
fixture carry it), yet it sailed through L0 with zero findings, caught only later
(and only incidentally) as a generic field-gate "out of scope" UNKNOWN, with no
message saying *why*.

We want the OAS treated as the authoritative payload contract: **if a requested
field is not in the OAS, flag it and report it.** The set of documented keys
*is* the schema, so this needs **no hand-maintained allowlist** — it is derived
from the OAS itself.

**Prerequisite — the embedded OAS must be current.** This only works if the
committed OAS extracts document the fields that appear in real, Mist-accepted
payloads. They are **stale**: the captured fixture shows real switches carrying
`bgp_config` (all 5) and a `port_config.*.ae_lacp_force_up` leaf that the
committed `device_switch`/`networktemplate` extracts omit — enforcing against a
stale extract would **false-flag those real fields**. So refreshing the embedded
extracts from the official source — `github.com/mistsys/mist_openapi` via
`tools/extract_oas.py` — is the **first task** (a hard prerequisite). Validation
stays offline (the extract is embedded); the refresh just makes the snapshot
trustworthy. `disabled` stays correctly flagged afterward (it is genuinely not a
switch field). If the official OAS itself omits a field that real payloads carry,
that is fixed at the source (the OAS owner maintains `mistsys/mist_openapi`), not
worked around in the twin.

This is deliberately a **separate gate** from the field-gate allowlist:

- **OAS validity** (this feature): is this a *real, documented* Mist attribute?
- **Modeled-surface safety** (existing field gate): does the twin actually
  *simulate the effect* of this attribute? (default-deny; out-of-scope → UNKNOWN)

These are different sets. An attribute can be perfectly OAS-valid yet unmodeled
(`port_config.*.speed`, `esilag`) — those must stay UNKNOWN, not become SAFE.
Merging the two gates (e.g. scoping by OAS membership) would convert a large
class of UNKNOWNs into false SAFEs and break the no-false-SAFE doctrine. This
feature **does not widen, repurpose, or use the field-gate allowlist as an OAS
suppressor**; it only adds the OAS validity gate. (The OAS/allowlist reconciliation
may *narrow* a genuinely-invalid modeled leaf — see §5 — which is a correctness
change, not a widening.)

## 2. Goals / non-goals

**Goals**
- **Refresh the embedded OAS extracts first** (from `mistsys/mist_openapi` via
  `tools/extract_oas.py`) so enforce-by-default never false-flags real fields the
  stale snapshot omits (`bgp_config`, `ae_lacp_force_up`). Hard prerequisite.
- Detect any key in the requested payload that is not documented in the (refreshed)
  OAS, at any nesting depth, and report it as a finding.
- Derive everything from the embedded OAS — no per-leaf allowlist, no allowlist
  suppression (pure enforce-by-default; the refresh, not a suppressor, is what
  prevents false positives).
- Apply uniformly to all three simulate paths (site / org-template / org-NAC)
  via the existing L0 seam.

**Non-goals**
- **No override / suppression in the twin.** Accept/dismiss of an unknown-
  attribute finding belongs to the elicitation UI, not the digital twin. The
  twin's job is detect-and-report only.
- No **widening** or repurposing of the field-gate allowlist; it keeps its
  default-deny role. The §1 reconciliation MAY **narrow** it — removing a leaf
  that is genuinely not a field for a type, which moves that leaf modeled→UNKNOWN
  — and such narrowing ships with its own `tests/scope/` field-gate test updates.
- No change to `decide()` (site / org-NAC) — existing precedence already
  produces the right decision (below). The org-template path needs a one-line
  `decide_org()` floor addition (see §3).
- Not deriving the modeled-leaf set from checks/ingesters (a separate, larger
  refactor; explicitly out of scope here).

## 3. Behavior & decision mapping

Each undocumented key becomes one non-fatal L0 **adapter / operational** finding:

- `code = "l0.schema.unknown_attribute"`
- `source = FindingSource.ADAPTER`
- `category = FindingCategory.OPERATIONAL`
- `severity = Severity.WARNING`
- `confidence = HIGH`
- `evidence = {"path": "<dotted path>", "object_type": "<type>"}`
- `message` names the undocumented field and object type.

**Severity rationale.** WARNING, not ERROR: an undocumented field is *uncertain*
(it may be OAS-snapshot drift — a real, newer Mist field we have not re-extracted
— rather than a definite type breach). It still floors to REVIEW.

**Decision mapping — site / org-NAC (no `decide()` change).** Via existing
precedence in `verdict/decision.py`:
- A WARNING finding lands in the REVIEW bucket → the verdict **floors to REVIEW**.
- If the same op *also* misses the field gate (the usual case for a genuinely
  undocumented changed leaf — see §8), that rejection makes the op **UNKNOWN**,
  and `UNKNOWN > REVIEW`, so UNKNOWN wins. That is correct and intended.
- Unknown-attribute findings never drive UNSAFE (operational, never NETWORK).

**Decision mapping — org-template (one-line `decide_org()` change required).**
On the org-template path, L0 findings do NOT go through `decide()`: they become
`template_findings` (`pipeline.py:534`) consumed by `decide_org()`, which today
floors only operational **ERROR/CRITICAL**, not WARNING (`org_verdict.py:61`). A
WARNING unknown-attribute template finding that does not also trip the field gate
(e.g. a pre-existing-unchanged undocumented field) would therefore roll up
**SAFE** — a false-SAFE. Fix: `decide_org()` must also floor a WARNING template
finding to REVIEW, matching `decide()`. This is a **latent asymmetry** (`decide()`
floors any WARNING; `decide_org()` did not) that this feature is the first to
expose; the change is **purely additive** — only ever more conservative, and no
WARNING template findings exist today, so zero regression.

**Severity stays WARNING** (not ERROR): an undocumented field is genuinely less
certain than a type/enum breach — possible OAS-snapshot drift — so WARNING is the
honest signal, and we make `decide_org()` consistent with `decide()` rather than
overstate certainty. (Promoting to ERROR would also close the hole without a
`decide_org()` change; we deliberately chose WARNING + the `decide_org()` floor.)

## 4. The walker

New module `adapters/mist/validate/unknown_keys.py`, single entry point:

```python
def unknown_attribute_findings(
    schema: Mapping[str, Any],      # the raw committed schema (caller loads it)
    payload: Mapping[str, Any],
    *,
    object_type: str,               # for the skip-set check + finding evidence
    scope_roots: Collection[str] | None,
) -> tuple[Finding, ...]
```

(The caller passes the loaded schema — keeps the walker pure/testable and avoids a
circular import with `schema.py`.)

It recursively compares payload keys against the schema's documented key-set per
node. Preprocessing matches existing L0 exactly:

- **`null == absent`**: strip None-valued keys deeply first (reuse the same
  canon as `_without_nulls`), so `disabled: null` is never flagged — only
  present, non-null undocumented keys are.
- **Scope**: only descend into top-level roots in `scope_roots` (the changed
  roots). `scope_roots = None` (the `--l0-full-object` mode) walks the whole
  object. This is the same scoping `validate_payload` already uses, so an unknown
  key on an *untouched* root is not reported in default mode (refinement: old
  fetched oddities on untouched roots must not pollute a plan).
- **Secrets**: suppress a finding whose dotted path has any segment matching the
  shared `STRIP_KEY_PARTS` (same source the redaction/`_touches_secret` use), so
  we never surface a secret-bearing key path.
- **Cap**: at most `_MAX_FINDINGS` (50, the shared constant) unknown-attribute
  findings — a typo-heavy payload must not flood the verdict.

### 4.1 Per-node semantics

At each object node, first resolve composition into a documented key-set:

- **`allOf`**: union of all branches' `properties` keys.
- **`anyOf` / `oneOf`**: **conservative union** of all branches' `properties`
  keys — a key documented in *any* branch is "known"; "unknown" means documented
  in **no** branch we know about. (This needs its own logic: `norm_schema` takes
  only the *first* variant and is unsuitable for the union.) When the **same**
  property key (or a tied map value-schema) appears in multiple branches, the
  sub-schemas are **composed** (`anyOf` for union branches, `allOf` for `allOf`
  branches) — never overwritten — so a *nested* key documented in any branch is
  also accepted on recursion.

Let `props` = the resolved documented properties (name → sub-schema, union) and
`addl` = the node's effective `additionalProperties`. Resolution across
composition differs by combinator:

- **`anyOf` / `oneOf`** — resolve `addl` **toward the most permissive** (if any
  branch is `true` → OPEN; else if any branch is a schema → MAP; else if all are
  `false`/absent → CLOSED). With the `props` *union*, a key is flagged only when
  **no** branch documents it **and no** branch would otherwise allow it — the
  conservative direction (avoid false positives).
- **`allOf`** — intersection semantics: merge `properties` across branches and
  apply the **restrictive** `additionalProperties` (a `false` in any branch
  closes the node). The committed schemas have no `allOf` branch that sets
  `additionalProperties`, so today `allOf` reduces to a properties-merge; this is
  only approximated, and **Task 1's refresh gate fails** if a refreshed schema
  introduces an `allOf` branch with its own `additionalProperties` (so the
  approximation cannot be silently invalidated — implement exact support or skip
  the type then).

Then classify the node:

| `additionalProperties` | `properties` present? | Behavior |
|---|---|---|
| `true` | any | **OPEN** — stop enforcing extra keys here; still recurse keys that match `props` into their sub-schemas |
| a **schema** (dict) | any | **MAP** — extra (non-`props`) keys are allowed; recurse them into the additionalProperties schema. Keys in `props` recurse into **their own** sub-schema (not the map schema) |
| absent | yes | **CLOSED-by-OAS** — flag any present key not in `props`; recurse known keys into their sub-schemas |
| `false` | yes | **CLOSED** — same as closed-by-OAS (no map recursion) |
| absent | no | **UNDOCUMENTED node** — nothing to compare against; do not flag, do not recurse (treat as open) |

**Crux (CLOSED-by-OAS).** Committed schemas do not set `additionalProperties:
false`, so "enforce against the OAS" means: *a documented object (`properties`
present, no explicit open `additionalProperties`) is treated as a closed set.*
This is the rule that makes the `disabled` case fire. It is also where residual
false-positives live if the OAS lags Mist — those surface as REVIEW findings the
elicitation UI dismisses (per the override decision: UI-side, not twin-side).

**Mixed nodes (tweak).** A node with **both** `properties` and a schema-valued
`additionalProperties` must use `properties` to match/recurse known keys and the
additional-properties schema only for the *remaining* dynamic keys. Do **not**
treat such a node as a pure map and skip property-specific recursion.

**Recursion.** Recurse into nested objects (per the table), into array `items`
(each element against the items-schema), and into map values. Each recursion
re-applies the same node logic. The walker enforces only where the schema
describes an object/array/map; it never asserts scalar types, enums, or
`required` — those remain the existing jsonschema pass's job (a value whose
shape disagrees with the schema is that pass's finding, not an unknown-attribute
finding).

### 4.2 Worked example (the motivating case)

`device` → `device_switch.schema.json`:
- root: `properties` present, `additionalProperties` absent → CLOSED-by-OAS;
  `port_config` is a documented root → recurse.
- `port_config` node: only `additionalProperties` = entry-schema, no
  `properties` → MAP → port-name keys (`ge-0/0/10`, …) allowed; recurse each
  value into the entry-schema.
- entry node: `properties` = the documented switch port_config fields,
  `additionalProperties` absent → CLOSED-by-OAS → `usage`/`dynamic_usage`/
  `critical`/`description`/`no_local_overwrite` are documented (pass);
  **`disabled` is flagged** (it is a gateway port_config field, not a switch one)
  → `l0.schema.unknown_attribute` @ `port_config.ge-0/0/10.disabled`. After the
  refresh this entry also documents the real switch leaf `ae_lacp_force_up` (so it
  is *not* flagged), while `disabled` stays flagged.

## 5. Completeness / thin schemas

Enforce on the schemas **not** in the skip-set; skip the deliberately-thin ones:

```python
# Schemas too thin to enforce unknown-attribute detection (they document only a
# handful of leaves on purpose). Completing a schema's OAS extract flips it on by
# removing it here. This is about OAS COMPLETENESS, not per-leaf modeling.
OAS_UNKNOWN_KEY_SKIP: frozenset[str] = frozenset({"wlan", "nacrule", "sitetemplate"})
```

A skipped object type produces zero unknown-attribute findings (its other L0
checks are unchanged). The skip-set is the **single scope lever**.

**Enforcement requires the OAS and the modeled allowlist to AGREE per type.**
For each enforced type, every modeled root must be documented where the twin reads
it. Today they do not fully agree — the comprehensive Task 1 gate reports modeled
roots the committed OAS does not document top-level:

| Enforced type | modeled roots missing top-level in the OAS |
|---|---|
| `device` | `bgp_config` only — the §1 refresh adds it (live OAS has it). Clean after refresh. |
| `networktemplate` | `bgp_config`, `dhcpd_config`, `ospf_config`, `stp_config`, `vars` |
| `site_setting` | `bgp_config`, `dhcpd_config`, `ospf_config`, `stp_config` |
| `gatewaytemplate` | `vars` |

These are **not** "fields that never appear." The twin reads `site_setting.dhcpd_config`
**top-level** (`site_effective.get("dhcpd_config")`; the allowlist comment marks DHCP
leaves *site_setting only*), while the OAS documents `dhcpd_config` **nested under
`gateway`**; and `networktemplate` has no `ospf_config` at all (only `ospf_areas`), yet
the twin models `ospf_config.enabled`. Each mismatch is resolved in Task 1, **per
root**, by the OAS owner: document it top-level in `mistsys/mist_openapi` (if the twin
legitimately models it there) **or** narrow the allowlist (if it is genuinely not a
field for that type). A type that cannot be reconciled now goes into
`OAS_UNKNOWN_KEY_SKIP` (deferred).

The table above is **root-level**. Because the walker recurses, the **leaf** level
matters too: the Task 1 root gate is a first pass, and the definitive check is the
map-aware leaf-coverage test (Task 3, `test_no_modeled_allowlist_leaf_is_flagged`),
which exercises **every** allowlisted leaf at its real nesting through the real
walker. It surfaces nested mismatches the root gate cannot — e.g. the `device`
`port_config` entry omits modeled `mode`/`networks`/`all_networks`/`allow_dhcpd`,
so **even `device`** needs each of those resolved (document in the OAS, or narrow
the allowlist) before enforcing. "Clean after refresh" therefore means *after the
refresh AND the per-leaf reconciliation that both gates enforce*; `device` is
simply the smallest such reconciliation and the minimum viable scope.

## 6. Integration & output

Fold the walker into `validate_payload`: after the existing jsonschema pass, call
`unknown_attribute_findings(raw_schema, payload, object_type=object_type,
scope_roots=scope_roots)` (the caller loads the raw schema; skip-listed thin
types short-circuit to `()`) and append its findings to the `L0Result.findings`
(non-fatal; `fatal` is unaffected).
Because `validate_payload` is the single L0 seam, all three paths
(`simulate`, `simulate_org_template`, `simulate_org_nac`) get this automatically,
respecting `scope_roots` and `--l0-full-object` uniformly.

Findings then flow through the existing machinery with no other wiring:
`adapter_findings` → `decide()` (REVIEW floor) → rendered in human + JSON output,
stamped with the op's subject, and included in decision / confidence summary /
overall severity — exactly like today's L0 findings. (Coverage is **not**
affected: `rollup()` derives coverage only from `check_results`, never from
`adapter_findings`.)

Ordering in `L0Result.findings`: existing schema violations first, then
unknown-attribute findings (each capped at `_MAX_FINDINGS`).

## 7. Honesty / safety invariants

- **Field-gate allowlist is not widened, repurposed, or used as an OAS
  suppressor** (the reconciliation may *narrow* a genuinely-invalid leaf, with
  scope-test updates). OAS membership does not widen modeled surface;
  documented-but-unmodeled leaves still → UNKNOWN.
- **No false-SAFE introduced.** This feature only ever *adds* findings (→ REVIEW)
  or is dominated by an existing UNKNOWN; it can never turn a REVIEW/UNKNOWN into
  SAFE.
- **No new fetch, no new scope.** Reuses the effective object and `_changed_roots`
  that L0 already computes.
- **Secrets never surfaced.** Same `STRIP_KEY_PARTS` suppression as the rest of
  the twin.

## 8. Relationship to the field gate (why both, when each fires)

The field gate flags **changed** raw leaves not in the modeled allowlist; the
OAS check flags **present** keys not in the OAS. They overlap but are not the same:

- **Operator introduces an undocumented field** (e.g. adds `disabled`): both fire
  → decision **UNKNOWN** (field-gate rejection dominates). The OAS finding adds
  the actionable *reason* ("`disabled` not in OAS") the field gate alone does not.
- **Undocumented field already present and unchanged** within a changed root
  (root replaced wholesale, same value both sides): field gate sees no change and
  stays silent; the OAS check still flags it → decision **REVIEW**. This is a real
  case the field gate cannot catch.
- **`--l0-full-object` audit mode**: the OAS check reports undocumented keys on
  any root, independent of what changed.

So the OAS check earns its place via (a) diagnostics on the introduce case and
(b) detection of pre-existing/audit undocumented fields the field gate misses.

## 9. Testing

**Unit (walker)** — `tests/adapters/mist/test_unknown_keys.py`:
- `disabled` on a `device` `port_config` entry → flagged at the right path
  (genuinely invalid on a switch).
- Map keys accepted: `port_config.<port>`, `networks.<name>`, `port_usages.<name>`
  port/network/usage names are not flagged.
- CLOSED-by-OAS: a bogus key on a documented object → flagged.
- `additionalProperties: true` node → extra key NOT flagged (open); a known
  sub-property under it still recurses.
- Mixed node (properties + schema additionalProperties) → known key matched
  against properties (recursed into its schema), extra key recursed as map (not
  flagged at that node).
- Composition union: a key valid only in a non-first `anyOf`/`oneOf` branch is
  accepted (not flagged).
- Composition with a map branch: a non-first `anyOf`/`oneOf` branch with
  schema-valued `additionalProperties` makes the node MAP → its dynamic keys are
  accepted, and a leaf inside the value is still checked (guards the OPEN > MAP >
  absent ranking).
- `null` undocumented key (`disabled: null`) → NOT flagged (null == absent).
- Secret-bearing path (a key under `…password…`/`…psk…`) → suppressed.
- Thin schema (`wlan`/`nacrule`/`sitetemplate`) → zero unknown-attribute findings.
- Cap: > 50 undocumented keys → exactly `_MAX_FINDINGS` findings.
- Undocumented node (`{"type": "object"}`, no properties + no `additionalProperties`)
  → no findings.
- Explicit `additionalProperties: false` with no properties → flags every key
  (distinct from absent `additionalProperties`).

**scope_roots** — an unknown key on an **untouched** effective root must NOT
report (default scoped mode); the same key on a **changed** root MUST report; in
`--l0-full-object` mode the untouched-root key DOES report.

**Decision floors (unit):**
- `decide()` — a lone WARNING `l0.schema.unknown_attribute` adapter finding (no
  rejections, no checks) → **REVIEW** (site / org-NAC floor).
- `decide_org()` — a lone WARNING template finding (zero or all-SAFE sites) →
  **REVIEW**, not SAFE (org-template floor; the regression guard for the fixed
  asymmetry).

**Integration (pipeline e2e)** — `tests/engine/`:
- The exact motivating `port_config` payload (with `disabled`) on a `device`:
  assert an `l0.schema.unknown_attribute` finding for `disabled` is **present**,
  and assert decision **UNKNOWN** (the field gate also rejects
  `port_config.*.disabled`; do **not** pin REVIEW for this case unless that path
  is allowlisted).
- The site/org-NAC REVIEW floor and the org-template REVIEW floor are proven by
  the two decision-floor unit tests above plus the `validate_payload` integration
  test (which proves the WARNING finding is produced and, via `pipeline.py:534`,
  reaches `template_findings`); a full org pipeline e2e is therefore redundant.

**Real-field regression (post-refresh)** — real fields on the enforced types must
produce **zero** unknown-attribute findings. Device: a switch payload carrying
`bgp_config` and `port_config.*.ae_lacp_force_up` is clean. Templates (per the
Task 1 reconciliation): `networktemplate` `ospf_config.enabled` + nested
`switch_matching.rules[].port_config.*.ae_lacp_force_up`, and `site_setting`
`dhcpd_config`, are clean; and the existing `networktemplate` L0 test is
strengthened to assert no unknown findings. These guard that the refresh +
reconciliation actually closed every enforced type's gaps.

**Leaf-level allowlist coverage (post-refresh, map-aware)** — the definitive gate:
for every enforced type, a payload exercising **every** allowlisted leaf at its
real nesting produces zero unknown-attribute findings
(`test_no_modeled_allowlist_leaf_is_flagged`, built on the real walker). This
proves no *modeled* leaf — not only roots — is false-flagged, and surfaces nested
mismatches (e.g. `device port_config.*.mode`).

**Golden regression** — a fully-documented payload adds **zero**
unknown-attribute findings (guards against making existing goldens noisy).

## 10. Files

- **prerequisite (refresh)** `src/digital_twin/adapters/mist/oas/*.schema.json`
  + `oas/VERSION` — re-extract from `mistsys/mist_openapi` via
  `tools/extract_oas.py`; must add `bgp_config` (device_switch, networktemplate)
  and `ae_lacp_force_up` (switch port_config entry). `tools/extract_oas.py` source
  reference already points to `mistsys/mist_openapi`.
- **new** `adapters/mist/validate/unknown_keys.py` — the walker + skip set.
- **edit** `adapters/mist/validate/schema.py` — call the walker in
  `validate_payload`, merge findings; share `_MAX_FINDINGS` / null-strip /
  `STRIP_KEY_PARTS`.
- **edit** `verdict/org_verdict.py` — `decide_org()` floors a WARNING template
  finding to REVIEW (matching `decide()`); update the module docstring.
- **new** `tests/adapters/mist/test_unknown_keys.py` (flat layout, matching the
  existing `tests/adapters/mist/test_validate_l0.py`).
- **edit** `tests/adapters/mist/test_validate_l0.py` — `validate_payload`
  integration cases.
- **edit** `tests/verdict/test_org_verdict.py` — `decide_org` WARNING-floor test.
- **edit** `tests/verdict/test_decision.py`, `tests/engine/test_pipeline.py` —
  the decision-floor + pipeline e2e cases.
- **edit** `docs/ROADMAP.md` — record the feature (new L0 OAS-validity gate)
  under §2 (new coverage).

## 11. Deferred / out of scope

- Override/accept of unknown-attribute findings → elicitation UI, not the twin.
- Completing the thin OAS schemas (`wlan`/`nacrule`/`sitetemplate`) to flip
  enforcement on for them.
- Deriving the modeled-leaf set from checks/ingesters (replaces the maintained
  field-gate allowlist file with code-declared leaves) — separate refactor.

## 12. As-built amendments (2026-06-25)

Decisions made during the build that SUPERSEDE the design above. Recorded here for
fidelity; the code is the source of truth.

1. **Closedness owned by the walker; jsonschema stays permissive (`_strip_closed`).**
   The refreshed OAS extracts are kept FAITHFUL (closed, `additionalProperties:false`
   intact). The jsonschema validator path strips that keyword (`_strip_closed`) so L0
   never ERRORs on the GET-only fields the EFFECTIVE object carries; the walker reads
   the faithful schema (`_raw_schema`) and is the SOLE "not in OAS" finder. (The
   original draft implied the extract itself would be permissive — closedness instead
   moved entirely into the walker.)

2. **Split scope — `unknown_scope_roots` (SUPERSEDES §4 scope + §8 "pre-existing"
   promise).** ChangePlan ops are full-object PUTs, so `_changed_roots` = *every*
   root; scoping the walker to that made it re-audit the whole persisted object and
   false-flag pre-existing fields the closed OAS omits — top-level GET-only roots AND
   nested gaps (e.g. `mist_nac.*`). `validate_payload` now takes a SEPARATE
   `unknown_scope_roots` = roots with actual value deltas (`changed_paths(current,
   effective)`); `scope_roots` still governs the jsonschema/L0 scope unchanged. The
   honest contract: **default mode flags undocumented attrs only on roots whose
   VALUES changed; `--l0-full-object` flags all present.** §8's "catch a pre-existing
   undocumented field in an untouched root" is DROPPED — under full-object PUTs that
   promise *was* the noise source. The motivating case (an attr introduced on a
   changed root, e.g. `port_config.*.disabled`) is still caught.

3. **Device-scoped server-managed skip (`_SERVER_MANAGED_ROOTS_BY_TYPE`).** Rather
   than expand the global `IGNORED_RAW_FIELDS`, the walker skips top-level roots per
   object type: `device` = `IGNORED_RAW_FIELDS | _DEVICE_GET_ONLY_ROOTS`
   (`optic_port_config`, `evpn_scope`, `radio_config`, `st_ip_base`,
   `uses_description_from_port_usage`, plus map-placement / inventory roots). These
   are real config on OTHER object types, so a global ignore would be a false-SAFE
   there; the skip is device-scoped and ROOT-level only (a nested `networks.foo.id`
   still surfaces). Global `IGNORED_RAW_FIELDS` (the field gate) stays minimal.

4. **OAS source + upstream debt.** Extracts refreshed from the officially-supported
   `mistsys/mist_openapi`. `switch_bgp_config_neighbor.disabled` is patched locally
   pending an upstream PR (recorded in `src/digital_twin/adapters/mist/oas/VERSION`).
