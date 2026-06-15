# gatewaytemplate / sitetemplate as first-class object_types — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `gatewaytemplate` and `sitetemplate` first-class org-template `object_type`s for org-template simulation, on a unified layered effective-config compiler, reusing the existing checks and org fan-out, never false-SAFE.

**Architecture:** Generalize the networktemplate org fan-out to the typed set `{networktemplate, gatewaytemplate, sitetemplate}`. Introduce one `fold_layers(layers, policy)` primitive over the uniform vendor stack `<type>template → sitetemplate → site_setting → device`. Add the `sitetemplate` layer (switch + gateway), a thin gateway compile (gatewaytemplate folded under the device → existing GS22 gateway IR/checks reused), a role-keyed derived gate that also screens gateway effective + a shared DHCP-row-relevance helper, and a post-ingest device-profile gate. Everything is verified against the spec at `docs/superpowers/specs/2026-06-15-gateway-site-template-object-types-design.md`.

**Tech Stack:** Python 3.14, uv, pytest, jsonschema, networkx, mistapi SDK. Full gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src` (mypy strict on `src`; tests not type-checked). 100-col ruff limit. Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

**Spec reference:** read `docs/superpowers/specs/2026-06-15-gateway-site-template-object-types-design.md` before starting — every leaf list, screen rule, and the 3×3 DHCP matrix live there and are authoritative.

---

## File Structure

**New files:**
- `src/digital_twin/adapters/mist/compile/fold.py` — `fold_layers(layers, policy)` primitive + `PolicyTable`.
- `src/digital_twin/adapters/mist/compile/gateway.py` — `GATEWAY_POLICY` + `compile_gateway_device(...)` (fold gateway layers → per-key device overlay → `_resolve` last).
- `src/digital_twin/scope/dhcp_screen.py` — the shared row-level DHCP-relevance helper (`dhcp_row_rejection`).
- `src/digital_twin/scope/device_profile_gate.py` — the post-ingest device-profile relevance gate.
- `src/digital_twin/adapters/mist/oas/gatewaytemplate.schema.json`, `sitetemplate.schema.json` — committed OAS for L0.

**Modified files:**
- `src/digital_twin/adapters/mist/compile/merge.py` — reimplement `merge_site_effective` on `fold_layers`; add `SWITCH_POLICY`; keep `merge_only`/`merge_site_effective` 2-arg. (`GATEWAY_POLICY` lives in `compile/gateway.py`, NOT here — see the New files entry.)
- `src/digital_twin/adapters/mist/compile/switch.py` — `merge_only`/`compile_site`/`compile_device` accept an optional `sitetemplate` layer.
- `src/digital_twin/scope/allowlist.py` — `ORG_OBJECT_TYPES`; `RAW_ALLOWLIST` gateway/site entries; gateway leaf set; `GATEWAY_EFFECTIVE_ALLOWLIST`; `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE`.
- `src/digital_twin/scope/object_gate.py` — ORG detection on `ORG_OBJECT_TYPES`.
- `src/digital_twin/scope/derived_gate.py` — role-keyed `check_derived` + call the DHCP-row helper on each `dhcpd_config.*` row.
- `src/digital_twin/providers/base.py` — `RawSiteState` `+sitetemplate +gatewaytemplate`; `resolve_org_template(scope, id, object_type)`.
- `src/digital_twin/providers/mist_api.py` — typed `resolve_org_template`; per-site sitetemplate/gatewaytemplate fetch.
- `src/digital_twin/adapters/mist/adapter.py` — build gateway effective via `compile_gateway_device`, materialize into devices, expose a gateway-effective map for the derived gate.
- `src/digital_twin/adapters/mist/validate/schema.py` — register the two new schemas.
- `src/digital_twin/engine/org_template.py` — typed `override_template(object_type, ...)`; `apply_template` already type-agnostic.
- `src/digital_twin/engine/pipeline.py` — typed `simulate_org_template`; gateway compile + derived-gate wiring; device-profile post-ingest hook in `_simulate_site_state`.
- `src/digital_twin/drivers/cli.py`, `drivers/mcp_server.py` — `_is_org_plan` on `ORG_OBJECT_TYPES`; typed `resolve_org_template` delegate.
- `src/digital_twin/observability/replay/store.py` — `_RAW_FIELDS`/`load_fixture_doc` new fields; typed multi-template fixture.
- `tests/golden/builders.py`, `tests/golden/test_golden_scenarios.py` — gateway/site goldens.

**Phases (sequential):** 1 fold primitive + typed gates → 2 sitetemplate layer + provider fetch → 3 gateway compile + derived gate + DHCP helper → 4 device-profile post-ingest gate → 5 fan-out/drivers/replay typing → 6 goldens + live + docs.

Run the full gate (`uv run pytest tests -q && uv run ruff check . && uv run mypy src`) after each phase's final commit, not only per task.

---

## Phase 1 — The fold primitive + typed gates/allowlists

### Task 1: `fold_layers` primitive

**Files:**
- Create: `src/digital_twin/adapters/mist/compile/fold.py`
- Test: `tests/adapters/mist/compile/test_fold.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/compile/test_fold.py
from digital_twin.adapters.mist.compile.fold import MergePolicy, fold_layers


def test_replace_default_last_layer_wins():
    out = fold_layers([{"a": 1, "b": 1}, {"b": 2}], {})
    assert out == {"a": 1, "b": 2}


def test_none_layers_skipped():
    out = fold_layers([None, {"a": 1}, None], {})
    assert out == {"a": 1}


def test_dict_merge_per_key_later_layer_wins_per_key():
    policy = {"networks": MergePolicy.DICT_MERGE}
    base = {"networks": {"corp": {"vlan_id": 10}, "guest": {"vlan_id": 20}}}
    top = {"networks": {"guest": {"vlan_id": 99}, "iot": {"vlan_id": 30}}}
    out = fold_layers([base, top], policy)
    assert out["networks"] == {
        "corp": {"vlan_id": 10},
        "guest": {"vlan_id": 99},
        "iot": {"vlan_id": 30},
    }


def test_replace_field_not_merged():
    # a field absent from _POLICY replaces wholesale (a sitetemplate one-port
    # edit must not be merged when policy says REPLACE)
    out = fold_layers([{"x": {"a": 1}}, {"x": {"b": 2}}], {})
    assert out["x"] == {"b": 2}


def test_three_layer_fold_equals_left_fold_of_two():
    policy = {"networks": MergePolicy.DICT_MERGE}
    a = {"networks": {"n1": {"vlan_id": 1}}}
    b = {"networks": {"n2": {"vlan_id": 2}}}
    c = {"networks": {"n2": {"vlan_id": 22}, "n3": {"vlan_id": 3}}}
    assert fold_layers([a, b, c], policy)["networks"] == {
        "n1": {"vlan_id": 1}, "n2": {"vlan_id": 22}, "n3": {"vlan_id": 3},
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/compile/test_fold.py -q`
Expected: FAIL — `ModuleNotFoundError: digital_twin.adapters.mist.compile.fold`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/digital_twin/adapters/mist/compile/fold.py
"""Ordered layer fold over the vendor stack (base -> winner), per-field policy.

The single merge primitive for every device family: switch and gateway pass
their own PolicyTable so a field can merge differently per family without a
refactor. REPLACE (default) replaces the field wholesale; DICT_MERGE merges a
keyed collection per key (later layer wins per key) so a higher layer that sets
one key does not wipe the others.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

JsonObj = dict[str, Any]


class MergePolicy(StrEnum):
    REPLACE = "replace"
    DICT_MERGE = "dict_merge"


PolicyTable = Mapping[str, MergePolicy]


def fold_layers(layers: Sequence[JsonObj | None], policy: PolicyTable) -> JsonObj:
    out: JsonObj = {}
    for layer in layers:
        if layer is None:
            continue
        for field, value in layer.items():
            base = out.get(field)
            if (
                policy.get(field, MergePolicy.REPLACE) is MergePolicy.DICT_MERGE
                and isinstance(base, dict)
                and isinstance(value, dict)
            ):
                merged = dict(base)
                merged.update(copy.deepcopy(value))
                out[field] = merged
            else:
                out[field] = copy.deepcopy(value)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/adapters/mist/compile/test_fold.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/compile/fold.py tests/adapters/mist/compile/test_fold.py
git commit -m "feat: fold_layers primitive (ordered layers + per-field policy)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 2: Reimplement `merge_site_effective` on `fold_layers`; add policy tables

**Files:**
- Modify: `src/digital_twin/adapters/mist/compile/merge.py`
- Test: `tests/adapters/mist/compile/test_merge.py` (extend if present, else create)

- [ ] **Step 1: Write the failing test** — behaviour-identical when no sitetemplate, plus the new sitetemplate layer.

```python
# tests/adapters/mist/compile/test_merge.py  (add these)
from digital_twin.adapters.mist.compile.merge import SWITCH_POLICY, merge_site_effective


def test_merge_site_effective_unchanged_without_sitetemplate():
    nt = {"networks": {"corp": {"vlan_id": 10}}}
    ss = {"networks": {"corp": {"vlan_id": 11}}}
    assert merge_site_effective(nt, ss)["networks"]["corp"]["vlan_id"] == 11


def test_merge_site_effective_folds_sitetemplate_between_nt_and_site():
    # sitetemplate sits between networktemplate (base) and site_setting (wins)
    nt = {"networks": {"corp": {"vlan_id": 10}}}
    st = {"networks": {"corp": {"vlan_id": 20}, "guest": {"vlan_id": 30}}}
    ss = {"networks": {"guest": {"vlan_id": 31}}}
    out = merge_site_effective(nt, ss, sitetemplate=st)
    assert out["networks"]["corp"]["vlan_id"] == 20   # from sitetemplate
    assert out["networks"]["guest"]["vlan_id"] == 31   # site_setting wins


def test_switch_policy_dict_merge_fields():
    for f in ("networks", "port_usages", "vars", "dhcpd_config"):
        assert SWITCH_POLICY[f].value == "dict_merge"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/compile/test_merge.py -q`
Expected: FAIL — `merge_site_effective() got an unexpected keyword argument 'sitetemplate'` and `ImportError: SWITCH_POLICY`.

- [ ] **Step 3: Write minimal implementation** — replace the body of `merge.py` (keep `MergePolicy` import from `fold`, keep `merge_site_effective` callable with the existing 2-arg call sites working):

```python
# src/digital_twin/adapters/mist/compile/merge.py
"""Site-level merge: <type>template (base) + sitetemplate + site_setting (wins).

Reimplemented on fold_layers. The 2-arg merge_site_effective(nt, ss) signature
is preserved (existing callers + the offline Tier-2 equivalence gate); the
optional sitetemplate layer folds between the template and site_setting.
"""

from __future__ import annotations

from typing import Any

from .fold import MergePolicy, PolicyTable, fold_layers

JsonObj = dict[str, Any]

# Keyed collections merged per key (later layer wins per key). Everything else
# REPLACE. GATEWAY_POLICY adds the gateway keyed maps (Phase 3).
SWITCH_POLICY: PolicyTable = {
    "networks": MergePolicy.DICT_MERGE,
    "port_usages": MergePolicy.DICT_MERGE,
    "vars": MergePolicy.DICT_MERGE,
    "dhcpd_config": MergePolicy.DICT_MERGE,
    "switch_matching": MergePolicy.REPLACE,
}


def merge_site_effective(
    networktemplate: JsonObj | None,
    site_setting: JsonObj,
    *,
    sitetemplate: JsonObj | None = None,
) -> JsonObj:
    """Full effective SITE config (all fields). nt (base) -> sitetemplate ->
    site_setting (wins)."""
    return fold_layers([networktemplate, sitetemplate, site_setting], SWITCH_POLICY)
```

- [ ] **Step 4: Run the full suite to catch existing callers**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS. If a caller in `compile/switch.py` imported the old `MergePolicy`/`_POLICY` from `merge.py`, update the import to `from .fold import MergePolicy` and `from .merge import SWITCH_POLICY`. (Search: `grep -rn "from .merge import\|compile.merge import" src tests`.)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: merge_site_effective on fold_layers + optional sitetemplate layer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 3: Typed `ORG_OBJECT_TYPES`, RAW allowlists, gateway leaf set, GATEWAY_EFFECTIVE_ALLOWLIST

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py`
- Test: `tests/scope/test_allowlist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scope/test_allowlist.py  (add)
from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    GATEWAY_EFFECTIVE_ALLOWLIST,
    ORG_OBJECT_TYPES,
    RAW_ALLOWLIST,
)


def test_org_object_types_includes_all_three():
    assert set(ORG_OBJECT_TYPES) == {"networktemplate", "gatewaytemplate", "sitetemplate"}


def test_gatewaytemplate_raw_allowlist_is_modeled_leaves_only():
    gw = set(RAW_ALLOWLIST["gatewaytemplate"])
    assert "port_config.*.disabled" in gw and "ip_configs.*.ip" in gw
    assert "vars.*" in gw                        # a vars edit must pass the RAW field
    # gate so the derived gate can evaluate the ripple (mirrors site_setting)
    assert "port_config.*.usage" not in gw      # inert -> excluded
    assert "networks.*.vlan_id" not in gw       # org-namespace -> excluded


def test_sitetemplate_raw_allowlist_is_union():
    st = set(RAW_ALLOWLIST["sitetemplate"])
    assert set(RAW_ALLOWLIST["site_setting"]).issubset(st)        # switch/site surface
    assert "ip_configs.*.ip" in st                                # + gateway leaves


def test_gateway_effective_allowlist_includes_disabled_ip_and_vars():
    gw = set(GATEWAY_EFFECTIVE_ALLOWLIST)
    assert {"port_config.*.disabled", "ip_configs.*.ip", "vars.*"} <= gw
    assert "port_config.*.disabled" not in set(EFFECTIVE_ALLOWLIST)  # switch lacks it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scope/test_allowlist.py -q`
Expected: FAIL — `ImportError: GATEWAY_EFFECTIVE_ALLOWLIST` / `KeyError: 'gatewaytemplate'`.

- [ ] **Step 3: Write minimal implementation** — edit `allowlist.py`:

Change line 19:
```python
ORG_OBJECT_TYPES: tuple[str, ...] = ("networktemplate", "gatewaytemplate", "sitetemplate")
```

After the existing `_DHCP_LEAVES` / `_IRB_LEAVES` definitions, add the gateway modeled leaf set (the §4 list — `port_config.*.{networks,port_network,disabled}`, `ip_configs.*.ip`, `dhcpd_config.*.{type,servers,ip_start,ip_end,gateway}`):
```python
# Gateway modeled effective leaves (the §4 list): exactly what _gateway_ports_and_l3
# + gateway dhcp consume AND act on. NOT port_config.*.usage (inert -> Port.profile),
# NOT networks (gateway namespace is org_networks, not the device's own networks).
_GATEWAY_PORT_LEAVES: tuple[str, ...] = (
    "port_config.*.networks",
    "port_config.*.port_network",
    "port_config.*.disabled",
)
_GATEWAY_L3_LEAVES: tuple[str, ...] = ("ip_configs.*.ip",)
_GATEWAY_DHCP_LEAVES: tuple[str, ...] = (
    "dhcpd_config.*.type",
    "dhcpd_config.*.servers",
    "dhcpd_config.*.ip_start",
    "dhcpd_config.*.ip_end",
    "dhcpd_config.*.gateway",
)
_GATEWAY_LEAVES: tuple[str, ...] = (
    *_GATEWAY_PORT_LEAVES, *_GATEWAY_L3_LEAVES, *_GATEWAY_DHCP_LEAVES,
)
```

After the `RAW_ALLOWLIST["networktemplate"] = ...` line add:
```python
# vars.* is allowlisted (like site_setting/networktemplate) so a gatewaytemplate
# vars edit passes the RAW field gate and the derived gate evaluates its ripple.
RAW_ALLOWLIST["gatewaytemplate"] = (*_GATEWAY_LEAVES, "vars.*")
# sitetemplate sits in BOTH stacks -> union of switch/site leaves + gateway leaves.
# Verified against the committed sitetemplate OAS in Phase 5 (narrow only if the
# schema proves a leaf cannot appear).
RAW_ALLOWLIST["sitetemplate"] = (*RAW_ALLOWLIST["site_setting"], *_GATEWAY_LEAVES, "vars.*")
```

After the `EFFECTIVE_ALLOWLIST` definition add:
```python
# Gateway effective allowlist (role-keyed derived gate): the gateway modeled leaves
# + vars.* (the vars root survives _resolve; the derived gate catches its ripple,
# so the vars.* leaf itself must be allowed).
GATEWAY_EFFECTIVE_ALLOWLIST: tuple[str, ...] = (*_GATEWAY_LEAVES, "vars.*")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scope/test_allowlist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope/allowlist.py tests/scope/test_allowlist.py
git commit -m "feat: typed ORG_OBJECT_TYPES + gateway/site allowlists + GATEWAY_EFFECTIVE_ALLOWLIST

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 4: `object_gate` ORG detection on `ORG_OBJECT_TYPES`

**Files:**
- Modify: `src/digital_twin/scope/object_gate.py:31,41`
- Test: `tests/scope/test_object_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scope/test_object_gate.py  (add)
from digital_twin.contracts import ChangePlan  # adjust import to actual ChangePlan builder
from digital_twin.scope.object_gate import check_objects


def _org_plan(object_type, tid="t1"):
    return {"scope": {"org_id": "o1"}, "ops": [
        {"object_type": object_type, "object_id": tid, "action": "update", "payload": {}}]}


def test_gatewaytemplate_plan_classified_org():
    # parse via the same path the pipeline uses; assert ORG (not SITE/UNKNOWN)
    rej = check_objects(_org_plan("gatewaytemplate"))  # signature per current code
    assert rej is None  # ORG-mode accepted, no rejection


def test_sitetemplate_plan_classified_org():
    assert check_objects(_org_plan("sitetemplate")) is None
```

Note: match `check_objects`' real signature/return — read `object_gate.py` first and adapt the test to its parse contract (it may take a parsed `ChangePlan`). The behavioral assertion is "a single-op gatewaytemplate/sitetemplate plan with no site_id classifies ORG, not rejected as unsupported."

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scope/test_object_gate.py -q`
Expected: FAIL — gatewaytemplate/sitetemplate falls through to the non-ORG path and is rejected.

- [ ] **Step 3: Write minimal implementation** — at `object_gate.py:31`, change the ORG-mode predicate from the literal to the tuple:

```python
# was: all(op.object_type == "networktemplate" for op in plan.ops)
all(op.object_type in ORG_OBJECT_TYPES for op in plan.ops)
```
Add `from digital_twin.scope.allowlist import ORG_OBJECT_TYPES` if not already imported. At `:41`, generalize the single-id rejection message from "multiple networktemplate ids" to "multiple template ids in one org plan".

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scope/test_object_gate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope/object_gate.py tests/scope/test_object_gate.py
git commit -m "feat: object_gate ORG-mode recognizes all ORG_OBJECT_TYPES

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Phase 1 gate:** `uv run pytest tests -q && uv run ruff check . && uv run mypy src` → all green.

---

## Phase 2 — sitetemplate layer + provider fetch

### Task 5: `RawSiteState` gains `sitetemplate` / `gatewaytemplate`; `resolve_org_template` typed

**Files:**
- Modify: `src/digital_twin/providers/base.py`
- Test: `tests/providers/test_base.py` (or the nearest existing provider test)

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/test_base.py  (add)
from digital_twin.providers.base import RawSiteState


def test_rawsitestate_has_new_template_fields():
    fields = RawSiteState.__dataclass_fields__
    assert "sitetemplate" in fields and "gatewaytemplate" in fields
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_base.py -q`
Expected: FAIL — fields absent.

- [ ] **Step 3: Write minimal implementation** — in `base.py`, add two defaulted fields to `RawSiteState`. **They MUST go at the TRAILING/defaulted section, AFTER `org_networks: tuple[...] = ()`** — the fields after `networktemplate` (`devices`, `device_stats`, …) are *required* (no default), so placing a defaulted field there raises `TypeError: non-default argument follows default argument`. Also change the `StateProvider.resolve_org_template` protocol signature to `(self, scope: OrgScope, template_id: str, object_type: str) -> OrgTemplateContext | FetchError`.

```python
# in RawSiteState, AFTER the existing `org_networks: tuple[JsonObj, ...] = ()`:
# assigned sitetemplate / gatewaytemplate bodies (None = not assigned/not fetched).
# Trailing + defaulted so every existing constructor/fixture stays valid.
sitetemplate: JsonObj | None = None
gatewaytemplate: JsonObj | None = None
```

(`observability/replay/store.py:load_fixture_doc` constructs `RawSiteState` — Task 18 adds these two fields there with `.get(...)` back-compat.)

- [ ] **Step 4: Run test + suite to catch the protocol change**

Run: `uv run pytest tests/providers/test_base.py -q && uv run mypy src`
Expected: test PASS. mypy will flag the existing `resolve_org_template` impls (mist_api, FixtureProvider, _RecordingProvider) — those are updated in Tasks 6 / 19 / 17. For now keep the suite green by threading `object_type` through in the same commit if mypy fails; otherwise defer with a typed stub that ignores it. Prefer threading it now (it's a small signature change).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/providers/base.py tests/providers/test_base.py
git commit -m "feat: RawSiteState +sitetemplate +gatewaytemplate; typed resolve_org_template

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 6: `mist_api` typed `resolve_org_template` + per-site sitetemplate/gatewaytemplate fetch

**Files:**
- Modify: `src/digital_twin/providers/mist_api.py`
- Test: `tests/providers/test_mist_api.py` (mock the SDK as the existing tests do)

- [ ] **Step 1: Write the failing test** — `resolve_org_template(scope, id, "gatewaytemplate")` filters sites by `gatewaytemplate_id` and fetches the gatewaytemplate; the per-site fetch populates `sitetemplate`/`gatewaytemplate` from the site's `*_id`. Mirror the existing mist_api test's mocking style (read the file first; reuse its `_FakeSession`/recorded-response helper).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_mist_api.py -q`
Expected: FAIL — `resolve_org_template` ignores `object_type` / new fields unset.

- [ ] **Step 3: Write minimal implementation** — generalize `resolve_org_template` to map `object_type` → the site id-field (`{"networktemplate":"networktemplate_id","gatewaytemplate":"gatewaytemplate_id","sitetemplate":"sitetemplate_id"}`) and the org endpoint for that template type; filter `_org_sites` by that id field; fetch the template (lookup-fail → `FetchError`). In the per-site fetch, after resolving the site doc, fetch the assigned `sitetemplate` (by `sitetemplate_id`) and `gatewaytemplate` (by `gatewaytemplate_id`) and set them on `RawSiteState`; a fetch failure for an assigned one → record it as a `FetchError` for that site.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/providers/test_mist_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/providers/mist_api.py tests/providers/test_mist_api.py
git commit -m "feat: mist_api typed resolve_org_template + per-site sitetemplate/gatewaytemplate fetch

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 7: Switch compile threads the sitetemplate layer

**Files:**
- Modify: `src/digital_twin/adapters/mist/compile/switch.py` (`merge_only`, `compile_site`, `compile_device`)
- Modify: `src/digital_twin/adapters/mist/adapter.py` (pass `raw.sitetemplate`)
- Test: `tests/adapters/mist/compile/test_switch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/compile/test_switch.py  (add)
from digital_twin.adapters.mist.compile.switch import compile_site


def test_compile_site_includes_sitetemplate_layer():
    nt = {"networks": {"corp": {"vlan_id": 10}}}
    st = {"networks": {"corp": {"vlan_id": 20}}}
    ss = {"networks": {}}
    assert compile_site(nt, ss, sitetemplate=st)["networks"]["corp"]["vlan_id"] == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/compile/test_switch.py -q`
Expected: FAIL — `compile_site() got an unexpected keyword argument 'sitetemplate'`.

- [ ] **Step 3: Write minimal implementation** — give `merge_only` an optional `sitetemplate=None` and call `merge_site_effective(nt, ss, sitetemplate=sitetemplate)`; thread `sitetemplate` through `compile_site` and `compile_device` (their `merge_only` calls). In `adapter.py`'s `ingest`, pass `sitetemplate=raw.sitetemplate` to `compile_site`/`compile_device`.

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/adapters/mist/compile/test_switch.py -q && uv run pytest tests -q`
Expected: PASS (baseline-gap fix is behaviour-preserving when `sitetemplate is None`).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: switch compile threads the sitetemplate layer (fixes latent baseline gap)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Phase 2 gate:** full gate green.

---

## Phase 3 — gateway compile + role-keyed derived gate + DHCP-row helper

### Task 8: `compile_gateway_device`

**Files:**
- Create: `src/digital_twin/adapters/mist/compile/gateway.py`
- Test: `tests/adapters/mist/compile/test_gateway.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/compile/test_gateway.py
from digital_twin.adapters.mist.compile.gateway import compile_gateway_device


def test_fold_then_device_overlay_then_vars_last():
    gt = {"vars": {"GW": "10.0.0.1"}, "ip_configs": {"corp": {"ip": "{{GW}}"}},
          "port_config": {"ge-0/0/0": {"networks": ["corp"]}}}
    st = None
    ss = {}
    device = {"port_config": {"ge-0/0/1": {"networks": ["guest"]}}}
    eff = compile_gateway_device(gt, st, ss, device)
    # device port added (DICT_MERGE), template port kept (not wiped)
    assert set(eff["port_config"]) == {"ge-0/0/0", "ge-0/0/1"}
    # vars resolved LAST, after the device overlay
    assert eff["ip_configs"]["corp"]["ip"] == "10.0.0.1"


def test_sitetemplate_one_port_does_not_wipe_template_ports():
    gt = {"port_config": {"a": {"networks": ["x"]}, "b": {"networks": ["y"]}}}
    st = {"port_config": {"a": {"networks": ["z"]}}}
    eff = compile_gateway_device(gt, st, {}, {})
    assert set(eff["port_config"]) == {"a", "b"}      # DICT_MERGE, b survives
    assert eff["port_config"]["a"]["networks"] == ["z"]  # sitetemplate wins for a


def test_gateway_with_no_port_config_does_not_crash():
    # a gateway with no inherited/device port_config must compile cleanly (the
    # materialization helper reads eff.get("port_config", {}))
    eff = compile_gateway_device({"ip_configs": {"corp": {"ip": "10.0.0.1"}}}, None, {}, {})
    assert "port_config" not in eff or eff["port_config"] == {}
    assert eff["ip_configs"]["corp"]["ip"] == "10.0.0.1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/compile/test_gateway.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# src/digital_twin/adapters/mist/compile/gateway.py
"""Gateway device effective config: fold the gateway stack, overlay the device
PER-KEY (NOT root-replace), then resolve {{vars}} LAST (matching compile_device).

GATEWAY_POLICY DICT_MERGEs the keyed maps port_config/ip_configs/dhcpd_config so a
higher layer (and the device overlay) that sets one port/scope does not wipe the
rest. The device overlay is just another fold layer with the SAME policy — so the
keyed maps merge per key and device-own roots REPLACE. (Do NOT use
`effective_update`: it is a root-level merge — `device["port_config"]` would
replace the inherited `port_config` wholesale, wiping template ports. That is the
exact behaviour the test forbids; the switch path mirrors this with
`_DEVICE_DICT_MERGE_FIELDS`, not root-replace.) Exact Mist layering for these maps
is Tier-2 live-verified (starting from DICT_MERGE)."""

from __future__ import annotations

from typing import Any

from .fold import MergePolicy, PolicyTable, fold_layers
from .switch import _resolve  # vars substitution, resolve-last

JsonObj = dict[str, Any]

GATEWAY_POLICY: PolicyTable = {
    "networks": MergePolicy.DICT_MERGE,
    "vars": MergePolicy.DICT_MERGE,
    "dhcpd_config": MergePolicy.DICT_MERGE,
    "port_config": MergePolicy.DICT_MERGE,
    "ip_configs": MergePolicy.DICT_MERGE,
}


def compile_gateway_device(
    gatewaytemplate: JsonObj | None,
    sitetemplate: JsonObj | None,
    site_setting: JsonObj,
    device: JsonObj,
) -> JsonObj:
    site_effective = fold_layers(
        [gatewaytemplate, sitetemplate, site_setting], GATEWAY_POLICY
    )
    # device overlay = one more fold layer under the same policy: keyed maps merge
    # per key (device port adds, template ports survive), device-own roots replace.
    overlaid = fold_layers([site_effective, device], GATEWAY_POLICY)
    return _resolve(overlaid)
```

Note: the proposed gatewaytemplate edit (with any `{"-attr":""}` delete-markers) is already applied via `apply_template` BEFORE this fold, so the device overlay needs no delete-marker handling — a fetched device dict carries no markers. The fold-based overlay carries the device's identity roots (`mac`/`id`) through REPLACE.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/adapters/mist/compile/test_gateway.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/compile/gateway.py tests/adapters/mist/compile/test_gateway.py
git commit -m "feat: compile_gateway_device (fold -> device overlay -> _resolve last)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 9: Materialize gateway effective into `RawSiteState.devices` + expose gateway-effective map

**Files:**
- Modify: `src/digital_twin/adapters/mist/adapter.py`
- Test: `tests/adapters/mist/test_adapter.py`

- [ ] **Step 1: Write the failing test** — after `ingest`, a `type=="gateway"` device's `port_config`/`ip_configs`/`dhcpd_config` reflect the folded effective (a gatewaytemplate-supplied port shows up on the device read by the ingest); switch/AP device entries are untouched; and `IngestOutcome` exposes a gateway-effective map keyed by `device_id(mac)`.

```python
# tests/adapters/mist/test_adapter.py  (add — adapt construction to raw_site helper)
def test_gateway_effective_materialized_and_exposed(raw_gateway_state):
    out = MistAdapter().ingest(raw_gateway_state)
    assert out.gateway_effective                      # new map, keyed by device id
    # the materialized device the gateway ingest reads carries the folded ports
    # (assert via the resulting Port/L3Intf IR, or via the exposed map)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_adapter.py -q`
Expected: FAIL — `gateway_effective` absent.

- [ ] **Step 3: Write minimal implementation** — in `adapter.py`:
  - Add `gateway_effective: dict[str, _Json]` to `IngestOutcome` (default `{}`).
  - In `ingest`, build, for each `type == "gateway"` device **with a mac**, `eff = compile_gateway_device(raw.gatewaytemplate, raw.sitetemplate, raw.setting, dict(dev))`, key it `device_id(str(dev["mac"]))`. A gateway lacking `mac` is dropped (consistent with ingest's existing skip).
  - Materialize: build the `devices` tuple passed to ingest with each gateway device's modeled leaves replaced by the effective ones — `dc_replace`-style, do NOT mutate (RawSiteState is frozen). Concretely: construct `materialized_devices = tuple(_with_gateway_effective(d, gateway_effective) for d in raw.devices)` where the helper, for a `type == "gateway"` device **with a mac**, returns `{**d, **{k: eff.get(k, {}) for k in ("port_config", "ip_configs", "dhcpd_config")}}` (use `eff.get(..., {})` for **every** modeled map — a gateway may have no inherited/device `port_config`, so `eff["port_config"]` would `KeyError`; modeled leaves only, not `networks`), else returns `d` unchanged (mac-less gateway dropped from both the map and materialization — uniform blind spot). Run the gateway ingest over `materialized_devices`.
  - Expose both `device_effective` (switch, unchanged) and the new `gateway_effective` on `IngestOutcome`.

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/adapters/mist/test_adapter.py -q && uv run pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: materialize gateway effective into devices + expose gateway_effective map

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 10: The shared DHCP-row-relevance helper

**Files:**
- Create: `src/digital_twin/scope/dhcp_screen.py`
- Test: `tests/scope/test_dhcp_screen.py`

- [ ] **Step 1: Write the failing test** — the full 3×3 participation matrix + the two inert-leaf screens (from the spec). Parametrize all nine (S/R/I)×(S/R/I) transitions plus the inert-leaf cases.

```python
# tests/scope/test_dhcp_screen.py
import pytest
from digital_twin.scope.dhcp_screen import dhcp_row_rejection

S = {"type": "local", "ip_start": "10.0.0.10", "ip_end": "10.0.0.99"}
Rx = {"type": "relay", "servers": ["10.1.1.1"]}      # active relay, target x
Ry = {"type": "relay", "servers": ["10.2.2.2"]}      # active relay, target y
I = {"type": "none"}                                  # inactive
Rempty = {"type": "relay", "servers": []}             # inactive relay


@pytest.mark.parametrize("base,prop,stage", [
    (S, dict(Rx), "dhcp_mode_transition"),       # S->R : serving -> active-relay
    (dict(Rx), S, "dhcp_mode_transition"),       # R->S : active-relay -> serving
    (dict(Rx), dict(Ry), "dhcp_relay_target"),   # R->R differing servers
])
def test_participation_unknown_cells(base, prop, stage):
    rej = dhcp_row_rejection(base, prop)
    assert rej is not None and rej.stage == stage


@pytest.mark.parametrize("base,prop", [
    (S, dict(S)),                 # S->S same -> allowed
    (dict(Rx), dict(Rx)),         # R->R same servers -> allowed
    (S, dict(I)),                 # S->I  -> dhcp_path loss (REVIEW), not rejected here
    (dict(I), dict(Rx)),          # I->R  -> provider gain, allowed
    ({**S, "ip_start": "10.0.0.10"}, {**S, "ip_start": "10.0.0.10"}),  # no change
])
def test_participation_allowed_cells(base, prop):
    assert dhcp_row_rejection(base, prop) is None


def test_empty_servers_exemption_is_not_preempted():
    # local,["x"] -> relay,[] : S->I, >=1 inactive -> NOT rejected (stays dhcp_path)
    base = {"type": "local", "servers": ["10.1.1.1"], "ip_start": "10.0.0.10"}
    prop = {"type": "relay", "servers": []}
    assert dhcp_row_rejection(base, prop) is None


def test_inert_servers_on_both_serving_is_unknown():
    base = {"type": "local", "servers": ["10.1.1.1"], "ip_start": "10.0.0.10"}
    prop = {"type": "local", "servers": ["10.2.2.2"], "ip_start": "10.0.0.10"}
    rej = dhcp_row_rejection(base, prop)
    assert rej is not None and rej.stage == "dhcp_inert_servers"


def test_inert_scope_field_on_both_non_serving_is_unknown():
    base = {"type": "relay", "servers": [], "gateway": "10.0.0.1"}
    prop = {"type": "relay", "servers": [], "gateway": "10.9.9.9"}
    rej = dhcp_row_rejection(base, prop)
    assert rej is not None and rej.stage == "dhcp_scope_field"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scope/test_dhcp_screen.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation** — reuse the ingest's pure predicates so the helper stays row-local and consistent with the model.

```python
# src/digital_twin/scope/dhcp_screen.py
"""Row-level DHCP-relevance screen for the derived gate. Pure row-local rule via
the same predicates the ingest uses (_dhcp_active / _dhcp_serves_scope) — never a
check's output (the derived gate runs before checks). Complete rejection set
(UNKNOWN if ANY): (1) inert servers on a row serving on BOTH sides (S->S);
(2) participation/target — both sides active and the relay-target identity differs
(exactly one active relay -> dhcp_mode_transition; both active relays, differing
servers -> dhcp_relay_target); (3) inert range/gateway while both sides non-serving
-> dhcp_scope_field. See the 3x3 matrix in the design spec."""

from __future__ import annotations

from typing import Any

from digital_twin.adapters.mist.ingest.switch import _dhcp_active, _dhcp_serves_scope
from digital_twin.contracts import Rejection

JsonObj = dict[str, Any]
_SCOPE_FIELDS = ("ip_start", "ip_end", "gateway")


def _is_active_relay(row: JsonObj) -> bool:
    return _dhcp_active(row) and str((row or {}).get("type") or "local") == "relay"


def dhcp_row_rejection(base: JsonObj, prop: JsonObj) -> Rejection | None:
    base, prop = base or {}, prop or {}
    serves_b, serves_p = _dhcp_serves_scope(base), _dhcp_serves_scope(prop)
    active_b, active_p = _dhcp_active(base), _dhcp_active(prop)

    # (1) inert servers — serving on BOTH sides, servers changed
    if serves_b and serves_p and base.get("servers") != prop.get("servers"):
        return Rejection(stage="dhcp_inert_servers",
                         reasons=("servers changed on a serving row (inert)",))

    # (2) participation/relay-target — both active, target identity differs
    if active_b and active_p:
        ar_b, ar_p = _is_active_relay(base), _is_active_relay(prop)
        if ar_b != ar_p:
            return Rejection(stage="dhcp_mode_transition",
                             reasons=("serving<->active-relay; relay target unmodeled",))
        if ar_b and ar_p and base.get("servers") != prop.get("servers"):
            return Rejection(stage="dhcp_relay_target",
                             reasons=("active relay target changed (unmodeled)",))

    # (3) inert scope-fact — both sides non-serving, range/gateway changed
    if not serves_b and not serves_p and any(
        base.get(f) != prop.get(f) for f in _SCOPE_FIELDS
    ):
        return Rejection(stage="dhcp_scope_field",
                         reasons=("range/gateway changed on a non-serving row (inert)",))
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scope/test_dhcp_screen.py -q`
Expected: PASS (all matrix + inert-leaf cases).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope/dhcp_screen.py tests/scope/test_dhcp_screen.py
git commit -m "feat: shared row-level DHCP-relevance helper (3x3 matrix + inert-leaf screens)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 11: Role-keyed `check_derived` + invoke the DHCP-row helper on every `dhcpd_config.*` row

**Files:**
- Modify: `src/digital_twin/scope/derived_gate.py`
- Test: `tests/scope/test_derived_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scope/test_derived_gate.py  (add)
from digital_twin.scope.allowlist import GATEWAY_EFFECTIVE_ALLOWLIST
from digital_twin.scope.derived_gate import check_derived


def test_role_keyed_allowlist_param():
    # gateway disabled flip is in GATEWAY_EFFECTIVE_ALLOWLIST -> NOT rejected by path
    base = {"port_config": {"a": {"disabled": False}}}
    prop = {"port_config": {"a": {"disabled": True}}}
    assert check_derived(base, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST) is None


def test_dhcp_row_screen_runs_inside_check_derived():
    # an effective dhcpd row transition that the row helper rejects -> UNKNOWN,
    # even though dhcpd_config.*.* paths are allowlisted
    base = {"dhcpd_config": {"n": {"type": "local", "servers": ["a"], "ip_start": "1"}}}
    prop = {"dhcpd_config": {"n": {"type": "relay", "servers": ["a"]}}}
    rej = check_derived(base, prop, allowlist=GATEWAY_EFFECTIVE_ALLOWLIST)
    assert rej is not None and rej.stage == "dhcp_mode_transition"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scope/test_derived_gate.py -q`
Expected: FAIL — `check_derived()` has no `allowlist` kwarg and does no row screen.

- [ ] **Step 3: Write minimal implementation** — in `derived_gate.py`:
  - Add `allowlist: tuple[str, ...] = EFFECTIVE_ALLOWLIST` keyword param to `check_derived`; use it in the `allowed(path, allowlist)` call (keep the default = switch allowlist so existing site/switch-device calls are unchanged).
  - After the path-membership check (and regardless of whether any path was out of scope — the value screen is independent), iterate the union of `dhcpd_config` keys in baseline/proposed; for each row call `dhcp_row_rejection(base_row, prop_row)`; the first non-None → return it. Return the path-membership rejection first if present, else the first row rejection, else None.

```python
# sketch inside check_derived (after computing `offending`)
from digital_twin.scope.dhcp_screen import dhcp_row_rejection
if offending:
    return Rejection(stage=_STAGE, reasons=(...))  # existing
b_dhcp = (baseline.get("dhcpd_config") or {})
p_dhcp = (proposed.get("dhcpd_config") or {})
for name in sorted(set(b_dhcp) | set(p_dhcp)):
    rej = dhcp_row_rejection(b_dhcp.get(name) or {}, p_dhcp.get(name) or {})
    if rej is not None:
        return rej
return None
```

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/scope/test_derived_gate.py -q && uv run pytest tests -q`
Expected: PASS. (The site/switch-device calls keep the default `EFFECTIVE_ALLOWLIST`; their dhcpd rows now also get the row screen — this is the intended switch/site tightening. If a pre-existing golden flips SAFE→UNKNOWN because it edited a serving-row `servers` or a both-non-serving range, that is the correct new behaviour; update the golden + note it.)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: role-keyed check_derived + DHCP-row screen inside the derived gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 12: Wire the gateway-effective map into the derived gate in the pipeline

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` (`_simulate_site_state`, the derived-gate stage ~line 139)
- Test: `tests/engine/test_pipeline_gateway_derived.py`

- [ ] **Step 1: Write the failing test** — FOUR assertions: (1) a gatewaytemplate edit rippling (via `vars`) into an effective gateway leaf **outside `GATEWAY_EFFECTIVE_ALLOWLIST`** (e.g. `ip_configs.*.netmask`) → per-site UNKNOWN with a `derived_gate` rejection; (2) a benign gateway `vars` edit landing only in in-scope leaves is NOT rejected on the `vars.*` path; (3) **a `sitetemplate.networks.*` edit on a site that also has a gateway is NOT rejected by the GATEWAY derived gate** (the switch derived gate owns `networks`; the gateway namespace is `org_networks`, so `networks` is projected away on the gateway side — review P2); (4) **a `gatewaytemplate.vars.*` edit that resolves INTO `gatewaytemplate.networks.*` → UNKNOWN** (gatewaytemplate's own `networks` is NOT in site_effective, so the switch gate never sees it; the gateway gate screens FULL for gatewaytemplate edits — review P1, a false-SAFE otherwise). Use the FixtureProvider/builders path (read `tests/golden/builders.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_pipeline_gateway_derived.py -q`
Expected: FAIL — gateway effective is not diffed by the derived gate; and `networks` leaks into the gateway screen.

- [ ] **Step 3: Write minimal implementation** — `compile_gateway_device` keeps switch-surface roots (`networks`, `port_usages`, …) that the shared `sitetemplate`/`site_setting` layers carry, but the gateway IR consumes only `port_config`/`ip_configs`/`dhcpd_config` (+ `vars` for the ripple). **The gateway derived gate must screen ONLY those roots (was a P2 false-UNKNOWN):** diffing the full `gateway_effective` against `GATEWAY_EFFECTIVE_ALLOWLIST` (which excludes `networks`) would UNKNOWN a valid `sitetemplate.networks.*` edit that the SWITCH derived gate already handles. Define the screened-root set and project before the gateway diff:

```python
# in pipeline.py (module-level)
GATEWAY_SCREENED_ROOTS = ("port_config", "ip_configs", "dhcpd_config", "vars")

def _gw_screen_view(eff: dict, *, full: bool) -> dict:
    # SOURCE-AWARE (review P1). For a `gatewaytemplate` edit, screen the FULL
    # effective: gatewaytemplate's OWN roots (e.g. networks, or a vars edit
    # rippling into networks) are NOT in site_effective, so the switch derived
    # gate never sees them — dropping them here would resolve a gatewaytemplate
    # networks/vars-into-networks change SAFE (false-SAFE). For a sitetemplate/
    # site_setting edit, project to the gateway-consumed roots: a networks change
    # there IS in site_effective and the switch gate owns it (the gateway
    # namespace is org_networks), so screening it here would false-UNKNOWN.
    return eff if full else {k: eff[k] for k in GATEWAY_SCREENED_ROOTS if k in eff}
```

In `_simulate_site_state`, after `proposed = adapter.ingest(proposed_raw)`, in addition to the existing site + switch-`device_effective` `check_derived` calls, iterate the gateway map: for each `did` in `set(baseline.gateway_effective) | set(proposed.gateway_effective)`, call `check_derived(_gw_screen_view(baseline.gateway_effective.get(did, {}), full=gateway_screen_full), _gw_screen_view(proposed.gateway_effective.get(did, {}), full=gateway_screen_full), allowlist=GATEWAY_EFFECTIVE_ALLOWLIST)`; the first non-None rejection → `_unknown(rejection, ...)`. Import `GATEWAY_EFFECTIVE_ALLOWLIST`. Add a `gateway_screen_full: bool` kwarg to `_simulate_site_state`: the org fan-out passes `gateway_screen_full=(object_type == "gatewaytemplate")`; single-site `simulate` passes `False` (single-site edits are `site_setting`/`device`, never `gatewaytemplate`). (Note: `gateway_effective` stays FULL on `IngestOutcome` — `affected_device_ids` (Task 14) needs `port_usages`/`networks` to resolve references; only the derived-gate *input* is conditionally projected.)

The Step 1 test (3) must therefore distinguish: a **sitetemplate**`.networks.*` edit → NOT rejected by the gateway gate (projected away); a **gatewaytemplate**`.vars.*` edit that resolves into `gatewaytemplate.networks.*` → **UNKNOWN** (screened full).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_pipeline_gateway_derived.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: derived gate screens gateway effective (vars/override ripple no longer gateway-blind)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Phase 3 gate:** full gate green.

---

## Phase 4 — device-profile post-ingest gate

### Task 13: `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` + the relevance gate

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py` (add the map)
- Create: `src/digital_twin/scope/device_profile_gate.py`
- Test: `tests/scope/test_device_profile_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/scope/test_device_profile_gate.py
from digital_twin.scope.device_profile_gate import device_profile_rejection


def _ctx(devices, changed_leaves, device_effective, gateway_effective):
    # build the minimal context the gate needs; adapt to the real signature
    ...


def test_profiled_gateway_with_affected_leaf_rejects():
    rej = device_profile_rejection(
        devices=[{"type": "gateway", "mac": "m1", "deviceprofile_id": "p1"}],
        changed_leaves=("port_config.ge-0/0/0.disabled",),
        affected_device_ids={"m1"},  # the edit participates in m1's effective path
    )
    assert rej is not None and rej.stage == "device_profile_gate"


def test_ap_profile_does_not_taint():
    rej = device_profile_rejection(
        devices=[{"type": "ap", "mac": "m2", "deviceprofile_id": "p2"}],
        changed_leaves=("port_config.ge-0/0/0.disabled",),
        affected_device_ids=set(),
    )
    assert rej is None


def test_unaffected_modeled_device_does_not_taint():
    rej = device_profile_rejection(
        devices=[{"type": "gateway", "mac": "m1", "deviceprofile_id": "p1"}],
        changed_leaves=("dhcpd_config.n.gateway",),
        affected_device_ids=set(),   # edit does not touch m1's path
    )
    assert rej is None


def test_mac_normalized_and_changed_leaf_wildcard_matched():
    # affected_device_ids is device_id-keyed (normalized: lower, no colons), and
    # a concrete changed leaf must match the role's *.disabled pattern. This proves
    # the gate normalizes dev["mac"] via device_id() and uses allowed() wildcards,
    # NOT raw-string / exact-tuple comparison.
    rej = device_profile_rejection(
        devices=[{"type": "switch", "mac": "AA:BB:CC:DD:EE:FF", "deviceprofile_id": "p1"}],
        changed_leaves=("port_config.ge-0/0/0.disabled",),
        affected_device_ids={"aabbccddeeff"},
    )
    assert rej is not None and rej.stage == "device_profile_gate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scope/test_device_profile_gate.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation** — add to `allowlist.py`:
```python
# Modeled leaves a device-profile (higher precedence, unmodeled layer) can
# override, per role. Used by the device-profile coverage gate.
DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "gateway": (*_GATEWAY_LEAVES,),
    "switch": (*_NETWORK_LEAVES, *_USAGE_LEAVES, *_DEVICE_PORT_LEAVES, *_DHCP_LEAVES),
}
```
Then `device_profile_gate.py`: the gate takes the in-scope devices, the changed effective leaves (by dot-path), and the set of device ids the edit actually affects. It rejects (`Rejection(stage="device_profile_gate", reasons=(...))`) iff some device of role ∈ {switch, gateway} has a `deviceprofile_id`, is in `affected_device_ids`, AND a changed leaf matches that role's overridable set. **Two precision requirements (review P2):**
  - **Normalize device ids the SAME way the effective maps are keyed** — `device_effective`/`gateway_effective` are keyed by `device_id(str(dev["mac"]))` (`ir/entities.py:device_id`), so the gate must compute `device_id(str(dev["mac"]))` for each raw device before comparing against `affected_device_ids` (never compare raw colon/dash MAC strings directly — they won't match). Import `from digital_twin.ir.entities import device_id`.
  - **Match changed leaves with WILDCARD patterns, not exact tuple membership** — a changed leaf is a concrete path like `port_config.ge-0/0/0.disabled`; the overridable set holds patterns like `port_config.*.disabled`. Use `from digital_twin.scope.paths import allowed` → `allowed(changed_leaf, DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE[role])` (NOT `changed_leaf in <tuple>`).

  AP and unaffected devices never taint. The caller (Task 14) computes `affected_device_ids` already as `device_id`-keyed (from the effective maps), so both sides use the same key space.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scope/test_device_profile_gate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: device-profile coverage gate (relevance-scoped Rejection -> UNKNOWN)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 14: Hook the device-profile gate into `_simulate_site_state` (post-ingest)

**Files:**
- Modify: `src/digital_twin/engine/pipeline.py` — `_simulate_site_state` (new kwargs `profile_proposed: IngestOutcome | None = None` for the below-profile gate input, and `gateway_screen_full: bool = False` for the source-aware gateway derived gate; the `rejections=()` site at ~line 167) AND both call sites: `simulate` (~line 326 — build the below-profile-only `profile_proposed` when the plan has device ops; `gateway_screen_full=False`) and the org fan-out (~line 453 — `profile_proposed=None`; `gateway_screen_full=(object_type == "gatewaytemplate")`).
- Modify: `src/digital_twin/scope/device_profile_gate.py` (the `affected_device_ids` helper from Task 13)
- Test: `tests/scope/test_affected_devices.py`, `tests/engine/test_pipeline_device_profile.py`

- [ ] **Step 1: Write the failing test** — TWO test files.

  (a) `tests/scope/test_affected_devices.py` — the referenced-path helper. An **unused** `port_usages`/`networks` change must NOT taint; only a device that actually references the changed key is affected (review P2 — shared site/template maps appear in every switch effective, so "the effective carries the changed map" over-taints):

```python
# tests/scope/test_affected_devices.py
from digital_twin.scope.device_profile_gate import affected_device_ids

# device d1 has a port using usage "foo" and network "corp"; d2 uses neither.
# d1's port uses profile "foo"; the profile (port_usages.foo) references network
# "corp" -> the network is referenced INDIRECTLY through the usage (the helper must
# resolve it, not only read raw port_config attrs). d2 uses "bar"/"guest".
D1 = {"port_config": {"ge-0/0/0": {"usage": "foo"}}, "port_usages": {"foo": {"networks": ["corp"]}}}
D2 = {"port_config": {"ge-0/0/1": {"usage": "bar"}}, "port_usages": {"bar": {"networks": ["guest"]}}}
EFF = {"d1": D1, "d2": D2}


def test_unused_port_usage_change_taints_nobody():
    assert affected_device_ids(("port_usages.baz.mode",), EFF, EFF) == set()


def test_referenced_port_usage_taints_only_referencing_device():
    assert affected_device_ids(("port_usages.foo.mode",), EFF, EFF) == {"d1"}


def test_unused_network_change_taints_nobody():
    assert affected_device_ids(("networks.iot.vlan_id",), EFF, EFF) == set()


def test_network_referenced_THROUGH_usage_taints_device():
    # networks.corp reached via port_usages.foo -> d1 affected even though no
    # port lists "corp" directly (review P2 — must resolve, not read raw attrs)
    assert affected_device_ids(("networks.corp.vlan_id",), EFF, EFF) == {"d1"}


def test_owned_port_config_key_taints_its_device():
    assert affected_device_ids(("port_config.ge-0/0/1.disabled",), EFF, EFF) == {"d2"}


def test_port_config_RANGE_key_matches_expanded_members():
    # Mist supports range/list port keys; the changed key "ge-0/0/0-1" must match a
    # device whose resolved members include ge-0/0/0 (review P2 — exact match would
    # miss it; _device_ports holds expanded members).
    dev = {"d": {"port_config": {"ge-0/0/0": {"usage": "foo"}}, "port_usages": {"foo": {}}}}
    assert affected_device_ids(("port_config.ge-0/0/0-1.disabled",), dev, dev) == {"d"}


def test_added_reference_in_proposed_only_taints_device():
    # baseline port references guest; proposed switches it to corp. A networks.corp
    # edit affects the device via the PROPOSED side (review P2 — baseline-only misses
    # newly-introduced references).
    base = {"d": {"port_config": {"p": {"networks": ["guest"]}}}}
    prop = {"d": {"port_config": {"p": {"networks": ["corp"]}}}}
    assert affected_device_ids(("networks.corp.vlan_id",), base, prop) == {"d"}


def test_removed_reference_in_baseline_only_taints_device():
    # symmetric: baseline referenced corp, proposed removed it -> still affected
    base = {"d": {"port_config": {"p": {"networks": ["corp"]}}}}
    prop = {"d": {"port_config": {"p": {"networks": ["guest"]}}}}
    assert affected_device_ids(("networks.corp.vlan_id",), base, prop) == {"d"}
```

  (b) `tests/engine/test_pipeline_device_profile.py` — end-to-end: (i) a per-site **template/site** edit where a modeled gateway/switch carries `deviceprofile_id` and the edit changes an overridable, **referenced** leaf → site verdict UNKNOWN via `device_profile_gate`; (ii) an AP-profile-only site, or an **unused** `port_usages` edit, → not tainted; (iii) **a single-site `device` plan on a profiled device → NOT tainted** (review P2 — `device` is above the profile in precedence; `simulate` passes `apply_device_profile_gate=False` for an all-`device` plan). Verdict per the real checks for the non-tainted cases.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scope/test_affected_devices.py tests/engine/test_pipeline_device_profile.py -q`
Expected: FAIL — `affected_device_ids` missing; no rejection injected.

- [ ] **Step 3: Write minimal implementation** — add the referenced-path helper to `device_profile_gate.py`, then hook it into the pipeline.

```python
# src/digital_twin/scope/device_profile_gate.py  (add)
from digital_twin.adapters.mist.ingest.ports import (
    expand_port_members,
    resolve_effective_ports,
)

# Keyed maps whose changed KEY is a name referenced INDIRECTLY (a resolved port,
# IP-config, or DHCP scope must point at it). port_config/local_port_config/
# ip_configs/dhcpd_config keys are owned per-device directly.


def _resolved_ports(eff: dict) -> list:
    # resolve_effective_ports layers port_config + local_port_config +
    # port_config_overwrite onto the named port_usages profile, so a network
    # referenced via port_usages.foo.{networks,port_network} (NOT directly on the
    # port) is surfaced. _USAGE_OVERRIDE_ATTRS includes networks/port_network, so
    # inline gateway ports (no usage) are surfaced the same way — uniform.
    return list(resolve_effective_ports(eff or {}))


def _device_ports(eff: dict) -> set[str]:
    return {m for m, _u, _un, _r in _resolved_ports(eff)}


def _device_usages(eff: dict) -> set[str]:
    return {un for _m, _u, un, _r in _resolved_ports(eff) if un}


def _device_networks(eff: dict) -> set[str]:
    nets: set[str] = set()
    for _m, usage, _un, _r in _resolved_ports(eff):
        usage = usage or {}
        nets.update(str(n) for n in (usage.get("networks") or []))
        if usage.get("port_network"):
            nets.add(str(usage["port_network"]))
    # gateway ip_configs/dhcpd_config keys ARE network names
    nets.update(str(k) for k in ((eff or {}).get("ip_configs") or {}))
    nets.update(str(k) for k in ((eff or {}).get("dhcpd_config") or {}))
    return nets


def _references_all_networks(eff: dict) -> bool:
    return any((u or {}).get("all_networks") for _m, u, _un, _r in _resolved_ports(eff))


def affected_device_ids(
    changed_leaves: tuple[str, ...],
    baseline_eff: dict[str, dict],
    proposed_eff: dict[str, dict],
) -> set[str]:
    """*_eff: device_id -> effective dict (switch device_effective + gateway_effective)
    for that snapshot. A device is affected by a changed leaf iff it RESOLVES a
    reference to the changed keyed-map key in EITHER baseline OR proposed (so an
    edit that ADDS or REMOVES the reference still counts) — NOT merely carries the
    map. Unused port_usages/networks changes taint nobody (review P2)."""
    out: set[str] = set()
    dids = set(baseline_eff) | set(proposed_eff)
    for leaf in changed_leaves:
        parts = leaf.split(".")
        if len(parts) < 2:
            continue
        mp, key = parts[0], parts[1]
        for did in dids:
            be, pe = baseline_eff.get(did) or {}, proposed_eff.get(did) or {}
            if mp == "port_usages":
                hit = key in _device_usages(be) or key in _device_usages(pe)
            elif mp == "networks":
                hit = (
                    key in _device_networks(be) or key in _device_networks(pe)
                    or _references_all_networks(be) or _references_all_networks(pe)
                )
            elif mp in ("port_config", "local_port_config"):
                # the changed key may be a RANGE/LIST (e.g. "ge-0/0/0-3" or
                # "ge-0/0/0,ge-0/0/2"); _device_ports holds EXPANDED members, so
                # expand the key and intersect (review P2 — exact match misses
                # range/list keys Mist supports).
                members = set(expand_port_members(key))
                hit = bool(members & (_device_ports(be) | _device_ports(pe)))
            elif mp in ("ip_configs", "dhcpd_config"):
                hit = key in (be.get(mp) or {}) or key in (pe.get(mp) or {})
            else:
                hit = False
            if hit:
                out.add(did)
    return out
```

Hook it into `_simulate_site_state`, after `proposed = adapter.ingest(proposed_raw)`:

```python
# build the per-snapshot device->effective maps (switch + gateway)
baseline_eff = {**baseline.device_effective, **baseline.gateway_effective}
proposed_eff = {**proposed.device_effective, **proposed.gateway_effective}

# changed = DE-PREFIXED union of per-device effective leaf diffs. (was a P1 bug:
# changed_leaf_paths over the whole {did -> eff} map yields "d1.port_usages.foo.x",
# so affected_device_ids would parse parts[0]=="d1" and match nothing.)
# CRUCIAL (review P2): diff baseline against the BELOW-PROFILE effective, NOT the
# full proposed. A `device` edit is ABOVE the profile (device wins), so its changes
# must NOT taint; only changes from below-profile layers (template/sitetemplate/
# site_setting) can be overridden by the unmodeled profile.
profile_eff = {**profile_proposed.device_effective, **profile_proposed.gateway_effective}
changed: set[str] = set()
for did in set(baseline_eff) | set(profile_eff):
    changed |= set(changed_leaf_paths(baseline_eff.get(did) or {}, profile_eff.get(did) or {}))
changed_leaves = tuple(sorted(changed))

dp_rej = None
if changed_leaves:                       # empty for an all-`device` plan -> no taint
    aff = affected_device_ids(changed_leaves, baseline_eff, profile_eff)
    dp_rej = device_profile_rejection(
        devices=proposed_raw.devices, changed_leaves=changed_leaves, affected_device_ids=aff,
    )
```

Then thread `dp_rej` into `DecisionInputs.rejections` (replace the hardcoded `rejections=()` at line ~167 with `rejections=(dp_rej,) if dp_rej else ()`).

**The gate diffs the BELOW-PROFILE effective (was a P1 over-skip + a P2 mixed-plan over-taint):** the precedence stack is `<type>template → sitetemplate → site_setting → device-profile → device` — a `device` edit is ABOVE the profile (it wins), so its effective changes must NOT taint; only below-profile changes can. Add a kwarg `profile_proposed: IngestOutcome | None = None` to `_simulate_site_state` (the ingest of the proposed state with **only below-profile ops applied**); default `None` → use `proposed` (all changes are below-profile). The callers build it:
- **org fan-out** (`pipeline.py:453`): every op is a template edit (below-profile) → pass `profile_proposed=None` (= `proposed`).
- **single-site `simulate`** (`pipeline.py:326`): if the plan has device ops, build `below_profile_proposed_raw = apply_ops(baseline_raw, [op for op in plan.ops if op.object_type != "device"])`, ingest it, pass that; a **pure-`device`** plan → its below-profile application equals the baseline → `changed_leaves` empty → no taint (subsumes the old skip); a **pure-`site_setting`** plan → below == proposed; a **mixed** plan → only the `site_setting`-attributable changes reach the gate (the `device`-edit changes are excluded → no false UNKNOWN on a device-owned leaf). This is the new post-ingest hook the spec calls for.

Add a mixed-plan regression: a single-site plan with a `device` op (changing the profiled device's own `port_config`) **and** an unrelated `site_setting` op that does NOT affect the profiled device → the profiled device is NOT tainted (its only change is the device-edit, above the profile).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scope/test_affected_devices.py tests/engine/test_pipeline_device_profile.py -q`
Expected: PASS (assert UNKNOWN, **not** REVIEW).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: post-ingest device-profile gate -> per-site UNKNOWN rejection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Phase 4 gate:** full gate green.

---

## Phase 5 — fan-out typing + drivers + replay + OAS schemas

### Task 15: Typed `override_template` + `simulate_org_template`

**Files:**
- Modify: `src/digital_twin/engine/org_template.py` (`override_template`)
- Modify: `src/digital_twin/engine/pipeline.py` (`simulate_org_template` ~lines 312-437)
- Test: `tests/engine/test_org_template.py`

- [ ] **Step 1: Write the failing test** — `override_template(object_type, fetched, snapshot, proposed)` sets the typed field (e.g. `gatewaytemplate=`) on both baseline/proposed raws, leaving the other layers pinned to one snapshot; a `sitetemplate` edit sets `sitetemplate=` consumed by both switch and gateway compiles.

```python
# tests/engine/test_org_template.py  (add)
from digital_twin.engine.org_template import override_template


def test_override_sets_typed_field_both_sides():
    fetched = _raw(sitetemplate={"networks": {}}, gatewaytemplate={"port_config": {}})
    base, prop = override_template("gatewaytemplate", fetched, {"id": "g"}, {"id": "g", "x": 1})
    assert base.gatewaytemplate == {"id": "g"} and prop.gatewaytemplate["x"] == 1
    assert base.sitetemplate == fetched.sitetemplate   # other layers pinned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_org_template.py -q`
Expected: FAIL — `override_template` only handles `networktemplate`.

- [ ] **Step 3: Write minimal implementation** — generalize `override_template` to take `object_type` and `dc_replace` the matching typed field (`{"networktemplate":..., "gatewaytemplate":..., "sitetemplate":...}`) on both raws; in `simulate_org_template`, derive `object_type` from `plan.ops[0].object_type`, pass it to `resolve_org_template(scope, template_id, object_type)`, to `screen_op(object_type, snapshot, proposed_template)` (line 382), and to `override_template(object_type, fetched, snapshot, proposed)`. The org-level L0 `adapter.validate(...)` already dispatches on `op.object_type` — no change beyond using the right op.

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/engine/test_org_template.py -q && uv run pytest tests -q`
Expected: PASS — existing MS networktemplate goldens unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: typed override_template + simulate_org_template (object_type threaded)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 16: Register the OAS schemas (fail-closed)

**Files:**
- Create: `src/digital_twin/adapters/mist/oas/gatewaytemplate.schema.json`, `sitetemplate.schema.json`
- Modify: `src/digital_twin/adapters/mist/validate/schema.py:27` (`_SCHEMA_FILES`)
- Test: `tests/adapters/mist/validate/test_schema.py`

- [ ] **Step 1: Write the failing test** — `validate_payload` for a `gatewaytemplate`/`sitetemplate` payload loads the right schema; and confirm the fail-closed behaviour (a not-yet-registered org type → fatal → UNKNOWN) is preserved for any other org type.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/validate/test_schema.py -q`
Expected: FAIL — `KeyError: 'gatewaytemplate'` / missing schema files.

- [ ] **Step 3: Write minimal implementation** — obtain the committed OAS for `gatewaytemplate` and `sitetemplate` from the Mist OpenAPI (same provenance as `networktemplate.schema.json`; record source + version in `oas/VERSION`). Add to `_SCHEMA_FILES`:
```python
"gatewaytemplate": "gatewaytemplate.schema.json",
"sitetemplate": "sitetemplate.schema.json",
```
Keep the existing `if object_type not in _SCHEMA_FILES: -> fatal` path intact (fail-closed).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/adapters/mist/validate/test_schema.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: register committed gatewaytemplate/sitetemplate OAS schemas (L0)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 17: Typed CLI/MCP dispatch

**Files:**
- Modify: `src/digital_twin/drivers/cli.py` (`_is_org_plan` ~line 68; `_RecordingProvider.resolve_org_template` ~line 62)
- Modify: `src/digital_twin/drivers/mcp_server.py`
- Test: `tests/drivers/test_cli.py`, `tests/drivers/test_mcp_server.py`

- [ ] **Step 1: Write the failing test** — a `gatewaytemplate` plan and a `sitetemplate` plan each route to the org path (not SITE/UNKNOWN) via both CLI and MCP; a malformed plan still falls to the SITE path (no crash).

```python
# tests/drivers/test_cli.py  (add)
from digital_twin.drivers.cli import _is_org_plan


def test_is_org_plan_recognizes_all_org_types():
    for t in ("networktemplate", "gatewaytemplate", "sitetemplate"):
        plan = {"ops": [{"object_type": t}], "scope": {"org_id": "o1"}}
        assert _is_org_plan(plan) is True
    assert _is_org_plan({"ops": [{"object_type": "device"}], "scope": {}}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/drivers/test_cli.py -q`
Expected: FAIL — `_is_org_plan` hardcodes `networktemplate`.

- [ ] **Step 3: Write minimal implementation** — in `cli.py`, replace the `o.get("object_type") == "networktemplate"` literal in `_is_org_plan` with `o.get("object_type") in ORG_OBJECT_TYPES` (import `from digital_twin.scope.allowlist import ORG_OBJECT_TYPES`); update `_RecordingProvider.resolve_org_template` to the 3-arg signature `(self, scope, template_id, object_type)` and delegate. `mcp_server.py` reuses `_is_org_plan` — no change beyond confirming it passes `object_type` through. Keep the defensive isinstance/`.get` guards.

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/drivers -q && uv run pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: typed CLI/MCP org-plan dispatch on ORG_OBJECT_TYPES

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 18: Single-site replay carries the new fields

**Files:**
- Modify: `src/digital_twin/observability/replay/store.py` (`_RAW_FIELDS` ~line 30, `load_fixture_doc` ~line 92)
- Test: `tests/observability/test_replay_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/observability/test_replay_store.py  (add)
def test_new_template_fields_round_trip(tmp_path):
    store = ReplayStore(tmp_path)
    raw = raw_site()  # extend raw_site to allow sitetemplate/gatewaytemplate kwargs
    raw = dataclasses.replace(raw, sitetemplate={"networks": {}}, gatewaytemplate={"port_config": {}})
    loaded = load_fixture_raw(store.save_raw("r", raw))
    assert loaded.sitetemplate == {"networks": {}}
    assert loaded.gatewaytemplate == {"port_config": {}}


def test_legacy_fixture_without_new_fields_loads_as_none(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("r", raw_site())
    data = json.loads(path.read_text())
    data.pop("sitetemplate", None); data.pop("gatewaytemplate", None)
    p = tmp_path / "legacy.json"; p.write_text(json.dumps(data))
    assert load_fixture_raw(p).sitetemplate is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/observability/test_replay_store.py -q`
Expected: FAIL — fields dropped on round-trip.

- [ ] **Step 3: Write minimal implementation** — add `"sitetemplate"`, `"gatewaytemplate"` to `_RAW_FIELDS`; in `load_fixture_doc`, read them with `data.get("sitetemplate")` / `data.get("gatewaytemplate")` (back-compat → `None`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/observability/test_replay_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: single-site replay round-trips sitetemplate/gatewaytemplate (back-compat None)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 19: Typed multi-template `FixtureProvider`

**Files:**
- Modify: `src/digital_twin/observability/replay/store.py` (`FixtureProvider`, `resolve_org_template`)
- Test: `tests/observability/test_replay_store.py`

- [ ] **Step 1: Write the failing test** — a multi-site fixture with `"templates": {"gatewaytemplate": {"g1": {...}}, "sitetemplate": {"s1": {...}}}`; `resolve_org_template(scope, "g1", "gatewaytemplate")` filters sites by `gatewaytemplate_id` and returns the g1 body; the legacy single-`"template"` doc still resolves as `networktemplate`; wrong-org/missing-template → `FetchError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/observability/test_replay_store.py -q`
Expected: FAIL — `resolve_org_template` ignores `object_type` / typed templates.

- [ ] **Step 3: Write minimal implementation** — extend `FixtureProvider.__init__` to read `data.get("templates")` (typed map) in addition to the legacy `data.get("template")` (treated as `{"networktemplate": {<id>: body}}`); make `resolve_org_template(scope, template_id, object_type)` look up `self._templates[object_type].get(template_id)` (missing → `FetchError`), filter sites by `site.<object_type>_id == template_id`; keep the wrong-org/missing-template strictness per type.

- [ ] **Step 4: Run test + suite**

Run: `uv run pytest tests/observability/test_replay_store.py -q && uv run pytest tests -q`
Expected: PASS (existing MS-a..d goldens still valid via the legacy `"template"` key).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: typed multi-template FixtureProvider (back-compat legacy template key)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Phase 5 gate:** full gate green.

---

## Phase 6 — goldens, live verification, docs

### Task 20: Golden scenarios

**Files:**
- Modify: `tests/golden/builders.py` (gateway/site multisite builders)
- Modify: `tests/golden/test_golden_scenarios.py`
- Test: the goldens themselves

- [ ] **Step 1: Write the failing goldens** — one assertion per scenario from the spec's Goldens list:
  - `sitetemplate` edit breaks a switch leaf at one site → org UNSAFE naming it.
  - `gatewaytemplate` edit → gateway `same_ip` / `gateway_unowned` → org UNSAFE.
  - `gatewaytemplate` edit on an unmodeled field (e.g. `routing.*`) → UNKNOWN.
  - `sitetemplate` fetch-fail site → UNKNOWN.
  - cosmetic edit → SAFE.
  - device-profile-present, edit hits an overridable participating leaf → UNKNOWN; AP-profile-only site → unaffected.
  - role-projection: a sitetemplate edit to `ip_configs.*.ip` (gateway-only) moves only the gateway IR (switch verdict unchanged).
  - `gatewaytemplate.networks.*` edit → UNKNOWN.

  Build each via the typed multi-template fixture from Task 19 + builders mirroring the existing MS builders. Assert the org decision + driving site.

- [ ] **Step 2: Run to verify they fail** (then pass once builders exist)

Run: `uv run pytest tests/golden/test_golden_scenarios.py -q`

- [ ] **Step 3: Implement builders** in `tests/golden/builders.py` (gateway device fixtures, sitetemplate/gatewaytemplate-assigned site docs, the typed `templates` doc).

- [ ] **Step 4: Run goldens to verify they pass**

Run: `uv run pytest tests/golden/test_golden_scenarios.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: gateway/sitetemplate org-template goldens

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

### Task 21: Full gate + live verification (read-only)

- [ ] **Step 1: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: all green.

- [ ] **Step 2: Live verification (read-only / simulate-only)** — credentials in `.env` (gitignored, NEVER commit). Run the single-site regression + an org fan-out on a real gatewaytemplate and a real sitetemplate:

```bash
set -a; source .env; set +a
for p in plan.json test-plans/*.json; do printf '%s ' "$p"; uv run digital-twin --plan "$p" 2>/dev/null | head -1; done
```
Expected: the 8 single-site plans unchanged from their documented verdicts. Then craft a no-op `{}` edit on a real `gatewaytemplate` and a real `sitetemplate` assigned to sites and confirm the org fan-out runs and the rollup is consistent.

- [ ] **Step 3: Tier-2 live-verify (the two flagged items)** — using the live `derived_setting` / observed Mist layering, confirm whether gateway `port_config`/`ip_configs` merge per-key (DICT_MERGE, current `GATEWAY_POLICY`) or REPLACE across the `gatewaytemplate → sitetemplate → site_setting` fold, and whether gateway *device* objects can carry `{{vars}}`. Harden `GATEWAY_POLICY` if the observation diverges. Record findings.

- [ ] **Step 4: Docs + memory** — flip the ROADMAP §3 `gatewaytemplate / sitetemplate` entry to ✅ with a results paragraph (mirroring the multisite entry); update the wireless-vlan-observation-gap memory with a new round. Commit.

```bash
git add docs/ROADMAP.md
git commit -m "docs: mark gatewaytemplate/sitetemplate first-class object_types done

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review checklist (run before execution)

- **Spec coverage:** fold primitive (T1-2) ✓; typed gates/allowlists (T3-4) ✓; sitetemplate layer (T5-7) ✓; gateway compile + materialize + role-keyed derived gate + DHCP-row helper + gateway-effective derived-gate wiring (T8-12) ✓; device-profile post-ingest gate (T13-14) ✓; typed fan-out (T15) ✓; OAS schemas (T16) ✓; typed drivers (T17) ✓; replay single-site + typed multi-template (T18-19) ✓; goldens + live + docs (T20-21) ✓. The two Tier-2 items are carried as live-verify (T21-3).
- **Placeholder scan:** the only "adapt to the real signature" notes are in Tasks 4/6/9/12/14/15/20 where the test must mirror existing helper/builder signatures — each names the file to read first and the exact behavioural assertion. No code step ships a `TODO`/`pass`-stub as the implementation.
- **Type consistency:** `fold_layers(layers, policy)`, `MergePolicy`, `SWITCH_POLICY`/`GATEWAY_POLICY`, `merge_site_effective(nt, ss, *, sitetemplate=None)`, `compile_gateway_device(gt, st, ss, device)`, `dhcp_row_rejection(base, prop) -> Rejection|None`, `check_derived(base, prop, *, allowlist=...)`, `device_profile_rejection(...)`, `override_template(object_type, fetched, snapshot, proposed)`, `resolve_org_template(scope, id, object_type)`, `IngestOutcome.gateway_effective` — names are used identically across tasks.
- **Stage names** are the four spec stages exactly: `dhcp_inert_servers`, `dhcp_relay_target`, `dhcp_mode_transition`, `dhcp_scope_field` (+ `device_profile_gate`, `derived_gate`).
