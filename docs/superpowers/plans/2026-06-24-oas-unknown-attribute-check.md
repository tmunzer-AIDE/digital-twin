# OAS Unknown-Attribute Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the embedded Mist OAS extracts (from `mistsys/mist_openapi`), then make L0 validation flag any payload attribute not documented in that OAS — so genuinely-invalid fields (e.g. a switch `port_config.*.disabled`) are reported instead of passing silently, **without** false-flagging real fields the stale snapshot omitted.

**Architecture:** A **prerequisite first task** refreshes the committed OAS extracts from `mistsys/mist_openapi` (they are stale — missing real fields like `bgp_config` and switch `port_config.*.ae_lacp_force_up`), so pure enforce-by-default does not false-flag real fields; validation stays offline (the extract stays embedded). Then a new composition/map-aware walker (`validate/unknown_keys.py`) compares the effective changed object against the raw (refreshed) committed schema and emits one adapter/operational/WARNING finding per undocumented key. It is folded into `validate_payload`, so all three simulate paths get it for free; findings floor to REVIEW via decision precedence — `decide()` (site/NAC) already floors WARNING, and one additive line in `decide_org()` (org-template) makes the rollup floor WARNING too (closing a latent false-SAFE). The field-gate allowlist is not widened, repurposed, or used as an OAS suppressor (Task 1 may *narrow* invalid modeled leaves, with scope-test updates) — this is the separate "OAS validity" gate, not "modeled surface."

**Tech Stack:** Python 3.14, `jsonschema` (existing L0), pytest, ruff, mypy.

## Global Constraints

- Python 3.14; no new third-party dependencies.
- Gate (all must pass): `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`. (`mypy` type-checks `src/`, not `tests/`.)
- Ruff: 100-column lines, rules E/F/I.
- Finding contract for every unknown key, **verbatim**: `code="l0.schema.unknown_attribute"`, `source=FindingSource.ADAPTER`, `category=FindingCategory.OPERATIONAL`, `severity=Severity.WARNING`, `confidence=HIGH`, `evidence={"path": <dotted path>, "object_type": <type>}`.
- Enforce on complete schemas only; skip the thin ones: `OAS_UNKNOWN_KEY_SKIP = {"wlan", "nacrule", "sitetemplate"}`.
- Cap unknown-attribute findings at 50 (`_MAX_FINDINGS`).
- `null == absent`: a key with value `None` is never flagged.
- Secrets never surfaced: suppress a finding whose dotted path has any segment containing a `STRIP_KEY_PARTS` token.
- Detect-and-report ONLY — no override/suppression flag in the twin (that is the elicitation UI's job).
- **Prerequisite (Task 1):** the embedded OAS extracts must be refreshed from `github.com/mistsys/mist_openapi` (via `tools/extract_oas.py`) before the walker is wired — they currently omit real fields (`bgp_config`, switch `port_config.*.ae_lacp_force_up`). `disabled` is correctly absent from switch `port_config` and must stay flagged. No allowlist suppression — the refresh, not a suppressor, is what prevents false positives.
- Spec of record: `docs/superpowers/specs/2026-06-24-oas-unknown-attribute-check-design.md`.

---

### Task 1: Refresh the embedded OAS extracts (prerequisite)

**Files:**
- Modify: `src/digital_twin/adapters/mist/oas/*.schema.json` (regenerated)
- Modify: `src/digital_twin/adapters/mist/oas/VERSION`
- Modify: `tools/extract_oas.py` (source-reference docstring → `mistsys/mist_openapi`; already changed in the working tree — commit it here)
- Modify (ONLY if reconciliation narrows the allowlist): `src/digital_twin/scope/allowlist.py` (keep `EFFECTIVE_ALLOWLIST` + the `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` lists in lockstep) and the affected `tests/scope/` field-gate tests

**Why:** the committed extracts are stale vs the official OAS — real switches carry `bgp_config` and a `port_config.*.ae_lacp_force_up` leaf that `device_switch`/`networktemplate` omit. Pure enforce-by-default (Task 3) would false-flag these real fields. Refreshing makes the embedded snapshot trustworthy; validation stays offline.

- [ ] **Step 1: Obtain the official OAS spec**

```bash
git clone --depth 1 https://github.com/mistsys/mist_openapi /tmp/mist_openapi
# the OpenAPI document is the repo's mist.openapi.json (or .yaml)
```
(Which version to pin is the maintainer's call; record it in `oas/VERSION`.)

- [ ] **Step 2: Re-extract the committed schemas**

Run:
```bash
.venv/bin/python tools/extract_oas.py /tmp/mist_openapi/mist.openapi.json
```
This rewrites `src/digital_twin/adapters/mist/oas/*.schema.json` from the official components.

- [ ] **Step 3: Gate — every ENFORCED schema documents every modeled root**

This is a pure schema-structure check (no walker — it runs here, before Task 2/3). For each enforced object type (the types NOT in `OAS_UNKNOWN_KEY_SKIP`), every modeled allowlist root must be a documented top-level property; the switch `port_config` entry must document `ae_lacp_force_up` and must NOT document `disabled`; and the nested `networktemplate` `switch_matching.rules[].port_config` entry must document `ae_lacp_force_up`.

```bash
.venv/bin/python - <<'PY'
from digital_twin.adapters.mist.oas import load_schema
from digital_twin.scope.allowlist import RAW_ALLOWLIST

ALL = {"device": "device_switch.schema.json",
       "networktemplate": "networktemplate.schema.json",
       "site_setting": "site_setting.schema.json",
       "gatewaytemplate": "gatewaytemplate.schema.json"}
# THE SCOPE DECISION (the single lever). This set MUST be identical to
# OAS_UNKNOWN_KEY_SKIP in Task 2 (unknown_keys.py). Full scope = thin schemas only;
# device-only v1 = uncomment the templates line.
SKIP = {"wlan", "nacrule", "sitetemplate"}
# SKIP = {"wlan", "nacrule", "sitetemplate", "networktemplate", "site_setting", "gatewaytemplate"}
ENFORCED = {ot: fn for ot, fn in ALL.items() if ot not in SKIP}

problems = []
for ot, fn in ENFORCED.items():
    props = load_schema(fn).get("properties", {})
    for root in sorted({p.split(".")[0] for p in RAW_ALLOWLIST[ot]}):
        if root not in props:
            problems.append(f"{ot}: modeled root {root!r} not documented top-level")
if "device" in ENFORCED:
    dev_entry = (load_schema(ALL["device"])["properties"]["port_config"]
                 ["additionalProperties"]["properties"])
    if "ae_lacp_force_up" not in dev_entry:
        problems.append("device port_config entry: missing ae_lacp_force_up")
    if "disabled" in dev_entry:
        problems.append("device port_config entry: 'disabled' present (must stay absent)")
if "networktemplate" in ENFORCED:
    try:
        nt_entry = (load_schema(ALL["networktemplate"])["properties"]["switch_matching"]
                    ["properties"]["rules"]["items"]["properties"]["port_config"]
                    ["additionalProperties"]["properties"])
        if "ae_lacp_force_up" not in nt_entry:
            problems.append("networktemplate switch port_config entry: missing ae_lacp_force_up")
    except (KeyError, TypeError):
        problems.append("networktemplate switch_matching.rules[].port_config entry not found")

# Guard the walker's allOf approximation: the walker only approximates an `allOf`
# whose branches set their OWN `additionalProperties`. Fail if a refreshed schema
# introduces that shape (then either implement exact support or skip the type).
def allof_addl_hits(node, path="$"):
    hits = []
    if isinstance(node, dict):
        for i, branch in enumerate(node.get("allOf") or []):
            if isinstance(branch, dict) and "additionalProperties" in branch:
                hits.append(f"{path}/allOf[{i}]")
        for k, v in node.items():
            hits += allof_addl_hits(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, e in enumerate(node):
            hits += allof_addl_hits(e, f"{path}[{i}]")
    return hits

for ot, fn in ENFORCED.items():
    for h in allof_addl_hits(load_schema(fn)):
        problems.append(f"{ot}: allOf branch sets additionalProperties at {h} "
                        "(walker only approximates this — implement exact support or skip the type)")

if problems:
    print("REFRESH/RECONCILE INCOMPLETE:")
    for p in problems:
        print("  -", p)
    raise SystemExit(1)
print("OK: enforced schemas cover every modeled root; no unsupported allOf shape")
PY
```
Expected: `OK: ...`. Against **today's** schemas this reports — besides `device: bgp_config` (closed by the refresh) — `networktemplate` missing `dhcpd_config`/`ospf_config`/`stp_config`/`vars`, `site_setting` missing `dhcpd_config`/`ospf_config`/`stp_config`, `gatewaytemplate` missing `vars`. **For each, decide as the OAS owner:** document it top-level in `mistsys/mist_openapi` and re-extract (if the twin legitimately models it there), OR — if it is genuinely not a field for that type — **narrow** `RAW_ALLOWLIST` (and `EFFECTIVE_ALLOWLIST` / the role lists in lockstep). Narrowing moves that leaf modeled→UNKNOWN, so update the affected `tests/scope/` field-gate tests and re-check goldens, and commit `scope/allowlist.py` + those tests in this task. If a type cannot be reconciled now, add it to `OAS_UNKNOWN_KEY_SKIP` (and Task 1's `SKIP`) to defer it. `device` is clean after the refresh, so a **device-only v1** (templates skip-listed) is the minimum that ships the motivating case.

> **This gate is root-level** (it runs before the walker exists). The exhaustive **map-aware leaf** check — every allowlisted leaf at its real nesting (e.g. `port_config.*.mode`, `local_port_config.*.usage`, `bgp_config.*.neighbors.**.neighbor_as`) yields zero findings — is `test_no_modeled_allowlist_leaf_is_flagged` in **Task 3** (it uses the real walker). Expect it to surface more reconciliation than the roots alone: the device `port_config` entry currently omits modeled `mode`/`networks`/`all_networks`/`allow_dhcpd`, so even **device** needs either an OAS addition or an allowlist narrowing for those leaves. Resolve every leaf the same way (document in `mistsys/mist_openapi`, or narrow `RAW_ALLOWLIST`) until both gates pass.
>
> **Device-only v1:** set **both** Task 1's `SKIP` (this step) and Task 2's `OAS_UNKNOWN_KEY_SKIP` to `{"wlan", "nacrule", "sitetemplate", "networktemplate", "site_setting", "gatewaytemplate"}` (they must be identical). The root gate, the leaf-coverage test, and the template regression tests all iterate **only non-skip types** (skip-listed types yield no findings), so they pass without further edits — the skip-set is the single scope lever.

- [ ] **Step 4: Confirm no offline-suite regression from the schema change**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. A refreshed schema can change L0 enum/required/nullable behavior; if any existing L0 / golden test breaks, reconcile it (the refreshed schema is now authoritative) before continuing.

- [ ] **Step 5: Update VERSION + commit**

Update `oas/VERSION` to record the pinned `mistsys/mist_openapi` version + extraction date (and drop the "re-extract pending" note). Then:
```bash
git add src/digital_twin/adapters/mist/oas/ tools/extract_oas.py
git commit -m "chore(oas): refresh embedded extracts from mistsys/mist_openapi"
```
(If reconciliation narrowed the allowlist for any leaf, also `git add src/digital_twin/scope/allowlist.py tests/scope/` — in this or a dedicated commit.)

---

### Task 2: The unknown-attribute walker

**Files:**
- Create: `src/digital_twin/adapters/mist/validate/unknown_keys.py`
- Test: `tests/adapters/mist/test_unknown_keys.py`

**Interfaces:**
- Produces:
  - `OAS_UNKNOWN_KEY_SKIP: frozenset[str]`
  - `unknown_attribute_findings(schema: Mapping[str, Any], payload: Mapping[str, Any], *, object_type: str, scope_roots: Collection[str] | None) -> tuple[Finding, ...]`
- Consumes: `Finding`/`Severity`/`FindingSource`/`FindingCategory` from `digital_twin.contracts`; `Confidence`/`ConfidenceLevel` from `digital_twin.ir`; `STRIP_KEY_PARTS` from `digital_twin.redaction`.

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_unknown_keys.py`:

```python
from digital_twin.adapters.mist.validate.unknown_keys import (
    OAS_UNKNOWN_KEY_SKIP,
    unknown_attribute_findings,
)
from digital_twin.contracts import FindingCategory, FindingSource, Severity

CLOSED = {"type": "object", "properties": {"a": {"type": "string"}}}  # addl absent -> closed
MAP = {"type": "object", "additionalProperties": {"type": "object",
       "properties": {"x": {"type": "integer"}}}}
OPEN = {"type": "object", "properties": {"a": {}}, "additionalProperties": True}
MIXED = {"type": "object", "properties": {"a": {"type": "string"}},
         "additionalProperties": {"type": "object", "properties": {"x": {}}}}


def _paths(findings):
    return {f.evidence["path"] for f in findings}


def test_closed_node_flags_unknown_key():
    out = unknown_attribute_findings(CLOSED, {"a": "ok", "b": 1},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"b"}
    f = out[0]
    assert f.code == "l0.schema.unknown_attribute"
    assert f.source is FindingSource.ADAPTER
    assert f.category is FindingCategory.OPERATIONAL
    assert f.severity is Severity.WARNING
    assert f.evidence["object_type"] == "device"


def test_documented_keys_pass():
    out = unknown_attribute_findings(CLOSED, {"a": "ok"}, object_type="device", scope_roots=None)
    assert out == ()


def test_null_value_treated_as_absent():
    out = unknown_attribute_findings(CLOSED, {"a": "ok", "b": None},
                                     object_type="device", scope_roots=None)
    assert out == ()


def test_map_node_dynamic_keys_pass_and_recurse():
    # arbitrary map keys are fine; an undocumented key INSIDE a map value is flagged
    out = unknown_attribute_findings(
        MAP, {"any-name": {"x": 1, "bogus": 2}}, object_type="device", scope_roots=None)
    assert _paths(out) == {"any-name.bogus"}


def test_open_node_true_allows_extra():
    out = unknown_attribute_findings(OPEN, {"a": 1, "whatever": 2},
                                     object_type="device", scope_roots=None)
    assert out == ()


def test_undocumented_object_node_not_flagged():
    # no properties AND no additionalProperties -> nothing to compare against
    out = unknown_attribute_findings({"type": "object"}, {"anything": 1, "x": 2},
                                     object_type="device", scope_roots=None)
    assert out == ()


def test_explicit_closed_empty_object_flags_keys():
    # additionalProperties: false with no properties -> NO keys allowed
    out = unknown_attribute_findings({"type": "object", "additionalProperties": False},
                                     {"x": 1}, object_type="device", scope_roots=None)
    assert _paths(out) == {"x"}


def test_mixed_node_props_and_additional():
    # 'a' matches properties; any other key is allowed by the map schema (recurses
    # into it); an undocumented key under such a value is flagged
    out = unknown_attribute_findings(
        MIXED, {"a": "ok", "extra": {"x": 1, "nope": 2}},
        object_type="device", scope_roots=None)
    assert _paths(out) == {"extra.nope"}


def test_composition_anyof_union_accepts_second_branch():
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {}}},
        {"type": "object", "properties": {"b": {}}},
    ]}
    out = unknown_attribute_findings(schema, {"b": 1}, object_type="device", scope_roots=None)
    assert out == ()


def test_composition_anyof_map_branch_allows_dynamic_keys():
    # a non-first anyOf branch with schema-valued additionalProperties -> node is MAP,
    # so dynamic keys it allows are NOT flagged, and a leaf inside the value IS checked
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {}}},
        {"type": "object",
         "additionalProperties": {"type": "object", "properties": {"x": {}}}},
    ]}
    assert unknown_attribute_findings(schema, {"dyn": {"x": 1}},
                                      object_type="device", scope_roots=None) == ()
    out = unknown_attribute_findings(schema, {"dyn": {"x": 1, "bad": 2}},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"dyn.bad"}


def test_composition_same_property_across_branches_unions_subschemas():
    # 'p' is documented in BOTH branches with different nested props -> recursion must
    # see the UNION, so a nested key from either branch is accepted (not overwritten).
    schema = {"anyOf": [
        {"type": "object", "properties": {"p": {"type": "object",
                                                "properties": {"a": {}}}}},
        {"type": "object", "properties": {"p": {"type": "object",
                                                "properties": {"b": {}}}}},
    ]}
    assert unknown_attribute_findings(schema, {"p": {"a": 1, "b": 2}},
                                      object_type="device", scope_roots=None) == ()
    out = unknown_attribute_findings(schema, {"p": {"a": 1, "c": 3}},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"p.c"}


def test_composition_two_map_branches_union_value_schemas():
    # two anyOf MAP branches with different value schemas -> a dynamic value's keys
    # from EITHER map are accepted (tied map schemas are combined, not dropped).
    schema = {"anyOf": [
        {"type": "object", "additionalProperties": {"type": "object",
                                                    "properties": {"a": {}}}},
        {"type": "object", "additionalProperties": {"type": "object",
                                                    "properties": {"b": {}}}},
    ]}
    assert unknown_attribute_findings(schema, {"k": {"a": 1, "b": 2}},
                                      object_type="device", scope_roots=None) == ()
    out = unknown_attribute_findings(schema, {"k": {"a": 1, "c": 3}},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"k.c"}


def test_composition_allof_merges_properties():
    schema = {"allOf": [
        {"type": "object", "properties": {"a": {}}},
        {"type": "object", "properties": {"b": {}}},
    ]}
    out = unknown_attribute_findings(schema, {"a": 1, "b": 2, "c": 3},
                                     object_type="device", scope_roots=None)
    assert _paths(out) == {"c"}


def test_array_items_recursion():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {"k": {}}}}}}
    out = unknown_attribute_findings(
        schema, {"items": [{"k": 1}, {"k": 2, "bad": 3}]},
        object_type="device", scope_roots=None)
    assert _paths(out) == {"items.1.bad"}


def test_secret_path_suppressed():
    out = unknown_attribute_findings(
        CLOSED, {"a": "ok", "shared_secret": "zzz"}, object_type="device", scope_roots=None)
    assert out == ()  # 'secret' is a STRIP_KEY_PARTS token


def test_skip_listed_object_type_returns_empty():
    assert "wlan" in OAS_UNKNOWN_KEY_SKIP
    out = unknown_attribute_findings(CLOSED, {"b": 1}, object_type="wlan", scope_roots=None)
    assert out == ()


def test_cap_limits_findings():
    payload = {f"k{i}": 1 for i in range(120)}
    out = unknown_attribute_findings(CLOSED, payload, object_type="device", scope_roots=None)
    assert len(out) == 50


def test_scope_roots_limits_to_changed_roots():
    schema = {"type": "object", "properties": {
        "port_config": {"type": "object", "additionalProperties":
                        {"type": "object", "properties": {"usage": {}}}},
        "other": {"type": "object", "properties": {"ok": {}}}}}
    payload = {"port_config": {"ge-0/0/1": {"usage": "x", "disabled": True}},
               "other": {"ok": 1, "weird": 2}}
    scoped = unknown_attribute_findings(schema, payload, object_type="device",
                                        scope_roots={"port_config"})
    assert _paths(scoped) == {"port_config.ge-0/0/1.disabled"}  # 'other.weird' not in scope
    full = unknown_attribute_findings(schema, payload, object_type="device", scope_roots=None)
    assert _paths(full) == {"port_config.ge-0/0/1.disabled", "other.weird"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/adapters/mist/test_unknown_keys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'digital_twin.adapters.mist.validate.unknown_keys'`.

- [ ] **Step 3: Implement the walker**

Create `src/digital_twin/adapters/mist/validate/unknown_keys.py`:

```python
"""L0 sub-check: flag payload attributes not documented in the committed Mist OAS.

The committed schemas are permissive (no `additionalProperties: false`), so this
treats a documented object (has `properties`, no explicit-open
`additionalProperties`) as a CLOSED set: any present, non-null key not in
`properties` is reported. Map nodes (`additionalProperties` is a schema) and
explicitly-open nodes (`additionalProperties: true`) are not flagged; we recurse
into them. An object node with NO `properties` and no `additionalProperties` is
UNDOCUMENTED (nothing to compare against) and is not judged — but an explicit
`additionalProperties: false` allows NO keys, so it flags every extra key even
with no `properties`. Composition is
resolved conservatively: `anyOf`/`oneOf` take the most
permissive openness (a key allowed in ANY branch is OK); `allOf` merges
properties and takes the most restrictive openness. Findings are
adapter/operational/WARNING -> floor to REVIEW; secret-bearing paths are never
surfaced. Input is expected null-stripped by the caller; the walk also skips
None defensively.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.redaction import STRIP_KEY_PARTS

# Object types NOT enforced (their committed OAS is too thin, or they are deferred
# pending OAS/allowlist reconciliation). This is the single SCOPE LEVER and MUST be
# identical to Task 1's `SKIP` set. Full scope = the three thin schemas; device-only
# v1 = also add networktemplate/site_setting/gatewaytemplate.
OAS_UNKNOWN_KEY_SKIP: frozenset[str] = frozenset({"wlan", "nacrule", "sitetemplate"})

_MAX_FINDINGS = 50  # same cap as schema.py L0 violations — don't flood the verdict
_HIGH = Confidence(level=ConfidenceLevel.HIGH)
# Node states, valued by PERMISSIVENESS (higher = allows more unknown keys), so
# allOf takes the min (most restrictive) and anyOf/oneOf the max (most permissive).
# Order OPEN > MAP > ABSENT > CLOSED matches the spec's anyOf rule: a MAP branch
# wins over a plain/absent branch, so the dynamic keys it allows are not flagged.
# _ABSENT (no `additionalProperties` keyword) is distinct from _CLOSED (explicit
# `additionalProperties: false`): _ABSENT+no-properties is UNDOCUMENTED (skip),
# but _CLOSED allows NO keys, so it flags even with no properties.
_OPEN, _MAP, _ABSENT, _CLOSED = 3, 2, 1, 0


def _self_state(schema: Mapping[str, Any]) -> tuple[int, Mapping[str, Any] | None]:
    if "additionalProperties" not in schema:
        return _ABSENT, None
    ap = schema["additionalProperties"]
    if ap is True:
        return _OPEN, None
    if isinstance(ap, Mapping):
        return _MAP, ap
    return _CLOSED, None  # explicit False (or non-true/non-dict) -> no keys allowed


def _merge_props(into: dict[str, Any], branch: Mapping[str, Any], combinator: str) -> None:
    """Compose duplicate property sub-schemas instead of overwriting, so recursion
    sees EVERY branch's view of a shared key (conservative union -> no false flag)."""
    for k, v in branch.items():
        into[k] = {combinator: [into[k], v]} if k in into else v


def _norm_node(
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], int, Mapping[str, Any] | None]:
    """Resolve an object node (incl. allOf/anyOf/oneOf) to (props, state, map_schema).
    Duplicate property/map sub-schemas across branches are COMPOSED, not overwritten:
    `anyOf` for union branches, `allOf` for intersection branches — so a nested key
    documented in ANY branch is accepted. (NOTE: an `allOf` whose branches set
    their OWN `additionalProperties` is only approximated; Task 1's refresh gate
    FAILS if a refreshed schema introduces that shape, so it cannot land silently —
    implement exact support or skip the type then.)"""
    props: dict[str, Any] = dict(schema.get("properties") or {})
    state, map_schema = _self_state(schema)

    for branch in schema.get("allOf") or []:
        if isinstance(branch, Mapping):
            b_props, b_state, b_map = _norm_node(branch)
            _merge_props(props, b_props, "allOf")
            if b_state < state:  # intersection: most restrictive
                state, map_schema = b_state, b_map
            elif b_state == _MAP and state == _MAP:  # both map constraints apply
                map_schema = {"allOf": [map_schema, b_map]}

    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key) or []:
            if isinstance(branch, Mapping):
                b_props, b_state, b_map = _norm_node(branch)
                _merge_props(props, b_props, "anyOf")
                if b_state > state:  # union: most permissive (avoid false positives)
                    state, map_schema = b_state, b_map
                elif b_state == _MAP and state == _MAP:  # union of map value-schemas
                    map_schema = {"anyOf": [map_schema, b_map]}

    return props, state, map_schema


def _is_secret_path(path: str) -> bool:
    return any(part in seg for seg in path.lower().split(".") for part in STRIP_KEY_PARTS)


def _finding(path: str, object_type: str) -> Finding:
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.unknown_attribute",
        severity=Severity.WARNING,
        confidence=_HIGH,
        message=f"attribute {path!r} is not documented in the {object_type} OAS schema",
        evidence={"path": path, "object_type": object_type},
    )


def _descend(
    value: Any, schema: Mapping[str, Any], path: str, object_type: str, out: list[Finding]
) -> None:
    if len(out) >= _MAX_FINDINGS:
        return
    if isinstance(value, Mapping):
        _walk(value, schema, path, object_type, out)
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for i, elem in enumerate(value):
                _descend(elem, items, f"{path}.{i}", object_type, out)


def _walk(
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
    object_type: str,
    out: list[Finding],
) -> None:
    props, state, map_schema = _norm_node(schema)
    for key, value in payload.items():
        if len(out) >= _MAX_FINDINGS:
            return
        if value is None:  # null == absent
            continue
        child = f"{path}.{key}" if path else key
        if key in props:
            _descend(value, props[key], child, object_type, out)
        elif state == _OPEN:
            continue
        elif state == _MAP:
            if map_schema is not None:
                _descend(value, map_schema, child, object_type, out)
        elif state == _ABSENT and not props:
            continue  # undocumented node (no `additionalProperties`, no properties) -> can't judge
        elif _is_secret_path(child):
            continue
        else:  # _CLOSED (explicit false; flags even with no props), or _ABSENT + documented props
            out.append(_finding(child, object_type))


def unknown_attribute_findings(
    schema: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    object_type: str,
    scope_roots: Collection[str] | None,
) -> tuple[Finding, ...]:
    """Findings for payload keys not documented in `schema`. Empty for skip-listed
    (thin) object types. `scope_roots` limits the walk to those top-level roots
    (None = whole object, the --l0-full-object mode)."""
    if object_type in OAS_UNKNOWN_KEY_SKIP or not isinstance(payload, Mapping):
        return ()
    if scope_roots is not None:
        payload = {k: v for k, v in payload.items() if k in scope_roots}
    out: list[Finding] = []
    _walk(payload, schema, "", object_type, out)
    return tuple(out)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters/mist/test_unknown_keys.py -q`
Expected: PASS (18 tests).

- [ ] **Step 5: Lint + type-check the new module**

Run: `.venv/bin/ruff check src/digital_twin/adapters/mist/validate/unknown_keys.py && .venv/bin/mypy src/digital_twin/adapters/mist/validate/unknown_keys.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/validate/unknown_keys.py tests/adapters/mist/test_unknown_keys.py
git commit -m "feat(l0): unknown-attribute walker (OAS validity gate)"
```

---

### Task 3: Wire the walker into `validate_payload`

**Files:**
- Modify: `src/digital_twin/adapters/mist/validate/schema.py`
- Test: `tests/adapters/mist/test_validate_l0.py` (append)

**Interfaces:**
- Consumes: `unknown_attribute_findings` (Task 2); the refreshed extracts (Task 1); existing `_SCHEMA_FILES`, `load_schema`, `_without_nulls`, `_validator`, `_MAX_FINDINGS`.
- Produces: `validate_payload` now returns `L0Result.findings` containing both `l0.schema.violation` and `l0.schema.unknown_attribute` findings.

- [ ] **Step 1: Write the failing tests**

Append to `tests/adapters/mist/test_validate_l0.py`:

```python
def test_unknown_attribute_flagged_on_device_port_config():
    res = validate_payload(
        "device",
        {"type": "switch", "port_config": {"ge-0/0/10": {"usage": "srv", "disabled": True}}},
    )
    hits = [f for f in res.findings if f.code == "l0.schema.unknown_attribute"]
    assert len(hits) == 1
    assert hits[0].severity is Severity.WARNING
    assert hits[0].evidence["path"] == "port_config.ge-0/0/10.disabled"


def test_unknown_attribute_skipped_for_thin_schema():
    res = validate_payload("wlan", {"isolation": True, "totally_made_up": 1})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_unknown_attribute_respects_scope_roots():
    payload = {"type": "switch",
               "port_config": {"ge-0/0/10": {"usage": "srv", "disabled": True}}}
    flagged = validate_payload("device", payload, scope_roots={"port_config"})
    assert any(f.code == "l0.schema.unknown_attribute" for f in flagged.findings)
    ignored = validate_payload("device", payload, scope_roots={"name"})  # port_config out of scope
    assert not any(f.code == "l0.schema.unknown_attribute" for f in ignored.findings)


def test_clean_device_payload_has_no_unknown_attribute_findings():
    res = validate_payload(
        "device", {"type": "switch", "port_config": {"ge-0/0/0": {"usage": "office"}}})
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_refreshed_oas_recognizes_real_switch_fields():
    # post-Task-1 refresh: bgp_config + port_config.*.ae_lacp_force_up are real
    # switch fields the OAS now documents -> NOT flagged as unknown attributes.
    res = validate_payload("device", {
        "type": "switch",
        "bgp_config": {"sess": {"local_as": 65000}},
        "port_config": {"ge-0/0/0": {"usage": "office", "ae_lacp_force_up": True}},
    })
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)


def test_refreshed_oas_recognizes_template_modeled_roots():
    # post-Task-1 reconciliation: every modeled root on the enforced template types
    # must be documented -> a normal payload using them is NOT flagged. If any of
    # these fails, Task 1's gate was not fully reconciled for that type (resolve in
    # the OAS or by narrowing the allowlist before this task).
    nt = validate_payload("networktemplate", {
        "ospf_config": {"enabled": True},
        "switch_matching": {"rules": [{"port_config": {
            "ge-0/0/0": {"usage": "office", "ae_lacp_force_up": True}}}]},
    })
    assert not any(f.code == "l0.schema.unknown_attribute" for f in nt.findings)
    ss = validate_payload("site_setting", {
        "networks": {"corp": {"vlan_id": 10}},
        "dhcpd_config": {"corp": {"type": "local"}},
    })
    assert not any(f.code == "l0.schema.unknown_attribute" for f in ss.findings)


def test_no_modeled_allowlist_leaf_is_flagged():
    # The LEAF-LEVEL coverage gate (map-aware, via the real walker): every modeled
    # (allowlisted) leaf, at its real nesting, must be documented -> ZERO unknown
    # findings per enforced type. If this fails, a nested modeled leaf (e.g.
    # port_config.*.mode) is undocumented; resolve in the OAS or narrow the
    # allowlist for that type before enforcing it.
    from digital_twin.adapters.mist.validate.unknown_keys import OAS_UNKNOWN_KEY_SKIP
    from digital_twin.scope.allowlist import RAW_ALLOWLIST

    def payload_from(patterns):
        d: dict = {}
        for pat in patterns:
            cur = d
            segs = pat.split(".")
            for i, s in enumerate(segs):
                key = "k" if s == "*" else "10.0.0.1" if s == "**" else s
                if i == len(segs) - 1:
                    cur[key] = 1
                else:
                    cur = cur.setdefault(key, {})
        return d

    for ot in ("device", "networktemplate", "site_setting", "gatewaytemplate"):
        if ot in OAS_UNKNOWN_KEY_SKIP:
            continue
        res = validate_payload(ot, payload_from(RAW_ALLOWLIST[ot]))
        bad = sorted(f.evidence["path"] for f in res.findings
                     if f.code == "l0.schema.unknown_attribute")
        assert not bad, f"{ot}: modeled leaves flagged as unknown: {bad}"
```

- [ ] **Step 1b: Strengthen the existing networktemplate L0 test**

In `tests/adapters/mist/test_validate_l0.py`, `test_networktemplate_l0_schema_registered` currently only asserts `res.fatal is False`. Add an unknown-attribute assertion so it guards the reconciliation:

```python
def test_networktemplate_l0_schema_registered():
    from digital_twin.adapters.mist.validate import validate_payload
    res = validate_payload("networktemplate", {"id": "nt1", "ospf_config": {"enabled": True}})
    assert res.fatal is False  # a valid template body validates
    assert not any(f.code == "l0.schema.unknown_attribute" for f in res.findings)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/adapters/mist/test_validate_l0.py -q -k "unknown_attribute"`
Expected: FAIL — no `l0.schema.unknown_attribute` findings are produced yet.

- [ ] **Step 3: Implement the integration**

In `src/digital_twin/adapters/mist/validate/schema.py`, add the import near the other imports:

```python
from digital_twin.adapters.mist.validate.unknown_keys import unknown_attribute_findings
```

Add a cached raw-schema loader (place it next to `_validator`):

```python
@cache
def _raw_schema(object_type: str) -> dict[str, Any]:
    """Raw committed schema (unmutated) for the unknown-attribute walker."""
    return load_schema(_SCHEMA_FILES[object_type])
```

Replace the tail of `validate_payload` (the `errors`/`findings`/`return` block) with:

```python
    cleaned = _without_nulls(dict(payload))
    errors = (
        err
        for err in _validator(object_type).iter_errors(cleaned)
        if not _touches_secret(err) and _in_scope(err, scope_roots)
    )
    violations = tuple(
        _finding(
            "l0.schema.violation",
            err.message,
            path=".".join(str(p) for p in err.absolute_path),
        )
        for _, err in zip(range(_MAX_FINDINGS), errors, strict=False)
    )
    unknown = unknown_attribute_findings(
        _raw_schema(object_type), cleaned, object_type=object_type, scope_roots=scope_roots
    )
    return L0Result(findings=violations + unknown, fatal=False)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/adapters/mist/test_validate_l0.py -q`
Expected: PASS — the new tests pass AND every pre-existing L0 test still passes (the `findings == ()` assertions on `site_setting`/`device`/`gatewaytemplate` are now also unknown-attribute regression guards; the walker was validated to produce zero findings for those payloads).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/validate/schema.py tests/adapters/mist/test_validate_l0.py
git commit -m "feat(l0): flag undocumented OAS attributes in validate_payload"
```

---

### Task 4: Decision floors — `decide()` (site/NAC) and `decide_org()` (org-template)

**Files:**
- Modify: `src/digital_twin/verdict/org_verdict.py`
- Test: `tests/verdict/test_decision.py` (append)
- Test: `tests/verdict/test_org_verdict.py` (append)

**Interfaces:**
- Consumes: `decide`/`DecisionInputs`/`Decision`, `decide_org`, the `Finding` contract.
- Produces: `decide_org()` floors a WARNING template finding to REVIEW (matching `decide()`), closing the org-template false-SAFE hole.

> **Why:** org-template L0 findings become `template_findings` (`pipeline.py:534`) consumed by `decide_org()`, which today floors only operational ERROR/CRITICAL — not WARNING (`org_verdict.py:61`). A WARNING unknown-attribute finding that doesn't also trip the field gate would roll up SAFE. `decide()` (site/NAC) already floors WARNING, so only `decide_org()` needs the fix.

- [ ] **Step 1: Write the tests (one locks current behavior, one fails)**

Append to `tests/verdict/test_decision.py` (PASSES today — locks the site/NAC floor):

```python
def test_unknown_attribute_finding_alone_floors_to_review():
    from digital_twin.contracts import (
        Finding, FindingCategory, FindingSource, Severity,
    )
    from digital_twin.ir import Confidence, ConfidenceLevel
    from digital_twin.verdict.decision import Decision, DecisionInputs, decide

    f = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.unknown_attribute",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message=(
            "attribute 'port_config.ge-0/0/1.disabled' is not documented "
            "in the device OAS schema"
        ),
    )
    decision, _ = decide(DecisionInputs(
        rejections=(), l0_fatal=False, baseline_unavailable=False,
        check_results=(), adapter_findings=(f,)))
    assert decision is Decision.REVIEW
```

Append to `tests/verdict/test_org_verdict.py` (FAILS today — `decide_org()` ignores WARNING):

```python
def test_decide_org_floors_warning_template_finding():
    from digital_twin.contracts import (
        Finding, FindingCategory, FindingSource, Severity,
    )
    from digital_twin.ir import Confidence, ConfidenceLevel
    from digital_twin.verdict.decision import Decision
    from digital_twin.verdict.org_verdict import decide_org

    wf = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.unknown_attribute",
        severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message=(
            "attribute 'port_config.ge-0/0/1.disabled' is not documented "
            "in the networktemplate OAS schema"
        ),
    )
    # zero assigned sites + one WARNING template finding -> REVIEW, not SAFE
    decision, _, _ = decide_org({}, template_findings=(wf,), org_rejections=())
    assert decision is Decision.REVIEW
```

- [ ] **Step 2: Run the tests — confirm the decide_org one fails**

Run: `.venv/bin/python -m pytest tests/verdict/test_decision.py::test_unknown_attribute_finding_alone_floors_to_review tests/verdict/test_org_verdict.py::test_decide_org_floors_warning_template_finding -v`
Expected: the `decide()` test PASSES (already floors WARNING); the `decide_org()` test FAILS (returns SAFE for zero sites + a WARNING template finding).

- [ ] **Step 3: Fix `decide_org()` to floor warnings**

In `src/digital_twin/verdict/org_verdict.py`, replace the `template_floor` computation in `decide_org()`:

```python
    # a WARNING, or an operational ERROR/CRITICAL, template-level finding floors
    # REVIEW (computed FIRST, so it still applies with zero assigned sites).
    # Mirrors decide(): any WARNING -> REVIEW.
    template_floor = Decision.REVIEW if any(
        f.severity is Severity.WARNING
        or (f.category is FindingCategory.OPERATIONAL
            and f.severity in (Severity.ERROR, Severity.CRITICAL))
        for f in template_findings
    ) else Decision.SAFE
```

Also update the module docstring at the top of the file: change "an operational ERROR/CRITICAL floors REVIEW" to "an operational ERROR/CRITICAL or any WARNING floors REVIEW".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/verdict/test_decision.py tests/verdict/test_org_verdict.py -q`
Expected: PASS — both new tests AND all existing decide/decide_org tests (the change is additive; no existing test feeds a WARNING template finding, so nothing regresses).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/verdict/org_verdict.py tests/verdict/test_decision.py tests/verdict/test_org_verdict.py
git commit -m "fix(verdict): decide_org floors WARNING template findings (no false-SAFE)"
```

---

### Task 5: Pipeline e2e (the motivating case)

**Files:**
- Test: `tests/engine/test_pipeline.py` (append)

**Interfaces:**
- Consumes: the wiring from Tasks 2-4 (no new production code); `simulate`, `Decision`, and the `_plan`/`_op`/`FakeProvider` helpers already in `test_pipeline.py`.

- [ ] **Step 1: Write the acceptance test**

Append to `tests/engine/test_pipeline.py`:

```python
def test_unknown_attribute_surfaces_finding_then_field_gate_drives_unknown():
    # the motivating case: an undocumented `disabled` on a switch port. The L0
    # walker reports it (finding present), but the same out-of-allowlist changed
    # leaf also fails the field gate, so the op resolves UNKNOWN (UNKNOWN > REVIEW).
    payload = {"port_config": {"ge-0/0/0-1": {"usage": "office"},
                               "mge-0/0/0": {"usage": "default", "disabled": True}}}
    v = simulate(
        _plan([_op(object_type="device", object_id="dev-a", payload=payload)]),
        provider=FakeProvider(),
    )
    assert any(
        f.code == "l0.schema.unknown_attribute" and "disabled" in f.evidence.get("path", "")
        for f in v.findings
    )
    assert v.decision is Decision.UNKNOWN
```

- [ ] **Step 2: Run the acceptance test**

Run: `.venv/bin/python -m pytest tests/engine/test_pipeline.py::test_unknown_attribute_surfaces_finding_then_field_gate_drives_unknown -q`
Expected: PASS (no new production code — it locks the end-to-end behavior from Tasks 2-3: finding present, field gate drives UNKNOWN).

- [ ] **Step 3: Commit**

```bash
git add tests/engine/test_pipeline.py
git commit -m "test(l0): e2e unknown-attribute surfaced; field gate drives UNKNOWN"
```

---

### Task 6: ROADMAP + full gate

**Files:**
- Modify: `docs/ROADMAP.md`

- [ ] **Step 1: Record the feature in the ROADMAP**

In `docs/ROADMAP.md`, under `## 2. New coverage — more checks over the existing IR`, add this bullet at the top of that section:

```markdown
- ✅ **OAS unknown-attribute check** (L0 validity gate) — done 2026-06-24.
  `validate_payload` now flags any payload attribute not documented in the
  committed OAS (`l0.schema.unknown_attribute`, WARNING → REVIEW) via a
  composition/map-aware walker (`adapters/mist/validate/unknown_keys.py`).
  Enforce-by-default on the complete schemas; the three thin schemas
  (`wlan`/`nacrule`/`sitetemplate`) are skipped until their OAS extracts are
  completed. Detect-and-report only — accept/override is the elicitation UI's
  job. Preserves the field-gate allowlist as the SEPARATE modeled-surface
  boundary (OAS validity ≠ twin-modeled; merging them would risk false-SAFE).
  Spec/plan: docs/superpowers/{specs,plans}/2026-06-24-oas-unknown-attribute-check*.md.
```

- [ ] **Step 2: Run the full gate**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/mypy src`
Expected: all tests pass; ruff clean; mypy clean.

- [ ] **Step 3: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "docs(roadmap): OAS unknown-attribute check done"
```

---

## Self-Review

**1. Spec coverage** (against `2026-06-24-oas-unknown-attribute-check-design.md`):
- §1/§5 OAS-refresh + per-type reconciliation prerequisite → Task 1 **root gate** (every enforced modeled root documented top-level; device `port_config` `ae_lacp_force_up` present + `disabled` absent; nested networktemplate switch `port_config` `ae_lacp_force_up`; no offline-suite regression) **+ Task 3 leaf gate** (`test_no_modeled_allowlist_leaf_is_flagged`, map-aware, every allowlisted leaf documented). ✅
- §3 site/NAC decision mapping (WARNING → REVIEW) → Task 2 finding contract + Task 4 `decide()` test. ✅
- §3 org-template decision mapping (`decide_org()` must floor WARNING) → Task 4 `decide_org()` fix + `test_decide_org_floors_warning_template_finding`. ✅
- §4 walker (composition union + map-branch + dup-subschema compose, map vs closed, open-node, undocumented-node, explicit-closed, `null==absent`, secrets, cap) → Task 2 `_norm_node`/`_merge_props`/`_walk` + the 18 unit tests. ✅
- §4.1 mixed nodes → `test_mixed_node_props_and_additional`; undocumented node (absent addl + no props) → `test_undocumented_object_node_not_flagged`; explicit `additionalProperties: false` empty → `test_explicit_closed_empty_object_flags_keys`; anyOf map branch (OPEN>MAP>absent) → `test_composition_anyof_map_branch_allows_dynamic_keys`; same-key/two-map union compose → `test_composition_same_property_across_branches_unions_subschemas` + `test_composition_two_map_branches_union_value_schemas`. ✅
- §4.1 `allOf` restrictive / `anyOf`/`oneOf` permissive → `_norm_node` + `test_composition_*`. ✅
- §5 thin-schema skip → `OAS_UNKNOWN_KEY_SKIP` + `test_skip_listed_object_type_returns_empty` + `test_unknown_attribute_skipped_for_thin_schema`. ✅
- §6 integration via `validate_payload`, all 3 paths, `scope_roots`/`l0_full_object` → Task 3. ✅
- §8 field-gate relationship (introduce → UNKNOWN; REVIEW floor in isolation) → Task 4 floors + Task 5 pipeline e2e. ✅
- §9 testing (decision floors, walker unit, scope_roots, e2e, real-field + template + leaf-coverage regression, golden regression) → Tasks 2-5 + `test_refreshed_oas_recognizes_real_switch_fields` + `test_refreshed_oas_recognizes_template_modeled_roots` + `test_no_modeled_allowlist_leaf_is_flagged` + the strengthened `test_networktemplate_l0_schema_registered` + the pre-existing `findings == ()` guards. ✅
- §10 files → Tasks 1-6 (NOTE: walker tests live at `tests/adapters/mist/test_unknown_keys.py`, matching the existing flat layout, not a `validate/` subdir).

**2. Placeholder scan:** none — every step has complete code and exact commands (the Task 1 OAS version pin is the maintainer's choice, called out explicitly, not a placeholder).

**3. Type consistency:** `unknown_attribute_findings(schema, payload, *, object_type, scope_roots)` is defined in Task 2 and called identically in Task 3; `OAS_UNKNOWN_KEY_SKIP` name matches; the `decide_org()` `template_floor` edit in Task 4 keeps its existing return shape; finding `code`/`severity`/`evidence` keys match across Tasks 2-5.
