# Configuration diff in results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a raw, before→after configuration diff (`ObjectConfigDiff`) to every simulate verdict — the literal Mist config the admin edited, leaf by leaf, with secrets masked — as additive, non-load-bearing evidence.

**Architecture:** A shared leaf-diff walk (`scope.paths.leaf_changes`) produces raw per-leaf changes; an assembly function (`config_diff.object_config_diff`) redacts each value for display and packages an `ObjectConfigDiff`. Each simulate path collects one per op at its apply seam and the outer orchestrator attaches them to the verdict, gated on `decision != UNKNOWN`. Redaction relocates to a neutral shared module.

**Tech Stack:** Python 3.14, dataclasses (frozen), pytest, ruff (100-col), mypy-strict, networkx (unaffected).

## Global Constraints

- **Evidence-only / non-load-bearing:** `config_diffs` is NEVER read by `decide()`, any check, coverage, or confidence. A run is byte-identical apart from the new field. (Mirrors `Finding.caused_by`.)
- **Redact display, diff raw:** the diff is computed on RAW values (so a changed secret is still reported as `changed`); only the *display* value stored on `FieldChange` is redacted. The raw secret MUST NOT land on the verdict.
- **Only fully-evaluated verdicts carry diffs:** attach iff `verdict.decision is not Decision.UNKNOWN`.
- **Determinism:** `FieldChange`s sorted by path; `ObjectConfigDiff`s in op/overlay order (golden-stable).
- **Gate (run after every task):** `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src` (mypy checks `src` only, not `tests`). Per-test: `.venv/bin/python -m pytest <path>::<test> -v`.
- **Object identity stays raw:** `ObjectConfigDiff.object_id`/`name` are NOT redacted (verdict-wide `ObjectRef` convention); only `changes[*].before/after` are.
- Spec: `docs/superpowers/specs/2026-06-23-config-diff-in-results-design.md`.

## File Structure

- `src/digital_twin/contracts/config_diff.py` (NEW) — `FieldChange`, `ObjectConfigDiff` (pure types).
- `src/digital_twin/contracts/__init__.py` (MODIFY) — export the two types.
- `src/digital_twin/scope/paths.py` (MODIFY) — `LeafDelta` + `leaf_changes`; re-express `changed_leaf_paths` over it.
- `src/digital_twin/redaction.py` (NEW, via `git mv`) — relocated redaction engine + `REDACTED` + `redact_leaf`.
- `src/digital_twin/observability/replay/redaction.py` (REPLACE) — back-compat re-export shim.
- `src/digital_twin/adapters/mist/validate/schema.py` (MODIFY) — import shared `STRIP_KEY_PARTS`, drop the local duplicate.
- `src/digital_twin/config_diff.py` (NEW) — `object_config_diff` assembly.
- `src/digital_twin/verdict/{verdict,org_verdict,org_nac_verdict}.py` (MODIFY) — add `config_diffs` field.
- `src/digital_twin/drivers/render.py` (MODIFY) — `_render_config_diffs` + wire into dict/human renderers.
- `src/digital_twin/engine/pipeline.py` (MODIFY) — collect + decision-gated attach in all three paths (+ NAC delete branch).
- `docs/ROADMAP.md`, memory (MODIFY) — mark the feature.
- Tests: `tests/contracts/test_config_diff_types.py`, `tests/scope/test_paths.py` (extend), `tests/test_redaction_leaf.py`, `tests/test_config_diff.py`, `tests/drivers/test_render_config_diff.py`, and extensions to `tests/engine/test_simulate_org_nac.py`, `tests/engine/test_pipeline.py`, `tests/engine/test_org_plan.py`.

---

### Task 1: `FieldChange` / `ObjectConfigDiff` contracts

**Files:**
- Create: `src/digital_twin/contracts/config_diff.py`
- Modify: `src/digital_twin/contracts/__init__.py`
- Test: `tests/contracts/test_config_diff_types.py`

**Interfaces:**
- Produces: `FieldChange(path: str, kind: str, before: Any|None, after: Any|None)` and `ObjectConfigDiff(object_type: str, object_id: str, name: str|None, action: str, changes: tuple[FieldChange, ...])`, both frozen, importable from `digital_twin.contracts`.

- [ ] **Step 1: Write the failing test**

Create `tests/contracts/test_config_diff_types.py`:

```python
from digital_twin.contracts import FieldChange, ObjectConfigDiff


def test_field_change_is_frozen_and_holds_values():
    c = FieldChange(path="order", kind="changed", before=2, after=0)
    assert (c.path, c.kind, c.before, c.after) == ("order", "changed", 2, 0)


def test_object_config_diff_holds_changes():
    d = ObjectConfigDiff(
        object_type="nacrule", object_id="b", name="b", action="update",
        changes=(FieldChange("order", "changed", 2, 0),),
    )
    assert d.object_id == "b" and d.action == "update"
    assert d.changes[0].path == "order"
```

- [ ] **Step 2: Run it — expect failure** (`ImportError: cannot import name 'FieldChange'`).

Run: `.venv/bin/python -m pytest tests/contracts/test_config_diff_types.py -v`

- [ ] **Step 3: Create the contract module**

Create `src/digital_twin/contracts/config_diff.py`:

```python
"""Raw configuration diff (before→after) for a simulated change — additive,
non-load-bearing evidence on every verdict. Values here are the REDACTED display
values (masked at assembly in config_diff.object_config_diff); this module is
pure types and reads nothing back into the decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldChange:
    """One changed configuration LEAF. `path` is the dot-path within the object
    (e.g. "enabled", "matching.nactags", "networks.corp.vlan_id"); `before`/`after`
    are REDACTED display values — `before` is None for kind="added", `after` is
    None for kind="removed"."""

    path: str
    kind: str  # "added" | "removed" | "changed"
    before: Any | None
    after: Any | None


@dataclass(frozen=True)
class ObjectConfigDiff:
    """The raw config delta for ONE object an op touches. `object_id`/`name` are
    raw (the verdict-wide ObjectRef convention); only `changes[*].before/after`
    are redacted."""

    object_type: str
    object_id: str
    name: str | None
    action: str  # "create" | "update" | "delete"
    changes: tuple[FieldChange, ...]
```

- [ ] **Step 4: Export from the contracts package**

Modify `src/digital_twin/contracts/__init__.py` — add the import after the `change_plan` line and add both names to `__all__` (keep alphabetical-ish, matching the existing style):

```python
from .change_plan import ChangeOp, ChangePlan, ChangeScope
from .config_diff import FieldChange, ObjectConfigDiff
from .diagram import Diagram
from .finding import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
from .rejection import Rejection

__all__ = [
    "Cause",
    "ChangeOp",
    "ChangePlan",
    "ChangeScope",
    "Diagram",
    "FieldChange",
    "Finding",
    "FindingCategory",
    "FindingSource",
    "ObjectConfigDiff",
    "ObjectRef",
    "Severity",
    "Rejection",
]
```

- [ ] **Step 5: Run the test — expect PASS**, then the full gate.

Run: `.venv/bin/python -m pytest tests/contracts/test_config_diff_types.py -v && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/contracts/config_diff.py src/digital_twin/contracts/__init__.py tests/contracts/test_config_diff_types.py
git commit -m "feat(config-diff): add FieldChange/ObjectConfigDiff contracts"
```

---

### Task 2: `leaf_changes` + `LeafDelta` (one walk, shared with the field gate)

**Files:**
- Modify: `src/digital_twin/scope/paths.py`
- Test: `tests/scope/test_paths.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces: `LeafDelta(path: str, kind: str, before: Any, after: Any)` and `leaf_changes(current, new, ignore_top=()) -> tuple[LeafDelta, ...]` (sorted by path, RAW values). `changed_leaf_paths` keeps its exact signature/output, now derived from `leaf_changes`.

- [ ] **Step 1: Write the failing tests**

First, fold `leaf_changes` into the EXISTING import at the top of `tests/scope/test_paths.py` (line 6) — keep imports at module top (Ruff `E402`) and import only what's used (`LeafDelta` is asserted via its attributes, never referenced by name, so do NOT import it — Ruff `F401`):

```python
from digital_twin.scope.paths import allowed, changed_leaf_paths, leaf_changes, matches
```

Then append the test functions (no new import line in the appended block):

```python
def test_leaf_changes_added_removed_changed():
    cur = {"a": 1, "b": 2, "d": {"x": 1}}
    new = {"a": 1, "b": 3, "c": 9, "d": {}}
    by = {d.path: d for d in leaf_changes(cur, new)}
    assert by["b"].kind == "changed" and by["b"].before == 2 and by["b"].after == 3
    assert by["c"].kind == "added" and by["c"].before is None and by["c"].after == 9
    assert by["d.x"].kind == "removed" and by["d.x"].before == 1 and by["d.x"].after is None


def test_leaf_changes_list_is_atomic():
    by = {d.path: d for d in leaf_changes({"t": [1, 2]}, {"t": [1, 2, 3]})}
    assert set(by) == {"t"}
    assert by["t"].before == [1, 2] and by["t"].after == [1, 2, 3]


def test_leaf_changes_null_equals_absent():
    assert leaf_changes({"a": None}, {}) == ()
    assert leaf_changes({}, {"a": None}) == ()


def test_leaf_changes_ignore_top():
    paths = [d.path for d in leaf_changes(
        {"meta": 1, "a": 1}, {"meta": 2, "a": 2}, ignore_top=("meta",))]
    assert paths == ["a"]


def test_changed_leaf_paths_parity_with_leaf_changes():
    cur = {"a": 1, "b": {"x": 2}, "c": [1]}
    new = {"a": 9, "b": {"x": 2, "y": 3}, "c": [1, 2]}
    assert changed_leaf_paths(cur, new) == tuple(d.path for d in leaf_changes(cur, new))
    assert changed_leaf_paths(cur, new) == ("a", "b.y", "c")  # sorted, unchanged behavior
```

- [ ] **Step 2: Run — expect failure** (`ImportError: cannot import name 'leaf_changes'`).

Run: `.venv/bin/python -m pytest tests/scope/test_paths.py -v`

- [ ] **Step 3: Refactor the walk to capture values**

In `src/digital_twin/scope/paths.py`, add the dataclass import and `LeafDelta`, and replace `changed_leaf_paths` + `_walk` (lines 35-66) with a values-bearing walk that both functions share. Add at the top with the other imports:

```python
from dataclasses import dataclass
```

Replace the existing `changed_leaf_paths` and `_walk` (keep `_normalized`, `_MISSING`, and everything below unchanged) with:

```python
@dataclass(frozen=True)
class LeafDelta:
    path: str
    kind: str  # "added" | "removed" | "changed"
    before: Any
    after: Any


def leaf_changes(
    current: Mapping[str, Any], new: Mapping[str, Any], ignore_top: tuple[str, ...] = (),
) -> tuple[LeafDelta, ...]:
    """Every LEAF that differs between two mappings, WITH its raw before/after.
    Same traversal/semantics as changed_leaf_paths (null==absent, descended
    add/removed subtrees, atomic lists); sorted by path for determinism."""
    out: list[LeafDelta] = []
    _walk(dict(current), dict(new), "", out, ignore_top)
    return tuple(sorted(out, key=lambda d: d.path))


def changed_leaf_paths(
    current: Mapping[str, Any], new: Mapping[str, Any], ignore_top: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs — now derived from leaf_changes so the
    field gate and the config diff share ONE definition of 'what changed'."""
    return tuple(d.path for d in leaf_changes(current, new, ignore_top))


def _walk(cur: Any, new: Any, path: str, out: list[LeafDelta], ignore_top: tuple[str, ...]) -> None:
    if isinstance(cur, dict) and isinstance(new, dict):
        for key in sorted(set(cur) | set(new)):
            if not path and key in ignore_top:
                continue
            sub = f"{path}.{key}" if path else key
            cv, nv = cur.get(key, _MISSING), new.get(key, _MISSING)
            # null == absent (Mist PUT semantics, same canon as compile equivalence)
            if cv is _MISSING and nv is None or nv is _MISSING and cv is None:
                continue
            # descend into an added/removed SUBTREE so its leaves surface individually
            if cv is _MISSING and isinstance(nv, dict):
                cv = {}
            if nv is _MISSING and isinstance(cv, dict):
                nv = {}
            if cv is _MISSING:
                out.append(LeafDelta(sub, "added", None, nv))  # scalar/list added
            elif nv is _MISSING:
                out.append(LeafDelta(sub, "removed", cv, None))  # scalar/list removed
            else:
                _walk(cv, nv, sub, out, ignore_top)
        return
    if _normalized(cur) != _normalized(new):
        out.append(LeafDelta(path, "changed", cur, new))
```

Note: the `changed_leaf_paths` output is byte-identical to before (same paths, same sort order), so the field-gate tests continue to pass — the parity test pins this.

- [ ] **Step 4: Run the new tests + the existing path/gate tests — expect PASS**

Run: `.venv/bin/python -m pytest tests/scope/test_paths.py tests/scope/test_allowlist.py tests/scope/test_bgp_allowlist.py -v`

- [ ] **Step 5: Full gate**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/scope/paths.py tests/scope/test_paths.py
git commit -m "feat(config-diff): add leaf_changes value-walk; derive changed_leaf_paths from it"
```

---

### Task 3: Relocate redaction + `redact_leaf` + centralize `STRIP_KEY_PARTS`

**Files:**
- Move: `src/digital_twin/observability/replay/redaction.py` → `src/digital_twin/redaction.py` (via `git mv`)
- Modify (the moved file): add `REDACTED` + `redact_leaf`
- Create: `src/digital_twin/observability/replay/redaction.py` (back-compat shim)
- Modify: `src/digital_twin/adapters/mist/validate/schema.py`
- Test: `tests/test_redaction_leaf.py`

**Interfaces:**
- Produces: `digital_twin.redaction` exposing the existing `REDACTION_VERSION`, `STRIP_KEY_PARTS`, `redact`, plus new `REDACTED: str` and `redact_leaf(path: str, value: Any) -> Any`.
- The old import path `digital_twin.observability.replay.redaction` keeps re-exporting `REDACTION_VERSION`, `redact` (+ the rest) unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_redaction_leaf.py`:

```python
from digital_twin.redaction import REDACTED, STRIP_KEY_PARTS, redact_leaf


def test_strips_secret_leaf_key():
    assert redact_leaf("psk", "topsecret") == REDACTED


def test_strips_secret_under_sensitive_ancestor():
    # generic leaf key under a sensitive PARENT must still be masked (P1)
    assert redact_leaf("private_key.value", "abc") == REDACTED
    assert redact_leaf("radius.secret.value", "abc") == REDACTED


def test_benign_scalar_passes_through():
    assert redact_leaf("order", 5) == 5


def test_ip_is_pseudonymized():
    out = redact_leaf("gateway", "10.1.2.3")
    assert out != "10.1.2.3" and out.startswith("198.51.")


def test_none_stays_none():
    assert redact_leaf("anything", None) is None


def test_schema_uses_shared_strip_key_parts():
    # parity: L0 secret-suppression and config-diff redaction share ONE source (P2)
    from digital_twin.adapters.mist.validate import schema
    assert schema.STRIP_KEY_PARTS is STRIP_KEY_PARTS


def test_back_compat_import_path_still_works():
    from digital_twin.observability.replay.redaction import REDACTION_VERSION, redact
    assert isinstance(REDACTION_VERSION, str)
    assert redact({"psk": "x"})["psk"] is None
```

- [ ] **Step 2: Run — expect failure** (`ModuleNotFoundError: digital_twin.redaction`).

Run: `.venv/bin/python -m pytest tests/test_redaction_leaf.py -v`

- [ ] **Step 3: Move the module**

```bash
git mv src/digital_twin/observability/replay/redaction.py src/digital_twin/redaction.py
```

- [ ] **Step 4: Add `REDACTED` + `redact_leaf` to the moved module**

Append to `src/digital_twin/redaction.py` (after the existing `redact(...)` function):

```python
REDACTED = "‹redacted›"  # sentinel for stripped secrets (distinct from None)


def redact_leaf(path: str, value: Any) -> Any:
    """Redact ONE config-diff leaf value for display. Takes the FULL dot-path and
    STRIP-matches EVERY segment, so a generic leaf under a sensitive PARENT
    (private_key.value, radius_secret.value) is masked even though its own key is
    benign — mirroring redact()'s strip-before-descend and schema.py's
    _touches_secret (which matches all absolute_path keys). The leaf key drives
    redact()'s scalar pseudonymization (NAME_KEYS, MAC/IP/UUID)."""
    if value is None:
        return None
    if any(part in seg for seg in path.lower().split(".") for part in STRIP_KEY_PARTS):
        return REDACTED
    return redact(value, key=path.rsplit(".", 1)[-1])
```

- [ ] **Step 5: Write the back-compat shim at the old path**

Create `src/digital_twin/observability/replay/redaction.py`:

```python
"""Back-compat re-export. The redaction engine moved to digital_twin.redaction —
now shared by replay fixtures AND result config-diff rendering. Import from there;
this shim keeps existing replay imports working."""

from digital_twin.redaction import (  # noqa: F401
    NAME_KEY_PARTS,
    NAME_KEYS,
    REDACTED,
    REDACTION_VERSION,
    STRIP_KEY_PARTS,
    redact,
    redact_leaf,
)
```

- [ ] **Step 6: Centralize the secret-key list in `schema.py`**

In `src/digital_twin/adapters/mist/validate/schema.py`:

(a) Add the import after the existing `from digital_twin.ir import ...` line (line 25):

```python
from digital_twin.redaction import STRIP_KEY_PARTS
```

(b) Delete the local `_SECRET_KEY_PARTS` tuple (lines 55-64) and tighten the comment above it to:

```python
# Violations on secret-bearing keys are SUPPRESSED: the twin never stores or
# simulates secrets. The key list is the SHARED STRIP_KEY_PARTS from
# digital_twin.redaction (single source — config-diff redaction uses the same),
# and Mist's own API still validates real payloads at apply time.
```

(c) In `_touches_secret` (line 78-81), replace `_SECRET_KEY_PARTS` with `STRIP_KEY_PARTS`:

```python
def _touches_secret(err: jsonschema.ValidationError) -> bool:
    path_keys = [str(p).lower() for p in err.absolute_path]
    blob = " ".join((*path_keys, err.message.lower()))
    return any(part in blob for part in STRIP_KEY_PARTS)
```

- [ ] **Step 7: Run the new tests + existing redaction/replay tests — expect PASS**

Run: `.venv/bin/python -m pytest tests/test_redaction_leaf.py tests/observability/test_redaction.py tests/observability/test_replay_store.py tests/test_public_api.py -v`

- [ ] **Step 8: Full gate** (mypy must still pass — the shim re-exports satisfy importers).

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 9: Commit**

```bash
git add src/digital_twin/redaction.py src/digital_twin/observability/replay/redaction.py src/digital_twin/adapters/mist/validate/schema.py tests/test_redaction_leaf.py
git commit -m "refactor(redaction): relocate to shared module, add redact_leaf, centralize STRIP_KEY_PARTS"
```

---

### Task 4: `object_config_diff` assembly (+ security tests)

**Files:**
- Create: `src/digital_twin/config_diff.py`
- Test: `tests/test_config_diff.py`

**Interfaces:**
- Consumes: `leaf_changes` (Task 2), `redact_leaf` (Task 3), `FieldChange`/`ObjectConfigDiff` (Task 1), `IGNORED_RAW_FIELDS` (`digital_twin.scope.allowlist`).
- Produces: `object_config_diff(*, object_type, object_id, name, action, before, after) -> ObjectConfigDiff`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_diff.py`:

```python
from digital_twin.config_diff import object_config_diff


def _d(action, before, after, ot="nacrule", oid="b", name="b"):
    return object_config_diff(object_type=ot, object_id=oid, name=name,
                              action=action, before=before, after=after)


def test_update_changed_only():
    d = _d("update", {"order": 2, "enabled": True}, {"order": 0, "enabled": True})
    by = {c.path: c for c in d.changes}
    assert set(by) == {"order"}
    assert by["order"].kind == "changed" and by["order"].before == 2 and by["order"].after == 0


def test_create_all_added():
    d = _d("create", {}, {"order": 0, "action": "allow"}, oid="z", name="z")
    assert {c.kind for c in d.changes} == {"added"}
    by = {c.path: c for c in d.changes}
    assert by["action"].before is None and by["action"].after == "allow"


def test_delete_all_removed():
    d = _d("delete", {"order": 2, "action": "allow"}, {})
    assert {c.kind for c in d.changes} == {"removed"}


def test_list_leaf_is_atomic():
    d = _d("update", {"tags": ["a", "b"]}, {"tags": ["a", "b", "c"]})
    by = {c.path: c for c in d.changes}
    assert "tags" in by and by["tags"].before == ["a", "b"] and by["tags"].after == ["a", "b", "c"]


def test_secret_leaf_masked_not_leaked():
    d = _d("update", {"psk": "OLDSECRET"}, {"psk": "NEWSECRET"})
    c = d.changes[0]
    assert c.path == "psk" and c.kind == "changed"
    assert c.before == "‹redacted›" and c.after == "‹redacted›"
    assert "OLDSECRET" not in repr(d.changes) and "NEWSECRET" not in repr(d.changes)


def test_secret_under_sensitive_parent_masked():
    # P1: generic child key "value" under sensitive parent "private_key"
    d = _d("update", {"private_key": {"value": "OLD"}}, {"private_key": {"value": "NEW"}})
    c = d.changes[0]
    assert c.path == "private_key.value"
    assert c.before == "‹redacted›" and c.after == "‹redacted›"
    assert "OLD" not in repr(d.changes) and "NEW" not in repr(d.changes)


def test_object_identity_kept_raw():
    d = _d("update", {"order": 2}, {"order": 0}, oid="rule-42", name="guest")
    assert d.object_id == "rule-42" and d.name == "guest"
```

- [ ] **Step 2: Run — expect failure** (`ModuleNotFoundError: digital_twin.config_diff`).

Run: `.venv/bin/python -m pytest tests/test_config_diff.py -v`

- [ ] **Step 3: Create the assembly module**

Create `src/digital_twin/config_diff.py`:

```python
"""Assemble a redacted before→after ObjectConfigDiff for one changed object.
Pure: diffs raw leaves (scope.paths.leaf_changes), redacts each value for display
(redaction.redact_leaf), never read back into the verdict/decision."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import FieldChange, ObjectConfigDiff
from digital_twin.redaction import redact_leaf
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS
from digital_twin.scope.paths import leaf_changes


def object_config_diff(
    *,
    object_type: str,
    object_id: str,
    name: str | None,
    action: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> ObjectConfigDiff:
    deltas = leaf_changes(before or {}, after or {}, ignore_top=IGNORED_RAW_FIELDS)
    changes = tuple(
        FieldChange(
            path=d.path,
            kind=d.kind,
            before=redact_leaf(d.path, d.before),  # FULL path → any-segment STRIP (P1)
            after=redact_leaf(d.path, d.after),
        )
        for d in deltas
    )
    return ObjectConfigDiff(object_type, object_id, name, action, changes)
```

- [ ] **Step 4: Run — expect PASS**, then full gate.

Run: `.venv/bin/python -m pytest tests/test_config_diff.py -v && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/config_diff.py tests/test_config_diff.py
git commit -m "feat(config-diff): add object_config_diff assembly with redacted display values"
```

---

### Task 5: Verdict `config_diffs` fields + rendering

**Files:**
- Modify: `src/digital_twin/verdict/verdict.py`, `src/digital_twin/verdict/org_verdict.py`, `src/digital_twin/verdict/org_nac_verdict.py`
- Modify: `src/digital_twin/drivers/render.py`
- Test: `tests/drivers/test_render_config_diff.py`

**Interfaces:**
- Consumes: `ObjectConfigDiff`, `FieldChange` (Task 1).
- Produces: `config_diffs: tuple[ObjectConfigDiff, ...] = ()` on `Verdict`, `OrgVerdict`, `OrgNacVerdict`; `_render_config_diffs(diffs) -> list[str]` in render.py; `config_diffs` key in `org_verdict_to_dict`/`org_nac_verdict_to_dict` and a `config changes:` block in the three human renderers. Site `verdict_to_dict` serializes it automatically via `_plain`.

- [ ] **Step 1: Write the failing tests**

Create `tests/drivers/test_render_config_diff.py`:

```python
from digital_twin.contracts import FieldChange, ObjectConfigDiff
from digital_twin.drivers.render import (
    org_nac_verdict_to_dict,
    render_org_nac_human,
)
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.org_nac_verdict import OrgNacVerdict

_CD = ObjectConfigDiff(
    object_type="nacrule", object_id="b", name="b", action="update",
    changes=(
        FieldChange("order", "changed", 2, 0),
        FieldChange("apply_tags", "added", None, ["t"]),
        FieldChange("note", "removed", "old", None),
    ),
)


def _nac_verdict():
    return OrgNacVerdict(Decision.REVIEW, ("r",), (), (), (), (), (_CD,))


def test_org_nac_dict_serializes_config_diffs():
    out = org_nac_verdict_to_dict(_nac_verdict())
    assert out["config_diffs"] == [{
        "object_type": "nacrule", "object_id": "b", "name": "b", "action": "update",
        "changes": [
            {"path": "order", "kind": "changed", "before": 2, "after": 0},
            {"path": "apply_tags", "kind": "added", "before": None, "after": ["t"]},
            {"path": "note", "kind": "removed", "before": "old", "after": None},
        ],
    }]


def test_org_nac_human_renders_config_block():
    human = render_org_nac_human(_nac_verdict())
    assert "config changes:" in human
    assert '  nacrule "b" (update):' in human
    assert "~ order: 2 → 0" in human
    assert "+ apply_tags: ['t']" in human
    assert "- note: 'old'" in human


def test_empty_config_diffs_no_block():
    v = OrgNacVerdict(Decision.SAFE, (), (), (), (), (), ())
    assert "config changes:" not in render_org_nac_human(v)


def test_config_diffs_default_is_empty_tuple():
    # additive/back-compat: omitting config_diffs is valid (6-arg construction)
    v = OrgNacVerdict(Decision.SAFE, (), (), (), (), ())
    assert v.config_diffs == ()


def test_secret_never_appears_in_serialized_output():
    # P3b / spec security bar: pin the SERIALIZER path — a secret assembled into a
    # config diff must not surface in either the JSON dict or the human render.
    import json

    from digital_twin.config_diff import object_config_diff

    cd = object_config_diff(object_type="nacrule", object_id="b", name="b",
                            action="update", before={"psk": "OLDSECRET"},
                            after={"psk": "NEWSECRET"})
    v = OrgNacVerdict(Decision.REVIEW, ("r",), (), (), (), (), (cd,))
    blob = json.dumps(org_nac_verdict_to_dict(v)) + render_org_nac_human(v)
    assert "OLDSECRET" not in blob and "NEWSECRET" not in blob
    assert "‹redacted›" in blob


def test_org_verdict_dict_and_human_render_config_diffs():
    # P3: pin the manually-wired ORG renderers (a missed key/block would slip past
    # the NAC-only tests above).
    from digital_twin.drivers.render import org_verdict_to_dict, render_org_human
    from digital_twin.verdict.org_verdict import OrgVerdict

    cd = ObjectConfigDiff(
        object_type="sitetemplate", object_id="st1", name="st1", action="update",
        changes=(FieldChange("port_usages.trunkB.networks", "changed", ["corp"], []),),
    )
    ov = OrgVerdict(
        decision=Decision.REVIEW, decision_reasons=("r",), changes=(),
        per_site={}, driving_sites=(), site_failures={},
        template_findings=(), org_rejections=(), config_diffs=(cd,),
    )
    out = org_verdict_to_dict(ov)
    assert out["config_diffs"] == [{
        "object_type": "sitetemplate", "object_id": "st1", "name": "st1", "action": "update",
        "changes": [{"path": "port_usages.trunkB.networks", "kind": "changed",
                     "before": ["corp"], "after": []}],
    }]
    human = render_org_human(ov)
    assert "config changes:" in human
    assert '  sitetemplate "st1" (update):' in human
```

(The secret and org tests depend on Task 4's `object_config_diff`, which lands before Task 5.)

- [ ] **Step 2: Run — expect failure** (`TypeError: __init__() takes 6 ... ` / missing key).

Run: `.venv/bin/python -m pytest tests/drivers/test_render_config_diff.py -v`

- [ ] **Step 3: Add the field to the three verdict dataclasses**

`src/digital_twin/verdict/verdict.py` — extend the contracts import (line 10) and add the trailing field after `diagrams` (line 33):

```python
from digital_twin.contracts import Diagram, Finding, ObjectConfigDiff, Severity
```
```python
    diagrams: tuple[Diagram, ...] = ()  # topology charts (mermaid); () when no proposed IR
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after (non-load-bearing)
```

`src/digital_twin/verdict/org_verdict.py` — extend the contracts import (line 14) and add the trailing field after `org_rejections` (line 37):

```python
from digital_twin.contracts import (
    Finding, FindingCategory, ObjectConfigDiff, ObjectRef, Rejection, Severity,
)
```
```python
    org_rejections: tuple[Rejection, ...]  # short-circuit causes: gate/conflict/lookup/fatal-L0
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after of the touched org objects
```

`src/digital_twin/verdict/org_nac_verdict.py` — extend the contracts import (line 9) and add the trailing field after `rejections` (line 29):

```python
from digital_twin.contracts import Finding, ObjectConfigDiff, Rejection
```
```python
    rejections: tuple[Rejection, ...]
    config_diffs: tuple[ObjectConfigDiff, ...] = ()  # raw before→after of the touched nacrules
```

- [ ] **Step 4: Add the shared human renderer + wire all renderers**

In `src/digital_twin/drivers/render.py`:

(a) Extend the contracts import (line 9):

```python
from digital_twin.contracts import Finding, ObjectConfigDiff
```

(b) Add the helper after `_impact_lines` (after line 74):

```python
_MAX_DIFF_LEAVES = 25


def _fmt_val(v: Any) -> str:
    return "∅" if v is None else repr(v)


def _render_config_diffs(diffs: tuple[ObjectConfigDiff, ...]) -> list[str]:
    """Human 'config changes:' block — one group per object, ~/+/- per leaf,
    capped at _MAX_DIFF_LEAVES with an explicit '(+k more)' (no silent truncation)."""
    if not diffs:
        return []
    lines = ["config changes:"]
    for d in diffs:
        who = f'"{d.name}"' if d.name else d.object_id
        lines.append(f"  {d.object_type} {who} ({d.action}):")
        for c in d.changes[:_MAX_DIFF_LEAVES]:
            if c.kind == "changed":
                lines.append(f"    ~ {c.path}: {_fmt_val(c.before)} → {_fmt_val(c.after)}")
            elif c.kind == "added":
                lines.append(f"    + {c.path}: {_fmt_val(c.after)}")
            else:  # removed
                lines.append(f"    - {c.path}: {_fmt_val(c.before)}")
        extra = len(d.changes) - _MAX_DIFF_LEAVES
        if extra > 0:
            lines.append(f"    ... (+{extra} more)")
    return lines
```

(c) In `render_human`, add the block after the diagrams loop (after line 113, before the `state_meta` block):

```python
    lines += _render_config_diffs(verdict.config_diffs)
```

(d) In `org_verdict_to_dict`, add a key (after `"per_site"`):

```python
        "per_site": {sid: verdict_to_dict(v) for sid, v in ov.per_site.items()},
        "config_diffs": [_plain(d) for d in ov.config_diffs],
```

(e) In `org_nac_verdict_to_dict`, add a key (after `"rejections"`):

```python
        "rejections": [{"stage": r.stage, "reasons": list(r.reasons)} for r in v.rejections],
        "config_diffs": [_plain(d) for d in v.config_diffs],
```

(f) In `render_org_nac_human`, add the block before `return`:

```python
    lines += _render_config_diffs(v.config_diffs)
    return "\n".join(lines)
```

(g) In `render_org_human`, add the block before `return` (after the site-failures block):

```python
    lines += _render_config_diffs(ov.config_diffs)
    return "\n".join(lines)
```

- [ ] **Step 5: Run the render tests — expect PASS**

Run: `.venv/bin/python -m pytest tests/drivers/test_render_config_diff.py -v`

- [ ] **Step 6: Full gate** (existing render/verdict tests must still pass — the field is defaulted).

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/verdict/verdict.py src/digital_twin/verdict/org_verdict.py src/digital_twin/verdict/org_nac_verdict.py src/digital_twin/drivers/render.py tests/drivers/test_render_config_diff.py
git commit -m "feat(config-diff): add config_diffs verdict field + dict/human rendering"
```

---

### Task 6: Wire the org-NAC path (+ delete branch) + NAC e2e

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`simulate_org_nac`, ~620-719)
- Test: `tests/engine/test_simulate_org_nac.py` (extend)

**Interfaces:**
- Consumes: `object_config_diff` (Task 4), `config_diffs` field (Task 5), `Decision` (already imported in pipeline).
- Produces: `OrgNacVerdict.config_diffs` populated on the success path (create→all-added, update→changed, delete→all-removed); `()` on every UNKNOWN.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_simulate_org_nac.py`:

```python
def test_config_diff_update_shows_redacted_before_after():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    cds = {d.object_id: d for d in v.config_diffs}
    assert "b" in cds and cds["b"].object_type == "nacrule" and cds["b"].action == "update"
    by = {c.path: c for c in cds["b"].changes}
    assert by["order"].kind == "changed" and by["order"].before == 2 and by["order"].after == 0


def test_config_diff_create_lists_added_leaves():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("create", "z", _rule("z", 0))), provider=FakeProvider(nf))
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["z"].action == "create"
    assert cds["z"].name == "z"  # name comes from `effective`, not the {"id":...} stub
    assert {c.kind for c in cds["z"].changes} == {"added"}
    paths = {c.path for c in cds["z"].changes}
    assert "order" in paths and "action" in paths


def test_config_diff_delete_lists_removed_leaves():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("delete", "b", {})), provider=FakeProvider(nf))
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["b"].action == "delete"
    assert {c.kind for c in cds["b"].changes} == {"removed"}
    paths = {c.path for c in cds["b"].changes}
    assert "order" in paths and "action" in paths


def test_config_diff_empty_on_unknown():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"guest_auth_state": "x"})),
                         provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    assert v.config_diffs == ()
```

- [ ] **Step 2: Run — expect failure** (`config_diffs` empty / attribute mismatch).

Run: `.venv/bin/python -m pytest tests/engine/test_simulate_org_nac.py -k config_diff -v`

- [ ] **Step 3: Import the assembler**

In `src/digital_twin/engine/pipeline.py`, add to the GS34 import block (near line 600, with the other `# noqa: E402` engine imports):

```python
from digital_twin.config_diff import object_config_diff  # noqa: E402
from digital_twin.contracts import ObjectConfigDiff  # noqa: E402
```

(If `ObjectConfigDiff` is already imported from `digital_twin.contracts` at the top of the file, extend that import instead of adding a second line.)

- [ ] **Step 4: Collect diffs in the apply loop**

In `simulate_org_nac`, declare the accumulator just before the `for op in sorted(...)` loop (after `adapter_findings: tuple[Finding, ...] = ()`, ~line 651):

```python
    nac_diffs: list[ObjectConfigDiff] = []
```

In the **delete** branch (currently `if op.action == "delete": proposed_raw.pop(...) ; continue`, ~line 663-665) append before `continue`:

```python
        if op.action == "delete":
            nac_diffs.append(object_config_diff(
                object_type="nacrule", object_id=op.object_id,
                name=baseline_raw[op.object_id].get("name"),
                action="delete", before=baseline_raw[op.object_id], after={}))
            proposed_raw.pop(op.object_id, None)
            continue
```

For create/update, append right after the gate passes and before `proposed_raw[op.object_id] = effective` (~line 687):

```python
        nac_diffs.append(object_config_diff(
            object_type="nacrule", object_id=op.object_id,
            # create's `current` is only {"id": ...}; the new name lives in `effective`
            name=effective.get("name") if op.action == "create" else current.get("name"),
            action=op.action,
            before={} if op.action == "create" else current, after=effective))
        proposed_raw[op.object_id] = effective
```

- [ ] **Step 5: Attach on the success return**

Change the final `return OrgNacVerdict(...)` (line 718-719) to attach the diffs, decision-gated:

```python
    return OrgNacVerdict(
        decision, reasons, nac_changes(diff, base_map, prop_map),
        results, adapter_findings, (),
        tuple(nac_diffs) if decision is not Decision.UNKNOWN else (),
    )
```

(Every early-return `OrgNacVerdict(...)`/`_org_nac_unknown(...)` keeps the 6-arg form → `config_diffs` defaults to `()`. Leave them unchanged.)

- [ ] **Step 6: Run the NAC tests — expect PASS** (new + all existing GS34 tests, proving non-load-bearing).

Run: `.venv/bin/python -m pytest tests/engine/test_simulate_org_nac.py -v`

- [ ] **Step 7: Full gate**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_simulate_org_nac.py
git commit -m "feat(config-diff): wire org-NAC path (create/update/delete), decision-gated"
```

---

### Task 7: Wire the site path + site e2e + UNKNOWN-drops-diffs (P2b)

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`simulate`, ~342-444)
- Test: `tests/engine/test_pipeline.py` (extend)

**Interfaces:**
- Consumes: `object_config_diff` (Task 4, already imported in Task 6), `config_diffs` field, `replace`/`Decision` (already imported).
- Produces: `Verdict.config_diffs` populated on SAFE/REVIEW/UNSAFE; `()` on every UNKNOWN (pre-apply rejections AND post-apply ingest/derived/device-profile failures).

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_pipeline.py`:

```python
def test_site_update_carries_config_diff():
    new = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=new)]), provider=FakeProvider())
    assert v.decision is not Decision.UNKNOWN
    cds = {d.object_id: d for d in v.config_diffs}
    assert SITE in cds and cds[SITE].object_type == "site_setting" and cds[SITE].action == "update"
    by = {c.path: c for c in cds[SITE].changes}
    assert by["networks.voice.vlan_id"].kind == "changed"
    assert by["networks.voice.vlan_id"].before == 30 and by["networks.voice.vlan_id"].after == 31


def test_pre_apply_unknown_drops_config_diffs():
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert v.config_diffs == ()


def test_post_apply_unknown_drops_config_diffs():
    # vars ripple passes the field gate (vars.* allowlisted) then fails the DERIVED
    # gate inside _simulate_site_state — a post-apply UNKNOWN. The decision gate must
    # still drop diffs (P2b), proving it keys off the final decision, not the path.
    ripple = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    v = simulate(_plan([_op(payload=ripple)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("derived_gate" in r for r in v.decision_reasons)
    assert v.config_diffs == ()
```

- [ ] **Step 2: Run — expect failure** (`config_diffs` empty on the success test).

Run: `.venv/bin/python -m pytest tests/engine/test_pipeline.py -k config_diff -v`

- [ ] **Step 3: Collect diffs in the per-op loop**

In `simulate`, declare the accumulator just before the per-op loop (before `for op in sorted(plan.ops, key=lambda o: o.order):`, ~line 343):

```python
    site_diffs: list[ObjectConfigDiff] = []
```

After the field gate passes (after the `rejection = screen_op(...)` block, ~line 396, immediately before `applied = adapter.apply(...)`), append:

```python
            site_diffs.append(object_config_diff(
                object_type=op.object_type, object_id=op.object_id,
                name=current.get("name"), action=op.action,
                before=current, after=effective))
```

(Site ops are `update` only, so `before=current, after=effective`.)

- [ ] **Step 4: Decision-gated attach on the final return**

Replace the final `return _simulate_site_state(...)` (lines 439-444) with a capture-then-attach:

```python
    verdict = _simulate_site_state(
        raw, proposed_raw,
        adapter=adapter, registry=registry, run=run,
        state_meta=state_meta, adapter_findings=adapter_findings,
        profile_proposed=profile_proposed,
    )
    if verdict.decision is not Decision.UNKNOWN:
        verdict = replace(verdict, config_diffs=tuple(site_diffs))
    return verdict
```

(Every earlier `return _unknown(...)` is untouched → `config_diffs` defaults `()`.)

- [ ] **Step 5: Run the site tests — expect PASS** (new + all existing).

Run: `.venv/bin/python -m pytest tests/engine/test_pipeline.py -v`

- [ ] **Step 6: Full gate**

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_pipeline.py
git commit -m "feat(config-diff): wire site path with decision-gated attach (drops diffs on UNKNOWN)"
```

---

### Task 8: Wire the org-template path + org e2e + docs/roadmap/memory + final verify

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`simulate_org_plan`, ~447-591)
- Test: `tests/engine/test_org_plan.py` (extend)
- Modify: `docs/ROADMAP.md`; project memory

**Interfaces:**
- Consumes: `object_config_diff`, `config_diffs` field, `Decision`.
- Produces: `OrgVerdict.config_diffs` populated on the success returns (final + no-sites), `()` on UNKNOWN.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_org_plan.py`:

```python
def test_org_update_carries_config_diff_on_org_verdict():
    st_drop = {"port_usages": {"trunkB": {"mode": "trunk", "networks": []}}}
    ov = simulate_org_plan(_plan(_upd("sitetemplate", "st1", st_drop)),
                           provider=_two_op_provider())
    assert ov.decision is not Decision.UNKNOWN
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "st1" in cds
    assert cds["st1"].object_type == "sitetemplate" and cds["st1"].action == "update"
    by = {c.path: c for c in cds["st1"].changes}
    assert by["port_usages.trunkB.networks"].before == ["corp"]
    assert by["port_usages.trunkB.networks"].after == []


def test_org_delete_lists_removed_leaves():
    ov = simulate_org_plan(_plan(_del("networktemplate", "nt1")),
                           provider=_single_delete_provider())
    cds = {d.object_id: d for d in ov.config_diffs}
    assert "nt1" in cds and cds["nt1"].action == "delete"
    assert {c.kind for c in cds["nt1"].changes} == {"removed"}
    assert "networks.corp.vlan_id" in {c.path for c in cds["nt1"].changes}


def test_org_unknown_drops_config_diffs():
    # non-empty delete payload → object_gate UNKNOWN → no diffs
    ov = simulate_org_plan(
        _plan(_del("networktemplate", "nt1", payload={"networks": {}})),
        provider=_two_op_provider())
    assert ov.decision is Decision.UNKNOWN
    assert ov.config_diffs == ()
```

- [ ] **Step 2: Run — expect failure**.

Run: `.venv/bin/python -m pytest tests/engine/test_org_plan.py -k config_diff -v`

- [ ] **Step 3: Collect diffs in the overlay loop**

In `simulate_org_plan`, declare the accumulator just before the `for i, op in enumerate(plan.ops):` overlay loop (after `template_findings: list[Finding] = []`, ~line 498):

```python
    org_diffs: list[ObjectConfigDiff] = []
```

Append a diff at the END of the overlay-loop body, right after `overlays.append(OrgOverlay(...))` (~line 535):

```python
        org_diffs.append(object_config_diff(
            object_type=op.object_type, object_id=op.object_id,
            name=snapshot.get("name"), action=op.action,
            before=snapshot, after=proposed))
```

(`proposed` is `None` for delete → `object_config_diff` treats it as `{}` → all leaves removed.)

- [ ] **Step 4: Attach on both success returns, decision-gated**

The no-sites return (~line 545-547) — add the field:

```python
        return OrgVerdict(decision=decision, decision_reasons=reasons, changes=tuple(changes),
            per_site={}, driving_sites=driving, site_failures={},
            template_findings=tf, org_rejections=(),
            config_diffs=tuple(org_diffs) if decision is not Decision.UNKNOWN else ())
```

The final return (~line 587-591) — add the field:

```python
    return OrgVerdict(
        decision=decision, decision_reasons=reasons, changes=tuple(changes),
        per_site=per_site, driving_sites=driving, site_failures=site_failures,
        template_findings=tf, org_rejections=(),
        config_diffs=tuple(org_diffs) if decision is not Decision.UNKNOWN else (),
    )
```

(Every `org_unknown(...)` early return keeps the default `()`.)

- [ ] **Step 5: Run the org tests — expect PASS** (new + all existing).

Run: `.venv/bin/python -m pytest tests/engine/test_org_plan.py tests/engine/test_org_pipeline.py tests/engine/test_org_template.py -v`

- [ ] **Step 6: Update ROADMAP + memory**

In `docs/ROADMAP.md`, add a done entry for "configuration diff in results" (match the existing ✅ format used for other features). Update the project memory file `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md` with a one-line note that config-diff (ObjectConfigDiff, redacted before→after on all three verdicts) shipped, and add a pointer line to `MEMORY.md` if a new memory file is created.

- [ ] **Step 7: Final full gate** (the whole suite, proving every existing decision/golden is unchanged — the non-load-bearing invariant).

Run: `.venv/bin/python -m pytest && .venv/bin/ruff check . && .venv/bin/mypy src`

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/engine/pipeline.py tests/engine/test_org_plan.py docs/ROADMAP.md
git commit -m "feat(config-diff): wire org-template path; roadmap + docs"
```

---

## Self-Review

**Spec coverage:**
- §1 contract (FieldChange/ObjectConfigDiff, raw identity) → Task 1.
- §2 leaf_changes + changed_leaf_paths re-expression (atomic lists, null==absent) → Task 2.
- §3 redaction relocation + redact_leaf full-path STRIP + schema centralization → Task 3.
- §4 object_config_diff (create/update/delete table) → Task 4.
- §5 verdict fields + wiring at apply seams + decision-gated honesty → Tasks 5-8.
- Rendering (dict free via _plain for site; explicit for org/nac; human block, capped) → Task 5.
- Tests: leaf_changes/assembly units, security (secret + sensitive-parent), per-path e2e, UNKNOWN drops diffs (pre + post-apply), parity (changed_leaf_paths, STRIP source) → Tasks 2-8.

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `object_config_diff(*, object_type, object_id, name, action, before, after)` is called identically in Tasks 6/7/8; `config_diffs` is the field name on all three verdicts and in render; `redact_leaf(path, value)` and `leaf_changes(current, new, ignore_top=())` signatures match across Tasks 2/3/4. `OrgNacVerdict` 7th positional arg matches the Task 5 field order (decision, reasons, changes, check_results, adapter_findings, rejections, config_diffs).
