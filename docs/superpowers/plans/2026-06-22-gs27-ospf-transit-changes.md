# GS27 — OSPF Transit Changes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn mutation of retained OSPF transit config (metric / passive / area / added participation / advertised-prefix) into precise REVIEW findings, and add an escalate-only live-telemetry adjacency-break layer that sharpens confirmed peer losses to UNSAFE.

**Architecture:** Extend the existing `wired.l3.ospf_withdrawal` check in place (Approach A) — one OSPF check, one `applies_to`, shared participation model — and quarantine the blind-built reachability logic in a pure `analysis/ospf_reachability.py`. Adds `OspfIntf.metric`, a non-diff-bearing observational `OspfNeighbor` entity fed by a self-isolating ingester, and the `OSPF_TELEMETRY` capability.

**Tech Stack:** Python 3.14, uv, pytest/ruff/mypy, mistapi SDK. Spec: `docs/superpowers/specs/2026-06-22-gs27-ospf-transit-changes-design.md`.

## Global Constraints

- **Never false-SAFE.** Verdict precedence UNKNOWN > UNSAFE > REVIEW > SAFE. Telemetry is **escalate-only**: it may add/raise findings, never produce PASS/SAFE or remove a finding.
- Gate (every task ends green): `uv run pytest tests -q && uv run ruff check . && uv run mypy src`. Ruff line length 100. mypy strict on `src` (tests not type-checked). Pyright IDE diagnostics are noise.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- `.env` (MIST_HOST/MIST_APITOKEN) is gitignored, NEVER committed. Live runs are READ-ONLY (simulate only). Live org `9777c1a0-6ef6-11e6-8bbf-02e208b2d34f`, Live-Demo Cupertino site `978c48e6-6ef6-11e6-8bbf-02e208b2d34f`.
- `OspfNeighbor` is OBSERVATIONAL and non-load-bearing: not in `diff._ENTITY_KINDS`, no strict IR validation, a self-isolating ingester that can never flip `report.ok`.
- The live org has **zero OSPF** — telemetry is built blind (synthetic fixtures only); live-verify is regression-only.
- `searchSiteOspfStats(session, site_id)` → `/sites/{id}/stats/ospf_peers/search`; record field names confirmed against the Mist OAS at build; the ingester is fail-soft (unknown field → that row counted in `ospf_telemetry_unparsed_count`).

---

## File map

```
ir/capabilities.py            + IRCapability.OSPF_TELEMETRY
ir/entities.py                OspfIntf.metric; new OspfNeighbor
ir/model.py                   IR.ospf_neighbors + ospf_telemetry_unparsed_count + builder setters + build wiring
ir/__init__.py                export OspfNeighbor
ir/diff.py                    (no change — OspfNeighbor deliberately absent from _ENTITY_KINDS)
adapters/mist/ingest/switch.py            _metric_int; _ospf reads metric
adapters/mist/ingest/ospf_neighbors.py    NEW self-isolating OspfNeighborIngester
adapters/mist/adapter.py                  register OspfNeighborIngester
scope/allowlist.py            + ospf_areas.*.networks.*.metric
providers/base.py             RawSiteState.ospf_neighbors
providers/mist_api.py         _ospf_neighbors (searchSiteOspfStats), fail-soft, meta.fetched
observability/replay/store.py + ospf_neighbors save/load
analysis/ospf_reachability.py  NEW pure module
checks/wired/ospf_withdrawal.py  per-area participation, 5 structural codes, telemetry escalation, precise applies_to
tests/golden/builders.py + tests/golden/test_golden_scenarios.py  GS27 goldens + GS26 update
```

---

## Phase 1 — data layer

### Task 1: `OspfIntf.metric` + ingest + allowlist

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (OspfIntf, ~line 253)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_ospf`, ~line 754)
- Modify: `src/digital_twin/scope/allowlist.py` (~line 104)
- Test: `tests/adapters/mist/test_ospf_ingest.py` (extend if exists, else create), `tests/scope/test_field_gate.py`

**Interfaces:**
- Produces: `OspfIntf.metric: int | None` (None = absent/unparseable); diff-bearing automatically (new dataclass field, not in any ignore set).

- [ ] **Step 1: Write the failing test** — create/extend `tests/adapters/mist/test_ospf_ingest.py`:

```python
from datetime import UTC, datetime
from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(*, devices, setting) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"},
        setting=setting, networktemplate=None, devices=tuple(devices), device_stats=(),
        port_stats=(), wireless_clients=(), wired_clients=(), derived_setting=None,
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t",
                       fetched=("site", "setting", "devices"), failures=()),
    )


def test_ospf_metric_minted_and_absent_is_none():
    dev = {"mac": "001122334455", "type": "switch", "name": "sw",
           "ospf_config": {"enabled": True},
           "ospf_areas": {"0": {"networks": {"corp": {"metric": 50}, "guest": {}}}}}
    setting = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"},
                            "guest": {"vlan_id": 20, "subnet": "10.0.1.0/24"}}}
    ir = MistAdapter().ingest(_raw(devices=[dev], setting=setting)).ir
    by_name = {o.network_name: o for o in ir.ospf_intfs}
    assert by_name["corp"].metric == 50
    assert by_name["guest"].metric is None      # absent -> None


def test_ospf_metric_templated_is_none():
    dev = {"mac": "001122334455", "type": "switch", "name": "sw",
           "ospf_config": {"enabled": True},
           "ospf_areas": {"0": {"networks": {"corp": {"metric": "{{cost}}"}}}}}
    setting = {"networks": {"corp": {"vlan_id": 10, "subnet": "10.0.0.0/24"}}}
    ir = MistAdapter().ingest(_raw(devices=[dev], setting=setting)).ir
    assert next(o for o in ir.ospf_intfs if o.network_name == "corp").metric is None
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/adapters/mist/test_ospf_ingest.py -q` → FAIL (`metric` attribute missing / unexpected kwarg).

- [ ] **Step 3: Add the field** in `entities.py` `OspfIntf`, after `passive: bool = False`:

```python
    passive: bool = False
    metric: int | None = None        # OSPF cost; None = absent/unparseable (Mist default)
```

- [ ] **Step 4: Parse + mint** in `switch.py`. Add a module-level parser near `_vlan_int`:

```python
def _metric_int(value: Any) -> int | None:
    """OSPF metric -> int or None (templated/unparseable/absent -> None)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
```

In `_ospf`, pass it to the `OspfIntf(...)` constructor:

```python
                        passive=bool(ncfg.get("passive", False)),
                        metric=_metric_int(ncfg.get("metric")),
                        unresolved=(vid is None),
```

- [ ] **Step 5: Allowlist the leaf** in `allowlist.py`, in the OSPF leaf tuple (the one containing `"ospf_areas.*.networks.*.passive"`):

```python
    "ospf_config.enabled",
    "ospf_areas.*.networks.*.passive",
    "ospf_areas.*.networks.*.metric",
```

- [ ] **Step 6: Add the field-gate test** to `tests/scope/test_field_gate.py` (a metric edit is now in-scope, not UNKNOWN). Mirror an existing OSPF field-gate test; assert `screen_op` returns `None` for a payload changing only `ospf_areas.0.networks.corp.metric`. (If no OSPF field-gate test exists, assert via `changed_paths`/`allowed` that `ospf_areas.0.networks.corp.metric` is allowed.)

- [ ] **Step 7: Run + gate** — `uv run pytest tests/adapters/mist/test_ospf_ingest.py tests/scope/test_field_gate.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`. All green.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/ir/entities.py src/digital_twin/adapters/mist/ingest/switch.py src/digital_twin/scope/allowlist.py tests/adapters/mist/test_ospf_ingest.py tests/scope/test_field_gate.py
git commit -m "feat(gs27): model OspfIntf.metric + allowlist the metric leaf"
```

### Task 2: `OspfNeighbor` entity + `OSPF_TELEMETRY` + IR wiring

**Files:**
- Modify: `src/digital_twin/ir/capabilities.py`, `src/digital_twin/ir/entities.py`, `src/digital_twin/ir/model.py`, `src/digital_twin/ir/__init__.py`
- Test: `tests/ir/test_ospf_neighbor.py` (new)

**Interfaces:**
- Produces: `OspfNeighbor(device_id, peer_ip, area=None, state="", vrf=None, neighbor_router_id=None, meta=OBSERVED_META, id="")` with `id = f"{device_id}:ospfnbr:{area or '*'}:{peer_ip}"`; `IR.ospf_neighbors: tuple[OspfNeighbor, ...]`; `IR.ospf_telemetry_unparsed_count: int`; `IRBuilder.set_ospf_neighbors(neighbors, unparsed_count)`; `IRCapability.OSPF_TELEMETRY`.

- [ ] **Step 1: Write the failing test** `tests/ir/test_ospf_neighbor.py`:

```python
from digital_twin.ir import IRCapability, OspfNeighbor
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir(neighbors, unparsed=0):
    return (IRBuilder().with_capability(IRCapability.WIRED_L2)
            .set_ospf_neighbors(neighbors, unparsed).build())


def test_ospf_neighbor_id_and_absent_area():
    n = OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Full")
    assert n.area is None and n.id == "d1:ospfnbr:*:10.0.0.5"
    n2 = OspfNeighbor(device_id="d1", peer_ip="10.0.0.6", area="0")
    assert n2.id == "d1:ospfnbr:0:10.0.0.6"


def test_ospf_neighbor_is_not_diff_bearing():
    base = _ir([OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", state="Full")])
    prop = _ir([])  # neighbor vanished
    assert diff_ir(base, prop).is_empty()       # telemetry change != config change


def test_unparsed_count_and_capability_carried():
    ir = _ir([OspfNeighbor(device_id="d1", peer_ip="10.0.0.5")], unparsed=3)
    assert ir.ospf_telemetry_unparsed_count == 3
    # capability is earned by the ingester, not the builder setter — see Task 4.
```

- [ ] **Step 2: Run, expect FAIL** — import error / missing setter.

- [ ] **Step 3: Capability** — in `capabilities.py` add to `IRCapability`:

```python
    OSPF_TELEMETRY = "ospf.telemetry"  # site_ospf neighbor stats fetched (peer-break layer)
```

- [ ] **Step 4: Entity** — in `entities.py` (after `OspfIntf`), import `OBSERVED_META` if not already, and add:

```python
@dataclass(frozen=True)
class OspfNeighbor:
    """OBSERVATIONAL live OSPF adjacency (site_ospf stats). Evidence/escalation
    input only: NOT in diff_ir, no strict IR validation. `area=None` means the
    telemetry omitted the area -> the reachability join matches on subnet only."""

    device_id: str
    peer_ip: str
    area: str | None = None
    state: str = ""                       # raw Mist state, e.g. "Full"
    vrf: str | None = None
    neighbor_router_id: str | None = None
    meta: FactMeta = OBSERVED_META
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            object.__setattr__(self, "id", f"{self.device_id}:ospfnbr:{self.area or '*'}:{self.peer_ip}")
```

- [ ] **Step 5: IR + builder** — in `model.py`:
  - `IR`: add after `client_enrichment`:
    ```python
    ospf_neighbors: tuple[OspfNeighbor, ...] = ()
    ospf_telemetry_unparsed_count: int = 0
    ```
  - `IRBuilder.__init__`: `self._ospf_neighbors: list[OspfNeighbor] = []` and `self._ospf_unparsed = 0`.
  - Add setter (mirror `set_client_enrichment`):
    ```python
    def set_ospf_neighbors(self, neighbors: Iterable[OspfNeighbor], unparsed_count: int = 0) -> IRBuilder:
        self._ospf_neighbors = list(neighbors)
        self._ospf_unparsed = unparsed_count
        return self
    ```
  - `build()`: pass `ospf_neighbors=tuple(self._ospf_neighbors), ospf_telemetry_unparsed_count=self._ospf_unparsed`.
  - Import `OspfNeighbor` at top of `model.py`. Do **not** add validation for neighbors (non-load-bearing).

- [ ] **Step 6: Export** — `ir/__init__.py`: add `OspfNeighbor` to the entities import + `__all__`.

- [ ] **Step 7: Confirm NOT diff-bearing** — do nothing to `diff.py` (`OspfNeighbor` is intentionally absent from `_ENTITY_KINDS`); `test_ospf_neighbor_is_not_diff_bearing` pins it.

- [ ] **Step 8: Run + gate** — `uv run pytest tests/ir/test_ospf_neighbor.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

- [ ] **Step 9: Commit**

```bash
git add src/digital_twin/ir/ tests/ir/test_ospf_neighbor.py
git commit -m "feat(gs27): OspfNeighbor observational entity + OSPF_TELEMETRY capability (non-diff-bearing)"
```

---

## Phase 2 — fetch plumbing + self-isolating ingester

### Task 3: `site_ospf` fetch + RawSiteState + replay round-trip

**Files:**
- Modify: `src/digital_twin/providers/base.py` (RawSiteState), `src/digital_twin/providers/mist_api.py`, `src/digital_twin/observability/replay/store.py`
- Modify: `tests/adapters/mist/fixtures.py` (add `ospf_neighbors` kwarg to `raw_site`)
- Test: `tests/observability/test_replay_store.py` (extend — this is where replay tests actually live)

**Interfaces:**
- Produces: `RawSiteState.ospf_neighbors: tuple[JsonObj, ...] = ()`; `meta.fetched` contains `"ospf_neighbors"` on success; replay docs carry `ospf_neighbors`. `raw_site(..., ospf_neighbors=())`.

- [ ] **Step 1: Extend the `raw_site` helper** in `tests/adapters/mist/fixtures.py`: add `ospf_neighbors: tuple[dict[str, Any], ...] = ()` to the signature and pass `ospf_neighbors=ospf_neighbors` into the `RawSiteState(...)` it returns.

- [ ] **Step 2: Write the failing test** — in `tests/observability/test_replay_store.py` (mirror the existing `test_wlans_round_trip_and_default_when_absent`; redaction REMAPS `peer_ip`/`mac`, so assert on `state`, which is preserved):

```python
def test_ospf_neighbors_round_trip_and_default_when_absent(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site(ospf_neighbors=(
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "area": "0", "state": "Full"},)))
    raw = load_fixture_raw(path)
    assert raw.ospf_neighbors and raw.ospf_neighbors[0]["state"] == "Full"
    # a fixture predating GS27 (no "ospf_neighbors" key) loads as empty, not a crash
    data = json.loads(path.read_text())
    del data["ospf_neighbors"]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(data))
    assert load_fixture_raw(legacy).ospf_neighbors == ()
```

- [ ] **Step 3: Run, expect FAIL** — `RawSiteState`/`raw_site` has no `ospf_neighbors` / loader drops it.

- [ ] **Step 4: RawSiteState field** — `providers/base.py`, after `nac_clients`:

```python
    # observed OSPF neighbor stats (GET /sites/{id}/stats/ospf_peers/search) — the
    # GS27 telemetry layer. Trailing + defaulted: absence is "not fetched".
    ospf_neighbors: tuple[JsonObj, ...] = ()
```

- [ ] **Step 5: Provider fetch** — `providers/mist_api.py`, add a fetch method mirroring `_nac_clients`:

```python
    def _ospf_neighbors(self, s: SiteScope) -> list[_Json]:
        # OBSERVATIONAL OSPF adjacency telemetry (GS27). A failure is NON-FATAL —
        # `attempt` records it in StateMeta.failures and the OspfNeighborIngester
        # degrades to telemetry-blind, never UNKNOWN.
        resp = mistapi.api.v1.sites.stats.searchSiteOspfStats(self._session, s.site_id)
        return [dict(d) for d in mistapi.get_all(self._session, resp)]
```

In `_fetch_one`, in the `RawSiteState(...)` construction, add (mirroring `nac_clients`):

```python
            ospf_neighbors=tuple(attempt("ospf_neighbors", lambda: self._ospf_neighbors(scope), [])),
```

- [ ] **Step 6: Replay store** — `observability/replay/store.py`: add `"ospf_neighbors"` to `_RAW_FIELDS` (after `"nac_clients"`), and in `load_fixture_doc` add:

```python
        ospf_neighbors=tuple(data.get("ospf_neighbors", ())),  # .get: pre-GS27 fixtures
```

- [ ] **Step 7: Run + gate** — `uv run pytest tests/observability/test_replay_store.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

- [ ] **Step 8: Commit**

```bash
git add src/digital_twin/providers/ src/digital_twin/observability/replay/store.py tests/observability/test_replay_store.py tests/adapters/mist/fixtures.py
git commit -m "feat(gs27): fetch site_ospf neighbors (fail-soft) + RawSiteState + replay round-trip"
```

### Task 4: self-isolating `OspfNeighborIngester`

**Files:**
- Create: `src/digital_twin/adapters/mist/ingest/ospf_neighbors.py`
- Modify: `src/digital_twin/adapters/mist/adapter.py` (register)
- Test: `tests/adapters/mist/test_ospf_neighbor_ingest.py` (new)

**Interfaces:**
- Consumes: `RawSiteState.ospf_neighbors`, `ctx.raw.meta.fetched`, `IRBuilder.set_ospf_neighbors`, `IRCapability.OSPF_TELEMETRY`.
- Produces: earns `OSPF_TELEMETRY` iff `"ospf_neighbors" in fetched`; publishes neighbors + `ospf_telemetry_unparsed_count`. Field map (confirm names vs OAS at build): device mac `mac`; `peer_ip`; `area`; state `state`/`status`; `vrf_name`; `neighbor_router_id`/`router_id`.

- [ ] **Step 1: Write the failing test** `tests/adapters/mist/test_ospf_neighbor_ingest.py`:

```python
from datetime import UTC, datetime
from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.ir import IRCapability
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(neighbors, *, fetched=("site", "setting", "devices", "ospf_neighbors")):
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={"id": "s1"},
        setting={"networks": {}}, networktemplate=None,
        devices=({"mac": "001122334455", "type": "switch", "name": "sw"},),
        device_stats=(), port_stats=(), wireless_clients=(), wired_clients=(),
        derived_setting=None, ospf_neighbors=tuple(neighbors),
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=fetched, failures=()),
    )


def test_neighbors_parsed_and_capability_earned():
    out = MistAdapter().ingest(_raw([
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "area": "0", "state": "Full"}]))
    assert out.report.ok and out.ir is not None
    assert IRCapability.OSPF_TELEMETRY in out.ir.capabilities
    assert len(out.ir.ospf_neighbors) == 1 and out.ir.ospf_telemetry_unparsed_count == 0


def test_partial_unparsed_rows_counted_not_fatal():
    out = MistAdapter().ingest(_raw([
        {"mac": "001122334455", "peer_ip": "10.0.0.5", "state": "Full"},  # ok
        {"garbage": True},                                                # no peer_ip
    ]))
    assert out.report.ok                              # self-isolating: never fatal
    assert IRCapability.OSPF_TELEMETRY in out.ir.capabilities
    assert len(out.ir.ospf_neighbors) == 1 and out.ir.ospf_telemetry_unparsed_count == 1


def test_not_fetched_earns_nothing():
    out = MistAdapter().ingest(_raw([], fetched=("site", "setting", "devices")))
    assert IRCapability.OSPF_TELEMETRY not in out.ir.capabilities
    assert out.ir.ospf_neighbors == ()
```

- [ ] **Step 2: Run, expect FAIL** — module missing.

- [ ] **Step 3: Implement** `ospf_neighbors.py`:

```python
"""OSPF neighbor telemetry ingester (GS27). OBSERVATIONAL, SELF-ISOLATING: it
never lets an exception reach IngesterRegistry.run. Earns OSPF_TELEMETRY iff the
site_ospf fetch succeeded (shape reachable, incl. genuinely-zero). A row with no
usable peer_ip is COUNTED as unparsed (not silently dropped) so a partially
unrecognized fetch reads telemetry-blind, never 'no neighbors'."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from digital_twin.ir import IRCapability, OspfNeighbor

from .base import IngestContext

_Json = Mapping[str, Any]


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _row_to_neighbor(row: _Json) -> OspfNeighbor | None:
    # Field names confirmed against the Mist OAS (ospf_peers) at build; fail-soft.
    mac = _clean(row.get("mac"))
    peer_ip = _clean(row.get("peer_ip") or row.get("neighbor_ip"))
    if not mac or not peer_ip:
        return None                       # unusable -> caller counts it unparsed
    from digital_twin.ir import device_id  # local import: avoid cycle at module load
    return OspfNeighbor(
        device_id=device_id(mac),
        peer_ip=peer_ip,
        area=_clean(row.get("area")),
        state=_clean(row.get("state") or row.get("status")) or "",
        vrf=_clean(row.get("vrf_name") or row.get("vrf")),
        neighbor_router_id=_clean(row.get("neighbor_router_id") or row.get("router_id")),
    )


def build_ospf_neighbors(rows: tuple[_Json, ...]) -> tuple[list[OspfNeighbor], int]:
    neighbors: list[OspfNeighbor] = []
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


class OspfNeighborIngester:
    """Earns OSPF_TELEMETRY on fetch-success; publishes neighbors + unparsed count."""

    name = "ospf_neighbors"

    def produces(self) -> frozenset[str]:
        return frozenset({IRCapability.OSPF_TELEMETRY})

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        if "ospf_neighbors" not in ctx.raw.meta.fetched:
            return frozenset()            # not fetched -> telemetry-blind, no claim
        try:
            neighbors, unparsed = build_ospf_neighbors(tuple(ctx.raw.ospf_neighbors))
            ctx.builder.set_ospf_neighbors(neighbors, unparsed)
        except Exception:  # noqa: BLE001 — best-effort: degrade to blind, never fatal
            return frozenset()
        return frozenset({IRCapability.OSPF_TELEMETRY})
```

Note: confirm `device_id` is exported from `digital_twin.ir` (it is — used by `client_enrichment.py`). Confirm `ctx.builder` and `ctx.raw` match `IngestContext` (see `client_enrichment.py`).

- [ ] **Step 4: Register** — `adapters/mist/adapter.py`: import `OspfNeighborIngester` and append it to the ingester list (after `ClientEnrichmentIngester()`):

```python
            else [SwitchIngester(), LldpIngester(), ClientsIngester(), WlanIngester(),
                  ClientEnrichmentIngester(), OspfNeighborIngester()]
```

- [ ] **Step 5: Run + gate** — `uv run pytest tests/adapters/mist/test_ospf_neighbor_ingest.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/ospf_neighbors.py src/digital_twin/adapters/mist/adapter.py tests/adapters/mist/test_ospf_neighbor_ingest.py
git commit -m "feat(gs27): self-isolating OspfNeighborIngester (earns OSPF_TELEMETRY, counts unparsed)"
```

---

## Test harness (read before Phase 3)

`tests/checks/test_ospf_withdrawal.py` already provides the harness GS27 tests extend — **use it, don't reinvent**:
- `_ir(ospf_rows, *, clients=(), routed=(10, 20, 30), with_clients_cap=True)` — builds a switch `sw("S")`, a `Vlan(vlan_id=vid, subnet="198.51.{vid}.0/24")` + `irb("S", vid, …)` per routed vid, adds the `ospf_rows`, and earns `WIRED_L2 + L3_EXITS` (+`CLIENTS_ACTIVE`). Returns a built IR.
- `_run(base, prop)` — runs `OspfWithdrawalCheck()` over a `CheckContext(... diff=diff_ir(base, prop))`.
- Factories (`from tests.factories import ospf, sw, irb, access_port`): `sw(did="S")` → switch Device id `did`; `ospf(did, vlan, area="0", *, passive=False, name=None, unresolved=False)` → OspfIntf.

**Two harness extensions GS27 needs (make them in the first Phase-3/4 task that needs them):**
1. `tests/factories.py:ospf(...)` — add `metric: int | None = None` and pass it to `OspfIntf(..., metric=metric)`.
2. `_ir(...)` in `test_ospf_withdrawal.py` — add optional `neighbors=()` + `unparsed=0` (→ `b.set_ospf_neighbors(neighbors, unparsed)`), `telemetry_cap=False` (→ `b.with_capability(IRCapability.OSPF_TELEMETRY)` when `neighbors` or `telemetry_cap`), and `subnets: dict[int, str | None] | None = None` to override a routed vid's `Vlan.subnet` (`None` → mint `Vlan(vlan_id=vid, subnet=None, subnet_unresolved=True)` and skip its `irb`, for the unresolved-prefix tests). Build an `OspfNeighbor` from each `(device, peer_ip, area, state)` tuple. The `_run_ospf_*` names in the snippets below are thin wrappers you write over `_ir`/`_run` using these (a `telemetry=None` arg means "don't earn OSPF_TELEMETRY"; `telemetry=[...]` earns it + sets neighbors; `unparsed=N` adds dropped-row count) — the assert bodies are exact.

> All Phase 3/4 check work is in `src/digital_twin/checks/wired/ospf_withdrawal.py`. Read the spec Sections 2–4 before starting. Unit tests extend `tests/checks/test_ospf_withdrawal.py`.

### Task 5: per-area participation model + collision guard

**Files:**
- Modify: `checks/wired/ospf_withdrawal.py` (`_Seg`, `_participation`)
- Test: `tests/checks/test_ospf_withdrawal.py`

**Interfaces:**
- Produces: `_Row(passive: bool, metric: int | None)`; `_Seg.by_area: dict[str, _Row]`, `_Seg.active: bool` (derived), `_Seg.areas: set[str]` (derived), `_Seg.ambiguous_areas: set[str]`; `_participation(ir) -> _Part` with `by_dev_vlan: dict[tuple[str,int], _Seg]` unchanged in key shape.

- [ ] **Step 1: Write the failing test**:

```python
def test_participation_by_area_and_ambiguity():
    from digital_twin.checks.wired.ospf_withdrawal import _participation
    from tests.factories import ospf
    # two networks on the SAME (S, vlan 10, area 0) with DIFFERENT metric (needs the
    # ospf(metric=) factory extension from the Test harness note). vlan 10 is in _ir's
    # default routed=(10,20,30) so it already has a subnet.
    ir = _ir([ospf("S", 10, area="0", name="a", metric=5),
              ospf("S", 10, area="0", name="b", metric=9)])
    seg = _participation(ir).by_dev_vlan[("S", 10)]
    assert "0" in seg.ambiguous_areas    # differing metric -> ambiguous, no last-win
```

- [ ] **Step 2: Run, expect FAIL**.

- [ ] **Step 3: Implement** — replace `_Seg`/`_participation`:

```python
@dataclass(frozen=True)
class _Row:
    passive: bool
    metric: int | None


@dataclass
class _Seg:
    by_area: dict[str, _Row] = field(default_factory=dict)
    ambiguous_areas: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return any(not r.passive for r in self.by_area.values())

    @property
    def areas(self) -> set[str]:
        return set(self.by_area)


@dataclass
class _Part:
    by_dev_vlan: dict[tuple[str, int], _Seg]
    active_by_dev: dict[str, set[int]]


def _participation(ir: IR) -> _Part:
    by_dev_vlan: dict[tuple[str, int], _Seg] = {}
    active_by_dev: dict[str, set[int]] = {}
    for o in ir.ospf_intfs:
        if o.vlan_id is None:
            continue
        seg = by_dev_vlan.setdefault((o.device_id, o.vlan_id), _Seg())
        row = _Row(passive=o.passive, metric=o.metric)
        if o.area in seg.by_area and seg.by_area[o.area] != row:
            seg.ambiguous_areas.add(o.area)   # differing (passive, metric) -> ambiguous
        else:
            seg.by_area[o.area] = row
        if not o.passive:
            active_by_dev.setdefault(o.device_id, set()).add(o.vlan_id)
    return _Part(by_dev_vlan, active_by_dev)
```

(Keep imports `from dataclasses import dataclass, field`.)

- [ ] **Step 4: Run + gate** — targeted test green; `uv run pytest tests/checks/test_ospf_withdrawal.py -q` (existing tests may break where they read `_Seg.active`/`.areas` as plain attributes — they're now properties, so reads still work; fix any direct `_Seg(active=..., areas=...)` constructions in tests to use `by_area`). Full gate green.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/ospf_withdrawal.py tests/checks/test_ospf_withdrawal.py
git commit -m "feat(gs27): per-area OSPF participation (_Row metric/passive) + ambiguity guard"
```

### Task 6: the four ospf_intf-diff structural codes + precise applies_to

**Files:** `checks/wired/ospf_withdrawal.py`, `tests/checks/test_ospf_withdrawal.py`

**Interfaces:**
- Produces codes `wired.l3.ospf_withdrawal.{metric_changed,passive_flip,area_changed,participation_added}` (WARNING/REVIEW, `_UNVERIFIED`); removes `.transit_mutation`. `applies_to` = `diff.touches("ospf_intf") or _touches_vlan_subnet(diff)`. Helper `_touches_vlan_subnet(diff) -> bool`.

- [ ] **Step 1: Write the failing tests** — assert each code fires for the right edit and `.transit_mutation` is gone:

```python
# build base/prop IRs via the file's existing ctx helper. Examples:
def test_metric_changed_is_review():
    res = _run_ospf(base_metric=5, prop_metric=20)   # helper builds a retained (d1,vlan10) metric edit
    codes = {f.code for f in res.findings}
    assert "wired.l3.ospf_withdrawal.metric_changed" in codes
    assert res.status is Status.WARN

def test_passive_flip_non_collapsing_is_review():
    res = _run_ospf_two_active_one_flips()            # device keeps another active intf
    assert any(f.code.endswith(".passive_flip") for f in res.findings)
    assert res.status is Status.WARN

def test_participation_added_routed_is_review():
    res = _run_ospf_added()                            # (d1,vlan10) newly in OSPF (incl. bare {})
    assert any(f.code.endswith(".participation_added") for f in res.findings)

def test_transit_mutation_code_removed():
    res = _run_ospf_area_change()
    assert all(".transit_mutation" not in f.code for f in res.findings)
    assert any(f.code.endswith(".area_changed") for f in res.findings)

def test_applies_to_vlan_name_change_does_not_fire():
    from digital_twin.ir.diff import IRDiff, Modified, EntityRef
    diff = IRDiff((), (), (Modified(EntityRef("vlan", "10"), ("name",)),))
    assert OspfWithdrawalCheck().applies_to(diff) is False
    diff2 = IRDiff((), (), (Modified(EntityRef("vlan", "10"), ("subnet",)),))
    assert OspfWithdrawalCheck().applies_to(diff2) is True
```

(Write the small `_run_ospf*` helpers using the existing `ospf` test scaffolding in the file. Each returns `OspfWithdrawalCheck().run(ctx)`.)

- [ ] **Step 2: Run, expect FAIL**.

- [ ] **Step 3: Implement** — add the helper + replace the code-3 block. Helper (module level):

```python
def _touches_vlan_subnet(diff: IRDiff) -> bool:
    """vlan add/remove, or a modified vlan whose changed fields touch the subnet —
    never name/collisions/dhcp_sources/etc."""
    if any(r.kind == "vlan" for r in (*diff.added, *diff.removed)):
        return True
    return any(
        m.ref.kind == "vlan" and ({"subnet", "subnet_unresolved"} & set(m.changed_fields))
        for m in diff.modified
    )
```

`applies_to`:

```python
    def applies_to(self, diff: IRDiff) -> bool:
        return diff.touches("ospf_intf") or _touches_vlan_subnet(diff)
```

Replace the `# 3. retained participation mutated ...` block with precise per-area emission. For each retained `(did, vid)` not in `egress_owned_pairs` and `_routed(prop_ir, vid, prop_l3)`:
  - `b, p = base.by_dev_vlan[key], prop.by_dev_vlan[key]`
  - **area_changed**: if `b.areas != p.areas` → emit `.area_changed` (WARNING/`_UNVERIFIED`, subject vlan, evidence `{"device": did, "vlan": vid, "base_areas": sorted(b.areas), "proposed_areas": sorted(p.areas)}`).
  - For each `area in (b.areas & p.areas)` not in `b.ambiguous_areas | p.ambiguous_areas`:
    - if `b.by_area[area].passive != p.by_area[area].passive` → `.passive_flip`.
    - if `b.by_area[area].metric != p.by_area[area].metric` → `.metric_changed` (evidence includes `base`/`proposed` metric).
  - For each `area in (b.ambiguous_areas | p.ambiguous_areas)` → append a coverage note `f"OSPF vlan {vid} on {did} area {area} is claimed by multiple network entries with differing passive/metric — transit-change detection skipped"`.

**participation_added**: for `(did, vid)` in `set(prop.by_dev_vlan) - set(base.by_dev_vlan)`, if `_routed(prop_ir, vid, prop_l3)` → `.participation_added` (WARNING/`_UNVERIFIED`). (A *new area* on an existing `(did,vid)` is handled by `.area_changed` above, not here — never double-report.)

Each finding sets `caused_by=ctx.delta_index.causes("ospf_intf", [oi.id for oi in (*base_ir.ospf_intfs, *prop_ir.ospf_intfs) if oi.device_id == did and oi.vlan_id == vid])`. Use the existing `_HIGH`/`_UNVERIFIED`/`Status` machinery and the `worst` rollup already in `run()`.

- [ ] **Step 4: Run + gate** — targeted then full. Update any existing GS26 test that referenced `.transit_mutation` (there is one in the unit tests + one golden — golden handled in Task 10).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/ospf_withdrawal.py tests/checks/test_ospf_withdrawal.py
git commit -m "feat(gs27): precise structural codes (metric/passive/area/added) + precise applies_to; drop .transit_mutation"
```

### Task 7: `.advertised_prefix_changed` + structural prefix-coverage note

**Files:** `checks/wired/ospf_withdrawal.py`, `tests/checks/test_ospf_withdrawal.py`

**Interfaces:**
- Produces code `.advertised_prefix_changed` (WARNING/REVIEW) and a structural prefix-coverage PARTIAL note. Consumes `Vlan.subnet`, the OSPF participation sets, and `_touches_vlan_subnet`-style per-vlan subnet-touch detection.

- [ ] **Step 1: Write the failing tests**:

```python
def test_advertised_prefix_changed_review_not_pass():
    res = _run_ospf_subnet_edit(base="10.0.0.0/24", prop="10.0.0.0/23",
                                vlan_in_ospf=True, telemetry=())   # adjacency survives
    assert any(f.code.endswith(".advertised_prefix_changed") for f in res.findings)
    assert res.status is Status.WARN

def test_unresolved_prefix_on_ospf_vlan_is_review_note():
    res = _run_ospf_subnet_edit(base="10.0.0.0/24", prop=None,     # became unresolved
                                vlan_in_ospf=True, telemetry=())
    assert res.coverage.state is CoverageState.PARTIAL
    assert all(not f.code.endswith(".advertised_prefix_changed") for f in res.findings)

def test_subnet_edit_on_non_ospf_vlan_is_pass():
    res = _run_ospf_subnet_edit(base="10.0.0.0/24", prop="10.0.0.0/23",
                                vlan_in_ospf=False, telemetry=())
    assert res.status is Status.PASS and res.coverage.state is CoverageState.COMPLETE
```

- [ ] **Step 2: Run, expect FAIL**.

- [ ] **Step 3: Implement** — add a block over `(device, vlan)` pairs that are in OSPF participation in **both** IRs (active OR passive) whose vlan subnet was delta-touched. Reuse a canonical-net helper like GS31's (`ipaddress.ip_network(s, strict=False)` or import the existing `subnet_overlap._net` if exported; else a local `_net`). For each such `(did, vid)`:
  - `bnet, pnet = _net(base_ir.vlans[vid].subnet), _net(prop_ir.vlans[vid].subnet)` (guard missing vlan).
  - both resolve and `bnet != pnet` → `.advertised_prefix_changed` (subject vlan, caused_by the changed `vlan`: `ctx.delta_index.causes("vlan", [str(vid)])`).
  - either unresolved/None → append structural prefix-coverage note `f"OSPF-participating vlan {vid} advertised prefix could not be compared (unresolved/absent) — prefix-change impact unverifiable"`.
  - "delta-touched subnet" = `vid` is among the vlan refs whose `changed_fields` include `subnet`/`subnet_unresolved`, OR the vlan was added/removed (compute once from `ctx.diff`, mirror `_touches_vlan_subnet` but returning the touched vid set).

  Define `_subnet_touched_vids(diff) -> set[int]` returning the vlan ids; use it to scope both the finding and the note (relevance — never note an untouched vlan).

- [ ] **Step 4: Run + gate**.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/ospf_withdrawal.py tests/checks/test_ospf_withdrawal.py
git commit -m "feat(gs27): .advertised_prefix_changed + unresolved-prefix structural coverage note"
```

---

## Phase 4 — telemetry reachability

### Task 8: pure `analysis/ospf_reachability.py`

**Files:**
- Create: `src/digital_twin/analysis/ospf_reachability.py`
- Test: `tests/analysis/test_ospf_reachability.py` (new)

**Interfaces:**
- Produces (pure, IR-only):
  - `is_established(state: str) -> bool` — normalized `Full`.
  - `covering_dev_vlan(neighbor, ir) -> tuple[str, int] | None` — the `(device, vlan)` of the active OSPF interface whose predicted subnet covers `peer_ip` (area-matched; peer area None → subnet-only), else None.
  - `covered(neighbor, ir) -> bool` — `covering_dev_vlan(...) is not None`.
  - `broken_peers(base_ir, prop_ir) -> list[OspfNeighbor]` — **confirmed** breaks: established, covered-in-base, uncovered-in-prop, AND proposed coverage was evaluable (not the subnet→unresolved case).
  - `unevaluable_peers(base_ir, prop_ir) -> list[OspfNeighbor]` — covered-in-base but proposed coverage UNEVALUABLE (covering interface still active OSPF, subnet now unresolved) → REVIEW note, not a break.
  - `blind_peers(ir) -> list[OspfNeighbor]` — established but NOT covered (model couldn't place it).

- [ ] **Step 1: Write the adversarial failing tests** (this is the blind-built module — be thorough):

```python
from digital_twin.analysis.ospf_reachability import (
    broken_peers, blind_peers, covered, is_established)
from digital_twin.ir import IRCapability, OspfIntf, OspfNeighbor
from digital_twin.ir.entities import Vlan
from digital_twin.ir.model import IRBuilder


from tests.factories import sw   # build() validates ospf_intfs -> the switch must exist

def _switch_ir(*, intfs, vlans, neighbors):
    b = IRBuilder().add_device(sw("d1"))      # device id "d1" matches the OspfIntf rows below
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.OSPF_TELEMETRY)
    for v in vlans: b.add_vlan(v)
    for i in intfs: b.add_ospf_intf(i)
    b.set_ospf_neighbors(neighbors, 0)
    return b.build()


def test_is_established_normalizes():
    assert is_established("Full") and is_established(" full ")
    assert not is_established("Init") and not is_established("") and not is_established("2-Way")


def test_covered_in_subnet_area_match():
    ir = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")])
    assert covered(ir.ospf_neighbors[0], ir) is True


def test_peer_not_in_subnet_is_blind_not_broken():
    ir = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="192.168.9.9", area="0", state="Full")])
    assert covered(ir.ospf_neighbors[0], ir) is False
    assert ir.ospf_neighbors[0] in blind_peers(ir)


def test_broken_when_interface_goes_passive():
    base = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=False)],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")])
    prop = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=True)],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")])
    assert [n.peer_ip for n in broken_peers(base, prop)] == ["10.0.0.5"]


def test_subnet_exclude_breaks_peer():
    base = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")])
    prop = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.1.0/24")],   # excludes .0.5
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")])
    assert [n.peer_ip for n in broken_peers(base, prop)] == ["10.0.0.5"]


def test_proposed_unresolved_is_unevaluable_not_broken():
    # covered in baseline; proposed keeps the active OSPF interface but its subnet is
    # unresolved (None) -> coverage UNEVALUABLE -> blind, NOT a confirmed break.
    from digital_twin.analysis.ospf_reachability import unevaluable_peers
    from digital_twin.ir.entities import Vlan
    nb = [OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Full")]
    base = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=nb)
    prop = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet=None, subnet_unresolved=True)],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c")],
        neighbors=nb)
    assert broken_peers(base, prop) == []                      # NOT broken
    assert [n.peer_ip for n in unevaluable_peers(base, prop)] == ["10.0.0.5"]


def test_non_established_never_broken():
    base = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=False)],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Init")])
    prop = _switch_ir(
        vlans=[Vlan(vlan_id=10, name="c", subnet="10.0.0.0/24")],
        intfs=[OspfIntf(device_id="d1", vlan_id=10, area="0", network_name="c", passive=True)],
        neighbors=[OspfNeighbor(device_id="d1", peer_ip="10.0.0.5", area="0", state="Init")])
    assert broken_peers(base, prop) == []
```

- [ ] **Step 2: Run, expect FAIL** — module missing.

- [ ] **Step 3: Implement**:

```python
"""Pure OSPF reachability join (GS27 telemetry layer). IR-only, no I/O. Predicts
each active OSPF interface's connected subnet from Vlan.subnet, then asks whether a
live established peer is covered; broken_peers = covered-in-baseline, uncovered-in-
proposed. Escalate-only: a wrong prediction can only over- or under-flag, never SAFE."""

from __future__ import annotations

import ipaddress

from digital_twin.ir import OspfNeighbor
from digital_twin.ir.model import IR

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network
_ESTABLISHED = {"full"}


def is_established(state: str) -> bool:
    return state.strip().lower() in _ESTABLISHED


def _net(subnet: str | None) -> _Net | None:
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def _addr(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(ip)
    except ValueError:
        return None


def _active_intf_subnets(ir: IR) -> list[tuple[str, int, str, _Net]]:
    """(device_id, vlan_id, area, subnet) for each ACTIVE (non-passive) resolved OSPF intf."""
    out: list[tuple[str, int, str, _Net]] = []
    for o in ir.ospf_intfs:
        if o.passive or o.vlan_id is None:
            continue
        vlan = ir.vlans.get(o.vlan_id)
        net = _net(vlan.subnet) if vlan is not None else None
        if net is not None:
            out.append((o.device_id, o.vlan_id, o.area, net))
    return out


def covering_dev_vlan(neighbor: OspfNeighbor, ir: IR) -> tuple[str, int] | None:
    addr = _addr(neighbor.peer_ip)
    if addr is None:
        return None
    for did, vid, area, net in _active_intf_subnets(ir):
        if did != neighbor.device_id:
            continue
        if neighbor.area is not None and neighbor.area != area:
            continue                      # area given -> must match; absent -> subnet-only
        if addr in net:
            return (did, vid)
    return None


def covered(neighbor: OspfNeighbor, ir: IR) -> bool:
    return covering_dev_vlan(neighbor, ir) is not None


def _proposed_unevaluable(neighbor: OspfNeighbor, base_ir: IR, prop_ir: IR) -> bool:
    """The peer's baseline-covering (device, vlan) is STILL active OSPF in proposed but
    its subnet is now unresolved/None -> proposed coverage CANNOT be evaluated. This is
    'unknown/blind', NOT a confirmed break (do not escalate to UNSAFE)."""
    cv = covering_dev_vlan(neighbor, base_ir)
    if cv is None:
        return False
    did, vid = cv
    for o in prop_ir.ospf_intfs:
        if o.passive or o.device_id != did or o.vlan_id != vid:
            continue
        vlan = prop_ir.vlans.get(vid)
        if vlan is None or _net(vlan.subnet) is None:
            return True                   # active OSPF here, but subnet unevaluable
    return False


def broken_peers(base_ir: IR, prop_ir: IR) -> list[OspfNeighbor]:
    """CONFIRMED breaks only: established, covered-in-baseline, uncovered-in-proposed,
    and proposed coverage was EVALUABLE (covering interface structurally gone, or still
    active with a RESOLVED subnet that excludes the peer). The unevaluable case is blind."""
    return [
        n for n in base_ir.ospf_neighbors
        if is_established(n.state) and covered(n, base_ir) and not covered(n, prop_ir)
        and not _proposed_unevaluable(n, base_ir, prop_ir)
    ]


def unevaluable_peers(base_ir: IR, prop_ir: IR) -> list[OspfNeighbor]:
    """Baseline-covered established peers whose PROPOSED coverage is unevaluable (covering
    interface still active OSPF but subnet unresolved) -> a REVIEW coverage note, not a break."""
    return [
        n for n in base_ir.ospf_neighbors
        if is_established(n.state) and covered(n, base_ir) and not covered(n, prop_ir)
        and _proposed_unevaluable(n, base_ir, prop_ir)
    ]


def blind_peers(ir: IR) -> list[OspfNeighbor]:
    """Established peers the model could not place in THIS ir (no covering active subnet)."""
    return [n for n in ir.ospf_neighbors if is_established(n.state) and not covered(n, ir)]
```

- [ ] **Step 4: Run + gate** — `uv run pytest tests/analysis/test_ospf_reachability.py -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/analysis/ospf_reachability.py tests/analysis/test_ospf_reachability.py
git commit -m "feat(gs27): pure ospf_reachability (predict/cover/broken_peers, established-only)"
```

### Task 9: telemetry escalation + `.peer_unreachable` + relevance-scoped blind notes

**Files:** `checks/wired/ospf_withdrawal.py`, `tests/checks/test_ospf_withdrawal.py`

**Interfaces:**
- Consumes `broken_peers`/`blind_peers`/`is_established` from `analysis.ospf_reachability`; `OSPF_TELEMETRY`, `ospf_telemetry_unparsed_count`.
- Produces: escalation (an owning structural/withdrawal finding built ERROR/HIGH naming the peer); standalone `.peer_unreachable` (ERROR/HIGH) for un-owned breaks; relevance-scoped telemetry-blind + baseline-uncovered notes.

- [ ] **Step 1: Write the failing tests**:

```python
# telemetry tuples are (device_id, peer_ip, area, state); device "S" matches the _ir harness.
def test_passive_flip_with_live_peer_is_unsafe():
    res = _run_ospf_passive_flip(telemetry=[("S", "198.51.10.5", "0", "Full")])  # vid 10 subnet 198.51.10.0/24
    assert res.status is Status.FAIL
    assert any(f.code.endswith(".passive_flip") and f.severity is Severity.ERROR for f in res.findings)

def test_metric_change_with_live_peer_does_not_escalate():
    res = _run_ospf_metric_change(telemetry=[("S", "198.51.10.5", "0", "Full")])
    assert res.status is Status.WARN
    assert all(f.severity is not Severity.ERROR for f in res.findings)

def test_resolved_subnet_break_escalates_advertised_prefix_changed():
    # subnet 198.51.10.0/24 -> 198.51.99.0/24 (excludes the peer) on a RETAINED OSPF vlan:
    # the break HAS an owner -> escalate .advertised_prefix_changed, NOT .peer_unreachable.
    res = _run_ospf_subnet_edit(vid=10, base="198.51.10.0/24", prop="198.51.99.0/24",
                                vlan_in_ospf=True, telemetry=[("S", "198.51.10.5", "0", "Full")])
    assert any(f.code.endswith(".advertised_prefix_changed") and f.severity is Severity.ERROR
               for f in res.findings)
    assert all(not f.code.endswith(".peer_unreachable") for f in res.findings)

def test_subnet_to_unresolved_with_live_peer_is_review_note_not_unsafe():
    # subnet -> unresolved/None: proposed coverage is UNEVALUABLE (the interface is still
    # active OSPF, we just can't test containment) -> REVIEW note, NOT a confirmed break.
    res = _run_ospf_subnet_edit(vid=10, base="198.51.10.0/24", prop=None,
                                vlan_in_ospf=True, telemetry=[("S", "198.51.10.5", "0", "Full")])
    assert res.status is Status.WARN and res.coverage.state is CoverageState.PARTIAL
    assert all(not f.code.endswith(".peer_unreachable") and f.severity is not Severity.ERROR
               for f in res.findings)

def test_partial_unparsed_does_not_suppress_escalation():
    # one valid established peer that breaks via passive_flip + 1 unparsed row:
    # escalation still fires (UNSAFE) AND a PARTIAL note flags the dropped row.
    res = _run_ospf_passive_flip(telemetry=[("S", "198.51.10.5", "0", "Full")], unparsed=1)
    assert res.status is Status.FAIL
    assert any(f.severity is Severity.ERROR for f in res.findings)
    assert res.coverage.state is CoverageState.PARTIAL

def test_telemetry_absent_on_ospf_subnet_edit_is_review_note():
    res = _run_ospf_subnet_edit(vid=10, base="198.51.10.0/24", prop="198.51.10.0/23",
                                vlan_in_ospf=True, telemetry=None)   # OSPF_TELEMETRY not earned
    assert res.coverage.state is CoverageState.PARTIAL   # blind + OSPF-relevant -> note

def test_preexisting_blind_peer_untouched_device_no_note():
    # a blind peer (uncovered in BASELINE) on a device with NO structural finding + an
    # unrelated non-OSPF subnet edit -> no note, clean PASS.
    res = _run_unrelated_edit_with_blind_peer()
    assert res.coverage.state is CoverageState.COMPLETE and res.status is Status.PASS

def test_baseline_blind_peer_on_touched_device_emits_note():
    # POSITIVE relevance case: a baseline-uncovered (blind) peer whose DEVICE is delta-touched
    # (a structural OSPF edit on it) -> PARTIAL note (the branch must actually fire).
    res = _run_ospf_metric_change(  # a metric edit on device S touches it
        telemetry=[("S", "203.0.113.9", "0", "Full")])  # peer NOT in any S subnet -> blind
    assert res.coverage.state is CoverageState.PARTIAL
```

- [ ] **Step 2: Run, expect FAIL**.

- [ ] **Step 3: Implement** — in `run()`. **Partial parse loss must NOT disable escalation** — split the gate into two independent signals:
  - `telemetry_known = (IRCapability.OSPF_TELEMETRY in base_ir.capabilities and IRCapability.OSPF_TELEMETRY in prop_ir.capabilities)`.
  - `has_unparsed = base_ir.ospf_telemetry_unparsed_count > 0 or prop_ir.ospf_telemetry_unparsed_count > 0`.
  - **Escalation runs whenever `telemetry_known`** (the parsed rows are usable — one bad row never suppresses escalation for valid peers): `broken = broken_peers(base_ir, prop_ir)` (confirmed breaks only — subnet→unresolved is excluded by the pure module). Attribute each broken peer to the structural/withdrawal finding owning its baseline-covering `(did, vid)` (via `covering_dev_vlan(n, base_ir)`); if found AND adjacency-affecting (egress_lost / advertised_removed / passive_flip / area_changed / **advertised_prefix_changed** — NOT metric_changed / participation_added) → build that finding `Severity.ERROR`, `_HIGH`, naming the `peer_ip`(s). `.peer_unreachable` (ERROR/`_HIGH`) is a **defensive backstop** for a confirmed broken peer with no matched owner — not expected to fire given the attribution above (every confirmed break has a structural owner), kept so an attribution gap can never silently drop a confirmed break to SAFE.
  - **Unevaluable peers → REVIEW note (NOT a break):** for each `n in unevaluable_peers(base_ir, prop_ir)` (covered-in-base, proposed coverage unevaluable because the covering interface's subnet went unresolved) append a PARTIAL coverage note `f"OSPF peer {n.peer_ip} on {n.device_id}: proposed coverage unevaluable (the advertising prefix is unresolved) — adjacency impact not confirmed"`. This is relevant by construction (the subnet that went unresolved is delta-touched). Pairs with Task 7's structural prefix-coverage note; both → REVIEW, never UNSAFE.
    - Attribution: peer→`(did, vid)` = the active OspfIntf in `base_ir` on the peer's device whose vlan subnet contained `peer_ip`. Expose `covering_dev_vlan(neighbor, ir) -> tuple[str, int] | None` in `ospf_reachability` (it already computes this inside `covered` — return the `(device, vlan)` instead of a bool).
  - **Relevance-scoped notes (PARTIAL → REVIEW):**
    - **Blind note** when `not telemetry_known` **OR** `has_unparsed` (the unparsed *portion* is blind even though parsed peers escalated), appended **iff OSPF-relevant**: a structural OSPF finding exists, OR a `_subnet_touched_vids(diff)` vlan has active OSPF participation in base or prop.
    - **Baseline-uncovered (blind) peer note:** iterate `blind_peers(base_ir)` (peers the model could not place **in baseline** — `prop_ir` is wrong here). A blind peer has **no covering vlan** by definition, so scope by **device**: note it iff its `device_id` is delta-touched (an `ospf_intf` ref on that device in the diff, OR a `_subnet_touched_vids` vlan whose OSPF participation is on that device) OR a structural finding is on that device. Never note a blind peer on an untouched device.
  - Coverage `PARTIAL` iff any notes (existing logic).

- [ ] **Step 4: Run + gate** — targeted then full.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/ospf_withdrawal.py tests/checks/test_ospf_withdrawal.py
git commit -m "feat(gs27): telemetry escalation (established peer break -> UNSAFE) + .peer_unreachable + relevance-scoped blind notes"
```

---

## Phase 5 — goldens, live-verify, docs

### Task 10: end-to-end goldens + GS26 golden update

**Files:** `tests/golden/builders.py`, `tests/golden/test_golden_scenarios.py`

**Interfaces:** consumes `ospf_doc`/`ospf_op`/`OSPF_NETS` (existing). Add an `ospf_neighbors=` kwarg to `ospf_doc` (default `None`) that, when given, sets `doc["ospf_neighbors"]` and appends `"ospf_neighbors"` to `doc["meta"]["fetched"]`. Add `metric=`/`subnet edit` helpers as needed (a `subnet`-only op uses a `site_setting` `networks.<name>.subnet` change; reuse the config-lint `_cl_setting_op` shape or a device op that sets `other_ip_configs`/`networks` — match how `ospf_doc` wires subnets).

- [ ] **Step 1: Add the GS27 goldens** in `tests/golden/test_golden_scenarios.py` (GS26-style, via `simulate(plan, provider=FixtureProvider(write_doc(doc, …)))`). Cover (assert decision + a finding code):
  - metric change → REVIEW + `.metric_changed`
  - non-collapsing passive flip → REVIEW + `.passive_flip`
  - area move → REVIEW + `.area_changed`
  - participation added (bare `{}`) → REVIEW + `.participation_added`
  - OSPF-vlan subnet edit, adjacency survives, no telemetry → REVIEW + `.advertised_prefix_changed`
  - OSPF-vlan subnet → unresolved, telemetry usable+empty → REVIEW (PARTIAL coverage), no `.advertised_prefix_changed`
  - non-OSPF-vlan subnet edit → SAFE
  - passive flip + live established peer (telemetry) → UNSAFE + `.passive_flip` ERROR
  - resolved subnet edit that excludes a live peer (retained OSPF vlan) → UNSAFE + `.advertised_prefix_changed` ERROR (the break has an owner)
  - subnet → unresolved with a live peer → REVIEW (PARTIAL): proposed coverage unevaluable → prefix-coverage + unevaluable-peer notes, no UNSAFE, no `.peer_unreachable`
  - telemetry-absent on OSPF-active subnet edit → REVIEW + telemetry-blind note

- [ ] **Step 2: Update the GS26 golden** — find the existing scenario asserting `.transit_mutation` (non-collapsing flip) in `test_golden_scenarios.py` and change the expected code to `.passive_flip` (decision unchanged: REVIEW).

- [ ] **Step 3: Run + gate** — `uv run pytest tests/golden -q && uv run pytest tests -q && uv run ruff check . && uv run mypy src`. If a golden's decision is off, debug the SCENARIO (the unit tests pin behavior) — do not change the check.

- [ ] **Step 4: Commit**

```bash
git add tests/golden/builders.py tests/golden/test_golden_scenarios.py
git commit -m "test(gs27): end-to-end goldens (structural REVIEW + telemetry UNSAFE) + GS26 .transit_mutation->.passive_flip"
```

### Task 11: live regression verify + roadmap + memory + final gate

**Files:** `docs/ROADMAP.md`, `docs/superpowers/specs/2026-06-22-gs27-ospf-transit-changes-design.md`, memory.

- [ ] **Step 1: Live read-only verify** — with `.env` sourced, run a small script (mirror the config-lint live-verify pattern): fetch the Live-Demo site, ingest, confirm `report.ok`, `IRCapability.OSPF_TELEMETRY` earned (empty fetch → zero neighbors, `ospf_telemetry_unparsed_count == 0`), `state_meta.fetched` contains `ospf_neighbors`, and `ir.ospf_intfs == ()` (no live OSPF). Then simulate the 8 existing live test plans and confirm **decisions / findings / check statuses unchanged** vs main, aside from `state_meta.fetched` gaining `ospf_neighbors`. READ-ONLY; never apply. Delete the temp script after.

- [ ] **Step 2: Spec status** — flip to `Implemented — live-verified (regression) 2026-06-22`.

- [ ] **Step 3: Roadmap** — mark GS27 done in `docs/ROADMAP.md` (the GS27 bullet) with the new codes; note the telemetry layer is built blind (no live OSPF) and the deferred "ground/validate on a real OSPF org" follow-up.

- [ ] **Step 4: Memory** — add a bullet to `digital-twin-project.md`: the OSPF transit tier (5 structural codes replacing `.transit_mutation`, `metric` modeled, `OspfNeighbor`/`OSPF_TELEMETRY` non-load-bearing, the escalate-only telemetry layer + `analysis/ospf_reachability.py`, the false-SAFE surfaces closed across 5 review rounds, live regression-only).

- [ ] **Step 5: Final gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add docs/ROADMAP.md docs/superpowers/specs/2026-06-22-gs27-ospf-transit-changes-design.md
git commit -m "docs(gs27): Implemented + roadmap + live regression verify"
```

---

## Notes for the implementer

- **Read first:** the approved spec (all 4 sections), `checks/wired/ospf_withdrawal.py` (GS26), and `adapters/mist/ingest/client_enrichment.py` (the self-isolating pattern GS27 mirrors).
- **Confidence/Status objects** already exist in the check: `_HIGH`, `_UNVERIFIED`, `_EGRESS_UNCONFIRMED`, `Status.{PASS,WARN,FAIL}`, `Coverage`, `CoverageState`. Reuse them; do not invent new ones.
- **Redaction caveat (replay only):** redaction *remaps* IPs deterministically per-string, so a replay fixture's `peer_ip` and `Vlan.subnet` remap independently → containment fails → telemetry-blind in replay (safe-side). Goldens therefore use **non-redacted synthetic** test IPs (e.g. `10.0.0.0/24`, peer `10.0.0.5`) written directly into the doc, not run through redaction.
- **mypy:** `IR.ospf_neighbors`/`ospf_telemetry_unparsed_count` need defaults (trailing fields). The pure module's `_Net` union mirrors `subnet_overlap.py`.
- **Never widen `requires()`** — it stays `{WIRED_L2, L3_EXITS}`; telemetry is conditional inside `run()`.
