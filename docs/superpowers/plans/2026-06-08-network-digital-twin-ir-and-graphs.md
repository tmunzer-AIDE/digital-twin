# Network Digital Twin — Plan 1: IR Core + Derived Graphs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the vendor-neutral Intermediate Representation (IR) — typed immutable entities with per-fact provenance/confidence, capability/versioning metadata, a validating builder, the neutral `IRDiff` change set, and the derived L2 / per-VLAN graphs — that every later layer (ingest, gates, checks, verdict) consumes.

**Architecture:** Pure-Python library, no I/O, no vendor knowledge. Frozen dataclass entities each carry a `FactMeta` (provenance + confidence), where provenance maps to confidence through one canonical table. An `IRBuilder` validates referential integrity and id-uniqueness, then produces an immutable `IR` (with `ir_version` + `capabilities`). `diff_ir` produces a neutral `IRDiff`. Graph builders project the IR into `networkx` multigraphs: a **device-level L2 graph whose edges are derived from specific ports** (so a port-level config change is detected), collapsing LAG/MCLAG by `bundle_id` (not just device pair), keeping independent physical links as parallel edges (redundancy = cycle), and folding VC fabric into one node. Per-VLAN graphs include only participating nodes and annotate each with `access_ports`/`exits`/membership.

**Tech Stack:** Python 3.14, `uv` (env/deps), `networkx` (graphs), `netaddr` (later plans), `pytest` + `ruff` + `mypy`. src layout (`src/digital_twin/`).

This is **Plan 1 of 5** for Milestone 1. Later plans: (2) StateProvider + ingest + compiler + equivalence gate; (3) ScopeResolver + L0 validation + apply; (4) check engine + verdict/decision + the 4 checks; (5) drivers + observability/replay + golden scenarios.

**Key design decisions (from spec review):**
- **Device-level graph with port-aware edges.** Nodes are devices; every edge stores its participating port ids, `bundle_id`, carried VLANs, per-edge confidence. A port config change (e.g. dropping VLAN 30 from the trunk feeding an AP) changes that edge's carried-VLAN set, so it *is* detected (GS7). Device-level loses only intra-device loops (rare, STP-handled, not in any golden scenario).
- **Per-fact provenance/confidence on every entity** via a shared `FactMeta`; the canonical provenance→confidence table lives in one place.
- **Correct access/trunk VLAN semantics:** an access port joins a trunk only via the trunk's *native* VLAN (untagged), never a tagged VLAN.

---

## File Structure

```
src/digital_twin/
├── __init__.py
└── ir/
    ├── __init__.py          # re-exports the public IR API
    ├── confidence.py        # ConfidenceLevel, Confidence, min_confidence()
    ├── provenance.py        # Provenance, FactMeta, fact_meta(), CONFIG_META, OBSERVED_META
    ├── capabilities.py      # IRCapability enum (domain-presence flags)
    ├── entities.py          # enums, id helpers, frozen entity dataclasses (with meta)
    ├── model.py             # IR, IRBuilder (validating), IRValidationError, IR_VERSION
    ├── diff.py              # EntityRef, Modified, IRDiff, diff_ir()
    └── graphs.py            # link_carried_vlans(), build_l2_graph(), build_vlan_graph()
tests/
├── test_smoke.py
└── ir/
    ├── __init__.py
    ├── test_confidence.py
    ├── test_provenance.py
    ├── test_capabilities.py
    ├── test_entities.py
    ├── test_model.py
    ├── test_diff.py
    ├── test_graphs.py
    └── test_public_api.py
```

Dependency chain: `graphs → {model, diff, entities}`, `diff → {model, entities}`, `model → entities`, `entities → {provenance, capabilities}`, `provenance → confidence`.

---

## Task 0: Project scaffold

**Files:**
- Create: `pyproject.toml`, `src/digital_twin/__init__.py`, `src/digital_twin/ir/__init__.py`, `tests/__init__.py`, `tests/ir/__init__.py`, `tests/test_smoke.py`

- [ ] **Step 1: Initialize the uv package**

Run:
```bash
uv init --package --name digital-twin --python 3.14
```
Expected: creates `pyproject.toml` and `src/digital_twin/__init__.py`. (If uv reports 3.14 missing, run `uv python install 3.14` first.)

- [ ] **Step 2: Add dependencies**

Run:
```bash
uv add networkx netaddr
uv add --dev pytest ruff mypy
```
Expected: deps recorded in `pyproject.toml`; `uv.lock` written.

- [ ] **Step 3: Add tool configuration to `pyproject.toml`**

Merge the blocks below into the generated `pyproject.toml`. **Keep the `[build-system]` and dependency sections uv generated;** only set `requires-python` and add the `[tool.*]` blocks:

```toml
# in [project]:
requires-python = ">=3.14"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.14"
files = ["src/digital_twin"]
strict = true

[[tool.mypy.overrides]]
module = ["networkx.*", "netaddr.*"]
ignore_missing_imports = true
```

- [ ] **Step 4: Create package markers and a smoke test**

Run:
```bash
mkdir -p tests/ir && touch tests/__init__.py tests/ir/__init__.py src/digital_twin/ir/__init__.py
```

Create `tests/test_smoke.py`:

```python
def test_package_imports():
    import digital_twin

    assert digital_twin is not None
```

- [ ] **Step 5: Verify the toolchain runs green (exit 0)**

Run:
```bash
uv run pytest -q
```
Expected: PASS (1 passed) — exit code 0. (The smoke test guarantees a non-empty suite so automated runners don't trip on pytest's "no tests" exit code 5.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: scaffold digital-twin package (uv, py3.14, pytest/ruff/mypy)"
```

---

## Task 1: Confidence value object

**Files:**
- Create: `src/digital_twin/ir/confidence.py`
- Test: `tests/ir/test_confidence.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_confidence.py`:

```python
import pytest

from digital_twin.ir.confidence import Confidence, ConfidenceLevel, min_confidence


def test_levels_are_ordered():
    assert ConfidenceLevel.LOW < ConfidenceLevel.MEDIUM < ConfidenceLevel.HIGH


def test_single_confidence_returned_as_is():
    c = Confidence(ConfidenceLevel.HIGH, ("two-sided LLDP",))
    assert min_confidence(c) == c


def test_min_picks_lowest_and_keeps_lowest_reasons():
    high = Confidence(ConfidenceLevel.HIGH, ("configured",))
    low_a = Confidence(ConfidenceLevel.LOW, ("one-sided LLDP",))
    low_b = Confidence(ConfidenceLevel.LOW, ("uncorroborated",))
    result = min_confidence(high, low_a, low_b)
    assert result.level is ConfidenceLevel.LOW
    assert result.reasons == ("one-sided LLDP", "uncorroborated")
    assert "configured" not in result.reasons


def test_min_requires_at_least_one_argument():
    with pytest.raises(ValueError):
        min_confidence()


def test_confidence_is_frozen():
    c = Confidence(ConfidenceLevel.MEDIUM)
    with pytest.raises(Exception):
        c.level = ConfidenceLevel.HIGH  # type: ignore[misc]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_confidence.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/confidence.py`:

```python
"""Confidence: categorical (HIGH/MEDIUM/LOW) + reasons, with MIN composition.

A derived fact's confidence is the lowest level among the facts it relied on (and
the inference method). Reasons explaining the floor accumulate from the lowest-level
inputs. Never a float — false precision undermines explainability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ConfidenceLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class Confidence:
    level: ConfidenceLevel
    reasons: tuple[str, ...] = ()


def min_confidence(*confidences: Confidence) -> Confidence:
    """Compose confidences: take the lowest level; keep reasons from inputs at it."""
    if not confidences:
        raise ValueError("min_confidence requires at least one Confidence")
    lowest = min(c.level for c in confidences)
    reasons: tuple[str, ...] = ()
    for c in confidences:
        if c.level == lowest:
            reasons += c.reasons
    return Confidence(level=lowest, reasons=reasons)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_confidence.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/confidence.py tests/ir/test_confidence.py
git commit -m "feat(ir): confidence value object with MIN composition"
```

---

## Task 2: Provenance + FactMeta (the canonical fact→confidence table)

Every fact carries provenance and confidence. Provenance maps to a confidence level through **one** table — the spec's canonical rule, in a single place.

**Files:**
- Create: `src/digital_twin/ir/provenance.py`
- Test: `tests/ir/test_provenance.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_provenance.py`:

```python
from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.provenance import (
    CONFIG_META,
    OBSERVED_META,
    FactMeta,
    Provenance,
    fact_meta,
)


def test_authoritative_provenances_are_high():
    for prov in (Provenance.CONFIG, Provenance.DESIGNATED,
                 Provenance.LLDP_TWO_SIDED, Provenance.OBSERVED):
        assert fact_meta(prov).confidence.level is ConfidenceLevel.HIGH


def test_inferred_is_medium_and_one_sided_lldp_is_low():
    assert fact_meta(Provenance.INFERRED).confidence.level is ConfidenceLevel.MEDIUM
    assert fact_meta(Provenance.LLDP_ONE_SIDED).confidence.level is ConfidenceLevel.LOW


def test_fact_meta_carries_reasons():
    m = fact_meta(Provenance.LLDP_ONE_SIDED, ("seen from S only",))
    assert m.confidence.reasons == ("seen from S only",)


def test_default_metas():
    assert CONFIG_META.provenance is Provenance.CONFIG
    assert CONFIG_META.confidence.level is ConfidenceLevel.HIGH
    assert OBSERVED_META.provenance is Provenance.OBSERVED


def test_fact_meta_is_frozen():
    m = FactMeta(Provenance.CONFIG, fact_meta(Provenance.CONFIG).confidence)
    assert isinstance(m, FactMeta)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_provenance.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/provenance.py`:

```python
"""Provenance + FactMeta: where a fact came from, and its resulting confidence.

The provenance→confidence mapping is the spec's CANONICAL table — the single
source of truth. The axis is authority/corroboration: a device's report about
ITSELF (own STP, own clients) is authoritative HIGH; a single-source claim about
a relationship (one-sided LLDP) is LOW.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .confidence import Confidence, ConfidenceLevel


class Provenance(str, Enum):
    CONFIG = "config"                  # explicitly configured
    DESIGNATED = "designated"          # operator/template-marked (e.g. uplink)
    LLDP_TWO_SIDED = "lldp_two_sided"  # link confirmed from both ends
    OBSERVED = "observed"              # authoritative device self-report
    INFERRED = "inferred"              # config-inferred, unconfirmed
    LLDP_ONE_SIDED = "lldp_one_sided"  # single-source claim about a relationship


_LEVEL: dict[Provenance, ConfidenceLevel] = {
    Provenance.CONFIG: ConfidenceLevel.HIGH,
    Provenance.DESIGNATED: ConfidenceLevel.HIGH,
    Provenance.LLDP_TWO_SIDED: ConfidenceLevel.HIGH,
    Provenance.OBSERVED: ConfidenceLevel.HIGH,
    Provenance.INFERRED: ConfidenceLevel.MEDIUM,
    Provenance.LLDP_ONE_SIDED: ConfidenceLevel.LOW,
}


@dataclass(frozen=True)
class FactMeta:
    provenance: Provenance
    confidence: Confidence


def fact_meta(provenance: Provenance, reasons: tuple[str, ...] = ()) -> FactMeta:
    """Build a FactMeta whose confidence follows the canonical table."""
    return FactMeta(provenance, Confidence(_LEVEL[provenance], reasons))


CONFIG_META = fact_meta(Provenance.CONFIG)
OBSERVED_META = fact_meta(Provenance.OBSERVED)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_provenance.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/provenance.py tests/ir/test_provenance.py
git commit -m "feat(ir): provenance + FactMeta with canonical provenance->confidence table"
```

---

## Task 3: IRCapability vocabulary

Coarse **domain-presence** flags only. Fine-grained quality (e.g. one weak link among many) is per-fact confidence/coverage, *not* a global capability — so `LINKS_BIDIRECTIONAL` is deliberately absent.

**Files:**
- Create: `src/digital_twin/ir/capabilities.py`
- Test: `tests/ir/test_capabilities.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_capabilities.py`:

```python
from digital_twin.ir.capabilities import IRCapability


def test_capability_values_are_stable_strings():
    assert IRCapability.WIRED_L2.value == "wired.l2"
    assert IRCapability.CLIENTS_ACTIVE.value == "clients.active"
    assert IRCapability.STP_STATE.value == "stp.state"
    assert IRCapability.L3_EXITS.value == "l3.exits"


def test_bidirectional_is_not_a_capability():
    # link quality is per-fact confidence, never a global capability
    assert not hasattr(IRCapability, "LINKS_BIDIRECTIONAL")


def test_capabilities_are_set_members():
    caps = {IRCapability.WIRED_L2, IRCapability.STP_STATE}
    assert IRCapability.WIRED_L2 in caps
    assert IRCapability.CLIENTS_ACTIVE not in caps
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_capabilities.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/capabilities.py`:

```python
"""IRCapability: coarse domain-presence flags an IR instance declares.

Checks declare requires() against these; the engine self-gates a check to
INSUFFICIENT_DATA when a required capability is absent. These are PRESENCE flags
(was this domain populated at all), NOT quality — quality lives in per-fact
confidence and per-check coverage. New domains ADD capabilities; existing checks
are unaffected.
"""

from __future__ import annotations

from enum import Enum


class IRCapability(str, Enum):
    WIRED_L2 = "wired.l2"          # devices/ports/links/vlans populated
    STP_STATE = "stp.state"        # per-port STP state populated
    CLIENTS_ACTIVE = "clients.active"  # live client list populated
    L3_EXITS = "l3.exits"          # L3 interfaces (VLAN exits) populated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_capabilities.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/capabilities.py tests/ir/test_capabilities.py
git commit -m "feat(ir): IRCapability domain-presence vocabulary"
```

---

## Task 4: Entities + stable id helpers

Typed frozen facts. Each carries a `meta: FactMeta`. `Link` has a `bundle_id` (for correct LAG collapse) and no separate `source`/`bidirectional` (encoded by provenance). `L3Intf` auto-derives a stable `id`.

**Files:**
- Create: `src/digital_twin/ir/entities.py`
- Test: `tests/ir/test_entities.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_entities.py`:

```python
import pytest

from digital_twin.ir.entities import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    Port,
    PortMode,
    StpMode,
    StpProvenance,
    Vlan,
    client_id,
    device_id,
    link_id,
    port_id,
)
from digital_twin.ir.provenance import CONFIG_META, OBSERVED_META, Provenance, fact_meta


def test_id_helpers():
    assert device_id("AA:BB:CC:00:11:22") == "aabbcc001122"
    assert port_id("aabbcc001122", "ge-0/0/1") == "aabbcc001122:ge-0/0/1"
    assert link_id("d2:p", "d1:p") == link_id("d1:p", "d2:p")  # canonical
    assert client_id("DE:AD:BE:EF:00:01") == "deadbeef0001"


def test_entities_default_to_config_meta():
    dev = Device(id="d1", role=DeviceRole.SWITCH, site="s1")
    assert dev.meta is CONFIG_META


def test_link_carries_bundle_id_and_meta_not_separate_source():
    link = Link(
        id="l1", a_port="d1:p", b_port="d2:p", kind=LinkKind.LAG, bundle_id="ae0",
        meta=fact_meta(Provenance.LLDP_TWO_SIDED),
    )
    assert link.bundle_id == "ae0"
    assert link.meta.provenance is Provenance.LLDP_TWO_SIDED
    assert not hasattr(link, "source")


def test_port_keeps_stp_fields():
    port = Port(id="d1:ge-0/0/1", device_id="d1", name="ge-0/0/1", mode=PortMode.TRUNK,
                tagged_vlans=(10, 30), stp_enabled=True, stp_mode=StpMode.RSTP,
                stp_provenance=StpProvenance.OBSERVED)
    assert port.tagged_vlans == (10, 30)
    assert port.stp_provenance is StpProvenance.OBSERVED


def test_l3intf_auto_derives_stable_id():
    intf = L3Intf(device_id="d1", role=L3Role.IRB, vlan_id=30, subnet="10.0.30.0/24")
    assert intf.id == "d1:l3:irb:30"


def test_client_defaults_to_observed_meta():
    c = Client(mac="deadbeef0001", kind=ClientKind.WIRELESS,
               attach_kind=AttachKind.AP, attach_id="ap1", vlan=30)
    assert c.meta is OBSERVED_META
    assert c.active is True


def test_vlan_has_scope_in_identity():
    v = Vlan(vlan_id=30, name="voice", scope="s1")
    assert v.scope == "s1"


def test_entities_are_frozen():
    dev = Device(id="d1", role=DeviceRole.SWITCH, site="s1")
    with pytest.raises(Exception):
        dev.site = "s2"  # type: ignore[misc]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_entities.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/entities.py`:

```python
"""Vendor-neutral IR entities (frozen) with per-fact provenance/confidence.

Ids derive from stable keys (MAC, device+port name) — never a vendor object_id —
so baseline/proposed IRs line up for diffing and future cross-vendor reconciliation.
Every entity carries a FactMeta (provenance + confidence).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .provenance import CONFIG_META, OBSERVED_META, FactMeta


class DeviceRole(str, Enum):
    SWITCH = "switch"
    GATEWAY = "gateway"
    AP = "ap"
    MISTEDGE = "mistedge"


class PortMode(str, Enum):
    ACCESS = "access"
    TRUNK = "trunk"


class LinkKind(str, Enum):
    PHYSICAL = "physical"
    LAG = "lag"
    MCLAG = "mclag"
    VC = "vc"


class StpMode(str, Enum):
    RSTP = "rstp"
    MSTP = "mstp"
    VSTP = "vstp"
    NONE = "none"


class StpProvenance(str, Enum):
    OBSERVED = "observed"
    CONFIG = "config"
    UNKNOWN = "unknown"


class L3Role(str, Enum):
    IRB = "irb"
    SVI = "svi"
    WAN = "wan"
    LOOPBACK = "loopback"


class ClientKind(str, Enum):
    WIRED = "wired"
    WIRELESS = "wireless"


class AttachKind(str, Enum):
    PORT = "port"
    AP = "ap"


def _norm_mac(mac: str) -> str:
    return mac.lower().replace(":", "").replace("-", "")


def device_id(mac: str) -> str:
    return _norm_mac(mac)


def port_id(dev_id: str, name: str) -> str:
    return f"{dev_id}:{name}"


def link_id(port_a_id: str, port_b_id: str) -> str:
    a, b = sorted((port_a_id, port_b_id))
    return f"{a}__{b}"


def client_id(mac: str) -> str:
    return _norm_mac(mac)


@dataclass(frozen=True)
class Device:
    id: str
    role: DeviceRole
    site: str
    model: str | None = None
    vc_members: tuple[str, ...] = ()
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class Port:
    id: str
    device_id: str
    name: str
    mode: PortMode
    native_vlan: int | None = None
    tagged_vlans: tuple[int, ...] = ()
    speed: int | None = None
    poe: bool | None = None
    profile: str | None = None
    stp_enabled: bool | None = None
    stp_mode: StpMode = StpMode.NONE
    stp_state: str | None = None
    stp_provenance: StpProvenance = StpProvenance.UNKNOWN
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class Link:
    id: str
    a_port: str
    b_port: str
    kind: LinkKind
    bundle_id: str | None = None  # LAG/MCLAG bundle identity; None for standalone links
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class Vlan:
    vlan_id: int
    name: str | None = None
    scope: str = "site"  # M1: a single site name; part of identity for future multi-site
    meta: FactMeta = CONFIG_META


@dataclass(frozen=True)
class L3Intf:
    device_id: str
    role: L3Role
    vlan_id: int | None = None
    port: str | None = None
    subnet: str | None = None
    ip: str | None = None
    meta: FactMeta = CONFIG_META
    id: str = ""  # auto-derived in __post_init__ if empty

    def __post_init__(self) -> None:
        if not self.id:
            key = str(self.vlan_id) if self.vlan_id is not None else (self.port or "?")
            object.__setattr__(self, "id", f"{self.device_id}:l3:{self.role.value}:{key}")


@dataclass(frozen=True)
class Client:
    mac: str
    kind: ClientKind
    attach_kind: AttachKind
    attach_id: str  # a port id (wired) or an ap device id (wireless)
    vlan: int | None = None
    ip: str | None = None
    active: bool = True
    meta: FactMeta = OBSERVED_META

    @property
    def id(self) -> str:
        return client_id(self.mac)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_entities.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/entities.py tests/ir/test_entities.py
git commit -m "feat(ir): frozen entities with FactMeta, Link.bundle_id, stable ids"
```

---

## Task 5: IR container + validating IRBuilder

The immutable container plus a builder that **rejects duplicate ids and dangling references** at `build()` — the IR is the foundation for safety checks, so it must be internally consistent.

**Files:**
- Create: `src/digital_twin/ir/model.py`
- Test: `tests/ir/test_model.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_model.py`:

```python
import pytest

from digital_twin.ir.capabilities import IRCapability
from digital_twin.ir.entities import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    Link,
    LinkKind,
    Port,
    PortMode,
    Vlan,
)
from digital_twin.ir.model import IR_VERSION, IRBuilder, IRValidationError


def _sw(did: str) -> Device:
    return Device(id=did, role=DeviceRole.SWITCH, site="s1")


def _port(did: str, name: str) -> Port:
    return Port(id=f"{did}:{name}", device_id=did, name=name, mode=PortMode.TRUNK)


def test_empty_ir_has_version_and_no_capabilities():
    ir = IRBuilder().build()
    assert ir.ir_version == IR_VERSION
    assert ir.capabilities == frozenset()
    assert ir.links == ()


def test_builder_collects_and_lookups_work():
    p = _port("d1", "ge-0/0/1")
    ir = (IRBuilder().add_device(_sw("d1")).add_port(p).add_vlan(Vlan(vlan_id=30))
          .with_capability(IRCapability.WIRED_L2).build())
    assert ir.device("d1").role is DeviceRole.SWITCH
    assert ir.port("d1:ge-0/0/1") is p
    assert ir.vlans[30].vlan_id == 30
    assert ir.has(IRCapability.WIRED_L2) is True


def test_mappings_are_read_only():
    ir = IRBuilder().add_device(_sw("d1")).build()
    with pytest.raises(TypeError):
        ir.devices["d2"] = _sw("d2")  # type: ignore[index]


def test_duplicate_device_id_rejected():
    b = IRBuilder().add_device(_sw("d1"))
    with pytest.raises(IRValidationError):
        b.add_device(_sw("d1"))


def test_port_with_unknown_device_rejected_at_build():
    b = IRBuilder().add_port(_port("ghost", "ge-0/0/1"))
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "unknown device" in str(e.value)


def test_link_with_dangling_endpoint_rejected_at_build():
    b = (IRBuilder().add_device(_sw("d1")).add_port(_port("d1", "ge-0/0/1"))
         .add_link(Link(id="l1", a_port="d1:ge-0/0/1", b_port="d2:missing",
                        kind=LinkKind.PHYSICAL)))
    with pytest.raises(IRValidationError) as e:
        b.build()
    assert "d2:missing" in str(e.value)


def test_wired_client_with_unknown_port_rejected_at_build():
    b = (IRBuilder().add_device(_sw("d1"))
         .add_client(Client(mac="aa", kind=ClientKind.WIRED,
                            attach_kind=AttachKind.PORT, attach_id="d1:ghost")))
    with pytest.raises(IRValidationError):
        b.build()


def test_valid_ir_with_full_references_builds():
    ir = (IRBuilder()
          .add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(_port("d1", "ge-0/0/1")).add_port(_port("d2", "ge-0/0/5"))
          .add_link(Link(id="l1", a_port="d1:ge-0/0/1", b_port="d2:ge-0/0/5",
                         kind=LinkKind.PHYSICAL))
          .build())
    assert len(ir.links) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_model.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/model.py`:

```python
"""IR: the immutable, validated, vendor-neutral container, plus an IRBuilder.

build() rejects duplicate ids and dangling references — the IR is the foundation
for safety checks, so it must be internally consistent. Mappings are read-only
proxies; never mutate after build().
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .capabilities import IRCapability
from .entities import AttachKind, Client, Device, L3Intf, Link, Port, Vlan

IR_VERSION = "1.0"


class IRValidationError(ValueError):
    """Raised when an IR would be internally inconsistent (dup ids / dangling refs)."""


@dataclass(frozen=True)
class IR:
    ir_version: str
    capabilities: frozenset[IRCapability]
    devices: Mapping[str, Device]
    ports: Mapping[str, Port]
    links: tuple[Link, ...]
    vlans: Mapping[int, Vlan]
    l3intfs: tuple[L3Intf, ...]
    clients: tuple[Client, ...]

    def device(self, did: str) -> Device:
        return self.devices[did]

    def port(self, pid: str) -> Port:
        return self.ports[pid]

    def has(self, cap: IRCapability) -> bool:
        return cap in self.capabilities


class IRBuilder:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._ports: dict[str, Port] = {}
        self._links: list[Link] = []
        self._vlans: dict[int, Vlan] = {}
        self._l3intfs: list[L3Intf] = []
        self._clients: list[Client] = []
        self._capabilities: set[IRCapability] = set()

    def add_device(self, device: Device) -> IRBuilder:
        if device.id in self._devices:
            raise IRValidationError(f"duplicate device id {device.id}")
        self._devices[device.id] = device
        return self

    def add_port(self, port: Port) -> IRBuilder:
        if port.id in self._ports:
            raise IRValidationError(f"duplicate port id {port.id}")
        self._ports[port.id] = port
        return self

    def add_link(self, link: Link) -> IRBuilder:
        self._links.append(link)
        return self

    def add_vlan(self, vlan: Vlan) -> IRBuilder:
        if vlan.vlan_id in self._vlans:
            raise IRValidationError(f"duplicate vlan id {vlan.vlan_id}")
        self._vlans[vlan.vlan_id] = vlan
        return self

    def add_l3intf(self, intf: L3Intf) -> IRBuilder:
        self._l3intfs.append(intf)
        return self

    def add_client(self, client: Client) -> IRBuilder:
        self._clients.append(client)
        return self

    def with_capability(self, cap: IRCapability) -> IRBuilder:
        self._capabilities.add(cap)
        return self

    def _validate(self) -> None:
        errors: list[str] = []
        for p in self._ports.values():
            if p.device_id not in self._devices:
                errors.append(f"port {p.id} references unknown device {p.device_id}")
        for link in self._links:
            for endpoint in (link.a_port, link.b_port):
                if endpoint not in self._ports:
                    errors.append(f"link {link.id} references unknown port {endpoint}")
        for intf in self._l3intfs:
            if intf.device_id not in self._devices:
                errors.append(f"l3intf {intf.id} references unknown device {intf.device_id}")
        for c in self._clients:
            if c.attach_kind is AttachKind.PORT and c.attach_id not in self._ports:
                errors.append(f"client {c.mac} references unknown port {c.attach_id}")
            if c.attach_kind is AttachKind.AP and c.attach_id not in self._devices:
                errors.append(f"client {c.mac} references unknown ap {c.attach_id}")
        for d in self._devices.values():
            for member in d.vc_members:
                if member not in self._devices:
                    errors.append(f"device {d.id} lists unknown vc member {member}")
        if errors:
            raise IRValidationError("invalid IR:\n  " + "\n  ".join(errors))

    def build(self) -> IR:
        self._validate()
        return IR(
            ir_version=IR_VERSION,
            capabilities=frozenset(self._capabilities),
            devices=MappingProxyType(dict(self._devices)),
            ports=MappingProxyType(dict(self._ports)),
            links=tuple(self._links),
            vlans=MappingProxyType(dict(self._vlans)),
            l3intfs=tuple(self._l3intfs),
            clients=tuple(self._clients),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_model.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/model.py tests/ir/test_model.py
git commit -m "feat(ir): validating IRBuilder + immutable IR container"
```

---

## Task 6: IRDiff — the neutral change set

The vendor-neutral change set that checks (`applies_to`) and the derived-impact gate consume. Defines the shape now; the differ compares entities by stable id.

**Files:**
- Create: `src/digital_twin/ir/diff.py`
- Test: `tests/ir/test_diff.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_diff.py`:

```python
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.entities import Device, DeviceRole, Port, PortMode
from digital_twin.ir.model import IRBuilder


def _sw(did: str) -> Device:
    return Device(id=did, role=DeviceRole.SWITCH, site="s1")


def _trunk(did: str, name: str, tagged: tuple[int, ...]) -> Port:
    return Port(id=f"{did}:{name}", device_id=did, name=name, mode=PortMode.TRUNK,
                tagged_vlans=tagged)


def test_no_change_is_empty_diff():
    ir = IRBuilder().add_device(_sw("d1")).add_port(_trunk("d1", "p", (30,))).build()
    d = diff_ir(ir, ir)
    assert d.is_empty()


def test_added_and_removed_entities_are_detected():
    base = IRBuilder().add_device(_sw("d1")).build()
    proposed = IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).build()
    d = diff_ir(base, proposed)
    assert ("device", "d2") in {(r.kind, r.id) for r in d.added}
    rev = diff_ir(proposed, base)
    assert ("device", "d2") in {(r.kind, r.id) for r in rev.removed}


def test_modified_port_reports_changed_fields():
    base = (IRBuilder().add_device(_sw("d1")).add_port(_trunk("d1", "p", (10, 30))).build())
    proposed = (IRBuilder().add_device(_sw("d1")).add_port(_trunk("d1", "p", (10,))).build())
    d = diff_ir(base, proposed)
    mods = {(m.ref.kind, m.ref.id): m.changed_fields for m in d.modified}
    assert ("port", "d1:p") in mods
    assert "tagged_vlans" in mods[("port", "d1:p")]


def test_meta_only_change_is_not_a_modification():
    # confidence/provenance changes are not config changes
    from digital_twin.ir.provenance import Provenance, fact_meta
    base = (IRBuilder().add_device(_sw("d1"))
            .add_port(_trunk("d1", "p", (30,))).build())
    p2 = Port(id="d1:p", device_id="d1", name="p", mode=PortMode.TRUNK,
              tagged_vlans=(30,), meta=fact_meta(Provenance.LLDP_ONE_SIDED))
    proposed = IRBuilder().add_device(_sw("d1")).add_port(p2).build()
    d = diff_ir(base, proposed)
    assert d.is_empty()


def test_touches_reports_kinds():
    base = IRBuilder().add_device(_sw("d1")).build()
    proposed = IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).build()
    d = diff_ir(base, proposed)
    assert d.touches("device") is True
    assert d.touches("port") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_diff.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/diff.py`:

```python
"""IRDiff: the vendor-neutral change set between two IR snapshots.

Checks read this (never the raw vendor payload); the derived-impact gate reads it
too. Entities are compared by stable id; the per-fact `meta` (provenance/confidence)
is excluded from change detection — a confidence change is not a config change.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any

from .model import IR

_IGNORED_FIELDS = {"meta"}


@dataclass(frozen=True)
class EntityRef:
    kind: str  # "device" | "port" | "link" | "vlan" | "l3intf" | "client"
    id: str


@dataclass(frozen=True)
class Modified:
    ref: EntityRef
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class IRDiff:
    added: tuple[EntityRef, ...]
    removed: tuple[EntityRef, ...]
    modified: tuple[Modified, ...]

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def touches(self, kind: str) -> bool:
        refs: Iterable[EntityRef] = (
            *self.added,
            *self.removed,
            *(m.ref for m in self.modified),
        )
        return any(r.kind == kind for r in refs)


def _index(ir: IR) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    for d in ir.devices.values():
        out[("device", d.id)] = d
    for p in ir.ports.values():
        out[("port", p.id)] = p
    for link in ir.links:
        out[("link", link.id)] = link
    for v in ir.vlans.values():
        out[("vlan", str(v.vlan_id))] = v
    for intf in ir.l3intfs:
        out[("l3intf", intf.id)] = intf
    for c in ir.clients:
        out[("client", c.id)] = c
    return out


def _changed_fields(a: Any, b: Any) -> tuple[str, ...]:
    changed: list[str] = []
    for f in fields(a):
        if f.name in _IGNORED_FIELDS:
            continue
        if getattr(a, f.name) != getattr(b, f.name):
            changed.append(f.name)
    return tuple(changed)


def diff_ir(baseline: IR, proposed: IR) -> IRDiff:
    base = _index(baseline)
    prop = _index(proposed)
    added: list[EntityRef] = []
    removed: list[EntityRef] = []
    modified: list[Modified] = []
    for key in prop.keys() - base.keys():
        added.append(EntityRef(*key))
    for key in base.keys() - prop.keys():
        removed.append(EntityRef(*key))
    for key in base.keys() & prop.keys():
        changed = _changed_fields(base[key], prop[key])
        if changed:
            modified.append(Modified(EntityRef(*key), changed))
    return IRDiff(tuple(added), tuple(removed), tuple(modified))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_diff.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/diff.py tests/ir/test_diff.py
git commit -m "feat(ir): IRDiff neutral change set + diff_ir (meta-insensitive)"
```

---

## Task 7: `link_carried_vlans` (correct access/trunk semantics)

A VLAN crosses a link via the tagged intersection (both trunks) **or** a matching native VLAN. An access port presents its VLAN *untagged*, so it joins a trunk only via the trunk's native — never a tagged VLAN.

**Files:**
- Create: `src/digital_twin/ir/graphs.py`
- Test: `tests/ir/test_graphs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_graphs.py`:

```python
from digital_twin.ir.entities import Port, PortMode
from digital_twin.ir.graphs import link_carried_vlans


def _trunk(pid: str, native: int | None, tagged: tuple[int, ...]) -> Port:
    return Port(id=pid, device_id=pid.split(":")[0], name="p", mode=PortMode.TRUNK,
                native_vlan=native, tagged_vlans=tagged)


def _access(pid: str, native: int) -> Port:
    return Port(id=pid, device_id=pid.split(":")[0], name="p", mode=PortMode.ACCESS,
                native_vlan=native)


def test_trunk_to_trunk_is_tagged_intersection_plus_matching_native():
    a = _trunk("d1:p", native=1, tagged=(10, 30))
    b = _trunk("d2:p", native=1, tagged=(30, 40))
    assert link_carried_vlans(a, b) == {1, 30}


def test_trunk_to_trunk_native_mismatch_drops_native():
    a = _trunk("d1:p", native=1, tagged=(30,))
    b = _trunk("d2:p", native=99, tagged=(30,))
    assert link_carried_vlans(a, b) == {30}  # native 1 != 99, only tagged 30 carried


def test_access_match_carries_native_only():
    assert link_carried_vlans(_access("d1:p", 30), _access("d2:p", 30)) == {30}


def test_access_mismatch_carries_nothing():
    assert link_carried_vlans(_access("d1:p", 10), _access("d2:p", 20)) == set()


def test_access_joins_trunk_only_via_native_not_tagged():
    access = _access("d1:p", 10)
    # trunk tags VLAN 10 but its native is 1: the access port is untagged -> NOT carried
    trunk_tags_10 = _trunk("d2:p", native=1, tagged=(10, 30))
    assert link_carried_vlans(access, trunk_tags_10) == set()
    # but if the trunk's native is 10, the untagged access VLAN matches
    trunk_native_10 = _trunk("d2:p", native=10, tagged=(30,))
    assert link_carried_vlans(access, trunk_native_10) == {10}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_graphs.py -v`
Expected: FAIL — `ModuleNotFoundError: ...graphs`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/graphs.py`:

```python
"""Derived graph views over the IR (networkx).

Device-level L2 multigraph with PORT-DERIVED edges + per-VLAN subgraphs. LAG/MCLAG
collapse by bundle_id; independent physical links stay parallel (redundancy = cycle);
VC fabric is one node. Per-VLAN graphs include only participating nodes and annotate
each with access_ports/exits/membership.
"""

from __future__ import annotations

from .entities import Port, PortMode


def _tagged(port: Port) -> set[int]:
    return set(port.tagged_vlans) if port.mode is PortMode.TRUNK else set()


def link_carried_vlans(port_a: Port, port_b: Port) -> set[int]:
    """VLANs carried across a link.

    Tagged path: intersection of both endpoints' tagged sets (trunks only).
    Untagged path: the native VLAN, but only when both endpoints' natives match
    (an access port presents its VLAN untagged, so it joins a trunk only via the
    trunk's native — never a tagged VLAN).
    """
    carried = _tagged(port_a) & _tagged(port_b)
    if (
        port_a.native_vlan is not None
        and port_a.native_vlan == port_b.native_vlan
    ):
        carried.add(port_a.native_vlan)
    return carried
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_graphs.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/graphs.py tests/ir/test_graphs.py
git commit -m "feat(ir): link_carried_vlans with correct access/trunk semantics"
```

---

## Task 8: `build_l2_graph` (bundle collapse, port-aware edges, min-confidence merge)

**Files:**
- Modify: `src/digital_twin/ir/graphs.py`
- Test: `tests/ir/test_graphs.py`

- [ ] **Step 1: Write the failing tests**

Add these imports to the **top** import section of `tests/ir/test_graphs.py`:

```python
import networkx as nx

from digital_twin.ir.confidence import ConfidenceLevel
from digital_twin.ir.entities import Device, DeviceRole, Link, LinkKind
from digital_twin.ir.graphs import build_l2_graph
from digital_twin.ir.model import IRBuilder
from digital_twin.ir.provenance import Provenance, fact_meta
```

Then append these helpers and tests below the existing ones:

```python
def _sw(did: str) -> Device:
    return Device(id=did, role=DeviceRole.SWITCH, site="s1")


def _tp(did: str, name: str, tagged: tuple[int, ...]) -> Port:
    return Port(id=f"{did}:{name}", device_id=did, name=name, mode=PortMode.TRUNK,
                native_vlan=None, tagged_vlans=tagged)


def _link(pa: str, pb: str, kind: LinkKind, bundle: str | None = None,
          prov: Provenance = Provenance.LLDP_TWO_SIDED) -> Link:
    return Link(id=f"{pa}__{pb}", a_port=pa, b_port=pb, kind=kind, bundle_id=bundle,
                meta=fact_meta(prov))


def _edge(g: nx.MultiGraph, u: str, v: str) -> dict:
    return next(iter(g.get_edge_data(u, v).values()))


def test_single_trunk_is_one_edge_with_ports_and_vlans():
    pa, pb = _tp("d1", "ge-0/0/1", (30,)), _tp("d2", "ge-0/0/1", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).add_port(pa).add_port(pb)
          .add_link(_link(pa.id, pb.id, LinkKind.PHYSICAL)).build())
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 1
    data = _edge(g, "d1", "d2")
    assert data["vlans"] == {30}
    assert set(data["member_ports"]) == {pa.id, pb.id}


def test_two_independent_physical_links_are_parallel_edges():
    pa1, pb1 = _tp("d1", "ge-0/0/1", (30,)), _tp("d2", "ge-0/0/1", (30,))
    pa2, pb2 = _tp("d1", "ge-0/0/2", (30,)), _tp("d2", "ge-0/0/2", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa1).add_port(pb1).add_port(pa2).add_port(pb2)
          .add_link(_link(pa1.id, pb1.id, LinkKind.PHYSICAL))
          .add_link(_link(pa2.id, pb2.id, LinkKind.PHYSICAL)).build())
    assert build_l2_graph(ir).number_of_edges() == 2  # redundant L2 path


def test_one_lag_bundle_collapses_to_one_edge_unions_vlans_and_mins_confidence():
    pa1, pb1 = _tp("d1", "ae0a", (30,)), _tp("d2", "ae0a", (30,))
    pa2, pb2 = _tp("d1", "ae0b", (40,)), _tp("d2", "ae0b", (40,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa1).add_port(pb1).add_port(pa2).add_port(pb2)
          .add_link(_link(pa1.id, pb1.id, LinkKind.LAG, bundle="ae0",
                          prov=Provenance.LLDP_TWO_SIDED))
          .add_link(_link(pa2.id, pb2.id, LinkKind.LAG, bundle="ae0",
                          prov=Provenance.LLDP_ONE_SIDED)).build())
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 1
    data = _edge(g, "d1", "d2")
    assert data["vlans"] == {30, 40}
    assert data["confidence"].level is ConfidenceLevel.LOW  # min of HIGH and LOW


def test_two_independent_lags_same_pair_stay_two_edges():
    # distinct bundle_ids => two logical links => a real redundant path
    pa1, pb1 = _tp("d1", "ae0a", (30,)), _tp("d2", "ae0a", (30,))
    pa2, pb2 = _tp("d1", "ae1a", (30,)), _tp("d2", "ae1a", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa1).add_port(pb1).add_port(pa2).add_port(pb2)
          .add_link(_link(pa1.id, pb1.id, LinkKind.LAG, bundle="ae0"))
          .add_link(_link(pa2.id, pb2.id, LinkKind.LAG, bundle="ae1")).build())
    assert build_l2_graph(ir).number_of_edges() == 2


def test_vc_internal_link_dropped_and_member_folded():
    vc = Device(id="d1", role=DeviceRole.SWITCH, site="s1", vc_members=("d1b",))
    member = Device(id="d1b", role=DeviceRole.SWITCH, site="s1")
    pa, pb = _tp("d1", "vcp0", (30,)), _tp("d1b", "vcp1", (30,))
    ir = (IRBuilder().add_device(vc).add_device(member).add_port(pa).add_port(pb)
          .add_link(_link(pa.id, pb.id, LinkKind.VC)).build())
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 0
    assert "d1" in g.nodes and "d1b" not in g.nodes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_graphs.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_l2_graph'`.

- [ ] **Step 3: Write the implementation**

Update the imports at the **top** of `src/digital_twin/ir/graphs.py` so they read exactly:

```python
from __future__ import annotations

import networkx as nx

from .confidence import Confidence, min_confidence
from .entities import LinkKind, Port, PortMode
from .model import IR
```

Then append:

```python
def _vc_root_map(ir: IR) -> dict[str, str]:
    """Map each VC member device id -> the containing VC device id."""
    root: dict[str, str] = {}
    for dev in ir.devices.values():
        for member in dev.vc_members:
            root[member] = dev.id
    return root


def _node_for(dev_id: str, vc_root: dict[str, str]) -> str:
    return vc_root.get(dev_id, dev_id)


def build_l2_graph(ir: IR) -> nx.MultiGraph:
    """Device-level L2 multigraph with port-derived edges.

    - LAG/MCLAG links sharing (node-pair, bundle_id) collapse to ONE logical edge
      (vlans unioned, confidence = min over members, member_ports accumulated).
    - Standalone links each get their own edge, so two independent physical links
      (or two independent LAG bundles) between the same pair stay PARALLEL = a cycle.
    - VC fabric is one node; VC-internal links are dropped.

    Edge attrs: ``vlans: set[int]``, ``kind: str``, ``bundle_id: str | None``,
    ``link_ids: list[str]``, ``member_ports: list[str]``, ``confidence: Confidence``.
    """
    g: nx.MultiGraph = nx.MultiGraph()
    vc_root = _vc_root_map(ir)
    for dev in ir.devices.values():
        if dev.id not in vc_root:  # members fold into their VC root
            g.add_node(dev.id)

    bundle_keys: dict[tuple[frozenset[str], str], object] = {}
    for link in ir.links:
        pa, pb = ir.port(link.a_port), ir.port(link.b_port)
        na, nb = _node_for(pa.device_id, vc_root), _node_for(pb.device_id, vc_root)
        if na == nb:
            continue  # VC-internal / self
        vlans = link_carried_vlans(pa, pb)
        conf: Confidence = link.meta.confidence
        is_bundle = link.kind in (LinkKind.LAG, LinkKind.MCLAG) and link.bundle_id is not None
        if is_bundle:
            assert link.bundle_id is not None
            ckey = (frozenset((na, nb)), link.bundle_id)
            existing = bundle_keys.get(ckey)
            if existing is not None:
                data = g[na][nb][existing]
                data["vlans"] |= vlans
                data["link_ids"].append(link.id)
                data["member_ports"].extend((pa.id, pb.id))
                data["confidence"] = min_confidence(data["confidence"], conf)
                continue
            key = g.add_edge(na, nb, vlans=set(vlans), kind="lag",
                             bundle_id=link.bundle_id, link_ids=[link.id],
                             member_ports=[pa.id, pb.id], confidence=conf)
            bundle_keys[ckey] = key
        else:
            g.add_edge(na, nb, vlans=set(vlans), kind=link.kind.value,
                       bundle_id=link.bundle_id, link_ids=[link.id],
                       member_ports=[pa.id, pb.id], confidence=conf)
    return g
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_graphs.py -v`
Expected: PASS (all graph tests, including Task 7's).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/graphs.py tests/ir/test_graphs.py
git commit -m "feat(ir): build_l2_graph with bundle_id collapse, port-aware edges, min-confidence merge"
```

---

## Task 9: `build_vlan_graph` (participating nodes + annotations)

The per-VLAN subgraph that loop/blackhole checks consume. Includes only participating nodes (carrying an edge, **or** holding a member access port / exit for the VLAN) and annotates each — so an isolated *member* (the blackhole case) is present and marked, while pure non-members are excluded.

**Files:**
- Modify: `src/digital_twin/ir/graphs.py`
- Test: `tests/ir/test_graphs.py`

- [ ] **Step 1: Write the failing tests**

Add to the **top** import section of `tests/ir/test_graphs.py`:

```python
from collections import defaultdict

from digital_twin.ir.entities import L3Intf, L3Role
from digital_twin.ir.graphs import build_vlan_graph
```

Then append these tests below the existing ones:

```python
def _access(did: str, name: str, vlan: int) -> Port:
    return Port(id=f"{did}:{name}", device_id=did, name=name, mode=PortMode.ACCESS,
                native_vlan=vlan)


def _cyclomatic(g: nx.MultiGraph) -> int:
    return g.number_of_edges() - g.number_of_nodes() + nx.number_connected_components(g)


def test_vlan_graph_excludes_pure_non_member_nodes():
    # d3 carries only VLAN 40; for VLAN 30 it is a pure non-member -> excluded.
    pa, pb = _tp("d1", "u1", (30,)), _tp("d2", "u1", (30,))
    pc, pd = _tp("d1", "u2", (40,)), _tp("d3", "u1", (40,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).add_device(_sw("d3"))
          .add_port(pa).add_port(pb).add_port(pc).add_port(pd)
          .add_link(_link(pa.id, pb.id, LinkKind.PHYSICAL))
          .add_link(_link(pc.id, pd.id, LinkKind.PHYSICAL)).build())
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert set(v30.nodes) == {"d1", "d2"}  # d3 excluded


def test_vlan_graph_annotates_access_ports_and_exits():
    acc = _access("d2", "ge-0/0/9", 30)
    irb = L3Intf(device_id="d1", role=L3Role.IRB, vlan_id=30, subnet="10.0.30.0/24")
    pa, pb = _tp("d1", "u1", (30,)), _tp("d2", "u1", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa).add_port(pb).add_port(acc).add_l3intf(irb)
          .add_link(_link(pa.id, pb.id, LinkKind.PHYSICAL)).build())
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert v30.nodes["d2"]["access_ports"] == [acc.id]
    assert v30.nodes["d2"]["is_member"] is True
    assert v30.nodes["d1"]["exits"] == [irb.id]
    assert v30.nodes["d1"]["is_exit"] is True


def test_isolated_member_is_included_and_marked():
    # d2 has a VLAN-30 access port but NO link carrying VLAN 30 -> isolated member.
    acc = _access("d2", "ge-0/0/9", 30)
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(_tp("d1", "u1", (30,))).add_port(acc).build())
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert "d2" in v30.nodes
    assert v30.nodes["d2"]["is_member"] is True
    assert v30.degree("d2") == 0  # isolated => blackhole candidate for later checks


def test_vlan_graph_ring_is_a_cycle():
    ports = {
        ("d1", "a"): _tp("d1", "a", (30,)), ("d2", "a"): _tp("d2", "a", (30,)),
        ("d2", "b"): _tp("d2", "b", (30,)), ("d3", "a"): _tp("d3", "a", (30,)),
        ("d3", "b"): _tp("d3", "b", (30,)), ("d1", "b"): _tp("d1", "b", (30,)),
    }
    b = IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).add_device(_sw("d3"))
    for p in ports.values():
        b.add_port(p)
    b.add_link(_link(ports[("d1", "a")].id, ports[("d2", "a")].id, LinkKind.PHYSICAL))
    b.add_link(_link(ports[("d2", "b")].id, ports[("d3", "a")].id, LinkKind.PHYSICAL))
    b.add_link(_link(ports[("d3", "b")].id, ports[("d1", "b")].id, LinkKind.PHYSICAL))
    ir = b.build()
    v30 = build_vlan_graph(ir, build_l2_graph(ir), 30)
    assert _cyclomatic(v30) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/ir/test_graphs.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_vlan_graph'`.

- [ ] **Step 3: Write the implementation**

Update the entities import at the top of `src/digital_twin/ir/graphs.py` to include `L3Role` (so the line reads exactly `from .entities import L3Role, LinkKind, Port, PortMode`), then append these functions (no new import lines — `L3Role` is now in the top import):

```python
def _access_ports_by_node(ir: IR, vlan_id: int, vc_root: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in ir.ports.values():
        if p.mode is PortMode.ACCESS and p.native_vlan == vlan_id:
            out.setdefault(_node_for(p.device_id, vc_root), []).append(p.id)
    return out


def _exits_by_node(ir: IR, vlan_id: int, vc_root: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for intf in ir.l3intfs:
        if intf.role in (L3Role.IRB, L3Role.SVI) and intf.vlan_id == vlan_id:
            out.setdefault(_node_for(intf.device_id, vc_root), []).append(intf.id)
    return out


def build_vlan_graph(ir: IR, l2: nx.MultiGraph, vlan_id: int) -> nx.MultiGraph:
    """Per-VLAN subgraph of the L2 graph.

    Includes a node iff it participates in the VLAN: it carries a VLAN-bearing edge,
    OR it holds a member access port for the VLAN, OR it holds an exit (IRB/SVI) for
    the VLAN. Each node is annotated: ``access_ports``, ``exits``, ``is_member``
    (has access ports), ``is_exit`` (has exits). Pure non-members are excluded.
    """
    vc_root = _vc_root_map(ir)
    access_by_node = _access_ports_by_node(ir, vlan_id, vc_root)
    exits_by_node = _exits_by_node(ir, vlan_id, vc_root)

    carrying_edges = [
        (u, v, key, data)
        for u, v, key, data in l2.edges(keys=True, data=True)
        if vlan_id in data["vlans"]
    ]
    carrying_nodes = {n for u, v, _, _ in carrying_edges for n in (u, v)}
    participating = carrying_nodes | set(access_by_node) | set(exits_by_node)

    h: nx.MultiGraph = nx.MultiGraph()
    for node in participating:
        access = access_by_node.get(node, [])
        exits = exits_by_node.get(node, [])
        h.add_node(node, access_ports=access, exits=exits,
                   is_member=bool(access), is_exit=bool(exits))
    for u, v, key, data in carrying_edges:
        h.add_edge(u, v, key=key, vlans=set(data["vlans"]), kind=data["kind"],
                   bundle_id=data["bundle_id"], link_ids=list(data["link_ids"]),
                   member_ports=list(data["member_ports"]), confidence=data["confidence"])
    return h
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ir/test_graphs.py -v`
Expected: PASS (all graph tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/graphs.py tests/ir/test_graphs.py
git commit -m "feat(ir): build_vlan_graph with participating-node filtering + member/exit annotations"
```

---

## Task 10: Public API re-exports + full quality gate

**Files:**
- Modify: `src/digital_twin/ir/__init__.py`
- Test: `tests/ir/test_public_api.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/ir/test_public_api.py`:

```python
def test_public_api_importable_from_package_root():
    from digital_twin.ir import (
        IR,
        Client,
        Confidence,
        Device,
        FactMeta,
        IRBuilder,
        IRCapability,
        IRDiff,
        Link,
        Port,
        Provenance,
        Vlan,
        build_l2_graph,
        build_vlan_graph,
        diff_ir,
        fact_meta,
        link_carried_vlans,
        min_confidence,
    )

    assert IRBuilder().build().ir_version
    assert all(callable(f) for f in (build_l2_graph, build_vlan_graph,
                                     link_carried_vlans, diff_ir, min_confidence, fact_meta))
    assert all(x is not None for x in (IR, Client, Confidence, Device, FactMeta,
                                       IRCapability, IRDiff, Link, Port, Provenance, Vlan))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ir/test_public_api.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write the implementation**

Overwrite `src/digital_twin/ir/__init__.py`:

```python
"""Vendor-neutral Intermediate Representation (IR) for the network digital twin."""

from .capabilities import IRCapability
from .confidence import Confidence, ConfidenceLevel, min_confidence
from .diff import EntityRef, IRDiff, Modified, diff_ir
from .entities import (
    AttachKind,
    Client,
    ClientKind,
    Device,
    DeviceRole,
    L3Intf,
    L3Role,
    Link,
    LinkKind,
    Port,
    PortMode,
    StpMode,
    StpProvenance,
    Vlan,
    client_id,
    device_id,
    link_id,
    port_id,
)
from .graphs import build_l2_graph, build_vlan_graph, link_carried_vlans
from .model import IR_VERSION, IR, IRBuilder, IRValidationError
from .provenance import CONFIG_META, OBSERVED_META, FactMeta, Provenance, fact_meta

__all__ = [
    "IR",
    "IR_VERSION",
    "IRBuilder",
    "IRValidationError",
    "IRCapability",
    "Confidence",
    "ConfidenceLevel",
    "min_confidence",
    "Provenance",
    "FactMeta",
    "fact_meta",
    "CONFIG_META",
    "OBSERVED_META",
    "Device",
    "DeviceRole",
    "Port",
    "PortMode",
    "Link",
    "LinkKind",
    "Vlan",
    "L3Intf",
    "L3Role",
    "Client",
    "ClientKind",
    "AttachKind",
    "StpMode",
    "StpProvenance",
    "device_id",
    "port_id",
    "link_id",
    "client_id",
    "EntityRef",
    "Modified",
    "IRDiff",
    "diff_ir",
    "build_l2_graph",
    "build_vlan_graph",
    "link_carried_vlans",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/ir/test_public_api.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full quality gate**

Run:
```bash
uv run ruff format .
uv run ruff check --fix .
uv run ruff check .
uv run mypy
uv run pytest -q
```
Expected: `ruff format` normalizes line wrapping; `ruff check --fix` fixes import ordering; the second `ruff check` is clean; `mypy` reports no issues; all tests pass. Fix any remaining findings by hand until all are green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ir): public API re-exports; green format/lint/type/test gate"
```

---

## Done criteria for Plan 1

- `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `uv run pytest -q` — all green.
- `digital_twin.ir` exposes entities (each with `FactMeta`), `IRBuilder`/`IR` (validating), confidence + the canonical provenance table, capabilities, `IRDiff`/`diff_ir`, and the three graph builders.
- IR invariants enforced: duplicate ids and dangling references rejected at `build()`.
- L2 graph: edges are port-derived (a port change is detected — GS7); LAG/MCLAG collapse by `bundle_id`; independent physical links and independent bundles stay parallel (redundancy = cycle); VC folded; merged-edge confidence is `min`.
- Per-VLAN graph: only participating nodes; access-port/exit annotations; isolated members present and marked (blackhole-ready).
- `link_carried_vlans`: correct access/trunk semantics (access joins a trunk only via native).

**Next:** Plan 2 — StateProvider + Mist ingest + compiler + the equivalence gate.
