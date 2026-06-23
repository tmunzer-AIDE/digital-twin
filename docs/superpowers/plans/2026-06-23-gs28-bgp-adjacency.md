# GS28 — BGP adjacency break Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a switch/gateway BGP config change simulable and surface a peering break — structural codes at REVIEW, live-telemetry-confirmed established-peer breaks at UNSAFE — without ever false-SAFE.

**Architecture:** Mirror the merged GS27 OSPF pattern (`wired.l3.ospf_withdrawal`, `OspfNeighbor`, escalate-only telemetry). Add a role-aware diff-bearing `BgpPeer` IR entity (minted for switches AND gateways), a `BgpNeighbor` observational telemetry entity, a self-isolating telemetry ingester earning `BGP_TELEMETRY`, bgp leaves on the field-gate allowlist, and a role-agnostic `wired.l3.bgp_adjacency` check. The telemetry join is a DIRECT established-peer-IP set membership (config carries the neighbor IP) — no subnet prediction, no reachability module.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy strict on `src`. Full gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

## Global Constraints

- **Never false-SAFE.** Precedence UNKNOWN > UNSAFE > REVIEW > SAFE. A modeled-but-unactioned leaf is a false-SAFE; an unparseable value must stay diff-bearing or produce a coverage note, never collapse.
- **Structural floor is telemetry-independent.** `requires() == frozenset()`; `applies_to == diff.touches("bgp_peer")`. `BGP_TELEMETRY` is NEVER required — telemetry only escalates inside `run()`.
- **Telemetry is escalate-only:** it adds/raises findings, never produces or downgrades to SAFE, and uses BASELINE telemetry only (proposed telemetry is replay-of-today, not a post-change fact).
- **Secret-free:** `bgp_config.*.auth_key` is DENIED (not allowlisted) → an auth_key edit resolves UNKNOWN. `BgpNeighbor`/`BgpPeer` carry no secrets.
- **Leaf-tightened allowlist:** model EXACTLY the break-relevant leaves; everything else (timers, policies, bfd, multihop, `networks`, `auth_key`) stays denied → UNKNOWN.
- **L0 is permissive** for `bgp_config` (every committed OAS schema has top-level `additionalProperties` unset; `gatewaytemplate` defines `bgp_config`). No schema/OAS change is needed.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- mypy strict applies to `src` only (tests are not type-checked). Pyright/IDE diagnostics are noise.

---

## File Structure

- **Create** `src/digital_twin/adapters/mist/ingest/bgp_neighbors.py` — self-isolating `BgpNeighborIngester` (telemetry).
- **Create** `src/digital_twin/checks/wired/bgp_adjacency.py` — the `wired.l3.bgp_adjacency` check.
- **Modify** `src/digital_twin/ir/entities.py` — `BgpPeer`, `BgpNeighbor`.
- **Modify** `src/digital_twin/ir/model.py` — IR fields + IRBuilder methods + `_validate_bgp_peers`.
- **Modify** `src/digital_twin/ir/diff.py` — register `bgp_peer`.
- **Modify** `src/digital_twin/ir/capabilities.py` — `BGP_TELEMETRY`.
- **Modify** `src/digital_twin/ir/__init__.py` — exports.
- **Modify** `src/digital_twin/scope/allowlist.py` — `_BGP_LEAVES`, `_BGP_GATEWAY_LEAVES`.
- **Modify** `src/digital_twin/providers/base.py` — `RawSiteState.bgp_neighbors`.
- **Modify** `src/digital_twin/providers/mist_api.py` — `_bgp_neighbors` fetch.
- **Modify** `src/digital_twin/observability/replay/store.py` — `bgp_neighbors` round-trip.
- **Modify** `src/digital_twin/adapters/mist/ingest/switch.py` — role-aware `_bgp` pass.
- **Modify** `src/digital_twin/adapters/mist/adapter.py` — register ingester + materialize `bgp_config`.
- **Modify** `src/digital_twin/checks/wired/__init__.py` — register check.
- **Modify** `tests/test_public_api.py` — bump count 19→20 + registration test.
- **Modify** `tests/golden/builders.py` + `tests/golden/test_golden_scenarios.py` — BGP builders + goldens.
- **Modify** `docs/ROADMAP.md`, memory file — wrap-up.

---

### Task 1: `BgpPeer` config entity + IR/builder/diff registration

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (after `OspfNeighbor`, ~line 306)
- Modify: `src/digital_twin/ir/model.py` (IR fields ~line 72; IRBuilder `__init__` ~line 104; `add_bgp_peer` near `add_ospf_intf` ~line 143; `build()` ~line 377; `_validate` ~line 223 + new `_validate_bgp_peers`)
- Modify: `src/digital_twin/ir/diff.py` (`_ENTITY_KINDS` ~line 37; `_IGNORED_BY_KIND` ~line 23)
- Modify: `src/digital_twin/ir/__init__.py` (imports ~line 7; `__all__` ~line 70)
- Test: `tests/ir/test_bgp_peer.py` (create)

**Interfaces:**
- Produces: `BgpPeer` dataclass (frozen) with stable `.id = f"{device_id}:bgp:{neighbor_ip}"`; `IR.bgp_peers: tuple[BgpPeer, ...]`; `IRBuilder.add_bgp_peer(peer: BgpPeer) -> IRBuilder`; diff kind `"bgp_peer"` with `session_name` ignored.
- Consumes: `DeviceRole`, `CONFIG_META`, `FactMeta` (already in entities.py); `device_id` helper.

- [ ] **Step 1: Write the failing test**

```python
# tests/ir/test_bgp_peer.py
import pytest

from digital_twin.ir import BgpPeer, DeviceRole
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder, IRValidationError
from digital_twin.ir import IRCapability, Device


def _peer(nip="10.0.0.2", **kw):
    return BgpPeer(device_id="d1", role=DeviceRole.SWITCH, session_name="s1", neighbor_ip=nip, **kw)


def _ir(peers):
    b = (IRBuilder().with_capability(IRCapability.WIRED_L2)
         .add_device(Device(id="d1", role=DeviceRole.SWITCH, site="x")))
    for p in peers:
        b.add_bgp_peer(p)
    return b.build()


def test_id_is_device_and_neighbor_ip():
    assert _peer().id == "d1:bgp:10.0.0.2"


def test_session_name_is_diff_ignored():
    base = _ir([_peer(session_name="underlay")])
    prop = _ir([_peer(session_name="renamed")])  # same (device, ip), only session_name differs
    assert diff_ir(base, prop).is_empty()


def test_neighbor_as_change_is_diff_bearing():
    base = _ir([_peer(neighbor_as=65001)])
    prop = _ir([_peer(neighbor_as=65002)])
    assert not diff_ir(base, prop).is_empty()
    assert diff_ir(base, prop).touches("bgp_peer")


def test_duplicate_id_raises():
    with pytest.raises(IRValidationError):
        _ir([_peer(), _peer()])  # same (device, ip) added twice -> caller must dedup
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ir/test_bgp_peer.py -q`
Expected: FAIL (`ImportError: cannot import name 'BgpPeer'`).

- [ ] **Step 3: Add the `BgpPeer` entity**

In `src/digital_twin/ir/entities.py`, after the `OspfNeighbor` class (after line ~306), add:

```python
@dataclass(frozen=True)
class BgpPeer:
    """A switch/gateway BGP peering (one per session-neighbor), modeled for the
    wired.l3.bgp_adjacency check (GS28). Identity is (device, neighbor_ip): a
    device peers with a given neighbor IP once; session_name is config grouping,
    DIFF-IGNORED (see ir/diff.py) so a session rename is not a false change. ASN /
    type / via / disabled that are PRESENT-but-unparseable (templated {{var}} /
    non-enum / non-bool) keep their parsed field None and carry the raw token in
    the matching *_unresolved field (diff-bearing) so absent->templated does not
    collapse to None==None (the GS27 metric false-SAFE scar tissue). auth_key is
    NEVER modeled (secret). `unresolved` = the neighbor-IP map key is not a literal
    IP. `ambiguous` = 2+ sessions defined this (device, neighbor_ip) with differing
    modeled attrs (set by ingest, never last-win)."""

    device_id: str
    role: DeviceRole
    session_name: str
    neighbor_ip: str
    local_as: int | None = None
    neighbor_as: int | None = None
    session_type: str | None = None   # "external" | "internal"; None if absent OR unparseable
    disabled: bool = False            # per-neighbor admin shutdown (schema default False)
    via: str | None = None            # gateway transport lan|tunnel|vpn|wan; None if absent/unparseable
    local_as_unresolved: str | None = None
    neighbor_as_unresolved: str | None = None
    session_type_unresolved: str | None = None
    via_unresolved: str | None = None
    disabled_unresolved: str | None = None
    unresolved: bool = False
    ambiguous: bool = False
    meta: FactMeta = CONFIG_META
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.device_id}:bgp:{self.neighbor_ip}")
```

- [ ] **Step 4: Wire `BgpPeer` into the IR + builder + diff**

In `src/digital_twin/ir/model.py`:
- Import: add `BgpPeer` to the `.entities` import block (alongside `OspfIntf`, line ~27).
- IR dataclass: after `ospf_telemetry_unparsed_count: int = 0` (line ~72), add:
```python
    bgp_peers: tuple[BgpPeer, ...] = ()
```
- IRBuilder `__init__`: after `self._ospf_intf_ids: set[str] = set()` (line ~94), add:
```python
        self._bgp_peers: list[BgpPeer] = []
        self._bgp_peer_ids: set[str] = set()
```
- Add the builder method near `add_ospf_intf` (after line ~143):
```python
    def add_bgp_peer(self, peer: BgpPeer) -> IRBuilder:
        if peer.id in self._bgp_peer_ids:
            raise IRValidationError(f"duplicate bgp peer id {peer.id}")
        self._bgp_peer_ids.add(peer.id)
        self._bgp_peers.append(peer)
        return self
```
- `build()`: in the `IR(...)` constructor add `bgp_peers=tuple(self._bgp_peers),` (after `ospf_telemetry_unparsed_count=...`, line ~377).
- `_validate()`: after the `self._validate_ospf_intfs()` call (line ~223) add `self._validate_bgp_peers()`, and define the method near `_validate_ospf_intfs` (~line 284):
```python
    def _validate_bgp_peers(self) -> None:
        for p in self._bgp_peers:
            if p.device_id not in self._devices:
                raise IRValidationError(f"bgp peer {p.id} references unknown device {p.device_id}")
```

In `src/digital_twin/ir/diff.py`:
- `_ENTITY_KINDS` (line ~37): append before the closing `]`:
```python
    ("bgp_peer", lambda ir: ir.bgp_peers),
```
- `_IGNORED_BY_KIND` (line ~23): add an entry:
```python
    "bgp_peer": frozenset({"session_name"}),
```

In `src/digital_twin/ir/__init__.py`: add `BgpPeer` to the `.entities` import block (alphabetically, before `Client`) and `"BgpPeer"` to `__all__` (near the Ospf exports).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/ir/test_bgp_peer.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/ir/ tests/ir/test_bgp_peer.py
git commit -m "$(printf 'feat(gs28): BgpPeer config entity + diff registration\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: `BgpNeighbor` telemetry entity + `BGP_TELEMETRY` capability

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (after `BgpPeer`)
- Modify: `src/digital_twin/ir/capabilities.py` (`IRCapability`, after `OSPF_TELEMETRY` ~line 27)
- Modify: `src/digital_twin/ir/model.py` (IR fields; IRBuilder `__init__`; `set_bgp_neighbors`; `build()`)
- Modify: `src/digital_twin/ir/__init__.py` (exports)
- Test: `tests/ir/test_bgp_neighbor.py` (create)

**Interfaces:**
- Produces: `BgpNeighbor` (frozen, observational); `IRCapability.BGP_TELEMETRY = "bgp.telemetry"`; `IR.bgp_neighbors: tuple[BgpNeighbor, ...]`; `IR.bgp_telemetry_unparsed_count: int`; `IRBuilder.set_bgp_neighbors(neighbors, unparsed_count=0) -> IRBuilder`.
- Consumes: `OBSERVED_META`, `FactMeta`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ir/test_bgp_neighbor.py
from digital_twin.ir import BgpNeighbor, IRCapability
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(neighbors, unparsed=0):
    return (IRBuilder().with_capability(IRCapability.WIRED_L2)
            .set_bgp_neighbors(neighbors, unparsed).build())


def test_bgp_neighbor_id():
    n = BgpNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Established")
    assert n.id == "d1:bgpnbr:10.0.0.5"


def test_bgp_neighbor_is_not_diff_bearing():
    base = _ir([BgpNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Established")])
    prop = _ir([])  # telemetry vanished -> NOT a config change
    assert diff_ir(base, prop).is_empty()


def test_unparsed_carried_and_no_capability_from_setter():
    ir = _ir([BgpNeighbor(device_id="d1", peer_ip="10.0.0.5")], unparsed=2)
    assert ir.bgp_telemetry_unparsed_count == 2
    # the setter earns NO capability — BGP_TELEMETRY is the fetch layer's job (Task 5)
    assert IRCapability.BGP_TELEMETRY not in ir.capabilities


def test_up_flag_represented():
    n = BgpNeighbor(device_id="d1", peer_ip="10.0.0.5", up=True)
    assert n.up is True and n.state == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ir/test_bgp_neighbor.py -q`
Expected: FAIL (`ImportError: cannot import name 'BgpNeighbor'`).

- [ ] **Step 3: Add the entity + capability + builder plumbing**

In `src/digital_twin/ir/entities.py`, after `BgpPeer`:
```python
@dataclass(frozen=True)
class BgpNeighbor:
    """OBSERVATIONAL live BGP adjacency (org_bgp/site_bgp stats). Evidence/
    escalation input only: NOT in diff_ir, no IR validation. Both `state` and
    `up` are represented so liveness conveyed via the boolean (not the string)
    still escalates."""

    device_id: str
    peer_ip: str
    state: str = ""                   # raw BGP state, e.g. "Established"
    up: bool | None = None
    neighbor_as: int | None = None
    vrf: str | None = None
    meta: FactMeta = OBSERVED_META
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.device_id}:bgpnbr:{self.peer_ip}")
```

In `src/digital_twin/ir/capabilities.py`, after `OSPF_TELEMETRY` (line ~27):
```python
    BGP_TELEMETRY = "bgp.telemetry"  # org_bgp/site_bgp neighbor stats fetched (peer-break layer)
```

In `src/digital_twin/ir/model.py`:
- Import `BgpNeighbor` from `.entities`.
- IR dataclass: after `bgp_peers: tuple[BgpPeer, ...] = ()` add:
```python
    bgp_neighbors: tuple[BgpNeighbor, ...] = ()
    bgp_telemetry_unparsed_count: int = 0
```
- IRBuilder `__init__`: after the `_bgp_peer_ids` lines add:
```python
        self._bgp_neighbors: list[BgpNeighbor] = []
        self._bgp_unparsed = 0
```
- Add the setter near `set_ospf_neighbors` (~line 192):
```python
    def set_bgp_neighbors(
        self, neighbors: Iterable[BgpNeighbor], unparsed_count: int = 0
    ) -> IRBuilder:
        """Publish OBSERVATIONAL live BGP adjacencies atomically. NOT validated in
        build() — a bad neighbor must never fail the IR (non-load-bearing)."""
        self._bgp_neighbors = list(neighbors)
        self._bgp_unparsed = unparsed_count
        return self
```
- `build()`: add to the `IR(...)` constructor:
```python
            bgp_neighbors=tuple(self._bgp_neighbors),
            bgp_telemetry_unparsed_count=self._bgp_unparsed,
```

In `src/digital_twin/ir/__init__.py`: import + `__all__`-export `BgpNeighbor`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ir/test_bgp_neighbor.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/ tests/ir/test_bgp_neighbor.py
git commit -m "$(printf 'feat(gs28): BgpNeighbor telemetry entity + BGP_TELEMETRY capability\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: bgp allowlist leaves (switch + gateway) + L0 permissiveness test

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py` (`_BGP_LEAVES` near `_OSPF_LEAVES` ~line 107; `_GATEWAY_LEAVES` ~line 87; `RAW_ALLOWLIST` ~line 140; `EFFECTIVE_ALLOWLIST` ~line 208; `GATEWAY_EFFECTIVE_ALLOWLIST` ~line 223; `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` ~line 229)
- Test: `tests/scope/test_bgp_allowlist.py` (create)

**Interfaces:**
- Produces: `bgp_config.*.local_as`, `bgp_config.*.type`, `bgp_config.*.neighbors.*.neighbor_as`, `bgp_config.*.neighbors.*.disabled` in-scope on switch surfaces (site_setting/device/networktemplate/sitetemplate/effective/device-profile-switch) and (those + `bgp_config.*.via`) on gateway surfaces (gatewaytemplate/gateway-effective/device-profile-gateway). `auth_key`/`networks`/timers stay DENIED.
- Consumes: the existing `is_allowed`/field-gate path-matching (whatever `RAW_ALLOWLIST` feeds — confirm the matcher fn name when writing the test; below uses a direct membership check on the tuples, which is matcher-independent).

- [ ] **Step 1: Write the failing test**

```python
# tests/scope/test_bgp_allowlist.py
from digital_twin.scope.allowlist import (
    EFFECTIVE_ALLOWLIST,
    GATEWAY_EFFECTIVE_ALLOWLIST,
    RAW_ALLOWLIST,
    _BGP_LEAVES,
    _BGP_GATEWAY_LEAVES,
)

_MODELED = {
    "bgp_config.*.local_as",
    "bgp_config.*.type",
    "bgp_config.*.neighbors.*.neighbor_as",
    "bgp_config.*.neighbors.*.disabled",
}
_DENIED = {
    "bgp_config.*.auth_key",
    "bgp_config.*.networks",
    "bgp_config.*.hold_time",
    "bgp_config.*.neighbors.*.import_policy",
}


def test_switch_surfaces_carry_the_four_modeled_leaves():
    assert _MODELED == set(_BGP_LEAVES)
    for obj in ("site_setting", "device", "networktemplate", "sitetemplate"):
        assert _MODELED <= set(RAW_ALLOWLIST[obj])
    assert _MODELED <= set(EFFECTIVE_ALLOWLIST)


def test_gateway_surfaces_add_via():
    assert set(_BGP_GATEWAY_LEAVES) == _MODELED | {"bgp_config.*.via"}
    assert set(_BGP_GATEWAY_LEAVES) <= set(RAW_ALLOWLIST["gatewaytemplate"])
    assert set(_BGP_GATEWAY_LEAVES) <= set(GATEWAY_EFFECTIVE_ALLOWLIST)


def test_secrets_and_unmodeled_leaves_are_denied_everywhere():
    for obj in ("site_setting", "device", "gatewaytemplate"):
        assert not (_DENIED & set(RAW_ALLOWLIST[obj]))
    assert not (_DENIED & set(EFFECTIVE_ALLOWLIST))
    assert not (_DENIED & set(GATEWAY_EFFECTIVE_ALLOWLIST))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scope/test_bgp_allowlist.py -q`
Expected: FAIL (`ImportError: cannot import name '_BGP_LEAVES'`).

- [ ] **Step 3: Add the leaves**

In `src/digital_twin/scope/allowlist.py`, after `_OSPF_LEAVES` (line ~107):
```python
# BGP peering the IR models AND acts on (GS28 wired.l3.bgp_adjacency): per-neighbor
# AS + admin-state (the break signals), session local_as + type. EVERYTHING else
# (auth_key=secret, networks=advertised prefixes [no v1 check], timers, policies,
# bfd, multihop) stays DENIED -> UNKNOWN: allowlisting a leaf no check reasons
# about is a false-SAFE.
_BGP_LEAVES: tuple[str, ...] = (
    "bgp_config.*.local_as",
    "bgp_config.*.type",
    "bgp_config.*.neighbors.*.neighbor_as",
    "bgp_config.*.neighbors.*.disabled",
)
# Gateway BGP adds the transport selector `via` (lan|tunnel|vpn|wan); switches are
# implicitly LAN and have no via.
_BGP_GATEWAY_LEAVES: tuple[str, ...] = (*_BGP_LEAVES, "bgp_config.*.via")
```

Splat into the switch surfaces (each gets `*_BGP_LEAVES,`):
- `RAW_ALLOWLIST["site_setting"]` (line ~147, after `*_OSPF_LEAVES,`).
- `RAW_ALLOWLIST["device"]` (line ~157, after `*_OSPF_LEAVES,`).
- `EFFECTIVE_ALLOWLIST` (line ~216, after `*_OSPF_LEAVES,`).
- `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"]` (line ~240, after `*_OSPF_LEAVES,`).
(`networktemplate` and `sitetemplate` inherit from `site_setting` automatically via lines 171 + 178 — no edit needed there.)

Splat `_BGP_GATEWAY_LEAVES` into `_GATEWAY_LEAVES` (line ~87) so gatewaytemplate/sitetemplate/gateway-effective/device-profile-gateway all inherit:
```python
_GATEWAY_LEAVES: tuple[str, ...] = (
    *_GATEWAY_PORT_LEAVES, *_GATEWAY_L3_LEAVES, *_GATEWAY_DHCP_LEAVES, *_BGP_GATEWAY_LEAVES,
)
```
(Define `_BGP_GATEWAY_LEAVES` ABOVE `_GATEWAY_LEAVES` — move the two BGP constants to before line 87, or forward-reference by defining them earlier. Simplest: place the `_BGP_LEAVES`/`_BGP_GATEWAY_LEAVES` block just before `_GATEWAY_PORT_LEAVES` at line ~74.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scope/test_bgp_allowlist.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Add the L0-permissiveness regression test**

Append to `tests/scope/test_bgp_allowlist.py`:
```python
from digital_twin.adapters.mist.validate.schema import validate_payload


def test_bgp_config_edit_does_not_fatal_at_l0():
    # bgp_config is permissive on device/site_setting (additionalProperties unset) and
    # DEFINED on gatewaytemplate -> a bgp_config edit must never be structurally fatal.
    payload = {"bgp_config": {"underlay": {"type": "external", "local_as": 65000,
               "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}}}
    for obj in ("device", "site_setting", "gatewaytemplate"):
        res = validate_payload(obj, payload, scope_roots={"bgp_config"})
        assert not res.fatal, (obj, res.findings)
```

Run: `uv run pytest tests/scope/test_bgp_allowlist.py -q`
Expected: PASS (4 tests). If the gatewaytemplate case produces a non-fatal `l0.schema.violation` (a real type mismatch in the synthetic payload vs the committed bgp_config schema), adjust the payload to satisfy the committed schema — but `fatal` must be False regardless.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/scope/allowlist.py tests/scope/test_bgp_allowlist.py
git commit -m "$(printf 'feat(gs28): bgp_config allowlist leaves (switch + gateway via); L0 permissive\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: `bgp_neighbors` fetch + RawSiteState + replay round-trip

**Files:**
- Modify: `src/digital_twin/providers/base.py` (`RawSiteState`, after `ospf_neighbors` line ~81)
- Modify: `src/digital_twin/providers/mist_api.py` (`_bgp_neighbors` near `_ospf_neighbors` line ~378; the `RawSiteState(...)` constructor line ~260)
- Modify: `src/digital_twin/observability/replay/store.py` (`_RAW_FIELDS` line ~46; `load_fixture_doc` line ~112)
- Test: `tests/observability/test_replay_store.py` (add a test)

**Interfaces:**
- Produces: `RawSiteState.bgp_neighbors: tuple[JsonObj, ...] = ()`; the fetcher tags `"bgp_neighbors"` into `meta.fetched` on success; replay round-trips the field.
- Consumes: the `attempt(...)` closure inside `_fetch_one`; `mistapi` SDK.

- [ ] **Step 1: Write the failing test**

Add to `tests/observability/test_replay_store.py`:
```python
def test_bgp_neighbors_round_trip(tmp_path):
    from digital_twin.observability.replay.store import load_fixture_doc
    from digital_twin.providers.base import RawSiteState

    # a doc WITHOUT bgp_neighbors still loads (pre-GS28 fixtures)
    base_doc = _minimal_fixture_doc()  # existing helper in this test module
    assert load_fixture_doc(base_doc).bgp_neighbors == ()

    # a doc WITH bgp_neighbors round-trips
    base_doc["bgp_neighbors"] = [{"mac": "aa", "peer_ip": "10.0.0.2", "state": "Established"}]
    state = load_fixture_doc(base_doc)
    assert len(state.bgp_neighbors) == 1
    assert state.bgp_neighbors[0]["peer_ip"] == "10.0.0.2"
```
(If the test module's minimal-doc helper has a different name, use the one already present — grep the file for the existing `load_fixture_doc` test and mirror its doc construction.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/observability/test_replay_store.py -q -k bgp`
Expected: FAIL (`AttributeError: ... 'bgp_neighbors'` or KeyError).

- [ ] **Step 3: Add the field + fetch + replay**

`src/digital_twin/providers/base.py` — after `ospf_neighbors: tuple[JsonObj, ...] = ()` (line ~81):
```python
    # observed BGP neighbor stats (GET /sites/{id}/stats/bgp_peers/search) — the
    # GS28 telemetry layer. Trailing + defaulted: absence is "not fetched".
    bgp_neighbors: tuple[JsonObj, ...] = ()
```

`src/digital_twin/providers/mist_api.py` — add near `_ospf_neighbors` (line ~378):
```python
    def _bgp_neighbors(self, s: SiteScope) -> list[_Json]:
        # OBSERVATIONAL BGP adjacency telemetry (GS28). Non-fatal: `attempt` records
        # any failure in StateMeta.failures and BgpNeighborIngester degrades to blind.
        resp = mistapi.api.v1.sites.stats.searchSiteBgpStats(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]
```
**Verify the SDK call name** before running: confirm `searchSiteBgpStats` exists with `python -c "import mistapi; print(hasattr(mistapi.api.v1.sites.stats, 'searchSiteBgpStats'))"`. If it prints `False`, list candidates with `python -c "import mistapi.api.v1.sites.stats as m; print([n for n in dir(m) if 'gp' in n.lower()])"` and use the BGP-stats search function (the analog of `searchSiteOspfStats`).

In the `RawSiteState(...)` constructor inside `_fetch_one` (after the `ospf_neighbors=...` block, line ~260):
```python
            bgp_neighbors=tuple(
                attempt("bgp_neighbors", lambda: self._bgp_neighbors(scope), [])
            ),
```

`src/digital_twin/observability/replay/store.py`:
- `_RAW_FIELDS` (line ~46): append `"bgp_neighbors",` after `"ospf_neighbors",`.
- `load_fixture_doc` (after line ~112, the `ospf_neighbors=...` line):
```python
        bgp_neighbors=tuple(data.get("bgp_neighbors", ())),  # .get: pre-GS28 fixtures
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/observability/test_replay_store.py -q`
Expected: PASS (all replay tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/providers/ src/digital_twin/observability/replay/store.py tests/observability/test_replay_store.py
git commit -m "$(printf 'feat(gs28): bgp_neighbors fetch + RawSiteState + replay round-trip\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 5: self-isolating `BgpNeighborIngester`

**Files:**
- Create: `src/digital_twin/adapters/mist/ingest/bgp_neighbors.py`
- Modify: `src/digital_twin/adapters/mist/adapter.py` (import ~line 24; default ingester list ~line 51)
- Test: `tests/adapters/mist/ingest/test_bgp_neighbors.py` (create)

**Interfaces:**
- Produces: `BgpNeighborIngester` (`.name = "bgp_neighbors"`, `produces() == {BGP_TELEMETRY}`); `build_bgp_neighbors(rows) -> tuple[list[BgpNeighbor], int]`; `_row_to_neighbor(row) -> BgpNeighbor | None`.
- Consumes: `IngestContext` (`ctx.raw.meta.fetched`, `ctx.raw.bgp_neighbors`, `ctx.builder.set_bgp_neighbors`); `BgpNeighbor`, `IRCapability`, `device_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/ingest/test_bgp_neighbors.py
from digital_twin.adapters.mist.ingest.bgp_neighbors import (
    BgpNeighborIngester,
    build_bgp_neighbors,
)
from digital_twin.ir import IRCapability


def test_parses_good_rows_and_counts_bad():
    rows = (
        {"mac": "aa:bb:cc:dd:ee:01", "peer_ip": "10.0.0.2", "neighbor_as": 65001,
         "state": "Established", "vrf_name": "default", "up": True},
        {"mac": "aa:bb:cc:dd:ee:01", "neighbor": "10.0.0.3", "state": "Idle"},  # fallback key
        {"mac": "aa:bb:cc:dd:ee:01"},  # no peer ip -> unparsed
        {"peer_ip": "10.0.0.9"},  # no mac -> unparsed
    )
    neighbors, unparsed = build_bgp_neighbors(rows)
    assert unparsed == 2
    by_ip = {n.peer_ip: n for n in neighbors}
    assert by_ip["10.0.0.2"].neighbor_as == 65001 and by_ip["10.0.0.2"].up is True
    assert by_ip["10.0.0.3"].state == "Idle"


def test_one_exploding_row_never_drops_the_batch():
    class Boom(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("boom")
    neighbors, unparsed = build_bgp_neighbors(({"mac": "aa", "peer_ip": "10.0.0.2"}, Boom()))
    assert len(neighbors) == 1 and unparsed == 1


class _Builder:
    def __init__(self):
        self.calls = []
    def set_bgp_neighbors(self, neighbors, unparsed):
        self.calls.append((list(neighbors), unparsed))


class _Raw:
    def __init__(self, fetched, rows):
        self.meta = type("M", (), {"fetched": fetched})()
        self.bgp_neighbors = rows


class _Ctx:
    def __init__(self, fetched, rows):
        self.raw = _Raw(fetched, rows)
        self.builder = _Builder()


def test_earns_capability_only_when_fetched():
    ing = BgpNeighborIngester()
    # not fetched -> no claim, no publish
    ctx = _Ctx((), ())
    assert ing.ingest(ctx) == frozenset()
    assert ctx.builder.calls == []
    # fetched (even empty) -> earns BGP_TELEMETRY
    ctx2 = _Ctx(("bgp_neighbors",), ())
    assert ing.ingest(ctx2) == frozenset({IRCapability.BGP_TELEMETRY})
    assert ctx2.builder.calls == [([], 0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/ingest/test_bgp_neighbors.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Write the ingester**

```python
# src/digital_twin/adapters/mist/ingest/bgp_neighbors.py
"""BGP neighbor telemetry ingester (GS28). OBSERVATIONAL, SELF-ISOLATING: it
never lets an exception reach IngesterRegistry.run. Earns BGP_TELEMETRY iff the
bgp_neighbors fetch succeeded (shape reachable, incl. genuinely-zero). A row with
no usable (mac, peer_ip) is COUNTED as unparsed (not silently dropped) so a
partially unrecognized fetch reads telemetry-blind, never 'no neighbors'."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.ir import BgpNeighbor, IRCapability, device_id

from .base import IngestContext

_Json = Mapping[str, Any]


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _row_to_neighbor(row: _Json) -> BgpNeighbor | None:
    # Field names confirmed against the Mist OAS (bgp_peers) at build; fail-soft.
    mac = _clean(row.get("mac"))
    peer_ip = _clean(row.get("peer_ip") or row.get("neighbor"))
    if not mac or not peer_ip:
        return None                       # unusable -> caller counts it unparsed
    up_raw = row.get("up")
    return BgpNeighbor(
        device_id=device_id(mac),
        peer_ip=peer_ip,
        state=_clean(row.get("state") or row.get("status")) or "",
        up=(bool(up_raw) if isinstance(up_raw, bool) else None),
        neighbor_as=_as_int(row.get("neighbor_as")),
        vrf=_clean(row.get("vrf_name") or row.get("vrf")),
    )


def build_bgp_neighbors(rows: tuple[_Json, ...]) -> tuple[list[BgpNeighbor], int]:
    neighbors: list[BgpNeighbor] = []
    unparsed = 0
    for row in rows:
        try:
            n = _row_to_neighbor(row)
        except Exception:  # noqa: BLE001 — one bad row never drops the batch
            n = None
        if n is None:
            unparsed += 1
        else:
            neighbors.append(n)
    return neighbors, unparsed


class BgpNeighborIngester:
    """Earns BGP_TELEMETRY on fetch-success; publishes neighbors + unparsed count."""

    name = "bgp_neighbors"

    def produces(self) -> frozenset[str]:
        return frozenset({IRCapability.BGP_TELEMETRY})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "bgp_neighbors" not in ctx.raw.meta.fetched:
            return frozenset()            # not fetched -> telemetry-blind, no claim
        try:
            neighbors, unparsed = build_bgp_neighbors(tuple(ctx.raw.bgp_neighbors))
            ctx.builder.set_bgp_neighbors(neighbors, unparsed)
        except Exception:  # noqa: BLE001 — best-effort: degrade to blind, never fatal
            return frozenset()
        return frozenset({IRCapability.BGP_TELEMETRY})
```

Register in `src/digital_twin/adapters/mist/adapter.py`: import `from .ingest.bgp_neighbors import BgpNeighborIngester` (~line 24) and append `BgpNeighborIngester()` to the default ingester list (~line 51, after `OspfNeighborIngester()`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/adapters/mist/ingest/test_bgp_neighbors.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/bgp_neighbors.py src/digital_twin/adapters/mist/adapter.py tests/adapters/mist/ingest/test_bgp_neighbors.py
git commit -m "$(printf 'feat(gs28): self-isolating BgpNeighborIngester + adapter registration\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 6: role-aware `_bgp` ingest pass (switch + gateway) with collision/token handling

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_bgp` pass near `_ospf` ~line 797; dispatch in `ingest()` ~line 357; helpers near `_metric_int`)
- Modify: `src/digital_twin/adapters/mist/adapter.py` (`_materialize` keys tuple ~line 85)
- Test: `tests/adapters/mist/ingest/test_bgp_ingest.py` (create)

**Interfaces:**
- Produces: `BgpPeer` rows minted from each switch's effective `bgp_config` and each gateway's materialized `bgp_config`, with collision→`ambiguous`, non-literal-IP→`unresolved`, and templated tokens carried.
- Consumes: `ctx.device_effective`, `ctx.site_effective`, `ctx.builder.add_bgp_peer`; `Device.role` via `_ROLE`; the raw `dev` dict (gateway materialized config).

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/ingest/test_bgp_ingest.py
from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.ir import DeviceRole


def _peers(ir):
    return {p.neighbor_ip: p for p in ir.bgp_peers}


def test_switch_bgp_minted_from_effective(make_raw_switch):
    # make_raw_switch: a fixture/helper building a RawSiteState with one switch whose
    # effective config has bgp_config. (Build inline if no shared helper exists.)
    raw = make_raw_switch(bgp_config={
        "underlay": {"type": "external", "local_as": 65000,
                     "neighbors": {"10.0.0.2": {"neighbor_as": 65001},
                                   "10.0.0.3": {"neighbor_as": 65002, "disabled": True}}}})
    ir = MistAdapter().ingest(raw).ir
    peers = _peers(ir)
    assert peers["10.0.0.2"].role is DeviceRole.SWITCH
    assert peers["10.0.0.2"].local_as == 65000 and peers["10.0.0.2"].neighbor_as == 65001
    assert peers["10.0.0.2"].session_type == "external"
    assert peers["10.0.0.3"].disabled is True


def test_templated_tokens_carried_not_collapsed(make_raw_switch):
    raw = make_raw_switch(bgp_config={
        "s": {"type": "{{kind}}", "local_as": "{{asn}}",
              "neighbors": {"10.0.0.2": {"neighbor_as": "{{peer_asn}}", "disabled": "{{flag}}"}}}})
    p = _peers(MistAdapter().ingest(raw).ir)["10.0.0.2"]
    assert p.session_type is None and p.session_type_unresolved == "{{kind}}"
    assert p.local_as is None and p.local_as_unresolved == "{{asn}}"
    assert p.neighbor_as is None and p.neighbor_as_unresolved == "{{peer_asn}}"
    assert p.disabled is False and p.disabled_unresolved == "{{flag}}"


def test_non_literal_neighbor_ip_is_unresolved(make_raw_switch):
    raw = make_raw_switch(bgp_config={
        "s": {"type": "external", "local_as": 65000,
              "neighbors": {"{{peer}}": {"neighbor_as": 65001}}}})
    peers = _peers(MistAdapter().ingest(raw).ir)
    assert peers["{{peer}}"].unresolved is True


def test_same_neighbor_two_sessions_differing_attrs_is_ambiguous(make_raw_switch):
    raw = make_raw_switch(bgp_config={
        "a": {"type": "external", "local_as": 65000, "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}},
        "b": {"type": "internal", "local_as": 65000, "neighbors": {"10.0.0.2": {"neighbor_as": 65000}}}})
    peers = _peers(MistAdapter().ingest(raw).ir)
    assert peers["10.0.0.2"].ambiguous is True  # one peer, marked ambiguous, NOT last-win


def test_gateway_bgp_minted_from_materialized_config(make_raw_gateway):
    # make_raw_gateway: builds a RawSiteState with a gateway device + a gatewaytemplate
    # whose gateway_effective bgp_config has `via`.
    raw = make_raw_gateway(bgp_config={
        "wan": {"type": "external", "local_as": 65000, "via": "wan",
                "neighbors": {"203.0.113.1": {"neighbor_as": 65010}}}})
    p = _peers(MistAdapter().ingest(raw).ir)["203.0.113.1"]
    assert p.role is DeviceRole.GATEWAY and p.via == "wan"
```

If no `make_raw_switch`/`make_raw_gateway` helpers exist, build the `RawSiteState` inline in the test using the existing patterns from `tests/adapters/mist/ingest/` (grep for a sibling ingest test that constructs `RawSiteState` with one device + effective config, and copy its scaffolding). Keep the helpers local to this test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/adapters/mist/ingest/test_bgp_ingest.py -q`
Expected: FAIL (no `bgp_peers` minted → KeyError on `peers["10.0.0.2"]`).

- [ ] **Step 3: Materialize gateway bgp_config**

In `src/digital_twin/adapters/mist/adapter.py`, `_materialize` (~line 85), add `"bgp_config"` to the keys tuple:
```python
        for key in ("port_config", "ip_configs", "dhcpd_config", "bgp_config"):
```
(Confirm the exact current tuple literal at line ~85 and add `"bgp_config"` to it.)

- [ ] **Step 4: Write the `_bgp` pass + helpers**

In `src/digital_twin/adapters/mist/ingest/switch.py`, add helpers near `_metric_int` (module-level or static):
```python
import ipaddress

_BGP_TYPES = frozenset({"external", "internal"})
_BGP_VIAS = frozenset({"lan", "tunnel", "vpn", "wan"})


def _bgp_as_int(v: Any) -> tuple[int | None, str | None]:
    """(parsed, unresolved_token). absent -> (None, None); present-unparseable -> (None, raw)."""
    if v is None:
        return None, None
    try:
        return int(v), None
    except (TypeError, ValueError):
        return None, str(v)


def _bgp_enum(v: Any, allowed: frozenset[str]) -> tuple[str | None, str | None]:
    if v is None:
        return None, None
    s = str(v)
    return (s, None) if s in allowed else (None, s)


def _bgp_disabled(v: Any) -> tuple[bool, str | None]:
    if v is None:
        return False, None                # schema default False
    if isinstance(v, bool):
        return v, None
    return False, str(v)                  # templated/non-bool -> unresolved token


def _is_literal_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False
```

Add the role-aware pass (mirroring `_ospf` at line ~765). Collisions are resolved WITHIN the pass (a dict keyed by neighbor_ip with an ambiguity flag), because `add_bgp_peer` raises on a duplicate id:
```python
    def _bgp(self, ctx: IngestContext, dev: Mapping[str, Any], role: DeviceRole) -> None:
        """GS28: switch/gateway BGP peerings. Switch reads device/site effective
        config; gateway reads its materialized bgp_config off the raw device. One
        BgpPeer per (device, neighbor_ip) — a neighbor IP claimed by 2+ sessions
        with differing modeled attrs is marked ambiguous (never last-win)."""
        did = device_id(str(dev["mac"]))
        if role is DeviceRole.GATEWAY:
            bgp = dev.get("bgp_config") or {}
        else:
            eff = ctx.device_effective.get(did) or ctx.site_effective
            bgp = eff.get("bgp_config") or {}
        if not bgp:
            return
        # accumulate per neighbor_ip; detect cross-session ambiguity
        chosen: dict[str, BgpPeer] = {}
        ambiguous: set[str] = set()
        for sname, scfg in bgp.items():
            scfg = scfg or {}
            local_as, local_as_unresolved = _bgp_as_int(scfg.get("local_as"))
            stype, stype_unresolved = _bgp_enum(scfg.get("type"), _BGP_TYPES)
            via, via_unresolved = _bgp_enum(scfg.get("via"), _BGP_VIAS)
            for nip, ncfg in (scfg.get("neighbors") or {}).items():
                ncfg = ncfg or {}
                nas, nas_unresolved = _bgp_as_int(ncfg.get("neighbor_as"))
                disabled, disabled_unresolved = _bgp_disabled(ncfg.get("disabled"))
                key = str(nip)
                peer = BgpPeer(
                    device_id=did, role=role, session_name=str(sname), neighbor_ip=key,
                    local_as=local_as, neighbor_as=nas, session_type=stype,
                    disabled=disabled, via=via,
                    local_as_unresolved=local_as_unresolved,
                    neighbor_as_unresolved=nas_unresolved,
                    session_type_unresolved=stype_unresolved,
                    via_unresolved=via_unresolved,
                    disabled_unresolved=disabled_unresolved,
                    unresolved=not _is_literal_ip(key),
                )
                if key in chosen and _bgp_attrs(chosen[key]) != _bgp_attrs(peer):
                    ambiguous.add(key)     # differing modeled attrs -> ambiguous
                else:
                    chosen[key] = peer
        for key, peer in chosen.items():
            if key in ambiguous:
                peer = replace(peer, ambiguous=True)
            ctx.builder.add_bgp_peer(peer)
```
Add the module-level attr-key helper (the ambiguity comparison spans ALL modeled attrs incl. admin-state + every unresolved token) and ensure `from dataclasses import replace` is imported:
```python
def _bgp_attrs(p: BgpPeer) -> tuple:
    return (p.local_as, p.local_as_unresolved, p.neighbor_as, p.neighbor_as_unresolved,
            p.session_type, p.session_type_unresolved, p.via, p.via_unresolved,
            p.disabled, p.disabled_unresolved)
```
Import `BgpPeer` and `DeviceRole` at the top of switch.py (it already imports `OspfIntf`, `device_id`; add `BgpPeer`, and `DeviceRole` if not present).

Dispatch in `ingest()` (line ~357) — call `_bgp` for BOTH roles:
```python
            if dev.get("type") == "switch":
                self._switch_ports_and_l3(ctx, dev)
                self._ospf(ctx, dev)
                self._bgp(ctx, dev, DeviceRole.SWITCH)
            elif dev.get("type") == "gateway":
                self._gateway_ports_and_l3(ctx, dev)
                self._bgp(ctx, dev, DeviceRole.GATEWAY)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/adapters/mist/ingest/test_bgp_ingest.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/ tests/adapters/mist/ingest/test_bgp_ingest.py
git commit -m "$(printf 'feat(gs28): role-aware _bgp ingest pass (switch+gateway) with collision/token handling\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 7: `wired.l3.bgp_adjacency` check — structural codes + coverage notes

**Files:**
- Create: `src/digital_twin/checks/wired/bgp_adjacency.py`
- Test: `tests/checks/wired/test_bgp_adjacency_structural.py` (create)

**Interfaces:**
- Produces: `BgpAdjacencyCheck` (`id="wired.l3.bgp_adjacency"`, `domain="wired.l3"`, `default_severity=Severity.ERROR`, `requires()==frozenset()`, `applies_to(diff)==diff.touches("bgp_peer")`). Six structural codes (all WARNING/REVIEW) + four coverage-note classes. Evidence on session-breaking findings carries `device`/`neighbor_ip` for Task 8's escalation.
- Consumes: `CheckContext` (`ctx.baseline.ir`, `ctx.proposed.ir`, `ctx.diff`, `ctx.delta_index`); `BgpPeer`; the contracts/types from `digital_twin.contracts` + `digital_twin.checks.base`.

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/wired/test_bgp_adjacency_structural.py
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.bgp_adjacency import BgpAdjacencyCheck
from digital_twin.contracts import Severity
from digital_twin.ir import BgpPeer, Device, DeviceRole, IRCapability
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder
from digital_twin.analysis.analysis_context import build_analysis_context  # adjust import to the real path


def _peer(nip="10.0.0.2", **kw):
    return BgpPeer(device_id="d1", role=DeviceRole.SWITCH, session_name="s", neighbor_ip=nip, **kw)


def _ir(peers, neighbors=None, caps=(IRCapability.WIRED_L2,)):
    b = IRBuilder().add_device(Device(id="d1", role=DeviceRole.SWITCH, site="x"))
    for c in caps:
        b.with_capability(c)
    for p in peers:
        b.add_bgp_peer(p)
    if neighbors is not None:
        b.set_bgp_neighbors(neighbors)
    return b.build()


def _run(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    ctx = CheckContext(baseline=build_analysis_context(base_ir),
                       proposed=build_analysis_context(prop_ir), diff=diff)
    return BgpAdjacencyCheck().run(ctx)


def _codes(res):
    return {f.code for f in res.findings}


def test_applies_only_to_bgp_peer_diff():
    base, prop = _ir([_peer()]), _ir([])
    assert BgpAdjacencyCheck().applies_to(diff_ir(base, prop)) is True
    same = _ir([_peer()])
    assert BgpAdjacencyCheck().applies_to(diff_ir(same, _ir([_peer()]))) is False


def test_peering_removed_is_review():
    res = _run(_ir([_peer()]), _ir([]))
    assert "wired.l3.bgp_adjacency.peering_removed" in _codes(res)
    assert res.status is Status.WARN
    assert all(f.severity is Severity.WARNING for f in res.findings)


def test_peering_disabled_is_review():
    res = _run(_ir([_peer(disabled=False)]), _ir([_peer(disabled=True)]))
    assert "wired.l3.bgp_adjacency.peering_disabled" in _codes(res)


def test_peering_added_is_review():
    res = _run(_ir([]), _ir([_peer()]))
    assert "wired.l3.bgp_adjacency.peering_added" in _codes(res)


def test_as_changed_carries_side_evidence_and_cofires_with_type():
    res = _run(_ir([_peer(neighbor_as=65001, session_type="external")]),
               _ir([_peer(neighbor_as=65002, session_type="internal")]))
    codes = _codes(res)
    assert "wired.l3.bgp_adjacency.as_changed" in codes
    assert "wired.l3.bgp_adjacency.session_type_changed" in codes
    as_f = next(f for f in res.findings if f.code.endswith(".as_changed"))
    assert as_f.evidence["neighbor_as_changed"] is True
    assert as_f.evidence["local_as_changed"] is False


def test_transport_changed_for_gateway():
    g = lambda **kw: BgpPeer(device_id="g1", role=DeviceRole.GATEWAY, session_name="s",
                             neighbor_ip="203.0.113.1", **kw)
    base = (IRBuilder().add_device(Device(id="g1", role=DeviceRole.GATEWAY, site="x"))
            .with_capability(IRCapability.WIRED_L2).add_bgp_peer(g(via="wan")).build())
    prop = (IRBuilder().add_device(Device(id="g1", role=DeviceRole.GATEWAY, site="x"))
            .with_capability(IRCapability.WIRED_L2).add_bgp_peer(g(via="tunnel")).build())
    assert "wired.l3.bgp_adjacency.transport_changed" in _codes(_run(base, prop))


def test_ambiguous_peer_is_a_note_not_a_finding():
    res = _run(_ir([_peer(ambiguous=True, neighbor_as=1)]),
               _ir([_peer(ambiguous=True, neighbor_as=2)]))
    assert not res.findings
    assert res.coverage.notes


def test_unresolved_type_change_is_note_not_confident_change():
    res = _run(_ir([_peer(session_type=None, session_type_unresolved=None)]),
               _ir([_peer(session_type=None, session_type_unresolved="{{t}}")]))
    assert "wired.l3.bgp_adjacency.session_type_changed" not in _codes(res)
    assert res.coverage.notes


def test_ambiguous_on_either_side_abstains():
    # baseline ambiguous -> proposed clean: must STILL abstain (no confident as_changed)
    res1 = _run(_ir([_peer(ambiguous=True, neighbor_as=65001)]),
                _ir([_peer(ambiguous=False, neighbor_as=65002)]))
    assert "wired.l3.bgp_adjacency.as_changed" not in _codes(res1)
    assert res1.coverage.notes
    # baseline clean -> proposed ambiguous: also abstain
    res2 = _run(_ir([_peer(ambiguous=False, neighbor_as=65001)]),
                _ir([_peer(ambiguous=True, neighbor_as=65002)]))
    assert "wired.l3.bgp_adjacency.as_changed" not in _codes(res2)
    assert res2.coverage.notes


def test_added_with_templated_local_as_is_note_not_confident_add():
    res = _run(_ir([]),
               _ir([_peer(local_as=None, local_as_unresolved="{{asn}}", neighbor_as=65001)]))
    assert "wired.l3.bgp_adjacency.peering_added" not in _codes(res)
    assert res.coverage.notes


def test_switch_via_diff_is_silent():
    # a via difference on a SWITCH peer must NOT emit .transport_changed (switches are LAN)
    res = _run(_ir([_peer(via="lan")]), _ir([_peer(via="wan")]))
    assert "wired.l3.bgp_adjacency.transport_changed" not in _codes(res)
    assert not res.findings
```
Confirm the real import path for building an `AnalysisContext` from an IR (the OSPF tests use the same harness — grep `tests/checks/wired/` for how an existing check test constructs `CheckContext`, and copy that exact helper/import).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/checks/wired/test_bgp_adjacency_structural.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Write the check (structural half)**

```python
# src/digital_twin/checks/wired/bgp_adjacency.py
"""wired.l3.bgp_adjacency — a switch/gateway BGP peering removed/disabled/added or
a retained peering's session-breaking attribute changed (GS28).

The twin has no RIB: structural codes are config-certain but reachability-unconfirmed
-> WARNING/REVIEW. Live telemetry (baseline-established peers) escalates a session-
breaking change to ERROR/UNSAFE (Task 8). Identity is (device, neighbor_ip); an
'active peering' = present AND not disabled. Codes:
- .peering_removed / .peering_disabled: active in baseline, gone/disabled in proposed.
- .peering_added: not-active in baseline, active in proposed (no escalation).
- .as_changed / .session_type_changed / .transport_changed: retained active peering
  whose local_as/neighbor_as / type / via changed.
Ambiguous, unresolved-IP, and templated-token (AS/type/via/admin-state) cases become
relevance-scoped PARTIAL coverage notes, never confident findings (never false-SAFE)."""

from __future__ import annotations

from typing import Any

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import (
    Finding,
    FindingCategory,
    FindingSource,
    ObjectRef,
    Severity,
)
from digital_twin.ir import (
    BgpNeighbor,
    BgpPeer,
    Capability,
    Confidence,
    ConfidenceLevel,
    DeviceRole,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=(
        "BGP reachability is not computed — a redundant peering or redistribution "
        "the twin does not model may still carry these routes",
    ),
)

# session-breaking codes (telemetry escalates these to UNSAFE in Task 8; added does NOT)
_SESSION_BREAKING = ("peering_removed", "peering_disabled", "as_changed",
                     "session_type_changed", "transport_changed")


def _active(p: BgpPeer) -> bool:
    return not p.disabled


def _by_key(ir: IR) -> dict[tuple[str, str], BgpPeer]:
    # identity (device, neighbor_ip); ingest guarantees one row per key (ambiguous flag set)
    return {(p.device_id, p.neighbor_ip): p for p in ir.bgp_peers}


class BgpAdjacencyCheck:
    id = "wired.l3.bgp_adjacency"
    title = "BGP peering withdrawn or session-breaking change"
    domain = "wired.l3"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset()

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("bgp_peer")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        base, prop = _by_key(base_ir), _by_key(prop_ir)
        findings: list[Finding] = []
        notes: list[str] = []

        def _caused_by(p: BgpPeer):
            return ctx.delta_index.causes("bgp_peer", [p.id])

        def _mk(code: str, p: BgpPeer, message: str, extra: dict[str, Any]) -> Finding:
            return Finding(
                source=FindingSource.CHECK, category=FindingCategory.NETWORK,
                code=f"{self.id}.{code}", subject=ObjectRef("device", p.device_id),
                severity=Severity.WARNING, confidence=_UNVERIFIED, message=message,
                affected_entities=(p.neighbor_ip,),
                evidence={"device": p.device_id, "neighbor_ip": p.neighbor_ip, **extra},
                caused_by=_caused_by(p),
            )

        def _note_if_fuzzy(did: str, nip: str, b: BgpPeer | None, p: BgpPeer | None) -> bool:
            """Emit a coverage note if EITHER side is ambiguous/unresolved, and return
            True so the caller skips confident compare AND telemetry escalation on this
            peer. Checking both sides is load-bearing: a baseline-ambiguous peer that
            becomes clean (or vice-versa) must still abstain (spec §2)."""
            if (b is not None and b.ambiguous) or (p is not None and p.ambiguous):
                notes.append(f"BGP peer {nip} on {did} is claimed by multiple sessions with "
                             "differing attributes — change detection skipped")
                return True
            if (b is not None and b.unresolved) or (p is not None and p.unresolved):
                notes.append(f"BGP peer key {nip!r} on {did} is not a literal IP — peering "
                             "change impact cannot be verified")
                return True
            return False

        all_keys = sorted(set(base) | set(prop))
        for key in all_keys:
            b, p = base.get(key), prop.get(key)
            did, nip = key
            if _note_if_fuzzy(did, nip, b, p):
                continue
            b_active = b is not None and _active(b)
            p_active = p is not None and _active(p)

            # removed / disabled (active -> not active)
            if b_active and not p_active:
                if p is None:
                    findings.append(_mk("peering_removed", b,
                        f"BGP peering to {nip} on {did} is removed — the session is "
                        "withdrawn; routes learned/advertised over it are lost", {}))
                else:
                    findings.append(_mk("peering_disabled", p,
                        f"BGP peering to {nip} on {did} is administratively disabled — "
                        "the session goes down", {}))
                continue
            # added / enabled (not active -> active)
            if not b_active and p_active:
                # require literal identity + resolved ASN (BOTH local and neighbor) for a
                # confident add — else a coverage note, never a confident .peering_added
                if p.neighbor_as_unresolved is not None or p.local_as_unresolved is not None:
                    notes.append(f"BGP peer {nip} on {did} added with a templated AS "
                                 "— new-peering details unverifiable")
                    continue
                findings.append(_mk("peering_added", p,
                    f"BGP peering to {nip} on {did} is newly added — a new session shifts "
                    "advertised/learned routes; review intended scope", {}))
                continue
            # retained active peering: compare session-breaking attributes
            if b_active and p_active:
                assert b is not None and p is not None
                # AS (with unresolved-token guard)
                if b.local_as_unresolved != p.local_as_unresolved or \
                        b.neighbor_as_unresolved != p.neighbor_as_unresolved:
                    notes.append(f"BGP peer {nip} on {did} has a templated AS on one side "
                                 "— AS-change impact unverifiable")
                else:
                    local_changed = b.local_as != p.local_as
                    neighbor_changed = b.neighbor_as != p.neighbor_as
                    if local_changed or neighbor_changed:
                        findings.append(_mk("as_changed", p,
                            f"BGP peering to {nip} on {did} changed AS (local "
                            f"{b.local_as}->{p.local_as}, neighbor {b.neighbor_as}->"
                            f"{p.neighbor_as}) — the session must re-establish",
                            {"local_as_changed": local_changed,
                             "neighbor_as_changed": neighbor_changed,
                             "base_local_as": b.local_as, "proposed_local_as": p.local_as,
                             "base_neighbor_as": b.neighbor_as,
                             "proposed_neighbor_as": p.neighbor_as}))
                # session type
                if b.session_type_unresolved != p.session_type_unresolved:
                    notes.append(f"BGP peer {nip} on {did} has a templated session type on "
                                 "one side — type-change impact unverifiable")
                elif b.session_type != p.session_type:
                    findings.append(_mk("session_type_changed", p,
                        f"BGP peering to {nip} on {did} changed type {b.session_type}->"
                        f"{p.session_type} (iBGP/eBGP) — the session must re-establish",
                        {"base_type": b.session_type, "proposed_type": p.session_type}))
                # transport (gateway via) — role-gated: switches are implicitly LAN and
                # have no transport dimension; never rely on field-gate invariants in a
                # pure check (p.role == b.role, same device).
                if p.role is DeviceRole.GATEWAY:
                    if b.via_unresolved != p.via_unresolved:
                        notes.append(f"BGP peer {nip} on {did} has a templated transport on "
                                     "one side — transport-change impact unverifiable")
                    elif b.via != p.via:
                        findings.append(_mk("transport_changed", p,
                            f"BGP peering to {nip} on {did} changed transport {b.via}->{p.via} "
                            "— the session path changed",
                            {"base_via": b.via, "proposed_via": p.via}))
                # admin-state token (disabled templated on one side, value otherwise equal)
                if b.disabled_unresolved != p.disabled_unresolved:
                    notes.append(f"BGP peer {nip} on {did} has a templated admin-state on "
                                 "one side — enable/disable impact unverifiable")

        return self._finish(findings, notes)

    def _finish(self, findings: list[Finding], notes: list[str]) -> CheckResult:
        worst = Status.PASS
        for f in findings:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id, status=worst, findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(min_confidence(*(f.confidence for f in findings)) if findings else _HIGH),
            reasoning="compared per-(device, neighbor_ip) BGP peerings, baseline vs proposed",
        )
```
Note: `_SESSION_BREAKING`, `BgpNeighbor`, and the telemetry import are placed now but consumed in Task 8 — if ruff flags an unused import/constant at this task, add `# noqa: F401`/use them in Task 8 immediately (Task 8 is the next commit). Prefer removing `BgpNeighbor`/`_SESSION_BREAKING` here and adding them in Task 8 to keep this task lint-clean.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/checks/wired/test_bgp_adjacency_structural.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/bgp_adjacency.py tests/checks/wired/test_bgp_adjacency_structural.py
git commit -m "$(printf 'feat(gs28): wired.l3.bgp_adjacency structural codes + coverage notes\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 8: telemetry escalation (baseline-established direct join) + telemetry notes

**Files:**
- Modify: `src/digital_twin/checks/wired/bgp_adjacency.py` (add the escalation post-pass + notes inside `run()`, before `_finish`)
- Test: `tests/checks/wired/test_bgp_adjacency_telemetry.py` (create)

**Interfaces:**
- Produces: session-breaking findings escalated to ERROR/UNSAFE/HIGH when their `(device, neighbor_ip)` is a baseline-established peer; `evidence["broken_peers"]` (always the single peer IP as a 1-list here, for parity with GS27 aggregation), `baseline_state`, `baseline_neighbor_as`, `vrf`; the telemetry-blind note and the established-peer-not-in-config note.
- Consumes: `base_ir.bgp_neighbors`, `IRCapability.BGP_TELEMETRY`, `bgp_telemetry_unparsed_count`.

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/wired/test_bgp_adjacency_telemetry.py
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.bgp_adjacency import BgpAdjacencyCheck, is_established
from digital_twin.contracts import Severity
from digital_twin.ir import BgpNeighbor, BgpPeer, Device, DeviceRole, IRCapability
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder
from digital_twin.analysis.analysis_context import build_analysis_context  # adjust to real path


def _peer(nip="10.0.0.2", **kw):
    return BgpPeer(device_id="d1", role=DeviceRole.SWITCH, session_name="s", neighbor_ip=nip, **kw)


def _ir(peers, neighbors=None, telemetry=False):
    b = IRBuilder().add_device(Device(id="d1", role=DeviceRole.SWITCH, site="x"))
    b.with_capability(IRCapability.WIRED_L2)
    if telemetry:
        b.with_capability(IRCapability.BGP_TELEMETRY)
    for p in peers:
        b.add_bgp_peer(p)
    if neighbors is not None:
        b.set_bgp_neighbors(neighbors)
    return b.build()


def _run(base_ir, prop_ir):
    diff = diff_ir(base_ir, prop_ir)
    return BgpAdjacencyCheck().run(CheckContext(
        baseline=build_analysis_context(base_ir),
        proposed=build_analysis_context(prop_ir), diff=diff))


def test_is_established_by_state_or_up_flag():
    assert is_established(BgpNeighbor(device_id="d1", peer_ip="x", state="Established"))
    assert is_established(BgpNeighbor(device_id="d1", peer_ip="x", state="", up=True))
    assert not is_established(BgpNeighbor(device_id="d1", peer_ip="x", state="Idle"))


def test_removed_established_peer_escalates_to_unsafe():
    base = _ir([_peer()],
               neighbors=[BgpNeighbor(device_id="d1", peer_ip="10.0.0.2", state="Established",
                                      neighbor_as=65001, vrf="default")],
               telemetry=True)
    prop = _ir([], neighbors=[], telemetry=True)
    res = _run(base, prop)
    f = next(f for f in res.findings if f.code.endswith(".peering_removed"))
    assert f.severity is Severity.ERROR and res.status is Status.FAIL
    assert f.evidence["broken_peers"] == ["10.0.0.2"]
    assert f.evidence["baseline_neighbor_as"] == 65001 and f.evidence["vrf"] == "default"


def test_up_flag_only_peer_escalates():
    base = _ir([_peer()],
               neighbors=[BgpNeighbor(device_id="d1", peer_ip="10.0.0.2", state="", up=True)],
               telemetry=True)
    prop = _ir([], neighbors=[], telemetry=True)
    assert _run(base, prop).status is Status.FAIL


def test_added_peer_does_not_escalate():
    base = _ir([], neighbors=[], telemetry=True)
    prop = _ir([_peer()],
               neighbors=[BgpNeighbor(device_id="d1", peer_ip="10.0.0.2", state="Established")],
               telemetry=True)
    res = _run(base, prop)
    assert all(f.severity is Severity.WARNING for f in res.findings)


def test_telemetry_blind_note_when_no_capability_and_session_breaking():
    res = _run(_ir([_peer()]), _ir([]))   # no BGP_TELEMETRY
    assert any("telemetry unavailable" in n.lower() for n in res.coverage.notes)


def test_established_peer_not_in_config_note():
    # a live peer with no config BgpPeer on a delta-touched device -> note
    base = _ir([_peer("10.0.0.2")],
               neighbors=[BgpNeighbor(device_id="d1", peer_ip="10.0.0.9", state="Established")],
               telemetry=True)
    prop = _ir([], neighbors=[BgpNeighbor(device_id="d1", peer_ip="10.0.0.9", state="Established")],
               telemetry=True)
    res = _run(base, prop)
    assert any("not found in" in n.lower() or "not modeled" in n.lower()
               for n in res.coverage.notes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/checks/wired/test_bgp_adjacency_telemetry.py -q`
Expected: FAIL (`cannot import name 'is_established'`).

- [ ] **Step 3: Add `is_established` + the escalation post-pass**

In `src/digital_twin/checks/wired/bgp_adjacency.py`, add module-level:
```python
def is_established(n: BgpNeighbor) -> bool:
    return n.up is True or n.state.strip().lower() == "established"
```
Keep the `_SESSION_BREAKING` constant (now used). In `run()`, AFTER the structural loop and BEFORE `return self._finish(...)`, insert:
```python
        # Telemetry escalation (escalate-only; BASELINE telemetry). The structural
        # findings ARE the breaks; baseline telemetry confirms which were live.
        telemetry_known = IRCapability.BGP_TELEMETRY in base_ir.capabilities
        has_unparsed = base_ir.bgp_telemetry_unparsed_count > 0
        session_breaking_codes = frozenset(f"{self.id}.{c}" for c in _SESSION_BREAKING)
        has_session_breaking = any(f.code in session_breaking_codes for f in findings)

        if telemetry_known:
            established = {
                (n.device_id, n.peer_ip): n for n in base_ir.bgp_neighbors if is_established(n)
            }
            for i, f in enumerate(findings):
                if f.code not in session_breaking_codes:
                    continue
                ev = f.evidence or {}
                key = (ev.get("device"), ev.get("neighbor_ip"))
                n = established.get(key)
                if n is None:
                    continue
                findings[i] = Finding(
                    source=f.source, category=f.category, code=f.code, subject=f.subject,
                    severity=Severity.ERROR, confidence=_HIGH,
                    message=(f"{f.message} | telemetry: this peer was ESTABLISHED in baseline "
                             "— this change is session-breaking, so the peering would drop"),
                    affected_entities=f.affected_entities,
                    evidence={**ev, "broken_peers": [n.peer_ip], "baseline_state": n.state,
                              "baseline_neighbor_as": n.neighbor_as, "vrf": n.vrf},
                    caused_by=f.caused_by,
                )
            # established live peer with no config BgpPeer, on a delta-touched device -> note
            touched_devices = {
                r.id.split(":")[0]
                for r in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
                if r.kind == "bgp_peer"
            }
            config_keys = set(base) | set(prop)
            for (did, pip), n in established.items():
                if did in touched_devices and (did, pip) not in config_keys:
                    notes.append(f"BGP peer {pip} on {did} is established in telemetry but "
                                 "not found in the modeled config — the twin is blind for it")

        # telemetry-blind note: only when a session-breaking finding exists (adds excluded)
        if (not telemetry_known or has_unparsed) and has_session_breaking:
            notes.append("BGP neighbor telemetry unavailable/partial — confirmed-break "
                         "detection is blind for the changed peering(s)")
```
Adjust the `return` to remain `return self._finish(findings, notes)`. (The structural loop and `_finish` are unchanged from Task 7.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/checks/wired/test_bgp_adjacency_telemetry.py tests/checks/wired/test_bgp_adjacency_structural.py -q`
Expected: PASS (both files).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/bgp_adjacency.py tests/checks/wired/test_bgp_adjacency_telemetry.py
git commit -m "$(printf 'feat(gs28): bgp_adjacency telemetry escalation (baseline-established join) + notes\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 9: register the check + public-API count

**Files:**
- Modify: `src/digital_twin/checks/wired/__init__.py` (import; `ALL_WIRED_CHECKS`; `__all__`)
- Modify: `tests/test_public_api.py` (count assertion line ~185; add registration test ~line 250)
- Test: `tests/test_public_api.py`

**Interfaces:**
- Produces: `BgpAdjacencyCheck` in `ALL_WIRED_CHECKS` (count 20); `wired.l3.bgp_adjacency` discoverable.

- [ ] **Step 1: Write the failing test**

Edit `tests/test_public_api.py`: change `assert len(ALL_WIRED_CHECKS) == 19` to `== 20`, and add near the OSPF registration test (~line 250):
```python
def test_bgp_adjacency_is_registered():
    from digital_twin.checks.wired import ALL_WIRED_CHECKS

    assert any(c.id == "wired.l3.bgp_adjacency" for c in ALL_WIRED_CHECKS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_public_api.py -q -k "public_api or bgp_adjacency_is_registered"`
Expected: FAIL (count is 19; registration test errors).

- [ ] **Step 3: Register the check**

In `src/digital_twin/checks/wired/__init__.py`: add `from .bgp_adjacency import BgpAdjacencyCheck` (alphabetically near the top), append `BgpAdjacencyCheck(),` to `ALL_WIRED_CHECKS` (e.g. right after `OspfWithdrawalCheck()`), and add `"BgpAdjacencyCheck"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_public_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/__init__.py tests/test_public_api.py
git commit -m "$(printf 'feat(gs28): register wired.l3.bgp_adjacency (public-api count 19->20)\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 10: golden scenarios + real `org_bgp`-shape grounding

**Files:**
- Modify: `tests/golden/builders.py` (add `bgp_minimal_doc`, `bgp_op`, `bgp_gateway_doc` near the OSPF builders ~line 492)
- Modify: `tests/golden/test_golden_scenarios.py` (add BGP scenarios near the GS27 OSPF goldens ~line 1144)
- Test: `tests/golden/test_golden_scenarios.py`

**Interfaces:**
- Consumes: `_simulate`/`_simulate_org`, `write_doc`, `plan_for`, `FixtureProvider`, `Decision`, `Severity`; the GS27 builder patterns (`ospf_minimal_doc` hand-builds a synthetic single-device doc incl. `meta.fetched`).

- [ ] **Step 1: Write the failing goldens**

In `tests/golden/test_golden_scenarios.py`, add (mirroring the GS27 minimal-doc goldens):
```python
def test_gs28_switch_peering_removed_with_live_peer_is_unsafe(tmp_path):
    doc = bgp_minimal_doc(
        {"underlay": {"type": "external", "local_as": 65000,
                      "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}},
        bgp_neighbors=[{"mac": GS28_HUB_MAC, "peer_ip": "10.0.0.2", "state": "Established"}],
    )
    op = bgp_op(doc, {"underlay": {"type": "external", "local_as": 65000, "neighbors": {}}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    removed = [f for f in v.findings if f.code == "wired.l3.bgp_adjacency.peering_removed"]
    assert removed and any(f.severity is Severity.ERROR for f in removed)


def test_gs28_as_change_is_review(tmp_path):
    doc = bgp_minimal_doc({"underlay": {"type": "external", "local_as": 65000,
                                        "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}})
    op = bgp_op(doc, {"underlay": {"type": "external", "local_as": 65000,
                                   "neighbors": {"10.0.0.2": {"neighbor_as": 65099}}}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wired.l3.bgp_adjacency.as_changed" in {f.code for f in v.findings}


def test_gs28_auth_key_edit_is_unknown(tmp_path):
    doc = bgp_minimal_doc({"underlay": {"type": "external", "local_as": 65000,
                                        "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}})
    op = bgp_op(doc, {"underlay": {"type": "external", "local_as": 65000, "auth_key": "s3cret",
                                   "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNKNOWN, v.decision_reasons


def test_gs28_networks_edit_is_unknown(tmp_path):
    doc = bgp_minimal_doc({"underlay": {"type": "external", "local_as": 65000,
                                        "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}})
    op = bgp_op(doc, {"underlay": {"type": "external", "local_as": 65000,
                                   "networks": ["corp-net"],
                                   "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNKNOWN, v.decision_reasons


def test_gs28_gateway_only_bgp_via_gatewaytemplate(tmp_path):
    # proves gateway peers are minted from gateway_effective, end to end
    doc, plan = bgp_gateway_scenario(
        base={"wan": {"type": "external", "local_as": 65000, "via": "wan",
                      "neighbors": {"203.0.113.1": {"neighbor_as": 65010}}}},
        proposed={"wan": {"type": "external", "local_as": 65000, "via": "wan", "neighbors": {}}},
    )
    v = _simulate_org(doc, plan, tmp_path)
    assert "wired.l3.bgp_adjacency.peering_removed" in {f.code for f in v.findings}, v.findings
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/golden/test_golden_scenarios.py -q -k gs28`
Expected: FAIL (`bgp_minimal_doc` undefined).

- [ ] **Step 3: Add the builders**

In `tests/golden/builders.py`, near `ospf_minimal_doc` (~line 492), add `GS28_*` ids and:
```python
GS28_ORG_ID = "org-gs28-tests"
GS28_SITE_ID = "site-gs28-tests"
GS28_HUB_MAC = "aa0028000001"
GS28_HUB_ID = "sw-gs28-hub"


def bgp_minimal_doc(bgp_config, *, bgp_neighbors=None):
    """Synthetic single-switch site whose effective setting carries bgp_config (mirrors
    ospf_minimal_doc). Earns BGP_TELEMETRY only when bgp_neighbors is provided."""
    fetched = ["devices", "setting", "site"]
    if bgp_neighbors is not None:
        fetched.append("bgp_neighbors")
    return {
        "scope": {"org_id": GS28_ORG_ID, "site_id": GS28_SITE_ID},
        "site": {"id": GS28_SITE_ID, "name": "gs28"},
        "setting": {"bgp_config": bgp_config, "networks": {}},
        "networktemplate": None,
        "devices": [{"mac": GS28_HUB_MAC, "id": GS28_HUB_ID, "type": "switch",
                     "name": "hub", "bgp_config": bgp_config}],
        "device_stats": [], "port_stats": [], "wireless_clients": [], "wired_clients": [],
        "derived_setting": {"bgp_config": bgp_config, "networks": {}},
        "wlans": [], "org_networks": [], "sitetemplate": None, "gatewaytemplate": None,
        "nac_clients": [],
        "bgp_neighbors": list(bgp_neighbors or []),
        "meta": {"acquired_at": "2026-06-23T00:00:00+00:00", "host": "test",
                 "fetched": fetched, "failures": []},
    }


def bgp_op(doc, proposed_bgp_config, *, order=0):
    """Root-level device PUT touching ONLY bgp_config (avoids L2 reshape noise)."""
    dev = next(d for d in doc["devices"] if str(d.get("mac")) == GS28_HUB_MAC)
    return {"order": order, "object_type": "device", "object_id": dev["id"],
            "payload": {**dev, "bgp_config": proposed_bgp_config}}
```
For the gateway scenario, study the existing multi-site/org builders (`_ms_template`/`_ms_site_a`, `_simulate_org`/`simulate_org_template`) and add a `bgp_gateway_scenario(base, proposed)` that builds a doc with a gateway device + a `gatewaytemplate` carrying `bgp_config=base`, plus an org `gatewaytemplate` update op to `proposed`. Return `(doc, plan)`. Confirm the exact op/ChangePlan shape from an existing gatewaytemplate golden (grep `tests/golden/` for `gatewaytemplate`). The DEVICE-op forms (`bgp_op`) and the `_simulate` path must match the real `device`/`networktemplate`/`gatewaytemplate` envelope used by the OSPF goldens.

> If the exact field shape of `setting`/`derived_setting` or the device-op envelope differs from the snippet above (the GS27 `ospf_minimal_doc` is the authority), copy `ospf_minimal_doc`/`ospf_op` verbatim and swap `ospf_*` config for `bgp_config` — do NOT invent a divergent doc shape.

- [ ] **Step 4: Add the real-payload-shape grounding test**

Add a test pinning the ingester field mapping against a realistic record (the GS27 approach — update with the user's pasted `org_bgp` record when available):
```python
def test_gs28_bgp_neighbor_field_mapping_real_shape():
    from digital_twin.adapters.mist.ingest.bgp_neighbors import _row_to_neighbor
    # realistic org_bgp/site_bgp record shape (confirm/replace with a live paste)
    row = {"mac": "2093390f6200", "peer_ip": "10.3.172.3", "neighbor_as": 65001,
           "state": "Established", "vrf_name": "master", "up": True,
           "org_id": "x", "site_id": "y"}
    n = _row_to_neighbor(row)
    assert n is not None
    assert n.device_id == "2093390f6200" and n.peer_ip == "10.3.172.3"
    assert n.neighbor_as == 65001 and n.state == "Established" and n.up is True
    assert n.vrf == "master"
```

- [ ] **Step 5: Run goldens**

Run: `uv run pytest tests/golden/test_golden_scenarios.py -q -k gs28`
Expected: PASS (all GS28 scenarios). Debug envelope/shape mismatches against the GS27 goldens until green.

- [ ] **Step 6: Commit**

```bash
git add tests/golden/builders.py tests/golden/test_golden_scenarios.py
git commit -m "$(printf 'test(gs28): goldens (switch removed/UNSAFE, as_changed, auth_key/networks UNKNOWN, gateway-only) + field-shape grounding\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 11: full gate + live verify + roadmap/docs/memory

**Files:**
- Modify: `docs/ROADMAP.md` (GS28 bullet)
- Modify: `/Users/tmunzer/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md` (+ MEMORY.md pointer if needed)
- (No test file — this is the integration/wrap task)

- [ ] **Step 1: Run the full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: all pass, mypy clean on `src`. Fix any failures (ruff 100-col; mypy strict). Do NOT proceed until green.

- [ ] **Step 2: Live regression verify (READ-ONLY, simulate-only)**

Using the live provider against the test org (`9777c1a0-...`, Live-Demo Cupertino `978c48e6-...`) per the project's read-only live convention: run a no-op/benign simulate and confirm (a) the `bgp_neighbors` fetch is clean (empty in TM-LAB → `BGP_TELEMETRY` earned, zero peers; ingest stays `ok=True`), (b) `state_meta.fetched` now lists `bgp_neighbors`, (c) no spurious BGP findings on an unrelated plan. If the org carries switch/gateway `bgp_config`, confirm `BgpPeer` minting on real config. Record the outcome (and any `site_bgp` 404 behavior, mirroring the GS27 `site_ospf` note) in the roadmap/docs. Capture the exact commands + observed result.

- [ ] **Step 3: Update the roadmap**

In `docs/ROADMAP.md`, mark GS28 ✅ (BGP adjacency break — switch + gateway, structural codes + escalate-only telemetry), remove the stale OAS-refresh NOTE (resolved: bgp_config already in committed OAS / L0 permissive), and list deferred follow-ups: advertised-prefix (`networks`) BGP checking; auth_key-bearing-neighbor-removal sharpness; VRF-scoped peer identity; gateway device-op BGP (still UNKNOWN); full live-simulate against a BGP-bearing org.

- [ ] **Step 4: Update memory**

In `digital-twin-project.md`, add a GS28 bullet (BGP adjacency break: `BgpPeer`/`BgpNeighbor`, `wired.l3.bgp_adjacency`, `BGP_TELEMETRY`, direct established-peer-IP escalation, switch+gateway via `_materialize` bgp_config). Note the GS27 telemetry template was reused. Verify against the memory instructions (absolute dates, no secrets).

- [ ] **Step 5: Commit**

```bash
git add docs/ROADMAP.md
git commit -m "$(printf 'docs(gs28): roadmap done + live regression verify; deferred follow-ups\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```
(The memory file lives outside the repo — write it but it is not part of the commit.)

---

## Self-Review notes (author)

- **Spec coverage:** §1 entity/ingest/allowlist → T1/T2/T3/T6; §2 six structural codes + four notes → T7; §3 telemetry (`BgpNeighbor`, `is_established` with `up`, direct baseline-established escalation, aggregation evidence, two notes, no `.peer_unreachable`) → T2/T5/T8; §4 applies_to/requires/verdict/L0/testing/live → T7/T8/T3/T10/T11. Gateway-only golden → T10. All spec sections map to a task.
- **Diff-bearing unresolved tokens** (`*_as_unresolved`, `session_type_unresolved`, `via_unresolved`, `disabled_unresolved`) defined in T1, parsed in T6, consumed as notes in T7 — names consistent across tasks.
- **`ambiguous`** set in T6 (collision-preserving, never last-win), consumed in T7.
- **Escalation uses baseline telemetry only** and is gated on `IRCapability.BGP_TELEMETRY in base_ir.capabilities` (T8) — matches the spec's "baseline-only" rule.
- **Type consistency:** `add_bgp_peer`, `set_bgp_neighbors`, `IR.bgp_peers`, `IR.bgp_neighbors`, `IR.bgp_telemetry_unparsed_count`, `is_established(n)`, `_SESSION_BREAKING` used identically wherever referenced.
- **Open verification deferred to implementer (flagged in-task):** exact `mistapi` BGP-stats SDK call name (T4 Step 3); the precise `build_analysis_context`/CheckContext test harness import (T7/T8 — copy from an existing wired check test); the gatewaytemplate golden envelope (T10 — copy from an existing gatewaytemplate golden). Each is called out with a grep target.
