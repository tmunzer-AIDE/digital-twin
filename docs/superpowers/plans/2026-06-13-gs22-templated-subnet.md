# GS22-SUB — Templated-subnet false-SAFE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the templated-subnet false-SAFE — a vlan whose only subnet declaration is templated currently reads as "not routed" and silences `wired.l3.gateway_gap`. Give `Vlan` a `subnet_unresolved` flag (the twin of `gateway_unresolved`) so routed-but-unreadable intent abstains (PARTIAL → REVIEW) instead of passing SAFE.

**Architecture:** Generalize the five-leg winning-literal precedence that `_vlan_gateway` already encodes into one `_winning_literal(vid, rows, org_raw, *, parse, same)` core; `_vlan_gateway` becomes a thin wrapper (its tests are the regression net) and subnet mints through the same core with `_literal_subnet`/`same_subnet`. The consumer (`gateway_gap`) splits its "not routed" gate so an unresolved subnet abstains with a relevance-scoped coverage note rather than silencing.

**Tech Stack:** Python 3.14, uv, pytest, ipaddress, the existing IR/ingest/checks layering (adapters → {ir, contracts} → checks).

**Spec:** `docs/superpowers/specs/2026-06-13-gs22-templated-subnet-design.md`

**Full gate (run before every commit that touches src):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

---

### Task 1: `same_subnet` comparator

**Files:**
- Modify: `src/digital_twin/ir/ip_match.py`
- Modify: `src/digital_twin/ir/__init__.py` (export)
- Test: `tests/ir/test_ip_match.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/ir/test_ip_match.py` (it already imports from `digital_twin.ir`):

```python
from digital_twin.ir import same_subnet


def test_same_subnet_equal_normalizes_host_bits():
    assert same_subnet("10.0.10.0/24", "10.0.10.0/24") is True
    assert same_subnet("10.0.10.5/24", "10.0.10.0/24") is True  # strict=False
    assert same_subnet("10.0.10.0", "10.0.10.0") is True        # bare host -> /32


def test_same_subnet_different_networks_are_false():
    assert same_subnet("10.0.10.0/24", "10.0.11.0/24") is False
    assert same_subnet("10.0.10.0/24", "10.0.10.0/25") is False


def test_same_subnet_mixed_families_are_false():
    assert same_subnet("10.0.10.0/24", "2001:db8::/32") is False


def test_same_subnet_unknown_is_none():
    assert same_subnet(None, "10.0.10.0/24") is None
    assert same_subnet("10.0.10.0/24", None) is None
    assert same_subnet("not-a-subnet", "10.0.10.0/24") is None
    assert same_subnet("10.0.10.0/24", "{{subnet}}") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ir/test_ip_match.py -q`
Expected: FAIL — `ImportError: cannot import name 'same_subnet'`

- [ ] **Step 3: Implement `same_subnet`**

Append to `src/digital_twin/ir/ip_match.py` (module already imports `ipaddress`):

```python
def same_subnet(a: str | None, b: str | None) -> bool | None:
    """True/False = a definitive verdict over two parseable CIDR subnets;
    None = comparison UNKNOWN (either side absent or unparseable) — never a
    guessed (in)equality. Normalizes with strict=False so host bits set
    (10.0.10.5/24) compare equal to the network (10.0.10.0/24); a bare host
    becomes /32 (or /128). FAMILY-AWARE: mismatched versions are NOT equal
    (the GS25 lesson — never int-compare across families)."""
    if a is None or b is None:
        return None
    try:
        na = ipaddress.ip_network(str(a), strict=False)
        nb = ipaddress.ip_network(str(b), strict=False)
    except ValueError:
        return None
    if na.version != nb.version:
        return False
    return na == nb
```

- [ ] **Step 4: Export from the IR package**

In `src/digital_twin/ir/__init__.py`, change the import line `from .ip_match import same_ip` to:

```python
from .ip_match import same_ip, same_subnet
```

and add `"same_subnet"` to `__all__` immediately after the existing `"same_ip",` entry.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/ir/test_ip_match.py -q && uv run mypy src`
Expected: PASS, mypy clean.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/ir/ip_match.py src/digital_twin/ir/__init__.py tests/ir/test_ip_match.py
git commit -m "GS22-SUB: same_subnet — family-aware CIDR equality with honest unknowns"
```

---

### Task 2: `Vlan.subnet_unresolved` IR field

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (the `Vlan` dataclass)
- Test: `tests/ir/test_entities.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/ir/test_entities.py`:

```python
def test_vlan_subnet_unresolved_defaults_false():
    from digital_twin.ir.entities import Vlan
    assert Vlan(vlan_id=10).subnet_unresolved is False
    assert Vlan(vlan_id=10, subnet=None, subnet_unresolved=True).subnet_unresolved is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ir/test_entities.py::test_vlan_subnet_unresolved_defaults_false -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'subnet_unresolved'`

- [ ] **Step 3: Add the field**

In `src/digital_twin/ir/entities.py`, in the `Vlan` dataclass, immediately after the `subnet: str | None = None` line and its comment, add:

```python
    # True iff subnet INTENT exists but is unreadable (templated) or AMBIGUOUS
    # (a non-winning same-vlan row disagrees — conflict is unresolvable intent,
    # never a silent winner). Absent/empty subnet = no intent = stays False
    # (a blanket flag would PARTIAL-floor every ordinary subnet-less vlan).
    subnet_unresolved: bool = False
```

Place it BEFORE the `gateway:` field so the routed-intent fields (`subnet`, `subnet_unresolved`) stay adjacent.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/ir/test_entities.py -q && uv run mypy src`
Expected: PASS, mypy clean.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/entities.py tests/ir/test_entities.py
git commit -m "GS22-SUB: Vlan.subnet_unresolved — declared-but-unreadable routed intent"
```

---

### Task 3: Generalize the winning-literal core (no behavior change)

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_vlan_gateway` ~117-148)
- Test: `tests/adapters/mist/test_ingest_switch.py` (existing `_vlan_gateway` tests are the regression net — unchanged)

This task is a pure refactor: extract the five-leg skeleton, rewrite `_vlan_gateway` to call it. The existing gateway ingest tests (`test_vlan_gateway_from_winning_row_with_org_overlay`, `test_vlan_gateway_templated_winner_shadows_org`, `test_conflicting_nonwinning_device_row_makes_gateway_unresolved`, `test_agreeing_rows_and_gatewayless_rows_do_not_conflict`, `test_silent_winner_with_declaring_nonwinning_row_is_unresolved`, `test_explicit_null_gateway_is_no_intent`, `test_org_only_templated_gateway_sets_unresolved`) MUST stay green with no edits — that is the verification.

- [ ] **Step 1: Add the generalized core and rewrite `_vlan_gateway`**

In `src/digital_twin/adapters/mist/ingest/switch.py`, replace the entire `_vlan_gateway` function (currently ~117-148) with the core plus a thin wrapper. The core needs `Callable`; add it to the existing `from typing import` / `from collections.abc import` imports if not present (check the file head — `Mapping` is already imported from `collections.abc`, so add `Callable` there):

```python
def _winning_literal(
    vid: int,
    rows_by_vid: Mapping[int, list[Any]],
    org_raw: Mapping[int, Any],
    *,
    parse: Callable[[Any], str | None],
    same: Callable[[str | None, str | None], bool | None],
) -> tuple[str | None, bool]:
    """(value, unresolved) for a vlan id under the five-leg precedence shared
    by the gateway and subnet fields. `rows` are ALL same-vid network rows'
    raw field values in mint order — [0] is the true Vlan winner; None = no
    intent (null==absent canon, normalized by the caller).

    - nobody declares -> org overlay (unreadable org value -> (None, True))
    - winner silent but a NON-WINNING row declares -> (None, True): the
      singleton Vlan cannot represent a per-device value, never silently
      promote it nor fall to org over it
    - winner declares but unreadable -> (None, True), shadows org
    - winner declares, any declaring sibling disagrees -> (None, True)
    - winner declares, readable, every other declaring row agrees -> value
    """
    rows = rows_by_vid.get(vid, [])
    declaring = [r for r in rows if r is not None]
    if not declaring:
        if vid not in org_raw:
            return None, False  # no intent anywhere — not a blind spot
        lit = parse(org_raw[vid])
        return lit, lit is None
    if rows[0] is None:
        return None, True  # winner silent, a non-winning row declares
    winner = parse(rows[0])
    if winner is None:
        return None, True  # declared-but-unreadable (still shadows org)
    for other in declaring[1:]:
        if same(parse(other), winner) is not True:
            return None, True  # conflict = unresolvable intent
    return winner, False


def _vlan_gateway(
    vid: int, rows_by_vid: Mapping[int, list[Any]], org_raw: Mapping[int, Any]
) -> tuple[str | None, bool]:
    """(gateway, unresolved) for a vlan id — the gateway specialization of
    `_winning_literal` (GS22-GW). rows are same-vid `networks.*.gateway`
    values in mint order; None = no intent (null==absent canon)."""
    return _winning_literal(vid, rows_by_vid, org_raw, parse=_literal_ip, same=same_ip)
```

- [ ] **Step 2: Run the gateway regression net**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -q -k "gateway or winner or null"`
Expected: PASS — every existing gateway helper test green with no test edits.

- [ ] **Step 3: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS, clean.

- [ ] **Step 4: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/switch.py
git commit -m "GS22-SUB: extract _winning_literal core; _vlan_gateway becomes a wrapper"
```

---

### Task 4: Mint subnet through the core (closes the ingest false-SAFE)

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_vlans` ~357-409)
- Test: `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/adapters/mist/test_ingest_switch.py` (uses the existing `IngestContext`/`raw_site`/`IRBuilder`/`SwitchIngester` harness already imported at the top of the file):

```python
def test_vlan_templated_subnet_is_unresolved_not_routed():
    # the false-SAFE: a templated subnet must NOT read as a literal nor as
    # "not routed" — it is declared-but-unreadable
    eff = {"networks": {"corp": {"vlan_id": 10, "subnet": "{{vlan10_subnet}}"}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_vlan_empty_subnet_is_absent_not_blind():
    # present-but-empty "" = no routed intent, NOT a blind spot (no flag)
    eff = {"networks": {"corp": {"vlan_id": 10, "subnet": ""}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is False


def test_vlan_subnet_org_overlay_literal_still_routed():
    # regression: switch knows the vlan by id, org networks carry the subnet
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "subnet": "10.0.10.0/24"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet == "10.0.10.0/24" and v.subnet_unresolved is False


def test_vlan_org_only_templated_subnet_sets_unresolved():
    # no effective row declares subnet; org overlay value is templated
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "subnet": "{{sub}}"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_conflicting_nonwinning_device_row_makes_subnet_unresolved():
    # literal-disagreement leg: a device row for an already-seen vlan id
    # declares a DIFFERENT subnet than the winner -> ambiguous, never silent
    site = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.10.0/24"}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "subnet": "10.0.99.0/24"}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_silent_winner_with_declaring_nonwinning_subnet_row_is_unresolved():
    # review P2: the WINNING row has no subnet but a later device row declares
    # one -> never silently promote it, never fall through to org (the
    # distinct false-SAFE leg, twin of the gateway suite)
    site = {"networks": {"corp": {"vlan_id": 10}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "subnet": "10.0.99.0/24"}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "subnet": "10.0.10.0/24"},)),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet is None and v.subnet_unresolved is True


def test_agreeing_subnet_rows_do_not_conflict():
    # host-bits-set sibling that normalizes equal -> still the winner literal
    site = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.10.0/24"}}}
    dev = {"networks": {
        "corp_local": {"vlan_id": 10, "subnet": "10.0.10.5/24"},  # same_subnet True
        "corp_plain": {"vlan_id": 10},                            # no subnet key
    }}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.subnet == "10.0.10.0/24" and v.subnet_unresolved is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -q -k "subnet"`
Expected: FAIL — the templated/conflict cases return the old `or`-fallback values; `subnet_unresolved` is always False (AttributeError is avoided since Task 2 added the field, so assertions fail on value).

- [ ] **Step 3: Rewrite the subnet mint in `_vlans`**

In `src/digital_twin/adapters/mist/ingest/switch.py`, in `_vlans`:

(a) Replace the `org_subnets` block (currently ~365-370):

```python
        org_subnets: dict[int, str] = {}
        for net in ctx.raw.org_networks:
            vid = _vlan_int(net.get("vlan_id"))
            subnet = _literal_subnet(net.get("subnet"))
            if vid is not None and subnet:
                org_subnets.setdefault(vid, subnet)
```

with a RAW org overlay (templated must stay distinguishable from absent, exactly like `org_gw_raw`):

```python
        # org subnet overlay, RAW values (templated must be distinguishable
        # from absent — _literal_subnet only inside the winning-literal core)
        org_subnet_raw: dict[int, Any] = {}
        for net in ctx.raw.org_networks:
            vid = _vlan_int(net.get("vlan_id"))
            if vid is not None and net.get("subnet"):
                org_subnet_raw.setdefault(vid, net.get("subnet"))
```

(b) In the per-vlan row-collection loop that already builds `rows_by_vid` for gateway (currently ~383-388), also collect subnet rows. Rename the gateway map for clarity and add the subnet map. Replace:

```python
        rows_by_vid: dict[int, list[Any]] = {}
        for eff in sources:
            for net in (eff.get("networks") or {}).values():
                vid = _vlan_int(net.get("vlan_id"))
                if vid is not None:
                    rows_by_vid.setdefault(vid, []).append(net.get("gateway"))
```

with:

```python
        gw_rows_by_vid: dict[int, list[Any]] = {}
        subnet_rows_by_vid: dict[int, list[Any]] = {}
        for eff in sources:
            for net in (eff.get("networks") or {}).values():
                vid = _vlan_int(net.get("vlan_id"))
                if vid is not None:
                    gw_rows_by_vid.setdefault(vid, []).append(net.get("gateway"))
                    # "" subnet = absent (no routed intent); only {{templated}}
                    # is declared-unreadable — normalize empty to None here so
                    # the shared core's "declared = not None" stays uniform
                    subnet_rows_by_vid.setdefault(vid, []).append(net.get("subnet") or None)
```

(c) In the mint loop (currently ~393-409), update the `_vlan_gateway` call to use the renamed map and add the subnet call; pass both unresolved flags to `add_vlan`. Replace:

```python
                    gw, gw_unresolved = _vlan_gateway(vid, rows_by_vid, org_gw_raw)
                    ctx.builder.add_vlan(
                        Vlan(
                            vlan_id=vid,
                            name=name,
                            scope=ctx.raw.scope.site_id,
                            subnet=net.get("subnet") or org_subnets.get(vid),
                            gateway=gw,
                            gateway_unresolved=gw_unresolved,
                            dhcp_sources=tuple(sorted(dhcp_sources.get(vid, ()))),
                        )
                    )
```

with:

```python
                    gw, gw_unresolved = _vlan_gateway(vid, gw_rows_by_vid, org_gw_raw)
                    subnet, subnet_unresolved = _winning_literal(
                        vid, subnet_rows_by_vid, org_subnet_raw,
                        parse=_literal_subnet, same=same_subnet,
                    )
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
                        )
                    )
```

(d) Add `same_subnet` to the existing `from digital_twin.ir import ...` line at the top of the file (it already imports `same_ip`). Verify `_literal_subnet` is defined above `_winning_literal` (it is, ~56) and `_winning_literal` is module-level (Task 3).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS — new subnet tests green; the two pre-existing subnet assertions (lines ~587, ~603, org-overlay literal `198.51.100.0/24`) still pass (org overlay literal → routed, unchanged).

- [ ] **Step 5: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS, clean. (Watch for any golden that previously relied on a templated subnet reading as routed — none expected; if one appears, it is the false-SAFE being corrected, reconcile in Task 6.)

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/switch.py tests/adapters/mist/test_ingest_switch.py
git commit -m "GS22-SUB: mint Vlan.subnet via _winning_literal — templated/conflict -> unresolved"
```

---

### Task 5: `gateway_gap` abstains on unresolved-routed (closes the consumer false-SAFE)

**Files:**
- Modify: `src/digital_twin/checks/wired/gateway_gap.py` (`run`, ~76-261)
- Test: `tests/checks/test_gateway_gap.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/checks/test_gateway_gap.py` (reuse the file's existing helpers for building `CheckContext`/IR; mirror the construction used by the nearest existing `gateway_gap` test — find a test that builds a routed vlan with an L3 interface and adapt it). The three behaviors:

```python
def test_unresolved_subnet_in_delta_abstains_partial_not_safe():
    # a vlan whose subnet is unresolved (templated), CHANGED by the delta,
    # with no modeled L3 interface -> must NOT be SAFE: abstain note -> PARTIAL
    # (was the false-SAFE: subnet=None read as "not routed" and silenced)
    ctx = _ctx_with_vlan(
        vid=10, subnet=None, subnet_unresolved=True, l3_intfs=[],
        changed_vlan_ids={"10"},
    )
    result = GatewayGapCheck().run(ctx)
    assert result.coverage.state is CoverageState.PARTIAL
    assert any("unreadable or ambiguous" in n for n in result.coverage.notes)
    assert not any(f.severity is Severity.ERROR for f in result.findings)


def test_unresolved_subnet_not_in_delta_no_note_stays_complete():
    # relevance discipline: an unchanged unresolved vlan elsewhere never taints
    ctx = _ctx_with_vlan(
        vid=10, subnet=None, subnet_unresolved=True, l3_intfs=[],
        changed_vlan_ids=set(),
    )
    result = GatewayGapCheck().run(ctx)
    assert result.coverage.state is CoverageState.COMPLETE
    assert result.coverage.notes == ()


def test_absent_subnet_not_routed_is_silent():
    # subnet None + NOT unresolved = not routed: no finding, no note (unchanged)
    ctx = _ctx_with_vlan(
        vid=10, subnet=None, subnet_unresolved=False, l3_intfs=[],
        changed_vlan_ids={"10"},
    )
    result = GatewayGapCheck().run(ctx)
    assert result.coverage.state is CoverageState.COMPLETE
    assert result.findings == ()
```

NOTE TO IMPLEMENTER: `_ctx_with_vlan` is a helper you write at the top of the test file IF one does not already exist with equivalent capability. It must build a `CheckContext` whose `proposed.ir` holds one `Vlan(vlan_id=vid, subnet=subnet, subnet_unresolved=subnet_unresolved)`, whose `baseline.ir` mirrors it (so no existence finding fires), an empty `l3intfs` set when `l3_intfs=[]`, and a `diff` whose changed vlan ids equal `changed_vlan_ids` (the check derives relevance from `ctx.diff.added/removed/modified` refs with `kind=="vlan"`). Model it on the existing `gateway_unowned` tests in this same file, which already construct exactly this shape for the gateway field — copy that construction and set the subnet fields.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/checks/test_gateway_gap.py -q -k "subnet"`
Expected: FAIL — today the unresolved-subnet vlan is silenced (`subnet is None` → continue), so coverage is COMPLETE with no note; the first test's PARTIAL assertion fails.

- [ ] **Step 3: Implement the abstain split**

In `src/digital_twin/checks/wired/gateway_gap.py`, `run`:

(a) Hoist the relevance machinery ABOVE the existence loop. The `changed_vlan_ids` and `l3_touched_vlans` sets are currently computed at ~144-154 (between the two loops). Move that whole block to immediately before the existence loop (`for vid, vlan in sorted(prop_ir.vlans.items()):` at ~91), and add a `subnet_abstain_notes: list[str] = []` accumulator beside the existing `findings: list[Finding] = []`.

(b) Replace the existence-loop guard. Change:

```python
        for vid, vlan in sorted(prop_ir.vlans.items()):
            if vlan.subnet is None or prop_l3.get(vid):
                continue  # not routed, or served
```

to:

```python
        for vid, vlan in sorted(prop_ir.vlans.items()):
            if prop_l3.get(vid):
                continue  # served — a positive fact nothing can taint
            if vlan.subnet is None:
                if vlan.subnet_unresolved and (
                    str(vid) in changed_vlan_ids or str(vid) in l3_touched_vlans
                ):
                    subnet_abstain_notes.append(
                        f"vlan {vid}: declared subnet is unreadable or ambiguous "
                        "— routed intent cannot be verified"
                    )
                continue  # not routed, or unresolved -> abstained above
```

(c) Fold the new notes into the coverage. The final `notes` assembly (~248) is currently:

```python
        notes = (blind_notes if conclusions else ()) + tuple(abstain_notes)
```

Change it to also include the subnet abstain notes (they are already delta-scoped, attach whenever generated — same rule as the gateway `abstain_notes`):

```python
        notes = (
            (blind_notes if conclusions else ())
            + tuple(abstain_notes)
            + tuple(subnet_abstain_notes)
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/checks/test_gateway_gap.py -q`
Expected: PASS — all three new tests plus every pre-existing gateway_gap test green.

- [ ] **Step 5: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/checks/wired/gateway_gap.py tests/checks/test_gateway_gap.py
git commit -m "GS22-SUB: gateway_gap abstains on unresolved-routed subnet (no more false-SAFE)"
```

---

### Task 6: Goldens, roadmap, live verification, memory

**Files:**
- Modify: `tests/golden/test_golden_scenarios.py` (GS22-SUB a/b/c)
- Modify: `docs/ROADMAP.md` (§5 entry → ✅; §4 GS22 line if present)
- Modify: `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/wireless-vlan-observation-gap.md` + `MEMORY.md` (Round 15)

- [ ] **Step 1: Write the three goldens (RED)**

In `tests/golden/test_golden_scenarios.py`, add GS22-SUB a/b/c following the GS22-GW golden style (use the existing `_gs22gw_doc`/site-network helpers as the template; grep `gs22gw` in this file to find them). The scenarios:

- **GS22-SUB-a** — baseline: routed vlan with a templated-only subnet AND a modeled L3 interface (so baseline is coherent); op removes that L3 interface. Because the subnet is unresolved, the existence loop cannot fire `.removed` (subnet reads None); the vlan IS in the delta (l3intf touched) → subnet abstain note → PARTIAL → assert decision **REVIEW**. (Counterfactual the reviewer should confirm: with a LITERAL subnet this would be UNSAFE `.removed`; the templated subnet is exactly why it degrades to honest REVIEW rather than false-SAFE.)
- **GS22-SUB-b** — baseline vlan with a literal subnet on the site row and a modeled L3 interface (SAFE baseline); op adds a device-level network row for the same vlan id declaring a DIFFERENT subnet. `Vlan.subnet` flips literal→unresolved, the vlan changes (diff fires) → subnet abstain note → assert **REVIEW**.
- **GS22-SUB-c** (control) — baseline vlan with a templated-only subnet, unchanged; op touches an UNRELATED vlan (e.g. a name edit on a different vlan with a literal subnet and an owner). The templated vlan is not in the delta → no subnet note → assert **SAFE**.

Run: `uv run pytest tests/golden/test_golden_scenarios.py -q -k "gs22_sub or gs22sub"`
Expected: FAIL (scenarios not yet wired / assertions红).

- [ ] **Step 2: Make them pass (GREEN)**

Wire the fixtures until the three decisions match. If GS22-SUB-a/b trip an unrelated scope code (e.g. `scope.dynamic_ports.unverifiable`, the GS22-GW b/d fixture-staging artifact), resolve it via the SAME `_resolve_fixture_dynamic_ports` staging helper the GS22-GW goldens use — and verify the counterfactual (without staging the SAFE/REVIEW assertion still distinguishes the subnet behavior, i.e. the staging is baseline hygiene, not gate suppression).

Run: `uv run pytest tests/golden/test_golden_scenarios.py -q`
Expected: PASS (all goldens, GS22-SUB included).

- [ ] **Step 3: Full gate + commit goldens**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: PASS, clean.

```bash
git add tests/golden/test_golden_scenarios.py
git commit -m "GS22-SUB goldens: templated-subnet REVIEW / nonwinning-conflict REVIEW / unrelated SAFE"
```

- [ ] **Step 4: Live verification (read-only, real org)**

Run all eight live plans and confirm verdicts are unchanged from the pre-round baseline (plan.json UNSAFE; 01 SAFE, 02 SAFE, 03 REVIEW, 04 REVIEW, 05 UNSAFE, 06 SAFE, 07 REVIEW). Credentials load from the gitignored `.env`; the CLI emits TEXT with first line `decision: <VERDICT>`:

```bash
set -a; source .env; set +a
for p in plans/*.json; do printf '%s ' "$p"; uv run digital-twin --plan "$p" 2>/dev/null | head -1; done
```

Expected: every plan holds its prior verdict (the live org's routed vlans carry literal subnets; none templated — so the new abstain path is not exercised live). If any verdict moved, STOP and reconcile before the roadmap commit.

- [ ] **Step 5: Roadmap + memory + commit**

In `docs/ROADMAP.md` §5, flip the "templated-subnet false-SAFE in gateway_gap" entry from 🟡 to ✅ with a one-line done-note (date 2026-06-13, `Vlan.subnet_unresolved` + `_winning_literal` generalization + `same_subnet`, all eight live plans unchanged). Note that the §5 entry's "singleton Vlan drops a NON-WINNING ... subnet" sub-clause is now also closed (the conflict→unresolved rule). Append Round 15 to `wireless-vlan-observation-gap.md` (doctrine: the winning-literal generalization, empty=absent vs templated=unresolved for subnet, the silent-winner-shadowed leg pinned) and add/refresh its `MEMORY.md` pointer.

```bash
git add docs/ROADMAP.md
git commit -m "GS22-SUB: roadmap §5 templated-subnet ✅ + live verification (8 plans unchanged)"
```

---

## Self-Review

**Spec coverage:**
- `Vlan.subnet_unresolved` → Task 2. ✓
- Shared `_winning_literal` core + `_vlan_gateway` wrapper (regression net) → Task 3. ✓
- `same_subnet` (CIDR-normalized, family-aware, None-unknown) → Task 1. ✓
- Ingest mint: `subnet_rows_by_vid` (`or None` = empty-absent), `org_subnet_raw` (setdefault truthy), winner-shadows-org, templated→unresolved, both conflict legs → Task 4 (tests cover templated winner, empty-absent, org-overlay-literal, org-templated, literal-disagreement, silent-winner-shadowed, agreeing-rows). ✓
- Consumer abstain split + relevance scoping + PARTIAL note → Task 5. ✓
- Goldens a/b/c + live-8-hold + roadmap/memory → Task 6. ✓
- Both pinned conflict legs (literal-disagreement AND silent-winner-shadowed) have explicit ingest tests → Task 4. ✓

**Placeholder scan:** No TBD/TODO. The only deliberately-not-inlined code is the golden fixture bodies (Task 6) and the `_ctx_with_vlan` check-test helper (Task 5) — both directed to a named existing template (`_gs22gw_doc` goldens; the existing `gateway_unowned` tests) because they are large fixtures whose exact bytes depend on adjacent helpers the implementer reads in place. Every src-side change shows full code.

**Type consistency:** `_winning_literal(vid, rows_by_vid, org_raw, *, parse, same) -> tuple[str|None, bool]` is called identically in Task 3 (`_vlan_gateway` wrapper: `parse=_literal_ip, same=same_ip`) and Task 4 (subnet: `parse=_literal_subnet, same=same_subnet`). `same_subnet` signature matches `same_ip` (`str|None,str|None -> bool|None`). `Vlan(subnet_unresolved=...)` field (Task 2) matches the `add_vlan` call (Task 4) and the consumer reads (Task 5). Map rename `rows_by_vid`→`gw_rows_by_vid` is applied consistently in Task 4 (collection + `_vlan_gateway` call site).
