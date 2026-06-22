# GS34 — Org NAC rules: modeling, delta-reporting, and shadowing detection

**Status:** spec (pending user review)
**Date:** 2026-06-22
**Roadmap:** GS34 — *Security policy / NAC rule deltas (SEC-POLICY, SEC-NAC)*

## 1. Summary

Bring **org-level NAC rules** into scope as a first-class object type and deliver,
in one combined cycle:

1. **Foundation** — fetch + L0 schema + IR model + ingest for `nacrule` / `nactag`.
2. **GS34 delta-reporting** — honest add / remove / change / reorder reporting of NAC
   rules (REVIEW), *no impact modeling* (the roadmap's explicit "first step").
3. **NAC rule shadowing** — a single-state config-lint over the proposed rule set:
   an earlier *enabled* rule whose match **provably supersets** a later one makes the
   later rule unreachable.

NAC rules are **org objects** with no site / L2 dimension. They are simulated by a
**dedicated `simulate_org_nac(plan)` path** — not the wired site pipeline (which would
invent site semantics) and not the per-site/template fan-out (`simulate_org_template`,
whose `OrgVerdict` is per-site shaped).

## 2. Background — the Mist NAC data model

Validated against the live TM-LAB org (13 rules, 38 tags).

### nacrule (org object, evaluated in `order`, first match wins)

```json
{
  "id": "2f85ceae-…", "name": "Wi-Fi TLS - Corporate",
  "order": 5, "enabled": true, "action": "allow",      // action ∈ {allow, block}
  "matching": {
    "auth_type": "cert",                                 // string in the wild
    "port_types": ["wireless"],
    "nactags": ["4e6e252f-…", "2b4f96ae-…"],            // criteria tag ids
    "site_ids": [], "sitegroup_ids": [],
    "family": [], "mfg": [], "model": [], "os_type": [], "vendor": []
  },
  "not_matching": { … same shape … },                    // negative criteria
  "apply_tags": ["2080bce5-…"]                           // OUTCOME tag ids (allow path)
}
```

The full `nac_rule_matching` surface (per Juniper's OpenAPI) is: `auth_type`,
`port_types`, `nactags`, `site_ids`, `sitegroup_ids`, `family`, `mfg`, `model`,
`os_type`, `vendor`. **We model all of them** so the diff is complete; shadowing only
*reasons* about a provable subset (§6).

### nactag (org object, building block)

```json
{ "id": "…", "name": "mac.pc", "type": "match",
  "match": "client_mac", "values": ["aabb…"], "match_all": false }
```

`type` ∈ {`match`, `radius_group`, `vlan`, …}. For a `match` tag, `match` names the
field (`client_mac`, `mdm_status`, …) and `values` are the literals.

### Match semantics (confirmed vs. unconfirmed)

- **Confirmed** (Juniper API docs / OpenAPI): multiple `values` *inside one* match tag
  are **OR by default**, **AND** only when `match_all=true`; values support
  prefix/suffix/substring/negation syntax.
- **Unconfirmed** from public docs: whether multiple `matching.nactags` on a rule are
  always AND, or OR-grouped by `match` type.

Consequence: comparing arbitrary tag *predicates* is **not v1** — and even comparing
*different* tag-id sets is unsafe while cross-tag AND/OR is unconfirmed (if tags
OR-group, `{X}` does **not** cover `{X,Y}`). v1 therefore proves tag coverage in only
two cases: the earlier rule has **no tag constraint at all**, or the two rules have an
**identical tag-id set**. Tag internals are never read. This is conservative by
construction (we may miss real shadows; we never invent false ones), matching the twin's
no-false-positive doctrine.

## 3. Architecture

```
plan (ops: object_type="nacrule", no site_id)
  │  driver: _is_org_nac_plan(plan)  → routes BEFORE _is_org_plan / site fallback
  ▼
simulate_org_nac(plan, provider)            [engine/pipeline.py]
  ├─ provider.resolve_org_nac(scope) ……… baseline nacrules + nactags (errors-as-values)
  ├─ per op (mirrors the site pipeline):
  │   existence: update/delete id∉base, or create id∈base → Rejection → UNKNOWN
  │   • delete → drop id from proposed set; SKIP conflict/L0/field-gate (no proposed obj)
  │   • create | update:
  │       update_conflicts(op.payload)   (sets x AND sends '-x' → Rejection → UNKNOWN)
  │       current = baseline[id] (update) | {} + overlay id=object_id (create)
  │       effective = effective_update(current, op.payload)      # create ⇒ body (+id)
  │       adapter_findings += validate_payload("nacrule", effective,
  │              scope_roots = None (create, full obj) | _changed_roots(op.payload) (update))
  │       screen_op("nacrule", current, effective)  → FIELD GATE: any changed leaf outside
  │              RAW_ALLOWLIST["nacrule"] → Rejection → UNKNOWN (enforces leaf-tightening)
  │       proposed set ← effective
  ├─ ingest baseline & proposed → IR        (NacRule + NacTag entities)
  ├─ diff_ir(base_ir, proposed_ir)          (nacrule kind: add/remove/modify)
  ├─ checks → CheckResult:  nac.rule.change (GS34 delta) ; nac.rule.shadowed (delta-attr)
  └─ decide(DecisionInputs(rejections, l0_fatal, baseline_unavailable,
            check_results, adapter_findings))  → OrgNacVerdict
```

Short-circuits to **UNKNOWN** (honest, never a guess) when: the `nacrule` L0 schema is
missing/fatal, the payload is not an object, or the **`nacrules`** fetch fails. A
**`nactags`** fetch failure is *not* fatal — it is labels-only (see Provider, §4) and
yields REVIEW with id-only labels, not UNKNOWN.

## 4. Components

| Area | File | Change |
|---|---|---|
| Provider | `providers/*` | add `resolve_org_nac(scope) -> NacFetch \| FetchError` to the protocol + Mist impl (`listOrgNacRules`, `listOrgNacTags`); errors-as-values, mirrors `resolve_org_template`. **`NacFetch = {rules: tuple[raw,...], tags: tuple[raw,...], tag_findings: tuple[Finding,...]}`.** **Asymmetric failure:** a `nacrules` fetch failure returns `FetchError` → `baseline_unavailable` → UNKNOWN (load-bearing). A `nactags` failure is **labels-only** — shadowing keys on tag *ids* carried by the rules — so it returns the rules with `tags=()` plus a `Finding` in `tag_findings` (→ `adapter_findings`) — pinned `source=ADAPTER`, `category=OPERATIONAL`, `severity=WARNING`, HIGH confidence, so `decide()` floors it to **REVIEW** (id-only labels), **not** UNKNOWN. |
| L0 / OAS | `adapters/mist/oas/nacrule.schema.json` (new) + `validate/schema.py` | extract `nac_rule` from the Mist OpenAPI; register in `_SCHEMA_FILES["nacrule"]`. **Prerequisite** (see §9). |
| IR | `ir/entities.py` | new `NacRule`, `NacTag` frozen dataclasses; `IRBuilder.add_nacrule/add_nactag`; `IR.nacrules` / `IR.nactags` accessors. |
| Ingest | `adapters/mist/ingest/nac.py` (new) | nacrules + nactags → IR. **nacrule rows are load-bearing — the generic "skip bad row" pattern must NOT apply**: a row WITH a stable `id` that hits a parse problem is **minted with `opaque_digest` set** (a digest of its raw row) + best-effort fields (kept in the set, keyed by id) so the diff still sees it and shadowing skips it; only a row **without a usable id** is dropped (nothing to key against). Both emit a **pinned `Finding`** (`source=ADAPTER`, `category=OPERATIONAL`,
`severity=WARNING`, HIGH confidence) routed to the verdict's `adapter_findings` — WARNING
makes `decide()` floor **REVIEW** (an operational INFO would silently NOT floor). A genuinely-absent match field stays ∅ (real "any"); a present-but-unparseable proof-bearing field (e.g. `auth_type` not a string, `nactags` not a list) sets `opaque_digest` — never collapsed to ∅. **Absent `enabled` ⇒ `True`** (OAS default — so a created broad rule that omits it still participates/shadows); present non-bool `enabled` ⇒ `opaque_digest` set. The whole `not_matching` block is normalized to `(dimension, value)` pairs. Unparseable `order` → None. `nactag` rows are labels-only and may still be skipped. |
| Simulate | `engine/pipeline.py` | `simulate_org_nac(plan, provider) -> OrgNacVerdict`. |
| Scope | `scope/allowlist.py` | new `NAC_OBJECT_TYPES = ("nacrule",)` — **separate** from the site whitelist `SUPPORTED_OBJECT_TYPES` (its branch requires a `site_id`) and from `ORG_OBJECT_TYPES` (which drives the per-site fan-out routing). Plus `RAW_ALLOWLIST["nacrule"]` with **exact enumerated leaves** (see *nacrule allowlist leaves* below — no `matching.*` subtree, per the leaf-tightening rule). |
| Gate | `scope/object_gate.py` | new NAC branch `is_nac = bool(ops) and all(op.object_type in NAC_OBJECT_TYPES) and not scope.site_id`, evaluated **before** `is_org` and the site branch. Allowed actions: `create` \| `update` \| `delete` (delete payload must be empty). |
| Routing | `drivers/cli.py`, `drivers/mcp_server.py` | `_is_org_nac_plan(plan)` (mirrors `is_nac`) routes to `simulate_org_nac` **before** `_is_org_plan` and the site fallback, so a no-`site_id` nacrule plan is no longer rejected by the site branch. |
| Diff | `ir/diff.py` | register `nacrule` entity kind (add/remove/modify). Compares all CONFIG fields incl. `name`, `order`, and `opaque_digest` (the latter is **diff-bearing** so a change in *unparseable* content still shows — two different malformed values give different digests and cannot collapse-and-vanish). Only `meta` is ignored (already global); no per-kind ignore. |
| Checks | `checks/nac/delta.py`, `checks/nac/shadowing.py` (new) | the two checks below. |
| Verdict | `verdict/org_nac_verdict.py` (new) | lean `OrgNacVerdict`. |
| Drivers | `drivers/cli.py`, `drivers/mcp_server.py`, `drivers/render.py` | route + render. |

### IR entities

```python
@dataclass(frozen=True)
class NacRule:
    id: str
    name: str | None
    order: int | None            # None = unparseable/absent → never ordered/proven
    enabled: bool                # ABSENT ⇒ True (OAS default); non-bool ⇒ opaque_digest set
    action: str | None           # "allow" | "block" | None
    auth_types: frozenset[str]   # ∅ = GENUINELY unconstrained (matches any)
    port_types: frozenset[str]   # ∅ = genuinely unconstrained
    match_tags: frozenset[str]   # matching.nactags ids
    # remaining POSITIVE match dims — modeled for the diff; their non-emptiness makes a
    # rule non-provable for shadowing (it neither shadows nor is shadowed):
    site_ids: frozenset[str]; sitegroup_ids: frozenset[str]
    family: frozenset[str]; mfg: frozenset[str]; model: frozenset[str]
    os_type: frozenset[str]; vendor: frozenset[str]
    # the ENTIRE not_matching block normalized to (dimension, value) pairs — ONE field so
    # (a) the diff sees any negative-criteria change, and (b) `not not_matching` is the
    # whole non-emptiness test (no per-dim enumeration to forget). Any non-empty
    # not_matching ⇒ non-provable. Unparseable not_matching ⇒ a parse problem (below).
    not_matching: frozenset[tuple[str, str]]
    apply_tags: frozenset[str]
    # `None` = the row parsed cleanly. A non-None value is a stable **digest of the raw row**
    # (canonical JSON minus IGNORED_RAW_FIELDS), set when a proof field is unparseable or the
    # row only partially parsed (ingest mints id'd rows like this, never drops them). ONE
    # field, two roles: (1) `opaque_digest is None` IS the provability gate — non-None
    # excludes the rule from shadowing BOTH ways (its parsed proof fields aren't trustworthy;
    # a malformed value must never collapse to ∅ and become a catch-all shadower); (2) it is
    # **diff-bearing**, so two *different* malformed values yield different digests and a
    # change in unparseable content still surfaces in `diff_ir` — it cannot silently collapse
    # and vanish → false SAFE. (Replaces an earlier bool, which had no such fingerprint.)
    opaque_digest: str | None
    meta: FactMeta

@dataclass(frozen=True)
class NacTag:
    id: str; name: str | None; type: str | None
    match: str | None            # the match field, for `type=="match"`
    values: frozenset[str]
    match_all: bool
    meta: FactMeta
```

`NacTag.match/values/match_all` are carried for labels and future predicate work; v1
shadowing never reads them.

### NAC actions & apply model

Today no object type supports `create` (site = `update` only; org = `update`/`delete`).
GS34's "additions" — and the highest-risk shadowing case, *adding a broad rule that
buries existing ones* — require it, so NAC is the first type with a `create` action.

| Action | Op shape | Baseline → proposed |
|---|---|---|
| `update` | `object_id` = existing rule id; payload = changed leaves (incl. `order` for a reorder) | `effective_update(baseline[id], payload)` replaces that rule |
| `delete` | `object_id` = existing rule id; **empty** payload | drop `id` from the set |
| `create` | `object_id` = caller-supplied **provisional** id; payload = full rule body (may omit `id`) | add the effective body with **`id=op.object_id` overlaid** |

The **gate is pre-fetch** (no state), so it only checks action ∈ {create,update,delete},
object_type, no `site_id`, and empty delete payload. **Existence is validated post-fetch**
in `simulate_org_nac`: `update`/`delete` whose id ∉ baseline, or `create` whose id ∈
baseline, become a `Rejection` → UNKNOWN. A `delete` then **drops the row directly** and
runs **neither** `update_conflicts`, L0, nor the field gate (there is no proposed object —
exactly the org-template delete branch). Only `create`/`update` run
`update_conflicts(op.payload)` (a payload that both sets `x` and sends the `-x` delete
marker → `Rejection` → UNKNOWN) → effective merge → L0 → field gate.

**L0 validates the EFFECTIVE post-merge object, never the partial body** (matching the site
pipeline). This matters because the validator surfaces object-level `required` errors even
under `scope_roots`, and `nac_rule` requires `action`/`name` — validating a partial update
body directly would emit bogus "required" findings. So: `update` validates
`effective_update(baseline[id], payload)` with `scope_roots=_changed_roots(op.payload)`
(required fields inherited from baseline); `create` overlays `id=op.object_id` onto the
full body and validates it whole (`scope_roots=None`). The overlaid `id` also satisfies the
IR's required `NacRule.id` so the ingester keys the new row correctly (Mist assigns the
real id at apply; the provisional id is labelled as such and is needed only to key the rule
for diff + shadowing). Confirm `parse_change_plan` accepts `action="create"` (it parses the
action as a free string; the gate is the allowlist) as an early task.

### nacrule allowlist leaves

`RAW_ALLOWLIST["nacrule"]` — exact leaves only (`id`/`org_id`/`created_time`/
`modified_time` are already dropped by `IGNORED_RAW_FIELDS`). List values are atomic
leaves (the path flattener treats lists atomically, as with `ap_ids`):

```
name, order, enabled, action, apply_tags,
matching.auth_type, matching.port_types, matching.nactags,
matching.site_ids, matching.sitegroup_ids, matching.family,
matching.mfg, matching.model, matching.os_type, matching.vendor,
not_matching.auth_type, not_matching.port_types, not_matching.nactags,
not_matching.site_ids, not_matching.sitegroup_ids, not_matching.family,
not_matching.mfg, not_matching.model, not_matching.os_type, not_matching.vendor
```

Enforced by the **existing `screen_op` field gate** (`scope/field_gate.py`), which
`simulate_org_nac` must call post-L0 / pre-apply exactly as the site pipeline does
(`pipeline.py`). `screen_op` needs **no** NAC-specific branch — it diffs `current` vs the
effective object and rejects any changed leaf not in `RAW_ALLOWLIST["nacrule"]`. So an
OAS-valid but **unmodeled** field (e.g. `guest_auth_state`) passes L0 yet trips the field
gate → UNKNOWN. Without this call the field would be silently ignored by ingest → empty
diff → false SAFE; with it, anything not enumerated above is a deliberate UNKNOWN.

## 5. GS34 delta-reporting (`nac.rule.change`)

Reads the `nacrule` rows of `diff_ir`. For each **added / removed / modified** rule
(modify includes `name`, `order`, `enabled`, `action`, any `matching` / `not_matching` /
`apply_tags` field, and `opaque_digest` — a change in otherwise-unparseable content;
`name` is a real edit and is reported, *not* diff-ignored; see the diff note below):

- emit one `Finding`: `source=CHECK`, `category=NETWORK`, `severity=WARNING`,
  `subject = ObjectRef("nacrule", id, name)`. `caused_by` is `tuple[Cause, ...]` (not raw
  strings): for a modified rule
  `caused_by=(Cause(ref=ObjectRef("nacrule", id, name), fields=changed_fields),)`; for an
  add/remove `fields=()`. Message states *what* changed and that **access impact is not
  modeled** (REVIEW).
- No allow/block/role reasoning — that is deferred impact modeling, explicitly out of
  scope for GS34's first step.

WARNING ⇒ the run floors to **REVIEW**.

## 6. NAC rule shadowing (`nac.rule.shadowed`)

Single-state lint over a rule set, run on **baseline IR** and **proposed IR** for delta
attribution.

**Provable rule** (eligible for proof) — one centralized predicate so no dimension can be
forgotten:

```python
def is_provable(r: NacRule) -> bool:
    return (r.opaque_digest is None and r.order is not None  # cleanly parsed + ordered
            and not r.not_matching                      # ANY negative criterion ⇒ no
            and not (r.site_ids or r.sitegroup_ids or r.family or r.mfg
                     or r.model or r.os_type or r.vendor))  # any positive extra-dim ⇒ no
    # ⇒ the rule constrains ONLY on {auth_types, port_types, match_tags}.
```

A rule that fails this — `opaque_digest` set (unparseable proof field / partial row), order-less, or
carrying *any* negative (`not_matching`) or positive extra-dimension criterion — is
excluded from shadowing in **both** directions (it neither shadows nor is proven-shadowed;
no finding). This is the single chokepoint that prevents a malformed or
incompletely-modeled rule from manufacturing a false shadow.

**Coverage** — for two enabled, provable rules A (earlier `order`) and B (later):

```python
def covers_choice(a: frozenset, b: frozenset) -> bool:   # auth_types / port_types
    if not a: return True        # A unconstrained = matches any value ⊇ B
    if not b: return False        # B matches any, A does not
    return b <= a                 # A accepts every value B accepts

def covers_tags(a: frozenset, b: frozenset) -> bool:      # match_tags — CONSERVATIVE
    return (not a) or (a == b)    # A has no tag filter, OR identical tag set.
    # NOT `a <= b`: that assumes tags AND. Cross-tag AND/OR is UNCONFIRMED (§2),
    # so a strict-subset would false-positive if tags OR-group. Revisit only once
    # Mist's cross-nactag semantics are confirmed (§10).

A_covers_B = (covers_choice(A.auth_types, B.auth_types)
              and covers_choice(A.port_types, B.port_types)
              and covers_tags(A.match_tags, B.match_tags))
```

`auth_types`/`port_types` are **choice** dimensions (∅ = "any"; larger set = broader),
where the unconstrained/superset direction is sound regardless of tag semantics.
`match_tags` is the **unconfirmed** dimension, so coverage is allowed only when A has no
tag filter or the sets are identical (above). The directions differ per dimension — this
is the crux and is unit-tested exhaustively. Exact-duplicate matches are the `A == B`
special case and are caught for free.

For each enabled provable B, the **first** earlier enabled provable A with `A_covers_B`
is the shadower. Output: shadowed rule B, shadower A.

**Delta attribution** — the baseline status is a **tristate**, because with `opaque_digest`
a baseline "no shadow" can mean *proven absent* OR *couldn't be evaluated*. Conflating them
would let a baseline-unprovable rule masquerade as a newly-introduced shadow.

| Shadowed in proposed? | Baseline status of the A→B pair | Outcome |
|---|---|---|
| yes | **proven absent** — A & B both provable in baseline (or newly created), A did *not* cover B | **introduced** → `severity=WARNING` → REVIEW |
| yes | **proven present** — the same A→B pair shadowed in baseline | **pre-existing** → `severity=INFO` (context, no floor) |
| yes | **indeterminate** — A or B existed but was *unprovable* in baseline (`opaque_digest` / orderless / unmodeled criteria) | **suppress** the network shadow finding (cannot prove it is new) |
| no  | — | nothing |

The *suppress* row is safe: whenever the baseline pair is indeterminate, the rule that
became provable necessarily changed a provability-affecting field (so a `nac.rule.change`
delta fires) or carries a parse issue (so an operational finding fires) — REVIEW is reached
without a false "introduced" claim.

Finding: `source=CHECK`, `category=NETWORK`, `subject=ObjectRef("nacrule", B.id, B.name)`.
The shadower **A goes in `evidence["shadower"]`** (A's ref + both rules' `action` + the
covering dimensions) — **not** in `caused_by`, because A may be an *unchanged* baseline
catch-all and the `Cause` contract is reserved for entities the plan actually changed.
`caused_by` is built from the rule(s) in **this plan's diff** that introduced the shadow:
at least one of {A, B} has a non-empty diff row, since a newly-introduced shadow requires a
change to A's or B's **order, `enabled` state, or coverage/provability fields**
(auth/port/match_tags, or any field that flips provability). Examples: a reorder buries B
under a broader A; an earlier A flips `enabled` `false→true` and now shadows B; B flips
`enabled` `true` under an existing covering A. `caused_by` = those changed rows.

**Severity is uniform WARNING for v1.** A shadowed `block` rule is a latent security gap
and a shadowed `allow` rule is dead config; distinguishing them (escalating block-shadows
toward UNSAFE) needs action-interplay reasoning and is a **roadmap follow-up** (§10).

## 7. Verdict & decision

`OrgNacVerdict` (lean; no `per_site`/`driving_sites`). It carries the **decision inputs**
verbatim so nothing that reaches the decision can be dropped from the record — in
particular the non-fatal L0 `adapter_findings`, which are `Finding`s (not `Rejection`s):

```python
@dataclass(frozen=True)
class NacDelta:
    rule_id: str
    name: str | None
    kind: str                    # "added" | "removed" | "modified"
    changed_fields: tuple[str, ...]   # () for added/removed

@dataclass(frozen=True)
class OrgNacVerdict:
    decision: Decision
    decision_reasons: tuple[str, ...]
    changes: tuple[NacDelta, ...]            # the nacrule rows of the diff
    check_results: tuple[CheckResult, ...]   # nac.rule.change + nac.rule.shadowed
    adapter_findings: tuple[Finding, ...]    # NON-fatal L0 + ingest parse-issue findings
    rejections: tuple[Rejection, ...]        # short-circuit causes → UNKNOWN
```

**Reuse the existing `decide(DecisionInputs)`** — it is fully generic (no site
assumptions) and already routes *both* `adapter_findings` and `check_results` through the
proven floor logic. No bespoke `decide_nac`:

```python
decision, reasons = decide(DecisionInputs(
    rejections=rejections, l0_fatal=l0_fatal, baseline_unavailable=fetch_failed,
    check_results=check_results, adapter_findings=adapter_findings))
```

This guarantees the Critical case is handled structurally: a **non-fatal** malformed
nacrule payload emits an L0 `OPERATIONAL`/`ERROR` `Finding` → `decide` floors **REVIEW**
(operational ERROR/CRITICAL), never silently SAFE; a **fatal** L0 → `l0_fatal` → UNKNOWN.
The two NAC checks return `CheckResult`s like every other check (WARNING → REVIEW,
INFO → context). `render.py` flattens `check_results[].findings` + `adapter_findings`
(dict + human) like the other verdicts.

### Finding catalogue (single source of truth)

Every `Finding`/outcome the NAC path can produce, with the pinned shape that makes
`decide()` reach the stated decision. Implementers and tests reference THIS table — a
finding floors REVIEW only at `severity=WARNING` (any category) or operational
`ERROR`/`CRITICAL`, so the severities below are load-bearing, not cosmetic.

| Source | code / cause | source | category | severity | confidence | → decision |
|---|---|---|---|---|---|---|
| check | `nac.rule.change` (delta) | CHECK | NETWORK | WARNING | HIGH | REVIEW |
| check | `nac.rule.shadowed` — introduced | CHECK | NETWORK | WARNING | HIGH | REVIEW |
| check | `nac.rule.shadowed` — pre-existing | CHECK | NETWORK | INFO | HIGH | context (no floor) |
| adapter | L0 schema violation (non-fatal) | ADAPTER | OPERATIONAL | ERROR | HIGH | REVIEW |
| adapter | ingest parse-issue (opaque mint / dropped row) | ADAPTER | OPERATIONAL | WARNING | HIGH | REVIEW |
| adapter | `nactags` fetch failure (`tag_findings`) | ADAPTER | OPERATIONAL | WARNING | HIGH | REVIEW |
| — | `nacrules` fetch failure → `baseline_unavailable` | (not a finding) | — | — | — | UNKNOWN |
| — | fatal L0 / gate / conflict / bad-id → `rejection` or `l0_fatal` | (not a finding) | — | — | — | UNKNOWN |

v1 emits **no** NETWORK `ERROR`/`CRITICAL`, so **UNSAFE never occurs** — the decision range
is {SAFE, REVIEW, UNKNOWN}.

## 8. Testing

TDD throughout. Layers:

- **Shadowing algorithm** (`tests/checks/nac/test_shadowing.py`) — the core. Tables over:
  catch-all shadows all; equal auth/port/tags = duplicate shadow; auth/port coverage in
  the **choice** direction (∅=any, `b ⊆ a`); **match_tags conservatism** — A with no tag
  filter or an identical set shadows, but a strict-subset tag set (`{X}` vs `{X,Y}`) must
  **not** (guards the unconfirmed OR semantics); disabled A or B ⇒ no shadow;
  non-provable rule excluded both ways for *each* reason — unmodeled dim
  (`site_ids`/`family`/`not_matching`), **`opaque_digest` set (unparseable proof field)**,
  and `order is None`; a genuinely-empty (∅) field still counts as a real "any" (catch-all);
  **tristate attribution** — introduced (baseline proven-absent), pre-existing (baseline
  proven-present), and **suppressed** when baseline is indeterminate (A or B
  `opaque_digest`/orderless in baseline → no "introduced" finding; a baseline-only parse
  failure must not manufacture a newly-introduced shadow); **`enabled` as a cause** — an
  earlier A flipping `enabled` `false→true` (and B flipping `enabled` under an existing
  covering A) is an introduced shadow with the flipped rule in `caused_by`.
- **Delta-report** (`test_delta.py`) — add/remove/modify/reorder each yield one WARNING
  REVIEW finding naming the rule + changed fields; `caused_by` is a `Cause` (ref + fields,
  `fields=()` for add/remove); a **`not_matching.*` change** and a **name-only change**
  each emit `nac.rule.change` (not merely appear in `diff_ir`); a change to a rule's
  unparseable content (`opaque_digest` changes) also emits `nac.rule.change`.
- **IR/ingest** (`test_nac_ingest.py`) — full matching surface mapped; a row with an id
  but a malformed proof field → minted with `opaque_digest` set + a **WARNING** operational
  finding (per the catalogue; **not** dropped); a row with **no id** → dropped + the same
  finding; **a rule
  malformed in BOTH baseline and proposed still appears in `diff_ir`** — and **two
  *different* malformed proof values give different `opaque_digest` → a modify the diff
  catches** (regression: neither skipping nor digest-collapse may vanish a real change →
  false SAFE); identical malformed content → same digest → no spurious diff; a
  baseline-only parse failure does not manufacture a "newly introduced" shadow; **a
  `not_matching.*` change appears in `diff_ir`** (regression — gated + claimed-modeled,
  must not vanish); **absent `enabled` → `True`** and a rule with a non-tag negative
  criterion is **non-provable** (no false shadow); non-bool `enabled` → `opaque_digest`
  set + finding; unparseable `order` → None; `nactag` bad row skipped.
- **L0** — `validate_payload("nacrule", …)` registered, type violation caught, unknown
  type still fails closed.
- **Pipeline** (`test_simulate_org_nac.py`) — `nacrules` fetch error
  (`baseline_unavailable`) and bad-id rejections (create whose id ∈ baseline; update/delete
  whose id ∉ baseline) → UNKNOWN; **a `nactags` fetch failure → REVIEW with id-only labels
  + a `tag_findings` entry asserted `source=ADAPTER`/`category=OPERATIONAL`/`severity=WARNING`,
  NOT UNKNOWN** (shadowing still runs on tag ids); a **no-op** plan (empty effective diff) → SAFE; a reorder with **no new
  shadow** → REVIEW (delta finding only — every reorder is a GS34 delta); a reorder or
  `create` that **buries a rule** → REVIEW (delta + introduced-shadow findings);
  **non-fatal L0** violation in a payload → REVIEW with the L0 finding present in
  `adapter_findings` (regression for the dropped-adapter-findings bug — never SAFE);
  **fatal** L0 → UNKNOWN. create/update/delete each exercised, and an `update`'s partial
  body does **not** raise a bogus `required` L0 finding (effective-object validation). An
  **OAS-valid but unallowlisted field** (e.g. `guest_auth_state`) → field-gate Rejection →
  **UNKNOWN** (regression: without `screen_op` it would be ignored → empty diff → false
  SAFE). A payload that both sets and `-`-deletes the same attribute
  (`{"matching": …, "-matching": ""}`) → `update_conflicts` Rejection → **UNKNOWN**; a
  `delete` op runs **no** L0 / field gate (drops the row), only its existence check.
- **Routing** — `_is_org_nac_plan` true for nacrule/no-site plans, false otherwise; a
  no-`site_id` nacrule plan no longer falls through to rejection.
- **Golden / real-data validation** — replay the **13 TM-LAB rules** through shadowing and
  assert the result matches a hand-checked expectation (this is the empirical check on the
  conservative semantics; redact ids per the replay redaction manifest).

## 9. Prerequisite — OAS snapshot

The committed OAS carries `site_setting`, `device`, `networktemplate`, `gatewaytemplate`,
`sitetemplate`, and `wlan` — but **not** `nacrule`, so today a nacrule plan
L0-fails-closed → UNKNOWN. Implementation must add `nac_rule` (and the
`nac_rule_matching` component) to the `extract_oas` WANTED set, re-extract from the Mist
OpenAPI, commit `oas/nacrule.schema.json`, and bump the OAS VERSION. Done before the L0
registration task so `nacrule` validates instead of failing closed.

## 10. Out of scope / roadmap follow-ups

- **NAC impact modeling** (which clients/roles/VLANs an allow/block actually yields) — GS34
  is reporting-only by design.
- **Strict-subset & predicate tag coverage** — v1 proves tag coverage only when the
  earlier rule has *no* tag filter or an *identical* tag-id set. Strict-subset coverage
  (`{X}` covers `{X,Y}`, valid only if tags AND) and predicate subsumption across
  different tag ids (`match`/`values`/`match_all`, prefix/substring/negation) are both
  deferred until Mist's cross-nactag AND-vs-OR semantics are confirmed.
- **Block-shadow security escalation** (shadowed `block` ⇒ UNSAFE) — needs action-interplay
  reasoning.
- **`not_matching` / site / device-attribute coverage in proofs** — currently makes a rule
  non-provable (excluded from shadowing); could be modeled later.

## 11. Coordination

The other agent's org-delete / gateway-template / sitetemplate work touches the same
`allowlist` / `object_gate` / org-routing surface. This branch is based on latest
`origin/main` and isolated in a worktree; expect light merge coordination on
`allowlist.py` and the driver routing.
