# GS26 — OSPF exit withdrawal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when a config change structurally withdraws a switch's OSPF participation for a routed segment (network dropped from all areas, area removed, or `ospf_config.enabled → false`) and floor the verdict honestly (base REVIEW; UNSAFE only when the device's last active adjacency collapses and an affected segment has observed clients).

**Architecture:** A new frozen `OspfIntf` IR entity is minted by a switch-only `_ospf` ingest pass; a new `wired.l3.ospf_withdrawal` check compares per-`(device, vlan)` semantic participation across baseline/proposed and emits three codes (`.egress_lost`, `.advertised_removed`, `.transit_mutation`). The allowlist exposes only `ospf_config.enabled` + `ospf_areas.*.networks.*.passive`; the compiler is fixed to carry device-level `ospf_*`.

**Tech Stack:** Python 3.14, uv, pytest, dataclasses (frozen IR), the existing Mist adapter/ingest/checks layering. Gate after every task: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

**Spec:** `docs/superpowers/specs/2026-06-13-gs26-ospf-exit-withdrawal-design.md`

---

## File Structure

- **Create** `tests/checks/test_ospf_withdrawal.py` — unit tests for the new check.
- **Create** `src/digital_twin/checks/wired/ospf_withdrawal.py` — the `wired.l3.ospf_withdrawal` check.
- **Modify** `src/digital_twin/ir/entities.py` — add `OspfIntf`.
- **Modify** `src/digital_twin/ir/__init__.py` — export `OspfIntf`.
- **Modify** `src/digital_twin/ir/model.py` — `IR.ospf_intfs`, `IRBuilder.add_ospf_intf`, `_validate_ospf_intfs`, `build()`.
- **Modify** `src/digital_twin/ir/diff.py` — register the `ospf_intf` entity kind.
- **Modify** `src/digital_twin/adapters/mist/compile/switch.py` — carry `ospf_config`/`ospf_areas`.
- **Modify** `src/digital_twin/scope/allowlist.py` — `_OSPF_LEAVES`.
- **Modify** `src/digital_twin/adapters/mist/ingest/switch.py` — the `_ospf` pass.
- **Modify** `src/digital_twin/checks/wired/__init__.py` — register the check.
- **Modify** `tests/factories.py` — an `ospf(...)` builder.
- **Modify** `tests/golden/builders.py` — OSPF doc/op builders.
- **Modify** `tests/golden/test_golden_scenarios.py` — GS26 a–e.
- **Modify** `tests/adapters/mist/test_ingest_switch.py`, `tests/scope/test_field_gate.py` (or the existing allowlist/field-gate test module), `tests/ir/test_diff.py` — unit coverage.
- **Modify** `docs/ROADMAP.md` and the memory file — wrap-up.

---

## Task 1: `OspfIntf` IR entity, builder, validation, diff registration

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (after `L3Intf`, ~line 246)
- Modify: `src/digital_twin/ir/__init__.py`
- Modify: `src/digital_twin/ir/model.py`
- Modify: `src/digital_twin/ir/diff.py:21-29`
- Test: `tests/ir/test_model_ospf.py` (create), `tests/ir/test_diff.py`

- [ ] **Step 1: Write the failing entity + builder test**

Create `tests/ir/test_model_ospf.py`:

```python
"""OspfIntf entity + IRBuilder wiring + role-aware validation (GS26)."""

import pytest

from digital_twin.ir import IRBuilder, IRValidationError
from digital_twin.ir.entities import Device, DeviceRole, OspfIntf, Vlan


def _switch(did="S"):
    return Device(id=did, role=DeviceRole.SWITCH, site="s1")


def test_id_is_derived_from_device_area_and_name():
    o = OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp")
    assert o.id == "S:ospf:0:corp"
    assert o.passive is False and o.unresolved is False


def test_builder_adds_and_build_exposes_ospf_intfs():
    ir = (
        IRBuilder()
        .add_device(_switch())
        .add_vlan(Vlan(vlan_id=10, name="corp"))
        .add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))
        .build()
    )
    assert len(ir.ospf_intfs) == 1
    assert ir.ospf_intfs[0].id == "S:ospf:0:corp"


def test_duplicate_ospf_intf_id_rejected():
    b = IRBuilder().add_device(_switch()).add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="duplicate ospf"):
        b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))


def test_validation_rejects_unknown_device():
    b = IRBuilder().add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="GHOST", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="unknown device"):
        b.build()


def test_validation_rejects_non_switch_device():
    b = IRBuilder().add_device(Device(id="GW", role=DeviceRole.GATEWAY, site="s1"))
    b.add_vlan(Vlan(vlan_id=10))
    b.add_ospf_intf(OspfIntf(device_id="GW", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="is not a switch"):
        b.build()


def test_validation_rejects_resolved_vlan_not_minted():
    b = IRBuilder().add_device(_switch())
    b.add_ospf_intf(OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp"))
    with pytest.raises(IRValidationError, match="unknown vlan"):
        b.build()


def test_validation_rejects_unresolved_invariant_violation():
    b = IRBuilder().add_device(_switch()).add_vlan(Vlan(vlan_id=10))
    # unresolved must carry NO vlan_id
    b.add_ospf_intf(
        OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp", unresolved=True)
    )
    with pytest.raises(IRValidationError, match="unresolved"):
        b.build()


def test_unresolved_row_with_none_vlan_is_valid():
    ir = (
        IRBuilder()
        .add_device(_switch())
        .add_ospf_intf(
            OspfIntf(device_id="S", vlan_id=None, area="0", network_name="ghost", unresolved=True)
        )
        .build()
    )
    assert ir.ospf_intfs[0].unresolved is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/ir/test_model_ospf.py -q`
Expected: FAIL (`ImportError: cannot import name 'OspfIntf'`).

- [ ] **Step 3: Add the entity**

In `src/digital_twin/ir/entities.py`, after the `L3Intf` class (after line 245), add:

```python
@dataclass(frozen=True)
class OspfIntf:
    """A switch network's OSPF-area participation (GS26 withdrawal surface).

    Minted only when ospf_config.enabled is truthy. `passive=False` (the Mist
    default) means the interface forms adjacencies = adjacency-bearing
    (transit/uplink); `passive=True` is advertise-only (stub). `unresolved=True`
    is the OSPF analog of vlan-blind carriage — the network name did not resolve
    to a vlan (then vlan_id is None). Identity carries area+network_name for
    stability and messaging, but the ospf_withdrawal check NEVER compares by id:
    it reduces participation to the semantic (device, vlan[, area, active]) tuple
    so a rename/area-move is not a false withdrawal.
    """

    device_id: str
    vlan_id: int | None = None
    area: str = "0"
    network_name: str = ""
    passive: bool = False
    unresolved: bool = False
    meta: FactMeta = CONFIG_META
    id: str = ""  # auto-derived in __post_init__ if empty

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(
                self, "id", f"{self.device_id}:ospf:{self.area}:{self.network_name}"
            )
```

- [ ] **Step 4: Export it**

In `src/digital_twin/ir/__init__.py`: add `OspfIntf` to the `from .entities import (...)` block (alphabetically near `L3Role`) and to `__all__` (after `"L3Role",`).

- [ ] **Step 5: Wire the builder, IR field, validation, and build()**

In `src/digital_twin/ir/model.py`:

1. Import: add `OspfIntf` to the entities import line (wherever `L3Intf` is imported).
2. On the `IR` dataclass, add the field at the **end of the defaulted block** (right after `ap_wlan_unresolved`, line 58) — it must come after `clients` (which has no default) to keep dataclass field ordering valid:

```python
    ospf_intfs: tuple[OspfIntf, ...] = ()
```

(The `build()` call passes it by keyword, so its position relative to `l3intfs` there does not matter.)

3. In `IRBuilder.__init__` (after line 78, the l3intf lists), add:

```python
        self._ospf_intfs: list[OspfIntf] = []
        self._ospf_intf_ids: set[str] = set()
```

4. After `add_l3intf` (line 116), add:

```python
    def add_ospf_intf(self, intf: OspfIntf) -> IRBuilder:
        if intf.id in self._ospf_intf_ids:
            raise IRValidationError(f"duplicate ospf intf id {intf.id}")
        self._ospf_intf_ids.add(intf.id)
        self._ospf_intfs.append(intf)
        return self
```

5. In `_validate` (after `errors += self._validate_l3intfs()`, line 172), add:

```python
        errors += self._validate_ospf_intfs()
```

6. After the `_validate_l3intfs` method (after line 213), add:

```python
    def _validate_ospf_intfs(self) -> list[str]:
        # the ospf_withdrawal check trusts these fields for collapse/clients/
        # affected-segment computation — mirror the role-aware dhcp_scope rule
        errors: list[str] = []
        for o in self._ospf_intfs:
            dev = self._devices.get(o.device_id)
            if dev is None:
                errors.append(f"ospf intf {o.id} references unknown device {o.device_id}")
            elif dev.role is not DeviceRole.SWITCH:
                errors.append(f"ospf intf {o.id} device {o.device_id} is not a switch")
            if o.unresolved and o.vlan_id is not None:
                errors.append(f"ospf intf {o.id} is unresolved but carries vlan_id {o.vlan_id}")
            if not o.unresolved and o.vlan_id is None:
                errors.append(f"ospf intf {o.id} is resolved but has no vlan_id")
            if o.vlan_id is not None and o.vlan_id not in self._vlans:
                errors.append(f"ospf intf {o.id} references unknown vlan {o.vlan_id}")
        return errors
```

7. In `build()` (line 286), add `ospf_intfs=tuple(self._ospf_intfs),` to the `IR(...)` call — place it right after `l3intfs=tuple(self._l3intfs),` (line 293).

Confirm `DeviceRole` is already imported in `model.py` (it is — `_validate_dhcp_scopes` uses `DeviceRole.GATEWAY`).

- [ ] **Step 6: Run the entity/builder tests**

Run: `uv run pytest tests/ir/test_model_ospf.py -q`
Expected: PASS (all 8).

- [ ] **Step 7: Write the failing diff test**

In `tests/ir/test_diff.py`, append:

```python
def test_ospf_intf_removal_and_passive_flip_are_diffed():
    from digital_twin.ir import IRBuilder, diff_ir
    from digital_twin.ir.entities import Device, DeviceRole, OspfIntf, Vlan

    def _ir(passive, present=True):
        b = IRBuilder().add_device(Device(id="S", role=DeviceRole.SWITCH, site="s1"))
        b.add_vlan(Vlan(vlan_id=10))
        if present:
            b.add_ospf_intf(
                OspfIntf(device_id="S", vlan_id=10, area="0", network_name="corp", passive=passive)
            )
        return b.build()

    # removal: present -> absent surfaces a removed ospf_intf ref
    d = diff_ir(_ir(False, present=True), _ir(False, present=False))
    assert d.touches("ospf_intf")
    assert any(r.kind == "ospf_intf" and r.id == "S:ospf:0:corp" for r in d.removed)

    # passive flip (retained id): a MODIFIED ospf_intf with changed_fields=("passive",)
    d2 = diff_ir(_ir(False), _ir(True))
    assert d2.touches("ospf_intf")
    mod = next(m for m in d2.modified if m.ref.id == "S:ospf:0:corp")
    assert mod.changed_fields == ("passive",)
```

- [ ] **Step 8: Run to verify it fails**

Run: `uv run pytest tests/ir/test_diff.py::test_ospf_intf_removal_and_passive_flip_are_diffed -q`
Expected: FAIL (`touches("ospf_intf")` is False — kind not registered).

- [ ] **Step 9: Register the diff kind**

In `src/digital_twin/ir/diff.py`, add one line to `_ENTITY_KINDS` (after the `l3intf` line, line 26):

```python
    ("ospf_intf", lambda ir: ir.ospf_intfs),
```

- [ ] **Step 10: Run the diff test**

Run: `uv run pytest tests/ir/test_diff.py -q`
Expected: PASS.

- [ ] **Step 11: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/ir tests/ir
git commit -m "GS26 T1: OspfIntf IR entity + builder + role-aware validation + diff kind"
```

---

## Task 2: Compile carry-through + allowlist

**Files:**
- Modify: `src/digital_twin/adapters/mist/compile/switch.py:46`
- Modify: `src/digital_twin/scope/allowlist.py`
- Test: `tests/adapters/mist/test_compile_switch.py` (or the existing compile test module), `tests/scope/test_allowlist.py` (or the existing allowlist/field-gate test module)

- [ ] **Step 1: Write the failing compile test**

Find the compile test module (`rg -l "compile_device" tests`). Append to it (adjust imports to match the file):

```python
def test_compile_device_carries_device_level_ospf():
    from digital_twin.adapters.mist.compile.switch import compile_device

    site = {"networks": {"corp": {"vlan_id": 10}}}
    device = {
        "ospf_config": {"enabled": True},
        "ospf_areas": {"0": {"networks": {"corp": {}}}},
    }
    out = compile_device(None, site, device)
    assert out["ospf_config"] == {"enabled": True}
    assert out["ospf_areas"] == {"0": {"networks": {"corp": {}}}}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests -k compile_device_carries_device_level_ospf -q`
Expected: FAIL (`KeyError: 'ospf_config'` — dropped by compile).

- [ ] **Step 3: Carry the fields**

In `src/digital_twin/adapters/mist/compile/switch.py`, line 46, extend `_DEVICE_OWN_FIELDS`:

```python
_DEVICE_OWN_FIELDS = (
    "ip_config",
    "other_ip_configs",
    "stp_config",
    "dhcp_snooping",
    "ospf_config",
    "ospf_areas",
)
```

- [ ] **Step 4: Run the compile test**

Run: `uv run pytest tests -k compile_device_carries_device_level_ospf -q`
Expected: PASS.

- [ ] **Step 5: Write the failing allowlist test**

Find the allowlist/field-gate test module (`rg -l "RAW_ALLOWLIST\|EFFECTIVE_ALLOWLIST\|allowed\(" tests`). Append:

```python
def test_ospf_allowlist_is_leaf_tightened():
    from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST, RAW_ALLOWLIST
    from digital_twin.scope.paths import allowed

    for al in (RAW_ALLOWLIST["device"], RAW_ALLOWLIST["site_setting"], EFFECTIVE_ALLOWLIST):
        # modeled + acted-on leaves are in scope
        assert allowed("ospf_config.enabled", al)
        assert allowed("ospf_areas.0.networks.corp.passive", al)
        # unmodeled leaves stay DENIED (GS27 owns them; deny prevents false-SAFE)
        assert not allowed("ospf_areas.0.networks.corp.metric", al)
        assert not allowed("ospf_areas.0.type", al)
        assert not allowed("ospf_areas.0.networks.corp.auth_password", al)
        assert not allowed("ospf_areas.0.networks.corp.interface_type", al)
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests -k test_ospf_allowlist_is_leaf_tightened -q`
Expected: FAIL (`ospf_config.enabled` not allowed).

- [ ] **Step 7: Add the OSPF leaves**

In `src/digital_twin/scope/allowlist.py`, after `_SNOOPING_LEAVES` (line 72), add:

```python
# OSPF participation the IR models AND acts on (GS26 wired.l3.ospf_withdrawal):
# the master enable (disable = full collapse) + the per-network passive flag
# (active vs adjacency-bearing). EVERYTHING else (metric, area type, auth,
# timers, interface_type) stays DENIED -> UNKNOWN: GS27 owns those mutations,
# and allowlisting a leaf no check reasons about would be a false-SAFE.
_OSPF_LEAVES: tuple[str, ...] = (
    "ospf_config.enabled",
    "ospf_areas.*.networks.*.passive",
)
```

Then add `*_OSPF_LEAVES,` to all three allowlist tuples: `RAW_ALLOWLIST["site_setting"]` (after `*_SNOOPING_LEAVES,`, line 111), `RAW_ALLOWLIST["device"]` (after `*_SNOOPING_LEAVES,`, line 120), and `EFFECTIVE_ALLOWLIST` (after `*_SNOOPING_LEAVES,`, line 161).

- [ ] **Step 8: Run the allowlist test + full gate**

Run: `uv run pytest tests -k "test_ospf_allowlist_is_leaf_tightened or compile_device_carries_device_level_ospf" -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/adapters/mist/compile/switch.py src/digital_twin/scope/allowlist.py tests
git commit -m "GS26 T2: carry device ospf through compile + leaf-tightened ospf allowlist"
```

---

## Task 3: Switch `_ospf` ingest pass

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (the `ingest` loop ~line 318, plus a new `_ospf` method)
- Test: `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Write the failing ingest tests**

Append to `tests/adapters/mist/test_ingest_switch.py` (match the file's existing fixture/ingest harness — most tests there build a raw doc and call the adapter's ingest, then inspect `ir.ospf_intfs`; mirror the nearest existing `_ingest`/`_build_ir` helper in that file):

```python
def test_ospf_ingest_mints_participation_for_enabled_switch(ospf_switch_ir):
    # helper builds an IR from a switch whose effective config has:
    #   ospf_config={"enabled": True}
    #   ospf_areas={"0": {"networks": {"corp": {}, "guest": {"passive": True}}}}
    #   networks={"corp": {"vlan_id": 10}, "guest": {"vlan_id": 20}}
    ir = ospf_switch_ir(
        enabled=True,
        areas={"0": {"networks": {"corp": {}, "guest": {"passive": True}}}},
        networks={"corp": {"vlan_id": 10}, "guest": {"vlan_id": 20}},
    )
    by_name = {o.network_name: o for o in ir.ospf_intfs}
    assert by_name["corp"].vlan_id == 10 and by_name["corp"].passive is False
    assert by_name["guest"].vlan_id == 20 and by_name["guest"].passive is True
    assert all(o.unresolved is False for o in ir.ospf_intfs)


def test_ospf_ingest_silent_when_disabled(ospf_switch_ir):
    ir = ospf_switch_ir(
        enabled=False,
        areas={"0": {"networks": {"corp": {}}}},
        networks={"corp": {"vlan_id": 10}},
    )
    assert ir.ospf_intfs == ()


def test_ospf_ingest_unresolved_name(ospf_switch_ir):
    ir = ospf_switch_ir(
        enabled=True,
        areas={"0": {"networks": {"ghost": {}}}},
        networks={"corp": {"vlan_id": 10}},  # 'ghost' is not defined
    )
    o = next(o for o in ir.ospf_intfs if o.network_name == "ghost")
    assert o.vlan_id is None and o.unresolved is True
```

Add the `ospf_switch_ir` fixture at the top of the test file (or reuse the file's existing doc-builder; this is the shape — adapt the raw-doc plumbing to the module's existing helper that turns a `setting`/`devices` doc into an IR):

```python
@pytest.fixture
def ospf_switch_ir(build_switch_ir):  # build_switch_ir: the module's existing doc->IR helper
    def _make(*, enabled, areas, networks):
        return build_switch_ir(
            setting={"networks": networks},
            device={"ospf_config": {"enabled": enabled}, "ospf_areas": areas},
        )
    return _make
```

If no reusable `build_switch_ir` helper exists in the file, build the raw doc inline the way the neighbouring ingest tests do (set `doc["setting"]`, one switch device with `mac`, `type="switch"`, the `ospf_*` keys, then run the adapter's `ingest`). Keep the three assertions above unchanged.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -k ospf -q`
Expected: FAIL (`ir.ospf_intfs` is empty — no `_ospf` pass yet).

- [ ] **Step 3: Add the `_ospf` method and call it**

In `src/digital_twin/adapters/mist/ingest/switch.py`, in the `ingest` loop, change the switch branch (lines 318-320) to also run the OSPF pass:

```python
        for dev in ctx.raw.devices:
            if dev.get("type") == "switch":
                self._switch_ports_and_l3(ctx, dev)
                self._ospf(ctx, dev)
            elif dev.get("type") == "gateway":
                self._gateway_ports_and_l3(ctx, dev)
```

Add the method next to `_switch_ports_and_l3` (after line 720):

```python
    def _ospf(self, ctx: IngestContext, dev: Mapping[str, Any]) -> None:
        """GS26: switch OSPF participation. Gated by ospf_config.enabled; each
        ospf_areas.<area>.networks.<name> joins to the effective networks map.
        A name that does not resolve to a vlan_id mints an unresolved row (the
        only switch-side blindness — l3_unmodeled is gateway-only)."""
        did = device_id(str(dev["mac"]))
        eff = ctx.device_effective.get(did) or ctx.site_effective
        if not (eff.get("ospf_config") or {}).get("enabled"):
            return
        networks: dict[str, Any] = eff.get("networks") or {}
        for area, area_cfg in (eff.get("ospf_areas") or {}).items():
            for name, ncfg in ((area_cfg or {}).get("networks") or {}).items():
                ncfg = ncfg or {}
                vid = _vlan_int((networks.get(str(name)) or {}).get("vlan_id"))
                ctx.builder.add_ospf_intf(
                    OspfIntf(
                        device_id=did,
                        vlan_id=vid,
                        area=str(area),
                        network_name=str(name),
                        passive=bool(ncfg.get("passive", False)),
                        unresolved=(vid is None),
                    )
                )
```

Add `OspfIntf` to the entities import at the top of `switch.py` (the line importing `L3Intf`, `L3Role`, etc.). Confirm `_vlan_int` is defined in this module (it is — used by the gateway L3 pass).

- [ ] **Step 4: Run the ingest tests**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -k ospf -q`
Expected: PASS (all 3).

- [ ] **Step 5: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/adapters/mist/ingest/switch.py tests/adapters/mist/test_ingest_switch.py
git commit -m "GS26 T3: switch _ospf ingest pass (enabled-gated, name-resolved, unresolved blind)"
```

---

## Task 4: The `wired.l3.ospf_withdrawal` check

**Files:**
- Create: `src/digital_twin/checks/wired/ospf_withdrawal.py`
- Modify: `tests/factories.py`
- Test: `tests/checks/test_ospf_withdrawal.py` (create)

- [ ] **Step 1: Add the `ospf` factory**

In `tests/factories.py`: add `OspfIntf` to the entities import block, and after the `irb(...)` helper add:

```python
def ospf(did: str, vlan: int | None, area: str = "0", *, passive: bool = False,
         name: str | None = None, unresolved: bool = False) -> OspfIntf:
    return OspfIntf(
        device_id=did, vlan_id=vlan, area=area,
        network_name=name if name is not None else (f"net{vlan}" if vlan is not None else "ghost"),
        passive=passive, unresolved=unresolved,
    )
```

- [ ] **Step 2: Write the failing check tests**

Create `tests/checks/test_ospf_withdrawal.py`:

```python
"""wired.l3.ospf_withdrawal (GS26): structural withdrawal of a switch's OSPF
participation for a routed segment. Base REVIEW; UNSAFE only when the device's
last active adjacency collapses AND an affected segment has observed clients.
Comparison is by the semantic (device, vlan[, area, active]) tuple, never id."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, CoverageState, Status
from digital_twin.checks.wired.ospf_withdrawal import OspfWithdrawalCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Vlan, diff_ir
from digital_twin.ir.entities import Client, ClientKind, AttachKind
from tests.factories import irb, ospf, sw


def _ir(ospf_rows, *, clients=(), routed=(10, 20, 30), with_clients_cap=True):
    b = IRBuilder().add_device(sw("S"))
    for vid in routed:
        b.add_vlan(Vlan(vlan_id=vid, subnet=f"198.51.{vid}.0/24"))
        b.add_l3intf(irb("S", vid, subnet=f"198.51.{vid}.0/24"))
    for row in ospf_rows:
        b.add_ospf_intf(row)
    for mac, vid in clients:
        b.add_client(
            Client(mac=mac, kind=ClientKind.WIRED, attach_kind=AttachKind.PORT,
                   attach_id="S:p1", vlan=vid)
        )
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.L3_EXITS)
    if with_clients_cap:
        b.with_capability(IRCapability.CLIENTS_ACTIVE)
    return b.build()


def _run(base, prop):
    return OspfWithdrawalCheck().run(
        CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                     diff=diff_ir(base, prop))
    )


def test_requires_and_not_applicable_without_ospf_diff():
    assert OspfWithdrawalCheck().requires() == frozenset(
        {IRCapability.WIRED_L2, IRCapability.L3_EXITS}
    )
    # no ospf entities anywhere -> applies_to False
    base = _ir([])
    prop = _ir([])
    assert OspfWithdrawalCheck().applies_to(diff_ir(base, prop)) is False


def test_egress_lost_with_clients_is_fail():
    # one active transit on vlan 10; removing it = last adjacency collapses;
    # an islanded routed segment (vlan 20, advertised passively) has a client
    base = _ir(
        [ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)],
        clients=[("aa:bb", 20)],
    )
    prop = _ir([ospf("S", 20, name="corp", passive=True)], clients=[("aa:bb", 20)])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.ERROR
    assert r.status is Status.FAIL


def test_egress_lost_without_clients_is_warn():
    base = _ir([ospf("S", 10, name="transit")])
    prop = _ir([])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.WARNING
    assert r.status is Status.WARN


def test_egress_lost_clients_unfetched_stays_warn_and_partial():
    base = _ir([ospf("S", 10, name="transit")], clients=[("aa:bb", 10)], with_clients_cap=False)
    prop = _ir([], with_clients_cap=False)
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.WARNING
    assert r.coverage.state is CoverageState.PARTIAL


def test_disable_ospf_collapses_all():
    base = _ir([ospf("S", 10, name="a"), ospf("S", 20, name="b")], clients=[("aa:bb", 10)])
    prop = _ir([])  # ospf_config.enabled -> false drops every row
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.egress_lost")
    assert f.severity is Severity.ERROR


def test_advertised_removed_when_device_keeps_adjacency():
    # remove the passive stub (vlan 20); device keeps active transit (vlan 10)
    base = _ir([ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)])
    prop = _ir([ospf("S", 10, name="transit")])
    r = _run(base, prop)
    codes = {f.code for f in r.findings}
    assert "wired.l3.ospf_withdrawal.advertised_removed" in codes
    assert "wired.l3.ospf_withdrawal.egress_lost" not in codes
    assert r.status is Status.WARN


def test_addition_and_unrelated_are_silent():
    base = _ir([ospf("S", 10, name="transit")])
    prop = _ir([ospf("S", 10, name="transit"), ospf("S", 20, name="corp", passive=True)])
    r = _run(base, prop)
    assert r.findings == ()
    assert r.status is Status.PASS


def test_transit_mutation_on_noncollapsing_passive_flip():
    # two active interfaces; flip ONE to passive -> device still has an active
    # adjacency (no collapse) -> a retained (device,vlan) tuple changed -> REVIEW
    base = _ir([ospf("S", 10, name="a"), ospf("S", 20, name="b")])
    prop = _ir([ospf("S", 10, name="a", passive=True), ospf("S", 20, name="b")])
    r = _run(base, prop)
    f = next(f for f in r.findings if f.code == "wired.l3.ospf_withdrawal.transit_mutation")
    assert f.severity is Severity.WARNING
    assert r.status is Status.WARN


def test_pure_rename_is_silent():
    # same (device, vlan, area, active) tuple, only the network_name (=id) changed
    base = _ir([ospf("S", 10, name="corp")])
    prop = _ir([ospf("S", 10, name="corp2")])
    r = _run(base, prop)
    assert r.findings == ()
    assert r.status is Status.PASS


def test_area_move_is_transit_mutation_not_withdrawal():
    base = _ir([ospf("S", 10, name="corp", area="0")])
    prop = _ir([ospf("S", 10, name="corp", area="1")])
    r = _run(base, prop)
    codes = {f.code for f in r.findings}
    assert codes == {"wired.l3.ospf_withdrawal.transit_mutation"}


def test_unresolved_withdrawal_abstains_partial_never_unsafe():
    base = _ir([ospf("S", None, name="ghost", unresolved=True)])
    prop = _ir([])
    r = _run(base, prop)
    assert r.status is not Status.FAIL
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("does not resolve" in n for n in r.coverage.notes)
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/checks/test_ospf_withdrawal.py -q`
Expected: FAIL (`ModuleNotFoundError: ospf_withdrawal`).

- [ ] **Step 4: Implement the check**

Create `src/digital_twin/checks/wired/ospf_withdrawal.py`:

```python
"""wired.l3.ospf_withdrawal — structural withdrawal of a switch's OSPF
participation for a routed segment (GS26).

The twin has no RIB, so this detects MODELED participation leaving OSPF, never
real reachability, and floors accordingly. Three codes:
- .egress_lost: a device's last ACTIVE (adjacency-bearing) interface goes away
  (removed, ospf disabled, or active->passive flip that collapses it) -> the
  device loses modeled dynamic egress. ERROR/UNSAFE iff an affected routed
  segment has observed clients; else WARNING/REVIEW.
- .advertised_removed: a routed segment fully withdrawn from OSPF while its
  device keeps adjacency -> WARNING/REVIEW (prefix no longer distributed).
- .transit_mutation: a retained (device, vlan) whose active-status or area set
  changed but no withdrawal owns it -> WARNING/REVIEW (the deferred-mutation
  floor; GS27 replaces it with precise transit modeling). A pure rename leaves
  the semantic tuple unchanged -> silent.

Comparison is by the semantic (device, vlan[, area, active]) tuple, NEVER by
OspfIntf.id, so rename/area-move is not a false withdrawal. l3_unmodeled is
gateway-only; the sole switch-side blindness is an unresolved network name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=(
        "OSPF reachability is not computed — a static default or redistribution "
        "the twin does not model may still cover this segment",
    ),
)


@dataclass
class _Seg:
    active: bool = False
    areas: set[str] = field(default_factory=set)


@dataclass
class _Part:
    by_dev_vlan: dict[tuple[str, int], _Seg]
    active_by_dev: dict[str, set[int]]


def _participation(ir: IR) -> _Part:
    by_dev_vlan: dict[tuple[str, int], _Seg] = {}
    active_by_dev: dict[str, set[int]] = {}
    for o in ir.ospf_intfs:
        if o.vlan_id is None:
            continue  # unresolved rows handled separately
        seg = by_dev_vlan.setdefault((o.device_id, o.vlan_id), _Seg())
        seg.areas.add(o.area)
        if not o.passive:
            seg.active = True
            active_by_dev.setdefault(o.device_id, set()).add(o.vlan_id)
    return _Part(by_dev_vlan, active_by_dev)


def _l3_vids(ir: IR) -> set[int]:
    return {i.vlan_id for i in ir.l3intfs if i.vlan_id is not None}


def _routed(ir: IR, vid: int, l3_vids: set[int]) -> bool:
    vlan = ir.vlans.get(vid)
    return vlan is not None and (vlan.subnet is not None or vid in l3_vids)


class OspfWithdrawalCheck:
    id = "wired.l3.ospf_withdrawal"
    title = "routed segment withdrawn from OSPF"
    domain = "wired.l3"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2, IRCapability.L3_EXITS})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("ospf_intf")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        base, prop = _participation(base_ir), _participation(prop_ir)
        base_l3, prop_l3 = _l3_vids(base_ir), _l3_vids(prop_ir)
        clients_known = (
            IRCapability.CLIENTS_ACTIVE in base_ir.capabilities
            and IRCapability.CLIENTS_ACTIVE in prop_ir.capabilities
        )
        findings: list[Finding] = []
        notes: list[str] = []
        egress_owned: set[int] = set()

        # 1. device adjacency collapse -> .egress_lost
        collapsed = sorted(
            did
            for did, act in base.active_by_dev.items()
            if act and not prop.active_by_dev.get(did)
        )
        for did in collapsed:
            affected = sorted(
                {
                    vid
                    for (d, vid) in base.by_dev_vlan
                    if d == did and _routed(base_ir, vid, base_l3)
                }
            )
            if not affected:
                continue
            egress_owned.update(affected)
            n_clients = (
                sum(1 for c in base_ir.clients if c.vlan in set(affected))
                if clients_known
                else 0
            )
            severity = (
                Severity.ERROR if (clients_known and n_clients) else Severity.WARNING
            )
            if not clients_known:
                notes.append(
                    f"device {did}: client data unavailable — the egress-loss blast "
                    "radius is unknown"
                )
            who = (
                f"{n_clients} observed client(s)"
                if clients_known
                else "an unknown number of clients"
            )
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.egress_lost",
                    severity=severity,
                    confidence=_HIGH,
                    message=(
                        f"switch {did} loses its last active OSPF adjacency — routed "
                        f"segments {affected} lose their modeled dynamic egress; {who} "
                        "on them are affected"
                    ),
                    affected_entities=tuple(str(v) for v in affected),
                    evidence={
                        "device": did,
                        "affected_vlans": affected,
                        "observed_clients": n_clients if clients_known else None,
                    },
                )
            )

        # 2. per-segment full withdrawal -> .advertised_removed
        base_vlans = {vid for (_d, vid) in base.by_dev_vlan}
        prop_vlans = {vid for (_d, vid) in prop.by_dev_vlan}
        for vid in sorted(base_vlans - prop_vlans):
            if vid in egress_owned or not _routed(base_ir, vid, base_l3):
                continue
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.advertised_removed",
                    severity=Severity.WARNING,
                    confidence=_UNVERIFIED,
                    message=(
                        f"routed segment (vlan {vid}) is withdrawn from OSPF — its "
                        "prefix is no longer advertised; external reachability depends "
                        "on redistribution the twin does not model"
                    ),
                    affected_entities=(str(vid),),
                    evidence={"vlan": vid},
                )
            )

        # 3. retained participation mutated (active-status or area) -> .transit_mutation
        for key in sorted(set(base.by_dev_vlan) & set(prop.by_dev_vlan)):
            did, vid = key
            if vid in egress_owned or not _routed(prop_ir, vid, prop_l3):
                continue
            b, p = base.by_dev_vlan[key], prop.by_dev_vlan[key]
            if (b.active, b.areas) == (p.active, p.areas):
                continue  # tuple unchanged (a pure rename lands here -> silent)
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.transit_mutation",
                    severity=Severity.WARNING,
                    confidence=_UNVERIFIED,
                    message=(
                        f"OSPF participation for vlan {vid} on {did} changed "
                        "(passive/area) — transit & area-semantics impact is deferred "
                        "to GS27"
                    ),
                    affected_entities=(str(vid),),
                    evidence={"device": did, "vlan": vid},
                )
            )

        # 4. unresolved rows touched by the delta -> PARTIAL abstain (never silent)
        touched_ids = {
            r.id
            for r in (*ctx.diff.added, *ctx.diff.removed, *(m.ref for m in ctx.diff.modified))
            if r.kind == "ospf_intf"
        }
        seen: set[str] = set()
        for o in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs):
            if o.unresolved and o.id in touched_ids and o.id not in seen:
                seen.add(o.id)
                notes.append(
                    f"ospf interface {o.id}: network name {o.network_name!r} does not "
                    "resolve to a vlan — withdrawal impact cannot be verified"
                )

        worst = Status.PASS
        for f in findings:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(
                state=CoverageState.PARTIAL if notes else CoverageState.COMPLETE,
                notes=tuple(notes),
            ),
            confidence=(
                min_confidence(*(f.confidence for f in findings)) if findings else _HIGH
            ),
            reasoning="compared per-(device,vlan) OSPF participation, baseline vs proposed",
        )
```

- [ ] **Step 5: Run the check tests**

Run: `uv run pytest tests/checks/test_ospf_withdrawal.py -q`
Expected: PASS (all 11).

- [ ] **Step 6: Full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/checks/wired/ospf_withdrawal.py tests/checks/test_ospf_withdrawal.py tests/factories.py
git commit -m "GS26 T4: wired.l3.ospf_withdrawal check (egress_lost/advertised_removed/transit_mutation)"
```

---

## Task 5: Register the check

**Files:**
- Modify: `src/digital_twin/checks/wired/__init__.py`
- Test: the existing registry test (`rg -l "ALL_WIRED_CHECKS" tests`)

- [ ] **Step 1: Write the failing registry test**

Append to the registry test module (adjust import to match it):

```python
def test_ospf_withdrawal_is_registered():
    from digital_twin.checks.wired import ALL_WIRED_CHECKS

    assert any(c.id == "wired.l3.ospf_withdrawal" for c in ALL_WIRED_CHECKS)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests -k test_ospf_withdrawal_is_registered -q`
Expected: FAIL.

- [ ] **Step 3: Register it**

In `src/digital_twin/checks/wired/__init__.py`:
1. Add `from .ospf_withdrawal import OspfWithdrawalCheck` (after the `native_mismatch` import).
2. Add `OspfWithdrawalCheck(),` to `ALL_WIRED_CHECKS` (after `GatewayGapCheck(),`).
3. Add `"OspfWithdrawalCheck",` to `__all__`.

- [ ] **Step 4: Run + full gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/checks/wired/__init__.py tests
git commit -m "GS26 T5: register wired.l3.ospf_withdrawal in ALL_WIRED_CHECKS"
```

---

## Task 6: Goldens GS26 a–e, builders, roadmap, live verification, memory

**Files:**
- Modify: `tests/golden/builders.py`
- Modify: `tests/golden/test_golden_scenarios.py`
- Modify: `docs/ROADMAP.md`
- Modify: `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/...`

- [ ] **Step 1: Add the OSPF golden builders**

In `tests/golden/builders.py`, append (HUB is the core switch that already holds IRBs):

```python
OSPF_NETS = {  # name -> (vlan_id, subnet)
    "ospf_transit": (970, "198.51.70.0/24"),
    "ospf_corp": (971, "198.51.71.0/24"),
}


def ospf_doc(entries: dict[str, dict[str, Any]], *, client_vlan: int | None = None) -> dict[str, Any]:
    """HUB switch running OSPF. `entries` maps a name from OSPF_NETS to its
    ospf_areas network entry ({} = active, {"passive": True} = stub). Each named
    net gets a Vlan (with subnet) + an IRB on HUB (a routed segment). Optionally
    place one observed wired client on `client_vlan`."""
    doc = fixture_doc()
    hub = _device(doc, HUB)
    hub["ospf_config"] = {"enabled": True}
    networks_block: dict[str, Any] = {}
    for name, entry in entries.items():
        vid, subnet = OSPF_NETS[name]
        doc["setting"]["networks"][name] = {"vlan_id": vid, "subnet": subnet}
        hub.setdefault("other_ip_configs", {})[name] = {
            "type": "static", "ip": subnet.replace(".0/24", ".1"), "netmask": "255.255.255.0",
        }
        networks_block[name] = entry
    hub["ospf_areas"] = {"0": {"networks": networks_block}}
    if client_vlan is not None:
        hub_port = "ge-0/0/40"
        doc["setting"]["port_usages"]["ospf_access"] = {
            "mode": "access", "port_network": next(n for n, (v, _s) in OSPF_NETS.items() if v == client_vlan)
        }
        hub.setdefault("port_config", {})[hub_port] = {"usage": "ospf_access"}
        doc["wired_clients"] = list(doc["wired_clients"]) + [
            {"mac": WIRED_CLIENT_MAC, "device_mac": HUB, "port_id": hub_port, "vlan": client_vlan}
        ]
    return doc


def ospf_op(doc: dict[str, Any], entries: dict[str, dict[str, Any]] | None, *,
            disable: bool = False, order: int = 0) -> dict[str, Any]:
    """A HUB device op whose payload sets ospf to the given state. `entries=None`
    + disable=True flips ospf_config.enabled false; otherwise the payload's
    ospf_areas.0.networks is REPLACED with `entries` (omit a name = withdrawn)."""
    dev = copy.deepcopy(_device(doc, HUB))
    if disable:
        dev["ospf_config"] = {"enabled": False}
    else:
        dev["ospf_areas"] = {"0": {"networks": entries or {}}}
    return {
        "action": "update", "order": order, "object_type": "device",
        "object_id": str(dev["id"]), "payload": _drop_nones(dev),
    }
```

- [ ] **Step 2: Write the failing GS26 goldens**

Append to `tests/golden/test_golden_scenarios.py` (imports: add `ospf_doc, ospf_op` to the `from .builders import (...)` block; `OSPF_NETS` if needed):

```python
# --- GS26: OSPF exit withdrawal -------------------------------------------

def test_gs26a_passive_stub_withdrawal_is_review(tmp_path):
    # device keeps its active transit; a passive stub leaves OSPF -> REVIEW
    doc = ospf_doc({"ospf_transit": {}, "ospf_corp": {"passive": True}})
    op = ospf_op(doc, {"ospf_transit": {}})  # ospf_corp withdrawn
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.advertised_removed" in {f.code for f in v.findings}


def test_gs26b_bare_active_withdrawal_collapse_with_clients_is_unsafe(tmp_path):
    # bare {} active transit (default-active) removed = last adjacency collapses;
    # an islanded routed segment (ospf_corp, vlan 971) has an observed client
    doc = ospf_doc({"ospf_transit": {}, "ospf_corp": {"passive": True}}, client_vlan=971)
    op = ospf_op(doc, {"ospf_corp": {"passive": True}})  # active transit withdrawn
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.egress_lost" in {f.code for f in v.findings}


def test_gs26c_disable_ospf_with_clients_is_unsafe(tmp_path):
    doc = ospf_doc({"ospf_transit": {}}, client_vlan=970)
    op = ospf_op(doc, None, disable=True)
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.egress_lost" in {f.code for f in v.findings}


def test_gs26d_addition_to_ospf_is_safe(tmp_path):
    # baseline: only transit in OSPF; op ADDS ospf_corp -> not a withdrawal
    doc = ospf_doc({"ospf_transit": {}})
    doc["setting"]["networks"]["ospf_corp"] = {"vlan_id": 971, "subnet": "198.51.71.0/24"}
    op = ospf_op(doc, {"ospf_transit": {}, "ospf_corp": {"passive": True}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_gs26e_noncollapsing_passive_flip_is_review(tmp_path):
    # two active interfaces; flip ONE to passive -> device keeps an adjacency
    # -> .transit_mutation REVIEW (not SAFE, not UNSAFE)
    doc = ospf_doc({"ospf_transit": {}, "ospf_corp": {}})
    op = ospf_op(doc, {"ospf_transit": {}, "ospf_corp": {"passive": True}})
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wired.l3.ospf_withdrawal.transit_mutation" in {f.code for f in v.findings}
```

- [ ] **Step 3: Run the goldens to verify they fail, then pass**

Run: `uv run pytest tests/golden/test_golden_scenarios.py -k gs26 -q`
Expected: initially FAIL (assertions), then PASS once Tasks 1-5 are in (they are). If any golden mis-resolves, debug with `-k gs26X -q` and inspect `v.decision_reasons` / `{f.code for f in v.findings}`. Common causes: the OSPF entry carried a denied leaf (→ UNKNOWN — keep entries to `{}`/`{"passive": True}` only); the withdrawn segment lacked an IRB (→ not routed → silent).

- [ ] **Step 4: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: all green.

- [ ] **Step 5: Commit goldens**

```bash
git add tests/golden/builders.py tests/golden/test_golden_scenarios.py
git commit -m "GS26 T6: goldens a-e (advertised_removed/egress_lost/disable/addition-safe/transit_mutation)"
```

- [ ] **Step 6: Live verification (read-only, all 8 plans unchanged)**

The live org has empty `ospf_areas`, so no plan should change verdict. Run:

```bash
set -a; source .env; set +a; for p in plan.json test-plans/*.json; do printf '%s ' "$p"; uv run digital-twin --plan "$p" 2>/dev/null | head -1; done
```

Expected (unchanged from the GS22-SUB baseline): `plan.json` UNKNOWN/UNSAFE per its content, `test-plans/01` SAFE, `02` SAFE, `03` REVIEW, `04` REVIEW, `05` UNSAFE, `06` SAFE, `07` REVIEW. Confirm each first line matches the prior round; if any differs, STOP and investigate (OSPF should be inert live).

- [ ] **Step 7: Update the roadmap**

In `docs/ROADMAP.md`, flip the GS26 line (§2 routing tier, "OSPF exit withdrawal (GS26)") from 🔵 to ✅ with a one-paragraph summary: dedicated `OspfIntf` (switch-only), `wired.l3.ospf_withdrawal` with three codes, leaf-tightened allowlist (`ospf_config.enabled` + `ospf_areas.*.networks.*.passive`; metric/type/auth denied → UNKNOWN), `.transit_mutation` deferred-mutation floor, compile carry-through fix, gateway OSPF deferred. Note all 8 live plans unchanged. Reference the spec/plan paths.

- [ ] **Step 8: Update memory**

Append a Round entry to `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md` (or the wireless/L3 memory file, matching where prior GS rounds are logged): GS26 done — OSPF exit withdrawal, the switch-only scope decision (gateway OSPF deferred behind the M1 field-gate role boundary), the `.transit_mutation` deferred-mutation REVIEW floor keyed on the semantic tuple, and that metric stays denied so GS27 adopts it without a false-SAFE hole.

- [ ] **Step 9: Commit wrap-up**

```bash
git add docs/ROADMAP.md
git commit -m "GS26: roadmap OSPF exit withdrawal -> done; live plans unchanged"
```

---

## Self-Review (completed by the planner)

**Spec coverage:** OspfIntf entity (T1) ✓; switch-only scope + role validation (T1 validation, T3 switch branch) ✓; compile carry-through (T2) ✓; leaf-tightened allowlist enabled+passive, metric/type/auth denied (T2) ✓; `_ospf` ingest enabled-gated + unresolved (T3) ✓; check three codes + semantic-tuple comparison + clients gate + unresolved abstain (T4) ✓; bare-`{}` active in-scope (GS26-b golden + the check has no raw-leaf dependency) ✓; registry (T5) ✓; goldens a–e (T6) ✓; live unchanged + roadmap + memory (T6) ✓.

**Placeholder scan:** none — every code step shows full code; commands have expected output.

**Type consistency:** `OspfIntf(device_id, vlan_id, area, network_name, passive, unresolved, meta, id)` is identical across entity, builder, ingest, factory, and check. `add_ospf_intf`, `ir.ospf_intfs`, diff kind `"ospf_intf"`, and the three finding codes are spelled identically throughout. `_participation`/`_Part`/`_Seg`/`_routed`/`_l3_vids` are defined in T4 and used only there.
