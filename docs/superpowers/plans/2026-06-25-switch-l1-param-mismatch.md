# Switch L1 Link-Parameter Mismatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Model switch port L1 physical parameters (`speed`/`duplex`/`disable_autoneg`, config + observed) and add a `wired.l1.link_param_mismatch` check that flags duplex/autoneg/speed mismatches across a link — so these changes simulate instead of returning UNKNOWN.

**Architecture:** Add config-intent + observed L1 fields to `Port` (mirroring `poe`/`poe_draw`); thread the three config attrs through the SP1 resolver layering and populate them (plus canonicalized observed negotiated state from `port_stats`) in switch ingest; add a two-ended boundary-walk check modeled on `wired.l2.mtu_mismatch`; then (only after the check exists) allowlist the leaves in the field gate.

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col, E/F/I), mypy-strict, networkx.

**Spec:** `docs/superpowers/specs/2026-06-25-switch-l1-param-mismatch-design.md`

## Global Constraints

- **No false-SAFE.** Force the field-gate change (Task 4) only after the check exists (Task 3). Every allowlisted leaf is modeled by the check.
- **IR invariant: `"auto"` is never stored.** Ingest normalizes config `speed`/`duplex` `"auto"` (and absent) to `None`. So `forced ⇔ autoneg_disabled and speed is not None and duplex is not None`.
- **`disable_autoneg` is NOT on `port_config_overwrite`** (OAS) — overwrite carries `speed`+`duplex` only.
- **Time-honesty.** Baseline observation never proves a post-change mismatch: an *introduced* mismatch's severity/confidence come from config+link provenance only; observed state is consulted ONLY in the pre-existing branch (clean-negotiation suppression + half-duplex annotation).
- **No standalone observed-only finding** in v1 (observed half-duplex with no config-predicted, delta-attributable mismatch → silent).
- **Gate after every task, from the worktree root:** `uv run pytest -q && uv run ruff check . && uv run mypy src` — all green before commit. (mypy not enforced on `tests/`; test-only Pyright noise — duck-typed providers, unused `_x`, stale-index unresolved imports — is not a gate failure.)
- **Commits** end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Branch/worktree:** `worktree-feat+switch-l1-params` at `.claude/worktrees/sp2-l1` (already created off `origin/main`, which includes SP1).

---

## File Structure

- **Modify** `src/digital_twin/ir/entities.py` — replace dormant `Port.speed:int`; add `speed:str|None`, `duplex:str|None`, `autoneg_disabled:bool`, `observed_speed:str|None`, `observed_duplex:str|None`. (Task 1)
- **Modify** `src/digital_twin/adapters/mist/ingest/ports.py` — add `speed`/`duplex`/`disable_autoneg` to the resolver attr tuples. (Task 1)
- **Modify** `src/digital_twin/adapters/mist/ingest/switch.py` — `_l1_config` (config + `"auto"` normalization, Task 1) and `_l1_observed` (canonicalizer + up-gating, Task 2); thread both into the switch `Port(...)`. (Tasks 1, 2)
- **Create** `src/digital_twin/checks/wired/l1_param_mismatch.py` — the check. (Task 3)
- **Modify** `src/digital_twin/checks/wired/__init__.py` — register. (Task 3)
- **Modify** `tests/test_public_api.py` — bump `len(ALL_WIRED_CHECKS) == 21 → 22`. (Task 3)
- **Modify** `src/digital_twin/scope/allowlist.py` — allowlist the leaves. (Task 4)
- **Modify** `tests/engine/test_pipeline.py` (e2e), `docs/ROADMAP.md`. (Task 5)
- **Tests:** `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`, `tests/checks/test_l1_param_mismatch.py` (new), `tests/scope/test_allowlist.py`.

---

## Task 1: IR L1 config fields + resolver threading + config ingest (with `"auto"` normalization)

**Files:**
- Modify: `src/digital_twin/ir/entities.py:119` (the `speed:int` line) + near `poe_draw`
- Modify: `src/digital_twin/adapters/mist/ingest/ports.py:26-44` (attr tuples)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_l1_config` + switch `Port(...)`)
- Test: `tests/adapters/mist/test_ingest_ports.py`, `tests/adapters/mist/test_ingest_switch.py`

**Interfaces:**
- Consumes: SP1 resolver (`resolve_effective_ports`, `_USAGE_OVERRIDE_ATTRS`, `_OVERWRITE_ATTRS`, `_LOCAL_ATTRS`).
- Produces: `Port.speed: str | None`, `Port.duplex: str | None`, `Port.autoneg_disabled: bool`, `Port.observed_speed: str | None`, `Port.observed_duplex: str | None` (observed populated in Task 2, default `None` here). `_l1_config(usage: dict) -> tuple[str|None, str|None, bool]` in `ingest/switch.py`.

- [ ] **Step 1: Write failing resolver + ingest tests**

Add to `tests/adapters/mist/test_ingest_ports.py`:

```python
def test_l1_attrs_resolve_through_layers():
    eff = _eff(
        port_config={"ge-0/0/1": {"usage": "office", "speed": "1g", "duplex": "full",
                                  "disable_autoneg": True}},
    )
    usage, _name = _resolved(eff)["ge-0/0/1"]
    assert usage.get("speed") == "1g" and usage.get("duplex") == "full"
    assert usage.get("disable_autoneg") is True


def test_l1_overwrite_carries_speed_duplex_not_autoneg():
    # port_config_overwrite has speed+duplex but NOT disable_autoneg (OAS)
    eff = _eff(
        port_config={"ge-0/0/2": {"usage": "office"}},
        port_config_overwrite={"ge-0/0/2": {"speed": "10g", "duplex": "full"}},
    )
    usage, _name = _resolved(eff)["ge-0/0/2"]
    assert usage.get("speed") == "10g" and usage.get("duplex") == "full"
```

Add to `tests/adapters/mist/test_ingest_switch.py` (mirror the sibling `test_disabled_usage_marks_port_disabled` scaffold — `eff` dict + `IngestContext(raw=raw_site(devices=(...,)), site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder())`; mac prefix `aa0000000001`):

```python
def test_l1_config_sets_port_fields_and_normalizes_auto():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "forced": {"mode": "access", "port_network": "corp", "speed": "1g",
                       "duplex": "full", "disable_autoneg": True},
            "autoport": {"mode": "access", "port_network": "corp", "speed": "auto",
                         "duplex": "auto"},
        },
        "port_config": {"ge-0/0/1": {"usage": "forced"}, "ge-0/0/2": {"usage": "autoport"}},
    }
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},)),
        site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    forced = ir.ports["aa0000000001:ge-0/0/1"]
    assert (forced.speed, forced.duplex, forced.autoneg_disabled) == ("1g", "full", True)
    auto = ir.ports["aa0000000001:ge-0/0/2"]
    # "auto" is NEVER stored — normalized to None
    assert (auto.speed, auto.duplex, auto.autoneg_disabled) == (None, None, False)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py -k l1 -q`
Expected: FAIL (`Port` has no `autoneg_disabled`; resolver drops speed/duplex/disable_autoneg). (Both files' new tests contain `l1` in their names, so one `-k l1` across both paths selects them all.)

- [ ] **Step 3: Replace the IR `speed` field and add the L1 fields**

In `src/digital_twin/ir/entities.py`, replace the line `    speed: int | None = None` with:
```python
    # CONFIG intent (L1, SP2): concrete speed enum / duplex. None = unset/auto —
    # the IR NEVER stores "auto" (ingest normalizes "auto"/absent to None), so
    # forced ⇔ autoneg_disabled and speed is not None and duplex is not None.
    speed: str | None = None
    duplex: str | None = None  # "full" | "half" | None
    autoneg_disabled: bool = False  # from disable_autoneg
```
And immediately after the `poe_draw: bool | None = None` line, add:
```python
    # OBSERVED (L1, SP2): negotiated speed/duplex from port_stats, UP ports only;
    # None = down / no telemetry. Speed canonicalized to the config enum (Task 2).
    observed_speed: str | None = None
    observed_duplex: str | None = None
```

- [ ] **Step 4: Thread the config attrs through the resolver**

In `src/digital_twin/adapters/mist/ingest/ports.py`, edit `_USAGE_OVERRIDE_ATTRS` to add the three attrs (after `stp_edge`):
```python
_USAGE_OVERRIDE_ATTRS = (
    "mode",
    "port_network",
    "networks",
    "all_networks",
    "voip_network",
    "poe_disabled",
    "mtu",
    "allow_dhcpd",
    "stp_edge",  # schema: inline on local_port_config only (gate enforces)
    "speed",
    "duplex",
    "disable_autoneg",
)
```
And edit `_OVERWRITE_ATTRS` to add `speed`+`duplex` (NOT `disable_autoneg` — not on overwrite per OAS):
```python
_OVERWRITE_ATTRS = ("port_network", "poe_disabled", "disabled", "speed", "duplex")
```
(`_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled")` picks up the three automatically.)

- [ ] **Step 5: Add `_l1_config` and populate the switch `Port`**

In `src/digital_twin/adapters/mist/ingest/switch.py`, add a module-level helper next to `_poe_draw`:
```python
def _l1_config(usage: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """(speed, duplex, autoneg_disabled) from effective usage attrs. "auto" and
    absent normalize to None for speed & duplex — the IR never stores "auto"."""
    def _norm(v: Any) -> str | None:
        return v if v not in (None, "", "auto") else None
    return _norm(usage.get("speed")), _norm(usage.get("duplex")), bool(usage.get("disable_autoneg"))
```
In the switch `Port(...)` construction (the `_switch_ports_and_l3` loop, the `add_port(Port(...))` call), compute before the call and pass the fields. Add just before `ctx.builder.add_port(`:
```python
            l1_speed, l1_duplex, l1_autoneg = _l1_config(usage)
```
and add these keyword args inside `Port(...)` (next to `disabled=...`):
```python
                    speed=l1_speed,
                    duplex=l1_duplex,
                    autoneg_disabled=l1_autoneg,
```

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS. Goldens should be UNCHANGED — no check consumes the new fields yet (the check isn't registered until Task 3). If a golden churns, STOP and investigate (likely an unexpected `diff.touches("port")` interaction) before re-pinning.

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/ports.py \
  src/digital_twin/adapters/mist/ingest/switch.py \
  tests/adapters/mist/test_ingest_ports.py tests/adapters/mist/test_ingest_switch.py
git commit -m "$(cat <<'EOF'
feat(ir,ingest): model switch L1 config (speed/duplex/autoneg), "auto"->None

Replaces the dormant Port.speed:int with the speed/duplex enum + autoneg_disabled
config-intent fields (+ observed_* fields, populated in the next task). Threads
speed/duplex/disable_autoneg through the resolver (overwrite carries speed+duplex
only). Ingest normalizes "auto"/absent to None so the IR never stores "auto".

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Observed L1 extraction (canonicalizer + up-gating)

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_l1_observed` + switch `Port(...)`)
- Test: `tests/adapters/mist/test_ingest_switch.py`

**Interfaces:**
- Consumes: the stat `row` already fetched in `_switch_ports_and_l3` (`row = stat_rows.get(member)`), Task 1's `Port.observed_speed`/`observed_duplex`.
- Produces: `_l1_observed(row: dict | None) -> tuple[str|None, str|None]`; `_SPEED_MBPS` canonicalizer map.

- [ ] **Step 1: Write failing tests**

Add to `tests/adapters/mist/test_ingest_switch.py`:

```python
def test_observed_l1_canonicalized_and_up_gated():
    eff = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {"u": {"mode": "access", "port_network": "corp"}},
        "port_config": {"ge-0/0/1": {"usage": "u"}, "ge-0/0/2": {"usage": "u"},
                        "ge-0/0/3": {"usage": "u"}},
    }
    stats = [
        {"mac": "aa0000000001", "port_id": "ge-0/0/1", "up": True, "speed": 1000,
         "full_duplex": True},
        {"mac": "aa0000000001", "port_id": "ge-0/0/2", "up": True, "speed": 100,
         "full_duplex": False},
        {"mac": "aa0000000001", "port_id": "ge-0/0/3", "up": False, "speed": 0,
         "full_duplex": False},
    ]
    from digital_twin.adapters.mist.ingest.base import IngestContext
    from digital_twin.ir import IRBuilder
    ctx = IngestContext(
        raw=raw_site(devices=({**SWITCH_A, "port_config": eff["port_config"]},),
                     port_stats=tuple(stats)),
        site_effective=eff, device_effective={"aa0000000001": eff}, builder=IRBuilder(),
    )
    SwitchIngester().ingest(ctx)
    ir = ctx.builder.build()
    p1 = ir.ports["aa0000000001:ge-0/0/1"]
    assert (p1.observed_speed, p1.observed_duplex) == ("1g", "full")
    p2 = ir.ports["aa0000000001:ge-0/0/2"]
    assert (p2.observed_speed, p2.observed_duplex) == ("100m", "half")
    p3 = ir.ports["aa0000000001:ge-0/0/3"]  # down port: never spurious half
    assert (p3.observed_speed, p3.observed_duplex) == (None, None)
```

NOTE: confirm `raw_site` accepts a `port_stats=` kwarg in this test file (the sibling PoE test `test_poe_disabled_usage_*` passes stats). If it uses a different mechanism to attach stats, mirror that exact mechanism; do not invent one.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -k observed_l1 -q`
Expected: FAIL (`observed_speed`/`observed_duplex` stay None).

- [ ] **Step 3: Add `_l1_observed` + the canonicalizer**

In `src/digital_twin/adapters/mist/ingest/switch.py`, add next to `_l1_config`:
```python
# Observed negotiated speed (port_stats numeric Mbps) -> config speed enum.
_SPEED_MBPS: dict[int, str] = {
    10: "10m", 100: "100m", 1000: "1g", 2500: "2.5g", 5000: "5g",
    10000: "10g", 25000: "25g", 40000: "40g", 100000: "100g",
}


def _l1_observed(row: _Json | None) -> tuple[str | None, str | None]:
    """(observed_speed, observed_duplex) from a port_stats row, UP ports only.
    Numeric Mbps -> config enum (unknown/0 -> None); duplex from full_duplex.
    A down port (or no row) yields (None, None) — never a spurious 'half'."""
    if row is None or not row.get("up"):
        return None, None
    speed = _SPEED_MBPS.get(row.get("speed")) if isinstance(row.get("speed"), int) else None
    fd = row.get("full_duplex")
    duplex = ("full" if fd else "half") if isinstance(fd, bool) else None
    return speed, duplex
```

- [ ] **Step 4: Populate the observed fields on the switch `Port`**

In the switch `Port(...)` construction, before `ctx.builder.add_port(`, add:
```python
            obs_speed, obs_duplex = _l1_observed(row)
```
and add inside `Port(...)` (next to `poe_draw=...`):
```python
                    observed_speed=obs_speed,
                    observed_duplex=obs_duplex,
```

- [ ] **Step 5: Run tests + gate**

Run: `uv run pytest tests/adapters/mist/test_ingest_switch.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS (goldens unchanged — still no consumer of these fields).

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/switch.py tests/adapters/mist/test_ingest_switch.py
git commit -m "$(cat <<'EOF'
feat(ingest): observed L1 (negotiated speed/duplex) from port_stats, up-gated

_l1_observed canonicalizes numeric Mbps to the config speed enum and reads
full_duplex, only for UP ports — a down port yields (None, None), never a
spurious half-duplex.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `wired.l1.link_param_mismatch` check + registration

**Files:**
- Create: `src/digital_twin/checks/wired/l1_param_mismatch.py`
- Modify: `src/digital_twin/checks/wired/__init__.py`
- Modify: `tests/test_public_api.py` (`21 → 22`)
- Test: `tests/checks/test_l1_param_mismatch.py`

**Interfaces:**
- Consumes: `BoundaryView`, `config_stated` (`checks/wired/link_boundary.py`); `Port.speed`/`duplex`/`autoneg_disabled`/`observed_speed`/`observed_duplex`; `CheckContext`, `min_confidence`.
- Produces: `L1ParamMismatchCheck` (`id="wired.l1.link_param_mismatch"`); codes `.speed_conflict`, `.duplex_conflict`, `.autoneg_mismatch`, `.unverified`, `.preexisting`.

- [ ] **Step 1: Write the failing check tests**

Create `tests/checks/test_l1_param_mismatch.py`:

```python
"""wired.l1.link_param_mismatch: speed/duplex/autoneg incompatibility across a
link. forced-vs-forced different speed/duplex -> ERROR; forced-vs-autonegotiating
-> WARNING (.autoneg_mismatch); forced-vs-no-config-peer -> WARNING (.unverified);
both-auto/forced-identical -> silent. Observed enrichment is pre-existing-only:
clean negotiation suppresses, half-duplex annotates INFO; baseline observation
never upgrades an introduced mismatch."""

from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext, Status
from digital_twin.checks.wired.l1_param_mismatch import L1ParamMismatchCheck
from digital_twin.contracts import Severity
from digital_twin.ir import IRBuilder, IRCapability, Port, PortMode, diff_ir
from digital_twin.ir.provenance import Provenance, fact_meta
from tests.factories import link, sw


def _p(pid, *, speed=None, duplex=None, autoneg_disabled=False,
       observed_speed=None, observed_duplex=None, blind=False):
    did, name = pid.split(":")
    meta = fact_meta(Provenance.OBSERVED, ("ensured from stats",)) if blind else None
    kw = {"meta": meta} if meta else {}
    return Port(id=pid, device_id=did, name=name, mode=PortMode.TRUNK, speed=speed,
                duplex=duplex, autoneg_disabled=autoneg_disabled,
                observed_speed=observed_speed, observed_duplex=observed_duplex, **kw)


def _ir(pa, pb):
    b = IRBuilder().add_device(sw("S")).add_device(sw("T"))
    b.add_port(pa)
    b.add_port(pb)
    b.add_link(link(pa.id, pb.id))  # two-sided -> HIGH
    b.with_capability(IRCapability.WIRED_L2)
    return b.build()


def _run(base, prop):
    return L1ParamMismatchCheck().run(CheckContext(
        baseline=AnalysisContext(base), proposed=AnalysisContext(prop), diff=diff_ir(base, prop)))


def _forced(pid, speed="1g", duplex="full"):
    return _p(pid, speed=speed, duplex=duplex, autoneg_disabled=True)


def test_forced_vs_forced_different_speed_is_error():
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1"))
    prop = _ir(_forced("S:ge-0/0/1", "1g"), _forced("T:ge-0/0/1", "10g"))
    r = _run(base, prop)
    assert r.status is Status.FAIL
    assert r.findings[0].code == "wired.l1.link_param_mismatch.speed_conflict"
    assert r.findings[0].severity is Severity.ERROR


def test_forced_vs_forced_different_duplex_is_error():
    prop = _ir(_forced("S:ge-0/0/1", "1g", "full"), _forced("T:ge-0/0/1", "1g", "half"))
    r = _run(_ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1")), prop)
    assert r.status is Status.FAIL
    assert r.findings[0].code == "wired.l1.link_param_mismatch.duplex_conflict"


def test_forced_vs_autonegotiating_is_warning():
    # peer is config-stated and not forced -> autonegotiating
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1"))
    r = _run(_ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1")), prop)
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.l1.link_param_mismatch.autoneg_mismatch"
    assert f.severity is Severity.WARNING


def test_forced_vs_no_config_peer_is_unverified_not_autoneg():
    # blind peer (no config facts) -> .unverified, NOT .autoneg_mismatch
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1", blind=True))
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1", blind=True))
    r = _run(base, prop)
    assert r.findings[0].code == "wired.l1.link_param_mismatch.unverified"
    assert r.findings[0].severity is Severity.WARNING


def test_both_autonegotiating_is_silent():
    assert _run(_ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1")),
                _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1"))).findings == ()


def test_introduced_mismatch_not_upgraded_by_baseline_observed_half():
    # baseline peer observed half — must NOT make the INTRODUCED mismatch HIGH/ERROR
    # beyond what config provenance gives (time-honesty). Here forced-vs-auto stays WARNING.
    base = _ir(_p("S:ge-0/0/1"), _p("T:ge-0/0/1", observed_duplex="half"))
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1", observed_duplex="half"))
    r = _run(base, prop)
    assert r.findings[0].severity is Severity.WARNING  # not escalated


def test_preexisting_conflict_clean_negotiation_suppressed():
    # same forced-vs-auto config in baseline AND both observed full at the same
    # speed -> hardware negotiated a working link -> suppressed
    base = _ir(_p("S:ge-0/0/1", speed="1g", duplex="full", autoneg_disabled=True,
                  observed_speed="1g", observed_duplex="full"),
               _p("T:ge-0/0/1", observed_speed="1g", observed_duplex="full"))
    r = _run(base, base)  # unchanged
    assert r.findings == ()  # pre-existing autoneg_mismatch + clean obs -> suppressed


def test_preexisting_conflict_no_clean_obs_is_info():
    forced_obs = _p("S:ge-0/0/1", speed="1g", duplex="full", autoneg_disabled=True)
    auto_half = _p("T:ge-0/0/1", observed_duplex="half")
    base = _ir(forced_obs, auto_half)
    r = _run(base, base)  # unchanged
    f = r.findings[0]
    assert f.code == "wired.l1.link_param_mismatch.preexisting" and f.severity is Severity.INFO
    assert r.status is Status.PASS  # INFO does not floor


def test_preexisting_unverified_suppressed():
    forced = _forced("S:ge-0/0/1")
    blind = _p("T:ge-0/0/1", blind=True)
    base = _ir(forced, blind)
    assert _run(base, base).findings == ()  # baseline-parity suppression


def test_unknown_peer_becoming_config_stated_auto_is_not_demoted():
    # baseline: forced vs NO-CONFIG peer (.unverified). proposed: same L1 tuple
    # (None/None/False) but the peer is now CONFIG-STATED -> .autoneg_mismatch.
    # The endpoint-class change means this is NOT pre-existing: it must surface as
    # WARNING, not be demoted to INFO/suppressed by the parity check.
    base = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1", blind=True))
    prop = _ir(_forced("S:ge-0/0/1"), _p("T:ge-0/0/1"))  # peer now config-stated
    r = _run(base, prop)
    assert r.status is Status.WARN
    f = r.findings[0]
    assert f.code == "wired.l1.link_param_mismatch.autoneg_mismatch"
    assert f.severity is Severity.WARNING  # NOT preexisting/INFO
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/checks/test_l1_param_mismatch.py -q`
Expected: FAIL with `ModuleNotFoundError: ...l1_param_mismatch`.

- [ ] **Step 3: Implement the check**

Create `src/digital_twin/checks/wired/l1_param_mismatch.py`:

```python
"""wired.l1.link_param_mismatch — incompatible speed/duplex/autoneg across a link.

A duplex/autoneg mismatch is invisible to reachability (the link carries every
VLAN, pings work) yet silently wrecks throughput — same class as the MTU check.
Per evaluable boundary (BoundaryView, ap_transparent=False — L1 exists on every
Ethernet link), classify each end as forced / autonegotiating / unknown-peer and
compare:

- both forced, different speed -> ERROR (.speed_conflict) — link won't establish
- both forced, same speed, different duplex -> ERROR (.duplex_conflict)
- one forced / one autonegotiating -> WARNING (.autoneg_mismatch) — auto side
  falls to half-duplex; 1g+ may not link
- one forced / one unknown-peer (no config facts) -> WARNING (.unverified)
- otherwise -> silent

Observed negotiated state (port_stats) enriches the PRE-EXISTING branch only —
it can confirm a live symptom or show the hardware negotiated around a predicted
mismatch, but it can never prove a post-change (introduced) outcome. An introduced
mismatch's severity/confidence come from config + link provenance alone.
"""

from __future__ import annotations

from digital_twin.checks.base import CheckContext, CheckResult, Coverage, CoverageState, Status
from digital_twin.contracts import Finding, FindingCategory, FindingSource, ObjectRef, Severity
from digital_twin.ir import (
    Capability,
    Confidence,
    ConfidenceLevel,
    IRCapability,
    IRDiff,
    min_confidence,
)
from digital_twin.ir.entities import Port

from .link_boundary import BoundaryView, config_stated

_HIGH = Confidence(level=ConfidenceLevel.HIGH)
_UNVERIFIED = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("peer port has no config facts — an L1 mismatch cannot be ruled out",),
)


def _forced(p: Port) -> bool:
    return p.autoneg_disabled and p.speed is not None and p.duplex is not None


def _classify(pa: Port, pb: Port) -> tuple[str, Severity, str] | None:
    """(code-suffix, base severity, message) for a config L1 incompatibility, else None."""
    fa, fb = _forced(pa), _forced(pb)
    if fa and fb:
        if pa.speed != pb.speed:
            return ("speed_conflict", Severity.ERROR,
                    f"forced speeds differ ({pa.speed} vs {pb.speed}) — link will not establish")
        if pa.duplex != pb.duplex:
            return ("duplex_conflict", Severity.ERROR,
                    f"forced duplex differs ({pa.duplex} vs {pb.duplex}) at {pa.speed}")
        return None
    if fa != fb:
        fp, other = (pa, pb) if fa else (pb, pa)
        hard = f"{fp.id} hard-set {fp.speed}/{fp.duplex} (autoneg off)"
        if config_stated(other):
            return ("autoneg_mismatch", Severity.WARNING,
                    f"{hard} but peer {other.id} autonegotiates — duplex-mismatch risk")
        return ("unverified", Severity.WARNING,
                f"{hard} but peer {other.id} has no config facts — mismatch cannot be ruled out")
    return None


def _l1(p: Port) -> tuple[str | None, str | None, bool]:
    return p.speed, p.duplex, p.autoneg_disabled


def _l1_sig(p: Port) -> tuple[str | None, str | None, bool, bool]:
    """Parity signature = the L1 tuple PLUS the endpoint class (config_stated).
    The L1 tuple alone is identical for a config-stated autonegotiating port and a
    no-config unknown peer ((None, None, False) both) — but they classify
    differently (.autoneg_mismatch vs .unverified), so config_stated MUST be part
    of the signature or a peer becoming config-stated would be wrongly demoted."""
    return (*_l1(p), config_stated(p))


def _same_l1(base_pair: tuple[Port, Port] | None, pa: Port, pb: Port) -> bool:
    """The same config L1 AND endpoint class on both ends already lived on the
    baseline boundary (matched by port id) — so the mismatch is pre-existing, not
    delta-caused."""
    if base_pair is None:
        return False
    by_id = {p.id: p for p in base_pair}
    ba, bb = by_id.get(pa.id), by_id.get(pb.id)
    return ba is not None and bb is not None and _l1_sig(ba) == _l1_sig(pa) and _l1_sig(bb) == _l1_sig(pb)


def _clean_negotiation(base_pair: tuple[Port, Port]) -> bool:
    """Both baseline ends observed full-duplex at the same known speed — the
    hardware negotiated a working link despite the config-predicted mismatch."""
    a, b = base_pair
    return (
        a.observed_duplex == "full" and b.observed_duplex == "full"
        and a.observed_speed is not None and a.observed_speed == b.observed_speed
    )


def _observed_half(base_pair: tuple[Port, Port]) -> bool:
    return any(p.observed_duplex == "half" for p in base_pair)


class L1ParamMismatchCheck:
    id = "wired.l1.link_param_mismatch"
    title = "Speed/duplex/autoneg mismatch across a link"
    domain = "wired.l1"
    default_severity = Severity.ERROR

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        return any(diff.touches(k) for k in ("link", "port", "device"))

    def run(self, ctx: CheckContext) -> CheckResult:
        prop_view = BoundaryView(ctx.proposed.ir, ap_transparent=False)
        base_view = BoundaryView(ctx.baseline.ir, ap_transparent=False)
        findings: list[Finding] = []
        for lnk in ctx.proposed.ir.links:
            pair = prop_view.pair(lnk)
            if pair is None:
                continue
            pa, pb = pair
            verdict = _classify(pa, pb)
            if verdict is None:
                continue
            code, base_sev, message = verdict
            base_pair = base_view.pair(lnk)
            preexisting = _same_l1(base_pair, pa, pb)
            if preexisting:
                assert base_pair is not None  # _same_l1 is False when None
                if code == "unverified":
                    continue  # baseline-parity suppression (stale no-facts uncertainty)
                if _clean_negotiation(base_pair):
                    continue  # hardware negotiated around it, unchanged by the delta
                severity, code_out = Severity.INFO, "preexisting"
                live = "; a peer is observed half-duplex" if _observed_half(base_pair) else ""
                message = (
                    f"link {pa.id} <-> {pb.id}: pre-existing L1 mismatch, unchanged by the "
                    f"delta (context{live})"
                )
                confidence = min_confidence(
                    pa.meta.confidence, pb.meta.confidence, lnk.meta.confidence
                )
            else:
                confidence = min_confidence(
                    pa.meta.confidence, pb.meta.confidence, lnk.meta.confidence
                )
                if code == "unverified":
                    confidence = min_confidence(confidence, _UNVERIFIED)
                high = confidence.level is ConfidenceLevel.HIGH
                severity = base_sev if (base_sev is Severity.ERROR and high) else Severity.WARNING
                code_out = code
            findings.append(
                Finding(
                    source=FindingSource.CHECK,
                    category=FindingCategory.NETWORK,
                    code=f"{self.id}.{code_out}",
                    severity=severity,
                    confidence=confidence,
                    message=f"link {pa.id} <-> {pb.id}: {message}" if code_out != "preexisting"
                    else message,
                    affected_entities=(pa.id, pb.id),
                    subject=ObjectRef("link", lnk.id),
                    evidence={
                        "link": lnk.id,
                        "a_port": pa.id,
                        "b_port": pb.id,
                        "a_l1": list(_l1(pa)),
                        "b_l1": list(_l1(pb)),
                    },
                    caused_by=tuple(
                        c for c in (
                            ctx.delta_index.cause("port", lnk.a_port),
                            ctx.delta_index.cause("port", lnk.b_port),
                            ctx.delta_index.cause("link", lnk.id),
                        ) if c is not None
                    ) if severity is not Severity.INFO else (),
                )
            )
        worst = Status.PASS
        conclusions = [f for f in findings if f.severity is not Severity.INFO]
        for f in conclusions:
            this = Status.FAIL if f.severity is Severity.ERROR else Status.WARN
            if this is Status.FAIL or worst is Status.PASS:
                worst = this
        return CheckResult(
            check_id=self.id,
            status=worst,
            findings=tuple(findings),
            coverage=Coverage(state=CoverageState.COMPLETE),
            confidence=(
                min_confidence(*(f.confidence for f in conclusions)) if conclusions else _HIGH
            ),
            reasoning="compared L1 speed/duplex/autoneg across both ends of every link",
        )
```

- [ ] **Step 4: Run the check tests**

Run: `uv run pytest tests/checks/test_l1_param_mismatch.py -q`
Expected: PASS.

- [ ] **Step 5: Register the check**

In `src/digital_twin/checks/wired/__init__.py`: add the import (alphabetical):
```python
from .l1_param_mismatch import L1ParamMismatchCheck
```
Append to `ALL_WIRED_CHECKS` (place after `MtuMismatchCheck()` — the sibling boundary-walk check):
```python
    L1ParamMismatchCheck(),
```
Add to `__all__`:
```python
    "L1ParamMismatchCheck",
```

- [ ] **Step 6: Bump the public-API count**

In `tests/test_public_api.py`, change `assert len(ALL_WIRED_CHECKS) == 21` to `== 22`.

- [ ] **Step 7: Run tests + gate**

Run: `uv run pytest tests/checks/test_l1_param_mismatch.py tests/test_public_api.py tests/checks/test_registry.py -q`
Expected: PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS. The check is now registered — if a golden churns, a real config in `site.json` carries a forced/auto L1 combo the check now flags; STOP, inspect the finding, and confirm it's a true mismatch before re-pinning (with justification).

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/checks/wired/l1_param_mismatch.py src/digital_twin/checks/wired/__init__.py \
  tests/checks/test_l1_param_mismatch.py tests/test_public_api.py
git commit -m "$(cat <<'EOF'
feat(checks): wired.l1.link_param_mismatch — speed/duplex/autoneg across a link

forced-vs-forced different speed/duplex -> ERROR; forced-vs-autonegotiating ->
WARNING; forced-vs-no-config-peer -> .unverified; observed enrichment is
pre-existing-only (clean negotiation suppresses, half annotates INFO; baseline
observation never proves an introduced mismatch). Modeled on mtu_mismatch.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Field-gate leaves

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py` (`_MODELED_USAGE_ATTRS`, `_PORT_CONFIG_ATTRS`, `_OVERWRITE_LEAVES`)
- Test: `tests/scope/test_allowlist.py`

**Interfaces:**
- Consumes: post-SP1 allowlist composition. `_MODELED_USAGE_ATTRS` feeds `_USAGE_LEAVES` (port_usages) and `_LOCAL_PORT_CONFIG_LEAVES` (local); `_PORT_CONFIG_ATTRS` feeds `_PORT_CONFIG_LEAVES`.
- Produces: `speed`/`duplex`/`disable_autoneg` in scope on port_config / local_port_config / port_usages; `speed`/`duplex` on port_config_overwrite (no `disable_autoneg`).

- [ ] **Step 1: Write failing scope tests**

Add to `tests/scope/test_allowlist.py`:

```python
def test_l1_attrs_in_scope():
    dev = set(RAW_ALLOWLIST["device"])
    for leaf in ("port_config.*.speed", "port_config.*.duplex", "port_config.*.disable_autoneg",
                 "local_port_config.*.speed", "local_port_config.*.duplex",
                 "local_port_config.*.disable_autoneg",
                 "port_config_overwrite.*.speed", "port_config_overwrite.*.duplex"):
        assert leaf in dev, leaf


def test_overwrite_has_no_disable_autoneg():
    # OAS: port_config_overwrite carries speed+duplex but NOT disable_autoneg
    assert "port_config_overwrite.*.disable_autoneg" not in set(RAW_ALLOWLIST["device"])


def test_usage_l1_in_scope():
    from digital_twin.scope.allowlist import EFFECTIVE_ALLOWLIST
    site = set(RAW_ALLOWLIST["site_setting"])
    eff = set(EFFECTIVE_ALLOWLIST)
    for a in ("speed", "duplex", "disable_autoneg"):
        assert f"port_usages.*.{a}" in site, a
        assert f"port_usages.*.{a}" in eff, a
```

NOTE (verified): `_USAGE_LEAVES` is composed into `RAW_ALLOWLIST["site_setting"]`, `RAW_ALLOWLIST["device"]`, and `EFFECTIVE_ALLOWLIST` (allowlist.py lines ~174/184/252). The `test_usage_l1_in_scope` assertions above target `site_setting` + `EFFECTIVE_ALLOWLIST` accordingly — both genuinely contain `port_usages.*`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/scope/test_allowlist.py -k "l1 or autoneg" -q`
Expected: FAIL (leaves absent).

- [ ] **Step 3: Add the leaves**

In `src/digital_twin/scope/allowlist.py`:

Add `speed`/`duplex`/`disable_autoneg` to `_MODELED_USAGE_ATTRS` (this propagates to `_USAGE_LEAVES` for port_usages AND `_LOCAL_PORT_CONFIG_LEAVES` for local):
```python
_MODELED_USAGE_ATTRS: tuple[str, ...] = (
    "mode",
    "port_network",
    "networks",
    "all_networks",
    "poe_disabled",
    "mtu",
    "allow_dhcpd",
    "speed",
    "duplex",
    "disable_autoneg",
)
```
Add `speed`/`duplex`/`disable_autoneg` to `_PORT_CONFIG_ATTRS` (port_config):
```python
_PORT_CONFIG_ATTRS: tuple[str, ...] = (
    "usage", "dynamic_usage", "port_network", "networks", "poe_disabled", "mtu",
    "speed", "duplex", "disable_autoneg",
)
```
Add `speed`+`duplex` to `_OVERWRITE_LEAVES` (NOT `disable_autoneg`):
```python
_OVERWRITE_LEAVES: tuple[str, ...] = (
    "port_config_overwrite.*.port_network",
    "port_config_overwrite.*.poe_disabled",
    "port_config_overwrite.*.disabled",
    "port_config_overwrite.*.speed",
    "port_config_overwrite.*.duplex",
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
feat(scope): allowlist L1 speed/duplex/disable_autoneg (overwrite: speed+duplex only)

In scope now that wired.l1.link_param_mismatch models them. disable_autoneg is
not allowlisted on port_config_overwrite (absent from its OAS).

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
- Consumes: the existing pipeline e2e harness in `tests/engine/test_pipeline.py` (the same `dc_replace`/`_raw`/`FakeProvider`/`_plan`/`_op`/`simulate`/`Decision` helpers used by SP1's `test_port_config_overwrite_disable_is_simulated_not_unknown`).

- [ ] **Step 1: Write the failing e2e test**

Add to `tests/engine/test_pipeline.py`, mirroring SP1's switch device-update e2e. Baseline gives the disabled... no — here: baseline has a trunk uplink port autonegotiating on both ends; the delta pins one end forced (autoneg off, speed+duplex) → `.autoneg_mismatch`:

```python
def test_l1_forced_vs_autonegotiating_peer_is_simulated_not_unknown():
    # pinning one end of a trunk uplink to a forced speed/duplex while the peer
    # autonegotiates must SIMULATE (REVIEW via autoneg_mismatch), not UNKNOWN.
    payload = {"port_config": {"ge-0/0/47": {"usage": "uplink", "speed": "1g",
                                             "duplex": "full", "disable_autoneg": True}}}
    result = _simulate_switch_update(payload)  # use this file's real e2e helper/scaffold
    assert result.decision is not Decision.UNKNOWN
    codes = {f.code for f in result.findings}
    assert "wired.l1.link_param_mismatch.autoneg_mismatch" in codes
    assert result.decision in (Decision.REVIEW, Decision.UNSAFE)
```

NOTE: use this file's actual helper names/scaffold (SP1 added `test_port_config_overwrite_disable_is_simulated_not_unknown` — copy its baseline fixture, which already wires `ge-0/0/47` as a trunk uplink with a peer; here the peer must remain autonegotiating, i.e. no forced L1 on the far end). Confirm the baseline peer port exists and is config-stated + not forced so the classification is `.autoneg_mismatch` (not `.unverified`).

- [ ] **Step 2: Run to verify it fails for the right reason**

Run: `uv run pytest tests/engine/test_pipeline.py -k l1_forced -q`
Expected: FAIL on the ASSERTION (decision/finding), not an import/setup error. Fix scaffold wiring if it errors.

- [ ] **Step 3: Confirm it passes**

Tasks 1–4 deliver the behavior. Run: `uv run pytest tests/engine/test_pipeline.py -k l1_forced -q`
Expected: PASS. If it fails on the assertion, debug whether the baseline peer is config-stated-not-forced (else `.unverified`) and whether the gate/resolver reach the check.

- [ ] **Step 4: Run the FULL golden suite; investigate churn before re-pinning**

Run: `uv run pytest tests/golden/ -q`
Expected: PASS. `site.json` forced/auto L1 combos, if any, now evaluate. **If a golden churns, STOP and diff it** — confirm the finding is a true L1 mismatch (or a pre-existing one correctly demoted to INFO / suppressed) before updating any expected output.

- [ ] **Step 5: ROADMAP entry**

In `docs/ROADMAP.md`, add under the most recent completed entries (match the existing format):
```markdown
- ✅ Switch L1 link-parameter mismatch — speed/duplex/autoneg (SP2 of the port-config attribute-modeling program) — done 2026-06-25
```

- [ ] **Step 6: Full gate**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy src`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/engine/test_pipeline.py docs/ROADMAP.md
git commit -m "$(cat <<'EOF'
test(l1): e2e forced-vs-autonegotiating uplink simulates (not UNKNOWN); roadmap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §1 IR fields (config + observed) + `"auto"`→None invariant → Task 1 (fields, normalization) + Task 2 (observed). ✓
- §2 ingest (resolver threading; observed canonicalizer + up-gating) → Task 1 (config/resolver), Task 2 (observed). ✓
- §3 check (forced/autonegotiating/unknown-peer classification; ERROR/WARNING/.unverified; observed pre-existing-only enrichment; pre-existing `.unverified` suppression; registration + public-api bump) → Task 3. ✓
- §4 field gate (speed/duplex/disable_autoneg; overwrite speed+duplex only) → Task 4. ✓
- §5 L0 no change → no task. ✓
- §6 no standalone observed-only → enforced by `_classify` requiring a forced end (observed never originates a finding) — covered by `test_introduced_mismatch_not_upgraded_by_baseline_observed_half` + the silence tests. ✓
- Testing (canonicalizer/up-gating, resolver+normalization, check matrix incl. config_stated split + time-honesty + suppression, public API, e2e, goldens) → Tasks 1–5. ✓

**Placeholder scan:** No TBD/TODO, no `assert ... or True`. Two "NOTE" blocks point at *existing* scaffolds to mirror (the `raw_site` stats kwarg, the SP1 pipeline e2e helper); the `_USAGE_LEAVES` container NOTE is now resolved/verified. No unused imports or locals in the test code (dropped `ConfidenceLevel`/`ap` and the dead `_AUTO_*` / `a` / `bport`) — clean under ruff `F`.

**Type consistency:** `Port.speed/duplex: str | None`, `autoneg_disabled: bool`, `observed_speed/observed_duplex: str | None` used identically across ingest, check, and tests; `_forced`/`_classify`/`_l1`/`_l1_sig`/`_same_l1`/`_clean_negotiation`/`_observed_half` signatures consistent; finding codes `wired.l1.link_param_mismatch.{speed_conflict,duplex_conflict,autoneg_mismatch,unverified,preexisting}` match between impl and tests; `ALL_WIRED_CHECKS` 21→22 matches the verified current count; `_l1_config`/`_l1_observed` return shapes match their call sites.

**Review-round fixes (round 2):**
- **P1** — pre-existing parity now keys on `_l1_sig` = `(speed, duplex, autoneg_disabled, config_stated)`, so a no-config peer becoming config-stated-autonegotiating (same `None/None/False` tuple) is NOT demoted to INFO; regression `test_unknown_peer_becoming_config_stated_auto_is_not_demoted` pins WARNING.
- **P2** — Task 4 `test_usage_l1_in_scope` asserts `port_usages.*.{speed,duplex,disable_autoneg}` in both `site_setting` and `EFFECTIVE_ALLOWLIST` (no tautology).
- **P2** — check test drops unused `ConfidenceLevel`/`ap` imports and `a`/`bport`/`_AUTO_*` (ruff-`F`-clean before impl).
- **P3** — Task 1 verify-failure uses a single `-k l1` across both files.
