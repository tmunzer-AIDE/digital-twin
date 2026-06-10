# Plan 3 — Scope Gates + L0 Validation + Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the change-plan path of the twin — `ChangePlan`/`Finding` contracts, the four scope gates (envelope, object, field, derived) with the M1 allowlist, thin L0 payload validation against the committed Mist OAS, and `apply` (ordered rolling full-object replacement) — so a delta can be gated, validated, applied to raw state, recompiled, and derived-impact-checked end to end.

**Architecture:** Everything here is **errors-as-values** (a shared `Rejection` value; never exceptions for flow). Dependency direction per spec: `contracts` imports (almost) nobody, `scope → {ir, contracts}`, `adapters → {ir, contracts}` — scope and adapters never import each other. Gates run in pipeline order (envelope → object → fetch → field → L0 → apply → compile → derived) but each is a pure standalone function; the engine that sequences them is Plan 5. `apply` does object-level replacement only; inheritance derivation stays in the existing compiler, re-run on `raw'`.

**Tech Stack:** Python 3.14, uv, frozen dataclasses + StrEnum, `jsonschema` (new dep, L0 only), pytest/ruff/mypy(strict). Reuses Plan 2's `adapters/mist/oas` (committed schemas + `load_schema`) and `providers.base.RawSiteState`.

**Spec sections implemented:** "Supported delta types (M1)", "Delta semantics", component contracts for `scope/*`, `adapters/mist/validate`, `adapters/mist/apply`, module layout `contracts/` + `scope/` + `validate/` + `apply/` + `adapters/base.py` facade protocol.

**Documented deviations from the spec layout (intentional, small):**
- `contracts/rejection.py` is added (not in the spec tree): one shared errors-as-value type for every gate + apply, instead of N per-module rejection types. It is a pure value type — exactly what `contracts/` is for.
- `contracts/finding.py` imports `Confidence` from `ir/` (spec says "contracts imports nobody"). `ir` is pure and imports nothing, so the DAG stays acyclic; duplicating the Confidence type would be worse.
- Spec's `adapters/mist/validate/oas.py` (load/cache OAS) already exists as Plan 2's `adapters/mist/oas/` package — `validate/` reuses it; no second loader.
- `apply` preserves server-managed identity fields (`id`, `mac`, `type`, …) from the current object: Mist `PUT` replaces user config but never lets a payload change immutable server fields, and downstream ingest needs `mac`/`type`. Pure full-replacement would let a payload silently corrupt identity.

---

### Task 1: `contracts/finding.py` — Finding, Severity, categories

**Files:**
- Create: `src/digital_twin/contracts/__init__.py`
- Create: `src/digital_twin/contracts/finding.py`
- Test: `tests/contracts/__init__.py`, `tests/contracts/test_finding.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/contracts/__init__.py  (empty)
```

```python
# tests/contracts/test_finding.py
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel


def test_finding_constructs_with_spec_fields():
    f = Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code="l0.schema.type",
        severity=Severity.ERROR,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message="networks must be an object",
        evidence={"path": "networks"},
    )
    assert f.code == "l0.schema.type"
    assert f.affected_entities == ()  # default
    assert f.remediation is None  # default


def test_finding_is_frozen():
    import pytest

    f = Finding(
        source=FindingSource.CHECK,
        category=FindingCategory.NETWORK,
        code="x",
        severity=Severity.INFO,
        confidence=Confidence(level=ConfidenceLevel.LOW),
        message="m",
    )
    with pytest.raises(Exception):
        f.code = "y"  # type: ignore[misc]


def test_severity_values_match_spec():
    assert [s.value for s in Severity] == ["info", "warning", "error", "critical"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/contracts/ -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'digital_twin.contracts'`

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/contracts/finding.py
"""Finding: the one result DTO shared by adapter validation (L0) and checks (L2/L3).

Spec: source = adapter|check; category network|operational — NETWORK severity
ERROR/CRITICAL drives UNSAFE, OPERATIONAL never does (it drives REVIEW: the twin
had trouble, which is not evidence the network breaks).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from digital_twin.ir import Confidence


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FindingSource(StrEnum):
    ADAPTER = "adapter"  # L0/L1 payload validation
    CHECK = "check"  # L2/L3 neutral checks


class FindingCategory(StrEnum):
    NETWORK = "network"  # predicted network breakage
    OPERATIONAL = "operational"  # the twin itself had trouble


@dataclass(frozen=True)
class Finding:
    source: FindingSource
    category: FindingCategory
    code: str  # stable machine code, e.g. "l2.blackhole.vlan_isolated"
    severity: Severity
    confidence: Confidence
    message: str
    affected_entities: tuple[str, ...] = ()  # IR entity ids
    evidence: Mapping[str, Any] = field(default_factory=dict)
    remediation: str | None = None
```

```python
# src/digital_twin/contracts/__init__.py
"""Cross-cutting value types (pure) — imported by everyone, imports only ir."""

from .finding import Finding, FindingCategory, FindingSource, Severity

__all__ = ["Finding", "FindingCategory", "FindingSource", "Severity"]
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/contracts/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/contracts tests/contracts
git commit -m "Plan 3: contracts.Finding (shared L0/check result DTO)"
```

---

### Task 2: `contracts/change_plan.py` + `contracts/rejection.py`

**Files:**
- Create: `src/digital_twin/contracts/change_plan.py`
- Create: `src/digital_twin/contracts/rejection.py`
- Modify: `src/digital_twin/contracts/__init__.py`
- Test: `tests/contracts/test_change_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/contracts/test_change_plan.py
from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection


def test_change_plan_constructs():
    plan = ChangePlan(
        source="mist",
        scope=ChangeScope(org_id="o1", site_id="s1"),
        intent="move voice vlan",
        ops=(
            ChangeOp(
                action="update",
                order=0,
                object_type="site_setting",
                object_id="s1",
                payload={"networks": {}},
            ),
        ),
    )
    assert plan.ops[0].object_type == "site_setting"
    assert plan.scope.site_id == "s1"


def test_scope_site_id_is_optional():
    assert ChangeScope(org_id="o1").site_id is None


def test_rejection_carries_stage_and_reasons():
    r = Rejection(stage="object_gate", reasons=("unsupported object_type 'wlan'",))
    assert r.stage == "object_gate"
    assert "wlan" in r.reasons[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/contracts/test_change_plan.py -q`
Expected: FAIL — ImportError (ChangeOp etc. not defined)

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/contracts/change_plan.py
"""ChangePlan: the envelope an AI agent submits — ordered full-object-replacement ops.

A ChangeOp payload is the COMPLETE new object (Mist PUT semantics), never a
merge-patch. `order` is a total order; semantics are enforced by scope/envelope
(shape) and adapters apply (state), not here — these are plain value types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChangeScope:
    org_id: str
    site_id: str | None = None


@dataclass(frozen=True)
class ChangeOp:
    action: str  # M1: "update" only (gated in scope/object_gate)
    order: int
    object_type: str
    object_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ChangePlan:
    source: str  # owning adapter, e.g. "mist"
    scope: ChangeScope
    ops: tuple[ChangeOp, ...]
    intent: str | None = None
```

```python
# src/digital_twin/contracts/rejection.py
"""Rejection: the shared errors-as-value for gates and apply.

Every gating outcome carries its stage + human-readable reasons; the engine
(Plan 5) maps any Rejection to decision UNKNOWN with an UNSUPPORTED reason.
Never raised — always returned.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rejection:
    stage: str  # "envelope" | "object_gate" | "field_gate" | "apply" | "derived_gate"
    reasons: tuple[str, ...]
```

```python
# src/digital_twin/contracts/__init__.py
"""Cross-cutting value types (pure) — imported by everyone, imports only ir."""

from .change_plan import ChangeOp, ChangePlan, ChangeScope
from .finding import Finding, FindingCategory, FindingSource, Severity
from .rejection import Rejection

__all__ = [
    "ChangeOp",
    "ChangePlan",
    "ChangeScope",
    "Finding",
    "FindingCategory",
    "FindingSource",
    "Severity",
    "Rejection",
]
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/contracts/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/contracts tests/contracts
git commit -m "Plan 3: ChangePlan/ChangeOp/ChangeScope + shared Rejection value"
```

---

### Task 3: `scope/allowlist.py` — the M1 allowlist DATA

**Files:**
- Create: `src/digital_twin/scope/__init__.py`
- Create: `src/digital_twin/scope/allowlist.py`
- Test: `tests/scope/__init__.py`, `tests/scope/test_allowlist.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/__init__.py  (empty)
```

```python
# tests/scope/test_allowlist.py
from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    IGNORED_RAW_FIELDS,
    RAW_ALLOWLIST,
    SUPPORTED_OBJECT_TYPES,
)


def test_supported_object_types_are_the_m1_pair():
    assert SUPPORTED_OBJECT_TYPES == ("site_setting", "device")


def test_raw_allowlist_matches_spec_table():
    assert RAW_ALLOWLIST["site_setting"] == ("networks.*", "port_usages.*", "vars.*")
    assert RAW_ALLOWLIST["device"] == (
        "port_config.*",
        "networks.*",
        "port_usages.*",
        "name",
        "notes",
    )


def test_effective_allowlist_covers_what_the_ir_consumes():
    # everything resolve_effective_ports/vlans read, and vars (the allowed input)
    for f in ("networks", "port_usages", "vars", "port_config", "local_port_config",
              "port_config_overwrite"):
        assert f in EFFECTIVE_ALLOWLIST


def test_server_metadata_is_ignored_in_raw_diffs():
    for f in ("id", "org_id", "site_id", "created_time", "modified_time"):
        assert f in IGNORED_RAW_FIELDS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scope/ -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/scope/__init__.py
"""The change-plan gates — one module per gate, allowlist as data."""
```

```python
# src/digital_twin/scope/allowlist.py
"""The M1 allowlist DATA (spec: 'Supported delta types — the honest-decision boundary').

Default-deny everywhere: anything not listed here is out of scope -> UNKNOWN.
`X.*` entries allow a whole named subtree (and the key itself, e.g. removing the
whole subtree); bare entries allow exactly that leaf.
"""

from __future__ import annotations

SUPPORTED_OBJECT_TYPES: tuple[str, ...] = ("site_setting", "device")

# Raw changed-path allowlist per object_type (post-fetch field gate).
# vars.* is allowed ONLY because the post-compile derived gate catches ripple.
RAW_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "site_setting": ("networks.*", "port_usages.*", "vars.*"),
    "device": ("port_config.*", "networks.*", "port_usages.*", "name", "notes"),
}

# Server-managed fields excluded from the raw diff: a PUT payload never carries
# them, and their absence is not a user change.
IGNORED_RAW_FIELDS: tuple[str, ...] = (
    "id",
    "org_id",
    "site_id",
    "created_time",
    "modified_time",
    "mac",
    "serial",
    "model",
    "type",
)

# Effective-config fields the IR actually consumes (post-compile derived gate):
# any OTHER effective field differing between baseline and proposed -> UNKNOWN.
# vars is listed because it is the allowed input; its RIPPLE into any
# out-of-scope field (e.g. dhcpd_config) still trips the gate on that field.
EFFECTIVE_ALLOWLIST: tuple[str, ...] = (
    "networks",
    "port_usages",
    "vars",
    "port_config",
    "local_port_config",
    "port_config_overwrite",
)
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/scope/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope tests/scope
git commit -m "Plan 3: M1 allowlist data (raw paths, effective fields, ignored metadata)"
```

---

### Task 4: `scope/envelope.py` — ChangePlan shape validation

**Files:**
- Create: `src/digital_twin/scope/envelope.py`
- Test: `tests/scope/test_envelope.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/test_envelope.py
from digital_twin.contracts import ChangePlan, Rejection
from digital_twin.scope.envelope import parse_change_plan

VALID = {
    "source": "mist",
    "scope": {"org_id": "o1", "site_id": "s1"},
    "intent": "why",
    "ops": [
        {"action": "update", "order": 0, "object_type": "site_setting",
         "object_id": "s1", "payload": {"networks": {}}},
        {"action": "update", "order": 1, "object_type": "device",
         "object_id": "d1", "payload": {"name": "sw"}},
    ],
}


def test_valid_plan_parses():
    plan = parse_change_plan(VALID)
    assert isinstance(plan, ChangePlan)
    assert [op.order for op in plan.ops] == [0, 1]
    assert plan.intent == "why"


def test_intent_is_optional():
    data = {k: v for k, v in VALID.items() if k != "intent"}
    plan = parse_change_plan(data)
    assert isinstance(plan, ChangePlan) and plan.intent is None


def test_missing_source_rejects():
    r = parse_change_plan({**VALID, "source": ""})
    assert isinstance(r, Rejection) and r.stage == "envelope"


def test_empty_ops_rejects():
    assert isinstance(parse_change_plan({**VALID, "ops": []}), Rejection)


def test_duplicate_order_rejects():
    ops = [dict(VALID["ops"][0]), {**dict(VALID["ops"][1]), "order": 0}]
    r = parse_change_plan({**VALID, "ops": ops})
    assert isinstance(r, Rejection)
    assert any("order" in reason for reason in r.reasons)


def test_two_ops_on_same_object_rejects():
    # full-object replacement: the later op silently kills the earlier one
    op0 = dict(VALID["ops"][0])
    r = parse_change_plan({**VALID, "ops": [op0, {**op0, "order": 5}]})
    assert isinstance(r, Rejection)
    assert any("same object" in reason for reason in r.reasons)


def test_non_dict_payload_rejects():
    bad = {**dict(VALID["ops"][0]), "payload": "oops"}
    assert isinstance(parse_change_plan({**VALID, "ops": [bad]}), Rejection)


def test_all_reasons_collected_not_just_first():
    bad = {"source": "", "scope": {}, "ops": []}
    r = parse_change_plan(bad)
    assert isinstance(r, Rejection) and len(r.reasons) >= 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scope/test_envelope.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/scope/envelope.py
"""Envelope (shape) validation: dict in -> ChangePlan value or Rejection.

SHAPE only — vendor-neutral structural rules incl. the two static multi-op
constraints from the spec's Delta semantics (unique `order`; one op per
(object_type, object_id), because a full-object-replacement plan with two ops
on one object makes the earlier op dead — an authoring error). M1 *policy*
(which types/actions are supported) lives in object_gate, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection

_STAGE = "envelope"


def parse_change_plan(data: Mapping[str, Any]) -> ChangePlan | Rejection:
    reasons: list[str] = []

    source = data.get("source")
    if not isinstance(source, str) or not source:
        reasons.append("source must be a non-empty string")

    scope_raw = data.get("scope")
    org_id, site_id = "", None
    if not isinstance(scope_raw, Mapping) or not isinstance(scope_raw.get("org_id"), str) \
            or not scope_raw.get("org_id"):
        reasons.append("scope.org_id must be a non-empty string")
    else:
        org_id = str(scope_raw["org_id"])
        sid = scope_raw.get("site_id")
        if sid is not None and (not isinstance(sid, str) or not sid):
            reasons.append("scope.site_id must be a non-empty string when present")
        else:
            site_id = sid

    intent = data.get("intent")
    if intent is not None and not isinstance(intent, str):
        reasons.append("intent must be a string when present")

    ops_raw = data.get("ops")
    ops: list[ChangeOp] = []
    if not isinstance(ops_raw, list) or not ops_raw:
        reasons.append("ops must be a non-empty list")
    else:
        for i, op in enumerate(ops_raw):
            parsed = _parse_op(op, i, reasons)
            if parsed is not None:
                ops.append(parsed)

    if len(ops) == len(ops_raw or []):  # only run cross-op checks on fully-parsed ops
        orders = [op.order for op in ops]
        if len(set(orders)) != len(orders):
            reasons.append("op order values must be unique (duplicate order)")
        targets = [(op.object_type, op.object_id) for op in ops]
        if len(set(targets)) != len(targets):
            reasons.append(
                "two ops target the same object (full replacement makes the earlier op dead)"
            )

    if reasons:
        return Rejection(stage=_STAGE, reasons=tuple(reasons))
    return ChangePlan(
        source=str(source),
        scope=ChangeScope(org_id=org_id, site_id=site_id),
        ops=tuple(ops),
        intent=intent,
    )


def _parse_op(op: Any, index: int, reasons: list[str]) -> ChangeOp | None:
    if not isinstance(op, Mapping):
        reasons.append(f"ops[{index}] must be an object")
        return None
    problems: list[str] = []
    action = op.get("action")
    if not isinstance(action, str) or not action:
        problems.append("action")
    order = op.get("order")
    if not isinstance(order, int) or isinstance(order, bool):
        problems.append("order (int)")
    object_type = op.get("object_type")
    if not isinstance(object_type, str) or not object_type:
        problems.append("object_type")
    object_id = op.get("object_id")
    if not isinstance(object_id, str) or not object_id:
        problems.append("object_id")
    payload = op.get("payload")
    if not isinstance(payload, Mapping):
        problems.append("payload (object)")
    if problems:
        reasons.append(f"ops[{index}] invalid fields: {', '.join(problems)}")
        return None
    return ChangeOp(
        action=str(action),
        order=int(order),
        object_type=str(object_type),
        object_id=str(object_id),
        payload=dict(payload),
    )
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/scope/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope tests/scope
git commit -m "Plan 3: envelope gate (ChangePlan shape + static multi-op constraints)"
```

---

### Task 5: `scope/object_gate.py` — pre-fetch M1 policy gate

**Files:**
- Create: `src/digital_twin/scope/object_gate.py`
- Test: `tests/scope/test_object_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/test_object_gate.py
from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection
from digital_twin.scope.object_gate import check_objects


def _plan(ops, site_id="s1", source="mist"):
    return ChangePlan(
        source=source, scope=ChangeScope(org_id="o1", site_id=site_id), ops=tuple(ops)
    )


def _op(object_type="site_setting", object_id="s1", action="update", order=0):
    return ChangeOp(action=action, order=order, object_type=object_type,
                    object_id=object_id, payload={})


def test_m1_valid_plan_passes():
    plan = _plan([_op(), _op(object_type="device", object_id="d1", order=1)])
    assert check_objects(plan) is None


def test_unknown_source_rejects():
    r = check_objects(_plan([_op()], source="aruba"))
    assert isinstance(r, Rejection) and r.stage == "object_gate"


def test_template_object_type_rejects_as_fanout():
    r = check_objects(_plan([_op(object_type="networktemplate", object_id="nt1")]))
    assert isinstance(r, Rejection)
    assert any("networktemplate" in reason for reason in r.reasons)


def test_missing_site_id_rejects_single_site_rule():
    r = check_objects(_plan([_op()], site_id=None))
    assert isinstance(r, Rejection)


def test_site_setting_object_id_must_match_scope_site():
    r = check_objects(_plan([_op(object_id="OTHER-site")]))
    assert isinstance(r, Rejection)


def test_non_update_action_rejects():
    r = check_objects(_plan([_op(action="create")]))
    assert isinstance(r, Rejection)
    assert any("create" in reason for reason in r.reasons)


def test_all_offending_ops_reported():
    plan = _plan([_op(object_type="wlan", object_id="w1", order=0),
                  _op(object_type="rftemplate", object_id="r1", order=1)])
    r = check_objects(plan)
    assert isinstance(r, Rejection) and len(r.reasons) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scope/test_object_gate.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/scope/object_gate.py
"""Pre-fetch policy gate: M1 object_type whitelist, action, source, single-site.

Runs before any state fetch (no state needed). Everything outside the M1
boundary is rejected LOUDLY with per-op reasons — never silently passed.
The post-fetch device-ROLE check (switch-only) cannot run here; it lives with
the field gate where the fetched device is available.
"""

from __future__ import annotations

from digital_twin.contracts import ChangePlan, Rejection
from digital_twin.scope.allowlist import SUPPORTED_OBJECT_TYPES

_STAGE = "object_gate"
_M1_SOURCE = "mist"
_M1_ACTION = "update"


def check_objects(plan: ChangePlan) -> Rejection | None:
    reasons: list[str] = []
    if plan.source != _M1_SOURCE:
        reasons.append(f"unsupported source {plan.source!r} (M1 supports only 'mist')")
    if not plan.scope.site_id:
        reasons.append("scope.site_id is required (M1 simulates exactly one site)")
    for op in plan.ops:
        if op.action != _M1_ACTION:
            reasons.append(f"ops[order={op.order}]: unsupported action {op.action!r} "
                           f"(M1 supports only 'update')")
        elif op.object_type not in SUPPORTED_OBJECT_TYPES:
            reasons.append(
                f"ops[order={op.order}]: unsupported object_type {op.object_type!r} "
                "(templates/org objects fan out beyond one site; not modeled in M1)"
            )
        elif (
            op.object_type == "site_setting"
            and plan.scope.site_id
            and op.object_id != plan.scope.site_id
        ):
            reasons.append(
                f"ops[order={op.order}]: site_setting object_id {op.object_id!r} "
                f"!= scope.site_id {plan.scope.site_id!r} (cross-site fan-out)"
            )
    return Rejection(stage=_STAGE, reasons=tuple(reasons)) if reasons else None
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/scope/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope tests/scope
git commit -m "Plan 3: object gate (pre-fetch M1 whitelist + single-site + action)"
```

---

### Task 6: `scope/field_gate.py` — post-fetch raw changed-path pre-screen

**Files:**
- Create: `src/digital_twin/scope/field_gate.py`
- Test: `tests/scope/test_field_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/test_field_gate.py
from digital_twin.contracts import Rejection
from digital_twin.scope.field_gate import changed_paths, screen_op

CURRENT = {
    "id": "s1",
    "modified_time": 111,
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {"office": {"mode": "access", "port_network": "corp"}},
    "vars": {"x": "1"},
    "dhcpd_config": {"corp": {"ip": "10.0.0.2"}},
}


def test_changed_paths_detects_leaf_edit():
    payload = {**CURRENT, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    assert changed_paths(CURRENT, payload) == ("networks.voice.vlan_id",)


def test_changed_paths_counts_removal_as_change():
    # full-object replacement: a key present in current but absent from payload IS a change
    payload = {k: v for k, v in CURRENT.items() if k != "dhcpd_config"}
    assert changed_paths(CURRENT, payload) == ("dhcpd_config",)


def test_changed_paths_ignores_server_metadata():
    payload = {k: v for k, v in CURRENT.items() if k not in ("id", "modified_time")}
    assert changed_paths(CURRENT, payload) == ()


def test_in_scope_change_passes():
    payload = {**CURRENT, "vars": {"x": "2"}}
    assert screen_op("site_setting", CURRENT, payload) is None


def test_out_of_scope_change_rejects_with_paths():
    payload = {**CURRENT, "dhcpd_config": {"corp": {"ip": "10.0.0.99"}}}
    r = screen_op("site_setting", CURRENT, payload)
    assert isinstance(r, Rejection) and r.stage == "field_gate"
    assert any("dhcpd_config" in reason for reason in r.reasons)


def test_whole_subtree_removal_of_allowed_field_passes():
    payload = {k: v for k, v in CURRENT.items() if k != "vars"}
    assert screen_op("site_setting", CURRENT, payload) is None


def test_device_exact_leaves_name_notes():
    cur = {"name": "sw-a", "notes": "old", "port_config": {"ge-0/0/0": {"usage": "office"}}}
    assert screen_op("device", cur, {**cur, "name": "sw-b"}) is None
    r = screen_op("device", cur, {**cur, "managed": False})
    assert isinstance(r, Rejection)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scope/test_field_gate.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/scope/field_gate.py
"""Post-fetch raw pre-screen: which raw paths does this op actually change?

Diffs payload vs the CURRENT raw object (per spec, the rolling pre-op state —
the engine passes the right one) and matches every changed path against the
raw allowlist. Full-object-replacement semantics: a field present in current
but absent from payload counts as CHANGED (removed). Server-managed metadata
(IGNORED_RAW_FIELDS) is excluded — a payload never carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import IGNORED_RAW_FIELDS, RAW_ALLOWLIST

_STAGE = "field_gate"


def changed_paths(current: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Dot-paths of every leaf that differs (additions, edits, removals)."""
    out: list[str] = []
    _walk(dict(current), dict(payload), "", out, ignore_top=IGNORED_RAW_FIELDS)
    return tuple(sorted(out))


def _walk(
    cur: Any, new: Any, path: str, out: list[str], ignore_top: tuple[str, ...] = ()
) -> None:
    if isinstance(cur, dict) and isinstance(new, dict):
        for key in sorted(set(cur) | set(new)):
            if not path and key in ignore_top:
                continue
            sub = f"{path}.{key}" if path else key
            if key not in cur or key not in new:
                out.append(sub)  # added or removed = changed (PUT replaces the object)
            else:
                _walk(cur[key], new[key], sub, out)
        return
    if cur != new:
        out.append(path)


def _allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    for entry in allowlist:
        if entry.endswith(".*"):
            root = entry[:-2]
            if path == root or path.startswith(root + "."):
                return True
        elif path == entry:
            return True
    return False


def screen_op(
    object_type: str, current: Mapping[str, Any], payload: Mapping[str, Any]
) -> Rejection | None:
    allowlist = RAW_ALLOWLIST.get(object_type, ())
    offending = [p for p in changed_paths(current, payload) if not _allowed(p, allowlist)]
    if offending:
        return Rejection(
            stage=_STAGE,
            reasons=tuple(
                f"out-of-scope raw path changed: {p} (not in the M1 allowlist)"
                for p in offending
            ),
        )
    return None
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/scope/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope tests/scope
git commit -m "Plan 3: field gate (raw changed-path pre-screen, removal-aware)"
```

---

### Task 7: `adapters/mist/validate/` — thin L0 against the committed OAS

**Files:**
- Modify: `pyproject.toml` (add `jsonschema` dependency)
- Create: `src/digital_twin/adapters/mist/validate/__init__.py`
- Create: `src/digital_twin/adapters/mist/validate/schema.py`
- Test: `tests/adapters/mist/test_validate_l0.py`

- [ ] **Step 1: Add the dependency**

Run: `uv add jsonschema && uv add --dev types-jsonschema`
Expected: both resolve; lockfile updated.

- [ ] **Step 2: Write the failing tests**

```python
# tests/adapters/mist/test_validate_l0.py
from digital_twin.adapters.mist.validate import L0Result, validate_payload
from digital_twin.contracts import FindingCategory, FindingSource, Severity


def test_clean_site_setting_payload_yields_no_findings():
    res = validate_payload(
        "site_setting",
        {"networks": {"corp": {"vlan_id": 10}},
         "port_usages": {"office": {"mode": "access", "port_network": "corp"}}},
    )
    assert isinstance(res, L0Result)
    assert res.findings == () and res.fatal is False


def test_type_violation_yields_error_finding_with_path():
    res = validate_payload("site_setting", {"networks": "not-an-object"})
    assert res.fatal is False
    assert len(res.findings) >= 1
    f = res.findings[0]
    assert f.severity is Severity.ERROR
    assert f.source is FindingSource.ADAPTER
    assert f.category is FindingCategory.OPERATIONAL  # payload trouble, not net breakage
    assert "networks" in str(f.evidence.get("path"))


def test_enum_violation_detected_on_device_payload():
    res = validate_payload(
        "device", {"port_config": {"ge-0/0/0": {"usage": "office", "duplex": "warp-speed"}}}
    )
    assert any("duplex" in str(f.evidence.get("path")) for f in res.findings)


def test_non_object_payload_is_fatal():
    res = validate_payload("site_setting", "just-a-string")  # type: ignore[arg-type]
    assert res.fatal is True and len(res.findings) == 1


def test_unknown_object_type_is_fatal():
    res = validate_payload("wlan", {})
    assert res.fatal is True
    assert "wlan" in res.findings[0].message
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/adapters/mist/test_validate_l0.py -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 4: Write the implementation**

```python
# src/digital_twin/adapters/mist/validate/schema.py
"""L0: thin structural payload validation against the COMMITTED Mist OAS.

Types, enums, required, and machine-readably encoded conditionals — exactly what
jsonschema can assert from the extracted schemas (OAS-only keywords like
`nullable` are unknown keywords to jsonschema and simply don't constrain).
Deterministic -> every finding is HIGH confidence, source=adapter,
category=operational (a payload Mist would reject is not network breakage).
`fatal` means the run cannot meaningfully continue (payload not an object /
no schema for the type) -> the engine short-circuits to UNKNOWN.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

import jsonschema

from digital_twin.adapters.mist.oas import load_schema
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import Confidence, ConfidenceLevel

_SCHEMA_FILES: dict[str, str] = {
    "site_setting": "site_setting.schema.json",
    "device": "device_switch.schema.json",
}
_MAX_FINDINGS = 50
_HIGH = Confidence(level=ConfidenceLevel.HIGH)


@dataclass(frozen=True)
class L0Result:
    findings: tuple[Finding, ...]
    fatal: bool  # structurally fatal -> engine short-circuits to UNKNOWN


def _finding(code: str, message: str, path: str = "") -> Finding:
    return Finding(
        source=FindingSource.ADAPTER,
        category=FindingCategory.OPERATIONAL,
        code=code,
        severity=Severity.ERROR,
        confidence=_HIGH,
        message=message,
        evidence={"path": path} if path else {},
    )


@cache
def _validator(object_type: str) -> jsonschema.Draft202012Validator:
    schema = load_schema(_SCHEMA_FILES[object_type])
    return jsonschema.Draft202012Validator(schema)


def validate_payload(object_type: str, payload: Mapping[str, Any]) -> L0Result:
    if object_type not in _SCHEMA_FILES:
        return L0Result(
            findings=(_finding("l0.schema.unknown_type",
                               f"no OAS schema for object_type {object_type!r}"),),
            fatal=True,
        )
    if not isinstance(payload, Mapping):
        return L0Result(
            findings=(_finding("l0.schema.not_an_object",
                               "payload must be a JSON object (full-object PUT body)"),),
            fatal=True,
        )
    findings = tuple(
        _finding(
            "l0.schema.violation",
            err.message,
            path=".".join(str(p) for p in err.absolute_path),
        )
        for _, err in zip(range(_MAX_FINDINGS), _validator(object_type).iter_errors(dict(payload)))
    )
    return L0Result(findings=findings, fatal=False)
```

```python
# src/digital_twin/adapters/mist/validate/__init__.py
"""L0 payload validation (vendor-specific, pre-IR). Reuses adapters/mist/oas."""

from .schema import L0Result, validate_payload

__all__ = ["L0Result", "validate_payload"]
```

- [ ] **Step 5: Run tests + quality gate**

Run: `uv run pytest tests/adapters/mist/test_validate_l0.py -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean. *Pinned facts (verified against the committed schemas):
`port_config.*.duplex` IS enum-constrained (`auto|full|half`); `site_setting` has NO
top-level `required`; `device_switch` requires top-level `type` — so the partial
device payload in the enum test yields an extra required-finding alongside the
duplex one, which is why that test asserts with `any(...)`, not an exact count.*

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/digital_twin/adapters/mist/validate tests/adapters/mist/test_validate_l0.py
git commit -m "Plan 3: thin L0 payload validation against committed Mist OAS (jsonschema)"
```

---

### Task 8: `adapters/mist/apply/objects.py` — per-object_type targeting

**Files:**
- Create: `src/digital_twin/adapters/mist/apply/__init__.py`
- Create: `src/digital_twin/adapters/mist/apply/objects.py`
- Test: `tests/adapters/mist/test_apply_objects.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/adapters/mist/test_apply_objects.py
from digital_twin.adapters.mist.apply.objects import get_object, replace_object
from tests.adapters.mist.fixtures import raw_site

RAW = raw_site()  # SWITCH_A (id "dev-a") + AP_1, setting=SITE_EFFECTIVE-shaped


def test_get_site_setting_returns_the_setting():
    obj = get_object(RAW, "site_setting", RAW.scope.site_id)
    assert obj is RAW.setting


def test_get_device_by_id():
    obj = get_object(RAW, "device", "dev-a")
    assert obj is not None and obj["mac"] == "aa0000000001"


def test_get_unknown_returns_none():
    assert get_object(RAW, "device", "ghost") is None
    assert get_object(RAW, "site_setting", "not-this-site") is None


def test_replace_site_setting_swaps_whole_object():
    new = replace_object(RAW, "site_setting", RAW.scope.site_id, {"networks": {"only": {"vlan_id": 9}}})
    assert new.setting["networks"] == {"only": {"vlan_id": 9}}
    assert "port_usages" not in new.setting  # full replacement, not a merge
    assert RAW.setting != new.setting  # original untouched (immutability)


def test_replace_device_preserves_identity_fields():
    # Mist PUT never lets a payload change server-managed identity; ingest needs mac/type
    new = replace_object(RAW, "device", "dev-a", {"name": "renamed", "mac": "evil", "port_config": {}})
    dev = next(d for d in new.devices if d.get("id") == "dev-a")
    assert dev["name"] == "renamed"
    assert dev["mac"] == "aa0000000001"  # identity preserved over payload's attempt
    assert dev["type"] == "switch"
    assert dev.get("other_ip_configs") is None or "other_ip_configs" not in dev  # replaced away


def test_replace_does_not_touch_other_devices():
    new = replace_object(RAW, "device", "dev-a", {"name": "x"})
    assert any(d.get("id") == "dev-ap1" and d.get("model") == "AP45" for d in new.devices)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adapters/mist/test_apply_objects.py -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/adapters/mist/apply/objects.py
"""Per-object_type targeting: find and replace ONE raw object in RawSiteState.

Replacement is wholesale (Mist PUT semantics) with ONE honesty exception:
server-managed identity fields are preserved from the current object — Mist
ignores attempts to change them, and downstream ingest needs mac/type/model.
RawSiteState is frozen; replacement returns a NEW state (dataclasses.replace).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace as dc_replace
from typing import Any

from digital_twin.providers.base import RawSiteState

_Json = Mapping[str, Any]

# Server-managed: preserved from the current object regardless of the payload.
IDENTITY_FIELDS: tuple[str, ...] = (
    "id",
    "org_id",
    "site_id",
    "mac",
    "serial",
    "model",
    "type",
    "created_time",
    "modified_time",
)


def get_object(raw: RawSiteState, object_type: str, object_id: str) -> _Json | None:
    if object_type == "site_setting":
        return raw.setting if object_id == raw.scope.site_id else None
    if object_type == "device":
        for dev in raw.devices:
            if str(dev.get("id")) == object_id:
                return dev
    return None


def _merged(current: _Json, payload: _Json) -> dict[str, Any]:
    new = dict(payload)
    for key in IDENTITY_FIELDS:
        if key in current:
            new[key] = current[key]
    return new


def replace_object(
    raw: RawSiteState, object_type: str, object_id: str, payload: _Json
) -> RawSiteState:
    """Caller must have resolved the object first (get_object is not None)."""
    if object_type == "site_setting":
        return dc_replace(raw, setting=_merged(raw.setting, payload))
    devices = tuple(
        _merged(dev, payload) if str(dev.get("id")) == object_id else dev
        for dev in raw.devices
    )
    return dc_replace(raw, devices=devices)
```

```python
# src/digital_twin/adapters/mist/apply/__init__.py
"""apply: raw + ordered ops -> raw' (in memory; never a Mist API write)."""

from .objects import IDENTITY_FIELDS, get_object, replace_object

__all__ = ["IDENTITY_FIELDS", "get_object", "replace_object"]
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/adapters/mist/test_apply_objects.py -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean. *Pinned: `raw_site()` uses `SiteScope(org_id="o1", site_id="s1")`
and `setting=SITE_EFFECTIVE`, so `get_object(RAW, "site_setting", RAW.scope.site_id)`
returns that exact object.*

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/apply tests/adapters/mist/test_apply_objects.py
git commit -m "Plan 3: apply targeting (full-object replace, identity preserved)"
```

---

### Task 9: `adapters/mist/apply/apply.py` — ordered rolling apply

**Files:**
- Create: `src/digital_twin/adapters/mist/apply/apply.py`
- Modify: `src/digital_twin/adapters/mist/apply/__init__.py`
- Test: `tests/adapters/mist/test_apply_plan.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/adapters/mist/test_apply_plan.py
from digital_twin.adapters.mist.apply import apply_plan
from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState
from tests.adapters.mist.fixtures import raw_site

RAW = raw_site()


def _op(object_type, object_id, payload, order=0):
    return ChangeOp(action="update", order=order, object_type=object_type,
                    object_id=object_id, payload=payload)


def test_single_op_applies():
    out = apply_plan(RAW, (_op("device", "dev-a", {"name": "new-name"}),))
    assert isinstance(out, RawSiteState)
    dev = next(d for d in out.devices if d.get("id") == "dev-a")
    assert dev["name"] == "new-name"


def test_ops_apply_in_order_value_not_list_position():
    ops = (
        _op("device", "dev-a", {"name": "second"}, order=5),
        _op("site_setting", RAW.scope.site_id, {"networks": {}}, order=1),
    )
    out = apply_plan(RAW, ops)
    assert isinstance(out, RawSiteState)
    assert next(d for d in out.devices if d.get("id") == "dev-a")["name"] == "second"
    assert out.setting.get("networks") == {}


def test_unknown_object_id_rejects_with_stage_apply():
    r = apply_plan(RAW, (_op("device", "ghost", {"name": "x"}),))
    assert isinstance(r, Rejection) and r.stage == "apply"
    assert any("ghost" in reason for reason in r.reasons)


def test_duplicate_order_rejects_defense_in_depth():
    ops = (_op("device", "dev-a", {}, order=1),
           _op("site_setting", RAW.scope.site_id, {}, order=1))
    assert isinstance(apply_plan(RAW, ops), Rejection)


def test_same_target_twice_rejects():
    ops = (_op("device", "dev-a", {"name": "a"}, order=0),
           _op("device", "dev-a", {"name": "b"}, order=1))
    r = apply_plan(RAW, ops)
    assert isinstance(r, Rejection)


def test_original_raw_never_mutated():
    before = next(d for d in RAW.devices if d.get("id") == "dev-a")["name"]
    apply_plan(RAW, (_op("device", "dev-a", {"name": "mutant"}),))
    assert next(d for d in RAW.devices if d.get("id") == "dev-a")["name"] == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adapters/mist/test_apply_plan.py -q`
Expected: FAIL — ImportError (apply_plan)

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/adapters/mist/apply/apply.py
"""apply_plan: ordered rolling full-object replacement (spec: Delta semantics).

Ops apply in strictly increasing `order` against a ROLLING raw state — op N sees
the state already modified by earlier ops. Static constraints (unique order, one
op per object) were checked by the envelope gate; they are re-checked here
cheaply (defense in depth — apply must be safe even if a future caller skips the
gates). Unknown target -> Rejection (errors are values)."""

from __future__ import annotations

from collections.abc import Sequence

from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState

from .objects import get_object, replace_object

_STAGE = "apply"


def apply_plan(raw: RawSiteState, ops: Sequence[ChangeOp]) -> RawSiteState | Rejection:
    orders = [op.order for op in ops]
    if len(set(orders)) != len(orders):
        return Rejection(stage=_STAGE, reasons=("duplicate op order values",))
    targets = [(op.object_type, op.object_id) for op in ops]
    if len(set(targets)) != len(targets):
        return Rejection(stage=_STAGE, reasons=("two ops target the same object",))

    state = raw
    for op in sorted(ops, key=lambda o: o.order):
        if get_object(state, op.object_type, op.object_id) is None:
            return Rejection(
                stage=_STAGE,
                reasons=(
                    f"ops[order={op.order}]: no {op.object_type} with id "
                    f"{op.object_id!r} in fetched state",
                ),
            )
        state = replace_object(state, op.object_type, op.object_id, op.payload)
    return state
```

```python
# src/digital_twin/adapters/mist/apply/__init__.py
"""apply: raw + ordered ops -> raw' (in memory; never a Mist API write)."""

from .apply import apply_plan
from .objects import IDENTITY_FIELDS, get_object, replace_object

__all__ = ["IDENTITY_FIELDS", "apply_plan", "get_object", "replace_object"]
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/adapters/mist/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/apply tests/adapters/mist/test_apply_plan.py
git commit -m "Plan 3: apply_plan (ordered rolling state, errors as values)"
```

---

### Task 10: `scope/derived_gate.py` — post-compile derived-impact gate

**Files:**
- Create: `src/digital_twin/scope/derived_gate.py`
- Test: `tests/scope/test_derived_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/scope/test_derived_gate.py
from digital_twin.contracts import Rejection
from digital_twin.scope.derived_gate import changed_effective_fields, check_derived

BASE = {
    "networks": {"corp": {"vlan_id": 10}},
    "port_usages": {"office": {"mode": "access"}},
    "vars": {"dhcp_ip": "10.0.0.2"},
    "dhcpd_config": {"corp": {"ip": "10.0.0.2"}},
}


def test_no_change_passes():
    assert check_derived(BASE, dict(BASE)) is None


def test_in_scope_effective_change_passes():
    prop = {**BASE, "networks": {"corp": {"vlan_id": 11}}}
    assert check_derived(BASE, prop) is None


def test_vars_ripple_into_out_of_scope_field_rejects():
    # the spec's headline case: a vars edit compiles into a dhcpd_config change
    prop = {**BASE, "vars": {"dhcp_ip": "10.9.9.9"},
            "dhcpd_config": {"corp": {"ip": "10.9.9.9"}}}
    r = check_derived(BASE, prop)
    assert isinstance(r, Rejection) and r.stage == "derived_gate"
    assert any("dhcpd_config" in reason for reason in r.reasons)
    # vars itself changing is fine — it's the allowed input
    assert not any(reason.startswith("vars") for reason in r.reasons)


def test_out_of_scope_field_appearing_rejects():
    prop = {**BASE, "radius_config": {"servers": []}}
    assert isinstance(check_derived(BASE, prop), Rejection)


def test_changed_effective_fields_lists_top_level_only():
    prop = {**BASE, "networks": {"corp": {"vlan_id": 11}}, "extra": 1}
    assert changed_effective_fields(BASE, prop) == ("extra", "networks")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scope/test_derived_gate.py -q`
Expected: FAIL — ImportError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/scope/derived_gate.py
"""Post-compile derived-impact gate: diff the FULL effective configs.

The IR is a projection of in-scope fields only, so an out-of-scope effective
change (e.g. a vars edit rippling into dhcpd_config) NEVER enters the IR and
IRDiff cannot see it. This gate diffs the compiler's full effective output
(baseline vs proposed) — site effective AND each device effective — and rejects
if any field OUTSIDE the effective allowlist differs. Both sides come from the
identical compiler code path, so plain equality is sound (no normalization
needed — same code, same shapes)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.contracts import Rejection
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST

_STAGE = "derived_gate"


def changed_effective_fields(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any]
) -> tuple[str, ...]:
    keys = set(baseline) | set(proposed)
    return tuple(sorted(k for k in keys if baseline.get(k) != proposed.get(k)))


def check_derived(
    baseline: Mapping[str, Any], proposed: Mapping[str, Any], *, artifact: str = "site"
) -> Rejection | None:
    offending = [
        field
        for field in changed_effective_fields(baseline, proposed)
        if field not in EFFECTIVE_ALLOWLIST
    ]
    if offending:
        return Rejection(
            stage=_STAGE,
            reasons=tuple(
                f"{field}: out-of-scope EFFECTIVE field differs in {artifact} config "
                "(change ripples beyond the M1 model)"
                for field in offending
            ),
        )
    return None
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/scope/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope tests/scope
git commit -m "Plan 3: derived-impact gate (full effective config, default-deny)"
```

---

### Task 11: `adapters/base.py` protocol + `adapters/mist/adapter.py` facade

**Files:**
- Create: `src/digital_twin/adapters/base.py`
- Create: `src/digital_twin/adapters/mist/adapter.py`
- Test: `tests/adapters/mist/test_adapter_facade.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/adapters/mist/test_adapter_facade.py
from digital_twin.adapters.base import VendorAdapter
from digital_twin.adapters.mist.adapter import IngestOutcome, MistAdapter
from digital_twin.contracts import ChangeOp
from digital_twin.providers.base import RawSiteState
from tests.adapters.mist.fixtures import raw_site


def test_mist_adapter_satisfies_protocol():
    adapter: VendorAdapter = MistAdapter()
    assert adapter is not None


def test_ingest_compiles_and_builds_ir():
    out = MistAdapter().ingest(raw_site())
    assert isinstance(out, IngestOutcome)
    assert out.report.ok
    assert out.ir is not None and len(out.ir.devices) >= 2  # SWITCH_A + AP_1
    assert "networks" in out.site_effective
    # device_effective keyed by canonical device id (mac-derived)
    assert any(k.startswith("aa0000000001") for k in out.device_effective)


def test_validate_delegates_to_l0():
    res = MistAdapter().validate(
        ChangeOp(action="update", order=0, object_type="site_setting",
                 object_id="s1", payload={"networks": "bad"})
    )
    assert res.findings  # L0 caught the type violation


def test_apply_delegates_to_apply_plan():
    raw = raw_site()
    out = MistAdapter().apply(
        raw, (ChangeOp(action="update", order=0, object_type="device",
                       object_id="dev-a", payload={"name": "via-facade"}),)
    )
    assert isinstance(out, RawSiteState)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adapters/mist/test_adapter_facade.py -q`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write the implementation**

```python
# src/digital_twin/adapters/base.py
"""VendorAdapter: the seam a vendor must fill — validate (L0), ingest, apply.

One adapter per vendor; ChangePlan.source selects it. Everything is errors-as-
values: validate returns findings, ingest returns a report (crash-isolated),
apply returns Rejection on bad targets."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.providers.base import RawSiteState


class VendorAdapter(Protocol):
    def validate(self, op: ChangeOp) -> Any: ...  # vendor L0 result (findings + fatal)

    def ingest(self, raw: RawSiteState) -> Any: ...  # effective configs + IR + report

    def apply(
        self, raw: RawSiteState, ops: Sequence[ChangeOp]
    ) -> RawSiteState | Rejection: ...
```

```python
# src/digital_twin/adapters/mist/adapter.py
"""MistAdapter: the thin FACADE — wires validate/, compile/, ingest/, apply/.

No business logic here. ingest() runs the Plan-2 chain (compile_site +
compile_device per switch + ingester registry) and returns BOTH artifacts the
spec requires from compile: the full effective configs (derived gate's input)
and the IR projection (checks' input). ir is None when ingest failed —
IngestReport carries the names; the engine maps that to UNKNOWN."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from digital_twin.adapters.mist.apply import apply_plan
from digital_twin.adapters.mist.compile.switch import compile_device, compile_site
from digital_twin.adapters.mist.ingest.base import IngestContext, Ingester
from digital_twin.adapters.mist.ingest.clients import ClientsIngester
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.registry import IngesterRegistry, IngestReport
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.adapters.mist.validate import L0Result, validate_payload
from digital_twin.contracts import ChangeOp, Rejection
from digital_twin.ir import IR, IRBuilder, device_id
from digital_twin.providers.base import RawSiteState

_Json = dict[str, Any]


@dataclass(frozen=True)
class IngestOutcome:
    ir: IR | None  # None when report.ok is False (diagnostic-only builder state)
    site_effective: _Json
    device_effective: dict[str, _Json]
    report: IngestReport


class MistAdapter:
    def __init__(self, ingesters: list[Ingester] | None = None) -> None:
        self._registry = IngesterRegistry(
            ingesters if ingesters is not None
            else [SwitchIngester(), LldpIngester(), ClientsIngester()]
        )

    def validate(self, op: ChangeOp) -> L0Result:
        return validate_payload(op.object_type, op.payload)

    def ingest(self, raw: RawSiteState) -> IngestOutcome:
        nt = dict(raw.networktemplate) if raw.networktemplate else None
        setting = dict(raw.setting)
        site_effective = compile_site(nt, setting)
        device_effective = {
            device_id(str(d["mac"])): compile_device(nt, setting, dict(d))
            for d in raw.devices
            if d.get("type") == "switch" and d.get("mac")
        }
        builder = IRBuilder()
        report = self._registry.run(
            IngestContext(
                raw=raw,
                site_effective=site_effective,
                device_effective=device_effective,
                builder=builder,
            )
        )
        ir = builder.build() if report.ok else None
        return IngestOutcome(
            ir=ir,
            site_effective=site_effective,
            device_effective=device_effective,
            report=report,
        )

    def apply(
        self, raw: RawSiteState, ops: Sequence[ChangeOp]
    ) -> RawSiteState | Rejection:
        return apply_plan(raw, ops)
```

- [ ] **Step 4: Run tests + quality gate**

Run: `uv run pytest tests/adapters/mist/ -q && uv run ruff check . && uv run mypy`
Expected: PASS, clean. *`compile_device` is invoked with switch payloads from the
fixtures; if `raw_site()` devices lack `mac`/`type`, check `tests/adapters/mist/fixtures.py`
— SWITCH_A has both.*

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/base.py src/digital_twin/adapters/mist/adapter.py tests/adapters/mist/test_adapter_facade.py
git commit -m "Plan 3: VendorAdapter protocol + MistAdapter facade"
```

---

### Task 12: End-to-end Plan-3 slice test (gates precede apply)

**Files:**
- Test: `tests/test_plan3_flow.py`

- [ ] **Step 1: Write the integration test (this is the deliverable — it should pass immediately if Tasks 1–11 are correct; any failure is a real wiring bug)**

```python
# tests/test_plan3_flow.py
"""Plan-3 slice: envelope -> object gate -> field gate -> L0 -> apply -> compile
-> derived gate, in pipeline order, on synthetic Mist-shaped state.

The engine that sequences these is Plan 5; this test IS the sequence, proving
the pieces compose and that gates fire at the right stage for each scenario."""

from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.contracts import ChangePlan, Rejection
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects

SITE = "s1"
SETTING = {
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {
        "office": {"mode": "access", "port_network": "corp"},
        "uplink": {"mode": "trunk", "port_network": "corp", "networks": ["voice"]},
    },
    "vars": {"dhcp_ip": "10.0.0.2"},
    "dhcpd_config": {"corp": {"ip": "{{dhcp_ip}}"}},
}
SWITCH = {
    "mac": "aa0000000001", "id": "dev-a", "type": "switch", "model": "EX4100-48P",
    "name": "sw-a", "port_config": {"ge-0/0/0-1": {"usage": "office"}},
}


def _raw() -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id=SITE),
        site={"id": SITE},
        setting=SETTING,
        networktemplate=None,
        devices=(SWITCH,),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=("devices",),
                       failures=()),
    )


def _pipeline(plan_dict):
    """The Plan-3 slice in spec pipeline order; returns ('ok', ir_pair) or the Rejection."""
    plan = parse_change_plan(plan_dict)
    if isinstance(plan, Rejection):
        return plan
    rejection = check_objects(plan)
    if rejection:
        return rejection
    adapter, raw = MistAdapter(), _raw()
    for op in sorted(plan.ops, key=lambda o: o.order):  # rolling pre-op state
        from digital_twin.adapters.mist.apply import get_object

        current = get_object(raw, op.object_type, op.object_id)
        if current is None:
            return Rejection(stage="apply", reasons=(f"unknown {op.object_type}",))
        rejection = screen_op(op.object_type, current, op.payload)
        if rejection:
            return rejection
        l0 = adapter.validate(op)
        if l0.fatal:
            return Rejection(stage="l0", reasons=tuple(f.message for f in l0.findings))
        raw = adapter.apply(raw, (op,))
        assert isinstance(raw, RawSiteState)
    baseline, proposed = adapter.ingest(_raw()), adapter.ingest(raw)
    rejection = check_derived(baseline.site_effective, proposed.site_effective)
    if rejection:
        return rejection
    for did, base_eff in baseline.device_effective.items():
        rejection = check_derived(
            base_eff, proposed.device_effective.get(did, {}), artifact=f"device {did}"
        )
        if rejection:
            return rejection
    return ("ok", (baseline.ir, proposed.ir))


def _plan(ops):
    return {"source": "mist", "scope": {"org_id": "o1", "site_id": SITE}, "ops": ops}


def test_in_scope_site_setting_change_passes_all_gates():
    new_setting = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    result = _pipeline(_plan([{"action": "update", "order": 0,
                               "object_type": "site_setting", "object_id": SITE,
                               "payload": new_setting}]))
    assert isinstance(result, tuple) and result[0] == "ok"
    baseline_ir, proposed_ir = result[1]
    assert {v.vlan_id for v in baseline_ir.vlans} == {10, 30}
    assert {v.vlan_id for v in proposed_ir.vlans} == {10, 31}  # the change reached the IR


def test_out_of_scope_raw_path_stops_at_field_gate():
    new_setting = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    result = _pipeline(_plan([{"action": "update", "order": 0,
                               "object_type": "site_setting", "object_id": SITE,
                               "payload": new_setting}]))
    assert isinstance(result, Rejection) and result.stage == "field_gate"


def test_vars_ripple_stops_at_derived_gate():
    # raw change touches only vars.* (allowed) — but compiles into dhcpd_config
    new_setting = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    result = _pipeline(_plan([{"action": "update", "order": 0,
                               "object_type": "site_setting", "object_id": SITE,
                               "payload": new_setting}]))
    assert isinstance(result, Rejection) and result.stage == "derived_gate"
    assert any("dhcpd_config" in reason for reason in result.reasons)


def test_template_op_stops_at_object_gate():
    result = _pipeline(_plan([{"action": "update", "order": 0,
                               "object_type": "networktemplate", "object_id": "nt1",
                               "payload": {}}]))
    assert isinstance(result, Rejection) and result.stage == "object_gate"


def test_malformed_envelope_stops_at_envelope():
    result = _pipeline({"source": "mist", "scope": {"org_id": "o1"}, "ops": "nope"})
    assert isinstance(result, Rejection) and result.stage == "envelope"


def test_device_port_change_flows_through_to_proposed_ir():
    new_device = {**SWITCH, "port_config": {"ge-0/0/0-1": {"usage": "uplink"}}}
    result = _pipeline(_plan([{"action": "update", "order": 0,
                               "object_type": "device", "object_id": "dev-a",
                               "payload": new_device}]))
    assert isinstance(result, tuple)
    _, (baseline_ir, proposed_ir) = result
    port = proposed_ir.port("aa0000000001:ge-0/0/0")
    assert port.profile == "uplink"
    assert baseline_ir.port("aa0000000001:ge-0/0/0").profile == "office"
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_plan3_flow.py -q`
Expected: PASS. *If `ir.port(...)`/`ir.vlans` accessor names differ, check
`src/digital_twin/ir/model.py` — the lldp tests use `ir.port("...")` and `ir.links`,
and the switch tests use `ir.vlans`; mirror whatever they do.*

- [ ] **Step 3: Run the FULL quality gate**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q`
Expected: all clean, all tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_plan3_flow.py
git commit -m "Plan 3: end-to-end slice test (gates precede apply, ripple caught)"
```

---

### Task 13: Public API surface + plan sync

**Files:**
- Modify: `tests/test_public_api.py`
- Modify: `docs/superpowers/plans/2026-06-09-network-digital-twin-scope-l0-apply.md` (check boxes)

- [ ] **Step 1: Extend the public-API test**

Add to `tests/test_public_api.py` (follow the existing function style in that file):

```python
def test_plan3_public_api():
    from digital_twin.adapters.base import VendorAdapter
    from digital_twin.adapters.mist.adapter import IngestOutcome, MistAdapter
    from digital_twin.adapters.mist.apply import apply_plan, get_object, replace_object
    from digital_twin.adapters.mist.validate import L0Result, validate_payload
    from digital_twin.contracts import (
        ChangeOp,
        ChangePlan,
        ChangeScope,
        Finding,
        FindingCategory,
        FindingSource,
        Rejection,
        Severity,
    )
    from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST, RAW_ALLOWLIST
    from digital_twin.scope.derived_gate import check_derived
    from digital_twin.scope.envelope import parse_change_plan
    from digital_twin.scope.field_gate import screen_op
    from digital_twin.scope.object_gate import check_objects

    assert all(callable(f) for f in (parse_change_plan, check_objects, screen_op,
                                     check_derived, validate_payload, apply_plan,
                                     get_object, replace_object))
    assert all(x is not None for x in (VendorAdapter, MistAdapter, IngestOutcome,
                                       L0Result, ChangeOp, ChangePlan, ChangeScope,
                                       Finding, FindingCategory, FindingSource,
                                       Rejection, Severity,
                                       EFFECTIVE_ALLOWLIST, RAW_ALLOWLIST))
```

- [ ] **Step 2: Full gate**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q`
Expected: all clean

- [ ] **Step 3: Mark all checkboxes in this plan document, then commit**

```bash
git add tests/test_public_api.py docs/superpowers/plans/2026-06-09-network-digital-twin-scope-l0-apply.md
git commit -m "Plan 3: public API surface + plan doc synced"
```

---

## Acceptance (Plan 3 exit)

1. `uv run pytest -q` — every test green (existing 200+ plus the new contracts/scope/validate/apply/integration suites).
2. `uv run ruff check .` + `uv run mypy` — clean (strict).
3. The end-to-end slice test proves, on synthetic state: in-scope changes flow envelope→…→derived-gate→IR′; out-of-scope raw paths stop at the **field gate**; vars ripple stops at the **derived gate**; template ops stop at the **object gate**; gates always run **before** apply.
4. No live/API code added — Plan 3 is pure (the only I/O is reading committed OAS files).

**Explicitly deferred (per spec, not this plan):** the 10-stage `engine/pipeline.py` (Plan 5), checks/analysis/verdict + decision mapping of `Rejection`→UNKNOWN (Plan 4), drivers/observability (Plan 5), post-fetch device-ROLE check wiring (the gate exists per-op; the role lookup needs the pipeline's fetched-state context — Plan 5 wiring).
