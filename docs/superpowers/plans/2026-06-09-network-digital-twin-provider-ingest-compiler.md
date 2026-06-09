# Network Digital Twin — Plan 2: StateProvider + Mist Ingest + Compiler + Equivalence Gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the effectful data layer and the vendor-specific standardization layer: fetch a single site's raw Mist state (`MistApiProvider` via the `mistapi` SDK), compile the switch effective config (networktemplate + site_setting + device, then `{{vars}}`), ingest it into the Plan-1 IR through a per-domain ingester registry, and prove the compiler against Mist's own derivation with a **two-tier equivalence gate**.

**Architecture:** Two phases. **Phase A (offline, pure):** compiler (`merge`/`vars`/`switch`), OAS-driven full-coverage tests, ingesters (`switch`/`lldp`/`clients`) + registry, capability supply check — all TDD against synthetic Mist-shaped JSON. **Phase B (live):** `MistApiProvider` (mistapi SDK), a probe script that pins real response shapes, and the equivalence gate runner against read-only orgs. The compiler produces **two artifacts** per the spec: the *full effective config* (later consumed by the derived-impact gate) and the *IR projection* (built by the ingesters).

**Tech Stack:** Python 3.14, `uv`, `mistapi` (Mist SDK), `pyyaml` (dev, OAS extraction), pytest marker `live` for network tests (skipped by default).

This is **Plan 2 of 5**. Plan 1 (IR + representations) is implemented (`c79553d`, hardened `b5e7df9`). Later: (3) scope gates + L0 + apply; (4) analysis + checks + verdict; (5) drivers + observability + golden scenarios.

**Decisions locked for this plan:**
- **Fetch via the `mistapi` SDK** (env: `MIST_HOST`, `MIST_APITOKEN`). All SDK calls isolated in `providers/mist_api.py` behind small private helpers; a probe script validates exact call/field names against a real org before the gate runs. **Total fetch failure is a value, not an exception** (`fetch_site -> RawSiteState | FetchError`); Plan 3 maps `FetchError` → `UNKNOWN`.
- **Compile chain (M1):** org `networktemplate` → site `setting` (site wins) → per-device config; `{{vars}}` resolved **once, after the device overlay** (device-level config can reference site vars). **Confirmed (user): `getSiteSettingDerived` does NOT resolve `{{vars}}`** — the Tier-2 gate therefore compares the *pre-vars* merge output (`merge_site_effective`). Devices do not carry their own vars. The site-level merge is oracle-checked; the device layer has **no oracle** (derived is site-level) and is covered by unit tests only — stated honestly.
- **Capabilities are earned, not declared:** ingesters return the capabilities they *actually* produced from `ingest()` (gated on fetch success and/or data presence per capability); the registry collects those returns. The static `produces()` remains the *potential*-supply declaration used by `capability_check`.
- **Unmanaged LLDP neighbors are edge devices = wired clients (user decision):** a neighbor MAC that is not a Mist device becomes a wired `Client` attached to the local switch port — it stays in the impact surface (VLAN continuity, DHCP, routing, FW), instead of vanishing or polluting the device model.
- **OAS verified against the real spec** (`tmunzer/mist_openapi`, `mist.openapi.json`, 2,650 schemas): component names are exactly `site_setting`, `network_template`, `device_switch` (a `site_setting_derived` also exists). Their `$ref` closures contain `anyOf` (13/9/11 — mostly `vlan_id_with_variable` int-or-`{{var}}` unions), 1–2 `oneOf`, ~110 enums, and 434 `default`s — the generator must handle `allOf`/`anyOf`/`oneOf` and **fail loudly** on anything else.
- **Two-tier equivalence gate:**
  - **Tier 1 (offline, committed):** OAS-schema-driven tests generate payloads covering **every attribute** (incl. features no org has configured) and assert our merge precedence per leaf. Validates our semantics for self-consistency and full coverage.
  - **Tier 2 (live):** ours vs `getSiteSettingDerived` per real site. Validates our semantics **against Mist's engine**. Emits an **attribute-coverage report** (which schema leaves were exercised by real data) so a green gate carries explicit coverage, never false confidence.
  - **Gate rule (from spec):** target is 100% on in-scope fields after normalization; every residual diff must be explicitly catalogued in `divergences.json` with a reason; an uncatalogued diff fails the gate. *Do not build Plans 3–5 on top until Tier 2 passes.*
- **Real data is never committed:** live captures go to a git-ignored `.cache/` for debugging only. Redacted, committable fixtures arrive with Plan 5's replay store.
- **Mist VC note:** a Virtual Chassis is *one* Mist device (members appear in stats); `Device.vc_members` stays empty for Mist ingest — the IR's VC-folding is for vendors that model members as separate devices.

---

## File Structure

```
src/digital_twin/
├── providers/
│   ├── __init__.py          # re-exports
│   ├── base.py              # SiteScope, FetchFailure, StateMeta, RawSiteState, StateProvider
│   └── mist_api.py          # MistApiProvider (mistapi SDK, on-demand single-site fetch)
├── adapters/
│   ├── __init__.py
│   └── mist/
│       ├── __init__.py
│       ├── oas/             # extracted OAS schemas (JSON, committed) + VERSION
│       │   └── __init__.py  # norm_schema()/UnsupportedSchema — SHARED by Tier-1 gen + Tier-2 coverage
│       ├── ingest/
│       │   ├── __init__.py
│       │   ├── base.py      # Ingester protocol (produces() -> caps; ingest(...))
│       │   ├── registry.py  # run ingesters in order, collect produced capabilities
│       │   ├── ports.py     # expand_port_members(), port/usage/VLAN resolution helpers
│       │   ├── switch.py    # devices/ports/vlans/l3intfs from effective + device configs
│       │   ├── lldp.py      # links from port-stats LLDP (two-sided matching, bundles, STP)
│       │   └── clients.py   # wired + wireless clients from stats
│       └── compile/
│           ├── __init__.py
│           ├── merge.py     # per-field merge policy (DATA) + merge_site_effective()
│           ├── vars.py      # {{var}} resolution over string leaves
│           ├── switch.py    # compile_site() / compile_device() -> full effective config
│           ├── divergences.json  # catalogued, justified oracle divergences (DATA)
│           └── equivalence.py    # normalize/compare vs derived + attribute coverage report
├── engine/
│   ├── __init__.py
│   └── capability_check.py  # supply/demand validation (producers must cover requirements)
tools/
├── extract_oas.py           # pull needed component schemas out of the Mist OAS into oas/
├── probe_fetch.py           # live: dump one site's raw responses to .cache/ for shape pinning
└── equivalence_gate.py      # live: Tier-2 gate runner (per-site verdict + coverage report)
tests/
├── adapters/
│   ├── __init__.py
│   └── mist/
│       ├── __init__.py
│       ├── fixtures.py          # synthetic Mist-shaped JSON builders (shared)
│       ├── test_merge.py
│       ├── test_vars.py
│       ├── test_compile_switch.py
│       ├── test_oas_coverage.py # Tier-1: schema-driven full-attribute precedence tests
│       ├── test_ingest_ports.py
│       ├── test_ingest_switch.py
│       ├── test_ingest_lldp.py
│       ├── test_ingest_clients.py
│       └── test_equivalence.py
├── providers/
│   ├── __init__.py
│   ├── test_base.py
│   └── test_mist_api_live.py    # @pytest.mark.live (skipped without MIST_* env)
└── engine/
    ├── __init__.py
    └── test_capability_check.py
```

Dependency directions stay clean: `providers → (stdlib, mistapi)`; `adapters/mist → {ir, providers.base}`; `engine → adapters (registries only)`; nothing in `ir/`/`representations/` changes.

---

## Phase A — offline (pure, TDD, no network)

## Task 0: Dependencies, markers, hygiene

**Files:**
- Modify: `pyproject.toml`, `.gitignore`
- Create: `.env.example`, package `__init__.py` markers listed above

- [ ] **Step 1: Add dependencies**

Run:
```bash
uv add mistapi
uv add --dev pyyaml types-pyyaml
```

- [ ] **Step 2: Configure the `live` marker and mypy overrides**

In `pyproject.toml`, extend:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q -m 'not live'"
markers = [
    "live: requires MIST_HOST/MIST_APITOKEN env and network access",
]

[[tool.mypy.overrides]]
module = ["networkx.*", "netaddr.*", "mistapi.*", "yaml.*"]
ignore_missing_imports = true
```

(Default runs exclude `live`; run them explicitly with `uv run pytest -m live`.)

- [ ] **Step 3: Hygiene files**

Append to `.gitignore`:

```
.env
.cache/
```

Create `.env.example`:

```
# Mist API access for live tests / probes / the equivalence gate (read-only token)
MIST_HOST=api.eu.mist.com
MIST_APITOKEN=changeme
# Comma-separated site ids used by the Tier-2 equivalence gate
DT_GATE_SITE_IDS=
```

- [ ] **Step 4: Create package markers**

Run:
```bash
mkdir -p src/digital_twin/providers src/digital_twin/adapters/mist/oas \
         src/digital_twin/adapters/mist/ingest src/digital_twin/adapters/mist/compile \
         src/digital_twin/engine tools \
         tests/adapters/mist tests/providers tests/engine
touch src/digital_twin/providers/__init__.py src/digital_twin/adapters/__init__.py \
      src/digital_twin/adapters/mist/__init__.py src/digital_twin/adapters/mist/ingest/__init__.py \
      src/digital_twin/adapters/mist/compile/__init__.py src/digital_twin/adapters/mist/oas/__init__.py \
      src/digital_twin/engine/__init__.py \
      tests/adapters/__init__.py tests/adapters/mist/__init__.py \
      tests/providers/__init__.py tests/engine/__init__.py
```

- [ ] **Step 5: Verify gates still green** — `uv run pytest -q` (75 passed), `uv run ruff check .`, `uv run mypy`.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: deps (mistapi, pyyaml), live marker, env example, package layout for plan 2"
```

---

## Task 1: `providers/base.py` — the StateProvider seam

**Files:**
- Create: `src/digital_twin/providers/base.py`
- Test: `tests/providers/test_base.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/test_base.py`:

```python
from datetime import UTC, datetime

from digital_twin.providers.base import (
    FetchFailure,
    RawSiteState,
    SiteScope,
    StateMeta,
    StateProvider,
)


def test_scope_and_meta_construct():
    scope = SiteScope(org_id="o1", site_id="s1")
    meta = StateMeta(
        acquired_at=datetime(2026, 6, 9, tzinfo=UTC),
        host="api.eu.mist.com",
        fetched=("site", "setting"),
        failures=(FetchFailure(object="derived", error="404"),),
    )
    assert scope.site_id == "s1"
    assert meta.failures[0].object == "derived"
    assert meta.is_complete is False


def test_meta_complete_when_no_failures():
    meta = StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=("site",), failures=())
    assert meta.is_complete is True


def test_raw_site_state_holds_vendor_payloads():
    raw = RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1", "networktemplate_id": "nt1"},
        setting={"networks": {}},
        networktemplate={"name": "NT"},
        devices=({"mac": "aa", "type": "switch"},),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=(), failures=()),
    )
    assert raw.site["networktemplate_id"] == "nt1"
    assert raw.derived_setting is None


def test_total_fetch_failure_is_a_value_not_an_exception():
    from digital_twin.providers.base import FetchError

    err = FetchError(
        scope=SiteScope(org_id="o1", site_id="s1"),
        failures=(FetchFailure(object="setting", error="503"),),
        acquired_at=datetime.now(UTC),
        host="api.eu.mist.com",
    )
    assert err.failures[0].object == "setting"


def test_state_provider_is_a_protocol():
    from digital_twin.providers.base import FetchError

    class Fake:
        def fetch_site(
            self, scope: SiteScope, *, include_derived: bool = False
        ) -> RawSiteState | FetchError:
            raise NotImplementedError

    provider: StateProvider = Fake()
    assert provider is not None
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/providers/test_base.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/providers/base.py`:

```python
"""StateProvider seam: raw vendor state for a scope, plus freshness metadata.

RawSiteState holds VENDOR-SHAPED payloads (dicts as returned by the API) — the
adapter standardizes them; nothing else may interpret them. `derived_setting` is
fetched ONLY for the equivalence gate (the oracle); the live pipeline never uses it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

JsonObj = Mapping[str, Any]


@dataclass(frozen=True)
class SiteScope:
    org_id: str
    site_id: str


@dataclass(frozen=True)
class FetchFailure:
    object: str  # which fetch failed, e.g. "port_stats"
    error: str


@dataclass(frozen=True)
class StateMeta:
    acquired_at: datetime
    host: str
    fetched: tuple[str, ...]  # which objects were fetched successfully
    failures: tuple[FetchFailure, ...]

    @property
    def is_complete(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class RawSiteState:
    scope: SiteScope
    site: JsonObj  # GET /sites/{id} — carries networktemplate_id etc.
    setting: JsonObj  # GET /sites/{id}/setting (raw, pre-derive)
    networktemplate: JsonObj | None  # GET /orgs/{org}/networktemplates/{id}
    devices: tuple[JsonObj, ...]  # device configs (switches + aps)
    device_stats: tuple[JsonObj, ...]  # per-device stats (AP lldp_stat lives here)
    port_stats: tuple[JsonObj, ...]  # switch port stats (LLDP neighbors, STP, LAG)
    wireless_clients: tuple[JsonObj, ...]
    wired_clients: tuple[JsonObj, ...]
    derived_setting: JsonObj | None  # ORACLE ONLY (equivalence gate)
    meta: StateMeta


@dataclass(frozen=True)
class FetchError:
    """Total fetch failure — no usable baseline (site/setting could not be read).

    A VALUE, not an exception: callers must narrow `RawSiteState | FetchError`,
    and Plan 3's pipeline maps this to decision UNKNOWN.
    """

    scope: SiteScope
    failures: tuple[FetchFailure, ...]
    acquired_at: datetime
    host: str


class StateProvider(Protocol):
    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError: ...
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/providers/test_base.py -q` → PASS (4).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/providers tests/providers
git commit -m "feat(providers): StateProvider seam (SiteScope, RawSiteState, StateMeta)"
```

---

## Task 2: `compile/merge.py` — per-field merge policy as data

The site-level merge: `networktemplate` is the base, `site_setting` wins. *How* each field merges is a **data table**, not scattered code — the equivalence gate hardens exactly this table.

**Files:**
- Create: `src/digital_twin/adapters/mist/compile/merge.py`
- Test: `tests/adapters/mist/test_merge.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_merge.py`:

```python
from digital_twin.adapters.mist.compile.merge import MergePolicy, merge_site_effective


def test_site_scalar_overrides_template():
    tpl = {"ospf_areas": {"0": {"x": 1}}, "mtu": 1500}
    site = {"mtu": 9000}
    out = merge_site_effective(tpl, site)
    assert out["mtu"] == 9000
    assert out["ospf_areas"] == {"0": {"x": 1}}  # template-only survives


def test_dict_merge_fields_merge_per_key_site_wins():
    tpl = {"networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 20}}}
    site = {"networks": {"voice": {"vlan_id": 21}, "guest": {"vlan_id": 30}}}
    out = merge_site_effective(tpl, site)
    assert out["networks"] == {
        "corp": {"vlan_id": 10},
        "voice": {"vlan_id": 21},  # site wins per key
        "guest": {"vlan_id": 30},
    }


def test_dict_merge_applies_to_port_usages_and_vars_too():
    tpl = {"port_usages": {"ap": {"mode": "trunk"}}, "vars": {"a": "1"}}
    site = {"port_usages": {"office": {"mode": "access"}}, "vars": {"b": "2"}}
    out = merge_site_effective(tpl, site)
    assert set(out["port_usages"]) == {"ap", "office"}
    assert out["vars"] == {"a": "1", "b": "2"}


def test_replace_fields_replace_wholesale():
    # dhcp_snooping is REPLACE policy: site value replaces the whole object
    tpl = {"dhcp_snooping": {"enabled": True, "networks": ["corp"]}}
    site = {"dhcp_snooping": {"enabled": False}}
    out = merge_site_effective(tpl, site)
    assert out["dhcp_snooping"] == {"enabled": False}


def test_none_template_means_site_only():
    assert merge_site_effective(None, {"mtu": 1}) == {"mtu": 1}


def test_inputs_are_not_mutated():
    tpl = {"networks": {"corp": {"vlan_id": 10}}}
    site = {"networks": {"corp": {"vlan_id": 11}}}
    merge_site_effective(tpl, site)
    assert tpl["networks"]["corp"]["vlan_id"] == 10
    assert site["networks"]["corp"]["vlan_id"] == 11


def test_policy_table_is_data():
    assert MergePolicy.for_field("networks") is MergePolicy.DICT_MERGE
    assert MergePolicy.for_field("unknown_future_field") is MergePolicy.REPLACE
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/adapters/mist/test_merge.py -q` → FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/adapters/mist/compile/merge.py`:

```python
"""Site-level merge: networktemplate (base) + site_setting (wins), per-field policy.

The policy table is DATA. Default is REPLACE (site value replaces the field
wholesale) — conservative and Mist-like. DICT_MERGE fields merge per key with the
site winning per key. The Tier-1 OAS tests assert precedence for every schema leaf;
the Tier-2 live gate hardens this table against Mist's real derivation.
"""

from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any

JsonObj = dict[str, Any]


class MergePolicy(StrEnum):
    REPLACE = "replace"
    DICT_MERGE = "dict_merge"

    @classmethod
    def for_field(cls, field: str) -> MergePolicy:
        return _POLICY.get(field, cls.REPLACE)


# Fields whose values are keyed collections merged per key (site wins per key).
# Everything else: REPLACE. Grow/adjust as the Tier-2 gate uncovers divergences.
_POLICY: dict[str, MergePolicy] = {
    "networks": MergePolicy.DICT_MERGE,
    "port_usages": MergePolicy.DICT_MERGE,
    "vars": MergePolicy.DICT_MERGE,
    "dhcpd_config": MergePolicy.DICT_MERGE,
    "switch_matching": MergePolicy.REPLACE,
}


def merge_site_effective(networktemplate: JsonObj | None, site_setting: JsonObj) -> JsonObj:
    """Full effective SITE config (all fields, including out-of-scope ones)."""
    out: JsonObj = copy.deepcopy(dict(networktemplate or {}))
    for field, site_value in site_setting.items():
        policy = MergePolicy.for_field(field)
        base_value = out.get(field)
        if (
            policy is MergePolicy.DICT_MERGE
            and isinstance(base_value, dict)
            and isinstance(site_value, dict)
        ):
            merged = dict(base_value)
            merged.update(copy.deepcopy(site_value))
            out[field] = merged
        else:
            out[field] = copy.deepcopy(site_value)
    return out
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/adapters/mist/test_merge.py -q` → PASS (7).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters tests/adapters
git commit -m "feat(compile): site-level merge with per-field policy table (data)"
```

---

## Task 3: `compile/vars.py` — `{{var}}` resolution

**Files:**
- Create: `src/digital_twin/adapters/mist/compile/vars.py`
- Test: `tests/adapters/mist/test_vars.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_vars.py`:

```python
from digital_twin.adapters.mist.compile.vars import UnresolvedVars, resolve_vars

import pytest


def test_substitutes_string_leaves_recursively():
    cfg = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}", "name": "corp"}},
           "list": ["{{a}}", 1]}
    out = resolve_vars(cfg, {"corp_vlan": "30", "a": "x"})
    assert out["networks"]["corp"]["vlan_id"] == "30"
    assert out["list"] == ["x", 1]


def test_partial_substitution_inside_strings():
    out = resolve_vars({"desc": "vlan-{{id}}-prod"}, {"id": "7"})
    assert out["desc"] == "vlan-7-prod"


def test_unresolved_var_raises_with_paths():
    with pytest.raises(UnresolvedVars) as e:
        resolve_vars({"a": {"b": "{{missing}}"}}, {})
    assert "missing" in str(e.value)
    assert "a.b" in str(e.value)


def test_non_strings_pass_through_and_input_not_mutated():
    cfg = {"n": 5, "b": True, "s": "{{v}}"}
    out = resolve_vars(cfg, {"v": "ok"})
    assert out["n"] == 5 and out["b"] is True and out["s"] == "ok"
    assert cfg["s"] == "{{v}}"
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/adapters/mist/test_vars.py -q` → FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/adapters/mist/compile/vars.py`:

```python
"""{{var}} resolution over string leaves, from site_setting.vars.

Applied AFTER the site-level merge. An unresolved variable is an error carrying
the offending paths (feeds the L1 'unresolved template variable' finding later).
"""

from __future__ import annotations

import re
from typing import Any

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class UnresolvedVars(ValueError):
    def __init__(self, missing: dict[str, list[str]]) -> None:
        self.missing = missing  # var name -> paths where it occurs
        details = "; ".join(f"{var} at {', '.join(paths)}" for var, paths in missing.items())
        super().__init__(f"unresolved vars: {details}")


def resolve_vars(config: Any, variables: dict[str, str]) -> Any:
    missing: dict[str, list[str]] = {}
    resolved = _walk(config, variables, "", missing)
    if missing:
        raise UnresolvedVars(missing)
    return resolved


def _walk(node: Any, variables: dict[str, str], path: str, missing: dict[str, list[str]]) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, variables, f"{path}.{k}" if path else k, missing) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, variables, f"{path}[{i}]", missing) for i, v in enumerate(node)]
    if isinstance(node, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in variables:
                missing.setdefault(name, []).append(path)
                return m.group(0)
            return variables[name]

        return _VAR.sub(sub, node)
    return node
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/adapters/mist/test_vars.py -q` → PASS (4).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(compile): {{var}} resolution with unresolved-var error paths"
```

---

## Task 4: `compile/switch.py` — site + device effective config

**Files:**
- Create: `src/digital_twin/adapters/mist/compile/switch.py`
- Test: `tests/adapters/mist/test_compile_switch.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_compile_switch.py`:

```python
from digital_twin.adapters.mist.compile.switch import compile_device, compile_site, merge_only


def test_compile_site_merges_then_resolves_vars():
    tpl = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}"}}}
    setting = {"vars": {"corp_vlan": "30"}, "port_usages": {"office": {"mode": "access"}}}
    eff = compile_site(tpl, setting)
    assert eff["networks"]["corp"]["vlan_id"] == "30"
    assert eff["port_usages"]["office"]["mode"] == "access"


def test_merge_only_keeps_vars_unresolved_for_the_oracle():
    # getSiteSettingDerived does NOT resolve vars (confirmed) — the Tier-2 gate
    # compares this artifact, with {{...}} intact.
    tpl = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}"}}}
    merged = merge_only(tpl, {"vars": {"corp_vlan": "30"}})
    assert merged["networks"]["corp"]["vlan_id"] == "{{corp_vlan}}"


def test_compile_site_without_vars_skips_resolution():
    eff = compile_site(None, {"networks": {"corp": {"vlan_id": 30}}})
    assert eff["networks"]["corp"]["vlan_id"] == 30


def test_compile_device_layers_overrides_then_resolves_site_vars():
    # device-level config can reference SITE vars; resolution happens ONCE,
    # after the device overlay (devices have no vars of their own).
    tpl = {"networks": {"corp": {"vlan_id": "{{corp_vlan}}"}}}
    setting = {"vars": {"corp_vlan": "30"},
               "port_usages": {"office": {"mode": "access", "port_network": "corp"}}}
    device = {
        "mac": "aabbcc001122",
        "port_config": {"ge-0/0/1": {"usage": "office", "description": "desk-{{corp_vlan}}"}},
        "networks": {"lab": {"vlan_id": "{{lab_vlan}}"}},
    }
    setting["vars"]["lab_vlan"] = "99"
    dev_eff = compile_device(tpl, setting, device)
    assert dev_eff["networks"]["corp"]["vlan_id"] == "30"  # site leaf resolved
    assert dev_eff["networks"]["lab"]["vlan_id"] == "99"  # device leaf resolved w/ site vars
    assert dev_eff["port_config"]["ge-0/0/1"]["description"] == "desk-30"


def test_compile_device_does_not_mutate_inputs():
    setting = {"networks": {"corp": {"vlan_id": 30}}}
    device = {"networks": {"lab": {"vlan_id": 99}}}
    compile_device(None, setting, device)
    assert "lab" not in setting["networks"]
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/adapters/mist/test_compile_switch.py -q` → FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/adapters/mist/compile/switch.py`:

```python
"""Switch effective-config compilation (M1 chain).

merge_only:     networktemplate + site_setting (site wins), {{vars}} UNRESOLVED.
                This is the Tier-2 gate artifact — getSiteSettingDerived does NOT
                resolve vars (confirmed), so the oracle comparison uses this.
compile_site:   merge_only + {{vars}} resolved — the site-level live artifact
                (VLAN ids etc. usable by ingest).
compile_device: device config layered on the UNRESOLVED merge (per-key, device
                wins), THEN {{vars}} resolved once — device-level config can
                reference site vars; devices have no vars of their own.
                NO ORACLE exists for this layer (derived is site-level) —
                covered by unit tests only.
"""

from __future__ import annotations

import copy
from typing import Any

from .merge import merge_site_effective
from .vars import resolve_vars

JsonObj = dict[str, Any]

_DEVICE_DICT_MERGE_FIELDS = ("networks", "port_usages")
_DEVICE_OWN_FIELDS = ("port_config", "ip_config", "other_ip_configs")


def merge_only(networktemplate: JsonObj | None, site_setting: JsonObj) -> JsonObj:
    """Site-level merge with {{vars}} left intact (the oracle-comparison artifact)."""
    return merge_site_effective(networktemplate, site_setting)


def _resolve(effective: JsonObj) -> JsonObj:
    variables = effective.get("vars") or {}
    if not variables:
        return effective
    return resolve_vars(effective, {str(k): str(v) for k, v in variables.items()})


def compile_site(networktemplate: JsonObj | None, site_setting: JsonObj) -> JsonObj:
    return _resolve(merge_only(networktemplate, site_setting))


def compile_device(
    networktemplate: JsonObj | None, site_setting: JsonObj, device: JsonObj
) -> JsonObj:
    """Per-device effective: unresolved site merge + device overlay, then vars once."""
    out = merge_only(networktemplate, site_setting)
    for field in _DEVICE_DICT_MERGE_FIELDS:
        dev_val = device.get(field)
        if isinstance(dev_val, dict):
            merged = dict(out.get(field) or {})
            merged.update(copy.deepcopy(dev_val))
            out[field] = merged
    for field in _DEVICE_OWN_FIELDS:
        if field in device:
            out[field] = copy.deepcopy(device[field])
    return _resolve(out)
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/adapters/mist/test_compile_switch.py -q` → PASS (3).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(compile): compile_site (oracle-checked) + compile_device (unit-tested layer)"
```

---

## Task 5: OAS extraction + Tier-1 full-attribute precedence tests

The answer to "real data misses unconfigured attributes": generate test payloads from the **OAS schemas** so every attribute is exercised, and assert merge precedence per leaf.

**Files:**
- Create: `tools/extract_oas.py`, `src/digital_twin/adapters/mist/oas/VERSION`, extracted schema JSONs
- Test: `tests/adapters/mist/test_oas_coverage.py`

- [ ] **Step 1: Write the extractor**

Create `tools/extract_oas.py`:

```python
"""Extract the component schemas the twin needs from the Mist OpenAPI spec.

Usage:
  uv run python tools/extract_oas.py /path/to/mist.openapi.json|yaml

Source spec: https://github.com/tmunzer/mist_openapi (pin the version you use in
oas/VERSION). Writes small, fully-$ref-resolved JSON schema files into
src/digital_twin/adapters/mist/oas/. Re-run when bumping the OAS version.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path("src/digital_twin/adapters/mist/oas")
# OAS component name -> output file. Names VERIFIED against mist.openapi.json
# (2,650 components; a `site_setting_derived` schema also exists if ever needed).
WANTED = {
    "site_setting": "site_setting.schema.json",
    "network_template": "networktemplate.schema.json",
    "device_switch": "device_switch.schema.json",
}


def load_spec(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        return yaml.safe_load(text)  # type: ignore[no-any-return]
    return json.loads(text)  # type: ignore[no-any-return]


def resolve_refs(node: Any, components: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            if name in seen:  # break recursion cycles
                return {"type": "object"}
            return resolve_refs(components[name], components, (*seen, name))
        return {k: resolve_refs(v, components, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_refs(v, components, seen) for v in node]
    return node


def main() -> None:
    spec = load_spec(Path(sys.argv[1]))
    components: dict[str, Any] = spec["components"]["schemas"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, filename in WANTED.items():
        if name not in components:
            close = [c for c in components if name.replace("_", "") in c.lower().replace("_", "")]
            sys.exit(f"schema {name!r} not found; candidates: {close[:10]}")
        resolved = resolve_refs(components[name], components)
        (OUT_DIR / filename).write_text(json.dumps(resolved, indent=1, sort_keys=True))
        print(f"wrote {OUT_DIR / filename}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the extractor against the pinned Mist OAS**

Download the spec (from `tmunzer/mist_openapi`, pick the current release), then:

```bash
uv run python tools/extract_oas.py /path/to/mist.openapi.json
echo "<spec version / git sha used>" > src/digital_twin/adapters/mist/oas/VERSION
```

Expected: three `*.schema.json` files written. If `WANTED` names don't match the spec's component names, the error lists candidates — fix the mapping and re-run. Commit the extracted schemas (they are small and synthetic — no org data).

- [ ] **Step 3: Write the shared schema-normalization module**

Create (overwrite the empty) `src/digital_twin/adapters/mist/oas/__init__.py` — **shared** by the
Tier-1 generator and Tier-2 `attribute_coverage`, so both see the same leaves behind composed
schemas:

```python
"""Extracted Mist OAS schemas (data) + shared schema-normalization helpers.

norm_schema() resolves composition so BOTH the Tier-1 payload generator and the
Tier-2 attribute-coverage walker see the same leaves: allOf is merged, the FIRST
variant of anyOf/oneOf is taken (the Mist OAS uses these mostly for
int-or-{{var}} unions), nullable type-arrays collapse to their non-null type.
Unknown constructs raise UnsupportedSchema LOUDLY — silently under-generating
would fake full coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent

_KNOWN_KEYS = {"type", "properties", "additionalProperties", "items", "enum", "default",
               "description", "format", "minimum", "maximum", "minLength", "maxLength",
               "pattern", "example", "examples", "required", "nullable", "deprecated",
               "readOnly", "writeOnly", "title", "minItems", "maxItems", "uniqueItems",
               "allOf", "anyOf", "oneOf", "const", "exclusiveMinimum", "exclusiveMaximum",
               "multipleOf", "x-deprecation-note"}


class UnsupportedSchema(ValueError):
    """An OAS construct the tooling does not understand — fail loudly."""


def load_schema(filename: str) -> dict[str, Any]:
    return json.loads((_DIR / filename).read_text())  # type: ignore[no-any-return]


def norm_schema(schema: dict[str, Any]) -> dict[str, Any]:
    unknown = set(schema) - _KNOWN_KEYS
    if unknown:
        raise UnsupportedSchema(f"unsupported OAS constructs: {sorted(unknown)}")
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        props: dict[str, Any] = {}
        for sub in schema["allOf"]:
            sub = norm_schema(sub)
            props.update(sub.get("properties") or {})
            merged.update({k: v for k, v in sub.items() if k != "properties"})
        if props:
            merged["properties"] = props
        return norm_schema({**merged, **{k: v for k, v in schema.items() if k != "allOf"}})
    for comb in ("anyOf", "oneOf"):
        if comb in schema:
            return norm_schema(schema[comb][0])  # first variant, deterministic
    t = schema.get("type")
    if isinstance(t, list):  # nullable type arrays, e.g. ["integer", "null"]
        non_null = [x for x in t if x != "null"]
        return {**schema, "type": non_null[0] if non_null else "string"}
    return schema
```

- [ ] **Step 4: Write the Tier-1 schema-driven tests**

Create `tests/adapters/mist/test_oas_coverage.py`:

```python
"""Tier-1 equivalence: OAS-driven FULL-ATTRIBUTE precedence tests.

Generates payloads covering every schema leaf (including features no real org
has configured) and asserts our merge semantics per leaf: site wins on overlap,
template-only survives, DICT_MERGE fields merge per key. Validates OUR semantics
for self-consistency; Tier 2 (live gate) validates them against Mist's engine.
"""

from __future__ import annotations

from typing import Any

import pytest

from digital_twin.adapters.mist.compile.merge import MergePolicy, merge_site_effective
from digital_twin.adapters.mist.oas import load_schema, norm_schema


def _load(name: str) -> dict[str, Any]:
    return load_schema(name)


def _marker(schema: dict[str, Any], tag: str) -> Any:
    """A type-valid value for a leaf schema, distinguishable by `tag`."""
    t = schema.get("type")
    if "enum" in schema:
        options = schema["enum"]
        return options[0] if tag == "tpl" else options[-1]
    if t == "string":
        return tag
    if t in ("integer", "number"):
        return 1 if tag == "tpl" else 2
    if t == "boolean":
        return tag != "tpl"
    return tag  # untyped: treat as string


def _gen(schema: dict[str, Any], tag: str, depth: int = 0) -> Any:
    """Generate a payload populating EVERY property of the schema."""
    if depth > 12:
        return _marker({"type": "string"}, tag)
    schema = norm_schema(schema)
    t = schema.get("type")
    if t == "object" or "properties" in schema or "additionalProperties" in schema:
        out: dict[str, Any] = {}
        for prop, sub in (schema.get("properties") or {}).items():
            out[prop] = _gen(sub, tag, depth + 1)
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):  # keyed collection (networks, port_usages, ...)
            out[f"key_{tag}"] = _gen(ap, tag, depth + 1)
            out["key_shared"] = _gen(ap, tag, depth + 1)
        return out
    if t == "array":
        items = schema.get("items") or {"type": "string"}
        return [_gen(items, tag, depth + 1)]
    return _marker(schema, tag)


def _leaves(node: Any, path: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            out.update(_leaves(v, f"{path}.{k}" if path else k))
        return out
    return {path: node}


@pytest.fixture(scope="module")
def schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    return _load("networktemplate.schema.json"), _load("site_setting.schema.json")


def test_site_wins_on_every_overlapping_leaf(schemas):
    nt_schema, st_schema = schemas
    tpl = _gen(nt_schema, "tpl")
    site = _gen(st_schema, "site")
    out = merge_site_effective(tpl, site)
    out_leaves = _leaves(out)
    for path, value in _leaves(site).items():
        top = path.split(".", 1)[0]
        if MergePolicy.for_field(top) is MergePolicy.DICT_MERGE and ".key_tpl" in f".{path}":
            continue  # template-only keys of merged dicts are not in `site`
        assert out_leaves.get(path) == value, f"site value lost at {path}"


def test_template_only_fields_survive(schemas):
    nt_schema, _ = schemas
    tpl = _gen(nt_schema, "tpl")
    out = merge_site_effective(tpl, {})
    assert _leaves(out) == _leaves(tpl)


def test_dict_merge_unions_keys_from_both_sides(schemas):
    nt_schema, st_schema = schemas
    tpl = _gen(nt_schema, "tpl")
    site = _gen(st_schema, "site")
    out = merge_site_effective(tpl, site)
    for field, policy_field in (("networks", "networks"), ("port_usages", "port_usages")):
        if field in tpl and field in site:
            assert MergePolicy.for_field(policy_field) is MergePolicy.DICT_MERGE
            assert "key_tpl" in out[field], f"{field}: template-only key lost"
            assert "key_site" in out[field], f"{field}: site key lost"
            assert out[field]["key_shared"] == site[field]["key_shared"], f"{field}: site must win"
```

- [ ] **Step 5: Run to verify pass** — `uv run pytest tests/adapters/mist/test_oas_coverage.py -q`
Expected: PASS. If a leaf assertion fails, it has found a real precedence bug or a field needing a `_POLICY` entry — fix the policy table (that is this tier doing its job).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(compile): OAS extraction + shared schema norm + Tier-1 precedence tests"
```

---

## Task 6: Ingester protocol + registry

**Files:**
- Create: `src/digital_twin/adapters/mist/ingest/base.py`, `src/digital_twin/adapters/mist/ingest/registry.py`
- Test: `tests/adapters/mist/test_ingest_registry.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_ingest_registry.py`:

```python
from digital_twin.adapters.mist.ingest.base import IngestContext, Ingester
from digital_twin.adapters.mist.ingest.registry import IngesterRegistry
from digital_twin.ir import IRBuilder, IRCapability
from digital_twin.providers.base import RawSiteState


class FakeIngester:
    """Earns its capability only when its source data is actually present."""

    name = "fake"

    def produces(self) -> frozenset[str]:  # POTENTIAL supply (for capability_check)
        return frozenset({IRCapability.WIRED_L2})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:  # ACTUALLY produced
        if "devices" not in ctx.raw.meta.fetched:
            return frozenset()
        return frozenset({IRCapability.WIRED_L2})


def _raw(fetched: tuple[str, ...] = ()) -> RawSiteState:  # minimal stub
    from datetime import UTC, datetime

    from digital_twin.providers.base import SiteScope, StateMeta

    return RawSiteState(
        scope=SiteScope(org_id="o", site_id="s"), site={}, setting={}, networktemplate=None,
        devices=(), device_stats=(), port_stats=(), wireless_clients=(), wired_clients=(),
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="h", fetched=fetched, failures=()),
    )


def test_registry_collects_capabilities_actually_earned():
    reg = IngesterRegistry([FakeIngester()])
    builder = IRBuilder()
    report = reg.run(IngestContext(raw=_raw(fetched=("devices",)), site_effective={},
                                   device_effective={}, builder=builder))
    assert report.ok
    assert IRCapability.WIRED_L2 in report.produced
    assert builder.build().has(IRCapability.WIRED_L2)


def test_capability_not_claimed_when_source_data_missing():
    # the fetch failed -> the ingester earns nothing -> the IR must NOT claim it
    reg = IngesterRegistry([FakeIngester()])
    builder = IRBuilder()
    report = reg.run(IngestContext(raw=_raw(fetched=()), site_effective={},
                                   device_effective={}, builder=builder))
    assert report.produced == frozenset()
    assert not builder.build().has(IRCapability.WIRED_L2)


def test_crashing_ingester_becomes_a_named_failure_value_not_an_exception():
    class Crasher:
        name = "crasher"

        def produces(self) -> frozenset[str]:
            return frozenset({IRCapability.STP_STATE})

        def ingest(self, ctx: IngestContext) -> frozenset[str]:
            raise RuntimeError("boom")

    reg = IngesterRegistry([Crasher(), FakeIngester()])
    builder = IRBuilder()
    report = reg.run(IngestContext(raw=_raw(fetched=("devices",)), site_effective={},
                                   device_effective={}, builder=builder))
    assert not report.ok
    assert report.failures[0].ingester == "crasher" and "boom" in report.failures[0].error
    # the crash is isolated: the other ingester still ran, the crasher earned nothing
    assert IRCapability.WIRED_L2 in report.produced
    assert IRCapability.STP_STATE not in report.produced


def test_ingester_satisfies_protocol():
    ingester: Ingester = FakeIngester()
    assert ingester.name == "fake"
```

- [ ] **Step 2: Run to verify fail** — FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write the implementations**

Create `src/digital_twin/adapters/mist/ingest/base.py`:

```python
"""Ingester seam: one domain turns raw/effective Mist data into an IR slice.

Capabilities are EARNED, not declared: produces() states the POTENTIAL supply
(consumed by engine.capability_check for wiring validation), while ingest()
returns what was ACTUALLY produced — gated on fetch success and/or data
presence — and only those reach the IR. A failed fetch can therefore never
masquerade as a populated domain (the no-silent-blind-spot contract).
Adding a domain (gateway/wlan/wan) = adding one Ingester, nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from digital_twin.ir import Capability, IRBuilder
from digital_twin.providers.base import RawSiteState


@dataclass
class IngestContext:
    raw: RawSiteState
    site_effective: dict[str, Any]  # compile_site output
    device_effective: dict[str, dict[str, Any]]  # device_id -> compile_device output
    builder: IRBuilder


class Ingester(Protocol):
    name: str

    def produces(self) -> frozenset[Capability]:
        """POTENTIAL supply — what this ingester can produce when data is present."""
        ...

    def ingest(self, ctx: IngestContext) -> frozenset[Capability]:
        """Populate the IR slice; return the capabilities ACTUALLY earned."""
        ...
```

Create `src/digital_twin/adapters/mist/ingest/registry.py`:

```python
"""Run registered domain ingesters in order; collect the capabilities EARNED.

A crashing ingester is ISOLATED into a value (IngestFailure) — per the spec's
component contract, a domain ingester failure becomes a named UNKNOWN (mapped
by the Plan-3+ pipeline), never an unhandled exception. A failed ingester
contributes no capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass

from digital_twin.ir import Capability

from .base import IngestContext, Ingester


@dataclass(frozen=True)
class IngestFailure:
    ingester: str
    error: str


@dataclass(frozen=True)
class IngestReport:
    produced: frozenset[Capability]
    failures: tuple[IngestFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class IngesterRegistry:
    def __init__(self, ingesters: list[Ingester]) -> None:
        self._ingesters = list(ingesters)

    def potential_supply(self) -> frozenset[Capability]:
        """Union of declared produces() — for capability_check wiring validation."""
        out: set[Capability] = set()
        for ingester in self._ingesters:
            out |= ingester.produces()
        return frozenset(out)

    def run(self, ctx: IngestContext) -> IngestReport:
        produced: set[Capability] = set()
        failures: list[IngestFailure] = []
        for ingester in self._ingesters:
            try:
                produced |= ingester.ingest(ctx)  # earned, not declared
            except Exception as e:  # noqa: BLE001 — isolated into a value (spec contract)
                failures.append(IngestFailure(ingester=ingester.name, error=str(e)))
        for cap in produced:
            ctx.builder.with_capability(cap)
        return IngestReport(produced=frozenset(produced), failures=tuple(failures))
```

- [ ] **Step 4: Run to verify pass** — PASS (2).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(ingest): Ingester protocol + registry with produced-capability collection"
```

---

## Task 7: `ingest/ports.py` + `ingest/switch.py` — config → entities

**Files:**
- Create: `src/digital_twin/adapters/mist/ingest/ports.py`, `src/digital_twin/adapters/mist/ingest/switch.py`
- Create: `tests/adapters/mist/fixtures.py`
- Test: `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/fixtures.py` (shared synthetic Mist-shaped data):

```python
"""Synthetic Mist-shaped payload builders shared across adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta

SITE_EFFECTIVE: dict[str, Any] = {
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {
        "office": {"mode": "access", "port_network": "corp"},
        "uplink": {"mode": "trunk", "port_network": "corp", "networks": ["voice"]},
        "all": {"mode": "trunk", "all_networks": True},
    },
}

SWITCH_A: dict[str, Any] = {
    "mac": "aa0000000001",
    "id": "dev-a",
    "type": "switch",
    "model": "EX4100-48P",
    "name": "sw-a",
    "port_config": {
        "ge-0/0/0-1": {"usage": "office"},
        "ge-0/0/47": {"usage": "uplink"},
    },
    "other_ip_configs": {"corp": {"type": "static", "ip": "10.0.10.1", "netmask": "255.255.255.0"}},
}

AP_1: dict[str, Any] = {"mac": "cc0000000001", "id": "dev-ap1", "type": "ap", "model": "AP45",
                        "name": "ap-1"}


ALL_FETCHED = ("site", "setting", "networktemplate", "devices", "device_stats",
               "port_stats", "wireless_clients", "wired_clients")


def raw_site(
    devices: tuple[dict[str, Any], ...] = (SWITCH_A, AP_1),
    port_stats: tuple[dict[str, Any], ...] = (),
    device_stats: tuple[dict[str, Any], ...] = (),
    wireless_clients: tuple[dict[str, Any], ...] = (),
    wired_clients: tuple[dict[str, Any], ...] = (),
    fetched: tuple[str, ...] = ALL_FETCHED,
) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"),
        site={"id": "s1", "networktemplate_id": None},
        setting=SITE_EFFECTIVE,  # tests treat setting as already-effective
        networktemplate=None,
        devices=devices,
        device_stats=device_stats,
        port_stats=port_stats,
        wireless_clients=wireless_clients,
        wired_clients=wired_clients,
        derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="test", fetched=fetched, failures=()),
    )
```

Create `tests/adapters/mist/test_ingest_ports.py`:

```python
from digital_twin.adapters.mist.ingest.ports import expand_port_members, usage_vlans
from tests.adapters.mist.fixtures import SITE_EFFECTIVE


def test_expand_single_port():
    assert expand_port_members("ge-0/0/47") == ["ge-0/0/47"]


def test_expand_trailing_range():
    assert expand_port_members("ge-0/0/0-2") == ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2"]


def test_expand_comma_list_mixed():
    assert expand_port_members("ge-0/0/0,ge-0/0/5-6") == ["ge-0/0/0", "ge-0/0/5", "ge-0/0/6"]


def test_usage_vlans_access():
    native, tagged = usage_vlans(SITE_EFFECTIVE["port_usages"]["office"], SITE_EFFECTIVE["networks"])
    assert native == 10 and tagged == ()


def test_usage_vlans_trunk_with_named_networks():
    native, tagged = usage_vlans(SITE_EFFECTIVE["port_usages"]["uplink"], SITE_EFFECTIVE["networks"])
    assert native == 10 and tagged == (30,)


def test_usage_vlans_trunk_all_networks():
    native, tagged = usage_vlans(SITE_EFFECTIVE["port_usages"]["all"], SITE_EFFECTIVE["networks"])
    assert native is None and set(tagged) == {10, 30}


def test_native_is_excluded_from_tagged_with_all_networks():
    # the native network is carried UNTAGGED — it must not also appear tagged
    usage = {"mode": "trunk", "all_networks": True, "port_network": "corp"}
    native, tagged = usage_vlans(usage, SITE_EFFECTIVE["networks"])
    assert native == 10 and tagged == (30,)


def test_native_is_excluded_from_tagged_with_named_networks():
    usage = {"mode": "trunk", "port_network": "corp", "networks": ["corp", "voice"]}
    native, tagged = usage_vlans(usage, SITE_EFFECTIVE["networks"])
    assert native == 10 and tagged == (30,)
```

Create `tests/adapters/mist/test_ingest_switch.py`:

```python
from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import DeviceRole, IRBuilder, IRCapability, L3Role, PortMode
from tests.adapters.mist.fixtures import SITE_EFFECTIVE, SWITCH_A, raw_site


def _ingest() -> IngestContext:
    ctx = IngestContext(raw=raw_site(), site_effective=dict(SITE_EFFECTIVE),
                        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
                        builder=IRBuilder())
    SwitchIngester().ingest(ctx)
    return ctx


def test_devices_created_for_switches_and_aps():
    ir = _ingest().builder.build()
    assert ir.device("aa0000000001").role is DeviceRole.SWITCH
    assert ir.device("cc0000000001").role is DeviceRole.AP


def test_ports_expanded_with_modes_and_vlans():
    ir = _ingest().builder.build()
    p0 = ir.port("aa0000000001:ge-0/0/0")
    assert p0.mode is PortMode.ACCESS and p0.native_vlan == 10
    p47 = ir.port("aa0000000001:ge-0/0/47")
    assert p47.mode is PortMode.TRUNK and p47.native_vlan == 10 and p47.tagged_vlans == (30,)
    assert "aa0000000001:ge-0/0/1" in ir.ports  # range expanded


def test_vlans_and_irb_exits_created():
    ir = _ingest().builder.build()
    assert ir.vlans[10].name == "corp" and ir.vlans[30].name == "voice"
    irbs = [i for i in ir.l3intfs if i.role is L3Role.IRB]
    assert len(irbs) == 1 and irbs[0].vlan_id == 10 and irbs[0].device_id == "aa0000000001"


def test_device_local_network_also_creates_vlan_entity():
    dev_eff = {**SITE_EFFECTIVE, **SWITCH_A,
               "networks": {**SITE_EFFECTIVE["networks"], "lab": {"vlan_id": 99}}}
    ctx = IngestContext(raw=raw_site(), site_effective=dict(SITE_EFFECTIVE),
                        device_effective={"aa0000000001": dev_eff}, builder=IRBuilder())
    SwitchIngester().ingest(ctx)
    assert ctx.builder.build().vlans[99].name == "lab"


def test_produces_declares_potential_capabilities():
    caps = SwitchIngester().produces()
    assert IRCapability.WIRED_L2 in caps and IRCapability.L3_EXITS in caps


def test_capabilities_earned_only_when_devices_fetched():
    ok = IngestContext(raw=raw_site(), site_effective=dict(SITE_EFFECTIVE),
                       device_effective={}, builder=IRBuilder())
    assert IRCapability.WIRED_L2 in SwitchIngester().ingest(ok)

    failed = IngestContext(raw=raw_site(fetched=("site", "setting")),
                           site_effective=dict(SITE_EFFECTIVE),
                           device_effective={}, builder=IRBuilder())
    assert SwitchIngester().ingest(failed) == frozenset()
```

- [ ] **Step 2: Run to verify fail** — FAIL.

- [ ] **Step 3: Write the implementations**

Create `src/digital_twin/adapters/mist/ingest/ports.py`:

```python
"""Port-config helpers: member expansion and usage->VLAN resolution.

Mist port_config keys may be single ports, comma lists, or trailing ranges
("ge-0/0/0-23"). Usages resolve to (native_vlan, tagged_vlans) via the effective
networks map (name -> vlan_id).
"""

from __future__ import annotations

import re
from typing import Any

_RANGE = re.compile(r"^(?P<prefix>.*?/)(?P<start>\d+)-(?P<end>\d+)$")


def expand_port_members(key: str) -> list[str]:
    members: list[str] = []
    for part in key.split(","):
        part = part.strip()
        m = _RANGE.match(part)
        if m:
            prefix = m.group("prefix")
            for n in range(int(m.group("start")), int(m.group("end")) + 1):
                members.append(f"{prefix}{n}")
        else:
            members.append(part)
    return members


def usage_vlans(
    usage: dict[str, Any], networks: dict[str, Any]
) -> tuple[int | None, tuple[int, ...]]:
    """(native_vlan, tagged_vlans) for a port usage, resolved via `networks`.

    The native network is carried UNTAGGED, so it is always excluded from the
    tagged set (Plan 1's link_carried_vlans handles the native via the
    matching-natives path; double-listing it would carry it through the tagged
    path even on a native mismatch).
    """

    def vlan_of(name: str | None) -> int | None:
        if not name or name not in networks:
            return None
        vid = networks[name].get("vlan_id")
        return int(vid) if vid is not None else None

    native = vlan_of(usage.get("port_network"))
    if usage.get("mode") != "trunk":
        return native, ()
    names = list(networks) if usage.get("all_networks") else list(usage.get("networks") or [])
    tagged = tuple(
        sorted(v for v in (vlan_of(n) for n in names) if v is not None and v != native)
    )
    return native, tagged
```

Create `src/digital_twin/adapters/mist/ingest/switch.py`:

```python
"""Switch-domain ingester: effective config -> Device/Port/Vlan/L3Intf entities.

Reads device_effective (per-device compiled config) for ports/L3, and the raw
device list for identity (mac/model/role). APs become leaf Device entities here
(their links/clients come from the lldp/clients ingesters).
"""

from __future__ import annotations

from typing import Any

from digital_twin.ir import (
    Device,
    DeviceRole,
    IRCapability,
    L3Intf,
    L3Role,
    Port,
    PortMode,
    Vlan,
    device_id,
    port_id,
)

from .base import IngestContext
from .ports import expand_port_members, usage_vlans

_ROLE = {"switch": DeviceRole.SWITCH, "ap": DeviceRole.AP, "gateway": DeviceRole.GATEWAY}


class SwitchIngester:
    name = "switch"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "devices" not in ctx.raw.meta.fetched:
            return frozenset()  # no device data -> nothing earned, nothing claimed
        self._devices(ctx)
        self._vlans(ctx)
        for dev in ctx.raw.devices:
            if dev.get("type") == "switch":
                self._switch_ports_and_l3(ctx, dev)
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def _devices(self, ctx: IngestContext) -> None:
        for dev in ctx.raw.devices:
            role = _ROLE.get(str(dev.get("type")))
            if role is None or not dev.get("mac"):
                continue
            ctx.builder.add_device(
                Device(id=device_id(str(dev["mac"])), role=role,
                       site=ctx.raw.scope.site_id, model=dev.get("model"))
            )

    def _vlans(self, ctx: IngestContext) -> None:
        # VLANs come from the site effective AND every device effective — a
        # device-local network must still yield a Vlan entity (per-VLAN graphs
        # enumerate ir.vlans; a missing entity would hide it from analysis).
        seen: set[int] = set()
        sources: list[dict[str, Any]] = [ctx.site_effective, *ctx.device_effective.values()]
        for eff in sources:
            for name, net in (eff.get("networks") or {}).items():
                vid = net.get("vlan_id")
                if vid is not None and int(vid) not in seen:
                    seen.add(int(vid))
                    ctx.builder.add_vlan(
                        Vlan(vlan_id=int(vid), name=name, scope=ctx.raw.scope.site_id)
                    )

    def _switch_ports_and_l3(self, ctx: IngestContext, dev: dict[str, Any]) -> None:
        did = device_id(str(dev["mac"]))
        eff = ctx.device_effective.get(did) or ctx.site_effective
        networks: dict[str, Any] = eff.get("networks") or {}
        usages: dict[str, Any] = eff.get("port_usages") or {}
        for key, pc in (eff.get("port_config") or {}).items():
            usage = usages.get(str(pc.get("usage")), {})
            native, tagged = usage_vlans(usage, networks)
            mode = PortMode.TRUNK if usage.get("mode") == "trunk" else PortMode.ACCESS
            for member in expand_port_members(key):
                ctx.builder.add_port(
                    Port(id=port_id(did, member), device_id=did, name=member, mode=mode,
                         native_vlan=native, tagged_vlans=tagged,
                         profile=str(pc.get("usage")) if pc.get("usage") else None)
                )
        for net_name, ipc in (eff.get("other_ip_configs") or {}).items():
            vid = (networks.get(net_name) or {}).get("vlan_id")
            if vid is not None:
                ctx.builder.add_l3intf(
                    L3Intf(device_id=did, role=L3Role.IRB, vlan_id=int(vid),
                           ip=ipc.get("ip"), subnet=None)
                )
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py -q` → PASS (10).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(ingest): switch ingester (port expansion, usage->vlan, IRB exits)"
```

---

## Task 8: `ingest/lldp.py` — links (two-sided matching, bundles, STP)

**Files:**
- Modify: `src/digital_twin/ir/model.py` (public `has_device`/`has_port`/`replace_port`)
- Create: `src/digital_twin/adapters/mist/ingest/lldp.py`
- Test: `tests/ir/test_model.py` (extend), `tests/adapters/mist/test_ingest_lldp.py`

- [ ] **Step 0: Extend `IRBuilder` with the public lookups ingesters need**

Append to `tests/ir/test_model.py`:

```python
def test_builder_has_and_replace_port():
    b = IRBuilder().add_device(sw("d1")).add_port(trunk_port("d1", "a"))
    assert b.has_device("d1") is True and b.has_device("dX") is False
    assert b.has_port("d1:a") is True and b.has_port("d1:b") is False
    from dataclasses import replace as dc_replace

    updated = dc_replace(b._ports["d1:a"], stp_state="forwarding")  # noqa: SLF001 (test)
    b.replace_port(updated)
    assert b.build().port("d1:a").stp_state == "forwarding"


def test_replace_unknown_port_rejected():
    with pytest.raises(IRValidationError):
        IRBuilder().replace_port(trunk_port("d1", "a"))
```

Append to `IRBuilder` in `src/digital_twin/ir/model.py`:

```python
    def has_device(self, did: str) -> bool:
        return did in self._devices

    def has_port(self, pid: str) -> bool:
        return pid in self._ports

    def has_client(self, mac: str) -> bool:
        return client_id(mac) in self._client_ids

    def get_port(self, pid: str) -> Port:
        return self._ports[pid]

    def replace_port(self, port: Port) -> IRBuilder:
        """Replace an already-added port (same id) — used by ingesters to enrich
        config-built ports with observed live facts (e.g. STP state)."""
        if port.id not in self._ports:
            raise IRValidationError(f"cannot replace unknown port {port.id}")
        self._ports[port.id] = port
        return self
```

(Also add `client_id` to the existing `from .entities import ...` line in `model.py`.)

Run: `uv run pytest tests/ir/test_model.py -q` → PASS.

**Assumed stat shapes (pinned by `tools/probe_fetch.py` in Task 11 — adjust mapping there if reality differs):**
- Switch port stats (`port_stats`): `{mac, port_id, up, neighbor_mac, neighbor_port_id, neighbor_system_name, lldp?, aggregated?, lag_name?, stp_state?, stp_role?}`
- AP device stats (`device_stats`, `type=="ap"`): `{mac, type, lldp_stat: {lldp_system_name, port_id|port_desc, system_name, mgmt_addr, ...}}` where `lldp_stat.port_id` names the switch port.

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_ingest_lldp.py`:

```python
from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.lldp import LldpIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import ConfidenceLevel, IRBuilder, LinkKind, Port, PortMode, port_id
from tests.adapters.mist.fixtures import AP_1, SITE_EFFECTIVE, SWITCH_A, raw_site

SWITCH_B = {**SWITCH_A, "mac": "bb0000000002", "id": "dev-b", "name": "sw-b"}


def _ctx(port_stats, device_stats=()) -> IngestContext:
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, SWITCH_B, AP_1), port_stats=tuple(port_stats),
                     device_stats=tuple(device_stats)),
        site_effective=dict(SITE_EFFECTIVE), device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    # ensure stat-referenced ports exist even without port_config entries
    for did, name in (("aa0000000001", "ge-0/0/47"), ("bb0000000002", "ge-0/0/47"),
                      ("aa0000000001", "ge-0/0/10")):
        pid = port_id(did, name)
        if not ctx.builder.has_port(pid):
            ctx.builder.add_port(Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK))
    LldpIngester().ingest(ctx)
    return ctx


def test_two_sided_lldp_creates_one_high_confidence_link():
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True,
         "neighbor_mac": "bb0000000002", "neighbor_port_id": "ge-0/0/47"},
        {"mac": "bb0000000002", "port_id": "ge-0/0/47", "up": True,
         "neighbor_mac": "aa0000000001", "neighbor_port_id": "ge-0/0/47"},
    ]
    ir = _ctx(stats).builder.build()
    assert len(ir.links) == 1
    assert ir.links[0].meta.confidence.level is ConfidenceLevel.HIGH


def test_one_sided_lldp_creates_low_confidence_link():
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True,
              "neighbor_mac": "bb0000000002", "neighbor_port_id": "ge-0/0/47"}]
    ir = _ctx(stats).builder.build()
    assert len(ir.links) == 1
    assert ir.links[0].meta.confidence.level is ConfidenceLevel.LOW


def test_lag_members_get_bundle_id():
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "aggregated": True,
         "lag_name": "ae0", "neighbor_mac": "bb0000000002", "neighbor_port_id": "ge-0/0/47"},
        {"mac": "bb0000000002", "port_id": "ge-0/0/47", "up": True, "aggregated": True,
         "lag_name": "ae0", "neighbor_mac": "aa0000000001", "neighbor_port_id": "ge-0/0/47"},
    ]
    ir = _ctx(stats).builder.build()
    assert ir.links[0].kind is LinkKind.LAG and ir.links[0].bundle_id == "ae0"


def test_stp_state_attached_to_port_when_present():
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True,
              "stp_state": "forwarding", "stp_role": "designated"}]
    ctx = _ctx(stats)
    ir = ctx.builder.build()
    p = ir.port("aa0000000001:ge-0/0/10")
    assert p.stp_state == "forwarding"
    assert p.stp_meta is not None  # observed live fact


def test_ap_uplink_link_from_ap_lldp_stat():
    device_stats = [{"mac": "cc0000000001", "type": "ap",
                     "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/10",
                                   "mgmt_addr": "10.0.10.1"}}]
    ctx = _ctx([], device_stats)
    ir = ctx.builder.build()
    ap_links = [link for link in ir.links if "cc0000000001" in link.id]
    assert len(ap_links) == 1


def test_unmanaged_lldp_neighbor_becomes_wired_edge_client_not_link():
    # a printer/unmanaged router reported by LLDP: no Link (device unknown),
    # but a wired Client on the local port — it stays in the impact surface
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True,
              "neighbor_mac": "99eeddccbbaa", "neighbor_port_id": "p1"}]
    ir = _ctx(stats).builder.build()  # build() must NOT crash
    assert ir.links == ()
    edge = [c for c in ir.clients if c.mac == "99eeddccbbaa"]
    assert len(edge) == 1
    assert edge[0].attach_id == "aa0000000001:ge-0/0/10"
    assert "unmanaged LLDP neighbor" in edge[0].meta.confidence.reasons[0]


def test_ap_corroboration_requires_the_switch_to_name_that_ap():
    # the switch port reports SOME neighbor, but not the AP -> AP link stays LOW
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True,
              "neighbor_mac": "bb0000000002", "neighbor_port_id": "ge-0/0/47"}]
    device_stats = [{"mac": "cc0000000001", "type": "ap",
                     "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/10"}}]
    ir = _ctx(stats, device_stats).builder.build()
    ap_link = next(link for link in ir.links if "cc0000000001" in link.id)
    assert ap_link.meta.confidence.level is ConfidenceLevel.LOW


def test_switch_reporting_ap_yields_one_link_not_duplicates():
    # both the switch port-stat claim AND the AP lldp_stat describe the same
    # physical link -> exactly ONE Link entity (no duplicate-id crash)
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True,
              "neighbor_mac": "cc0000000001", "neighbor_port_id": "eth0"}]
    device_stats = [{"mac": "cc0000000001", "type": "ap",
                     "lldp_stat": {"system_name": "sw-a", "port_id": "ge-0/0/10"}}]
    ir = _ctx(stats, device_stats).builder.build()
    ap_links = [link for link in ir.links if "cc0000000001" in link.id]
    assert len(ap_links) == 1


def test_stp_capability_earned_only_when_stp_rows_seen():
    from digital_twin.ir import IRCapability

    # _ctx runs LldpIngester once internally; ingest() is re-run here purely to
    # capture the return value (idempotent on these inputs: stats unchanged).
    no_stp = _ctx([{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True}])
    assert IRCapability.STP_STATE not in LldpIngester().ingest(no_stp)

    with_stp = _ctx([{"mac": "aa0000000001", "port_id": "ge-0/0/10", "up": True,
                      "stp_state": "forwarding"}])
    assert IRCapability.STP_STATE in LldpIngester().ingest(with_stp)
    assert with_stp.builder.build().port("aa0000000001:ge-0/0/10").stp_state == "forwarding"
```

- [ ] **Step 2: Run to verify fail** — FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/adapters/mist/ingest/lldp.py`:

```python
"""LLDP-domain ingester: links from port stats + AP lldp_stat, with honesty rules.

- Both ends report each other        -> one Link, LLDP_TWO_SIDED (HIGH).
- Only one MANAGED end reports       -> one Link, LLDP_ONE_SIDED (LOW).
- Neighbor is NOT a Mist device      -> NO Link; the neighbor becomes a wired
  edge-device Client on the local port (user decision: printers/unmanaged
  routers stay in the impact surface — VLAN continuity, DHCP, routing, FW).
- aggregated/lag_name                -> LinkKind.LAG with bundle_id.
- stp_state on a port                -> Port.stp_state + stp_meta (OBSERVED);
  stp.state capability is EARNED only if >=1 such row was applied.
- AP lldp_stat names switch + port   -> AP uplink link; two-sided only when the
  switch's own claims name THAT AP back (not just any neighbor). A shared
  emitted-set prevents the same physical link being added twice.

Ports referenced by stats but absent from config are added as minimal OBSERVED
trunk ports (cannot invent VLANs). Stat shapes pinned by tools/probe_fetch.py.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from digital_twin.ir import (
    AttachKind,
    Client,
    ClientKind,
    IRCapability,
    Link,
    LinkKind,
    Port,
    PortMode,
    Provenance,
    client_id,
    device_id,
    fact_meta,
    link_id,
    port_id,
)

from .base import IngestContext

_Json = dict[str, Any]


class LldpIngester:
    name = "lldp"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.STP_STATE})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        claims = self._claims(ctx)
        stp_seen = self._apply_stp(ctx)
        emitted: set[str] = set()
        self._emit_links(ctx, claims, emitted)
        self._emit_ap_uplinks(ctx, claims, emitted)
        return frozenset({IRCapability.STP_STATE}) if stp_seen else frozenset()

    # -- claims ---------------------------------------------------------------
    def _claims(self, ctx: IngestContext) -> dict[tuple[str, str], _Json]:
        """(reporter_port_id_global, claimed_neighbor_port_id_global) -> stat row."""
        out: dict[tuple[str, str], _Json] = {}
        for row in ctx.raw.port_stats:
            if not row.get("neighbor_mac") or not row.get("port_id"):
                continue
            src = port_id(device_id(str(row["mac"])), str(row["port_id"]))
            dst = port_id(device_id(str(row["neighbor_mac"])),
                          str(row.get("neighbor_port_id") or "?"))
            out[(src, dst)] = row
        return out

    # -- STP ------------------------------------------------------------------
    def _apply_stp(self, ctx: IngestContext) -> bool:
        seen = False
        for row in ctx.raw.port_stats:
            if row.get("stp_state") is None or not row.get("port_id"):
                continue
            pid = port_id(device_id(str(row["mac"])), str(row["port_id"]))
            self._ensure_port(ctx, pid)
            ctx.builder.replace_port(replace(
                ctx.builder.get_port(pid), stp_state=str(row["stp_state"]),
                stp_enabled=True, stp_meta=fact_meta(Provenance.OBSERVED),
            ))
            seen = True
        return seen

    # -- links ----------------------------------------------------------------
    def _emit_links(self, ctx: IngestContext, claims: dict[tuple[str, str], _Json],
                    emitted: set[str]) -> None:
        for (src, dst), row in claims.items():
            neighbor_dev = dst.partition(":")[0]
            if not ctx.builder.has_device(neighbor_dev):
                self._edge_device_client(ctx, src, neighbor_dev)
                continue
            lid = link_id(src, dst)
            if lid in emitted:
                continue
            emitted.add(lid)
            two_sided = (dst, src) in claims
            prov = Provenance.LLDP_TWO_SIDED if two_sided else Provenance.LLDP_ONE_SIDED
            reasons = () if two_sided else (f"link {lid} seen from {src} only",)
            kind, bundle = self._kind(row, claims.get((dst, src)))
            for pid in (src, dst):
                self._ensure_port(ctx, pid)
            ctx.builder.add_link(Link(id=lid, a_port=src, b_port=dst, kind=kind,
                                      bundle_id=bundle, meta=fact_meta(prov, reasons)))

    def _edge_device_client(self, ctx: IngestContext, local_port: str, mac: str) -> None:
        """An unmanaged LLDP neighbor is an EDGE DEVICE = wired client on this port
        (its VLAN continuity / DHCP / routing / FW exposure must stay visible)."""
        if ctx.builder.has_client(mac):
            return
        ctx.builder.add_client(Client(
            mac=client_id(mac), kind=ClientKind.WIRED, attach_kind=AttachKind.PORT,
            attach_id=local_port,
            meta=fact_meta(Provenance.OBSERVED, ("unmanaged LLDP neighbor (edge device)",)),
        ))

    def _kind(self, a: _Json, b: _Json | None) -> tuple[LinkKind, str | None]:
        for row in (a, b or {}):
            if row.get("aggregated") or row.get("lag_name"):
                return LinkKind.LAG, str(row.get("lag_name") or "lag")
        return LinkKind.PHYSICAL, None

    def _emit_ap_uplinks(self, ctx: IngestContext, claims: dict[tuple[str, str], _Json],
                         emitted: set[str]) -> None:
        switch_by_name = {str(d.get("name")): device_id(str(d["mac"]))
                          for d in ctx.raw.devices if d.get("type") == "switch" and d.get("mac")}
        for stat in ctx.raw.device_stats:
            if stat.get("type") != "ap" or not stat.get("mac"):
                continue
            lldp = stat.get("lldp_stat") or {}
            sw_id = switch_by_name.get(str(lldp.get("system_name")))
            sw_port_name = lldp.get("port_id") or lldp.get("port_desc")
            if not sw_id or not sw_port_name:
                continue
            ap_id = device_id(str(stat["mac"]))
            ap_port = port_id(ap_id, "eth0")
            sw_port = port_id(sw_id, str(sw_port_name))
            lid = link_id(ap_port, sw_port)
            if lid in emitted or any(  # switch-side claim already produced this link
                link_id(src, dst) == lid for (src, dst) in claims if src == sw_port
            ):
                continue
            emitted.add(lid)
            # two-sided only if the switch's claim names THIS AP (not just anyone)
            corroborated = any(
                src == sw_port and dst.partition(":")[0] == ap_id for (src, dst) in claims
            )
            prov = Provenance.LLDP_TWO_SIDED if corroborated else Provenance.LLDP_ONE_SIDED
            for pid, did, name in ((ap_port, ap_id, "eth0"), (sw_port, sw_id, str(sw_port_name))):
                self._ensure_port(ctx, pid, did, name)
            ctx.builder.add_link(Link(id=lid, a_port=ap_port, b_port=sw_port,
                                      kind=LinkKind.PHYSICAL, meta=fact_meta(prov)))

    # -- helpers ----------------------------------------------------------------
    def _ensure_port(self, ctx: IngestContext, pid: str,
                     did: str | None = None, name: str | None = None) -> None:
        if ctx.builder.has_port(pid):
            return
        d, _, n = pid.partition(":")
        ctx.builder.add_port(Port(id=pid, device_id=did or d, name=name or n,
                                  mode=PortMode.TRUNK,
                                  meta=fact_meta(Provenance.OBSERVED)))
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest -q` → all tests pass (incl. the new `IRBuilder` ones).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(ingest): LLDP links (two-sided honesty, bundles, STP observed facts, AP uplinks)"
```

---

## Task 9: `ingest/clients.py` — wired + wireless clients

**Files:**
- Create: `src/digital_twin/adapters/mist/ingest/clients.py`
- Test: `tests/adapters/mist/test_ingest_clients.py`

**Assumed shapes (probe-pinned):** wireless `{mac, ap_mac, vlan_id?, ip?}`; wired (search results) `{mac, device_mac, port_id, vlan?, ip?}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_ingest_clients.py`:

```python
from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.clients import ClientsIngester
from digital_twin.adapters.mist.ingest.switch import SwitchIngester
from digital_twin.ir import AttachKind, IRBuilder, IRCapability
from tests.adapters.mist.fixtures import SITE_EFFECTIVE, SWITCH_A, raw_site


def _ingest(wireless=(), wired=()):
    ctx = IngestContext(
        raw=raw_site(wireless_clients=tuple(wireless), wired_clients=tuple(wired)),
        site_effective=dict(SITE_EFFECTIVE),
        device_effective={"aa0000000001": {**SITE_EFFECTIVE, **SWITCH_A}},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ClientsIngester().ingest(ctx)
    return ctx.builder.build()


def test_wireless_client_attaches_to_ap_with_vlan():
    ir = _ingest(wireless=[{"mac": "11:22:33:44:55:66", "ap_mac": "cc0000000001", "vlan_id": 30}])
    c = ir.clients[0]
    assert c.attach_kind is AttachKind.AP and c.attach_id == "cc0000000001" and c.vlan == 30


def test_wired_client_attaches_to_port():
    ir = _ingest(wired=[{"mac": "667788990011", "device_mac": "aa0000000001",
                         "port_id": "ge-0/0/0", "vlan": 10}])
    c = ir.clients[0]
    assert c.attach_kind is AttachKind.PORT and c.attach_id == "aa0000000001:ge-0/0/0"
    assert c.vlan == 10


def test_client_referencing_unknown_attachment_is_skipped_not_fatal():
    ir = _ingest(wireless=[{"mac": "aa", "ap_mac": "ffffffffffff", "vlan_id": 1}])
    assert ir.clients == ()


def test_capability_earned_only_when_both_client_fetches_succeeded():
    ctx = IngestContext(
        raw=raw_site(fetched=("site", "setting", "devices", "wireless_clients")),  # wired missing
        site_effective=dict(SITE_EFFECTIVE), device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    assert ClientsIngester().ingest(ctx) == frozenset()


def test_zero_clients_with_successful_fetches_still_earns_capability():
    ctx = IngestContext(raw=raw_site(), site_effective=dict(SITE_EFFECTIVE),
                        device_effective={}, builder=IRBuilder())
    SwitchIngester().ingest(ctx)
    assert IRCapability.CLIENTS_ACTIVE in ClientsIngester().ingest(ctx)


def test_produces_capability():
    assert IRCapability.CLIENTS_ACTIVE in ClientsIngester().produces()
```

- [ ] **Step 2: Run to verify fail** — FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/adapters/mist/ingest/clients.py`:

```python
"""Clients-domain ingester: observed wired + wireless clients (active now).

- A client referencing an unknown AP/port is SKIPPED (stale stats are not fatal)
  — the IRBuilder would rightly reject the dangling reference otherwise.
- A MAC already present (e.g. added by LldpIngester as an unmanaged edge device)
  is skipped — first writer wins, no duplicate-id crash.
- clients.active is EARNED only if BOTH client fetches succeeded: an empty site
  with successful fetches legitimately knows "no clients"; a failed fetch must
  not masquerade as that knowledge.
"""

from __future__ import annotations

from digital_twin.ir import (
    AttachKind,
    Client,
    ClientKind,
    IRCapability,
    client_id,
    device_id,
    port_id,
)

from .base import IngestContext


class ClientsIngester:
    name = "clients"

    def produces(self) -> frozenset[str]:  # potential supply
        return frozenset({IRCapability.CLIENTS_ACTIVE})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        fetched = ctx.raw.meta.fetched
        if "wireless_clients" not in fetched or "wired_clients" not in fetched:
            return frozenset()  # failed fetch -> no claim (zero clients != unknown)
        for w in ctx.raw.wireless_clients:
            if not w.get("mac") or not w.get("ap_mac"):
                continue
            ap = device_id(str(w["ap_mac"]))
            if not ctx.builder.has_device(ap) or ctx.builder.has_client(str(w["mac"])):
                continue  # stale stat / already known edge device
            vlan = w.get("vlan_id")
            ctx.builder.add_client(Client(
                mac=client_id(str(w["mac"])), kind=ClientKind.WIRELESS,
                attach_kind=AttachKind.AP, attach_id=ap,
                vlan=int(vlan) if vlan is not None else None, ip=w.get("ip")))
        for w in ctx.raw.wired_clients:
            if not w.get("mac") or not w.get("device_mac") or not w.get("port_id"):
                continue
            pid = port_id(device_id(str(w["device_mac"])), str(w["port_id"]))
            if not ctx.builder.has_port(pid) or ctx.builder.has_client(str(w["mac"])):
                continue
            vlan = w.get("vlan")
            ctx.builder.add_client(Client(
                mac=client_id(str(w["mac"])), kind=ClientKind.WIRED,
                attach_kind=AttachKind.PORT, attach_id=pid,
                vlan=int(vlan) if vlan is not None else None, ip=w.get("ip")))
        return frozenset({IRCapability.CLIENTS_ACTIVE})
```

- [ ] **Step 4: Run to verify pass** — PASS (4).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(ingest): clients ingester (observed wired/wireless, stale-stat tolerant)"
```

---

## Task 10: `engine/capability_check.py`

**Files:**
- Create: `src/digital_twin/engine/capability_check.py`
- Test: `tests/engine/test_capability_check.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/engine/test_capability_check.py`:

```python
import pytest

from digital_twin.engine.capability_check import CapabilityGapError, validate_supply


def test_ok_when_all_requirements_have_producers():
    validate_supply(produced=frozenset({"wired.l2", "stp.state"}),
                    required=frozenset({"wired.l2"}))


def test_missing_producer_raises_with_names():
    with pytest.raises(CapabilityGapError) as e:
        validate_supply(produced=frozenset({"wired.l2"}),
                        required=frozenset({"wired.l2", "analysis.reachability"}))
    assert "analysis.reachability" in str(e.value)


def test_explicit_not_yet_supported_is_allowed():
    validate_supply(produced=frozenset(), required=frozenset({"wan.tunnels"}),
                    not_yet_supported=frozenset({"wan.tunnels"}))
```

- [ ] **Step 2: Run to verify fail** — FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/engine/capability_check.py`:

```python
"""Capability supply/demand validation (the silent-blind-spot killer).

Every required capability must have a producer, or be explicitly declared
not-yet-supported. Run as a test/startup assertion so a consumer added without
its producer fails LOUDLY instead of returning INSUFFICIENT_DATA forever.
Consumers (checks/analyzers) arrive in Plan 4 and feed `required`.
"""

from __future__ import annotations

from digital_twin.ir import Capability


class CapabilityGapError(RuntimeError):
    pass


def validate_supply(
    produced: frozenset[Capability],
    required: frozenset[Capability],
    not_yet_supported: frozenset[Capability] = frozenset(),
) -> None:
    missing = required - produced - not_yet_supported
    if missing:
        raise CapabilityGapError(
            "required capabilities with no producer: " + ", ".join(sorted(missing))
        )
```

- [ ] **Step 4: Run to verify pass** — PASS (3).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(engine): capability supply/demand validation"
```

---

## Phase B — live (network, probe-first)

## Task 11: `providers/mist_api.py` + probe script

**Files:**
- Create: `src/digital_twin/providers/mist_api.py`, `tools/probe_fetch.py`
- Test: `tests/providers/test_mist_api_live.py` (`@pytest.mark.live`)

- [ ] **Step 1: Write the provider**

Create `src/digital_twin/providers/mist_api.py`:

```python
"""MistApiProvider: on-demand single-site fetch via the mistapi SDK.

Every endpoint call is isolated in a small private method so the probe script
(tools/probe_fetch.py) can validate exact SDK call names / response shapes per
SDK release, and a fix is a one-liner. Partial failures are RECORDED in
StateMeta; a failed BASELINE fetch (site/setting) returns a FetchError VALUE —
this provider never raises for fetch problems.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import mistapi

from .base import FetchError, FetchFailure, RawSiteState, SiteScope, StateMeta, StateProvider

_Json = dict[str, Any]


class MistApiProvider(StateProvider):
    def __init__(self, host: str | None = None, apitoken: str | None = None) -> None:
        self._host = host or os.environ["MIST_HOST"]
        self._session = mistapi.APISession(
            host=self._host, apitoken=apitoken or os.environ["MIST_APITOKEN"]
        )

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        fetched: list[str] = []
        failures: list[FetchFailure] = []

        def attempt(name: str, fn: Callable[[], Any], default: Any) -> Any:
            try:
                result = fn()
                fetched.append(name)
                return result
            except Exception as e:  # noqa: BLE001 — recorded, surfaced via StateMeta
                failures.append(FetchFailure(object=name, error=str(e)))
                return default

        # baseline: without site+setting there is nothing to simulate against
        site = attempt("site", lambda: self._site(scope), None)
        setting = attempt("setting", lambda: self._setting(scope), None)
        if site is None or setting is None:
            return FetchError(scope=scope, failures=tuple(failures),
                              acquired_at=datetime.now(UTC), host=self._host)

        nt_id = site.get("networktemplate_id")
        networktemplate = (
            attempt("networktemplate", lambda: self._networktemplate(scope, str(nt_id)), None)
            if nt_id
            else None
        )
        derived = (
            attempt("derived_setting", lambda: self._derived(scope), None)
            if include_derived
            else None
        )
        return RawSiteState(
            scope=scope,
            site=site,
            setting=setting,
            networktemplate=networktemplate,
            devices=tuple(attempt("devices", lambda: self._devices(scope), [])),
            device_stats=tuple(attempt("device_stats", lambda: self._device_stats(scope), [])),
            port_stats=tuple(attempt("port_stats", lambda: self._port_stats(scope), [])),
            wireless_clients=tuple(
                attempt("wireless_clients", lambda: self._wireless_clients(scope), [])
            ),
            wired_clients=tuple(attempt("wired_clients", lambda: self._wired_clients(scope), [])),
            derived_setting=derived,
            meta=StateMeta(acquired_at=datetime.now(UTC), host=self._host,
                           fetched=tuple(fetched), failures=tuple(failures)),
        )

    # -- one private helper per endpoint (probe-validated names) ---------------
    def _site(self, s: SiteScope) -> _Json:
        return dict(mistapi.api.v1.sites.sites.getSiteInfo(self._session, s.site_id).data)

    def _setting(self, s: SiteScope) -> _Json:
        return dict(mistapi.api.v1.sites.setting.getSiteSetting(self._session, s.site_id).data)

    def _derived(self, s: SiteScope) -> _Json:
        return dict(
            mistapi.api.v1.sites.setting.getSiteSettingDerived(self._session, s.site_id).data
        )

    def _networktemplate(self, s: SiteScope, nt_id: str) -> _Json:
        return dict(
            mistapi.api.v1.orgs.networktemplates.getOrgNetworkTemplate(
                self._session, s.org_id, nt_id
            ).data
        )

    def _devices(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.devices.listSiteDevices(self._session, s.site_id, type="all")
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _device_stats(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.stats.devices.listSiteDevicesStats(
            self._session, s.site_id, type="all"
        )
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _port_stats(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.stats.ports.searchSiteSwOrGwPorts(
            self._session, s.site_id, limit=1000
        )
        return [dict(d) for d in (resp.data or {}).get("results", [])]

    def _wireless_clients(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.stats.clients.listSiteWirelessClientsStats(
            self._session, s.site_id
        )
        return [dict(d) for d in mistapi.get_all(self._session, resp)]

    def _wired_clients(self, s: SiteScope) -> list[_Json]:
        resp = mistapi.api.v1.sites.wired_clients.searchSiteWiredClients(
            self._session, s.site_id, limit=1000
        )
        return [dict(d) for d in (resp.data or {}).get("results", [])]
```

- [ ] **Step 2: Write the probe script**

Create `tools/probe_fetch.py`:

```python
"""Live probe: fetch one site and dump every raw payload to .cache/ (git-ignored).

Purpose: PIN the real response shapes and SDK call names the adapter assumes
(port stats LLDP/STP/LAG fields, AP lldp_stat, client fields). Run this FIRST on
a real org; fix providers/mist_api.py helpers and ingest field mappings if any
assumption fails; only then run the equivalence gate.

Usage: uv run python tools/probe_fetch.py <org_id> <site_id>
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from digital_twin.providers.base import FetchError, SiteScope
from digital_twin.providers.mist_api import MistApiProvider

OUT = Path(".cache/probe")


def main() -> None:
    org_id, site_id = sys.argv[1], sys.argv[2]
    raw = MistApiProvider().fetch_site(SiteScope(org_id, site_id), include_derived=True)
    if isinstance(raw, FetchError):
        sys.exit(f"baseline fetch failed: {[f'{f.object}: {f.error}' for f in raw.failures]}")
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ("site", "setting", "networktemplate", "devices", "device_stats",
                 "port_stats", "wireless_clients", "wired_clients", "derived_setting"):
        (OUT / f"{name}.json").write_text(json.dumps(getattr(raw, name), indent=1, default=str))
    (OUT / "meta.json").write_text(json.dumps(asdict(raw.meta), indent=1, default=str))
    print(f"fetched: {raw.meta.fetched}")
    print(f"failures: {[f.object for f in raw.meta.failures]}")
    print(f"dumped to {OUT}/ — inspect port_stats.json for LLDP/STP/LAG field names")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Write the live smoke test**

Create `tests/providers/test_mist_api_live.py`:

```python
import os

import pytest

from digital_twin.providers.base import SiteScope

pytestmark = pytest.mark.live

_REQUIRED_ENV = ("MIST_HOST", "MIST_APITOKEN", "DT_GATE_ORG_ID", "DT_GATE_SITE_IDS")
requires_env = pytest.mark.skipif(
    not all(os.environ.get(v) for v in _REQUIRED_ENV),
    reason=f"live env not configured (need {', '.join(_REQUIRED_ENV)})",
)


@requires_env
def test_fetch_site_returns_baseline_and_meta():
    from digital_twin.providers.base import RawSiteState
    from digital_twin.providers.mist_api import MistApiProvider

    org_id = os.environ["DT_GATE_ORG_ID"]
    site_id = os.environ["DT_GATE_SITE_IDS"].split(",")[0]
    raw = MistApiProvider().fetch_site(SiteScope(org_id, site_id))
    assert isinstance(raw, RawSiteState), f"baseline fetch failed: {raw}"
    assert raw.setting is not None
    assert "site" in raw.meta.fetched and "setting" in raw.meta.fetched
```

(Add `DT_GATE_ORG_ID=` to `.env.example`.)

- [ ] **Step 4: Run the probe against a real org and fix mappings**

```bash
uv run python tools/probe_fetch.py <org_id> <site_id>
```

Expected: dumps written; `failures: []`. **This step is the checkpoint where assumed SDK call names and stat field names get corrected** — fix the `_`-helpers in `mist_api.py` and the field mappings in `ingest/lldp.py`/`ingest/clients.py` to match reality, then re-run offline tests (`uv run pytest -q`) and the live smoke (`uv run pytest -m live -q`).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(providers): MistApiProvider (mistapi) + live probe + live smoke test"
```

---

## Task 12: `compile/equivalence.py` — normalize, compare, coverage

**Files:**
- Create: `src/digital_twin/adapters/mist/compile/equivalence.py`, `src/digital_twin/adapters/mist/compile/divergences.json`
- Test: `tests/adapters/mist/test_equivalence.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/adapters/mist/test_equivalence.py`:

```python
from digital_twin.adapters.mist.compile.equivalence import (
    attribute_coverage,
    compare_effective,
)


def test_identical_configs_have_no_diffs():
    cfg = {"networks": {"corp": {"vlan_id": 10}}}
    assert compare_effective(ours=cfg, derived=cfg).diffs == ()


def test_numeric_string_normalization():
    r = compare_effective(ours={"v": "100"}, derived={"v": 100})
    assert r.diffs == ()


def test_absent_vs_null_vs_empty_are_equal():
    r = compare_effective(ours={"a": None, "b": {}}, derived={})
    assert r.diffs == ()


def test_real_difference_reported_with_path():
    r = compare_effective(ours={"networks": {"corp": {"vlan_id": 10}}},
                          derived={"networks": {"corp": {"vlan_id": 11}}})
    assert [d.path for d in r.diffs] == ["networks.corp.vlan_id"]


def test_catalogued_divergence_is_separated_not_failed():
    r = compare_effective(ours={"x": 1}, derived={"x": 2},
                          catalogued=("x",))
    assert r.diffs == () and [d.path for d in r.catalogued_diffs] == ["x"]


def test_divergence_entries_without_reason_are_rejected(tmp_path, monkeypatch):
    import digital_twin.adapters.mist.compile.equivalence as eq

    bad = tmp_path / "divergences.json"
    bad.write_text('{"entries": [{"path": "x"}]}')
    monkeypatch.setattr(eq, "_DIVERGENCES", bad)
    import pytest

    with pytest.raises(ValueError, match="without a reason"):
        eq.load_catalogued()


def test_attribute_coverage_lists_exercised_and_missing_leaves():
    schema = {"type": "object", "properties": {
        "a": {"type": "string"},
        "b": {"type": "object", "properties": {"c": {"type": "integer"}}},
        "d": {"type": "string"},
    }}
    cov = attribute_coverage(schema, [{"a": "x", "b": {"c": 1}}])
    assert "a" in cov.covered and "b.c" in cov.covered
    assert "d" in cov.uncovered


def test_attribute_coverage_sees_leaves_behind_composition():
    # same normalization as the Tier-1 generator: anyOf/allOf leaves still count
    schema = {"type": "object", "properties": {
        "vlan": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        "extra": {"allOf": [{"type": "object", "properties": {"x": {"type": "string"}}}]},
    }}
    cov = attribute_coverage(schema, [{"vlan": 30}])
    assert "vlan" in cov.covered
    assert "extra.x" in cov.uncovered  # visible despite the allOf wrapper
```

- [ ] **Step 2: Run to verify fail** — FAIL.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/adapters/mist/compile/divergences.json` — entries are
**records with a mandatory reason**, never bare paths:

```json
{
  "_comment": "Catalogued, justified divergences between our merge and getSiteSettingDerived. Every entry MUST carry a reason. An UNCATALOGUED diff fails the gate.",
  "entries": []
}
```

(Example entry shape, added during gate iteration:
`{"path": "modified_time", "reason": "server-side timestamp, not config"}`.)

Create `src/digital_twin/adapters/mist/compile/equivalence.py`:

```python
"""Tier-2 equivalence: our merge_only() site-level merge vs getSiteSettingDerived.

(merge_only, NOT compile_site: derived does not resolve {{vars}}, so the oracle
comparison uses the pre-vars artifact.) Comparison rules (from the spec):
canonical normalization (numeric strings, absent==null==empty), per-path diff
reporting, a catalogued-divergence list (data: divergences.json — every entry
justified), and an attribute-coverage report so a green gate states explicitly
which schema leaves real data exercised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digital_twin.adapters.mist.oas import norm_schema

_DIVERGENCES = Path(__file__).parent / "divergences.json"


@dataclass(frozen=True)
class Diff:
    path: str
    ours: Any
    derived: Any


@dataclass(frozen=True)
class CompareResult:
    diffs: tuple[Diff, ...]
    catalogued_diffs: tuple[Diff, ...]

    @property
    def passed(self) -> bool:
        return not self.diffs


@dataclass(frozen=True)
class Coverage:
    covered: frozenset[str]
    uncovered: frozenset[str]


def load_catalogued() -> tuple[str, ...]:
    """Paths of catalogued divergences; every entry must carry a reason."""
    entries = json.loads(_DIVERGENCES.read_text())["entries"]
    missing = [e for e in entries if not e.get("reason")]
    if missing:
        raise ValueError(f"divergence entries without a reason: {missing}")
    return tuple(e["path"] for e in entries)


def _norm(v: Any) -> Any:
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    if v in (None, {}, []):
        return None
    return v


def _walk_diffs(ours: Any, derived: Any, path: str, out: list[Diff]) -> None:
    if isinstance(ours, dict) or isinstance(derived, dict):
        o = ours if isinstance(ours, dict) else {}
        d = derived if isinstance(derived, dict) else {}
        for key in sorted(set(o) | set(d)):
            _walk_diffs(o.get(key), d.get(key), f"{path}.{key}" if path else key, out)
        return
    if isinstance(ours, list) and isinstance(derived, list):
        if [_norm(x) for x in ours] != [_norm(x) for x in derived]:
            out.append(Diff(path, ours, derived))
        return
    if _norm(ours) != _norm(derived):
        out.append(Diff(path, ours, derived))


def compare_effective(
    ours: dict[str, Any], derived: dict[str, Any], catalogued: tuple[str, ...] | None = None
) -> CompareResult:
    catalogued = load_catalogued() if catalogued is None else catalogued
    all_diffs: list[Diff] = []
    _walk_diffs(ours, derived, "", all_diffs)
    known = tuple(d for d in all_diffs if d.path in catalogued)
    real = tuple(d for d in all_diffs if d.path not in catalogued)
    return CompareResult(diffs=real, catalogued_diffs=known)


def _schema_leaves(schema: dict[str, Any], path: str = "", depth: int = 0) -> set[str]:
    if depth > 12:
        return {path} if path else set()
    schema = norm_schema(schema)  # SAME normalization as the Tier-1 generator —
    # leaves hidden behind allOf/anyOf/oneOf must count in coverage too
    props = schema.get("properties")
    if props:
        out: set[str] = set()
        for k, sub in props.items():
            out |= _schema_leaves(sub, f"{path}.{k}" if path else k, depth + 1)
        return out
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        return _schema_leaves(ap, f"{path}.*" if path else "*", depth + 1)
    return {path} if path else set()


def _data_leaves(node: Any, path: str = "") -> set[str]:
    if isinstance(node, dict):
        out: set[str] = set()
        for k, v in node.items():
            out |= _data_leaves(v, f"{path}.{k}" if path else k)
        return out
    return {path} if path else set()


def attribute_coverage(schema: dict[str, Any], samples: list[dict[str, Any]]) -> Coverage:
    """Which schema leaves did real data exercise? (wildcard-aware for keyed dicts)"""
    schema_leaves = _schema_leaves(schema)
    seen: set[str] = set()
    for sample in samples:
        seen |= _data_leaves(sample)

    def exercised(leaf: str) -> bool:
        if "*" not in leaf:
            return leaf in seen
        import re

        pattern = re.compile("^" + re.escape(leaf).replace("\\*", "[^.]+") + "$")
        return any(pattern.match(s) for s in seen)

    covered = frozenset(leaf for leaf in schema_leaves if exercised(leaf))
    return Coverage(covered=covered, uncovered=frozenset(schema_leaves) - covered)
```

- [ ] **Step 4: Run to verify pass** — PASS (6).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(compile): equivalence compare (normalize, catalogued divergences) + attribute coverage"
```

---

## Task 13: `tools/equivalence_gate.py` — the Tier-2 gate runner

**Files:**
- Create: `tools/equivalence_gate.py`

- [ ] **Step 1: Write the runner**

Create `tools/equivalence_gate.py`:

```python
"""Tier-2 equivalence gate: our merge_only() vs getSiteSettingDerived, per real site.

(merge_only, NOT compile_site: derived does not resolve {{vars}} — confirmed.)

Usage:  uv run python tools/equivalence_gate.py
Env:    MIST_HOST, MIST_APITOKEN, DT_GATE_ORG_ID, DT_GATE_SITE_IDS (comma-separated)

Per site: fetch raw + derived, merge ours, compare. Exit 0 only if EVERY site
has zero uncatalogued diffs. Always prints the attribute-coverage report — a
green gate explicitly states what real data did NOT exercise (Tier 1 covers it).

GATE RULE: do not build Plans 3-5 on top until this passes on the target orgs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from digital_twin.adapters.mist.compile.equivalence import attribute_coverage, compare_effective
from digital_twin.adapters.mist.compile.switch import merge_only
from digital_twin.providers.base import FetchError, SiteScope
from digital_twin.providers.mist_api import MistApiProvider

OAS = Path("src/digital_twin/adapters/mist/oas/site_setting.schema.json")


def main() -> None:
    org_id = os.environ["DT_GATE_ORG_ID"]
    site_ids = [s for s in os.environ["DT_GATE_SITE_IDS"].split(",") if s]
    provider = MistApiProvider()
    failures = 0
    derived_samples: list[dict] = []

    for site_id in site_ids:
        raw = provider.fetch_site(SiteScope(org_id, site_id), include_derived=True)
        if isinstance(raw, FetchError):
            print(f"[FAIL] {site_id}: baseline fetch failed: "
                  f"{[f.error for f in raw.failures]}")
            failures += 1
            continue
        if raw.derived_setting is None:
            print(f"[FAIL] {site_id}: derived_setting fetch failed: "
                  f"{[f.error for f in raw.meta.failures]}")
            failures += 1
            continue
        # derived does NOT resolve {{vars}} (confirmed) -> compare the pre-vars merge
        ours = merge_only(dict(raw.networktemplate) if raw.networktemplate else None,
                          dict(raw.setting))
        derived = dict(raw.derived_setting)
        derived_samples.append(derived)
        result = compare_effective(ours, derived)
        if result.passed:
            note = f" ({len(result.catalogued_diffs)} catalogued)" if result.catalogued_diffs else ""
            print(f"[ OK ] {site_id}{note}")
        else:
            failures += 1
            print(f"[FAIL] {site_id}: {len(result.diffs)} uncatalogued diff(s):")
            for d in result.diffs[:25]:
                print(f"         {d.path}: ours={d.ours!r} derived={d.derived!r}")

    if OAS.exists() and derived_samples:
        cov = attribute_coverage(json.loads(OAS.read_text()), derived_samples)
        total = len(cov.covered) + len(cov.uncovered)
        print(f"\nattribute coverage: {len(cov.covered)}/{total} schema leaves exercised")
        for leaf in sorted(cov.uncovered)[:40]:
            print(f"  uncovered: {leaf}   (validated by Tier-1 OAS tests only)")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the gate against the read-only orgs**

```bash
uv run python tools/equivalence_gate.py
```

Expected: `[ OK ]` for every configured site, plus the coverage report. For each `[FAIL]`:
1. If our merge rule is wrong → fix `_POLICY` in `merge.py` (or `compile_site`), re-run Tier-1 + gate.
2. If Mist's derived adds server-side noise (timestamps, computed defaults) → add the path to `divergences.json` **with a reason** in `_comment` form, re-run.
3. If derived systematically materializes **schema defaults** for fields we leave absent (the spec's "defaults" rule — the `site_setting` closure carries 434 `default` annotations, so expect this), don't catalogue them one-by-one: add a small `apply_schema_defaults(config, schema)` step in `equivalence.py` that fills `default`-annotated leaves from the OAS schema on *both* sides before comparing, with a test.

Iterate until green on all configured sites across at least two orgs. **This is the Plan-2 exit gate.**

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat(gate): Tier-2 equivalence gate runner with coverage report"
```

---

## Task 14: Public API + full quality gate

**Files:**
- Modify: `src/digital_twin/providers/__init__.py`, `src/digital_twin/adapters/mist/__init__.py`, `src/digital_twin/engine/__init__.py`
- Test: extend `tests/test_public_api.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_public_api.py`:

```python
def test_plan2_public_api():
    from digital_twin.adapters.mist import (
        ClientsIngester,
        IngesterRegistry,
        IngestReport,
        LldpIngester,
        SwitchIngester,
        compile_device,
        compile_site,
        merge_only,
    )

    assert IngestReport is not None
    from digital_twin.engine import validate_supply
    from digital_twin.providers import (
        FetchError,
        MistApiProvider,
        RawSiteState,
        SiteScope,
        StateProvider,
    )

    assert all(callable(f) for f in (compile_site, compile_device, merge_only, validate_supply))
    assert FetchError is not None
    assert all(x is not None for x in (SwitchIngester, LldpIngester, ClientsIngester,
                                       IngesterRegistry, MistApiProvider, RawSiteState,
                                       SiteScope, StateProvider))
```

- [ ] **Step 2: Implement the re-exports**

`src/digital_twin/providers/__init__.py`:

```python
"""State providers (effectful): fetch raw vendor state for a scope."""

from .base import FetchError, FetchFailure, RawSiteState, SiteScope, StateMeta, StateProvider
from .mist_api import MistApiProvider

__all__ = ["FetchError", "FetchFailure", "RawSiteState", "SiteScope", "StateMeta",
           "StateProvider", "MistApiProvider"]
```

`src/digital_twin/adapters/mist/__init__.py`:

```python
"""Mist vendor adapter: compile (raw -> effective) + ingest (effective -> IR)."""

from .compile.switch import compile_device, compile_site, merge_only
from .ingest.clients import ClientsIngester
from .ingest.lldp import LldpIngester
from .ingest.registry import IngesterRegistry, IngestFailure, IngestReport
from .ingest.switch import SwitchIngester

__all__ = ["compile_site", "compile_device", "merge_only",
           "IngesterRegistry", "IngestReport", "IngestFailure",
           "SwitchIngester", "LldpIngester", "ClientsIngester"]
```

`src/digital_twin/engine/__init__.py`:

```python
"""Engine: orchestration-only modules (no business logic)."""

from .capability_check import CapabilityGapError, validate_supply

__all__ = ["CapabilityGapError", "validate_supply"]
```

- [ ] **Step 3: Full quality gate**

```bash
uv run ruff format . && uv run ruff check --fix . && uv run ruff check .
uv run mypy
uv run pytest -q          # offline suite
uv run pytest -m live -q  # live smoke (env required)
```
Expected: all green. Fix residuals by hand.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: plan 2 public API re-exports; green gates"
```

---

## Done criteria for Plan 2

- Offline suite green (ruff format/check, mypy strict, pytest) — Phase A is fully unit-tested with synthetic Mist-shaped data; **Tier-1 OAS tests cover every schema attribute** of the merge.
- `tools/probe_fetch.py` ran against a real org; SDK call names and stat field shapes pinned (corrections applied to `mist_api.py` / `lldp.py` / `clients.py`).
- **Tier-2 equivalence gate green** on all configured sites across ≥2 read-only orgs: zero uncatalogued diffs; every catalogued divergence justified in `divergences.json`; attribute-coverage report printed and reviewed (uncovered leaves are consciously delegated to Tier 1).
- A real site round-trips end-to-end offline→IR: `fetch_site` → `compile_site`/`compile_device` → `IngesterRegistry.run` → valid `IR` claiming `wired.l2`, `l3.exits`, `clients.active` (spot-checked via a throwaway script or REPL). `stp.state` is **earned only if the site's port stats expose STP** — prefer at least one gate site that does; on a site without STP stats its *absence* is the correct, honest outcome (later: loop check → `INSUFFICIENT_DATA`).
- No real org data committed (`.cache/` git-ignored).

**Next:** Plan 3 — scope gates (object/field/derived-impact) + L0 OAS validation + apply (ordered rolling state).
