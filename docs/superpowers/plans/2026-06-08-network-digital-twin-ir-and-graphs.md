# Network Digital Twin — Plan 1: IR Core + Derived Graphs

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the vendor-neutral Intermediate Representation (IR) — typed immutable entities, capability/versioning metadata, and the derived L2 / per-VLAN graphs — that every later layer (ingest, checks, verdict) consumes.

**Architecture:** Pure-Python library with no I/O and no vendor knowledge. Frozen dataclass entities carry provenance/confidence. An `IRBuilder` assembles an immutable `IR` (with `ir_version` + `capabilities`). Graph builders project the IR into `networkx` multigraphs: a device-level L2 graph that collapses LAG/MCLAG bundles to one logical edge but keeps independent physical links as parallel edges (so redundancy reads as a cycle), and per-VLAN subgraphs for loop/blackhole analysis in later plans.

**Tech Stack:** Python 3.14, `uv` (env/deps), `networkx` (graphs), `netaddr` (IP/subnet, used in later plans), `pytest` + `ruff` + `mypy`. src layout (`src/digital_twin/`).

This is **Plan 1 of 5** for Milestone 1. Later plans: (2) StateProvider + ingest + compiler + equivalence gate; (3) ScopeResolver + L0 validation + apply; (4) check engine + verdict/decision + the 4 checks; (5) drivers + observability/replay + golden scenarios.

---

## File Structure

```
src/digital_twin/
├── __init__.py
└── ir/
    ├── __init__.py          # re-exports the public IR API
    ├── confidence.py        # ConfidenceLevel, Confidence, min_confidence()
    ├── capabilities.py      # IRCapability enum
    ├── entities.py          # enums, id helpers, frozen entity dataclasses
    ├── model.py             # IR (frozen), IRBuilder, IR_VERSION
    └── graphs.py            # link_carried_vlans(), build_l2_graph(), build_vlan_graph()
tests/
└── ir/
    ├── __init__.py
    ├── test_confidence.py
    ├── test_capabilities.py
    ├── test_entities.py
    ├── test_model.py
    └── test_graphs.py
```

Each module has one responsibility: `confidence` (the confidence value object + composition rule), `capabilities` (the runtime capability vocabulary), `entities` (typed facts + stable ids), `model` (the immutable container + builder), `graphs` (derived views). They form a clean dependency chain: `graphs → model → entities → {confidence, capabilities}`.

---

## Task 0: Project scaffold

**Files:**
- Create: `pyproject.toml`, `src/digital_twin/__init__.py`, `src/digital_twin/ir/__init__.py`, `tests/__init__.py`, `tests/ir/__init__.py`

- [ ] **Step 1: Initialize the uv package**

Run:
```bash
uv init --package --name digital-twin --python 3.14
```
Expected: creates `pyproject.toml` and `src/digital_twin/__init__.py`. (If `uv` reports 3.14 is not installed, run `uv python install 3.14` first.)

- [ ] **Step 2: Add runtime and dev dependencies**

Run:
```bash
uv add networkx netaddr
uv add --dev pytest ruff mypy
```
Expected: dependencies appear in `pyproject.toml`; `uv.lock` is written.

- [ ] **Step 3: Add tool configuration to `pyproject.toml`**

Merge the blocks below into the `pyproject.toml` that `uv init`/`uv add` produced. **Keep the
`[build-system]` and `[project]`/`[dependency-groups]` sections uv generated** (don't hand-edit the
dependency version specifiers); only set `requires-python` and add the `[tool.*]` blocks:

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

(mypy is scoped to `src/digital_twin` via `files`, so test modules don't need strict annotations.)

- [ ] **Step 4: Create the test package markers**

Create `tests/__init__.py` (empty) and `tests/ir/__init__.py` (empty), and ensure `src/digital_twin/ir/__init__.py` exists (empty for now).

Run:
```bash
mkdir -p tests/ir && touch tests/__init__.py tests/ir/__init__.py src/digital_twin/ir/__init__.py
```

- [ ] **Step 5: Verify the toolchain runs**

Run:
```bash
uv run pytest -q
```
Expected: pytest runs and reports "no tests ran" (exit code 5) — confirms the environment works.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: scaffold digital-twin package (uv, py3.14, pytest/ruff/mypy)"
```

---

## Task 1: Confidence value object

The confidence axis from the spec: a categorical level plus reasons, with a deterministic `MIN`-composition rule (a derived fact's confidence is the lowest of its inputs).

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


def test_single_confidence_is_returned_as_is():
    c = Confidence(ConfidenceLevel.HIGH, ("two-sided LLDP",))
    assert min_confidence(c) == c


def test_min_picks_lowest_level():
    high = Confidence(ConfidenceLevel.HIGH, ("configured",))
    low = Confidence(ConfidenceLevel.LOW, ("one-sided LLDP",))
    result = min_confidence(high, low)
    assert result.level is ConfidenceLevel.LOW


def test_min_keeps_reasons_only_from_lowest_inputs():
    high = Confidence(ConfidenceLevel.HIGH, ("configured",))
    low_a = Confidence(ConfidenceLevel.LOW, ("one-sided LLDP",))
    low_b = Confidence(ConfidenceLevel.LOW, ("uncorroborated neighbor",))
    result = min_confidence(high, low_a, low_b)
    assert result.level is ConfidenceLevel.LOW
    assert result.reasons == ("one-sided LLDP", "uncorroborated neighbor")
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

Run:
```bash
uv run pytest tests/ir/test_confidence.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'digital_twin.ir.confidence'`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/confidence.py`:

```python
"""Confidence: categorical (HIGH/MEDIUM/LOW) + reasons, with MIN composition.

A derived fact's confidence is the lowest level among the facts it relied on
(and the inference method). Reasons explaining the floor are accumulated from
the lowest-level inputs. Never a float — false precision undermines explainability.
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
    """Compose confidences: take the lowest level; keep reasons from every
    input at that lowest level."""
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

Run:
```bash
uv run pytest tests/ir/test_confidence.py -v
```
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/confidence.py tests/ir/test_confidence.py
git commit -m "feat(ir): confidence value object with MIN composition"
```

---

## Task 2: IRCapability vocabulary

The runtime capability set an IR instance declares, matched against each check's `requires()` in later plans.

**Files:**
- Create: `src/digital_twin/ir/capabilities.py`
- Test: `tests/ir/test_capabilities.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_capabilities.py`:

```python
from digital_twin.ir.capabilities import IRCapability


def test_capability_values_are_stable_strings():
    assert IRCapability.WIRED_L2.value == "wired.l2"
    assert IRCapability.LINKS_BIDIRECTIONAL.value == "links.bidirectional"
    assert IRCapability.CLIENTS_ACTIVE.value == "clients.active"
    assert IRCapability.STP_STATE.value == "stp.state"
    assert IRCapability.L3_EXITS.value == "l3.exits"


def test_capabilities_are_hashable_set_members():
    caps = {IRCapability.WIRED_L2, IRCapability.STP_STATE}
    assert IRCapability.WIRED_L2 in caps
    assert IRCapability.CLIENTS_ACTIVE not in caps
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/ir/test_capabilities.py -v
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/capabilities.py`:

```python
"""IRCapability: what facts an IR instance actually populated.

Checks declare requires() against these; the engine self-gates a check to
INSUFFICIENT_DATA when a required capability is absent. New domains ADD
capabilities; existing checks are unaffected.
"""

from __future__ import annotations

from enum import Enum


class IRCapability(str, Enum):
    WIRED_L2 = "wired.l2"
    LINKS_BIDIRECTIONAL = "links.bidirectional"
    CLIENTS_ACTIVE = "clients.active"
    STP_STATE = "stp.state"
    L3_EXITS = "l3.exits"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/ir/test_capabilities.py -v
```
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/capabilities.py tests/ir/test_capabilities.py
git commit -m "feat(ir): IRCapability vocabulary"
```

---

## Task 3: Entities + stable id helpers

The typed, immutable facts and the deterministic id derivation that makes baseline/proposed IRs diffable and (later) cross-vendor reconcilable.

**Files:**
- Create: `src/digital_twin/ir/entities.py`
- Test: `tests/ir/test_entities.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_entities.py`:

```python
import pytest

from digital_twin.ir.confidence import Confidence, ConfidenceLevel
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
    LinkSource,
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


def test_device_id_normalizes_mac():
    assert device_id("AA:BB:CC:00:11:22") == "aabbcc001122"


def test_port_id_combines_device_and_name():
    assert port_id("aabbcc001122", "ge-0/0/1") == "aabbcc001122:ge-0/0/1"


def test_link_id_is_canonical_regardless_of_endpoint_order():
    a = "dev1:ge-0/0/1"
    b = "dev2:ge-0/0/5"
    assert link_id(a, b) == link_id(b, a)


def test_client_id_normalizes_mac():
    assert client_id("DE:AD:BE:EF:00:01") == "deadbeef0001"


def test_entities_construct_with_expected_fields():
    dev = Device(id="d1", role=DeviceRole.SWITCH, site="s1", model="EX4100")
    port = Port(
        id="d1:ge-0/0/1",
        device_id="d1",
        name="ge-0/0/1",
        mode=PortMode.TRUNK,
        tagged_vlans=(10, 30),
        stp_enabled=True,
        stp_mode=StpMode.RSTP,
        stp_provenance=StpProvenance.OBSERVED,
    )
    link = Link(
        id="d1:ge-0/0/1__d2:ge-0/0/5",
        a_port="d1:ge-0/0/1",
        b_port="d2:ge-0/0/5",
        kind=LinkKind.PHYSICAL,
        source=LinkSource.LLDP,
        bidirectional=True,
        confidence=Confidence(ConfidenceLevel.HIGH),
    )
    vlan = Vlan(vlan_id=30, name="voice")
    intf = L3Intf(device_id="d1", role=L3Role.IRB, vlan_id=30, subnet="10.0.30.0/24")
    client = Client(
        mac="deadbeef0001",
        kind=ClientKind.WIRELESS,
        attach_kind=AttachKind.AP,
        attach_id="ap1",
        vlan=30,
    )
    assert dev.role is DeviceRole.SWITCH
    assert port.tagged_vlans == (10, 30)
    assert link.bidirectional is True
    assert vlan.scope == "site"
    assert intf.role is L3Role.IRB
    assert client.active is True


def test_entities_are_frozen():
    dev = Device(id="d1", role=DeviceRole.SWITCH, site="s1")
    with pytest.raises(Exception):
        dev.site = "s2"  # type: ignore[misc]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/ir/test_entities.py -v
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/entities.py`:

```python
"""Vendor-neutral IR entities (frozen) and stable id helpers.

Ids derive from stable keys (MAC, device+port name) — never a vendor object_id —
so baseline/proposed IRs line up for diffing and future cross-vendor reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .confidence import Confidence


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


class LinkSource(str, Enum):
    LLDP = "lldp"
    CONFIG = "config"


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


@dataclass(frozen=True)
class Link:
    id: str
    a_port: str
    b_port: str
    kind: LinkKind
    source: LinkSource
    bidirectional: bool
    confidence: Confidence


@dataclass(frozen=True)
class Vlan:
    vlan_id: int
    name: str | None = None
    scope: str = "site"


@dataclass(frozen=True)
class L3Intf:
    device_id: str
    role: L3Role
    vlan_id: int | None = None
    port: str | None = None
    subnet: str | None = None
    ip: str | None = None


@dataclass(frozen=True)
class Client:
    mac: str
    kind: ClientKind
    attach_kind: AttachKind
    attach_id: str
    vlan: int | None = None
    ip: str | None = None
    active: bool = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/ir/test_entities.py -v
```
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/entities.py tests/ir/test_entities.py
git commit -m "feat(ir): frozen entities and stable id helpers"
```

---

## Task 4: IR container + IRBuilder

The immutable container (with `ir_version` + `capabilities`) and an ergonomic builder used by tests and (later) the Mist ingest.

**Files:**
- Create: `src/digital_twin/ir/model.py`
- Test: `tests/ir/test_model.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/ir/test_model.py`:

```python
import pytest

from digital_twin.ir.capabilities import IRCapability
from digital_twin.ir.entities import Device, DeviceRole, Port, PortMode, Vlan
from digital_twin.ir.model import IR_VERSION, IR, IRBuilder


def _switch(did: str) -> Device:
    return Device(id=did, role=DeviceRole.SWITCH, site="s1")


def test_empty_ir_has_version_and_no_capabilities():
    ir = IRBuilder().build()
    assert ir.ir_version == IR_VERSION
    assert ir.capabilities == frozenset()
    assert ir.links == ()


def test_builder_collects_entities_and_lookups_work():
    port = Port(id="d1:ge-0/0/1", device_id="d1", name="ge-0/0/1", mode=PortMode.TRUNK)
    ir = (
        IRBuilder()
        .add_device(_switch("d1"))
        .add_port(port)
        .add_vlan(Vlan(vlan_id=30))
        .with_capability(IRCapability.WIRED_L2)
        .build()
    )
    assert ir.device("d1").role is DeviceRole.SWITCH
    assert ir.port("d1:ge-0/0/1") is port
    assert ir.vlans[30].vlan_id == 30
    assert ir.has(IRCapability.WIRED_L2) is True
    assert ir.has(IRCapability.STP_STATE) is False


def test_ir_mappings_are_read_only():
    ir = IRBuilder().add_device(_switch("d1")).build()
    with pytest.raises(TypeError):
        ir.devices["d2"] = _switch("d2")  # type: ignore[index]


def test_missing_lookup_raises_keyerror():
    ir = IRBuilder().build()
    with pytest.raises(KeyError):
        ir.port("nope")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/ir/test_model.py -v
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/model.py`:

```python
"""IR: the immutable, vendor-neutral container, plus an IRBuilder.

An IR is a frozen snapshot (mappings are read-only proxies). It carries an
ir_version (schema contract) and a capabilities set (runtime contract). Build
one via IRBuilder; never mutate after build().
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .capabilities import IRCapability
from .entities import Client, Device, L3Intf, Link, Port, Vlan

IR_VERSION = "1.0"


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
        self._devices[device.id] = device
        return self

    def add_port(self, port: Port) -> IRBuilder:
        self._ports[port.id] = port
        return self

    def add_link(self, link: Link) -> IRBuilder:
        self._links.append(link)
        return self

    def add_vlan(self, vlan: Vlan) -> IRBuilder:
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

    def build(self) -> IR:
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

Run:
```bash
uv run pytest tests/ir/test_model.py -v
```
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/model.py tests/ir/test_model.py
git commit -m "feat(ir): immutable IR container and IRBuilder"
```

---

## Task 5: `link_carried_vlans` helper

Determines which VLANs cross a link — the basis for per-VLAN graphs. A VLAN is carried only if *both* endpoints carry it.

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


def test_trunk_to_trunk_is_intersection():
    a = _trunk("d1:p", native=1, tagged=(10, 30))
    b = _trunk("d2:p", native=1, tagged=(30, 40))
    assert link_carried_vlans(a, b) == {1, 30}


def test_access_match_carries_native_only():
    a = _access("d1:p", native=30)
    b = _access("d2:p", native=30)
    assert link_carried_vlans(a, b) == {30}


def test_access_mismatch_carries_nothing():
    a = _access("d1:p", native=10)
    b = _access("d2:p", native=20)
    assert link_carried_vlans(a, b) == set()


def test_access_tagged_vlans_are_ignored_on_access_port():
    # An access port only presents its native VLAN even if tagged_vlans is set.
    a = Port(id="d1:p", device_id="d1", name="p", mode=PortMode.ACCESS,
             native_vlan=10, tagged_vlans=(30,))
    b = _trunk("d2:p", native=1, tagged=(10, 30))
    assert link_carried_vlans(a, b) == {10}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/ir/test_graphs.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'digital_twin.ir.graphs'`.

- [ ] **Step 3: Write the implementation**

Create `src/digital_twin/ir/graphs.py`:

```python
"""Derived graph views over the IR (networkx).

Device-level L2 multigraph + per-VLAN subgraphs. LAG/MCLAG bundles collapse to
one logical edge; independent physical links stay parallel (redundancy = cycle);
VC fabric is one node. These views feed loop/blackhole checks in later plans.
"""

from __future__ import annotations

from .entities import Port, PortMode


def _port_vlans(port: Port) -> set[int]:
    vlans: set[int] = set()
    if port.native_vlan is not None:
        vlans.add(port.native_vlan)
    if port.mode is PortMode.TRUNK:
        vlans.update(port.tagged_vlans)
    return vlans


def link_carried_vlans(port_a: Port, port_b: Port) -> set[int]:
    """VLANs carried across a link = intersection of both endpoints' VLAN sets.

    Access port presents only its native VLAN; trunk presents native + tagged.
    """
    return _port_vlans(port_a) & _port_vlans(port_b)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/ir/test_graphs.py -v
```
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/graphs.py tests/ir/test_graphs.py
git commit -m "feat(ir): link_carried_vlans helper"
```

---

## Task 6: `build_l2_graph`

The device-level L2 multigraph with the normalization rules that make later loop detection correct.

**Files:**
- Modify: `src/digital_twin/ir/graphs.py`
- Test: `tests/ir/test_graphs.py`

- [ ] **Step 1: Write the failing tests**

Add these imports to the **top** import section of `tests/ir/test_graphs.py` (alongside the existing
`Port`/`PortMode`/`link_carried_vlans` imports — keep all imports at the top so ruff E402 stays
happy):

```python
import networkx as nx

from digital_twin.ir.confidence import Confidence, ConfidenceLevel
from digital_twin.ir.entities import Device, DeviceRole, Link, LinkKind, LinkSource
from digital_twin.ir.graphs import build_l2_graph
from digital_twin.ir.model import IRBuilder
```

Then append these helpers and test functions below the existing ones:

```python
def _sw(did: str) -> Device:
    return Device(id=did, role=DeviceRole.SWITCH, site="s1")


def _trunk_port(did: str, name: str, tagged: tuple[int, ...]) -> Port:
    return Port(id=f"{did}:{name}", device_id=did, name=name, mode=PortMode.TRUNK,
                native_vlan=None, tagged_vlans=tagged)


def _link(pa: str, pb: str, kind: LinkKind) -> Link:
    return Link(id=f"{pa}__{pb}", a_port=pa, b_port=pb, kind=kind,
                source=LinkSource.LLDP, bidirectional=True,
                confidence=Confidence(ConfidenceLevel.HIGH))


def test_single_trunk_between_two_switches_is_one_edge():
    pa = _trunk_port("d1", "ge-0/0/1", (30,))
    pb = _trunk_port("d2", "ge-0/0/1", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa).add_port(pb)
          .add_link(_link(pa.id, pb.id, LinkKind.PHYSICAL)).build())
    g = build_l2_graph(ir)
    assert g.number_of_nodes() == 2
    assert g.number_of_edges() == 1
    data = next(iter(g.get_edge_data("d1", "d2").values()))
    assert data["vlans"] == {30}


def test_two_independent_physical_trunks_are_parallel_edges():
    pa1 = _trunk_port("d1", "ge-0/0/1", (30,))
    pb1 = _trunk_port("d2", "ge-0/0/1", (30,))
    pa2 = _trunk_port("d1", "ge-0/0/2", (30,))
    pb2 = _trunk_port("d2", "ge-0/0/2", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa1).add_port(pb1).add_port(pa2).add_port(pb2)
          .add_link(_link(pa1.id, pb1.id, LinkKind.PHYSICAL))
          .add_link(_link(pa2.id, pb2.id, LinkKind.PHYSICAL)).build())
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 2  # parallel edges == a redundant L2 path


def test_lag_members_collapse_to_one_logical_edge():
    pa1 = _trunk_port("d1", "ae0.0", (30,))
    pb1 = _trunk_port("d2", "ae0.0", (30,))
    pa2 = _trunk_port("d1", "ae0.1", (40,))
    pb2 = _trunk_port("d2", "ae0.1", (40,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa1).add_port(pb1).add_port(pa2).add_port(pb2)
          .add_link(_link(pa1.id, pb1.id, LinkKind.LAG))
          .add_link(_link(pa2.id, pb2.id, LinkKind.LAG)).build())
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 1  # bundle is one logical link, not a loop
    data = next(iter(g.get_edge_data("d1", "d2").values()))
    assert data["vlans"] == {30, 40}  # union across bundle members


def test_vc_internal_links_are_dropped_and_members_map_to_one_node():
    # d1 is a VC containing member "d1b"; a link between d1 and d1b is internal.
    vc = Device(id="d1", role=DeviceRole.SWITCH, site="s1", vc_members=("d1b",))
    member = Device(id="d1b", role=DeviceRole.SWITCH, site="s1")
    pa = _trunk_port("d1", "vcp-0", (30,))
    pb = _trunk_port("d1b", "vcp-1", (30,))
    ir = (IRBuilder().add_device(vc).add_device(member)
          .add_port(pa).add_port(pb)
          .add_link(_link(pa.id, pb.id, LinkKind.VC)).build())
    g = build_l2_graph(ir)
    assert g.number_of_edges() == 0  # internal to the VC node
    assert "d1" in g.nodes and "d1b" not in g.nodes  # member folded into VC root
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/ir/test_graphs.py -v
```
Expected: FAIL — `ImportError: cannot import name 'build_l2_graph'`.

- [ ] **Step 3: Write the implementation**

First, update the imports at the **top** of `src/digital_twin/ir/graphs.py` so they read exactly:

```python
from __future__ import annotations

import networkx as nx

from .entities import Link, LinkKind, Port, PortMode
from .model import IR
```

Then append these functions:

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
    """Device-level L2 multigraph.

    - LAG/MCLAG members between the same node pair collapse to ONE logical edge
      (intentional bundle, not a loop).
    - Independent physical links between the same pair stay PARALLEL edges
      (a redundant L2 path — a cycle).
    - VC fabric is one node; VC-internal links are dropped.

    Edge attrs: ``vlans: set[int]``, ``kind: str``, ``links: list[str]``,
    ``confidence: Confidence``.
    """
    g: nx.MultiGraph = nx.MultiGraph()
    vc_root = _vc_root_map(ir)
    for dev in ir.devices.values():
        if dev.id not in vc_root:  # skip members folded into their VC root
            g.add_node(_node_for(dev.id, vc_root))

    lag_keys: dict[frozenset[str], object] = {}
    for link in ir.links:
        pa = ir.port(link.a_port)
        pb = ir.port(link.b_port)
        na = _node_for(pa.device_id, vc_root)
        nb = _node_for(pb.device_id, vc_root)
        if na == nb:
            continue  # VC-internal / self
        vlans = link_carried_vlans(pa, pb)
        if link.kind in (LinkKind.LAG, LinkKind.MCLAG):
            pair = frozenset((na, nb))
            existing = lag_keys.get(pair)
            if existing is not None:
                data = g[na][nb][existing]
                data["vlans"] |= vlans
                data["links"].append(link.id)
                continue
            key = g.add_edge(na, nb, vlans=set(vlans), kind="lag",
                             links=[link.id], confidence=link.confidence)
            lag_keys[pair] = key
        else:
            g.add_edge(na, nb, vlans=set(vlans), kind=link.kind.value,
                       links=[link.id], confidence=link.confidence)
    return g
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/ir/test_graphs.py -v
```
Expected: PASS (all graph tests pass, including the 4 from Task 5).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/graphs.py tests/ir/test_graphs.py
git commit -m "feat(ir): build_l2_graph with LAG collapse, parallel-edge redundancy, VC folding"
```

---

## Task 7: `build_vlan_graph`

The per-VLAN subgraph — the input to loop and blackhole checks in later plans.

**Files:**
- Modify: `src/digital_twin/ir/graphs.py`
- Test: `tests/ir/test_graphs.py`

- [ ] **Step 1: Write the failing tests**

Add this import to the **top** import section of `tests/ir/test_graphs.py`:

```python
from digital_twin.ir.graphs import build_vlan_graph
```

Then append these test functions below the existing ones:

```python
def _cyclomatic(g: nx.MultiGraph) -> int:
    # Independent cycles = E - N + C. >0 means a redundant path / loop exists.
    return g.number_of_edges() - g.number_of_nodes() + nx.number_connected_components(g)


def test_vlan_graph_keeps_only_edges_carrying_the_vlan():
    pa = _trunk_port("d1", "ge-0/0/1", (30,))
    pb = _trunk_port("d2", "ge-0/0/1", (30,))
    pc = _trunk_port("d1", "ge-0/0/2", (40,))
    pd = _trunk_port("d3", "ge-0/0/1", (40,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).add_device(_sw("d3"))
          .add_port(pa).add_port(pb).add_port(pc).add_port(pd)
          .add_link(_link(pa.id, pb.id, LinkKind.PHYSICAL))
          .add_link(_link(pc.id, pd.id, LinkKind.PHYSICAL)).build())
    l2 = build_l2_graph(ir)
    v30 = build_vlan_graph(l2, 30)
    assert set(v30.edges()) == {("d1", "d2")}
    assert v30.number_of_edges() == 1


def test_vlan_graph_exposes_a_parallel_edge_loop():
    pa1 = _trunk_port("d1", "ge-0/0/1", (30,))
    pb1 = _trunk_port("d2", "ge-0/0/1", (30,))
    pa2 = _trunk_port("d1", "ge-0/0/2", (30,))
    pb2 = _trunk_port("d2", "ge-0/0/2", (30,))
    ir = (IRBuilder().add_device(_sw("d1")).add_device(_sw("d2"))
          .add_port(pa1).add_port(pb1).add_port(pa2).add_port(pb2)
          .add_link(_link(pa1.id, pb1.id, LinkKind.PHYSICAL))
          .add_link(_link(pa2.id, pb2.id, LinkKind.PHYSICAL)).build())
    v30 = build_vlan_graph(build_l2_graph(ir), 30)
    assert _cyclomatic(v30) == 1  # the redundant VLAN-30 path is a cycle


def test_vlan_graph_ring_of_three_is_a_cycle():
    # d1-d2-d3-d1 ring, all carrying VLAN 30 on single physical links.
    ports = {
        ("d1", "a"): _trunk_port("d1", "a", (30,)),
        ("d2", "a"): _trunk_port("d2", "a", (30,)),
        ("d2", "b"): _trunk_port("d2", "b", (30,)),
        ("d3", "a"): _trunk_port("d3", "a", (30,)),
        ("d3", "b"): _trunk_port("d3", "b", (30,)),
        ("d1", "b"): _trunk_port("d1", "b", (30,)),
    }
    b = IRBuilder().add_device(_sw("d1")).add_device(_sw("d2")).add_device(_sw("d3"))
    for p in ports.values():
        b.add_port(p)
    b.add_link(_link(ports[("d1", "a")].id, ports[("d2", "a")].id, LinkKind.PHYSICAL))
    b.add_link(_link(ports[("d2", "b")].id, ports[("d3", "a")].id, LinkKind.PHYSICAL))
    b.add_link(_link(ports[("d3", "b")].id, ports[("d1", "b")].id, LinkKind.PHYSICAL))
    v30 = build_vlan_graph(build_l2_graph(b.build()), 30)
    assert _cyclomatic(v30) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest tests/ir/test_graphs.py -v
```
Expected: FAIL — `ImportError: cannot import name 'build_vlan_graph'`.

- [ ] **Step 3: Write the implementation**

Append to `src/digital_twin/ir/graphs.py`:

```python
def build_vlan_graph(l2: nx.MultiGraph, vlan_id: int) -> nx.MultiGraph:
    """Subgraph of the L2 multigraph restricted to edges carrying ``vlan_id``.

    All device nodes are preserved (so isolated members are visible); only edges
    whose ``vlans`` set contains ``vlan_id`` are kept.
    """
    h: nx.MultiGraph = nx.MultiGraph()
    h.add_nodes_from(l2.nodes)
    for u, v, key, data in l2.edges(keys=True, data=True):
        if vlan_id in data["vlans"]:
            h.add_edge(
                u,
                v,
                key=key,
                vlans=set(data["vlans"]),
                kind=data["kind"],
                links=list(data["links"]),
                confidence=data["confidence"],
            )
    return h
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/ir/test_graphs.py -v
```
Expected: PASS (all graph tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/graphs.py tests/ir/test_graphs.py
git commit -m "feat(ir): build_vlan_graph per-VLAN subgraph"
```

---

## Task 8: Public API re-exports + full quality gate

Expose a clean `digital_twin.ir` surface and verify the whole package passes lint, types, and tests.

**Files:**
- Modify: `src/digital_twin/ir/__init__.py`
- Test: `tests/ir/test_public_api.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/ir/test_public_api.py`:

```python
def test_public_api_is_importable_from_package_root():
    from digital_twin.ir import (
        IR,
        Client,
        Confidence,
        ConfidenceLevel,
        Device,
        IRBuilder,
        IRCapability,
        Link,
        Port,
        Vlan,
        build_l2_graph,
        build_vlan_graph,
        link_carried_vlans,
        min_confidence,
    )

    assert IRBuilder().build().ir_version
    assert callable(build_l2_graph)
    assert callable(build_vlan_graph)
    assert callable(link_carried_vlans)
    assert callable(min_confidence)
    # reference the imported symbols so linters keep them
    assert all(x is not None for x in (IR, Client, Confidence, ConfidenceLevel,
                                       Device, IRCapability, Link, Port, Vlan))
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest tests/ir/test_public_api.py -v
```
Expected: FAIL — `ImportError` (symbols not re-exported).

- [ ] **Step 3: Write the implementation**

Overwrite `src/digital_twin/ir/__init__.py`:

```python
"""Vendor-neutral Intermediate Representation (IR) for the network digital twin."""

from .capabilities import IRCapability
from .confidence import Confidence, ConfidenceLevel, min_confidence
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
    LinkSource,
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
from .model import IR_VERSION, IR, IRBuilder

__all__ = [
    "IR",
    "IR_VERSION",
    "IRBuilder",
    "IRCapability",
    "Confidence",
    "ConfidenceLevel",
    "min_confidence",
    "Device",
    "DeviceRole",
    "Port",
    "PortMode",
    "Link",
    "LinkKind",
    "LinkSource",
    "StpMode",
    "StpProvenance",
    "Vlan",
    "L3Intf",
    "L3Role",
    "Client",
    "ClientKind",
    "AttachKind",
    "device_id",
    "port_id",
    "link_id",
    "client_id",
    "build_l2_graph",
    "build_vlan_graph",
    "link_carried_vlans",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/ir/test_public_api.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the full quality gate**

Run:
```bash
uv run ruff check --fix .
uv run ruff check .
uv run mypy
uv run pytest -q
```
Expected: the first command auto-fixes import ordering/formatting; the second reports clean; mypy reports no issues; all tests pass. Fix any remaining ruff/mypy findings (e.g. unused imports, missing annotations) by hand until all are green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ir): public API re-exports; green lint/type/test gate"
```

---

## Done criteria for Plan 1

- `uv run pytest -q` — all tests pass.
- `uv run ruff check .` — clean.
- `uv run mypy` — clean.
- `digital_twin.ir` exposes entities, `IRBuilder`/`IR`, confidence, capabilities, and the three graph builders.
- The L2 graph correctly: collapses LAG/MCLAG bundles, keeps independent physical links parallel (so a redundant path is a cycle), folds VC members into one node; per-VLAN graphs restrict to carried VLANs and expose cycles via the cyclomatic number.

**Next:** Plan 2 — StateProvider + Mist ingest + compiler + the equivalence gate (the foundation-risk gate that must pass before checks are built on top).
