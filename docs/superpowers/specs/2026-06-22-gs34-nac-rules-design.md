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

Consequence: comparing arbitrary tag *predicates* is **not v1**. The only safe atom is
**tag identity** — a rule referencing tag `X` requires exactly what another rule
referencing `X` requires. Shadowing therefore compares **sets of tag ids**, never tag
internals. This is conservative by construction (we may miss some real shadows; we will
not invent false ones), which matches the twin's no-false-positive doctrine.

## 3. Architecture

```
plan (ops: object_type="nacrule", no site_id)
  │  driver: _is_org_nac_plan(plan)  → routes BEFORE _is_org_plan / site fallback
  ▼
simulate_org_nac(plan, provider)            [engine/pipeline.py]
  ├─ provider.resolve_org_nac(scope) ……… baseline nacrules + nactags (errors-as-values)
  ├─ L0: validate_payload("nacrule", body, scope_roots=changed)  per edited rule
  ├─ apply ops → proposed nacrule set       (effective_update per rule, by id)
  ├─ ingest baseline & proposed → IR        (NacRule + NacTag entities)
  ├─ diff_ir(base_ir, proposed_ir)          (nacrule kind: add/remove/modify)
  ├─ checks:  nac.rule.change   (GS34 delta-report)
  │           nac.rule.shadowed (shadowing, delta-attributed)
  └─ assemble → OrgNacVerdict                (decision via decide_nac)
```

Short-circuits to **UNKNOWN** (honest, never a guess) when: the `nacrule` L0 schema is
missing/fatal, the payload is not an object, or the org fetch fails.

## 4. Components

| Area | File | Change |
|---|---|---|
| Provider | `providers/*` | add `resolve_org_nac(scope) -> NacFetch` to the protocol + Mist impl (`listOrgNacRules`, `listOrgNacTags`); errors-as-values, mirrors `resolve_org_template`. |
| L0 / OAS | `adapters/mist/oas/nacrule.schema.json` (new) + `validate/schema.py` | extract `nac_rule` from the Mist OpenAPI; register in `_SCHEMA_FILES["nacrule"]`. **Prerequisite** (see §9). |
| IR | `ir/entities.py` | new `NacRule`, `NacTag` frozen dataclasses; `IRBuilder.add_nacrule/add_nactag`; `IR.nacrules` / `IR.nactags` accessors. |
| Ingest | `adapters/mist/ingest/nac.py` (new) | nacrules + nactags → IR; per-row try/except (one bad row never drops the batch); unparseable values → conservative empties. |
| Simulate | `engine/pipeline.py` | `simulate_org_nac(plan, provider) -> OrgNacVerdict`. |
| Scope | `scope/allowlist.py` | add `"nacrule"` to `SUPPORTED_OBJECT_TYPES` + a `RAW_ALLOWLIST["nacrule"]` leaf set (order, enabled, action, matching.*, not_matching.*, apply_tags). **Not** added to `ORG_OBJECT_TYPES` (that constant means "fan out to sites"). |
| Routing | `scope/object_gate.py` or driver | `_is_org_nac_plan(plan)`: every op `object_type=="nacrule"` and no `site_id`. |
| Diff | `ir/diff.py` | register `nacrule` entity kind (add/remove/modify incl. `order`). |
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
    enabled: bool
    action: str | None           # "allow" | "block" | None
    auth_types: frozenset[str]   # ∅ = unconstrained (matches any)
    port_types: frozenset[str]   # ∅ = unconstrained
    match_tags: frozenset[str]   # matching.nactags ids (AND atoms)
    not_match_tags: frozenset[str]
    # remaining match dims — modeled for the diff, treated as opaque by shadowing:
    site_ids: frozenset[str]; sitegroup_ids: frozenset[str]
    family: frozenset[str]; mfg: frozenset[str]; model: frozenset[str]
    os_type: frozenset[str]; vendor: frozenset[str]
    apply_tags: frozenset[str]
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

## 5. GS34 delta-reporting (`nac.rule.change`)

Reads the `nacrule` rows of `diff_ir`. For each **added / removed / modified** rule
(modify includes `order`, `enabled`, `action`, any `matching`/`apply_tags` field):

- emit one `Finding`: `source=CHECK`, `category=NETWORK`, `severity=WARNING`,
  `subject = ObjectRef("nacrule", id, name)`, `caused_by` = the changed fields,
  message states *what* changed and that **access impact is not modeled** (REVIEW).
- No allow/block/role reasoning — that is deferred impact modeling, explicitly out of
  scope for GS34's first step.

WARNING ⇒ the run floors to **REVIEW**.

## 6. NAC rule shadowing (`nac.rule.shadowed`)

Single-state lint over a rule set, run on **baseline IR** and **proposed IR** for delta
attribution.

**Provable rule** (eligible for proof): `order is not None`, and it constrains *only* on
`{auth_types, port_types, match_tags}` — i.e. `not_match_tags` and every other match dim
(`site_ids, sitegroup_ids, family, mfg, model, os_type, vendor`) are empty. Any rule
that fails this is **opaque**: it can neither shadow nor be proven-shadowed (no finding).

**Coverage** — for two enabled, provable rules A (earlier `order`) and B (later):

```python
def covers_choice(a: frozenset, b: frozenset) -> bool:   # auth_types / port_types
    if not a: return True        # A unconstrained = matches any value ⊇ B
    if not b: return False        # B matches any, A does not
    return b <= a                 # A accepts every value B accepts

def covers_and(a: frozenset, b: frozenset) -> bool:       # match_tags (AND)
    return a <= b                 # A requires fewer-or-equal tags ⇒ broader

A_covers_B = (covers_choice(A.auth_types, B.auth_types)
              and covers_choice(A.port_types, B.port_types)
              and covers_and(A.match_tags, B.match_tags))
```

`auth_types`/`port_types` are **choice** dimensions (∅ = "any"; larger set = broader);
`match_tags` is an **AND** dimension (∅ = "no constraint"; larger set = narrower). The
direction differs per dimension — this is the crux and is unit-tested exhaustively.
Exact-duplicate matches are the `A == B` special case and are caught for free.

For each enabled provable B, the **first** earlier enabled provable A with `A_covers_B`
is the shadower. Output: shadowed rule B, shadower A.

**Delta attribution** (consistent with the config-lint tier):

| Shadowed in proposed? | Shadowed in baseline? | Outcome |
|---|---|---|
| yes | no  | **introduced** → `severity=WARNING` → REVIEW |
| yes | yes | **pre-existing** → `severity=INFO` (context, no floor) |

Finding: `source=CHECK`, `category=NETWORK`, `subject=ObjectRef("nacrule", B.id, B.name)`,
`caused_by` references A, evidence records both rules' `action` and the covering
dimensions. A reorder that buries a rule under a broader one therefore surfaces as an
*introduced* shadow.

**Severity is uniform WARNING for v1.** A shadowed `block` rule is a latent security gap
and a shadowed `allow` rule is dead config; distinguishing them (escalating block-shadows
toward UNSAFE) needs action-interplay reasoning and is a **roadmap follow-up** (§10).

## 7. Verdict & decision

`OrgNacVerdict` (lean; no `per_site`/`driving_sites` — mirrors `OrgVerdict`'s
`changes`/`rejections` conventions, reusing the existing `Rejection` type):

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
    changes: tuple[NacDelta, ...]     # the nacrule rows of the diff
    findings: tuple[Finding, ...]     # nac.rule.change + nac.rule.shadowed
    rejections: tuple[Rejection, ...] # short-circuit causes → UNKNOWN
```

Decision via a small `decide_nac(findings, rejections)` helper, mirroring the existing
floor rules (no per-site assumptions, so `decide_org` does not apply):
- any `rejection` (gate / fetch failure / fatal L0) ⇒ **UNKNOWN**.
- any WARNING check finding ⇒ **REVIEW**; any ERROR/CRITICAL `NETWORK` finding ⇒ UNSAFE
  (none emitted in v1).
- only INFO / none ⇒ **SAFE**.

Rendered by `render.py` (dict + human) like the other verdicts.

## 8. Testing

TDD throughout. Layers:

- **Shadowing algorithm** (`tests/checks/nac/test_shadowing.py`) — the core. Tables over:
  catch-all shadows all; equal auth/port/tags = duplicate shadow; subset vs superset on
  each of auth/port/tags *in the correct direction*; disabled A or B ⇒ no shadow;
  opaque rule (uses `site_ids`/`family`/`not_matching`) ⇒ neither shadows nor is shadowed;
  `order is None` ⇒ excluded; introduced-vs-pre-existing attribution.
- **Delta-report** (`test_delta.py`) — add/remove/modify/reorder each yield one WARNING
  REVIEW finding naming the rule + changed fields.
- **IR/ingest** (`test_nac_ingest.py`) — full matching surface mapped; bad row skipped;
  unparseable `order` → None.
- **L0** — `validate_payload("nacrule", …)` registered, type violation caught, unknown
  type still fails closed.
- **Pipeline** (`test_simulate_org_nac.py`) — fetch error → UNKNOWN; clean reorder with no
  shadow → SAFE; reorder that buries a rule → REVIEW.
- **Routing** — `_is_org_nac_plan` true for nacrule/no-site plans, false otherwise; a
  no-`site_id` nacrule plan no longer falls through to rejection.
- **Golden / real-data validation** — replay the **13 TM-LAB rules** through shadowing and
  assert the result matches a hand-checked expectation (this is the empirical check on the
  conservative semantics; redact ids per the replay redaction manifest).

## 9. Prerequisite — OAS snapshot

The committed OAS has only the three switch schemas; `nacrule` is absent, so today a
nacrule plan L0-fails-closed → UNKNOWN. Implementation must add `nac_rule` (and the
`nac_rule_matching` component) to the `extract_oas` WANTED set, re-extract from the Mist
OpenAPI, commit `oas/nacrule.schema.json`, and bump the OAS VERSION. Done before the L0
registration task so `nacrule` validates instead of failing closed.

## 10. Out of scope / roadmap follow-ups

- **NAC impact modeling** (which clients/roles/VLANs an allow/block actually yields) — GS34
  is reporting-only by design.
- **Tag-predicate subsumption** (reasoning across different tag ids via `match`/`values`/
  `match_all`, prefix/substring/negation, and cross-nactag AND-vs-OR semantics) — blocked
  on confirming Mist's cross-tag semantics; v1 uses tag-identity only.
- **Block-shadow security escalation** (shadowed `block` ⇒ UNSAFE) — needs action-interplay
  reasoning.
- **`not_matching` / site / device-attribute coverage in proofs** — currently makes a rule
  opaque; could be modeled later.

## 11. Coordination

The other agent's org-delete / gateway-template / sitetemplate work touches the same
`allowlist` / `object_gate` / org-routing surface. This branch is based on latest
`origin/main` and isolated in a worktree; expect light merge coordination on
`allowlist.py` and the driver routing.
