# Configuration diff in results (`ObjectConfigDiff`)

**Status:** PROPOSED
**Date:** 2026-06-23
**Author:** brainstormed with the repo owner

A simulation result tells the admin the *decision* (SAFE/REVIEW/UNSAFE/UNKNOWN)
and *findings*, but never shows **what the change actually was** at the
configuration level. This spec adds a raw, before→after configuration diff to
every verdict — the literal Mist config the admin is editing, leaf by leaf, with
sensitive values masked — as pure, non-load-bearing evidence.

## Problem

The pipeline already computes the precise set of changed config leaves at the
apply seam, then throws the values away. Today a NAC verdict says:

```
org-nac decision: REVIEW  changes: modified r1
  reason: NAC rule 'guest_access' modified (action, enabled) — access impact not modeled
```

The admin sees *which* rule and *which field names* changed, but not **`action:
allow → deny`**. To know what they actually changed they must diff the raw
`ChangePlan` they pushed against the live object by hand. The information exists:

- `effective_update(current, payload)` (`adapters/mist/apply/objects.py`) computes
  the exact "after" object Mist would hold;
- `changed_leaf_paths(current, effective)` (`scope/paths.py`) — via the field gate
  — already walks every leaf that differs (the same walk that gates scope);

…but only the *paths* survive (into `IRDiff.changed_fields`, as IR field names),
and the *values* are discarded after gating. We surface them.

## Goals

- Every fully-evaluated verdict (SAFE/REVIEW/UNSAFE) carries a **raw config
  before→after diff** of each object the plan touches: per-leaf `path`, `kind`
  (added/removed/changed), `before`, `after` — the actual Mist config values.
- **Uniform across all three simulate paths** — site (`simulate`), org-template
  (`simulate_org_plan`), and org-NAC (`simulate_org_nac`) — with one shared diff
  type, one assembly function, and one rendering path.
- **Sensitive values are masked.** Secrets (psk/password/secret/token/…) and
  identifiers (MAC/IP/UUID/hostname) appearing as config **values** are redacted
  before they reach the verdict, reusing the existing redaction engine. The diff
  is computed on **raw** values so a changed secret is still *reported as changed*
  — `psk: ‹redacted› → ‹redacted›` — without leaking it. (The object's own
  identity — `object_id`/`name` — is NOT redacted; see the boundary note in §1.)
- **Strictly additive and non-load-bearing.** The diff is evidence only. It MUST
  NOT change any decision, severity, confidence, coverage, finding, or reason. A
  run is byte-identical apart from the new field. (Same contract as
  `Finding.caused_by`.)
- **Consistent with the gate.** The diff's leaf granularity and null==absent /
  full-object-replacement semantics are *exactly* what the field gate evaluated —
  one shared walk, no second source of truth.

## Non-goals (recorded, deferred)

- **IR-semantic value diff** (old→new on normalized IR fields). We deliberately
  chose the *raw vendor config* (what the admin edits), not the IR. The IR field
  *names* already appear via `IRDiff.changed_fields` / `caused_by`.
- **Per-site rippled config diffs under an org-template plan.** When an org
  template changes, each assigned site's *effective* config also changes
  (`apply_overlays` yields `base_raw`/`prop_raw` per site). v1 shows the **template
  object** diff once on the `OrgVerdict` (the thing the admin edited); the
  derived per-site rippled diffs are deferred (large, secondary). Per-site
  `Verdict.config_diffs` stays `()` under the org path.
- **Config diffs on UNKNOWN/rejected plans.** A plan rejected by parse/scope/L0
  has no coherent "after"; the rejection *reasons* explain it. `config_diffs` is
  populated only on fully-evaluated verdicts. (See "Honesty: only complete plans".)
- **Diff-driven grouping or `--explain` re-simulation.** Presentation/precision
  features layered on this data; separate.

## Core mechanism

### 1. The contract — `FieldChange` + `ObjectConfigDiff`

Two frozen dataclasses in `contracts/` (new `contracts/config_diff.py`, exported
from `contracts/__init__.py`), and one trailing-defaulted field on each verdict:

```python
@dataclass(frozen=True)
class FieldChange:
    """One changed configuration LEAF. `path` is the dot-path within the object
    (e.g. "enabled", "matching.nactags", "networks.corp.vlan_id"); `before`/`after`
    are the REDACTED display values (masked for secrets/identifiers). `before` is
    None for kind="added", `after` is None for kind="removed"."""
    path: str
    kind: str            # "added" | "removed" | "changed"
    before: Any | None   # redacted display value
    after: Any | None    # redacted display value

@dataclass(frozen=True)
class ObjectConfigDiff:
    """The raw config delta for ONE object an op touches."""
    object_type: str     # "nacrule" | "device" | "wlan" | "networktemplate" | ...
    object_id: str
    name: str | None
    action: str          # "create" | "update" | "delete"
    changes: tuple[FieldChange, ...]   # sorted by path
```

Each verdict gains one additive, trailing-defaulted field — back-compatible, all
existing construction sites compile unchanged:

- `Verdict.config_diffs: tuple[ObjectConfigDiff, ...] = ()` (site)
- `OrgVerdict.config_diffs: tuple[ObjectConfigDiff, ...] = ()` (org-template)
- `OrgNacVerdict.config_diffs: tuple[ObjectConfigDiff, ...] = ()` (org-NAC)

`config_diffs` is distinct from the existing per-object summaries
(`OrgNacVerdict.changes`/`NacDelta`, `OrgVerdict.changes`/`OrgChange`,
`Verdict.ir_diff`): those are the *what-changed summary* (object + action + IR
field names); `config_diffs` is the *raw value detail*. They coexist.

**Crucially, `FieldChange` stores only REDACTED values.** The raw secret never
lands on the verdict, so the generic `_plain`/`verdict_to_dict` serializer cannot
leak it. Redaction happens at assembly time (below), not at render time.

**Redaction boundary — `object_id`/`name` are intentionally raw (P3).** Only
config **values** (the new surface this feature introduces) are redacted.
`ObjectConfigDiff.object_id` and `name` are NOT — they follow the convention every
verdict already uses: `ObjectRef(kind, id, name)`, `NacDelta.name`,
`OrgChange.ref.name`, and `Finding.subject` all carry raw ids/names today. Keeping
them raw is also load-bearing for the reader: it lets a `config_diffs` entry be
correlated 1:1 with its `changes`/`NacDelta`/`OrgChange` summary (which key off the
raw `rule_id`/`object_id`). Redacting object identity here would both diverge from
the rest of the verdict and break that correlation, so it is explicitly out of
scope. (Object *names* that are themselves sensitive are a pre-existing,
verdict-wide question, not something this feature changes.)

### 2. The diff walk — `leaf_changes` (one walk, shared with the gate)

`scope/paths.py` already has `changed_leaf_paths(current, new, ignore_top)`, whose
private `_walk` records the path of every differing leaf. Refactor that one walk
to also capture the values, and expose:

```python
@dataclass(frozen=True)
class LeafDelta:
    path: str
    kind: str            # "added" | "removed" | "changed"
    before: Any          # raw value (None when absent)
    after: Any           # raw value (None when absent)

def leaf_changes(
    current: Mapping[str, Any], new: Mapping[str, Any], ignore_top: tuple[str, ...] = (),
) -> tuple[LeafDelta, ...]:   # sorted by path
    ...

def changed_leaf_paths(...) -> tuple[str, ...]:   # now derived, single source
    return tuple(sorted(d.path for d in leaf_changes(current, new, ignore_top)))
```

The kind falls out of the existing traversal:
- both sides present, `_normalized(cur) != _normalized(new)` → `"changed"`;
- a leaf/subtree present only in `new` → `"added"` (`before=None`);
- a leaf/subtree present only in `current` → `"removed"` (`after=None`).

The existing semantics are preserved verbatim, which is the point:
- **null == absent** (a `None` leaf equals a missing one) — Mist PUT canon;
- **added/removed subtrees are descended** so each leaf gates individually;
- **lists compare atomically** — a list-valued field is ONE leaf at its own path
  (`sitegroup_ids`, not `sitegroup_ids.0`), with the whole list as before/after.
  This matches the gate exactly; renderers show `sitegroup_ids: [a,b] → [a,b,c]`.

Because `changed_leaf_paths` becomes a thin wrapper over `leaf_changes`, the diff
shown is provably the same leaf set the field gate screened. `leaf_changes`
returns **raw** values and stays free of any redaction dependency (clean layering:
`scope/` does not import redaction).

### 3. Redaction — relocate to a neutral module, mask for display

Redaction is now a shared concern (replay fixtures AND result display), so the
pure engine moves out of the replay subpackage:

- **Move** `observability/replay/redaction.py` → **`src/digital_twin/redaction.py`**
  (top-level, dependency-neutral). Content unchanged: `REDACTION_VERSION`,
  `redact`, `STRIP_KEY_PARTS`, the scalar/embedded/entropy machinery.
- Leave `observability/replay/redaction.py` as a **back-compat re-export**
  (`from digital_twin.redaction import *  # noqa` + explicit names) so its real
  importers under `observability/replay/` (`store.py`, `__init__.py`) and existing
  tests keep working unchanged. `REDACTION_VERSION` keeps its current value
  (fixtures embed it) — this is a move, not a semantic change.
- **Centralize the secret-key list (P2).** `adapters/mist/validate/schema.py`
  today **duplicates** the list as its own `_SECRET_KEY_PARTS`
  ([schema.py:55](../../../src/digital_twin/adapters/mist/validate/schema.py),
  byte-identical to `STRIP_KEY_PARTS`, with a "kept in sync" comment). Replace it
  with `from digital_twin.redaction import STRIP_KEY_PARTS` and use that in
  `_touches_secret`, so **L0 secret-suppression and config-diff redaction share
  one source and cannot drift.** `_touches_secret` already matches against every
  `absolute_path` key — the same any-segment rule `redact_leaf` adopts in P1, so
  the two are now consistent in both *list* and *match scope*.

Add one display helper to `redaction.py`:

```python
REDACTED = "‹redacted›"   # sentinel for stripped secrets (distinct from None)

def redact_leaf(path: str, value: Any) -> Any:
    """Redact ONE leaf value for display. Takes the FULL dot-path, not just the
    leaf key, and STRIP-matches EVERY segment — so a generic leaf under a
    sensitive PARENT (e.g. "private_key.value", "radius_secret.value",
    "auth_servers.0_is_atomic"…) is masked even though its own key ("value") is
    benign. This mirrors the original redact(), which strips a secret-bearing dict
    key BEFORE descending into it, and schema.py:_touches_secret, which matches
    against all absolute_path keys. The leaf key alone drives redact()'s scalar
    pseudonymization (NAME_KEYS, MAC/IP/UUID)."""
    if value is None:
        return None
    segments = path.lower().split(".")
    if any(part in seg for seg in segments for part in STRIP_KEY_PARTS):
        return REDACTED
    return redact(value, key=path.rsplit(".", 1)[-1])  # MAC/IP/UUID/name + entropy backstop
```

Two-layer protection:
1. The explicit STRIP check is matched against **every path segment**, so a
   secret under *any* sensitive ancestor key — not just a sensitive leaf key — is
   masked to the `‹redacted›` sentinel. This closes the
   `private_key.value`-style leak that a leaf-key-only check would miss.
2. `redact(value, key=leaf_key)` then handles MAC/IP/UUID/hostname pseudonymization
   and the entropy backstop (catching a high-entropy secret in a benignly-named
   field). A list-valued leaf passes through `redact`, which **recurses** into list
   elements — so secret keys *inside* a list-of-dicts (e.g. `radius_servers:
   [{"secret": …}]`) are still stripped by `redact()`'s own dict branch.

Together the two layers reproduce the original redaction's coverage: secret under a
sensitive ancestor (layer 1), secret inside a list element dict (layer 2 recursion),
and unkeyed high-entropy secret (layer 2 backstop).

Because the diff is computed on RAW values (step 2) and only the *display* value
is redacted, a changed secret is still emitted as `kind="changed"` with
`before=after=‹redacted›` — the admin learns *that* it changed without seeing it.

### 4. Assembly — `object_config_diff`

One pure function (new `src/digital_twin/config_diff.py`), called by all three
paths. It is the only place `leaf_changes` meets `redact_leaf`:

```python
def object_config_diff(
    *, object_type: str, object_id: str, name: str | None, action: str,
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
) -> ObjectConfigDiff:
    deltas = leaf_changes(before or {}, after or {}, ignore_top=IGNORED_RAW_FIELDS)
    changes = tuple(
        FieldChange(
            path=d.path, kind=d.kind,
            before=redact_leaf(d.path, d.before),   # FULL path → any-segment STRIP (P1)
            after=redact_leaf(d.path, d.after),
        )
        for d in deltas
    )
    return ObjectConfigDiff(object_type, object_id, name, action, changes)
```

`ignore_top=IGNORED_RAW_FIELDS` matches the field gate, so server-managed metadata
never appears in the diff. `before/after` per action:

| action | before | after | result |
|---|---|---|---|
| create | `{}` | effective | every leaf `added` |
| update | current | effective | only changed leaves |
| delete | current | `{}` | every leaf `removed` (the agreed full enumeration) |

`create` arises **only on the org-NAC path** — `nacrule` is the only object type
whose gate permits `create` (the `is_nac` branch,
[object_gate.py:28-45](../../../src/digital_twin/scope/object_gate.py),
`_NAC_ACTIONS` at line 17); site and org-template ops are `update`/`delete` only
(the org branch at lines 55-66). The assembly function
handles all three actions generically, but in practice site/org never feed it a
`create`.

### 5. Wiring — one `ObjectConfigDiff` per op, at the apply seam

Each path already holds `(object_type, object_id, action, before, after)` exactly
where it gates. Collect a diff per op into a list and attach to the verdict.

In each path, collect the per-op `ObjectConfigDiff`s into a list during the apply
loop, then attach them to the verdict in the **outer** orchestrator with a single
decision-gated `replace` (see "Honesty" below). `_simulate_site_state` is **not**
modified.

- **site `simulate`** (`engine/pipeline.py` ~344-405): in the per-op loop, after
  `effective = effective_update(current, op.payload)`, append
  `object_config_diff(..., action=op.action, before=current, after=effective)`
  (delete→`after={}`; site ops are `update` only today, so `before=current`).
  After `_simulate_site_state(...)` returns, attach the collected tuple to the
  returned `Verdict` (decision-gated, below).
- **org `simulate_org_plan`** (~499-535, 587): in the overlay loop, after
  `proposed_t` is computed (or `proposed=None` for delete), append
  `object_config_diff(..., before=snapshot, after=proposed)`. Attach to the final
  `OrgVerdict` (and the no-sites `OrgVerdict` at ~545), decision-gated. Org ops are
  `update`/`delete` only. Per-site `Verdict`s keep `config_diffs=()` (non-goal:
  rippled diffs) — `_simulate_site_state` is called per-site and never attaches.
- **org-NAC `simulate_org_nac`** (~653-687): in the apply loop, append
  `object_config_diff(...)` for each op — including the **delete** branch
  (currently a bare `continue`: emit `before=current, after={}` before
  continuing). create→`before={}, after=effective`; update→`before=current,
  after=effective`. Attach to the final `OrgNacVerdict` (the success path that
  calls `decide` + `nac_changes`), decision-gated.

### Honesty: only fully-evaluated verdicts carry diffs

`config_diffs` is attached to a verdict **only when its `decision` is not
`UNKNOWN`** — i.e. the plan fully evaluated to SAFE/REVIEW/UNSAFE. The attachment
happens in the **outer** orchestrator *after* the verdict is built, via
`replace(verdict, config_diffs=…) if verdict.decision is not Decision.UNKNOWN`.

Gating on the **final decision** (not on which code path produced the verdict) is
what makes this robust. The site path reaches UNKNOWN through *many* exits —
pre-apply rejections in `simulate`, and post-apply failures *inside*
`_simulate_site_state` (baseline/proposed ingest crash, ingest-None,
derived-gate rejection, and a device-profile rejection at the final `assemble`,
[pipeline.py:176-272](../../../src/digital_twin/engine/pipeline.py)). A single
decision check covers them all, and it leaves `_simulate_site_state` untouched
(no new param) — mirroring how `diagrams` is already grafted on via `replace` at
the end of that function.

Rationale: a plan that did not fully evaluate has no coherent applied state, so a
partial value diff could mislead; the rejection *reasons* already explain what
blocked it. (The object-level `changes` summaries that org/NAC thread through
their UNKNOWNs are unaffected — those name *attempted* objects, not values.) This
also avoids threading `config_diffs` through a dozen `_unknown` call sites.

## Rendering

One shared human helper, generic dict serialization.

- **Dict / JSON.**
  - *site* `verdict_to_dict` → `_plain(verdict)` already recurses dataclasses +
    tuples, so `config_diffs` serializes **for free**.
  - *NAC* `org_nac_verdict_to_dict` and *org* `org_verdict_to_dict` build dicts by
    hand → add `"config_diffs": [_plain(d) for d in v.config_diffs]` (reuse the
    generic walker; DRY). Existing keys unchanged; the array is additive.
  - Shape: `{"object_type","object_id","name","action","changes":[{"path","kind",
    "before","after"}, …]}`.
- **Human.** A shared `_render_config_diffs(diffs) -> list[str]` used by
  `render_human`, `render_org_human`, and `render_org_nac_human`:

  ```
  config changes:
    nacrule "guest_access" (update):
      ~ action: allow → deny
      ~ enabled: true → false
      ~ sitegroup_ids: [sg-a, sg-b] → [sg-a]
      + apply_tags: [tag-x]
      - description: "legacy"
    networktemplate "corp" (update):
      ~ networks.corp.vlan_id: 10 → 20
      ... (+12 more)
  ```

  `~`/`+`/`-` = changed/added/removed; values are the redacted display values
  (`psk: ‹redacted› → ‹redacted›`). Cap at N leaves per object (e.g. 25) with an
  explicit `... (+k more)` line — **no silent truncation** (project doctrine).
  No block when `config_diffs=()`.

## Impact on verdict / contracts

- Three verdict dataclasses each gain one trailing-defaulted field → fully
  back-compatible.
- `decide()` / coverage / confidence / checks are untouched: `config_diffs` is
  never read by the verdict or analysis layers. The non-load-bearing invariant is
  test-pinned.
- `scope/paths.py` gains `LeafDelta` + `leaf_changes`; `changed_leaf_paths` is
  re-expressed over it (behavior identical, covered by existing gate tests).
- Redaction relocates to `src/digital_twin/redaction.py` with a back-compat
  re-export at the old path; `redact_leaf` + `REDACTED` are added there.
  `schema.py`'s duplicate `_SECRET_KEY_PARTS` is replaced by an import of the
  shared `STRIP_KEY_PARTS` (single source; L0 and config-diff cannot drift).
- New pure module `src/digital_twin/config_diff.py` (`object_config_diff`).

## Testing

- **`leaf_changes` unit:** added/removed/changed kinds; null==absent skip;
  descended subtrees; **atomic list** leaf; `ignore_top` honored; sorted output.
  Plus a parity test: `changed_leaf_paths == sorted(d.path for d in leaf_changes)`
  on the existing gate fixtures (proves the refactor is behavior-preserving).
- **`object_config_diff` unit:** create→all added; update→changed only;
  delete→all removed; name/action carried; deterministic order.
- **Redaction (security-critical):** a plan changing a `psk`/`passphrase`/
  `password` leaf → `FieldChange(kind="changed", before="‹redacted›",
  after="‹redacted›")`, and the raw secret string appears **nowhere** in
  `verdict_to_dict(...)` nor `render_human(...)` output. A high-entropy value in a
  benignly-named field → redacted by the backstop. MAC/IP → pseudonymized.
- **Redaction — sensitive parent, generic child (P1):** a leaf whose own key is
  benign but whose **ancestor** key is sensitive — `private_key.value`,
  `radius_secret.value`, a secret nested in a list-of-dicts (`radius_servers:
  [{"secret": …}]`) — is masked. The raw value must not appear in any output. This
  is the leak the leaf-key-only check would have missed.
- **Redaction parity / no-drift (P2):** assert config-diff redaction and L0
  `_touches_secret` resolve to the **same** `STRIP_KEY_PARTS` object (import
  identity), so the two can never diverge. (`schema.py` no longer owns a private
  copy.)
- **Per-path e2e:** one golden each — **site update**, **org-template update**
  (and/or delete), **NAC create+modify+delete** (NAC is the only path exercising
  `create`→all-added, so the golden covers all three actions) — asserting the
  rendered before→after and the dict shape.
- **Non-load-bearing golden:** same NAC plan as the GS34 golden; assert
  decision/severity/findings/reasons are byte-identical to the pre-feature verdict
  (only `config_diffs` differs).
- **UNKNOWN drops diffs (P2b):** assert `config_diffs == ()` for BOTH a *pre-apply*
  rejection (parse/scope) AND a *post-apply* UNKNOWN on the site path — e.g. a plan
  whose ops pass gating but then hit a derived-gate or device-profile rejection
  (decision=UNKNOWN). The decision-gated attach must drop diffs in both, proving
  the gate keys off the final decision, not the code path.
- **Back-compat:** `observability/replay/redaction.py` re-export still satisfies
  `replay/store.py` + `test_public_api`; `REDACTION_VERSION` unchanged.
- Gate unchanged: `.venv/bin/python -m pytest && .venv/bin/ruff check . &&
  .venv/bin/mypy src`.

## Phasing (for the implementation plan)

1. **Contract + walk:** `FieldChange`/`ObjectConfigDiff` in `contracts/`;
   `LeafDelta` + `leaf_changes` in `scope/paths.py` with `changed_leaf_paths`
   re-expressed over it; parity + unit tests. (No behavior change yet.)
2. **Redaction relocation + centralization:** move engine to
   `src/digital_twin/redaction.py`, back-compat re-export, add `redact_leaf`
   (full-path/any-segment STRIP) + `REDACTED`; replace `schema.py`'s
   `_SECRET_KEY_PARTS` with an import of `STRIP_KEY_PARTS` (P2); redaction unit
   tests incl. the sensitive-parent/generic-child case (P1) and the parity
   assertion (P2).
3. **Assembly:** `src/digital_twin/config_diff.py` (`object_config_diff`) + unit
   tests (incl. the secret-masking test).
4. **Verdict fields + rendering:** add `config_diffs` to the three verdicts;
   `_render_config_diffs` (human) + dict serialization; non-load-bearing golden.
5. **Wire the three paths:** site, org-template, org-NAC apply seams (incl. the
   NAC delete branch); per-path e2e goldens; ROADMAP/memory update; live verify
   (read-only) that existing decisions are unchanged and diffs now render.

## Open questions / risks

- **`changed_leaf_paths` refactor is on a hot path** (the field gate). Mitigated
  by the parity test against existing gate fixtures — the path output must be
  byte-identical; only an additional values-bearing return is new.
- **List-valued leaves can be large** (e.g. a long `sitegroup_ids`). JSON keeps
  the full list; human render caps per object. Acceptable; flagged for review.
- **Redaction completeness is the security bar.** The diff must never leak a
  secret. Coverage is three-layered: full-path/any-segment STRIP (secret under a
  sensitive ancestor), `redact()` recursion (secret inside a list element dict),
  and the entropy backstop (unkeyed high-entropy secret). The dedicated security
  tests pin all three; new redaction-sensitive config surfaces (if any) should
  extend `STRIP_KEY_PARTS` (now the single shared source), not this feature.
- **Any-segment STRIP can over-redact** a benign path that happens to contain a
  strip substring (e.g. a key containing "cert"). This is intentional and matches
  the *existing* behavior of `redact()` and `schema.py:_touches_secret` (both
  substring-match) — over-redaction is the safe side for a security boundary.
- **Create `before={}` vs `{"id":…}` (NAC).** Using `before={}` for create elides
  the identity field from the diff (it is not a meaningful "added" value);
  intentional.
