# Switch Port Admin-Disable + Precedence Rework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a device PUT that administratively disables switch ports (via `port_config_overwrite` / `local_port_config`) a *simulated* operation with a surfaced, blast-radius-weighted finding — instead of UNKNOWN — and fix the port-config precedence resolver to match real Mist semantics.

**Architecture:** Three layers. (1) `ingest/ports.py` resolver is reworked to layer `port_config` → `port_config_overwrite` → `local_port_config` (highest, per-member, gated on `no_local_overwrite`), over a member set that includes overwrite-only ports; inline `disabled` is threaded into the effective attrs that `ingest/switch.py` already reads into `Port.disabled`. (2) A new `wired.port.admin_disable` check (modeled on `wired.poe.disconnect`) surfaces newly-disabled ports, weighted by AP-uplink / trunk-or-non-AP-peer / active-wired-client / bare-edge. (3) The field gate allowlists `disabled` on the two maps (NOT `no_local_overwrite`, which stays UNKNOWN). The L2-blackhole consequence machinery already exists and composes for free.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col, E/F/I), mypy-strict, networkx.

**Spec:** `docs/superpowers/specs/2026-06-25-switch-port-admin-disable-design.md`

## Global Constraints

- **No false-SAFE.** Every newly in-scope leaf is modeled by a check or not allowlisted. `port_config.*.no_local_overwrite` stays UNKNOWN for v1.
- **`disabled` is valid only on `local_port_config` + `port_config_overwrite`** (OAS), NOT `port_config`. `port_config.*.disabled` must stay flagged.
- **Gate after every task, from the worktree root:** `uv run pytest -q && uv run ruff check . && uv run mypy src` — all green before commit. (mypy is not enforced on `tests/`.)
- **Commits** end with the trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Branch/worktree:** `worktree-feat+switch-port-admin-disable` at `.claude/worktrees/sp-port-disable` (already created off `origin/main`).
- **Determinism:** the resolver yields members in sorted order; checks iterate `dict` in insertion order then sort findings deterministically where order is asserted.

---

## File Structure

- **Modify** `src/digital_twin/adapters/mist/ingest/ports.py` — resolver precedence, `no_local_overwrite` gate, member-set union, per-layer attr tuples, inline `disabled`. (Tasks 1–2)
- **Create** `src/digital_twin/checks/wired/admin_disable.py` — the `wired.port.admin_disable` check. (Task 3)
- **Modify** `src/digital_twin/checks/wired/__init__.py` — register the check. (Task 3)
- **Modify** `tests/test_public_api.py` — bump `len(ALL_WIRED_CHECKS) == 20 → 21`. (Task 3)
- **Modify** `src/digital_twin/scope/allowlist.py` — add `disabled` leaves. (Task 4)
- **Modify** existing tests that encode the OLD unconditional-local behavior: `tests/adapters/mist/test_ingest_ports.py`, `tests/test_plan3_flow.py`, `tests/adapters/mist/test_ingest_switch.py` (+ any straggler the full-suite run surfaces). (Task 1)
- **Create** `tests/checks/test_admin_disable.py`. (Task 3)
- **Modify** `tests/engine/test_pipeline.py` (e2e), `docs/ROADMAP.md`. (Task 5)

`ingest/switch.py` needs **no change** — line 803 already builds `Port(disabled=bool(usage.get("disabled")))` from the resolver's effective attrs; once the resolver populates `disabled`, ingest picks it up (verified by a Task-2 ingest test).

---

## Task 1: Resolver precedence rework + `no_local_overwrite` gate + member-set union

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/ports.py:25-40` (attr tuples), `:90-138` (`resolve_port_bases`, `resolve_effective_ports`)
- Test: `tests/adapters/mist/test_ingest_ports.py`

**Interfaces:**
- Consumes: `_expand_map`, `usage_definition`, `expand_port_members` (unchanged in this file); `SITE_EFFECTIVE` fixture (`tests/adapters/mist/fixtures.py`) — `port_usages` has `office` (access/corp=10), `uplink` (trunk/corp+voice=30); `networks` corp=10, voice=30.
- Produces: `resolve_effective_ports(eff) -> Iterator[tuple[member, effective_attrs, usage_name|None, resolution]]` (signature unchanged); `resolve_port_bases(eff) -> dict[member, attrs]` (gated merge); `_overridable(pc_member: dict|None) -> bool` (new module-private). `disabled` is NOT yet honored (Task 2).

- [ ] **Step 1: Write failing tests for the gate, precedence, and member-set**

Add to `tests/adapters/mist/test_ingest_ports.py`:

```python
def test_local_discarded_when_no_local_overwrite_defaults_true():
    # port_config present, no_local_overwrite absent (OAS default true) -> local DISCARDED
    eff = _eff(
        port_config={"ge-0/0/7": {"usage": "office"}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
    assert _resolved(eff)["ge-0/0/7"][1] == "office"  # local ignored


def test_local_applies_when_no_local_overwrite_false():
    eff = _eff(
        port_config={"ge-0/0/7": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
    assert _resolved(eff)["ge-0/0/7"][1] == "uplink"


def test_standalone_local_applies_without_port_config_entry():
    eff = _eff(local_port_config={"ge-0/0/8": {"usage": "uplink"}})
    assert _resolved(eff)["ge-0/0/8"][1] == "uplink"


def test_local_outranks_port_config_overwrite():
    # both set port_network; local is the highest layer -> local wins
    eff = _eff(
        port_config={"ge-0/0/9": {"usage": "office", "no_local_overwrite": False}},
        port_config_overwrite={"ge-0/0/9": {"port_network": "voice"}},
        local_port_config={"ge-0/0/9": {"port_network": "corp"}},
    )
    usage, _name = _resolved(eff)["ge-0/0/9"]
    assert usage["port_network"] == "corp"


def test_overwrite_only_member_is_resolved():
    # a port present ONLY in port_config_overwrite still yields a resolved port
    eff = _eff(port_config_overwrite={"ge-0/0/12": {"port_network": "voice"}})
    res = _resolved(eff)
    assert "ge-0/0/12" in res
    assert res["ge-0/0/12"][0]["port_network"] == "voice"
    assert res["ge-0/0/12"][1] is None  # no usage name resolves
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py -k "no_local_overwrite or outranks or overwrite_only or standalone_local" -q`
Expected: FAIL (gate not implemented; overwrite-only member dropped; local applied unconditionally).

- [ ] **Step 3: (No attr-tuple change in Task 1)**

Leave `ports.py:25-40` (`_USAGE_OVERRIDE_ATTRS` incl `stp_edge`; `_OVERWRITE_ATTRS = ("port_network", "poe_disabled")`) **exactly as they are in the worktree**. The worktree resolver is unchanged by PR #14 — only the *field gate* (`allowlist.py`) was narrowed per-map. This sub-project's precedence fix is purely in the layering (Steps 4–5); the per-map `disabled` attr is added in Task 2. (Applying `_USAGE_OVERRIDE_ATTRS` from both the port_config and local layers preserves today's behavior; the field gate, not the resolver, decides which inline leaves are in scope.)

- [ ] **Step 4: Add the `_overridable` gate and rework `resolve_port_bases`**

Replace `resolve_port_bases` (`ports.py:99-107`) with:

```python
def _overridable(pc_member: dict[str, Any] | None) -> bool:
    """local_port_config applies to a member ONLY when there is no port_config
    entry to protect, or that entry explicitly allows it. `no_local_overwrite`
    defaults to true (OAS) -> local is DISCARDED by default."""
    if pc_member is None:
        return True
    return not pc_member.get("no_local_overwrite", True)


def resolve_port_bases(eff: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """member -> merged base attrs (usage, dynamic_usage, inline) with real Mist
    precedence: port_config <- local_port_config, per member, ONLY when locally
    overridable (`no_local_overwrite == false` or no port_config entry). The base
    the named port_usages profile is then applied to. port_config_overwrite is
    NOT merged here (it tweaks effective attrs in resolve_effective_ports and
    carries no usage/dynamic_usage). A port present only in local_port_config is
    included."""
    pc = _expand_map(eff.get("port_config") or {})
    out: dict[str, dict[str, Any]] = {m: dict(a) for m, a in pc.items()}
    for member, attrs in _expand_map(eff.get("local_port_config") or {}).items():
        if _overridable(pc.get(member)):
            out[member] = {**out.get(member, {}), **attrs}
    return out
```

- [ ] **Step 5: Rework `resolve_effective_ports` for layered precedence + member-set union**

Replace `resolve_effective_ports` body (`ports.py:110-138`) with:

```python
def resolve_effective_ports(
    eff: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], str | None, str]]:
    """Yield (member, effective_usage, usage_name, resolution) per configured port.

    Layers (lowest -> highest precedence): named port_usages profile <- inline
    port_config attrs <- port_config_overwrite attrs <- local_port_config attrs
    (highest, applied only when the member is locally overridable). The member
    set is the union of all three maps, so an overwrite-only or local-only port
    still yields a port (resolution "none" when no usage name resolves).
    `resolution` states where the usage came from: "explicit"/"system"/
    "unresolved" (see usage_definition) or "none" (no usage name).
    """
    pc = _expand_map(eff.get("port_config") or {})
    overwrite = _expand_map(eff.get("port_config_overwrite") or {})
    local = _expand_map(eff.get("local_port_config") or {})
    bases = resolve_port_bases(eff)
    for member in sorted(set(bases) | set(overwrite)):
        usage_name = (bases.get(member) or {}).get("usage")
        effective: dict[str, Any]
        if usage_name is None:
            effective, resolution = {}, "none"
        else:
            effective, resolution = usage_definition(eff, str(usage_name))
        for key in _USAGE_OVERRIDE_ATTRS:  # port_config inline
            if key in pc.get(member, {}):
                effective[key] = pc[member][key]
        for key in _OVERWRITE_ATTRS:  # port_config_overwrite
            if key in overwrite.get(member, {}):
                effective[key] = overwrite[member][key]
        if _overridable(pc.get(member)):  # local_port_config (highest, gated)
            for key in _USAGE_OVERRIDE_ATTRS:
                if key in local.get(member, {}):
                    effective[key] = local[member][key]
        yield member, effective, (str(usage_name) if usage_name is not None else None), resolution
```

- [ ] **Step 6: Reconcile existing tests that encoded the OLD unconditional-local behavior**

Several tests across the suite assumed `local_port_config` always wins; under real Mist an override needs `no_local_overwrite: false` on the port_config entry, or a standalone port (no port_config entry). This is the fix, not a regression. Apply the known sites below, then run the FULL suite (next step) to catch any straggler — reconcile each by the same pattern (add `no_local_overwrite: false`, or make the port standalone).

**(A) In `tests/adapters/mist/test_ingest_ports.py`:**

`test_local_port_config_reassigns_usage` — change the `port_config` entry:
```python
    eff = _eff(
        port_config={"ge-0/0/7": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/7": {"usage": "uplink"}},
    )
```

`test_local_override_targets_one_member_of_a_range` — change the range entry:
```python
    eff = _eff(
        port_config={"ge-0/0/0-3": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/2": {"usage": "uplink"}},
    )
```

`test_resolve_port_bases_merges_local_over_port_config_and_keeps_dynamic_flag` — add the flag to the entry the local override targets:
```python
    eff = {
        "port_config": {
            "ge-0/0/0": {"usage": "office", "dynamic_usage": "dynamic"},
            "ge-0/0/1-2": {"usage": "office", "no_local_overwrite": False},
        },
        "local_port_config": {"ge-0/0/1": {"usage": "uplink"}},
    }
```

**(B) In `tests/test_plan3_flow.py`, `test_local_port_config_override_flows_through_to_proposed_ir` (~line 177):** the override targets `ge-0/0/1`, which is inside `SWITCH`'s `port_config` range `ge-0/0/0-1` (no `no_local_overwrite`). Adding the flag to the shared `SWITCH` fixture would ripple into other tests (a later port_config-replacing PUT would drop the flag → a `no_local_overwrite` change → UNKNOWN). Instead, retarget the override to a **standalone** port not in `port_config` (applies unconditionally), and update the assertion's port id to match:

```python
    new_device = {**SWITCH, "local_port_config": {"ge-0/0/5": {"usage": "uplink"}}}
    # ... and update the proposed-IR assertion below to check port "ge-0/0/5"
    #     (e.g. the port_id helper "<mac>:ge-0/0/5") instead of ge-0/0/1.
```

**(C) In `tests/adapters/mist/test_ingest_switch.py` (~line 717, the local-`stp_edge` test):** the `local_port_config` entry for `ge-0/0/3` reassigns inline `stp_edge` but `port_config["ge-0/0/3"]` has no flag — local would now be discarded. Add the flag to that inline `port_config` entry (self-contained `eff` dict, no fixture ripple):

```python
        "port_config": {
            "ge-0/0/1": {"usage": "nostp"},
            "ge-0/0/2": {"usage": "edge"},
            "ge-0/0/3": {"usage": "plain", "no_local_overwrite": False},
        },
        "local_port_config": {"ge-0/0/3": {"usage": "plain", "stp_edge": True}},
```

- [ ] **Step 7: Run the affected suites + full gate (catch any straggler)**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/test_plan3_flow.py tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS (new + reconciled tests).
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS. Any *other* failure is almost certainly another test encoding the old unconditional-local behavior — reconcile it with the same pattern (add `no_local_overwrite: false`, or make the port standalone). (If a golden churns, STOP — `site.json` has no `local_port_config` and should be unchanged; investigate before re-pinning.)

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/ports.py \
  tests/adapters/mist/test_ingest_ports.py \
  tests/test_plan3_flow.py \
  tests/adapters/mist/test_ingest_switch.py
git commit -m "$(cat <<'EOF'
fix(ports): rework port-config precedence — local_port_config highest, no_local_overwrite gated

port_config -> port_config_overwrite -> local_port_config (highest, per-member,
gated on no_local_overwrite == false; OAS default true => local discarded).
Member set now unions port_config_overwrite so overwrite-only ports resolve.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Honor inline `disabled` → `Port.disabled`

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/ports.py` (`_OVERWRITE_ATTRS`, `_LOCAL_ATTRS`)
- Test: `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`

**Interfaces:**
- Consumes: the Task-1 resolver layering; `ingest/switch.py:803` `disabled=bool(usage.get("disabled"))` (unchanged).
- Produces: inline `disabled` from `port_config_overwrite` / `local_port_config` now appears in the effective attrs; `port_config.*.disabled` is NOT honored.

- [ ] **Step 1: Write failing resolver tests**

Add to `tests/adapters/mist/test_ingest_ports.py`:

```python
def test_overwrite_disabled_marks_effective_disabled():
    # the bug-report shape: overwrite-only members carry disabled with no port_config entry
    eff = _eff(port_config_overwrite={"mge-0/0/0-3": {"disabled": True}})
    res = _resolved(eff)
    assert res["mge-0/0/0"][0].get("disabled") is True
    assert res["mge-0/0/3"][0].get("disabled") is True


def test_local_disabled_honored_when_overridable():
    eff = _eff(
        port_config={"ge-0/0/5": {"usage": "office", "no_local_overwrite": False}},
        local_port_config={"ge-0/0/5": {"disabled": True}},
    )
    assert _resolved(eff)["ge-0/0/5"][0].get("disabled") is True


def test_port_config_disabled_is_ignored():
    # disabled is NOT valid on port_config (OAS) -> the resolver must not honor it
    eff = _eff(port_config={"ge-0/0/6": {"usage": "office", "disabled": True}})
    assert _resolved(eff)["ge-0/0/6"][0].get("disabled") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py -k "disabled" -q`
Expected: FAIL (`disabled` not yet in the overwrite/local attr sets).

- [ ] **Step 3: Honor `disabled` on the overwrite + local layers**

`disabled` is OAS-valid on `port_config_overwrite` + `local_port_config` only (NOT `port_config`). In `ports.py`:

(a) add `disabled` to `_OVERWRITE_ATTRS`:
```python
_OVERWRITE_ATTRS = ("port_network", "poe_disabled", "disabled")
```
(b) add a local-only attr tuple just below `_OVERWRITE_ATTRS` (`_USAGE_OVERRIDE_ATTRS` already includes `stp_edge`):
```python
# local_port_config may additionally carry the admin-down boolean (OAS).
_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled")
```
(c) in `resolve_effective_ports`, change the local layer to iterate `_LOCAL_ATTRS` (it currently iterates `_USAGE_OVERRIDE_ATTRS`):
```python
        if _overridable(pc.get(member)):  # local_port_config (highest, gated)
            for key in _LOCAL_ATTRS:
                if key in local.get(member, {}):
                    effective[key] = local[member][key]
```
`disabled` is NOT added to the port_config layer (`_USAGE_OVERRIDE_ATTRS`), so `port_config.*.disabled` stays unhonored — matching the OAS and `test_port_config_disabled_is_ignored`.

- [ ] **Step 4: Write the ingest end-to-end test (resolver → Port.disabled)**

Add to `tests/adapters/mist/test_ingest_switch.py` (mirror the existing `test_disabled_usage_marks_port_disabled` style — it builds a device dict and asserts `ir.ports[...].disabled`):

```python
def test_inline_disabled_via_overwrite_marks_port_disabled():
    # overwrite-only port with disabled:true -> Port.disabled True (the bug shape)
    ir = _build_switch_ir(  # use this file's existing switch-IR builder helper
        port_usages={"office": {"mode": "access", "port_network": "corp"}},
        port_config={"ge-0/0/1": {"usage": "office"}},
        port_config_overwrite={"mge-0/0/0": {"disabled": True}},
        networks={"corp": {"vlan_id": 10}},
    )
    assert ir.ports["aa0000000001:mge-0/0/0"].disabled is True
    assert ir.ports["aa0000000001:ge-0/0/1"].disabled is False
```

NOTE to implementer: match the exact construction helper / device-mac prefix used by the sibling tests in `test_ingest_switch.py` (e.g. `test_disabled_usage_marks_port_disabled` at line ~79). If that file builds the device dict inline rather than via a helper, copy that inline shape; do not invent a helper that doesn't exist.

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/ports.py tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py
git commit -m "$(cat <<'EOF'
feat(ports): honor inline `disabled` on port_config_overwrite + local_port_config

Threads the admin-down boolean into effective attrs; ingest/switch.py already
reads it into Port.disabled. port_config.*.disabled stays unhonored (OAS).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `wired.port.admin_disable` check + registration

**Files:**
- Create: `src/digital_twin/checks/wired/admin_disable.py`
- Modify: `src/digital_twin/checks/wired/__init__.py`
- Modify: `tests/test_public_api.py:187`
- Test: `tests/checks/test_admin_disable.py`

**Interfaces:**
- Consumes: `CheckContext` (`.baseline.ir`, `.proposed.ir`, `.delta_index.cause("port", pid)`); `_ap_uplink_ports(ir) -> dict[sw_port_id, tuple[ap_id, Link]]` (imported from `poe_disconnect`); `clients_by_port(ir)`, `clients_by_ap(ir)` (`ir.indexes`); `Port.disabled`, `Port.mode`, `Link.meta.confidence`.
- Produces: `AdminDisableCheck` with `id = "wired.port.admin_disable"`; finding codes `wired.port.admin_disable.{impact,edge,unattributable}`.

- [ ] **Step 1: Write the failing check tests**

Create `tests/checks/test_admin_disable.py`:

```python
"""wired.port.admin_disable: administratively disabling a switch port. ERROR
(UNSAFE) when a HIGH-confidence AP uplink is cut; WARNING (REVIEW) for a
MEDIUM AP tie, a trunk/inter-switch link, or a port with active wired clients;
INFO (context) for a bare edge port or a prop-only port with no baseline state.
Pre-existing-disabled and re-enable are not flagged."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.admin_disable import AdminDisableCheck
from digital_twin.contracts import Severity
from digital_twin.ir import ConfidenceLevel, IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.provenance import Provenance
from tests.factories import ap, link, sw, wired_client


def _run(base, prop):
    return AdminDisableCheck().run(
        CheckContext(
            baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)
        )
    )


def _ap_ir(*, disabled, prov=Provenance.LLDP_TWO_SIDED):
    b = IRBuilder().add_device(sw("S")).add_device(ap("A"))
    b.add_port(Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.TRUNK,
                    disabled=disabled))
    b.add_port(Port(id="A:eth0", device_id="A", name="eth0", mode=PortMode.TRUNK))
    b.add_link(link("S:ge-0/0/1", "A:eth0", prov=prov))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_disabling_a_high_confidence_ap_uplink_is_unsafe():
    r = _run(_ap_ir(disabled=False), _ap_ir(disabled=True))
    assert r.status is Status.FAIL
    f = r.findings[0]
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert "A" in f.affected_entities


def test_medium_ap_tie_is_warning_not_unsafe():
    # one-sided/inferred tie -> WARNING even though it's an AP (decide() floors
    # UNSAFE on any network ERROR before confidence, so ERROR needs a HIGH tie)
    r = _run(_ap_ir(disabled=False, prov=Provenance.INFERRED),
             _ap_ir(disabled=True, prov=Provenance.INFERRED))
    assert r.status is Status.WARN
    assert r.findings[0].severity is Severity.WARNING


def _edge_ir(*, disabled, mode=PortMode.ACCESS, with_client=False):
    b = IRBuilder().add_device(sw("S"))
    b.add_port(Port(id="S:ge-0/0/2", device_id="S", name="ge-0/0/2", mode=mode, disabled=disabled))
    if with_client:
        b.add_client(wired_client("cc:01", "S:ge-0/0/2", vlan=10))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_disabling_a_bare_edge_port_is_info_context():
    r = _run(_edge_ir(disabled=False), _edge_ir(disabled=True))
    assert r.status is Status.PASS  # INFO does not floor
    assert r.findings[0].severity is Severity.INFO


def test_disabling_a_trunk_edge_port_is_review():
    r = _run(_edge_ir(disabled=False, mode=PortMode.TRUNK),
             _edge_ir(disabled=True, mode=PortMode.TRUNK))
    assert r.status is Status.WARN
    assert r.findings[0].severity is Severity.WARNING


def test_nonap_peer_uses_link_confidence_not_high():
    # an inter-switch ACCESS port with a one-sided LLDP peer -> WARNING, but the
    # finding's confidence is the LINK's (LOW), not overstated HIGH (P3)
    def ir(disabled):
        b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
        b.add_port(Port(id="S:ge-0/0/3", device_id="S", name="ge-0/0/3",
                        mode=PortMode.ACCESS, disabled=disabled))
        b.add_port(Port(id="T:ge-0/0/3", device_id="T", name="ge-0/0/3", mode=PortMode.ACCESS))
        b.add_link(link("S:ge-0/0/3", "T:ge-0/0/3", prov=Provenance.LLDP_ONE_SIDED))
        b.with_capability(IRCapability.WIRED_L2)
        return b.build()

    r = _run(ir(False), ir(True))
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.LOW  # link confidence, not HIGH


def test_disabling_a_port_with_active_wired_clients_is_review():
    r = _run(_edge_ir(disabled=False, with_client=True),
             _edge_ir(disabled=True, with_client=True))
    assert r.status is Status.WARN
    assert r.findings[0].severity is Severity.WARNING


def test_prop_only_disabled_port_is_info_unattributable():
    # no baseline Port for this pid -> INFO (blast radius unattributable), NOT skipped
    base = IRBuilder().add_device(sw("S")).with_capability(IRCapability.WIRED_L2).build()
    prop = IRBuilder().add_device(sw("S"))
    prop.add_port(Port(id="S:mge-0/0/0", device_id="S", name="mge-0/0/0",
                       mode=PortMode.ACCESS, disabled=True))
    prop.with_capability(IRCapability.WIRED_L2)
    r = _run(base, prop.build())
    assert r.status is Status.PASS
    assert len(r.findings) == 1
    assert r.findings[0].severity is Severity.INFO
    assert r.findings[0].code == "wired.port.admin_disable.unattributable"


def test_already_disabled_is_not_flagged():
    assert _run(_edge_ir(disabled=True), _edge_ir(disabled=True)).findings == ()


def test_re_enable_is_not_flagged():
    assert _run(_edge_ir(disabled=True), _edge_ir(disabled=False)).findings == ()


def test_caused_by_points_at_the_port():
    r = _run(_ap_ir(disabled=False), _ap_ir(disabled=True))
    f = r.findings[0]
    assert f.caused_by and f.caused_by[0].ref.kind == "port"
    assert f.caused_by[0].ref.id == "S:ge-0/0/1"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/checks/test_admin_disable.py -q`
Expected: FAIL with `ModuleNotFoundError: ...admin_disable`.

- [ ] **Step 3: Implement the check**

Create `src/digital_twin/checks/wired/admin_disable.py`:

```python
"""wired.port.admin_disable — administratively disabling a switch port.

Disabling a port (inline `disabled` on local_port_config / port_config_overwrite,
or a `usage: "disabled"` reassignment) takes the link down: Port.disabled
forwards NOTHING and the L2 graph drops its edge, so a disabled trunk/uplink
strands downstream segments (wired.l2.blackhole). This check surfaces the ACTION
and weights it by blast radius — an AP uplink, an inter-switch/trunk link, or a
port with active wired clients floors REVIEW (or UNSAFE at HIGH confidence); a
bare edge port is INFO context.

ERROR is emitted ONLY when the port<->peer tie is HIGH-confidence: decide()
floors UNSAFE on any network ERROR before consulting confidence, so a
MEDIUM/one-sided LLDP tie caps at WARNING even when wireless clients are observed
on the AP (observed clients raise the consequence, not the tie). Complementary to
wired.l2.blackhole (action vs consequence); both may fire on one delta.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.checks.wired.poe_disconnect import _ap_uplink_ports
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    Link,
    min_confidence,
)
from digital_twin.ir.entities import Client, DeviceRole, Port, PortMode
from digital_twin.ir.indexes import clients_by_ap, clients_by_port
from digital_twin.ir.model import IR

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
# (severity, confidence, finding code, message, headline subject)
_Verdict = tuple[Severity, Confidence, str, str, ObjectRef]


def _nonap_peer_links(ir: IR) -> dict[str, Link]:
    """switch-port id -> the baseline link to a managed NON-AP peer (inter-switch
    / gateway uplink). Carries the LINK so classification can use its confidence —
    a one-sided LLDP peer is weaker evidence than a two-sided one."""
    out: dict[str, Link] = {}
    for lk in ir.links:
        pa, pb = ir.ports.get(lk.a_port), ir.ports.get(lk.b_port)
        if pa is None or pb is None:
            continue
        a_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        if not a_ap and not b_ap:
            out[pa.id] = lk
            out[pb.id] = lk
    return out


class AdminDisableCheck:
    id = "wired.port.admin_disable"
    title = "Administratively disabling a switch port"
    domain = "wired.port"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("port")

    def run(self, ctx: CheckContext) -> CheckResult:
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        ap_ports = _ap_uplink_ports(base_ir)
        nonap_peers = _nonap_peer_links(base_ir)
        wired = clients_by_port(base_ir)
        ap_clients = clients_by_ap(base_ir)
        findings: list[Finding] = []
        for pid, prop_port in prop_ir.ports.items():
            if not prop_port.disabled:
                continue
            base_port = base_ir.ports.get(pid)
            if base_port is not None and base_port.disabled:
                continue  # already disabled -> not the delta
            findings.append(
                self._finding(ctx, pid, base_port, ap_ports, nonap_peers, wired, ap_clients)
            )
        worst = Status.PASS
        for f in findings:
            if f.severity is Severity.ERROR:
                worst = Status.FAIL
            elif f.severity is Severity.WARNING and worst is Status.PASS:
                worst = Status.WARN
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=min_confidence(*(f.confidence for f in findings)) if findings else _HIGH,
            reasoning="compared per-port admin-disable state baseline vs proposed",
        )

    def _finding(
        self,
        ctx: CheckContext,
        pid: str,
        base_port: Port | None,
        ap_ports: dict[str, tuple[str, Link]],
        nonap_peers: dict[str, Link],
        wired: dict[str, list[Client]],
        ap_clients: dict[str, list[Client]],
    ) -> Finding:
        severity, confidence, code, message, subject = self._classify(
            pid, base_port, ap_ports, nonap_peers, wired, ap_clients
        )
        return Finding(
            source=FindingSource.CHECK,
            category=FindingCategory.NETWORK,
            code=code,
            severity=severity,
            confidence=confidence,
            message=message,
            affected_entities=(subject.id,),
            subject=subject,
            evidence={"port": pid, "disabled": True},
            caused_by=tuple(c for c in (ctx.delta_index.cause("port", pid),) if c is not None),
        )

    def _classify(
        self,
        pid: str,
        base_port: Port | None,
        ap_ports: dict[str, tuple[str, Link]],
        nonap_peers: dict[str, Link],
        wired: dict[str, list[Client]],
        ap_clients: dict[str, list[Client]],
    ) -> _Verdict:
        port_ref = ObjectRef("port", pid)
        if base_port is None:
            return (
                Severity.INFO, _HIGH, "wired.port.admin_disable.unattributable",
                f"port {pid} administratively disabled — no baseline state, blast radius unknown",
                port_ref,
            )
        ap = ap_ports.get(pid)
        if ap is not None:
            ap_id, lk = ap
            conf = lk.meta.confidence
            high = conf.level is ConfidenceLevel.HIGH
            n = len(ap_clients.get(ap_id, []))
            return (
                Severity.ERROR if high else Severity.WARNING, conf,
                "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — AP {ap_id} ({n} observed "
                "wireless client(s)) loses its uplink",
                ObjectRef("device", ap_id),
            )
        n_wired = len(wired.get(pid, []))
        if n_wired:
            return (
                Severity.WARNING, _HIGH, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — {n_wired} active wired client(s) "
                "disconnect",
                port_ref,
            )
        if base_port.mode is PortMode.TRUNK:
            # config trunk is a HIGH fact
            return (
                Severity.WARNING, _HIGH, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — a trunk link goes down",
                port_ref,
            )
        peer_lk = nonap_peers.get(pid)
        if peer_lk is not None:
            # peer-only tie: confidence is the LINK's (a one-sided LLDP peer is weak)
            return (
                Severity.WARNING, peer_lk.meta.confidence, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — an inter-switch / gateway link goes down",
                port_ref,
            )
        return (
            Severity.INFO, _HIGH, "wired.port.admin_disable.edge",
            f"port {pid} administratively disabled — edge port, no downstream impact modeled",
            port_ref,
        )
```

- [ ] **Step 4: Run the check tests**

Run: `uv run pytest tests/checks/test_admin_disable.py -q`
Expected: PASS. (`mypy src` must be clean — `_finding`/`_classify`/`_nonap_peer_links` are fully annotated and `_Verdict` is the shared return alias; mypy is not enforced on `tests/`.)

- [ ] **Step 5: Register the check**

In `src/digital_twin/checks/wired/__init__.py`: add the import (alphabetical, before `bgp_adjacency`):
```python
from .admin_disable import AdminDisableCheck
```
Add to `ALL_WIRED_CHECKS` (place after `PoeDisconnectCheck()` — the other port/PoE check):
```python
    AdminDisableCheck(),
```
Add to `__all__` (alphabetical, after `"ALL_WIRED_CHECKS",`):
```python
    "AdminDisableCheck",
```

- [ ] **Step 6: Bump the public-API count**

In `tests/test_public_api.py:187` change:
```python
    assert len(ALL_WIRED_CHECKS) == 21
```

- [ ] **Step 7: Run tests + gate**

Run: `uv run pytest tests/checks/test_admin_disable.py tests/test_public_api.py tests/checks/test_registry.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/checks/wired/admin_disable.py src/digital_twin/checks/wired/__init__.py tests/checks/test_admin_disable.py tests/test_public_api.py
git commit -m "$(cat <<'EOF'
feat(checks): wired.port.admin_disable — surface admin port-disable by blast radius

AP uplink (HIGH tie) -> ERROR/UNSAFE; MEDIUM tie / trunk / inter-switch / active
wired clients -> WARNING/REVIEW; bare edge or prop-only port -> INFO. Mirrors
poe_disconnect; complementary to l2.blackhole.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Field-gate leaves (`disabled` in scope; `no_local_overwrite` NOT)

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py:150-157` (`_LOCAL_PORT_CONFIG_LEAVES`, `_OVERWRITE_LEAVES`)
- Test: `tests/scope/test_allowlist.py` (or `tests/scope/test_field_gate.py` — match where leaf-membership is asserted)

**Interfaces:**
- Consumes: `RAW_ALLOWLIST["device"]`, `EFFECTIVE_ALLOWLIST`, `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"]` — all compose `_DEVICE_PORT_LEAVES`, so adding to the two tuples propagates automatically.
- Produces: `local_port_config.*.disabled` and `port_config_overwrite.*.disabled` are in scope; `port_config.*.disabled` and `port_config.*.no_local_overwrite` remain OUT of scope.

- [ ] **Step 1: Write failing scope tests**

Add to `tests/scope/test_allowlist.py`:

```python
from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST, RAW_ALLOWLIST


def test_disabled_in_scope_on_overwrite_and_local():
    dev = set(RAW_ALLOWLIST["device"])
    assert "port_config_overwrite.*.disabled" in dev
    assert "local_port_config.*.disabled" in dev
    assert "port_config_overwrite.*.disabled" in set(EFFECTIVE_ALLOWLIST)


def test_disabled_not_in_scope_on_port_config():
    assert "port_config.*.disabled" not in set(RAW_ALLOWLIST["device"])


def test_no_local_overwrite_stays_out_of_scope():
    # a lone no_local_overwrite flip could activate unmodeled local leaves -> UNKNOWN
    assert "port_config.*.no_local_overwrite" not in set(RAW_ALLOWLIST["device"])


def test_local_dynamic_usage_still_out_of_scope():
    # P1 regression: adding `disabled` must NOT reintroduce local dynamic_usage,
    # which PR #14 deliberately narrowed out (it's a port_config-only pointer)
    assert "local_port_config.*.dynamic_usage" not in set(RAW_ALLOWLIST["device"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/scope/test_allowlist.py -k "disabled or no_local_overwrite" -q`
Expected: FAIL (leaves not present).

- [ ] **Step 3: Add the `disabled` leaves**

The worktree's current `_LOCAL_PORT_CONFIG_LEAVES` (line ~154) is
`("usage", "stp_edge", *_MODELED_USAGE_ATTRS)` — PR #14 deliberately dropped
`dynamic_usage` from local (it is a port_config-only pointer; pinned out-of-scope
by `tests/scope/test_field_gate.py`). **Add ONLY `disabled`; do NOT reintroduce
`dynamic_usage`:**
```python
_LOCAL_PORT_CONFIG_LEAVES: tuple[str, ...] = tuple(
    f"local_port_config.*.{a}"
    for a in ("usage", "stp_edge", "disabled", *_MODELED_USAGE_ATTRS)
)
```
And edit `_OVERWRITE_LEAVES` (line ~157):
```python
_OVERWRITE_LEAVES: tuple[str, ...] = (
    "port_config_overwrite.*.port_network",
    "port_config_overwrite.*.poe_disabled",
    "port_config_overwrite.*.disabled",
)
```

- [ ] **Step 4: Run tests + gate**

Run: `uv run pytest tests/scope/ -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/scope/allowlist.py tests/scope/test_allowlist.py
git commit -m "$(cat <<'EOF'
feat(scope): allowlist `disabled` on port_config_overwrite + local_port_config

Brings admin port-disable in scope (modeled by wired.port.admin_disable).
no_local_overwrite stays out of scope (false-SAFE guard); port_config.*.disabled
stays flagged (not in OAS).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: End-to-end pipeline + goldens + ROADMAP

**Files:**
- Test: `tests/engine/test_pipeline.py`
- Modify: `docs/ROADMAP.md`

**Interfaces:**
- Consumes: the existing pipeline test harness in `tests/engine/test_pipeline.py` (find the existing switch device-update test, e.g. the OAS `disabled`-on-switch case added by the prior feature, and mirror its fixture/builder + assertion helpers — do NOT invent a new harness).
- Produces: an e2e proof that the bug-report payload resolves to a real verdict, not UNKNOWN.

- [ ] **Step 1: Write the failing e2e test**

Add to `tests/engine/test_pipeline.py`, mirroring the existing switch device-update test's fixture construction (baseline device config + a `ChangePlan`/op carrying the payload). The baseline MUST give the disabled ports a `Port` (e.g. a trunk uplink) so the verdict is REVIEW/UNSAFE rather than INFO/SAFE:

```python
def test_port_config_overwrite_disable_is_simulated_not_unknown():
    # the reported bug: disabling ports via port_config_overwrite must simulate
    # (admin_disable + blackhole), not return UNKNOWN. Baseline has these ports
    # as a trunk uplink so the blast radius is real.
    payload = {"port_config_overwrite": {"ge-0/0/1": {"disabled": True}}}
    result = _simulate_switch_update(payload)  # this file's existing e2e helper
    assert result.decision is not Decision.UNKNOWN
    codes = {f.code for f in result.findings}
    assert "wired.port.admin_disable.impact" in codes
    assert result.decision in (Decision.REVIEW, Decision.UNSAFE)
```

NOTE to implementer: use this file's real helper names/imports (`Decision`, the simulate entry point, the fixture builder). If the existing switch e2e test disables a port via `usage` rather than `port_config_overwrite`, copy its baseline scaffold and only swap the payload. Choose a baseline port id that exists as a trunk/uplink in that fixture.

- [ ] **Step 2: Run to verify it fails for the RIGHT reason**

Run: `uv run pytest tests/engine/test_pipeline.py -k "port_config_overwrite_disable" -q`
Expected: FAIL — but assert the failure is the *assertion* (e.g. decision currently UNKNOWN or finding missing), NOT an import/setup error. Fix harness wiring if it errors out.

- [ ] **Step 3: Confirm it passes (no production code change expected)**

Tasks 1–4 already deliver the behavior; this task proves it end-to-end. If the test fails on the assertion, debug whether the baseline fixture gives the port a baseline `Port` (else INFO/SAFE) and whether the gate/resolver wiring reaches the check.

Run: `uv run pytest tests/engine/test_pipeline.py -k "port_config_overwrite_disable" -q`
Expected: PASS.

- [ ] **Step 4: Run the FULL golden suite; investigate any churn before re-pinning**

Run: `uv run pytest tests/golden/ -q`
Expected: PASS unchanged. `site.json` has no `local_port_config` and its only `port_config_overwrite` entry (`ge-0/0/6: {description}`) carries no modeled attr, so no golden should churn. **If a golden changes, STOP and diff it** — confirm the change is a genuine consequence of the precedence fix (justified) before updating the expected output; never re-pin blindly.

- [ ] **Step 5: ROADMAP entry**

In `docs/ROADMAP.md`, add under the most recent completed entries:
```markdown
- ✅ Switch port admin-disable + port-config precedence rework (SP1 of the port-config attribute-modeling program) — done 2026-06-25
```

- [ ] **Step 6: Full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/engine/test_pipeline.py docs/ROADMAP.md
git commit -m "$(cat <<'EOF'
test(port-disable): e2e port_config_overwrite disable simulates (not UNKNOWN); roadmap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §1 resolver precedence + `no_local_overwrite` gate + member-set union → Task 1. ✓
- §2 inline `disabled` → `Port.disabled` → Task 2. ✓
- §3 `wired.port.admin_disable` (proposed-port iteration, base-missing INFO, AP/trunk/non-AP-peer/wired-client tiers, HIGH-only ERROR rail, registration + public-API bump) → Task 3. ✓
- §4 field-gate `disabled` leaves; `no_local_overwrite` stays UNKNOWN → Task 4. ✓
- §5 L0 no change → no task needed (asserted in spec). ✓
- Testing (resolver unit, check unit, public API, e2e, goldens) → Tasks 1–5. ✓

**Placeholder scan:** No TBD/TODO. Two "NOTE to implementer" blocks point at *existing* harness/helpers to mirror (the sibling switch-ingest test and the pipeline e2e helper) rather than leaving code blank — necessary because those helpers' exact names live in files not fully quoted here; the surrounding test code is complete.

**Type consistency:** `resolve_effective_ports` 4-tuple shape unchanged; `_overridable(pc_member)` used identically in `resolve_port_bases` and `resolve_effective_ports`; check uses `_ap_uplink_ports` (imported) and `clients_by_port`/`clients_by_ap`/`_nonap_peer_links` (dict, P3) consistently; `_finding`/`_classify` fully annotated returning the `_Verdict` alias (P2b); finding codes `wired.port.admin_disable.{impact,edge,unattributable}` match between impl and tests; `ALL_WIRED_CHECKS` count 20→21 matches the verified current value.

**Review-round corrections (baselined against the worktree `origin/main`, post-PR#14):**
- **P1** — Task 4 adds ONLY `disabled` to `_LOCAL_PORT_CONFIG_LEAVES` (`("usage", "stp_edge", "disabled", *_MODELED_USAGE_ATTRS)`); `dynamic_usage` stays out (regression-pinned). The worktree resolver (`ports.py`) is unchanged by PR#14, so Task 1 keeps `_USAGE_OVERRIDE_ATTRS` as-is.
- **P2** — Task 1 reconciles the cross-file old-behavior tests (`test_plan3_flow.py` → standalone port; `test_ingest_switch.py:717` → `no_local_overwrite: false`) plus a full-suite straggler sweep.
- **P2b** — all `admin_disable.py` helpers are typed (`mypy src` strict-clean).
- **P3** — `_nonap_peer_links` carries the `Link`; peer-only classification uses the link's confidence (one-sided LLDP → LOW), trunk uses HIGH config confidence; regression test added.
- **OAS** — re-verified against the refreshed worktree OAS: `disabled` present on overwrite/local (closed), absent on port_config; `no_local_overwrite` on port_config. L0 unchanged. ✓
