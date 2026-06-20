# Config-lint tier (GS30–GS33) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four single-state config-lint checks (VLAN-id collision, IP-subnet overlap, duplicate SSID, open-guest-without-isolation) that detect on the proposed IR but contribute **delta-conditioned** (introduced→WARNING, pre-existing→INFO), plus the IR + apply-pipeline work that makes WLAN changes simulable.

**Architecture:** A new typed `Wlan` IR entity + a `Vlan.collisions` field carry the two facts the IR lacks today (subnets already exist); both join `diff_ir` so the checks get baseline-vs-proposed conditioning for free. WLAN becomes a simulable site object (object_gate + allowlist + L0 schema + apply). Each check follows the existing `native_mismatch` shape via one shared `run_delta_lint` helper.

**Tech Stack:** Python 3.14, uv, pytest/ruff/mypy. Spec: `docs/superpowers/specs/2026-06-20-config-lint-tier-design.md`.

**Gate (run after every task):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

---

## Reference: invariants (do not violate)

- **Delta-conditioned**: introduced/worsened → `WARNING`; pre-existing (same violation key in baseline) → `INFO` context (never floors an unrelated change). Key carries the violation FACTS (so a changed violation reads as introduced).
- **Never false-positive on unknowns**: skip unresolved/unparseable/unverifiable items; emit a coverage note ONLY when the skipped item is delta-touched or a WARNING's correctness depends on it (relevance-scoped PARTIAL — PARTIAL floors to REVIEW).
- **`inherited` is fail-closed and NOT diff-bearing** (in `_IGNORED_BY_KIND["wlan"]`).

---

## Phase 1 — `Wlan` IR entity + ingest + diff

### Task 1: `Wlan` entity + `IR.wlans` + builder + diff kind

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (add `Wlan` after `Client`/`ClientEnrichment`)
- Modify: `src/digital_twin/ir/__init__.py` (export `Wlan`)
- Modify: `src/digital_twin/ir/model.py` (IR field + builder)
- Modify: `src/digital_twin/ir/diff.py` (`_ENTITY_KINDS` + `_IGNORED_BY_KIND`)
- Test: `tests/ir/test_wlan_ir.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/ir/test_wlan_ir.py
from digital_twin.ir import Wlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(*wlans: Wlan):
    b = IRBuilder()
    for w in wlans:
        b.add_wlan(w)
    return b.build()


def test_builder_exposes_wlans():
    ir = _ir(Wlan(id="w1", ssid="corp", enabled=True))
    assert ir.wlans[0].ssid == "corp" and ir.wlans[0].id == "w1"


def test_modeled_change_diffs():
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, isolation=False))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, isolation=True))
    assert diff_ir(base, prop).touches("wlan")


def test_inherited_only_flip_does_not_diff():
    # ownership is NOT a lint fact -> must not fire wlan checks
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, inherited=True))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, inherited=False))
    assert not diff_ir(base, prop).touches("wlan")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/ir/test_wlan_ir.py -v` — FAIL (`ImportError: cannot import name 'Wlan'`).

- [ ] **Step 3: Add the `Wlan` entity** in `src/digital_twin/ir/entities.py`, after the `ClientEnrichment` class:

```python
@dataclass(frozen=True)
class Wlan:
    """A site's effective WLAN (from the derived WLAN list), modeled for the
    config-lint checks. Secret-free by construction. `inherited` = org-template
    owned (NOT site-writable); it is observational ownership, not a lint fact."""

    id: str            # provider WLAN id (pragmatic identity: rename => modify)
    ssid: str
    enabled: bool = False
    auth_type: str | None = None     # auth.type ("open"|"psk"|"eap"|…); None = unparsed
    isolation: bool = False          # isolation OR l2_isolation
    apply_to: str | None = None      # "site" | "aps" | "wxtags" | None
    ap_ids: tuple[str, ...] = ()     # sorted+deduped explicit AP scope
    wxtag_ids: tuple[str, ...] = ()  # sorted+deduped
    inherited: bool = False          # True = org-template-owned (fail-closed at ingest)
    meta: FactMeta = CONFIG_META
```

`FactMeta`/`CONFIG_META` are already imported in entities.py.

- [ ] **Step 4: Export it** in `src/digital_twin/ir/__init__.py`: add `Wlan` to the `from .entities import (...)` block and to `__all__` (keep alphabetical/existing ordering for ruff).

- [ ] **Step 5: IR field + builder** in `src/digital_twin/ir/model.py`:
  - extend the `from .entities import (...)` block with `Wlan,`.
  - add the IR field after `clients: tuple[Client, ...]` (keep with the other tuple entities):
    ```python
    wlans: tuple[Wlan, ...] = ()
    ```
  - in `IRBuilder.__init__` (with the other list inits):
    ```python
    self._wlans: list[Wlan] = []
    ```
  - add the builder method (near `add_client`):
    ```python
    def add_wlan(self, wlan: Wlan) -> IRBuilder:
        self._wlans.append(wlan)
        return self
    ```
  - wire into `build()`'s `IR(...)`:
    ```python
    wlans=tuple(self._wlans),
    ```
  (No `_validate_*` for wlans — observational; a bad WLAN must never fail the build.)

- [ ] **Step 6: diff kind + ignored field** in `src/digital_twin/ir/diff.py`:
  - append to `_ENTITY_KINDS`:
    ```python
    ("wlan", lambda ir: ir.wlans),
    ```
  - extend `_IGNORED_BY_KIND` so an ownership-only flip doesn't diff:
    ```python
    _IGNORED_BY_KIND: dict[str, frozenset[str]] = {
        "device": frozenset({"name"}),
        "wlan": frozenset({"inherited"}),
    }
    ```

- [ ] **Step 7: Run + gate**

Run: `uv run pytest tests/ir/test_wlan_ir.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .` — all green.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/ir/entities.py src/digital_twin/ir/__init__.py src/digital_twin/ir/model.py src/digital_twin/ir/diff.py tests/ir/test_wlan_ir.py
git commit -m "feat(lint): Wlan IR entity + diff kind (inherited not diff-bearing)"
```

### Task 2: mint `Wlan` from all WLAN rows in `WlanIngester`

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/wlan.py`
- Test: `tests/adapters/mist/test_wlan_mint.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/test_wlan_mint.py
from digital_twin.adapters.mist.ingest.wlan import _mint_wlan


def test_mints_disabled_open_unisolated_inherited():
    w = _mint_wlan({"id": "w1", "ssid": "guest", "enabled": False,
                    "auth": {"type": "open"}, "l2_isolation": True,
                    "apply_to": "site", "for_site": False, "template_id": "t1"})
    assert w.ssid == "guest" and w.enabled is False
    assert w.auth_type == "open" and w.isolation is True   # via l2_isolation
    assert w.apply_to == "site" and w.inherited is True     # template-owned


def test_site_owned_and_scope_normalization():
    w = _mint_wlan({"id": "w2", "ssid": "corp", "enabled": True, "for_site": True,
                    "apply_to": "aps", "ap_ids": ["b", "a", "a"]})
    assert w.inherited is False                            # positively site-owned
    assert w.ap_ids == ("a", "b")                          # sorted+deduped
    assert w.isolation is False and w.auth_type is None     # absent -> defaults


def test_ambiguous_ownership_is_inherited_fail_closed():
    # no for_site, no template_id -> cannot confirm site-owned -> inherited
    assert _mint_wlan({"id": "w3", "ssid": "x"}).inherited is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_wlan_mint.py -v` — FAIL (`cannot import name '_mint_wlan'`).

- [ ] **Step 3: Add `_mint_wlan` + mint in the ingester.** In `src/digital_twin/adapters/mist/ingest/wlan.py`, add the import for `Wlan` (with the other `digital_twin.ir` imports) and the helper + minting:

```python
def _mint_wlan(row: Mapping[str, Any]) -> Wlan:
    auth = row.get("auth") or {}
    auth_type = auth.get("type")
    return Wlan(
        id=str(row.get("id", "")),
        ssid=str(row.get("ssid", "")),
        enabled=bool(row.get("enabled")),
        auth_type=str(auth_type) if auth_type is not None else None,
        isolation=bool(row.get("isolation")) or bool(row.get("l2_isolation")),
        apply_to=str(row["apply_to"]) if row.get("apply_to") is not None else None,
        ap_ids=tuple(sorted({str(x) for x in (row.get("ap_ids") or [])})),
        wxtag_ids=tuple(sorted({str(x) for x in (row.get("wxtag_ids") or [])})),
        # fail-closed: site-writable ONLY when positively site-owned
        inherited=not (row.get("for_site") is True and not row.get("template_id")),
    )
```

Then inside `WlanIngester.ingest`, after the existing `ap_required_vlans` handling and BEFORE `return frozenset({IRCapability.WLAN_CONFIG})`, mint one `Wlan` per row that has an id:

```python
        for row in ctx.raw.wlans:
            if row.get("id"):
                ctx.builder.add_wlan(_mint_wlan(row))
```

Add the needed imports at the top of the file if missing: `from collections.abc import Mapping`, `from typing import Any`, and `Wlan` from `digital_twin.ir`.

- [ ] **Step 4: Run + gate**

Run: `uv run pytest tests/adapters/mist/test_wlan_mint.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .` — all green (existing wlan-ingest tests still pass; minting is additive).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/wlan.py tests/adapters/mist/test_wlan_mint.py
git commit -m "feat(lint): mint Wlan from all derived WLAN rows (incl disabled; fail-closed inherited)"
```

---

## Phase 2 — `Vlan.collisions`

### Task 3: record VLAN-id collisions at the `_vlans` dedup

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (add `Vlan.collisions`)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_vlans`)
- Test: `tests/adapters/mist/test_vlan_collisions.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/test_vlan_collisions.py
from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from datetime import UTC, datetime


def _raw(networks: dict) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"},
        setting={"networks": networks}, networktemplate=None, devices=(), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t",
                       fetched=("site", "setting", "devices"), failures=()),
    )


def _vlan(ir, vid):
    return ir.vlans[vid]


def test_two_names_same_vlan_id_records_collision():
    ir = MistAdapter().ingest(_raw({
        "corp": {"vlan_id": 10}, "guest": {"vlan_id": 10}, "iot": {"vlan_id": 30},
    })).ir
    assert _vlan(ir, 10).collisions == ("guest",)   # distinct OTHER name (winner=corp)
    assert _vlan(ir, 30).collisions == ()           # no collision


def test_repeated_same_name_is_not_a_collision():
    # the same logical network legitimately repeats across effective sources
    ir = MistAdapter().ingest(_raw({"corp": {"vlan_id": 10}})).ir
    assert _vlan(ir, 10).collisions == ()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_vlan_collisions.py -v` — FAIL (`Vlan` has no attribute `collisions`).

- [ ] **Step 3: Add the field** in `src/digital_twin/ir/entities.py`, on the `Vlan` dataclass after `dhcp_sources`:

```python
    dhcp_sources: tuple[str, ...] = ()
    # GS30: distinct OTHER network names that also claim this vlan_id (the dedup
    # keeps the first; this surfaces the shadowed claimants). () = no collision.
    collisions: tuple[str, ...] = ()
    meta: FactMeta = CONFIG_META
```

- [ ] **Step 4: Record collisions** in `src/digital_twin/adapters/mist/ingest/switch.py:_vlans`. Build a per-vid name list while iterating sources, then pass distinct-other names to the mint. Add, right after `subnet_rows_by_vid` is populated (after the loop ending ~line 436):

```python
        names_by_vid: dict[int, list[str]] = {}
        for eff in sources:
            for name, net in (eff.get("networks") or {}).items():
                vid = _vlan_int(net.get("vlan_id"))
                if vid is not None:
                    names_by_vid.setdefault(vid, []).append(name)
```

Then in the mint loop, compute `collisions` for the winning `name` and pass it to `Vlan(...)`:

```python
                if vid is not None and vid not in seen:
                    seen.add(vid)
                    gw, gw_unresolved = _vlan_gateway(vid, gw_rows_by_vid, org_gw_raw)
                    subnet, subnet_unresolved = _winning_literal(
                        vid, subnet_rows_by_vid, org_subnet_raw,
                        parse=_literal_subnet, same=same_subnet,
                    )
                    collisions = tuple(sorted(set(names_by_vid.get(vid, [])) - {name}))
                    ctx.builder.add_vlan(
                        Vlan(
                            vlan_id=vid,
                            name=name,
                            scope=ctx.raw.scope.site_id,
                            subnet=subnet,
                            subnet_unresolved=subnet_unresolved,
                            gateway=gw,
                            gateway_unresolved=gw_unresolved,
                            dhcp_sources=tuple(sorted(dhcp_sources.get(vid, ()))),
                            collisions=collisions,
                        )
                    )
```

- [ ] **Step 5: Run + gate**

Run: `uv run pytest tests/adapters/mist/test_vlan_collisions.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .` — all green.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/switch.py tests/adapters/mist/test_vlan_collisions.py
git commit -m "feat(lint): record Vlan.collisions (distinct other names) at the _vlans dedup"
```

---

## Phase 3 — WLAN as a simulable site object

### Task 4: object_gate + allowlist + apply + inherited screen for `wlan`

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py` (`SUPPORTED_OBJECT_TYPES`, `RAW_ALLOWLIST["wlan"]`)
- Modify: `src/digital_twin/adapters/mist/apply/objects.py` (`get_object`, `replace_object`)
- Modify: `src/digital_twin/scope/field_gate.py` (`screen_op` inherited rejection)
- Test: `tests/scope/test_wlan_object.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/scope/test_wlan_object.py
from datetime import UTC, datetime

from digital_twin.adapters.mist.apply.objects import effective_update, get_object, replace_object
from digital_twin.contracts import ChangeOp, ChangePlan, ChangeScope, Rejection
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.scope.field_gate import screen_op
from digital_twin.scope.object_gate import check_objects

_SITE = {"id": "w1", "ssid": "corp", "enabled": True, "for_site": True, "isolation": False}
_INHERITED = {"id": "w2", "ssid": "guest", "enabled": True, "for_site": False, "template_id": "t1"}


def _raw() -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"}, setting={},
        networktemplate=None, devices=(), device_stats=(), port_stats=(),
        wireless_clients=(), wired_clients=(), derived_setting=None, wlans=(_SITE, _INHERITED),
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
    )


def _op(object_id, payload):
    return ChangeOp(action="update", order=0, object_type="wlan",
                    object_id=object_id, payload=payload)


def test_object_gate_accepts_wlan():
    plan = ChangePlan(source="mist", scope=ChangeScope(org_id="o1", site_id="s1"),
                      ops=(_op("w1", {"isolation": True}),))
    assert check_objects(plan) is None


def test_get_and_replace_target_raw_wlans_by_id():
    raw = _raw()
    assert get_object(raw, "wlan", "w1")["ssid"] == "corp"
    out = replace_object(raw, "wlan", "w1", {"isolation": True})
    assert next(w for w in out.wlans if w["id"] == "w1")["isolation"] is True
    assert out.devices == ()  # device branch not taken (no fall-through)


def test_field_gate_modeled_leaf_passes_unmodeled_rejects():
    # the engine passes the EFFECTIVE object (effective_update) to screen_op, not the
    # partial payload — a partial dict would read every other root as a deletion.
    assert screen_op("wlan", _SITE, effective_update(_SITE, {"isolation": True})) is None
    r = screen_op("wlan", _SITE, effective_update(_SITE, {"hide_ssid": True}))   # unmodeled
    assert isinstance(r, Rejection)


def test_inherited_wlan_op_rejected_post_fetch():
    r = screen_op("wlan", _INHERITED, effective_update(_INHERITED, {"isolation": True}))
    assert isinstance(r, Rejection) and any("inherited" in x for x in r.reasons)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/scope/test_wlan_object.py -v` — FAIL (object_gate rejects wlan; get_object returns None).

- [ ] **Step 3: allowlist** in `src/digital_twin/scope/allowlist.py`:
  - add `"wlan"` to `SUPPORTED_OBJECT_TYPES`:
    ```python
    SUPPORTED_OBJECT_TYPES: tuple[str, ...] = ("site_setting", "device", "wlan")
    ```
  - add the WLAN leaf tuple + allowlist entry (after the `RAW_ALLOWLIST` dict literal, near the networktemplate/gatewaytemplate assignments). `ap_ids`/`wxtag_ids` are WHOLE-LIST (atomic) leaves — `changed_leaf_paths` treats lists atomically:
    ```python
    # Modeled WLAN leaves (exactly what _mint_wlan consumes). ap_ids/wxtag_ids are
    # atomic list leaves (NOT ap_ids.* — the path flattener treats lists atomically).
    _WLAN_LEAVES: tuple[str, ...] = (
        "ssid", "enabled", "auth.type", "isolation", "l2_isolation",
        "apply_to", "ap_ids", "wxtag_ids",
    )
    RAW_ALLOWLIST["wlan"] = _WLAN_LEAVES
    ```

- [ ] **Step 4: apply** in `src/digital_twin/adapters/mist/apply/objects.py`:
  - in `get_object`, add a `wlan` branch before the final `return None`:
    ```python
        if object_type == "wlan":
            for w in raw.wlans:
                if str(w.get("id")) == object_id:
                    return w
            return None
    ```
  - in `replace_object`, branch EXPLICITLY (no device fall-through):
    ```python
    def replace_object(
        raw: RawSiteState, object_type: str, object_id: str, payload: _Json
    ) -> RawSiteState:
        """Caller must have resolved the object first (get_object is not None)."""
        if object_type == "site_setting":
            return dc_replace(raw, setting=effective_update(raw.setting, payload))
        if object_type == "wlan":
            wlans = tuple(
                effective_update(w, payload) if str(w.get("id")) == object_id else w
                for w in raw.wlans
            )
            return dc_replace(raw, wlans=wlans)
        devices = tuple(
            effective_update(dev, payload) if str(dev.get("id")) == object_id else dev
            for dev in raw.devices
        )
        return dc_replace(raw, devices=devices)
    ```

- [ ] **Step 5: inherited screen** in `src/digital_twin/scope/field_gate.py:screen_op`, add a `wlan` ownership check alongside the existing device-role check (before the allowlist scan):

```python
    if object_type == "wlan" and not (
        current.get("for_site") is True and not current.get("template_id")
    ):
        return Rejection(
            stage=_STAGE,
            reasons=(
                f"WLAN {current.get('id')!r} is inherited from an org wlantemplate "
                "(not a site-writable object) — simulate the change at the org/template level",
            ),
        )
```

- [ ] **Step 6: Update existing tests that pinned `wlan` as unsupported.** Adding `"wlan"` to `SUPPORTED_OBJECT_TYPES` breaks two pre-existing assertions:
  - `tests/scope/test_allowlist.py` — update the M1-pair assertion:
    ```python
    def test_supported_object_types_are_the_m1_pair():
        assert SUPPORTED_OBJECT_TYPES == ("site_setting", "device", "wlan")
    ```
  - `tests/scope/test_object_gate.py::test_all_offending_ops_reported` — `wlan` is now supported, so swap it for another genuinely-unsupported type to keep two offending ops:
    ```python
    def test_all_offending_ops_reported():
        plan = _plan(
            [
                _op(object_type="wxtag", object_id="x1", order=0),
                _op(object_type="rftemplate", object_id="r1", order=1),
            ]
        )
        r = check_objects(plan)
        assert isinstance(r, Rejection) and len(r.reasons) == 2
    ```

- [ ] **Step 7: Run + gate**

Run: `uv run pytest tests/scope/test_wlan_object.py tests/scope/test_allowlist.py tests/scope/test_object_gate.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .` — all green.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/scope/allowlist.py src/digital_twin/adapters/mist/apply/objects.py src/digital_twin/scope/field_gate.py tests/scope/test_wlan_object.py tests/scope/test_allowlist.py tests/scope/test_object_gate.py
git commit -m "feat(lint): wlan as a simulable site object (gate+allowlist+apply+inherited screen)"
```

### Task 5: L0 `wlan` schema (else `wlan` ops fatal pre-apply)

**Files:**
- Create: `src/digital_twin/adapters/mist/oas/wlan.schema.json`
- Modify: `src/digital_twin/adapters/mist/validate/schema.py` (`_SCHEMA_FILES`)
- Modify: `tests/adapters/mist/test_validate_l0.py` (flip the wlan-fatal case)

- [ ] **Step 1: Update the failing test.** In `tests/adapters/mist/test_validate_l0.py`, replace the case that pins `validate_payload("wlan", {})` as fatal with:

```python
def test_wlan_schema_validates_not_fatal():
    from digital_twin.adapters.mist.validate.schema import validate_payload
    ok = validate_payload("wlan", {"isolation": True})       # modeled leaf
    assert ok.fatal is False and ok.findings == ()
    bad = validate_payload("wlan", {"enabled": "yes"})       # wrong type
    assert bad.fatal is False and any("enabled" in f.evidence.get("path", "") or
                                      "enabled" in f.message for f in bad.findings)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_validate_l0.py::test_wlan_schema_validates_not_fatal -v` — FAIL (`wlan` has no schema → fatal).

- [ ] **Step 3: Commit the thin schema.** Create `src/digital_twin/adapters/mist/oas/wlan.schema.json` (thin + permissive — covers the modeled leaves; `additionalProperties` left permissive, same precedent as `site_template`):

```json
{
  "type": "object",
  "properties": {
    "ssid": { "type": "string" },
    "enabled": { "type": "boolean" },
    "isolation": { "type": "boolean" },
    "l2_isolation": { "type": "boolean" },
    "apply_to": { "type": "string", "enum": ["site", "aps", "wxtags"] },
    "ap_ids": { "type": "array", "items": { "type": "string" } },
    "wxtag_ids": { "type": "array", "items": { "type": "string" } },
    "auth": {
      "type": "object",
      "properties": { "type": { "type": "string" } }
    }
  }
}
```

- [ ] **Step 4: Register it** in `src/digital_twin/adapters/mist/validate/schema.py`, add to `_SCHEMA_FILES`:

```python
    "sitetemplate": "sitetemplate.schema.json",
    # thin/permissive WLAN schema: types the modeled lint leaves so a `wlan` op
    # L0-validates instead of fatal-rejecting; scoped L0 (changed roots) means a
    # partial WLAN update only validates the touched root.
    "wlan": "wlan.schema.json",
```

- [ ] **Step 5: Run + gate**

Run: `uv run pytest tests/adapters/mist/test_validate_l0.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .` — all green.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/oas/wlan.schema.json src/digital_twin/adapters/mist/validate/schema.py tests/adapters/mist/test_validate_l0.py
git commit -m "feat(lint): L0 wlan schema (thin/permissive) so wlan ops validate not fatal"
```

---

## Phase 4 — the shared lint helper + GS33 + GS32

### Task 6: shared `run_delta_lint` helper

**Files:**
- Create: `src/digital_twin/checks/wired/config_lint.py`
- Test: `tests/checks/test_config_lint_helper.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_config_lint_helper.py
from digital_twin.checks.base import Coverage, CoverageState, Status
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir.diff import EntityRef, IRDiff, Modified


def test_touched_ids_filters_by_kind():
    diff = IRDiff(added=(EntityRef("wlan", "w2"),), removed=(),
                  modified=(Modified(EntityRef("vlan", "10"), ("subnet",)),))
    assert touched_ids(diff, "wlan") == {"w2"}
    assert touched_ids(diff, "vlan") == {"10"}


def _v(key, summary):
    return Violation(key=key, subject=ObjectRef("vlan", "10"), affected=("10",),
                     evidence={"k": key}, summary=summary, caused_by=())


def test_introduced_is_warning_preexisting_is_info():
    res = run_delta_lint(
        check_id="wired.l2.vlan_collision",
        base=[_v("old", "old")],
        proposed=[_v("old", "old"), _v("new", "new")],
        coverage=Coverage(state=CoverageState.COMPLETE),
    )
    by_code = {f.code: f for f in res.findings}
    assert by_code["wired.l2.vlan_collision.introduced"].severity is Severity.WARNING
    assert by_code["wired.l2.vlan_collision.preexisting"].severity is Severity.INFO
    assert res.status is Status.WARN   # an introduced violation


def test_all_preexisting_is_pass():
    res = run_delta_lint(
        check_id="wired.l2.vlan_collision", base=[_v("k", "k")], proposed=[_v("k", "k")],
        coverage=Coverage(state=CoverageState.COMPLETE),
    )
    assert res.status is Status.PASS
    assert all(f.severity is Severity.INFO for f in res.findings)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/checks/test_config_lint_helper.py -v` — FAIL (module missing).

- [ ] **Step 3: Implement** `src/digital_twin/checks/wired/config_lint.py`:

```python
"""Shared delta-conditioning core for the config-lint tier (GS30–GS33).

Each lint check computes a list of `Violation`s on baseline and on proposed; this
core emits INTRODUCED violations (key not in baseline) as WARNING and PRE-EXISTING
ones (key in baseline) as INFO context. The violation KEY carries the violation
facts, so a *changed* violation reads as introduced, not pre-existing."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any

from digital_twin.checks.base import CheckResult, Coverage, Status
from digital_twin.contracts import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import Confidence, ConfidenceLevel, IRDiff

_HIGH = Confidence(level=ConfidenceLevel.HIGH)


def touched_ids(diff: IRDiff, kind: str) -> set[str]:
    """Entity ids of `kind` the delta added/removed/modified. Used to RELEVANCE-SCOPE
    coverage notes: a lint check emits a PARTIAL note only when the unverifiable item is
    itself delta-touched (PARTIAL floors to REVIEW, so an unrelated old wxtag/unparseable
    item must never taint an unrelated change)."""
    refs = (*diff.added, *diff.removed, *(m.ref for m in diff.modified))
    return {r.id for r in refs if r.kind == kind}


@dataclass(frozen=True)
class Violation:
    key: Hashable               # identity incl. facts (changed violation => introduced)
    subject: ObjectRef
    affected: tuple[str, ...]
    summary: str                # human phrase
    evidence: dict[str, Any] = field(default_factory=dict)
    caused_by: tuple[Cause, ...] = ()


def run_delta_lint(
    *, check_id: str, base: list[Violation], proposed: list[Violation], coverage: Coverage
) -> CheckResult:
    base_keys = {v.key for v in base}
    findings: list[Finding] = []
    for v in proposed:
        introduced = v.key not in base_keys
        sev = Severity.WARNING if introduced else Severity.INFO
        code = "introduced" if introduced else "preexisting"
        suffix = "" if introduced else " (pre-existing, unchanged by the delta — context)"
        findings.append(
            Finding(
                source=FindingSource.CHECK,
                category=FindingCategory.NETWORK,
                code=f"{check_id}.{code}",
                severity=sev,
                confidence=_HIGH,
                message=f"{v.summary}{suffix}",
                affected_entities=v.affected,
                subject=v.subject,
                evidence=dict(v.evidence),
                caused_by=v.caused_by if introduced else (),
            )
        )
    conclusions = [f for f in findings if f.severity is not Severity.INFO]
    return CheckResult(
        check_id=check_id,
        status=Status.WARN if conclusions else Status.PASS,
        findings=tuple(findings),
        coverage=coverage,
        confidence=_HIGH,
        reasoning=f"{len(proposed)} violation(s) on proposed; {len(conclusions)} introduced",
    )
```

- [ ] **Step 4: Run + gate**

Run: `uv run pytest tests/checks/test_config_lint_helper.py -v && uv run mypy src && uv run ruff check .` — green.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/config_lint.py tests/checks/test_config_lint_helper.py
git commit -m "feat(lint): shared run_delta_lint helper (introduced=WARNING, preexisting=INFO)"
```

### Task 7: GS33 `wireless.wlan.open_guest`

**Files:**
- Create: `src/digital_twin/checks/wired/wlan_open_guest.py`
- Modify: `src/digital_twin/checks/wired/__init__.py` (register)
- Test: `tests/checks/test_wlan_open_guest.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_wlan_open_guest.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.wlan_open_guest import WlanOpenGuestCheck
from digital_twin.ir import IRCapability, Wlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder
from digital_twin.contracts import Severity


def _ir(*wlans):
    b = IRBuilder().with_capability(IRCapability.WLAN_CONFIG)
    for w in wlans:
        b.add_wlan(w)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


_OPEN = dict(ssid="guest", enabled=True, auth_type="open", apply_to="site")


def test_introduced_open_no_isolation_is_warning():
    base = _ir(Wlan(id="w1", isolation=True, **_OPEN))          # was isolated
    prop = _ir(Wlan(id="w1", isolation=False, **_OPEN))         # isolation removed
    res = WlanOpenGuestCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN
    assert res.findings[0].code.endswith(".introduced")


def test_isolated_open_guest_is_clean():
    ir = _ir(Wlan(id="w1", isolation=True, **_OPEN))
    assert WlanOpenGuestCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_empty_explicit_scope_is_silent():
    # apply_to aps + no ap_ids => applies nowhere => not a finding/note
    ir = _ir(Wlan(id="w1", ssid="g", enabled=True, auth_type="open", apply_to="aps"))
    res = WlanOpenGuestCheck().run(_ctx(ir, ir))
    assert res.findings == () and res.coverage.state is CoverageState.COMPLETE


def test_wxtag_scope_INTRODUCED_is_partial_note_not_finding():
    # the wxtag WLAN is delta-touched (added) -> PARTIAL note, no WARNING
    wx = Wlan(id="w1", ssid="g", enabled=True, auth_type="open", isolation=False,
              apply_to="wxtags", wxtag_ids=("t1",))
    res = WlanOpenGuestCheck().run(_ctx(_ir(), _ir(wx)))
    assert all(f.severity is not Severity.WARNING for f in res.findings)
    assert res.coverage.state is CoverageState.PARTIAL


def test_unrelated_diff_leaves_old_wxtag_wlan_complete():
    # relevance-scoping: a pre-existing wxtag WLAN (untouched) + an UNRELATED wlan change
    # must NOT floor to PARTIAL/REVIEW.
    wx = Wlan(id="w1", ssid="g", enabled=True, auth_type="open", isolation=False,
              apply_to="wxtags", wxtag_ids=("t1",))
    base = _ir(wx, Wlan(id="w2", ssid="corp", enabled=True, apply_to="site"))
    prop = _ir(wx, Wlan(id="w2", ssid="corp2", enabled=True, apply_to="site"))  # only w2 changed
    res = WlanOpenGuestCheck().run(_ctx(base, prop))
    assert res.coverage.state is CoverageState.COMPLETE
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/checks/test_wlan_open_guest.py -v` — FAIL (module missing).

- [ ] **Step 3: Implement** `src/digital_twin/checks/wired/wlan_open_guest.py`:

```python
"""wireless.wlan.open_guest — an enabled open-auth WLAN with no client isolation.

Open auth + no isolation => any joined client can reach any other on the segment
(lateral traffic). Delta-conditioned: introduced => WARNING, pre-existing => INFO.
Scope-aware and never-false-positive: an explicit EMPTY AP scope applies nowhere
(silent); a wxtag-only / unknown scope is 'potentially active but unresolved'
(PARTIAL note, not a finding); unknown auth is skipped."""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff, Wlan
from digital_twin.ir.model import IR


def _active_scope(w: Wlan) -> str:
    """'active' | 'nowhere' | 'unresolved' — does this WLAN apply to any AP?"""
    if w.apply_to == "site":
        return "active"
    if w.apply_to == "aps":
        return "active" if w.ap_ids else "nowhere"
    # wxtags / None / unknown -> can't resolve membership
    return "unresolved"


class WlanOpenGuestCheck:
    id = "wireless.wlan.open_guest"
    title = "open guest WLAN without client isolation"
    domain = "wireless.wlan"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WLAN_CONFIG})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("wlan")

    def _violations(self, ir: IR) -> list[Violation]:
        viols: list[Violation] = []
        for w in ir.wlans:
            if not w.enabled or w.auth_type != "open" or w.isolation:
                continue
            if _active_scope(w) != "active":  # nowhere -> silent; unresolved -> note in run()
                continue
            viols.append(
                Violation(
                    key=w.id,
                    subject=ObjectRef("wlan", w.id, w.ssid or None),
                    affected=(w.id,),
                    summary=(
                        f"open guest WLAN '{w.ssid}' has no client isolation — "
                        "joined clients can reach each other (lateral traffic)"
                    ),
                    evidence={"ssid": w.ssid, "auth_type": w.auth_type, "isolation": w.isolation},
                )
            )
        return viols

    def _unresolved(self, ir: IR) -> list[Wlan]:
        return [
            w for w in ir.wlans
            if w.enabled and w.auth_type == "open" and not w.isolation
            and _active_scope(w) == "unresolved"
        ]

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx.baseline.ir)
        prop = self._violations(ctx.proposed.ir)
        # RELEVANCE-SCOPED: note an unresolved open WLAN only when it is delta-touched,
        # so an unrelated old wxtag WLAN never floors an unrelated change to REVIEW.
        touched = touched_ids(ctx.diff, "wlan")
        notes = tuple(
            f"WLAN '{w.ssid}' is open without isolation but its AP scope "
            f"({w.apply_to}) is unresolved — potentially active"
            for w in self._unresolved(ctx.proposed.ir) if w.id in touched
        )
        coverage = Coverage(
            state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE, notes=notes,
        )
        return run_delta_lint(check_id=self.id, base=base, proposed=prop, coverage=coverage)
```

- [ ] **Step 4: Register** in `src/digital_twin/checks/wired/__init__.py`: import `WlanOpenGuestCheck` and append `WlanOpenGuestCheck()` to `ALL_WIRED_CHECKS`.

- [ ] **Step 5: Run + gate**

Run: `uv run pytest tests/checks/test_wlan_open_guest.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .` — all green.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/checks/wired/wlan_open_guest.py src/digital_twin/checks/wired/__init__.py tests/checks/test_wlan_open_guest.py
git commit -m "feat(lint): GS33 wireless.wlan.open_guest (scope-aware, delta-conditioned)"
```

### Task 8: GS32 `wireless.wlan.duplicate_ssid`

**Files:**
- Create: `src/digital_twin/checks/wired/wlan_duplicate_ssid.py`
- Modify: `src/digital_twin/checks/wired/__init__.py` (register)
- Test: `tests/checks/test_wlan_duplicate_ssid.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_wlan_duplicate_ssid.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.wlan_duplicate_ssid import WlanDuplicateSsidCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRCapability, Wlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(*wlans):
    b = IRBuilder().with_capability(IRCapability.WLAN_CONFIG)
    for w in wlans:
        b.add_wlan(w)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_two_site_scoped_same_ssid_introduced_warns():
    base = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"))
    prop = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"),
               Wlan(id="w2", ssid="corp", enabled=True, apply_to="site"))
    res = WlanDuplicateSsidCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN and res.findings[0].code.endswith(".introduced")


def test_disabled_duplicate_not_flagged():
    ir = _ir(Wlan(id="w1", ssid="corp", enabled=True, apply_to="site"),
             Wlan(id="w2", ssid="corp", enabled=False, apply_to="site"))
    assert WlanDuplicateSsidCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_wxtag_scoped_duplicate_INTRODUCED_is_note_not_finding():
    # introducing the second wxtag WLAN touches it -> PARTIAL note, no WARNING
    w1 = Wlan(id="w1", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t1",))
    w2 = Wlan(id="w2", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t2",))
    res = WlanDuplicateSsidCheck().run(_ctx(_ir(w1), _ir(w1, w2)))
    assert all(f.severity is not Severity.WARNING for f in res.findings)
    assert res.coverage.state is CoverageState.PARTIAL


def test_unrelated_diff_leaves_old_wxtag_duplicate_complete():
    w1 = Wlan(id="w1", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t1",))
    w2 = Wlan(id="w2", ssid="corp", enabled=True, apply_to="wxtags", wxtag_ids=("t2",))
    base = _ir(w1, w2, Wlan(id="w3", ssid="iot", enabled=True, apply_to="site"))
    prop = _ir(w1, w2, Wlan(id="w3", ssid="iot2", enabled=True, apply_to="site"))  # only w3
    assert WlanDuplicateSsidCheck().run(_ctx(base, prop)).coverage.state is CoverageState.COMPLETE
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/checks/test_wlan_duplicate_ssid.py -v` — FAIL (module missing).

- [ ] **Step 3: Implement** `src/digital_twin/checks/wired/wlan_duplicate_ssid.py`:

```python
"""wireless.wlan.duplicate_ssid — same SSID on 2+ enabled WLANs with overlapping
AP scope. Provable overlap only: both site; site + explicit-AP; or a shared
explicit ap_id. wxtag/mixed/unknown scope -> unverifiable -> PARTIAL note, not a
finding. Key = the overlapping WLAN-id pair (a pre-existing dup on A/B must not
mask a new dup on C/D). Delta-conditioned via run_delta_lint."""

from __future__ import annotations

from itertools import combinations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff, Wlan
from digital_twin.ir.model import IR


def _overlap(a: Wlan, b: Wlan) -> str:
    """'yes' | 'no' | 'unknown' — do these two WLANs cover a common AP?"""
    sa, sb = a.apply_to, b.apply_to
    if "wxtags" in (sa, sb) or None in (sa, sb):
        return "unknown"
    if sa == "site" and sb == "site":
        return "yes"
    if sa == "site" and sb == "aps":
        return "yes" if b.ap_ids else "no"
    if sb == "site" and sa == "aps":
        return "yes" if a.ap_ids else "no"
    if sa == "aps" and sb == "aps":
        return "yes" if set(a.ap_ids) & set(b.ap_ids) else "no"
    return "unknown"


class WlanDuplicateSsidCheck:
    id = "wireless.wlan.duplicate_ssid"
    title = "duplicate SSID on overlapping APs"
    domain = "wireless.wlan"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WLAN_CONFIG})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("wlan")

    def _groups(self, ir: IR) -> dict[str, list[Wlan]]:
        by_ssid: dict[str, list[Wlan]] = {}
        for w in ir.wlans:
            if w.enabled and w.ssid:
                by_ssid.setdefault(w.ssid, []).append(w)
        return {s: g for s, g in by_ssid.items() if len(g) >= 2}

    def _violations(self, ir: IR) -> list[Violation]:
        viols: list[Violation] = []
        for ssid, group in self._groups(ir).items():
            for a, b in combinations(sorted(group, key=lambda w: w.id), 2):
                if _overlap(a, b) == "yes":
                    pair = (a.id, b.id)
                    viols.append(
                        Violation(
                            key=pair,
                            subject=ObjectRef("wlan", a.id, ssid),
                            affected=pair,
                            summary=(
                                f"SSID '{ssid}' is broadcast by two overlapping WLANs "
                                f"({a.id}, {b.id})"
                            ),
                            evidence={"ssid": ssid, "wlans": list(pair)},
                        )
                    )
        return viols

    def _unverifiable(self, ir: IR) -> list[tuple[str, str, str]]:
        """(ssid, a.id, b.id) for each pair whose overlap can't be verified."""
        out: list[tuple[str, str, str]] = []
        for ssid, group in self._groups(ir).items():
            for a, b in combinations(sorted(group, key=lambda w: w.id), 2):
                if _overlap(a, b) == "unknown":
                    out.append((ssid, a.id, b.id))
        return out

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx.baseline.ir)
        prop = self._violations(ctx.proposed.ir)
        # RELEVANCE-SCOPED: note an unverifiable duplicate only when one of its WLANs is
        # delta-touched, so a pre-existing wxtag duplicate never floors an unrelated change.
        touched = touched_ids(ctx.diff, "wlan")
        notes = tuple(dict.fromkeys(
            f"SSID '{ssid}' duplicated across WLANs with wxtag/unknown scope — overlap unverifiable"
            for ssid, a_id, b_id in self._unverifiable(ctx.proposed.ir)
            if a_id in touched or b_id in touched
        ))
        coverage = Coverage(
            state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE, notes=notes,
        )
        return run_delta_lint(check_id=self.id, base=base, proposed=prop, coverage=coverage)
```

- [ ] **Step 4: Register** in `src/digital_twin/checks/wired/__init__.py` (import + append `WlanDuplicateSsidCheck()`).

- [ ] **Step 5: Run + gate** — `uv run pytest tests/checks/test_wlan_duplicate_ssid.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/checks/wired/wlan_duplicate_ssid.py src/digital_twin/checks/wired/__init__.py tests/checks/test_wlan_duplicate_ssid.py
git commit -m "feat(lint): GS32 wireless.wlan.duplicate_ssid (provable-overlap only)"
```

---

## Phase 5 — GS31 + GS30

### Task 9: GS31 `wired.l3.subnet_overlap`

**Files:**
- Create: `src/digital_twin/checks/wired/subnet_overlap.py`
- Modify: `src/digital_twin/checks/wired/__init__.py` (register)
- Test: `tests/checks/test_subnet_overlap.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_subnet_overlap.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.subnet_overlap import SubnetOverlapCheck
from digital_twin.ir import IRCapability, Vlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(*vlans):
    b = IRBuilder().with_capability(IRCapability.WIRED_L2)
    for v in vlans:
        b.add_vlan(v)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_introduced_overlap_warns():
    base = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"))
    prop = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"),
               Vlan(vlan_id=20, subnet="10.0.0.0/25"))   # overlaps vlan 10
    res = SubnetOverlapCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN and res.findings[0].code.endswith(".introduced")


def test_disjoint_subnets_clean():
    ir = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"), Vlan(vlan_id=20, subnet="10.1.0.0/24"))
    assert SubnetOverlapCheck().run(_ctx(ir, ir)).status is Status.PASS


def test_unresolved_subnet_skipped_relevance_scoped():
    # an untouched unresolved subnet must NOT taint -> COMPLETE coverage, no finding
    ir = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"),
             Vlan(vlan_id=20, subnet="{{var}}", subnet_unresolved=True))
    res = SubnetOverlapCheck().run(_ctx(ir, ir))
    assert res.status is Status.PASS and res.coverage.state is CoverageState.COMPLETE


def test_touched_unparseable_subnet_is_partial_note():
    # a present-but-unparseable CIDR on a DELTA-TOUCHED vlan -> PARTIAL note (not silent PASS)
    base = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"))
    prop = _ir(Vlan(vlan_id=10, subnet="10.0.0.0/24"), Vlan(vlan_id=20, subnet="not-a-cidr"))
    res = SubnetOverlapCheck().run(_ctx(base, prop))
    assert res.coverage.state is CoverageState.PARTIAL and any("20" in n for n in res.coverage.notes)
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/checks/test_subnet_overlap.py -v` (module missing).

- [ ] **Step 3: Implement** `src/digital_twin/checks/wired/subnet_overlap.py`:

```python
"""wired.l3.subnet_overlap — two different VLANs whose subnets overlap (address
ambiguity / routing confusion). Keys on the canonical parsed network (not the raw
string). Unresolved/unparseable subnets are skipped; their coverage note is
relevance-scoped (only when the skipped vlan is delta-touched). Delta-conditioned."""

from __future__ import annotations

import ipaddress
from itertools import combinations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint, touched_ids
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff
from digital_twin.ir.entities import Vlan
from digital_twin.ir.model import IR

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


def _net(subnet: str | None) -> _Net | None:
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _unusable(v: Vlan) -> bool:
    """The vlan declares a subnet we could NOT compare: templated/unresolved, OR a
    present-but-unparseable CIDR (both must be skipped AND can warrant a note)."""
    return v.subnet_unresolved or (bool(v.subnet) and _net(v.subnet) is None)


class SubnetOverlapCheck:
    id = "wired.l3.subnet_overlap"
    title = "overlapping subnets across VLANs"
    domain = "wired.l3"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("vlan")

    def _violations(self, ir: IR) -> list[Violation]:
        rows: list[tuple[int, _Net]] = []
        for v in ir.vlans.values():
            if v.subnet_unresolved:
                continue
            net = _net(v.subnet)
            if net is not None:
                rows.append((v.vlan_id, net))
        viols: list[Violation] = []
        for (va, na), (vb, nb) in combinations(rows, 2):
            if va == vb or na.version != nb.version:
                continue
            if na.overlaps(nb):
                key = frozenset({(va, str(na)), (vb, str(nb))})
                lo, hi = sorted((va, vb))
                viols.append(
                    Violation(
                        key=key,
                        subject=ObjectRef("vlan", str(lo)),
                        affected=(str(lo), str(hi)),
                        summary=(
                            f"vlan {va} subnet {na} overlaps vlan {vb} subnet {nb}"
                        ),
                        evidence={"a": [va, str(na)], "b": [vb, str(nb)]},
                    )
                )
        return viols

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx.baseline.ir)
        prop = self._violations(ctx.proposed.ir)
        # RELEVANCE-SCOPED note: only when a DELTA-TOUCHED vlan has an unusable subnet
        # (unresolved OR unparseable) — an untouched bad subnet never floors to REVIEW.
        touched = touched_ids(ctx.diff, "vlan")
        notes = tuple(
            f"vlan {v.vlan_id} subnet {v.subnet!r} could not be compared (unresolved/unparseable)"
            for v in ctx.proposed.ir.vlans.values()
            if str(v.vlan_id) in touched and _unusable(v)
        )
        coverage = Coverage(
            state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE, notes=notes
        )
        return run_delta_lint(check_id=self.id, base=base, proposed=prop, coverage=coverage)
```

- [ ] **Step 4: Register** in `checks/wired/__init__.py` (import + append `SubnetOverlapCheck()`).

- [ ] **Step 5: Run + gate** — `uv run pytest tests/checks/test_subnet_overlap.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/checks/wired/subnet_overlap.py src/digital_twin/checks/wired/__init__.py tests/checks/test_subnet_overlap.py
git commit -m "feat(lint): GS31 wired.l3.subnet_overlap (canonical key, relevance-scoped)"
```

### Task 10: GS30 `wired.l2.vlan_collision`

**Files:**
- Create: `src/digital_twin/checks/wired/vlan_collision.py`
- Modify: `src/digital_twin/checks/wired/__init__.py` (register)
- Test: `tests/checks/test_vlan_collision.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_vlan_collision.py
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.vlan_collision import VlanCollisionCheck
from digital_twin.ir import IRCapability, Vlan
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder
from digital_twin.contracts import Severity


def _ir(*vlans):
    b = IRBuilder().with_capability(IRCapability.WIRED_L2)
    for v in vlans:
        b.add_vlan(v)
    return b.build()


def _ctx(base, prop):
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_introduced_collision_warns():
    base = _ir(Vlan(vlan_id=10, name="corp"))
    prop = _ir(Vlan(vlan_id=10, name="corp", collisions=("guest",)))
    res = VlanCollisionCheck().run(_ctx(base, prop))
    assert res.status is Status.WARN and res.findings[0].code.endswith(".introduced")


def test_altered_claimant_set_is_introduced_not_info():
    base = _ir(Vlan(vlan_id=10, name="corp", collisions=("guest",)))
    prop = _ir(Vlan(vlan_id=10, name="corp", collisions=("iot",)))   # claimant changed
    assert VlanCollisionCheck().run(_ctx(base, prop)).findings[0].code.endswith(".introduced")


def test_unchanged_collision_is_info():
    ir = _ir(Vlan(vlan_id=10, name="corp", collisions=("guest",)))
    res = VlanCollisionCheck().run(_ctx(ir, ir))
    assert res.status is Status.PASS and res.findings[0].severity is Severity.INFO
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/checks/test_vlan_collision.py -v` (module missing).

- [ ] **Step 3: Implement** `src/digital_twin/checks/wired/vlan_collision.py`:

```python
"""wired.l2.vlan_collision — one vlan_id claimed by 2+ distinct network names
(the IR dedup keeps the first; Vlan.collisions carries the shadowed claimants).
Key = (vlan_id, frozenset of ALL claimant names) so a changed claimant set reads
as introduced. Delta-conditioned."""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState
from digital_twin.checks.wired.config_lint import Violation, run_delta_lint
from digital_twin.contracts import ObjectRef, Severity
from digital_twin.ir import Capability, IRCapability, IRDiff
from digital_twin.ir.model import IR


class VlanCollisionCheck:
    id = "wired.l2.vlan_collision"
    title = "vlan_id claimed by multiple networks"
    domain = "wired.l2"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("vlan")

    def _violations(self, ctx: CheckContext, ir: IR) -> list[Violation]:
        viols: list[Violation] = []
        for v in ir.vlans.values():
            if not v.collisions:
                continue
            claimants = (v.name, *v.collisions) if v.name else v.collisions
            cause = ctx.delta_index.cause("vlan", str(v.vlan_id))
            viols.append(
                Violation(
                    key=(v.vlan_id, frozenset(claimants)),
                    subject=ObjectRef("vlan", str(v.vlan_id), v.name),
                    affected=(str(v.vlan_id),),
                    summary=f"vlan {v.vlan_id} is claimed by {', '.join(sorted(claimants))}",
                    evidence={"vlan_id": v.vlan_id, "claimants": sorted(claimants)},
                    caused_by=(cause,) if cause is not None else (),
                )
            )
        return viols

    def run(self, ctx: CheckContext) -> CheckResult:
        base = self._violations(ctx, ctx.baseline.ir)
        prop = self._violations(ctx, ctx.proposed.ir)
        return run_delta_lint(
            check_id=self.id, base=base, proposed=prop,
            coverage=Coverage(state=CoverageState.COMPLETE),
        )
```

- [ ] **Step 4: Register** in `checks/wired/__init__.py` (import + append `VlanCollisionCheck()`).

- [ ] **Step 5: Run + gate** — `uv run pytest tests/checks/test_vlan_collision.py -v && uv run pytest tests -q && uv run mypy src && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/checks/wired/vlan_collision.py src/digital_twin/checks/wired/__init__.py tests/checks/test_vlan_collision.py
git commit -m "feat(lint): GS30 wired.l2.vlan_collision (claimant-set key, delta-conditioned)"
```

---

## Phase 6 — goldens, live verify, docs

### Task 11: end-to-end goldens (GS30–GS33)

**Files:**
- Modify: `tests/golden/builders.py` (small WLAN/network lint scenario builders)
- Create: `tests/golden/test_config_lint_scenarios.py`

- [ ] **Step 1: Write the goldens.** Each builds a single-site doc + plan and asserts the introduced-vs-pre-existing behavior end-to-end through `simulate`. Reuse the existing `write_doc` + `FixtureProvider` + `plan_for` helpers. Create `tests/golden/test_config_lint_scenarios.py`:

```python
import copy

from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision
from tests.golden.builders import config_lint_base_doc, write_doc


def _run(doc, plan, tmp_path, tag):
    return simulate(plan, provider=FixtureProvider(write_doc(doc, tmp_path / f"{tag}.json")))


def test_gs30_introduced_vlan_collision_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="vlan_collision_introduce")
    v = _run(doc, plan, tmp_path, "gs30")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wired.l2.vlan_collision.introduced" for f in v.findings)


def test_gs31_introduced_subnet_overlap_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="subnet_overlap_introduce")
    v = _run(doc, plan, tmp_path, "gs31")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wired.l3.subnet_overlap.introduced" for f in v.findings)


def test_gs33_open_guest_remove_isolation_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="open_guest_introduce")
    v = _run(doc, plan, tmp_path, "gs33")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wireless.wlan.open_guest.introduced" for f in v.findings)


def test_gs32_duplicate_ssid_introduced_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="duplicate_ssid_introduce")
    v = _run(doc, plan, tmp_path, "gs32")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wireless.wlan.duplicate_ssid.introduced" for f in v.findings)


def test_preexisting_collision_with_benign_edit_is_safe_info(tmp_path):
    # the violation already exists in baseline; a benign in-domain edit produces a
    # diff (so applies_to fires) but leaves the violation key unchanged -> INFO, SAFE
    doc, plan = config_lint_base_doc(kind="vlan_collision_preexisting")
    v = _run(doc, plan, tmp_path, "pre")
    assert v.decision is Decision.SAFE
    assert any(f.code == "wired.l2.vlan_collision.preexisting" for f in v.findings)
```

- [ ] **Step 2: Add the `config_lint_base_doc` builder** to `tests/golden/builders.py`. Start from `fixture_doc()` (or `augmented_doc(parallel_carries_gs=True, with_wireless_client=False)`), then per `kind` construct the baseline networks/wlans and the op. The builder returns `(doc, plan)`. Implement each kind:
  - `vlan_collision_introduce`: baseline `setting.networks` has `corp:{vlan_id:10}`; the op (a `site_setting` update) adds `guest:{vlan_id:10}` → introduces the collision. Use `plan_for(doc, [{"action":"update","order":0,"object_type":"site_setting","object_id":SITE,"payload":{"networks":{**doc["setting"]["networks"],"guest":{"vlan_id":10}}}}])`.
  - `subnet_overlap_introduce`: baseline has `corp:{vlan_id:10,subnet:"10.0.0.0/24"}`; the op adds `iot:{vlan_id:20,subnet:"10.0.0.0/25"}`.
  - `open_guest_introduce`: baseline `doc["wlans"]` has a **site-owned** open WLAN WITH isolation (`{"id":"w1","ssid":"guest","enabled":True,"for_site":True,"auth":{"type":"open"},"isolation":True,"apply_to":"site"}`) + `doc["meta"]["fetched"]` includes `"wlans"`; the op is a `wlan` update `{"isolation": False}` on `w1`.
  - `duplicate_ssid_introduce`: baseline has one site-owned enabled site-scoped WLAN `corp`; the op... duplicate SSID needs a SECOND WLAN — since a `wlan` op only edits one WLAN, introduce the duplicate by **enabling** a pre-existing-but-disabled second `corp` WLAN: baseline `w2 corp enabled:False`, op `{"enabled": True}` on `w2`.
  - `vlan_collision_preexisting`: baseline already has `corp` and `guest` both `vlan_id:10` (collision present in baseline); the benign edit is a `site_setting` op adding an unrelated `mgmt:{vlan_id:99}` (produces a `vlan` diff, leaves the vid-10 collision key unchanged).
  Use `SITE = "s1"` consistent with the chosen base doc's scope; reuse `_drop_nones` where payloads come from the fixture. (The doc must declare `WLAN_CONFIG` by listing `"wlans"` in `meta.fetched` for the WLAN kinds.)

- [ ] **Step 3: Run + gate** — `uv run pytest tests/golden/test_config_lint_scenarios.py -v && uv run pytest tests -q && uv run ruff check . && uv run mypy src` — all green. If a golden does not produce the expected finding, debug the SCENARIO (not the check) — the check unit tests already pin behavior.

- [ ] **Step 4: Commit**

```bash
git add tests/golden/builders.py tests/golden/test_config_lint_scenarios.py
git commit -m "test(lint): GS30-GS33 end-to-end goldens (introduced REVIEW, pre-existing SAFE+INFO)"
```

### Task 12: live verify + docs/roadmap/memory

**Files:**
- Modify: `docs/ROADMAP.md` (mark the four GS30–GS33 bullets ✅; fix the stale GS20 "← recommended next" tag)
- Modify: `docs/superpowers/specs/2026-06-20-config-lint-tier-design.md` (Status → Implemented)
- Memory: `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md`

- [ ] **Step 1: Live read-only verify.** With `.env` sourced, run a small script that fetches the Live-Demo site (org `9777c1a0-6ef6-11e6-8bbf-02e208b2d34f`, site `978c48e6-6ef6-11e6-8bbf-02e208b2d34f`), ingests, and prints: the `Wlan` count + any `open_guest` candidates (enabled+open+not-isolated) + any `Vlan.collisions` + duplicate SSIDs across the effective list. Confirm `mist-guest` (open WITH `isolation:true`) is NOT flagged, and that ingest is clean. Then run a no-op-domain plan (e.g. a port-trust edit) and confirm the four lint checks return NOT_APPLICABLE (no spurious findings). This is read-only; do NOT apply.

- [ ] **Step 2: Flip the spec status** in `docs/superpowers/specs/2026-06-20-config-lint-tier-design.md`: `design — pending user review` → `Implemented — live-verified 2026-06-20`.

- [ ] **Step 3: Roadmap.** In `docs/ROADMAP.md`: mark the four config-lint bullets (VLAN ID collision / IP subnet overlap / Duplicate SSID / Open guest) ✅ done 2026-06-20 with the check ids; and **fix the stale GS20 tag** — remove `**← recommended next.**` from the wxtag-WLAN-scoping bullet and note it was investigated + deprioritized (real orgs don't use `apply_to: wxtags`; current behavior is already never-false-SAFE).

- [ ] **Step 4: Memory.** Add a bullet to `digital-twin-project.md` summarizing the config-lint tier: the delta-conditioned single-state pattern + `run_delta_lint`, the `Wlan` IR entity (secret-free, `inherited` fail-closed + not diff-bearing), `Vlan.collisions`, WLAN as a simulable object (object_gate/allowlist/L0-thin-schema/apply/inherited-screen), the four checks + their never-false-positive guards, and the live-verify result.

- [ ] **Step 5: Final gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add docs/ROADMAP.md docs/superpowers/specs/2026-06-20-config-lint-tier-design.md
git commit -m "docs(lint): config-lint tier Implemented + roadmap done + GS20 tag fixed"
```

---

## Self-review (against the spec)

- **§1 `Wlan` entity + ingest + diff** → Tasks 1–2 (entity, mint-all-rows, fail-closed inherited, diff kind, `inherited` ignored). ✓
- **§1 `Vlan.collisions`** → Task 3 (distinct-other names at dedup). ✓
- **§2 WLAN simulable object** → Task 4 (object_gate/allowlist/apply/inherited screen) + Task 5 (L0 thin schema, flip the fatal test). ✓
- **§3 delta-conditioned shape + the four checks + keys + guards** → Task 6 (`run_delta_lint`), Task 7 (GS33 scope-aware), Task 8 (GS32 overlap cases + wxtag note), Task 9 (GS31 canonical key + relevance-scoped note), Task 10 (GS30 claimant-set key). ✓
- **§4 capabilities/registration** → checks `requires`/`applies_to` set per check; registered in `ALL_WIRED_CHECKS` in Tasks 7–10. ✓
- **Testing** (per-check units, ingest units, object/apply/field/L0 units, diff, goldens, live, redaction-of-portal-url) → Tasks 1–12. The `portal_template_url` redaction is already covered by the existing `_URL_CRED` rule (the `Wlan` entity carries no such field); add an assertion in the live-verify/golden step if a raw `wlans` fixture is captured.

**Type consistency:** `Wlan` field set identical in entities (T1), `_mint_wlan` (T2), checks (T7–T10); `Violation`/`run_delta_lint` signature identical across T6–T10; `RAW_ALLOWLIST["wlan"]` leaves match `_mint_wlan`'s consumed fields; `add_wlan`/`ir.wlans` consistent; every check imports `Severity` at the top and sets `default_severity = Severity.WARNING`.
