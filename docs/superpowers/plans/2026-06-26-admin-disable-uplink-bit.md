# `admin_disable` + `Port.is_uplink` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Model the Mist port-stat `uplink` bit as `Port.is_uplink`, and stop `wired.port.admin_disable` from warning on a disabled trunk that has no modeled uplink or downstream.

**Architecture:** Add an observational `Port.is_uplink` fact (diff-isolated), read it from the already-fetched `port_stats` in the LLDP ingester independently of the STP guard, and reorder `admin_disable`'s classification so a modeled peer link is weighed first (at the link's confidence) and a trunk drops to INFO only on explicit `is_uplink == False` with no peer/AP/client.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy (strict on `src`, not tests).

## Global Constraints

- **Never-false-SAFE:** the WARNING→INFO demotion fires ONLY on `base_port.is_uplink is False` AND no modeled peer link AND (already excluded by earlier branches) no AP and no wired clients. `is_uplink` absent (`None`) or `True` → stays conservative WARNING.
- **Strict bool typing — no truthiness:** `Port.is_uplink` is set from a `port_stats` row's `uplink` value ONLY when `type(value) is bool`; any other shape (`0`, `""`, `"false"`, absent) → `is_uplink = None`.
- **`is_uplink` is observational and diff-isolated:** it must be in `_IGNORED_BY_KIND["port"]` so a change in the observed bit is never a config change and never wakes a check. (`Port` fields diff by default; `stp_state` IS diff-bearing — `is_uplink` must be explicitly ignored.)
- **Capability earning unchanged:** only `stp_state` earns `IRCapability.STP_STATE`; `is_uplink` earns nothing.
- **Gate (run before every commit that touches `src`):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Pyright/IDE diagnostics are noise.
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/digital_twin/ir/entities.py` **(modify)** — add `Port.is_uplink: bool | None = None`.
- `src/digital_twin/ir/diff.py` **(modify)** — add `"port": frozenset({"is_uplink"})` to `_IGNORED_BY_KIND`.
- `src/digital_twin/adapters/mist/ingest/lldp.py` **(modify)** — new `_apply_port_uplink` pass + call it from `ingest()`.
- `src/digital_twin/checks/wired/admin_disable.py` **(modify)** — reordered `_classify`; docstring summary.
- Tests in `tests/ir/test_diff.py`, `tests/adapters/mist/test_ingest_lldp.py`, `tests/checks/test_admin_disable.py`.

---

## Task 1: `Port.is_uplink` field + diff isolation

**Files:**
- Modify: `src/digital_twin/ir/entities.py`
- Modify: `src/digital_twin/ir/diff.py`
- Test: `tests/ir/test_diff.py`

**Interfaces:**
- Produces: `Port.is_uplink: bool | None` (default `None`) — observational; ignored by `diff_ir` for kind `port`.

- [ ] **Step 1: Write the failing test**

Append to `tests/ir/test_diff.py` (it already imports `diff_ir`, `IRBuilder`, `Port`, `PortMode` — confirm and add any missing import):

```python
def test_is_uplink_only_change_is_not_a_modification():
    # is_uplink is observational evidence, NOT a config change -> empty diff,
    # so flipping the observed uplink bit never wakes a check.
    base = IRBuilder().add_device(_dev("S")).add_port(
        Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.TRUNK,
             is_uplink=True)
    ).build()
    proposed = IRBuilder().add_device(_dev("S")).add_port(
        Port(id="S:ge-0/0/1", device_id="S", name="ge-0/0/1", mode=PortMode.TRUNK,
             is_uplink=False)
    ).build()
    assert diff_ir(base, proposed).is_empty()
```

> Use whatever device/port construction the neighbouring `test_diff.py` tests use — if there is a local `_dev(...)`/`sw(...)` helper or a `tests.factories` import already in the file, reuse it; otherwise build the `Device` inline as the other tests do. The point is two ports identical except `is_uplink`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ir/test_diff.py -k is_uplink -v`
Expected: FAIL — either `Port` has no `is_uplink` kwarg (TypeError) or the diff is non-empty (the field is compared).

- [ ] **Step 3: Add the field**

In `src/digital_twin/ir/entities.py`, in the `Port` dataclass, add the field in the OBSERVED block (next to `observed_speed`/`observed_duplex`):

```python
    # OBSERVED: Mist port-stat `uplink` bit — True = faces the core/uplink,
    # False = edge/leaf, None = not observed. Evidence-only (diff-isolated): it
    # weights admin_disable, never a config change.
    is_uplink: bool | None = None
```

- [ ] **Step 4: Add the diff isolation**

In `src/digital_twin/ir/diff.py`, add the `port` entry to `_IGNORED_BY_KIND`:

```python
_IGNORED_BY_KIND: dict[str, frozenset[str]] = {
    "device": frozenset({"name"}),
    "wlan": frozenset({"inherited"}),
    "bgp_peer": frozenset({"session_name"}),
    "port": frozenset({"is_uplink"}),
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/ir/test_diff.py -k is_uplink -v`
Expected: PASS.

- [ ] **Step 6: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/ir/entities.py src/digital_twin/ir/diff.py tests/ir/test_diff.py
git commit -m "feat(ir): Port.is_uplink observational fact, diff-isolated

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Ingest the `uplink` bit (independent of STP, strict bool)

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/lldp.py`
- Test: `tests/adapters/mist/test_ingest_lldp.py`

**Interfaces:**
- Consumes: `Port.is_uplink` (Task 1).
- Produces: `Port.is_uplink` set from `port_stats[*].uplink` (strict bool); no capability earned.

- [ ] **Step 1: Write the failing tests**

Append to `tests/adapters/mist/test_ingest_lldp.py` (reuses the existing `_ctx`, `SWITCH_A`, `port_id`, `Port`, `IRBuilder`). Add `IRCapability` to the imports from `digital_twin.ir`.

```python
def _port(ir, did, name):
    return ir.ports[port_id(did, name)]


def test_uplink_bit_sets_is_uplink_independent_of_stp():
    # a row with uplink=True and NO stp_state still annotates the port
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "uplink": True}]
    ir = _ctx(stats).builder.build()
    assert _port(ir, "aa0000000001", "ge-0/0/47").is_uplink is True


def test_uplink_false_is_recorded_as_false():
    stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "uplink": False}]
    ir = _ctx(stats).builder.build()
    assert _port(ir, "aa0000000001", "ge-0/0/47").is_uplink is False


def test_non_bool_uplink_stays_unknown():
    # strict typing: a drifted/non-bool shape must read as None, never coerced
    for bad in ("false", 0, 1, "", "true"):
        stats = [{"mac": "aa0000000001", "port_id": "ge-0/0/47", "up": True, "uplink": bad}]
        ir = _ctx(stats).builder.build()
        assert _port(ir, "aa0000000001", "ge-0/0/47").is_uplink is None, bad


def test_uplink_only_row_earns_no_stp_capability():
    # an uplink-bearing row with no stp_state must NOT earn STP_STATE
    ctx = _ctx_for_caps([{"mac": "aa0000000001", "port_id": "ge-0/0/47",
                          "up": True, "uplink": True}])
    caps = LldpIngester().ingest(ctx)
    assert IRCapability.STP_STATE not in caps
    assert _port(ctx.builder.build(), "aa0000000001", "ge-0/0/47").is_uplink is True
```

Add a `_ctx_for_caps` helper that mirrors `_ctx` but stops BEFORE the final `LldpIngester().ingest(ctx)` (so the test can call it and capture the returned capability set). Copy `_ctx`'s body verbatim and delete only its last two lines (the `LldpIngester().ingest(ctx)` call and the `return ctx` becomes `return ctx` without the ingest):

```python
def _ctx_for_caps(port_stats, device_stats=()):
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, SWITCH_B, AP_1),
                     port_stats=tuple(port_stats), device_stats=tuple(device_stats)),
        site_effective=dict(SITE_EFFECTIVE), device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    for did, name in (("aa0000000001", "ge-0/0/47"), ("bb0000000002", "ge-0/0/47"),
                      ("aa0000000001", "ge-0/0/10")):
        pid = port_id(did, name)
        if not ctx.builder.has_port(pid):
            ctx.builder.add_port(Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK))
    return ctx
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/adapters/mist/test_ingest_lldp.py -k "uplink" -v`
Expected: FAIL — `is_uplink` is never set (stays `None`) because nothing reads the bit yet.

- [ ] **Step 3: Implement `_apply_port_uplink`**

In `src/digital_twin/adapters/mist/ingest/lldp.py`, add a pass mirroring `_apply_stp` but gated on a strict-bool `uplink` (NOT on `stp_state`):

```python
    def _apply_port_uplink(self, ctx: IngestContext) -> None:
        for row in ctx.raw.port_stats:
            val = row.get("uplink")
            if type(val) is not bool or not row.get("port_id"):
                continue  # strict bool only; non-bool/absent -> leave is_uplink None
            pid = port_id(device_id(str(row["mac"])), str(row["port_id"]))
            self._ensure_port(ctx, pid)
            ctx.builder.replace_port(replace(ctx.builder.get_port(pid), is_uplink=val))
```

Call it from `ingest()`, right after `_apply_stp` (it earns no capability, so it does not affect the returned frozenset):

```python
    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        claims = self._claims(ctx)
        stp_seen = self._apply_stp(ctx)
        self._apply_port_uplink(ctx)
        emitted: set[str] = set()
        self._emit_links(ctx, claims, emitted)
        self._emit_ap_uplinks(ctx, claims, emitted)
        return frozenset({IRCapability.STP_STATE}) if stp_seen else frozenset()
```

> `_ensure_port`, `replace`, `port_id`, `device_id` are already imported/used by `_apply_stp` in this module — reuse them, add no new imports beyond what's present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/adapters/mist/test_ingest_lldp.py -k "uplink" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/adapters/mist/ingest/lldp.py tests/adapters/mist/test_ingest_lldp.py
git commit -m "feat(ingest): apply Mist port-stat uplink bit to Port.is_uplink (strict bool, STP-independent)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `admin_disable` reordered classification

**Files:**
- Modify: `src/digital_twin/checks/wired/admin_disable.py`
- Test: `tests/checks/test_admin_disable.py`

**Interfaces:**
- Consumes: `Port.is_uplink`, `nonap_peers` (existing), `ap_ports`, `wired` (existing).

The current `_classify` (admin_disable.py) checks, in order: `base_port is None`; AP port; wired clients; **TRUNK (blanket WARNING/_HIGH)**; `peer_lk` (nonap_peers); edge INFO. The TRUNK catch-all fires before `peer_lk`, so a linked trunk never gets the link's confidence, and an unconnected trunk is wrongly WARNING. Reorder: move `peer_lk` BEFORE the trunk branch, and make the trunk branch uplink-aware.

- [ ] **Step 1: Write the failing tests**

Append to `tests/checks/test_admin_disable.py` (reuses `_run`, `sw`, `link`, `Port`, `PortMode`, `Provenance`, `IRBuilder`, `IRCapability`, `Severity`, `ConfidenceLevel`). Build small IRs directly.

```python
def _trunk_ir(*, disabled, is_uplink, with_peer_link):
    """S:up is a TRUNK with no AP and no wired clients. Optionally a peer link to
    a 2nd switch, and an observed is_uplink bit."""
    b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
    up = Port(id="S:up", device_id="S", name="up", mode=PortMode.TRUNK,
              disabled=disabled, is_uplink=is_uplink)
    b.add_port(up)
    if with_peer_link:
        b.add_port(Port(id="T:down", device_id="T", name="down", mode=PortMode.TRUNK))
        b.add_link(link("S:up", "T:down", prov=Provenance.LLDP_TWO_SIDED))
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def test_unconnected_non_uplink_trunk_is_info():
    # is_uplink False, no peer link, no AP, no clients -> demoted to INFO
    res = _run(_trunk_ir(disabled=False, is_uplink=False, with_peer_link=False),
               _trunk_ir(disabled=True, is_uplink=False, with_peer_link=False))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.INFO
    assert f.code == "wired.port.admin_disable.edge"


def test_uplink_true_trunk_stays_warning():
    res = _run(_trunk_ir(disabled=False, is_uplink=True, with_peer_link=False),
               _trunk_ir(disabled=True, is_uplink=True, with_peer_link=False))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.WARNING


def test_unknown_uplink_trunk_stays_warning_conservative():
    # is_uplink None (absent bit) -> conservative WARNING, never demoted
    res = _run(_trunk_ir(disabled=False, is_uplink=None, with_peer_link=False),
               _trunk_ir(disabled=True, is_uplink=None, with_peer_link=False))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.WARNING


def test_linked_trunk_warns_at_link_confidence_even_if_not_uplink():
    # a modeled two-sided peer link -> WARNING at the LINK's confidence (HIGH),
    # NOT demoted, even though is_uplink is False
    res = _run(_trunk_ir(disabled=False, is_uplink=False, with_peer_link=True),
               _trunk_ir(disabled=True, is_uplink=False, with_peer_link=True))
    f = next(f for f in res.findings if f.evidence.get("port") == "S:up")
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.HIGH
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/checks/test_admin_disable.py -k "trunk" -v`
Expected: `test_unconnected_non_uplink_trunk_is_info` FAILS (today the trunk branch returns WARNING regardless). The others may already pass.

- [ ] **Step 3: Reorder `_classify`**

In `src/digital_twin/checks/wired/admin_disable.py`, replace the `if base_port.mode is PortMode.TRUNK: ...` block AND the following `peer_lk = ...` block (the trunk catch-all currently precedes the peer check) with this — peer link first, then the uplink-aware trunk branch:

```python
        peer_lk = nonap_peers.get(pid)
        if peer_lk is not None:
            # a modeled inter-switch / gateway link: confidence is the LINK's
            # (a one-sided LLDP peer is weaker than a two-sided one)
            return (
                Severity.WARNING, peer_lk.meta.confidence, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — an inter-switch / gateway link goes down",
                port_ref,
            )
        if base_port.mode is PortMode.TRUNK:
            if base_port.is_uplink is False:
                # POSITIVE evidence it is not an uplink and has no modeled peer/AP/
                # client -> a configured-but-unconnected trunk, no impact (INFO).
                return (
                    Severity.INFO, _HIGH, "wired.port.admin_disable.edge",
                    f"port {pid} administratively disabled — trunk port with no modeled "
                    "uplink or downstream, no impact",
                    port_ref,
                )
            # is_uplink True (faces the core) OR None (unknown) -> conservative WARNING
            return (
                Severity.WARNING, _HIGH, "wired.port.admin_disable.impact",
                f"port {pid} administratively disabled — a trunk link goes down",
                port_ref,
            )
```

(The final `return (Severity.INFO, ..., "wired.port.admin_disable.edge", "...edge port, no downstream impact modeled", port_ref)` access-port fallback stays unchanged below this.)

- [ ] **Step 4: Update the module docstring**

In `admin_disable.py`'s module docstring, update the per-severity summary so the WARNING/INFO line reflects: WARNING for an AP uplink, a modeled inter-switch/gateway link, active wired clients, OR a trunk that is an uplink/unknown; INFO for a bare edge port OR a trunk with `is_uplink=False` and no modeled peer/AP/client. Keep the existing ERROR/UNSAFE and pre-existing-disabled sentences.

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/checks/test_admin_disable.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS. If any pre-existing `test_admin_disable.py` test fails, investigate: a test that asserted the old blanket `_HIGH` for a *linked* trunk legitimately changes to the link's confidence (update it and note why in the report). A failure in any other check/golden is a real regression to fix, not to paper over.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/checks/wired/admin_disable.py tests/checks/test_admin_disable.py
git commit -m "fix(checks): admin_disable weighs peer link first, demotes unconnected non-uplink trunk to INFO

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `Port.is_uplink` observational field → Task 1 ✓
- Diff isolation (`_IGNORED_BY_KIND["port"]`) + lone-change-empty-diff test → Task 1 ✓
- Ingest independent of STP guard, no capability earned → Task 2 (`_apply_port_uplink`, `test_uplink_only_row_earns_no_stp_capability`) ✓
- Strict bool typing (non-bool → None) → Task 2 (`type(val) is not bool`, `test_non_bool_uplink_stays_unknown`) ✓
- admin_disable reorder: peer link first (link confidence), trunk uplink-aware, demote only on explicit False → Task 3 ✓
- Never-false-SAFE (None/True stay WARNING; linked trunk stays WARNING) → `test_unknown_uplink_trunk_stays_warning_conservative`, `test_uplink_true_trunk_stays_warning`, `test_linked_trunk_warns_at_link_confidence_even_if_not_uplink` ✓
- Docstring summary updated → Task 3 Step 4 ✓

**Type consistency:** `Port.is_uplink: bool | None`; ingest sets it via `type(val) is bool`; `_classify` reads `base_port.is_uplink is False`. Consistent across tasks.

**Placeholder scan:** none — every code step shows real code; the `_dev`/helper note in Task 1 names a concrete fallback (build the Device inline as neighbouring tests do), not a TBD.
