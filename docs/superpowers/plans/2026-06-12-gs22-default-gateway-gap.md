# GS22-GW — Default Gateway Gap (ownership + DHCP coherence) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The declared default-gateway IP becomes a modeled fact: `wired.l3.gateway_gap` gains `.gateway_unowned` (no modeled L3 interface OWNS the declared gateway) and `wired.dhcp.scope_lint` gains `.gateway_mismatch` (DHCP hands out a gateway incoherent with its owning network).

**Architecture:** Spec at `docs/superpowers/specs/2026-06-12-gs22-default-gateway-gap-design.md` (4 review rounds). New IR-layer `same_ip` helper (ingest needs it — adapters never import checks); `Vlan.gateway(+_unresolved)` minted from the WINNING effective network row with the conflict→unresolved rule; `DhcpScope.network_gateway(+_unresolved)` in the provider's namespace. Both checks extend in place — no new check files.

**Tech Stack:** Python 3.14, uv, pytest, mypy --strict, ruff. Run everything with `uv run`.

**Conventions binding every task:**
- TDD: failing test → SEE the failure → implement → green → full gate → commit.
- Full gate per task: `uv run pytest tests -q` (629 passing now), `uv run ruff check .`, `uv run mypy src`.
- Checks consume ONLY the IR. Unknown never collapses to a violation. INFO never drags status/confidence/coverage.
- Commits end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: `ir/ip_match.py` — the shared `same_ip` helper

**Files:**
- Create: `src/digital_twin/ir/ip_match.py`
- Modify: `src/digital_twin/ir/__init__.py` (export)
- Test: `tests/ir/test_ip_match.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""same_ip: family-aware, /prefix-tolerant IP equality with honest unknowns.

Lives in the IR layer because the ingester needs it (non-winning-row
conflict rule) and adapters never import checks (GS22-GW spec r4)."""

from digital_twin.ir import same_ip


def test_equal_and_prefix_tolerant():
    assert same_ip("10.0.0.1", "10.0.0.1") is True
    assert same_ip("10.0.0.1", "10.0.0.1/24") is True
    assert same_ip("10.0.0.1/32", "10.0.0.1/24") is True


def test_different_ips_are_false():
    assert same_ip("10.0.0.1", "10.0.0.2") is False


def test_mixed_families_are_not_equal():
    # GS25 lesson: never compare bare ints across families; v4 != v6 even
    # when the integer values collide (0.0.0.255 vs ::ff)
    assert same_ip("0.0.0.255", "::ff") is False


def test_unknown_inputs_return_none_never_a_guess():
    assert same_ip(None, "10.0.0.1") is None
    assert same_ip("10.0.0.1", None) is None
    assert same_ip("foo", "10.0.0.1") is None
    assert same_ip("10.0.0.1", "{{gw}}") is None
```

- [ ] **Step 2: Run** — `uv run pytest tests/ir/test_ip_match.py -q`. Expected: FAIL, `ImportError: cannot import name 'same_ip'`.

- [ ] **Step 3: Implement** `src/digital_twin/ir/ip_match.py`:

```python
"""Family-aware IP equality with honest unknowns (GS22-GW shared helper).

IR-layer-neutral: the ingester consumes it (the non-winning-row gateway
conflict rule) and so do the gateway_gap/scope_lint checks — the layering
is adapters -> {ir, contracts} with checks downstream, so this cannot live
under checks/.
"""

from __future__ import annotations

import ipaddress


def same_ip(a: str | None, b: str | None) -> bool | None:
    """True/False = a definitive verdict over two parseable addresses
    (mismatched families are simply NOT equal); None = comparison UNKNOWN
    (either side absent or unparseable) — never a guessed (in)equality.
    Tolerates a /prefix suffix on either side (10.0.0.1 == 10.0.0.1/24)."""
    if a is None or b is None:
        return None
    try:
        pa = ipaddress.ip_address(str(a).split("/", 1)[0])
        pb = ipaddress.ip_address(str(b).split("/", 1)[0])
    except ValueError:
        return None
    if pa.version != pb.version:
        return False
    return pa == pb
```

Export from `src/digital_twin/ir/__init__.py` (import + `__all__`, next to the other helpers — follow the existing style).

- [ ] **Step 4: Run + full gate.** Expected: PASS, 633 total.

- [ ] **Step 5: Commit** — `git commit -m "GS22-GW: ir.same_ip — family-aware IP equality with honest unknowns"`

---

### Task 2: IR fields — `Vlan.gateway(+_unresolved)`, `DhcpScope.network_gateway(+_unresolved)`

**Files:**
- Modify: `src/digital_twin/ir/entities.py`
- Test: `tests/ir/test_dhcp_scope.py` (extend — it already covers GS25 IR fields)

- [ ] **Step 1: Failing test (diff visibility, the exact-changed-fields idiom from `test_trust_and_snooping_are_diffable_facts`)**

```python
def test_gateway_fields_are_diffable_facts():
    from dataclasses import replace

    from digital_twin.ir import Vlan, diff_ir

    def build(vlan_gw, scope_net_gw):
        b = IRBuilder().add_device(sw("S"))
        b.add_vlan(Vlan(vlan_id=10, name="corp", gateway=vlan_gw))
        b.add_dhcp_scope(
            DhcpScope(provider="site", network="corp", vlan=10,
                      gateway="10.0.0.1", network_gateway=scope_net_gw)
        )
        return b.build()

    d = diff_ir(build("10.0.0.1", "10.0.0.1"), build("10.0.0.9", "10.0.0.9"))
    vlan_mods = [m for m in d.modified if m.ref.kind == "vlan"]
    assert len(vlan_mods) == 1 and "gateway" in vlan_mods[0].changed_fields
    scope_mods = [m for m in d.modified if m.ref.kind == "dhcp_scope"]
    assert len(scope_mods) == 1 and "network_gateway" in scope_mods[0].changed_fields
```

- [ ] **Step 2: Run** — expected FAIL: `TypeError: ... unexpected keyword argument 'gateway'` (Vlan).

- [ ] **Step 3: Implement** in `entities.py`:

On `Vlan` (after `subnet`):

```python
    # Declared default-gateway IP (networks.*.gateway), minted from the SAME
    # effective network row that wins this Vlan (org overlay only when no
    # row declares one). None = no declared intent OR unresolved (flag).
    gateway: str | None = None
    # True iff gateway INTENT exists but is unreadable (templated) or
    # AMBIGUOUS (a non-winning same-vlan row disagrees — conflict is
    # unresolvable intent, never a silent winner). Absent intent stays False.
    gateway_unresolved: bool = False
```

On `DhcpScope` (after `subnet_unresolved`):

```python
    # The OWNING network's declared gateway, resolved in the PROVIDER's
    # namespace (org for gateway scopes, site for site scopes — exactly
    # like subnet). Feeds wired.dhcp.scope_lint.gateway_mismatch.
    network_gateway: str | None = None
    # Mirrors subnet_unresolved: declared-but-unreadable, or unknowable
    # (unfetched org namespace / name missing from a fetched one).
    network_gateway_unresolved: bool = False
```

- [ ] **Step 4: Run + full gate.** Expected: PASS (diff walks dataclass fields automatically).

- [ ] **Step 5: Commit** — `git commit -m "GS22-GW: Vlan.gateway + DhcpScope.network_gateway IR facts (with unresolved flags)"`

---

### Task 3: ingest `Vlan.gateway` — winning row, conflict rule, org overlay

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_vlans`, ~line 325-355)
- Test: `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Failing tests** (reuse the file's `_ir_for(eff)` helper and the `IngestContext` patterns; for multi-source tests build `device_effective` with a second map):

```python
def test_vlan_gateway_from_winning_row_with_org_overlay():
    eff = {"networks": {
        "corp": {"vlan_id": 10, "gateway": "10.0.0.1"},
        "lab": {"vlan_id": 30},
    }}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "labnet", "vlan_id": 30,
                                    "gateway": "10.0.30.1"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    assert ir.vlans[10].gateway == "10.0.0.1"      # site row wins
    assert ir.vlans[30].gateway == "10.0.30.1"     # org overlay fills absence
    assert ir.vlans[10].gateway_unresolved is False


def test_vlan_gateway_templated_winner_shadows_org():
    # present-shadows contract: an unreadable declared value NEVER falls
    # through to another namespace
    eff = {"networks": {"corp": {"vlan_id": 10, "gateway": "{{gw}}"}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "gateway": "10.0.0.1"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True


def test_conflicting_nonwinning_device_row_makes_gateway_unresolved():
    # singleton-Vlan limitation (spec r3): a device row for an already-seen
    # vlan id that DISAGREES on the gateway = ambiguous intent, never a
    # silent winner — and the Vlan CHANGES (diff fires) when a device op
    # introduces the conflict, closing the false-SAFE shape
    site = {"networks": {"corp": {"vlan_id": 10, "gateway": "10.0.0.1"}}}
    dev = {"networks": {"corp_local": {"vlan_id": 10, "gateway": "10.0.0.9"}}}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True


def test_agreeing_rows_and_gatewayless_rows_do_not_conflict():
    site = {"networks": {"corp": {"vlan_id": 10, "gateway": "10.0.0.1"}}}
    dev = {"networks": {
        "corp_local": {"vlan_id": 10, "gateway": "10.0.0.1/24"},  # same_ip True
        "corp_plain": {"vlan_id": 10},                            # no gateway key
    }}
    ctx = IngestContext(
        raw=raw_site(),
        site_effective=site,
        device_effective={"aa0000000001": dev},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway == "10.0.0.1" and v.gateway_unresolved is False


def test_org_only_templated_gateway_sets_unresolved():
    eff = {"networks": {"corp": {"vlan_id": 10}}}
    ctx = IngestContext(
        raw=raw_site(org_networks=({"name": "corpnet", "vlan_id": 10,
                                    "gateway": "{{gw}}"},)),
        site_effective=eff,
        device_effective={"aa0000000001": eff},
        builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    v = ctx.builder.build().vlans[10]
    assert v.gateway is None and v.gateway_unresolved is True
```

- [ ] **Step 2: Run** — expected FAIL (gateway always None / flags False).

- [ ] **Step 3: Implement** in `_vlans` (read the current loop first — it builds `org_subnets`, then iterates `sources` first-seen-wins). Add alongside `org_subnets`:

```python
        # org gateway overlay, RAW values (templated must be distinguishable
        # from absent — _literal_ip only at decision time)
        org_gw_raw: dict[int, Any] = {}
        for net in ctx.raw.org_networks:
            vid = _vlan_int(net.get("vlan_id"))
            if vid is not None and "gateway" in net:
                org_gw_raw.setdefault(vid, net.get("gateway"))
        # every declared gateway per vlan id across ALL effective sources,
        # in source order — [0] is the row that wins the Vlan mint
        declared_gw: dict[int, list[Any]] = {}
        for eff in sources:
            for net in (eff.get("networks") or {}).values():
                vid = _vlan_int(net.get("vlan_id"))
                if vid is not None and "gateway" in net:
                    declared_gw.setdefault(vid, []).append(net.get("gateway"))


def _vlan_gateway(  # module level, near _literal_ip
    vid: int, declared: Mapping[int, list[Any]], org_raw: Mapping[int, Any]
) -> tuple[str | None, bool]:
    """(gateway, unresolved) for a vlan id. Winning-row-shadows-org +
    conflict-is-unresolvable (GS22-GW spec): the first declared row wins;
    an unreadable winner or ANY disagreeing sibling row -> (None, True);
    org overlay fills only when NO row declares a gateway at all."""
    rows = declared.get(vid)
    if rows is None:
        if vid not in org_raw:
            return None, False  # no intent anywhere — not a blind spot
        lit = _literal_ip(org_raw[vid])
        return lit, lit is None
    winner = _literal_ip(rows[0])
    if winner is None:
        return None, True  # declared-but-unreadable (still shadows org)
    for other in rows[1:]:
        if same_ip(_literal_ip(other), winner) is not True:
            return None, True  # conflict = unresolvable intent
    return winner, False
```

Wire into the `Vlan(...)` construction:

```python
                    gw, gw_unresolved = _vlan_gateway(int(vid), declared_gw, org_gw_raw)
                    ctx.builder.add_vlan(
                        Vlan(
                            ...existing fields...,
                            gateway=gw,
                            gateway_unresolved=gw_unresolved,
                        )
                    )
```

Import `same_ip` from `digital_twin.ir`.

- [ ] **Step 4: Run + full gate.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "GS22-GW: Vlan.gateway from the winning network row (conflict = unresolved intent)"`

---

### Task 4: ingest `DhcpScope.network_gateway` (provider namespace)

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_mint_dhcp_scopes`)
- Test: `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Failing tests**

```python
def test_scope_network_gateway_site_namespace():
    eff = {
        "networks": {"corp": {"vlan_id": 10, "gateway": "10.0.0.1"},
                     "iot": {"vlan_id": 20, "gateway": "{{gw}}"}},
        "dhcpd_config": {
            "corp": {"type": "local", "gateway": "10.0.0.254"},
            "iot": {"type": "local"},
        },
    }
    scopes = {s.id: s for s in _ir_for(eff).dhcp_scopes}
    s = scopes["site:corp"]
    assert s.network_gateway == "10.0.0.1"
    assert s.network_gateway_unresolved is False
    t = scopes["site:iot"]
    assert t.network_gateway is None and t.network_gateway_unresolved is True


def test_gateway_scope_network_gateway_org_namespace():
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {
        "corp": {"type": "local", "gateway": "198.51.100.254"}
    }}
    org = ({"name": "corp", "vlan_id": 10, "gateway": "198.51.100.1"},)
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), org_networks=org),
        site_effective={}, device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    s = next(x for x in ctx.builder.build().dhcp_scopes if x.provider == "cc0000000001")
    assert s.network_gateway == "198.51.100.1"
    assert s.network_gateway_unresolved is False


def test_gateway_scope_network_gateway_blind_namespace_unresolved():
    gw = {**_GATEWAY, "ip_configs": {}, "dhcpd_config": {"corp": {"type": "local"}}}
    fetched = tuple(f for f in ALL_FETCHED if f != "org_networks")
    ctx = IngestContext(
        raw=raw_site(devices=(SWITCH_A, gw), fetched=fetched),
        site_effective={}, device_effective={}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    s = next(x for x in ctx.builder.build().dhcp_scopes if x.provider == "cc0000000001")
    assert s.network_gateway is None and s.network_gateway_unresolved is True
```

- [ ] **Step 2: Run** — expected FAIL (fields default).

- [ ] **Step 3: Implement** in `_mint_dhcp_scopes`, mirroring the EXISTING subnet/subnet_unresolved logic exactly:

Site loop — alongside the subnet fields:

```python
                    network_gateway=_literal_ip(net.get("gateway")),
                    # site namespace is always fetched: unresolved only when
                    # a DECLARED gateway is unreadable (templated)
                    network_gateway_unresolved=(
                        "gateway" in net and _literal_ip(net.get("gateway")) is None
                    ),
```

Gateway loop — `net_entry` already distinguishes name-missing (GS25 fix); add:

```python
                declared_gw = (net_entry or {}).get("gateway")
                ...
                        network_gateway=_literal_ip(declared_gw),
                        # same three-way rule as subnet_unresolved: blind
                        # namespace OR name missing -> unknowable; present
                        # but templated -> unreadable; present without a
                        # gateway -> no intent
                        network_gateway_unresolved=(
                            not org_fetched
                            or net_entry is None
                            or ("gateway" in net_entry
                                and _literal_ip(declared_gw) is None)
                        ),
```

NOTE the asymmetry vs subnet (which uses `declared is not None`): use KEY-presence (`"gateway" in ...`) so an explicit `"gateway": null` row counts as no-intent, consistent with Task 3's `_vlan_gateway`. (If the existing subnet logic's `is not None` convention makes the site loop read inconsistently, keep each field's own convention and say so in the comment — do NOT change subnet behavior in this task.)

- [ ] **Step 4: Run + full gate.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "GS22-GW: DhcpScope.network_gateway in the provider's namespace"`

---

### Task 5: `gateway_gap.gateway_unowned`

**Files:**
- Modify: `src/digital_twin/checks/wired/gateway_gap.py`
- Test: `tests/checks/test_gateway_gap.py` (extend — read its existing harness/factories first)

- [ ] **Step 1: Failing tests** (adapt the builder helpers to the file's existing ones — it has IR-builder fixtures for routed vlans and L3Intfs; follow them, do not invent new harness styles):

```python
def _routed_ir(*, gateway="10.0.0.1", gw_unresolved=False, intf_ip="10.0.0.1",
               with_intf=True):
    # if the file already has an equivalent routed-vlan builder, EXTEND it
    # with gateway/gw_unresolved/intf_ip params instead of adding this one
    from digital_twin.ir import IRBuilder, IRCapability, Vlan
    from digital_twin.ir.entities import L3Intf, L3Role
    from tests.factories import sw

    b = IRBuilder().add_device(sw("S"))
    b.add_vlan(Vlan(vlan_id=10, name="corp", subnet="10.0.0.0/24",
                    gateway=gateway, gateway_unresolved=gw_unresolved))
    if with_intf:
        b.add_l3intf(L3Intf(device_id="S", role=L3Role.IRB, vlan_id=10,
                            ip=intf_ip))
    b.with_capability(IRCapability.WIRED_L2)
    b.with_capability(IRCapability.L3_EXITS)
    return b.build()


def _run(base, prop):
    from digital_twin.analysis.context import AnalysisContext
    from digital_twin.checks.base import CheckContext
    from digital_twin.checks.wired.gateway_gap import GatewayGapCheck
    from digital_twin.ir import diff_ir

    return GatewayGapCheck().run(
        CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                     diff=diff_ir(base, prop))
    )
# (reuse the file's existing _run if one exists)


def test_owned_gateway_is_silent():
    r = _run(_routed_ir(), _routed_ir())
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]


def test_breaking_a_known_owner_is_error():
    # baseline owned (intf ip == declared G); delta moves G away -> the
    # known owner is gone: ERROR at the owning fact's confidence
    r = _run(_routed_ir(gateway="10.0.0.1"), _routed_ir(gateway="10.0.0.9"))
    f = next(x for x in r.findings if x.code.endswith("gateway_unowned"))
    assert f.severity is Severity.ERROR


def test_never_owned_is_warning_medium():
    # interfaces exist but none ever owned G (baseline G already different
    # from the intf and CHANGED by the delta -> introduced, no known owner)
    base = _routed_ir(gateway="10.0.0.8", intf_ip="10.0.0.250")
    prop = _routed_ir(gateway="10.0.0.9", intf_ip="10.0.0.250")
    r = _run(base, prop)
    f = next(x for x in r.findings if x.code.endswith("gateway_unowned"))
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM


def test_preexisting_unowned_is_info():
    same = _routed_ir(gateway="10.0.0.9", intf_ip="10.0.0.250")
    r = _run(same, same)
    f = next(x for x in r.findings if x.code.endswith("gateway_unowned"))
    assert f.severity is Severity.INFO


def test_no_declared_gateway_is_silent():
    r = _run(_routed_ir(gateway=None), _routed_ir(gateway=None))
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]


def test_unresolved_gateway_abstains_with_note_when_vlan_touched():
    base = _routed_ir(gateway="10.0.0.1")
    prop = _routed_ir(gateway=None, gw_unresolved=True)  # delta touches vlan
    r = _run(base, prop)
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("10" in n and "gateway" in n.lower() for n in r.coverage.notes)


def test_unresolved_gateway_untouched_stays_complete():
    same = _routed_ir(gateway=None, gw_unresolved=True)
    r = _run(same, same)
    assert r.coverage.state is CoverageState.COMPLETE


def test_unknown_intf_ip_abstains_never_unowned():
    # an interface with ip=None may BE the owner: unknown never collapses
    # to a violation
    base = _routed_ir(gateway="10.0.0.1", intf_ip="10.0.0.1")
    prop = _routed_ir(gateway="10.0.0.1", intf_ip=None)
    r = _run(base, prop)
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]
    assert r.coverage.state is CoverageState.PARTIAL


def test_unparseable_declared_gateway_abstains():
    # "foo" passes _literal_ip (not templated) — same_ip returns None
    # against every interface -> ownership UNKNOWN, never .gateway_unowned
    base = _routed_ir(gateway="10.0.0.1")
    prop = _routed_ir(gateway="foo")
    r = _run(base, prop)
    assert not [f for f in r.findings if f.code.endswith("gateway_unowned")]
    assert r.coverage.state is CoverageState.PARTIAL


def test_no_interfaces_stays_with_existence_codes():
    # strict precedence: the no-intf case is .removed/.unserved territory;
    # .gateway_unowned never fires there (no double-fire)
    base = _routed_ir(gateway="10.0.0.1", with_intf=True)
    prop = _routed_ir(gateway="10.0.0.1", with_intf=False)
    r = _run(base, prop)
    codes = [f.code for f in r.findings]
    assert any(c.endswith(".removed") for c in codes)
    assert not any(c.endswith("gateway_unowned") for c in codes)
```

- [ ] **Step 2: Run** — expected FAIL (no such code emitted).

- [ ] **Step 3: Implement** in `gateway_gap.py`. Keep the existing loop untouched; add an OWNERSHIP pass after it (same findings/notes lists). Semantics (spec, distilled):

```python
        # --- .gateway_unowned: interfaces EXIST but none owns the declared
        # gateway (strict precedence: the no-interface cases belong to the
        # existence codes above; never double-fire)
        changed_vlan_ids = {
            r.id for r in (*ctx.diff.added, *ctx.diff.removed,
                           *(m.ref for m in ctx.diff.modified))
            if r.kind == "vlan"
        }
        l3_touched_vlans = {
            r.id.rsplit(":", 1)[-1]
            for r in (*ctx.diff.added, *ctx.diff.removed,
                      *(m.ref for m in ctx.diff.modified))
            if r.kind == "l3intf"
        }
        abstain_notes: list[str] = []
        for vid, vlan in sorted(prop_ir.vlans.items()):
            intfs = prop_l3.get(vid)
            if not intfs:
                continue  # existence codes own this case
            relevant = str(vid) in changed_vlan_ids or str(vid) in l3_touched_vlans
            if vlan.gateway_unresolved:
                if relevant:
                    abstain_notes.append(
                        f"vlan {vid}: declared default gateway is unreadable or "
                        "ambiguous — ownership cannot be verified"
                    )
                continue
            g = vlan.gateway
            if g is None:
                continue  # no declared intent
            verdicts = [same_ip(i.ip, g) for i in intfs]
            if any(v is True for v in verdicts):
                continue  # owned — a positive fact nothing can taint
            if any(v is None for v in verdicts):
                if relevant:
                    abstain_notes.append(
                        f"vlan {vid}: an L3 interface has an unknown/unparseable "
                        f"address — it may own the declared gateway {g}"
                    )
                continue
            # definitively unowned: parity + severity per the doctrine
            base_vlan = base_ir.vlans.get(vid)
            base_g = base_vlan.gateway if base_vlan is not None else None
            base_intfs = base_l3.get(vid, [])
            owners = [
                i for i in base_intfs
                if base_g is not None and same_ip(i.ip, base_g) is True
            ]
            if owners:
                # known owner broken (G moved, or the owner changed/left)
                severity, code = Severity.ERROR, "gateway_unowned"
                confidence = min_confidence(*(i.meta.confidence for i in owners))
                if blind_notes:
                    confidence = min_confidence(confidence, _BLIND_GATEWAY)
                message = (
                    f"vlan {vid}: declared default gateway {g} is owned by NO "
                    f"modeled L3 interface — the baseline owner "
                    f"({', '.join(i.id for i in owners)}) no longer matches"
                )
            elif (
                base_vlan is not None
                and not base_vlan.gateway_unresolved
                and base_g == g
                and base_intfs
                and all(same_ip(i.ip, base_g) is False for i in base_intfs)
            ):
                severity, code = Severity.INFO, "gateway_unowned"
                confidence = _UNMODELED
                message = (
                    f"vlan {vid}: pre-existing unowned declared gateway {g}, "
                    "unchanged by the delta (context)"
                )
            else:
                # never owned / newly declared / G changed between unowned
                # values: there was no KNOWN owner -> honest REVIEW, never
                # ERROR (the owner may live on an unmodeled box)
                severity, code = Severity.WARNING, "gateway_unowned"
                confidence = _UNMODELED
                message = (
                    f"vlan {vid}: declared default gateway {g} is owned by no "
                    "modeled L3 interface"
                )
            high = confidence.level is ConfidenceLevel.HIGH
            if severity is Severity.ERROR and not high:
                severity = Severity.WARNING
            findings.append(Finding(..., code=f"{self.id}.{code}", ...))
```

(Construct the Finding exactly like the existing ones: CHECK/NETWORK, evidence `{"vlan": vid, "gateway": g, "l3_interfaces": [i.id for i in intfs]}`, affected `(str(vid),)`.) Notes merge, EXACT rule: the existing check computes `notes = blind_notes if conclusions else ()`; change the final assembly to `notes = (blind_notes if conclusions else ()) + tuple(abstain_notes)` — blind_notes keep their conclusions-gate (GS22 rule), abstain_notes attach whenever generated because the `relevant` filter already scoped them (GS25 relevance discipline). Coverage stays `PARTIAL iff notes`. Import `same_ip` from `digital_twin.ir`. Update the module docstring with the new code's doctrine line (known-owner-broken strong; unknown-owner absence honest REVIEW).

- [ ] **Step 4: Run + full gate.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "GS22-GW: gateway_gap.gateway_unowned — declared gateway must have a modeled owner"`

---

### Task 6: `scope_lint.gateway_mismatch`

**Files:**
- Modify: `src/digital_twin/checks/wired/scope_lint.py`
- Test: `tests/checks/test_scope_lint.py` (extend)

- [ ] **Step 1: Failing tests**

```python
M_BAD = DhcpScope(provider="site", network="m", vlan=40, gateway="10.4.0.254",
                  network_gateway="10.4.0.1")
M_OK = DhcpScope(provider="site", network="m", vlan=40, gateway="10.4.0.1/24",
                 network_gateway="10.4.0.1")


def test_introduced_gateway_mismatch_is_warning():
    r = _run(_ir(), _ir(M_BAD))
    f = next(x for x in r.findings if x.code.endswith("gateway_mismatch"))
    assert f.severity is Severity.WARNING
    assert f.evidence["handed"] == "10.4.0.254"
    assert f.evidence["declared"] == "10.4.0.1"


def test_prefix_equal_gateways_do_not_mismatch():
    r = _run(_ir(), _ir(M_OK))
    assert not [f for f in r.findings if f.code.endswith("gateway_mismatch")]


def test_preexisting_mismatch_is_info_and_any_value_change_forfeits():
    r = _run(_ir(M_BAD), _ir(M_BAD))
    f = next(x for x in r.findings if x.code.endswith("gateway_mismatch"))
    assert f.severity is Severity.INFO
    moved = DhcpScope(provider="site", network="m", vlan=40,
                      gateway="10.4.0.253", network_gateway="10.4.0.1")
    r2 = _run(_ir(M_BAD), _ir(moved))
    f2 = next(x for x in r2.findings if x.code.endswith("gateway_mismatch"))
    assert f2.severity is Severity.WARNING


def test_missing_either_side_is_silent():
    a = DhcpScope(provider="site", network="m", gateway="10.4.0.254")
    b = DhcpScope(provider="site", network="n", network_gateway="10.4.0.1")
    r = _run(_ir(), _ir(a, b))
    assert not [f for f in r.findings if f.code.endswith("gateway_mismatch")]


def test_network_gateway_unresolved_touched_scope_notes_partial():
    s = DhcpScope(provider="site", network="m", gateway="10.4.0.254",
                  network_gateway=None, network_gateway_unresolved=True)
    r = _run(_ir(), _ir(s))
    assert r.coverage.state is CoverageState.PARTIAL
    assert any("site:m" in n and "gateway" in n.lower() for n in r.coverage.notes)


def test_unparseable_present_values_abstain_with_note():
    s = DhcpScope(provider="site", network="m", gateway="bogus",
                  network_gateway="10.4.0.1")
    r = _run(_ir(), _ir(s))
    assert not [f for f in r.findings if f.code.endswith("gateway_mismatch")]
    assert r.coverage.state is CoverageState.PARTIAL
```

- [ ] **Step 2: Run** — expected FAIL.

- [ ] **Step 3: Implement** in `scope_lint.py` — a third finding block after `.out_of_subnet`, plus two note sources merged into the existing dimension-specific note logic (both PER-SCOPE, keyed on `changed_ids` exactly like the subnet notes):

```python
        # --- .gateway_mismatch (DHCP hands out a gateway incoherent with
        # its owning network — config coherence, not proven outage)
        for sid, s in sorted(prop.items()):
            verdict = same_ip(s.gateway, s.network_gateway)
            if verdict is not False:
                continue  # equal, or either side absent/unreadable
            b = base.get(sid)
            preexisting = (
                b is not None
                and b.gateway == s.gateway
                and b.network_gateway == s.network_gateway
            )
            findings.append(self._finding(
                "gateway_mismatch",
                Severity.INFO if preexisting else Severity.WARNING,
                f"DHCP scope {sid} hands out gateway {s.gateway} but its "
                f"network declares {s.network_gateway}"
                + (" (pre-existing, unchanged)" if preexisting else ""),
                {"scope": sid, "handed": s.gateway, "declared": s.network_gateway},
                (sid,),
            ))
```

Note tuples (added to the `subnet_notes`-style block, same `changed_ids` relevance):

```python
        gw_notes = tuple(
            f"scope {sid}: owning network gateway is unreadable or unknowable "
            "— gateway coherence is unevaluated for it"
            for sid, s in sorted(prop.items())
            if s.network_gateway_unresolved and sid in changed_ids
        ) + tuple(
            f"scope {sid}: handed/declared gateway is unparseable — gateway "
            "coherence cannot be evaluated"
            for sid, s in sorted(prop.items())
            if s.gateway is not None and s.network_gateway is not None
            and same_ip(s.gateway, s.network_gateway) is None
            and sid in changed_ids
        )
        notes = range_notes + subnet_notes + gw_notes
```

Import `same_ip` from `digital_twin.ir`. Update the module docstring (third code, same parity/tier rules).

- [ ] **Step 4: Run + full gate.** Expected: PASS.

- [ ] **Step 5: Commit** — `git commit -m "GS22-GW: scope_lint.gateway_mismatch — DHCP-handed gateway must cohere with its network"`

---

### Task 7: goldens GS22-GW a–d

**Files:**
- Modify: `tests/golden/test_golden_scenarios.py`

Fixture facts to verify first (one command):

```bash
python3 -c "
import json; d=json.load(open('tests/golden/fixtures/site.json'))
gw=[x for x in d['devices'] if x.get('type')=='gateway'][0]
print('ip_configs:', gw.get('ip_configs'))
print('vlan2 site row:', (d['setting'].get('networks') or {}).get('vlan2'))"
```

Expected: `ip_configs` has one entry named `test` with ip `198.51.194.227`; the site `vlan2` row is `{"vlan_id": "2"}`. The org staging therefore includes `{"name": "test", "vlan_id": 2}` so the SRX's `ip_configs.test` resolves to a CONFIG/HIGH L3Intf on vlan 2 — the known owner.

- [ ] **Step 1: Write the goldens** (build on `_gs25_doc` — same org staging + OAS-cruft cleanup; READ it first):

```python
def _gs22gw_doc(*, vlan2_gateway="198.51.194.227", stage_mismatch=False):
    # org staging additionally resolves the SRX's ip_configs entry 'test'
    # to vlan 2 (CONFIG/HIGH L3Intf — the known owner of the declared
    # gateway); vlan2's declared gateway rides the site networks row
    doc = _gs25_doc()
    doc["org_networks"].append({"name": "test", "vlan_id": 2})
    nets = doc["setting"].setdefault("networks", {})
    nets["vlan2"] = {**nets.get("vlan2", {"vlan_id": "2"}),
                     "gateway": vlan2_gateway}
    if stage_mismatch:
        nets["gs22_m"] = {"vlan_id": 992, "gateway": "10.9.0.1"}
        doc["setting"].setdefault("dhcpd_config", {})["gs22_m"] = {
            "type": "local", "ip_start": "10.9.0.10", "ip_end": "10.9.0.99",
            "gateway": "10.9.0.99",
        }
    return doc


def _site_networks_op(doc, mutate):
    """Full-map networks update (root-replace semantics — the GS25 lesson:
    a partial networks payload would DELETE every other baseline row)."""
    nets = {k: dict(v) for k, v in (doc["setting"].get("networks") or {}).items()}
    mutate(nets)
    return {
        "action": "update", "order": 0, "object_type": "site_setting",
        "object_id": doc["scope"]["site_id"],
        "payload": {"networks": nets},
    }


def test_gs22gw_a_breaking_the_gateway_owner_is_unsafe(tmp_path):
    # baseline: vlan2's declared gateway IS the SRX ip_configs address
    # (owned, CONFIG/HIGH). The op moves the declared gateway to an address
    # no modeled interface owns -> known owner broken -> UNSAFE
    doc = _gs22gw_doc()
    op = _site_networks_op(
        doc, lambda nets: nets["vlan2"].__setitem__("gateway", "198.51.194.250")
    )
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.gateway_unowned")
    assert f.severity.value == "error" and f.evidence["vlan"] == 2


def test_gs22gw_b_preexisting_unowned_gateway_is_safe_info(tmp_path):
    # the unowned declared gateway already exists in baseline; the delta
    # adds an unrelated plain vlan -> INFO context, never a floor
    doc = _gs22gw_doc(vlan2_gateway="198.51.194.250")
    op = _site_networks_op(
        doc, lambda nets: nets.__setitem__("gs22_plain", {"vlan_id": 993})
    )
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons
    f = next(f for f in v.findings if f.code == "wired.l3.gateway_gap.gateway_unowned")
    assert f.severity.value == "info"


def test_gs22gw_c_dhcp_gateway_mismatch_is_review(tmp_path):
    # op introduces a site scope handing out a gateway that contradicts the
    # network's declared one -> WARNING -> REVIEW
    doc = _gs22gw_doc()
    nets_op = _site_networks_op(
        doc, lambda nets: nets.__setitem__("gs22_m", {"vlan_id": 992,
                                                      "gateway": "10.9.0.1"})
    )
    nets_op["payload"]["dhcpd_config"] = {
        **(doc["setting"].get("dhcpd_config") or {}),
        "gs22_m": {"type": "local", "ip_start": "10.9.0.10",
                   "ip_end": "10.9.0.99", "gateway": "10.9.0.99"},
    }
    v = _simulate(doc, plan_for(doc, [nets_op]), tmp_path)
    assert v.decision is Decision.REVIEW, v.decision_reasons
    f = next(f for f in v.findings
             if f.code == "wired.dhcp.scope_lint.gateway_mismatch")
    assert f.severity.value == "warning"


def test_gs22gw_d_preexisting_mismatch_is_safe_info(tmp_path):
    # mismatch pre-staged in baseline; op adds an unrelated coherent scope
    doc = _gs22gw_doc(stage_mismatch=True)
    op = _site_networks_op(
        doc, lambda nets: nets.__setitem__("gs22_far", {"vlan_id": 991,
                                                        "gateway": "10.50.0.1"})
    )
    op["payload"]["dhcpd_config"] = {
        **(doc["setting"].get("dhcpd_config") or {}),
        "gs22_far": {"type": "local", "ip_start": "10.50.0.10",
                     "ip_end": "10.50.0.99", "gateway": "10.50.0.1"},
    }
    v = _simulate(doc, plan_for(doc, [op]), tmp_path)
    assert v.decision is Decision.SAFE, v.decision_reasons
    f = next(f for f in v.findings
             if f.code == "wired.dhcp.scope_lint.gateway_mismatch")
    assert f.severity.value == "info"
```

Trap notes for the implementer (debug via `v.decision_reasons` before touching production code):
- GS22-GW-a: vlan 2 has observed clients — `client_impact` may add REVIEW findings; the assertion is decision UNSAFE which dominates. If the verdict is REVIEW instead, check whether the `.gateway_unowned` ERROR demoted (blind cap? the `test` org name must be staged or the SRX port goes blind).
- GS22-GW-b: the op touches `networks` — confirm the new plain vlan doesn't trip `.unserved` (it declares NO subnet → not routed → silent) or dynamic-port gates.
- GS22-GW-c/d: every staged/op scope carries parseable ranges (a rangeless scope triggers the GS25 range-blind PARTIAL note when scopes are touched → would break SAFE in d). Ranges 10.9.x/10.50.x are far from the SRX's 198.51.x — no `.overlap`.
- If `_gs25_doc` mutated state matters (it appends to `org_networks`), copy before append if shared.

- [ ] **Step 2: Run** — `uv run pytest tests/golden -q`. All four green if Tasks 1–6 are correct; report any failure with its decision_reasons rather than patching checks.

- [ ] **Step 3: Full gate.** Expected: ~660 tests green.

- [ ] **Step 4: Commit** — `git commit -m "GS22-GW goldens: owner-broken UNSAFE / preexisting INFO / dhcp mismatch REVIEW / preexisting mismatch SAFE"`

---

### Task 8: roadmap, live verification, memory

- [ ] **Step 1: Roadmap** — flip the "Default gateway gap" entry from IN PROGRESS to ✅ DONE (date, the two codes, the conflict→unresolved rule, `ir/ip_match.py`); the §5 templated-subnet debt entry stays.

- [ ] **Step 2: Live verification** (read-only):

```bash
set -a; source .env; set +a
echo "plan.json -> $(uv run digital-twin --plan plan.json 2>/dev/null | head -1)"
for p in test-plans/*.json; do
  echo "$p -> $(uv run digital-twin --plan "$p" 2>/dev/null | head -1)"
done
```

Expected UNCHANGED: plan.json UNSAFE; 01 SAFE, 02 SAFE, 03 REVIEW, 04 REVIEW, 05 UNSAFE, 06 SAFE, 07 REVIEW. The live org's vlan-2 gateway intent lives org-side; if any verdict moves, inspect which new code fired (`grep gateway` on the full output) before judging — a real live mismatch is a FINDING to report to the user, not a regression to silence.

- [ ] **Step 3: Memory** — append the GS22-GW round to the project memory file (`wireless-vlan-observation-gap.md`, Round 14): conflict = unresolvable intent; winning-row precedence; same_ip in IR layer (adapters never import checks); known-owner-broken vs never-owned doctrine.

- [ ] **Step 4: Final commit** — `git commit -m "GS22-GW: roadmap done-entry + live verification"`
