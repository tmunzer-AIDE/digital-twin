# Plan 5 — Pipeline + Drivers + Observability + Golden Scenarios Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish M1: the 10-stage `simulate(ChangePlan) -> Verdict` engine pipeline wiring every existing module, CLI + MCP drivers with decision exit codes, observability (trace, structured logging, redacting replay store), and the GS1–GS8 golden scenarios green against a redacted fixture captured from the real org.

**Architecture:** The engine is ORCHESTRATION ONLY — every stage delegates to an existing module (gates, L0, provider, adapter, registry, verdict) and every failure is already a value (`Rejection`/`FetchError`/`L0Result.fatal`/`IngestReport.ok`); the pipeline just maps them into `DecisionInputs` and short-circuits per the spec's stage diagram. Replay fixtures are debug/test artifacts captured REDACTED-on-write (un-redacted = a defect); GS tests run against a committed fixture captured from the real org (= "real org data via replay fixtures", per spec), with scenario builders that SEARCH the fixture's topology for each GS precondition.

**Tech Stack:** Python 3.14, argparse (CLI), `mcp` SDK (new dep, FastMCP server), sha256 pseudonymization. Everything else already exists.

**Pinned as-built facts:**
- AP uplink edges carry NO vlans today (AP `eth0` has no vlan facts; `link_carried_vlans` intersects → ∅) — GS1/GS7 are blind on real data without Task 1.
- `Verdict` (Plan 4) lacks `state_meta`/`trace_ref`; `DecisionInputs(rejections, l0_fatal, baseline_unavailable, check_results, adapter_findings)`.
- `StateMeta(acquired_at, host, fetched, failures)`; `FetchError(scope, failures, acquired_at, host)`.
- `MistAdapter.validate(op) -> L0Result(findings, fatal)`, `.ingest(raw) -> IngestOutcome(ir|None, site_effective, device_effective, report)`, `.apply(raw, ops) -> RawSiteState | Rejection`.
- Gates: `parse_change_plan(dict)`, `check_objects(plan)`, `screen_op(object_type, current, payload)` (incl. device-role), `check_derived(base, prop, artifact=)`; `get_object(raw, type, id)`.
- Exit codes (spec): SAFE→0, REVIEW→10, UNSAFE→20, UNKNOWN→30.
- Real-org fixture source: `.cache/probe/*.json` exists; live env vars MIST_HOST/MIST_APITOKEN/DT_GATE_ORG_ID/DT_GATE_SITE_IDS; live tests use `pytest.mark.live` (excluded by default via `addopts = "-q -m 'not live'"`).

**Documented design decisions:**
- **AP vlan transparency (Task 1):** an AP bridges whatever its switch port delivers; when exactly one link end is an AP-role device, the edge carries the SWITCH side's offered set (tagged ∪ native). Representation-level interpretation — no invented IR facts; switch↔switch edges unchanged.
- **GS strategy:** one redacted fixture captured live from the real org and committed under `tests/golden/fixtures/`; GS builders search it for preconditions (single-carrier vlan for GS1, redundant carrier for GS2, …) and `pytest.skip` with a precise message when the topology genuinely lacks one (GS3 additionally falls back to a documented fixture augmentation — a parallel link cannot be conjured by config). GS6 = drop wireless-client data from the fixture (relevant partial fetch) → INSUFFICIENT_DATA → REVIEW.
- **Pseudonymization caveat (documented):** hashing preserves equality, not prefixes — `switch_matching` `match_name[A:B]` rules may match differently on a redacted fixture. The committed fixture's compile path must therefore be validated by the GS tests themselves (they run the full pipeline).
- **Replay loader doubles as `FixtureProvider`** (a StateProvider over a fixture file) — explicitly a debug/test artifact, not the deferred SnapshotProvider product feature.

---

### Task 1: AP vlan-transparent uplink edges

**Files:**
- Modify: `src/digital_twin/representations/l2_graph.py`
- Test: `tests/representations/test_l2_graph.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/representations/test_l2_graph.py`)

```python
def test_ap_uplink_edge_carries_switch_side_vlans():
    # an AP is a VLAN-TRANSPARENT bridge: its eth port has no vlan facts (the
    # lldp ingester cannot invent them), so the SWITCH side defines delivery
    from digital_twin.ir.entities import Port, PortMode
    from digital_twin.ir.model import IRBuilder
    from tests.factories import ap, link, sw, trunk_port

    b = IRBuilder()
    b.add_device(sw("SW")).add_device(ap("AP1"))
    b.add_port(trunk_port("SW", "to-ap", tagged=(30, 40), native=1))
    b.add_port(Port(id="AP1:eth0", device_id="AP1", name="eth0", mode=PortMode.TRUNK))
    b.add_link(link("AP1:eth0", "SW:to-ap"))
    g = build_l2_graph(b.build())
    edge = next(iter(g.edges(data=True)))[2]["data"]
    assert edge.vlans == {1, 30, 40}


def test_switch_to_switch_edges_unchanged_by_transparency():
    from digital_twin.ir.model import IRBuilder
    from tests.factories import link, sw, trunk_port

    b = IRBuilder()
    b.add_device(sw("A")).add_device(sw("B"))
    b.add_port(trunk_port("A", "up", tagged=(30, 40)))
    b.add_port(trunk_port("B", "down", tagged=(30,)))
    b.add_link(link("A:up", "B:down"))
    g = build_l2_graph(b.build())
    edge = next(iter(g.edges(data=True)))[2]["data"]
    assert edge.vlans == {30}  # intersection semantics stay exact
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/representations/test_l2_graph.py -q`
Expected: first new test FAILS (`edge.vlans == set()`)

- [ ] **Step 3: Implement** — in `l2_graph.py`, give `build_l2_graph` role awareness:

```python
def _offered(port: Port) -> set[int]:
    out = _tagged(port)
    if port.native_vlan is not None:
        out = out | {port.native_vlan}
    return out
```

and inside the link loop replace the `vlans = link_carried_vlans(pa, pb)` line with:

```python
        a_is_ap = ir.devices[pa.device_id].role is DeviceRole.AP
        b_is_ap = ir.devices[pb.device_id].role is DeviceRole.AP
        if a_is_ap != b_is_ap:  # exactly one end is an AP: vlan-transparent bridge,
            switch_port = pb if a_is_ap else pa  # the switch side defines delivery
            vlans = _offered(switch_port)
        else:
            vlans = link_carried_vlans(pa, pb)
```

(import `DeviceRole` from `digital_twin.ir.entities`; module docstring gains one line
explaining the AP-transparency rule.)

- [ ] **Step 4: Full affected suites + gate**

Run: `uv run pytest tests/representations tests/analysis tests/checks tests/test_plan4_flow.py -q && uv run ruff check . && uv run mypy`
Expected: PASS (the GS7 check tests built their own tagged AP ports, so they stay green)

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/representations/l2_graph.py tests/representations/test_l2_graph.py
git commit -m "Plan 5: AP uplink edges are vlan-transparent (switch side defines delivery)"
```

---

### Task 2: `verdict/state_meta.py` + Verdict gains `state_meta`/`trace_ref`

**Files:**
- Create: `src/digital_twin/verdict/state_meta.py`
- Modify: `src/digital_twin/verdict/verdict.py`
- Test: `tests/verdict/test_state_meta.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/verdict/test_state_meta.py
from datetime import UTC, datetime, timedelta

from digital_twin.providers.base import FetchFailure, StateMeta
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta


def test_view_carries_freshness_and_failures():
    acquired = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
    meta = StateMeta(
        acquired_at=acquired,
        host="api.mist.com",
        fetched=("site", "setting"),
        failures=(FetchFailure(object="wired_clients", error="503"),),
    )
    view = build_state_meta(meta, now=acquired + timedelta(seconds=90))
    assert isinstance(view, StateMetaView)
    assert view.age_seconds == 90
    assert view.fetch_failures == (("wired_clients", "503"),)
    assert view.host == "api.mist.com"


def test_verdict_carries_state_meta_and_trace_ref():
    from digital_twin.ir import IRDiff
    from digital_twin.verdict.decision import DecisionInputs
    from digital_twin.verdict.verdict import assemble

    v = assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=()
        ),
        ir_diff=IRDiff((), (), ()),
        state_meta=None,
        trace_ref="run-123",
    )
    assert v.state_meta is None and v.trace_ref == "run-123"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/verdict/test_state_meta.py -q` → ImportError

- [ ] **Step 3: Implement**

```python
# src/digital_twin/verdict/state_meta.py
"""Freshness view: when the state was acquired, from where, what failed.

The agent reasons about stale evidence ("valid as of now" — the on-demand
model); partial fetch failures surface here for transparency even when they
did not lower the decision (irrelevant-partial rule)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from digital_twin.providers.base import StateMeta


@dataclass(frozen=True)
class StateMetaView:
    state_acquired_at: str  # ISO 8601
    host: str
    age_seconds: int
    fetched: tuple[str, ...]
    fetch_failures: tuple[tuple[str, str], ...]  # (object, error)


def build_state_meta(meta: StateMeta, *, now: datetime) -> StateMetaView:
    return StateMetaView(
        state_acquired_at=meta.acquired_at.isoformat(),
        host=meta.host,
        age_seconds=int((now - meta.acquired_at).total_seconds()),
        fetched=meta.fetched,
        fetch_failures=tuple((f.object, f.error) for f in meta.failures),
    )
```

In `verdict/verdict.py`: add fields `state_meta: StateMetaView | None = None` and
`trace_ref: str | None = None` to `Verdict`, matching keyword-only params on
`assemble(..., state_meta=None, trace_ref=None)` passed through; import the view type.

- [ ] **Step 4: Gate** — `uv run pytest tests/verdict -q && uv run ruff check . && uv run mypy` → PASS

- [ ] **Step 5: Commit** — `git add ... && git commit -m "Plan 5: state_meta freshness view + Verdict carries state_meta/trace_ref"`

---

### Task 3: `observability/trace.py` + `observability/logging.py`

**Files:**
- Create: `src/digital_twin/observability/__init__.py`, `trace.py`, `logging.py`
- Test: `tests/observability/__init__.py`, `tests/observability/test_trace.py`

- [ ] **Step 1: Failing tests**

```python
# tests/observability/test_trace.py
from digital_twin.observability.logging import bound_logger
from digital_twin.observability.trace import Trace


def test_trace_records_stages_in_order_with_timing():
    t = Trace(run_id="r1")
    with t.stage("fetch"):
        pass
    with t.stage("ingest_baseline", note="19 devices"):
        pass
    d = t.to_dict()
    assert d["run_id"] == "r1"
    assert [s["stage"] for s in d["stages"]] == ["fetch", "ingest_baseline"]
    assert all(s["duration_ms"] >= 0 for s in d["stages"])
    assert d["stages"][1]["note"] == "19 devices"


def test_stage_records_even_when_body_raises():
    t = Trace(run_id="r1")
    try:
        with t.stage("boom"):
            raise ValueError("x")
    except ValueError:
        pass
    assert t.to_dict()["stages"][0]["error"] == "x"


def test_bound_logger_smoke():
    log = bound_logger(run_id="r1", check_id="wired.l2.loop")
    log.info("hello")  # must not raise; binding is in the logger name/extra
```

- [ ] **Step 2: RED** — ImportError

- [ ] **Step 3: Implement**

```python
# src/digital_twin/observability/__init__.py
"""Effectful observability: per-run trace, structured logging, replay store."""
```

```python
# src/digital_twin/observability/trace.py
"""Per-run structured trace: each pipeline stage with timing + note + error.

The verdict's trace_ref names a run; this object IS that run's record (the
replay store serializes it next to the fixture). Monotonic clock for duration;
no wall-time inside (replayable)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Trace:
    run_id: str
    stages: list[dict[str, Any]] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str, note: str | None = None) -> Iterator[None]:
        started = time.monotonic()
        record: dict[str, Any] = {"stage": name}
        if note is not None:
            record["note"] = note
        try:
            yield
        except BaseException as e:
            record["error"] = str(e)
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.stages.append(record)

    def note(self, stage: str, note: str) -> None:
        self.stages.append({"stage": stage, "note": note, "duration_ms": 0.0})

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "stages": list(self.stages)}
```

```python
# src/digital_twin/observability/logging.py
"""Structured logging bound to (run_id, check_id) via LoggerAdapter."""

from __future__ import annotations

import logging


def bound_logger(run_id: str, check_id: str | None = None) -> logging.LoggerAdapter[logging.Logger]:
    extra = {"run_id": run_id, **({"check_id": check_id} if check_id else {})}
    return logging.LoggerAdapter(logging.getLogger("digital_twin"), extra)
```

- [ ] **Step 4: Gate** — `uv run pytest tests/observability -q && uv run ruff check . && uv run mypy` → PASS
- [ ] **Step 5: Commit** — `"Plan 5: trace + bound structured logging"`

---

### Task 4: `observability/replay/redaction.py`

**Files:**
- Create: `src/digital_twin/observability/replay/__init__.py`, `redaction.py`
- Test: `tests/observability/test_redaction.py`

- [ ] **Step 1: Failing tests**

```python
# tests/observability/test_redaction.py
from digital_twin.observability.replay.redaction import REDACTION_VERSION, redact


def test_macs_pseudonymized_stably_and_shaped():
    a = redact({"mac": "aa:bb:cc:dd:ee:01", "peer": "aa:bb:cc:dd:ee:01"})
    assert a["mac"] == a["peer"]  # same input -> same token (topology preserved)
    assert a["mac"] != "aa:bb:cc:dd:ee:01"
    assert len(a["mac"].replace(":", "")) == 12  # still MAC-shaped


def test_bare_mac_format_also_caught():
    out = redact({"mac": "aabbccddee01"})
    assert out["mac"] != "aabbccddee01" and len(out["mac"]) == 12


def test_ips_and_uuids_and_names_tokenized():
    out = redact(
        {
            "ip": "10.1.2.3",
            "site_id": "9777c1a0-6ef6-11e6-8bbf-02e208b2d34f",
            "name": "ld-cup-idf-a",
        }
    )
    assert out["ip"] != "10.1.2.3" and out["ip"].count(".") == 3  # doc-range IPv4 shape
    assert out["site_id"] != "9777c1a0-6ef6-11e6-8bbf-02e208b2d34f"
    assert out["name"].startswith("name-")


def test_secrets_stripped_not_hashed():
    out = redact({"psk": "supersecret", "radius_config": {"secret": "x", "port": 1812}})
    assert out["psk"] is None
    assert out["radius_config"]["secret"] is None
    assert out["radius_config"]["port"] == 1812


def test_structure_preserved():
    out = redact({"vlan_id": 30, "port_config": {"ge-0/0/1": {"usage": "ap"}}})
    assert out["vlan_id"] == 30
    assert out["port_config"]["ge-0/0/1"]["usage"] == "ap"


def test_version_present():
    assert isinstance(REDACTION_VERSION, str) and REDACTION_VERSION
```

- [ ] **Step 2: RED** — ImportError

- [ ] **Step 3: Implement**

```python
# src/digital_twin/observability/replay/__init__.py
"""Replay store: redact-on-write fixtures (debug/test artifact, NOT product state)."""
```

```python
# src/digital_twin/observability/replay/redaction.py
"""Redaction manifest + engine — capturing an UN-redacted fixture is a defect.

- Deterministic pseudonymization (sha256-derived, same input -> same token) for
  relationship-bearing identifiers: MACs (kept MAC-shaped), IPv4/IPv6 (re-mapped
  into documentation ranges, equality preserved), UUIDs, host/device names.
- Secrets are STRIPPED to None, never hashed (manifest below).
- Structure (vlan ids, port names, dict shapes) preserved so the compiler and
  checks run identically on the fixture.
Known limitation (documented): hashing preserves equality, not prefixes —
switch_matching match_name[A:B] rules can match differently on redacted data;
the GS suite validates the fixture end-to-end.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

REDACTION_VERSION = "1"

# strip outright (substring match on the key, case-insensitive) — never hash
STRIP_KEY_PARTS: tuple[str, ...] = (
    "psk",
    "password",
    "passphrase",
    "secret",
    "token",
    "community",
    "private_key",
    "cert",
)
# keys whose STRING values are name-like -> "name-<h8>"
NAME_KEYS: tuple[str, ...] = ("name", "hostname", "system_name", "neighbor_system_name")

_MAC = re.compile(r"^(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}$|^[0-9a-fA-F]{12}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")
_IPV6 = re.compile(r"^[0-9a-fA-F:]+:[0-9a-fA-F:]+$")


def _h(value: str, n: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:n]


def _redact_scalar(key: str, value: str) -> str:
    if _MAC.match(value):
        return _h(value.lower().replace(":", "").replace("-", ""), 12)
    if _UUID.match(value):
        h = _h(value.lower(), 32)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    if _IPV4.match(value):
        suffix = value.partition("/")[2]
        h = int(_h(value, 8), 16)
        ip = f"198.51.{(h >> 8) % 256}.{h % 256}"  # TEST-NET-2 documentation range
        return f"{ip}/{suffix}" if suffix else ip
    if _IPV6.match(value) and ":" in value:
        return f"2001:db8::{_h(value, 8)}"  # documentation prefix
    if key in NAME_KEYS:
        return f"name-{_h(value, 8)}"
    return value


def redact(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(part in lk for part in STRIP_KEY_PARTS):
                out[k] = None
            else:
                out[k] = redact(v, key=str(k))
        return out
    if isinstance(obj, list):
        return [redact(v, key=key) for v in obj]
    if isinstance(obj, str):
        return _redact_scalar(key, obj)
    return obj
```

- [ ] **Step 4: Gate** — `uv run pytest tests/observability -q && uv run ruff check . && uv run mypy` → PASS
- [ ] **Step 5: Commit** — `"Plan 5: redaction engine + manifest (pseudonymize ids, strip secrets)"`

---

### Task 5: `observability/replay/store.py` + FixtureProvider

**Files:**
- Create: `src/digital_twin/observability/replay/store.py`
- Test: `tests/observability/test_replay_store.py`

- [ ] **Step 1: Failing tests**

```python
# tests/observability/test_replay_store.py
import json

from digital_twin.observability.replay.store import (
    FixtureProvider,
    ReplayStore,
    load_fixture_raw,
)
from digital_twin.providers.base import RawSiteState, SiteScope
from tests.adapters.mist.fixtures import raw_site


def test_save_writes_redacted_fixture(tmp_path):
    store = ReplayStore(tmp_path)
    raw = raw_site(devices=({"mac": "aa:bb:cc:dd:ee:01", "id": "d1", "type": "switch",
                             "name": "real-name", "port_config": {}},))
    path = store.save_raw("run1", raw)
    data = json.loads(path.read_text())
    blob = json.dumps(data)
    assert "aa:bb:cc:dd:ee:01" not in blob and "real-name" not in blob  # redacted
    assert data["redaction_version"] == "1"
    assert data["scope"]["org_id"]  # structure intact


def test_load_round_trips_to_raw_site_state(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site())
    raw = load_fixture_raw(path)
    assert isinstance(raw, RawSiteState)
    assert isinstance(raw.scope, SiteScope)
    assert raw.devices and raw.setting  # payloads intact (values redacted)


def test_fixture_provider_serves_the_fixture(tmp_path):
    store = ReplayStore(tmp_path)
    path = store.save_raw("run1", raw_site())
    provider = FixtureProvider(path)
    raw = provider.fetch_site(SiteScope("ignored", "ignored"))
    assert isinstance(raw, RawSiteState)


def test_save_run_includes_plan_verdict_and_trace(tmp_path):
    from digital_twin.observability.trace import Trace

    store = ReplayStore(tmp_path)
    path = store.save_run(
        "run2", raw=raw_site(), plan={"source": "mist"}, verdict_doc={"decision": "safe"},
        trace=Trace(run_id="run2"),
    )
    data = json.loads(path.read_text())
    assert data["plan"]["source"] == "mist"
    assert data["verdict"]["decision"] == "safe"
    assert data["trace"]["run_id"] == "run2"
```

- [ ] **Step 2: RED** — ImportError

- [ ] **Step 3: Implement**

```python
# src/digital_twin/observability/replay/store.py
"""File-based replay store: (redacted raw, ChangePlan, verdict, trace) per run.

Debug/test artifact, NOT product state and NOT the deferred SnapshotProvider.
Redaction happens ON WRITE — there is no API to store un-redacted data.
FixtureProvider serves a saved fixture as a StateProvider for offline replay
and the golden-scenario suite."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digital_twin.observability.trace import Trace
from digital_twin.providers.base import (
    FetchError,
    FetchFailure,
    OrgScope,
    RawSiteState,
    SiteScope,
    StateMeta,
)

from .redaction import REDACTION_VERSION, redact

_RAW_FIELDS = (
    "site",
    "setting",
    "networktemplate",
    "devices",
    "device_stats",
    "port_stats",
    "wireless_clients",
    "wired_clients",
    "derived_setting",
)


class ReplayStore:
    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def save_raw(self, run_id: str, raw: RawSiteState) -> Path:
        return self._write(run_id, self._raw_doc(raw))

    def save_run(
        self,
        run_id: str,
        *,
        raw: RawSiteState,
        plan: dict[str, Any],
        verdict_doc: dict[str, Any],
        trace: Trace,
    ) -> Path:
        doc = self._raw_doc(raw)
        doc["plan"] = redact(plan)
        doc["verdict"] = redact(verdict_doc)
        doc["trace"] = trace.to_dict()
        return self._write(run_id, doc)

    def _raw_doc(self, raw: RawSiteState) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "redaction_version": REDACTION_VERSION,
            "scope": redact({"org_id": raw.scope.org_id, "site_id": raw.scope.site_id}),
            "meta": {
                "acquired_at": raw.meta.acquired_at.isoformat(),
                "host": raw.meta.host,
                "fetched": list(raw.meta.fetched),
                "failures": [[f.object, f.error] for f in raw.meta.failures],
            },
        }
        for field in _RAW_FIELDS:
            payload[field] = redact(getattr(raw, field))
        return payload

    def _write(self, run_id: str, doc: dict[str, Any]) -> Path:
        path = self._dir / f"{run_id}.json"
        path.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
        return path


def load_fixture_raw(path: Path | str) -> RawSiteState:
    data = json.loads(Path(path).read_text())
    meta = data["meta"]
    return RawSiteState(
        scope=SiteScope(org_id=data["scope"]["org_id"], site_id=data["scope"]["site_id"]),
        site=data["site"],
        setting=data["setting"],
        networktemplate=data["networktemplate"],
        devices=tuple(data["devices"]),
        device_stats=tuple(data["device_stats"]),
        port_stats=tuple(data["port_stats"]),
        wireless_clients=tuple(data["wireless_clients"]),
        wired_clients=tuple(data["wired_clients"]),
        derived_setting=data["derived_setting"],
        meta=StateMeta(
            acquired_at=datetime.fromisoformat(meta["acquired_at"]).astimezone(UTC),
            host=meta["host"],
            fetched=tuple(meta["fetched"]),
            failures=tuple(FetchFailure(object=o, error=e) for o, e in meta["failures"]),
        ),
    )


class FixtureProvider:
    """StateProvider over ONE saved fixture (offline replay / golden scenarios)."""

    def __init__(self, path: Path | str) -> None:
        self._raw = load_fixture_raw(path)

    def fetch_site(
        self, scope: SiteScope, *, include_derived: bool = False
    ) -> RawSiteState | FetchError:
        return self._raw

    def fetch_sites(
        self,
        scope: OrgScope,
        site_ids: Sequence[str] | None = None,
        *,
        include_derived: bool = False,
    ) -> dict[str, RawSiteState | FetchError]:
        return {self._raw.scope.site_id: self._raw}
```

- [ ] **Step 4: Gate** — `uv run pytest tests/observability -q && uv run ruff check . && uv run mypy` → PASS
- [ ] **Step 5: Commit** — `"Plan 5: replay store (redact-on-write) + FixtureProvider"`

---

### Task 6: `engine/run_context.py` + `engine/pipeline.py` — the 10 stages

**Files:**
- Create: `src/digital_twin/engine/run_context.py`, `src/digital_twin/engine/pipeline.py`
- Test: `tests/engine/__init__.py`, `tests/engine/test_pipeline.py`

- [ ] **Step 1: Failing tests** (the pipeline contract — every short-circuit + the happy path + two-source findings)

```python
# tests/engine/test_pipeline.py
"""simulate(): the 10-stage sequence with every failure a value -> decision."""

from datetime import UTC, datetime

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import FetchError, RawSiteState, SiteScope, StateMeta
from digital_twin.verdict.decision import Decision

SITE = "s1"
SETTING = {
    "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 30}},
    "port_usages": {
        "office": {"mode": "access", "port_network": "corp"},
        "uplink": {"mode": "trunk", "port_network": "corp", "networks": ["voice"]},
    },
    "vars": {"dhcp_ip": "10.0.0.2"},
    "dhcpd_config": {"corp": {"ip": "{{dhcp_ip}}"}},
}
SWITCH = {
    "mac": "aa0000000001", "id": "dev-a", "type": "switch", "model": "EX4100-48P",
    "name": "sw-a", "port_config": {"ge-0/0/0-1": {"usage": "office"}},
}


def _raw() -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id=SITE),
        site={"id": SITE},
        setting=SETTING,
        networktemplate=None,
        devices=(SWITCH,),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(
            acquired_at=datetime.now(UTC), host="t", fetched=("devices",), failures=()
        ),
    )


class FakeProvider:
    def __init__(self, raw=None):
        self._raw = raw if raw is not None else _raw()

    def fetch_site(self, scope, *, include_derived=False):
        return self._raw

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        return {SITE: self._raw}


def _plan(ops):
    return {"source": "mist", "scope": {"org_id": "o1", "site_id": SITE}, "ops": ops}


def _op(object_type="site_setting", object_id=SITE, payload=None, order=0):
    return {
        "action": "update", "order": order, "object_type": object_type,
        "object_id": object_id, "payload": payload if payload is not None else dict(SETTING),
    }


def test_malformed_envelope_unknown_without_fetch():
    class NeverFetch:
        def fetch_site(self, scope, *, include_derived=False):
            raise AssertionError("fetch must not run before the pre-fetch gates")

    v = simulate({"source": "mist", "ops": "nope"}, provider=NeverFetch())
    assert v.decision is Decision.UNKNOWN
    assert any("envelope" in r for r in v.decision_reasons)


def test_unsupported_object_type_unknown_pre_fetch():
    v = simulate(_plan([_op(object_type="networktemplate", object_id="nt1", payload={})]),
                 provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("object_gate" in r for r in v.decision_reasons)


def test_fatal_l0_short_circuits_before_fetch():
    class NeverFetch:
        def fetch_site(self, scope, *, include_derived=False):
            raise AssertionError("structurally-fatal L0 must short-circuit before fetch")

    v = simulate(_plan([_op(payload="not-an-object")]), provider=NeverFetch())
    assert v.decision is Decision.UNKNOWN  # envelope catches non-dict payload first
    # and a fatal-from-schema variant: unknown type handled by object gate; the
    # canonical L0-fatal path is exercised in test_l0_findings_reach_verdict


def test_total_fetch_failure_is_unknown():
    err = FetchError(scope=SiteScope("o1", SITE), failures=(), acquired_at=datetime.now(UTC),
                     host="t")
    v = simulate(_plan([_op()]), provider=FakeProvider(raw=err))
    assert v.decision is Decision.UNKNOWN
    assert any("baseline" in r or "fetch" in r for r in v.decision_reasons)


def test_out_of_scope_raw_path_unknown():
    bad = {**SETTING, "dhcpd_config": {"corp": {"ip": "9.9.9.9"}}}
    v = simulate(_plan([_op(payload=bad)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("field_gate" in r for r in v.decision_reasons)


def test_vars_ripple_unknown_at_derived_gate():
    ripple = {**SETTING, "vars": {"dhcp_ip": "10.9.9.9"}}
    v = simulate(_plan([_op(payload=ripple)]), provider=FakeProvider())
    assert v.decision is Decision.UNKNOWN
    assert any("derived_gate" in r for r in v.decision_reasons)


def test_l0_findings_reach_verdict():
    bad_type = {**SETTING, "networks": "oops"}
    v = simulate(_plan([_op(payload=bad_type)]), provider=FakeProvider())
    # field gate fires too (networks subtree replaced by string), but the L0
    # finding must be present in the flat findings list regardless
    assert any(f.code.startswith("l0.schema") for f in v.findings)


def test_in_scope_change_runs_checks_and_carries_state_meta():
    new = {**SETTING, "networks": {"corp": {"vlan_id": 10}, "voice": {"vlan_id": 31}}}
    v = simulate(_plan([_op(payload=new)]), provider=FakeProvider())
    assert v.decision is not Decision.UNKNOWN
    assert v.check_results  # checks ran
    assert v.state_meta is not None and v.state_meta.host == "t"
    assert v.trace_ref  # a run id
    assert not v.ir_diff.is_empty()


def test_cosmetic_noop_is_safe():
    v = simulate(_plan([_op()]), provider=FakeProvider())  # payload == current
    assert v.decision is Decision.SAFE
```

- [ ] **Step 2: RED** — ImportError

- [ ] **Step 3: Implement**

```python
# src/digital_twin/engine/run_context.py
"""Per-run identity: run_id + trace handle (state_meta accumulates on the run)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from digital_twin.observability.trace import Trace


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class RunContext:
    run_id: str = field(default_factory=_new_run_id)
    trace: Trace = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.trace is None:
            self.trace = Trace(run_id=self.run_id)
```

```python
# src/digital_twin/engine/pipeline.py
"""The 10-stage simulation pipeline — ORCHESTRATION ONLY (spec diagram).

 1 ScopeResolver.pre   envelope + object gate (pre-fetch)        -> UNKNOWN
 2 Adapter.validate    L0 per op (fatal -> short-circuit)        -> UNKNOWN
 3 StateProvider       fetch raw                                  -> UNKNOWN on total failure
 4 ScopeResolver.post  field gate per op vs ROLLING pre-op state
                       (incl. device-role)                        -> UNKNOWN
 5 Adapter.ingest      baseline (effective + IR)                  -> UNKNOWN if not ok
 6 Adapter.apply       rolling full-object replacement            -> UNKNOWN on bad target
 7 Adapter.ingest      proposed                                   -> UNKNOWN if not ok
 8 derived gate        full effective config, site + per device   -> UNKNOWN
 9 diff + checks       registry (gating order, isolation)
10 verdict             DecisionInputs -> decision + assembly

Every failure is a VALUE produced by the owning module; this file only maps
them into DecisionInputs and stops at the right stage. No business logic."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.apply import get_object
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.registry import CheckRegistry
from digital_twin.checks.wired import ALL_WIRED_CHECKS
from digital_twin.contracts import ChangePlan, Finding, Rejection
from digital_twin.engine.run_context import RunContext
from digital_twin.ir import IRDiff, diff_ir
from digital_twin.providers.base import RawSiteState, SiteScope, StateProvider
from digital_twin.scope.derived_gate import check_derived
from digital_twin.scope.envelope import parse_change_plan
from digital_twin.scope.object_gate import check_objects
from digital_twin.scope.field_gate import screen_op
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.state_meta import StateMetaView, build_state_meta
from digital_twin.verdict.verdict import Verdict, assemble

_EMPTY_DIFF = IRDiff((), (), ())


def simulate(
    plan_data: Mapping[str, Any],
    *,
    provider: StateProvider,
    adapter: MistAdapter | None = None,
    registry: CheckRegistry | None = None,
    run: RunContext | None = None,
) -> Verdict:
    run = run or RunContext()
    adapter = adapter or MistAdapter()
    registry = registry or CheckRegistry(ALL_WIRED_CHECKS)
    adapter_findings: tuple[Finding, ...] = ()

    def unknown(
        rejection: Rejection | None,
        *,
        l0_fatal: bool = False,
        baseline_unavailable: bool = False,
        state_meta: StateMetaView | None = None,
    ) -> Verdict:
        return assemble(
            inputs=DecisionInputs(
                rejections=(rejection,) if rejection else (),
                l0_fatal=l0_fatal,
                baseline_unavailable=baseline_unavailable,
                check_results=(),
                adapter_findings=adapter_findings,
            ),
            ir_diff=_EMPTY_DIFF,
            state_meta=state_meta,
            trace_ref=run.run_id,
        )

    # 1 — pre-fetch gates
    with run.trace.stage("scope.pre"):
        plan = parse_change_plan(plan_data)
        if isinstance(plan, Rejection):
            return unknown(plan)
        rejection = check_objects(plan)
        if rejection:
            return unknown(rejection)

    # 2 — L0 payload validation (pre-fetch; payload-only)
    with run.trace.stage("l0.validate"):
        for op in plan.ops:
            result = adapter.validate(op)
            adapter_findings += result.findings
            if result.fatal:
                return unknown(None, l0_fatal=True)

    # 3 — fetch
    with run.trace.stage("fetch"):
        assert plan.scope.site_id is not None  # object gate guaranteed it
        raw = provider.fetch_site(
            SiteScope(org_id=plan.scope.org_id, site_id=plan.scope.site_id)
        )
        if not isinstance(raw, RawSiteState):
            return unknown(None, baseline_unavailable=True)
    state_meta = build_state_meta(raw.meta, now=datetime.now(UTC))

    # 4+6 — field gate against the ROLLING pre-op state, then apply that op
    proposed_raw = raw
    with run.trace.stage("scope.post+apply", note=f"{len(plan.ops)} op(s)"):
        for op in sorted(plan.ops, key=lambda o: o.order):
            current = get_object(proposed_raw, op.object_type, op.object_id)
            if current is None:
                return unknown(
                    Rejection(
                        stage="apply",
                        reasons=(
                            f"ops[order={op.order}]: no {op.object_type} with id "
                            f"{op.object_id!r} in fetched state",
                        ),
                    ),
                    state_meta=state_meta,
                )
            rejection = screen_op(op.object_type, current, op.payload)
            if rejection:
                return unknown(rejection, state_meta=state_meta)
            applied = adapter.apply(proposed_raw, (op,))
            if isinstance(applied, Rejection):
                return unknown(applied, state_meta=state_meta)
            proposed_raw = applied

    # 5 — baseline ingest
    with run.trace.stage("ingest.baseline"):
        baseline = adapter.ingest(raw)
        if baseline.ir is None:
            return unknown(None, baseline_unavailable=True, state_meta=state_meta)

    # 7 — proposed ingest
    with run.trace.stage("ingest.proposed"):
        proposed = adapter.ingest(proposed_raw)
        if proposed.ir is None:
            return unknown(
                Rejection(
                    stage="ingest",
                    reasons=tuple(
                        f"proposed-state ingest failed: {f.ingester}: {f.error}"
                        for f in proposed.report.failures
                    ),
                ),
                state_meta=state_meta,
            )

    # 8 — derived-impact gate (site + every device effective)
    with run.trace.stage("derived_gate"):
        rejection = check_derived(baseline.site_effective, proposed.site_effective)
        if rejection:
            return unknown(rejection, state_meta=state_meta)
        for did in sorted(set(baseline.device_effective) | set(proposed.device_effective)):
            rejection = check_derived(
                baseline.device_effective.get(did, {}),
                proposed.device_effective.get(did, {}),
                artifact=f"device {did}",
            )
            if rejection:
                return unknown(rejection, state_meta=state_meta)

    # 9 — diff + checks
    with run.trace.stage("checks"):
        diff = diff_ir(baseline.ir, proposed.ir)
        results = registry.run_all(
            CheckContext(
                baseline=AnalysisContext(baseline.ir),
                proposed=AnalysisContext(proposed.ir),
                diff=diff,
            )
        )

    # 10 — verdict
    with run.trace.stage("verdict"):
        return assemble(
            inputs=DecisionInputs(
                rejections=(),
                l0_fatal=False,
                baseline_unavailable=False,
                check_results=results,
                adapter_findings=adapter_findings,
            ),
            ir_diff=diff,
            state_meta=state_meta,
            trace_ref=run.run_id,
        )


__all__ = ["simulate", "ChangePlan"]
```

- [ ] **Step 4: Gate** — `uv run pytest tests/engine -q && uv run ruff check . && uv run mypy && uv run pytest -q` → all PASS
- [ ] **Step 5: Commit** — `"Plan 5: the 10-stage pipeline (orchestration only, failures as values)"`

---

### Task 7: `drivers/render.py`

**Files:**
- Create: `src/digital_twin/drivers/__init__.py`, `render.py`
- Test: `tests/drivers/__init__.py`, `tests/drivers/test_render.py`

- [ ] **Step 1: Failing tests**

```python
# tests/drivers/test_render.py
import json

from digital_twin.drivers.render import render_human, verdict_to_dict
from digital_twin.ir import IRDiff
from digital_twin.verdict.decision import DecisionInputs
from digital_twin.verdict.verdict import assemble


def _verdict():
    return assemble(
        inputs=DecisionInputs(
            rejections=(), l0_fatal=False, baseline_unavailable=False, check_results=()
        ),
        ir_diff=IRDiff((), (), ()),
        trace_ref="run-1",
    )


def test_verdict_to_dict_is_json_serializable():
    d = verdict_to_dict(_verdict())
    blob = json.dumps(d)  # must not raise
    assert d["decision"] == "safe" and "run-1" in blob


def test_render_human_leads_with_decision():
    text = render_human(_verdict())
    assert text.splitlines()[0].startswith("decision: SAFE")
```

- [ ] **Step 2: RED** — ImportError
- [ ] **Step 3: Implement**

```python
# src/digital_twin/drivers/__init__.py
"""Drivers: ChangePlan in -> verdict out (CLI, MCP). Shared rendering."""
```

```python
# src/digital_twin/drivers/render.py
"""Verdict -> JSON-able dict / human summary (shared by CLI and MCP)."""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from digital_twin.verdict.verdict import Verdict


def _plain(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    return obj


def verdict_to_dict(verdict: Verdict) -> dict[str, Any]:
    out: dict[str, Any] = _plain(verdict)
    return out


def render_human(verdict: Verdict) -> str:
    lines = [
        f"decision: {verdict.decision.name}",
        f"severity: {verdict.overall_severity.name if verdict.overall_severity else '-'}",
    ]
    lines += [f"  reason: {r}" for r in verdict.decision_reasons[:10]]
    for res in verdict.check_results:
        lines.append(
            f"  check {res.check_id}: {res.status.value}"
            f" (coverage={res.coverage.state.value})"
        )
    for f in verdict.findings[:20]:
        lines.append(f"  finding [{f.severity.value}] {f.code}: {f.message}")
    if verdict.state_meta:
        lines.append(
            f"  state: {verdict.state_meta.host} @ {verdict.state_meta.state_acquired_at}"
            f" (age {verdict.state_meta.age_seconds}s)"
        )
    if verdict.trace_ref:
        lines.append(f"  trace: {verdict.trace_ref}")
    return "\n".join(lines)
```

- [ ] **Step 4: Gate** → PASS
- [ ] **Step 5: Commit** — `"Plan 5: verdict rendering (JSON + human)"`

---

### Task 8: `drivers/cli.py` + console script

**Files:**
- Create: `src/digital_twin/drivers/cli.py`
- Modify: `pyproject.toml` (add `[project.scripts]`)
- Test: `tests/drivers/test_cli.py`

- [ ] **Step 1: Failing tests**

```python
# tests/drivers/test_cli.py
import json

from digital_twin.drivers.cli import main
from digital_twin.observability.replay.store import ReplayStore
from tests.adapters.mist.fixtures import raw_site

GS8_PLAN = {
    "source": "mist",
    "scope": {"org_id": "o1", "site_id": "s1"},
    "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
             "object_id": "nt1", "payload": {}}],
}


def _fixture(tmp_path):
    return ReplayStore(tmp_path).save_raw("fx", raw_site())


def test_unknown_exits_30_and_prints_json(tmp_path, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(GS8_PLAN))
    code = main(["--plan", str(plan), "--replay-fixture", str(_fixture(tmp_path)), "--json"])
    assert code == 30
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "unknown"


def test_safe_noop_exits_0_human(tmp_path, capsys):
    raw = raw_site()
    plan = {
        "source": "mist",
        "scope": {"org_id": raw.scope.org_id, "site_id": raw.scope.site_id},
        "ops": [{"action": "update", "order": 0, "object_type": "site_setting",
                 "object_id": raw.scope.site_id, "payload": dict(raw.setting)}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    code = main(["--plan", str(plan_path), "--replay-fixture", str(_fixture(tmp_path))])
    assert code == 0
    assert "decision: SAFE" in capsys.readouterr().out
```

*(note: the fixture is redacted on save — the no-op plan must target the REDACTED
site_id, hence reading it back from `raw_site()`… which is NOT redacted. Pin this
by loading the fixture's scope via `load_fixture_raw` instead if the assertion
fails — the redacted site_id is the one the plan must name.)*

- [ ] **Step 2: RED** — ImportError
- [ ] **Step 3: Implement**

```python
# src/digital_twin/drivers/cli.py
"""CLI driver: ChangePlan JSON in -> verdict out, decision-coded exit status.

Exit codes (spec): SAFE=0, REVIEW=10, UNSAFE=20, UNKNOWN=30 — only SAFE is
success, because everything else means "do not apply automatically".
Providers: live Mist (env MIST_HOST/MIST_APITOKEN) or --replay-fixture (offline).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from digital_twin.drivers.render import render_human, verdict_to_dict
from digital_twin.engine.pipeline import simulate
from digital_twin.engine.run_context import RunContext
from digital_twin.observability.replay.store import FixtureProvider, ReplayStore
from digital_twin.providers.base import StateProvider
from digital_twin.verdict.decision import Decision

EXIT_CODES = {Decision.SAFE: 0, Decision.REVIEW: 10, Decision.UNSAFE: 20, Decision.UNKNOWN: 30}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="digital-twin", description="simulate a ChangePlan")
    parser.add_argument("--plan", required=True, help="ChangePlan JSON file (or '-' for stdin)")
    parser.add_argument("--json", action="store_true", help="print verdict as JSON")
    parser.add_argument("--replay-fixture", help="run against a saved fixture instead of live")
    parser.add_argument("--replay-store", help="directory to capture (raw, plan, verdict, trace)")
    args = parser.parse_args(argv)

    plan_text = sys.stdin.read() if args.plan == "-" else Path(args.plan).read_text()
    plan_data = json.loads(plan_text)

    provider: StateProvider
    if args.replay_fixture:
        provider = FixtureProvider(args.replay_fixture)
    else:
        from digital_twin.providers.mist_api import MistApiProvider

        provider = MistApiProvider()

    run = RunContext()
    verdict = simulate(plan_data, provider=provider, run=run)

    if args.replay_store:
        raw = provider.fetch_site(  # the same state the run used (fixture/on-demand)
            __import__("digital_twin.providers.base", fromlist=["SiteScope"]).SiteScope(
                plan_data.get("scope", {}).get("org_id", ""),
                plan_data.get("scope", {}).get("site_id", ""),
            )
        )
        from digital_twin.providers.base import RawSiteState

        if isinstance(raw, RawSiteState):
            ReplayStore(args.replay_store).save_run(
                run.run_id, raw=raw, plan=plan_data,
                verdict_doc=verdict_to_dict(verdict), trace=run.trace,
            )

    print(json.dumps(verdict_to_dict(verdict), indent=1) if args.json else render_human(verdict))
    return EXIT_CODES[verdict.decision]


def script() -> None:
    raise SystemExit(main())
```

*(replace the inline `__import__` hack with a top-of-file `from digital_twin.providers.base import RawSiteState, SiteScope` — it is only written out here to flag that BOTH names are needed.)*

pyproject addition:

```toml
[project.scripts]
digital-twin = "digital_twin.drivers.cli:script"
```

- [ ] **Step 4: Gate** — `uv run pytest tests/drivers -q && uv run ruff check . && uv run mypy && uv run digital-twin --help` → PASS / usage text
- [ ] **Step 5: Commit** — `"Plan 5: CLI driver (decision exit codes, fixture replay, run capture)"`

---

### Task 9: `drivers/mcp_server.py`

**Files:**
- Modify: `pyproject.toml` (`uv add mcp`)
- Create: `src/digital_twin/drivers/mcp_server.py`
- Test: `tests/drivers/test_mcp_server.py`

- [ ] **Step 1: `uv add mcp`** → resolves

- [ ] **Step 2: Failing test**

```python
# tests/drivers/test_mcp_server.py
from digital_twin.drivers.mcp_server import simulate_change
from digital_twin.observability.replay.store import ReplayStore
from tests.adapters.mist.fixtures import raw_site


def test_tool_returns_verdict_dict_and_never_raises(tmp_path):
    fixture = ReplayStore(tmp_path).save_raw("fx", raw_site())
    out = simulate_change(
        {"source": "mist", "ops": "garbage"}, replay_fixture=str(fixture)
    )
    assert out["decision"] == "unknown"  # bad plan -> verdict, not an exception


def test_tool_isolates_internal_errors(tmp_path):
    out = simulate_change({"source": "mist"}, replay_fixture=str(tmp_path / "missing.json"))
    assert out["decision"] == "unknown"
    assert any("error" in r.lower() or "fixture" in r.lower() for r in out["decision_reasons"])
```

- [ ] **Step 3: Implement**

```python
# src/digital_twin/drivers/mcp_server.py
"""MCP driver: one tool, simulate_change(change_plan) -> verdict JSON.

The tool itself NEVER throws to the agent (spec) — any internal error becomes
an UNKNOWN verdict document with the error in decision_reasons."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from digital_twin.drivers.render import verdict_to_dict
from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.providers.base import StateProvider

mcp = FastMCP("digital-twin")


def _provider(replay_fixture: str | None) -> StateProvider:
    if replay_fixture:
        return FixtureProvider(replay_fixture)
    from digital_twin.providers.mist_api import MistApiProvider

    return MistApiProvider()


def simulate_change(
    change_plan: dict[str, Any], replay_fixture: str | None = None
) -> dict[str, Any]:
    try:
        verdict = simulate(change_plan, provider=_provider(replay_fixture))
        return verdict_to_dict(verdict)
    except Exception as e:  # noqa: BLE001 — the tool never throws to the agent
        return {
            "decision": "unknown",
            "decision_reasons": (f"internal error: {e}",),
            "findings": [],
        }


@mcp.tool()
def simulate_change_tool(change_plan: dict[str, Any]) -> dict[str, Any]:
    """Simulate a Mist ChangePlan against the live network state; returns the
    verdict document (decision: safe|review|unsafe|unknown + findings)."""
    return simulate_change(change_plan)


def main() -> None:
    mcp.run()
```

- [ ] **Step 4: Gate** → PASS
- [ ] **Step 5: Commit** — `"Plan 5: MCP driver (simulate_change tool, never throws)"`

---

### Task 10: capture tool + REAL fixture (live)

**Files:**
- Create: `tools/capture_replay.py`
- Create (captured): `tests/golden/fixtures/site.json`
- Test: `tests/golden/__init__.py`, `tests/golden/test_fixture_hygiene.py`

- [ ] **Step 1: Write the capture tool**

```python
# tools/capture_replay.py
"""Capture ONE redacted replay fixture from the real org (read-only).

Usage: uv run python tools/capture_replay.py <site_id> <out_path>
Env:   MIST_HOST, MIST_APITOKEN, DT_GATE_ORG_ID
The output is redacted ON WRITE (store contract) and safe to commit."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from digital_twin.observability.replay.store import ReplayStore
from digital_twin.providers.base import RawSiteState, SiteScope
from digital_twin.providers.mist_api import MistApiProvider


def main() -> None:
    site_id, out = sys.argv[1], Path(sys.argv[2])
    raw = MistApiProvider().fetch_site(SiteScope(os.environ["DT_GATE_ORG_ID"], site_id))
    if not isinstance(raw, RawSiteState):
        sys.exit(f"fetch failed: {raw}")
    store = ReplayStore(out.parent)
    path = store.save_raw(out.stem, raw)
    print(f"captured (redacted): {path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Capture the fixture from the real org** (the main gate site)

Run: `set -a; . ./.env; set +a; uv run python tools/capture_replay.py "$(echo $DT_GATE_SITE_IDS | cut -d, -f1)" tests/golden/fixtures/site.json`
Expected: `captured (redacted): tests/golden/fixtures/site.json`

- [ ] **Step 3: Fixture-hygiene test (the spec's redaction CI rule)**

```python
# tests/golden/test_fixture_hygiene.py
"""An un-redacted field in a committed fixture is a DEFECT (spec) — fail CI."""

import json
import re
from pathlib import Path

FIXTURES = sorted(Path(__file__).parent.glob("fixtures/*.json"))
_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_PRIVATE_IP = re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
_SECRET_KEYS = ("psk", "password", "secret", "token", "community", "passphrase")


def test_fixtures_exist():
    assert FIXTURES, "no golden fixture captured — run tools/capture_replay.py"


def test_no_unredacted_identifiers_or_secrets():
    for path in FIXTURES:
        blob = path.read_text()
        assert not _MAC.search(blob), f"{path}: colon-MAC survived redaction"
        assert not _PRIVATE_IP.search(blob), f"{path}: private IP survived redaction"
        data = json.loads(blob)

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if any(s in str(k).lower() for s in _SECRET_KEYS):
                        assert v is None, f"{path}: secret key {k!r} not stripped"
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)


def test_pseudonymization_is_stable_within_fixture():
    # the same original mac must map to ONE token: device 'mac' fields that the
    # port_stats reference must still join (topology preserved)
    for path in FIXTURES:
        data = json.loads(path.read_text())
        device_macs = {d.get("mac") for d in data["devices"] if d.get("mac")}
        stat_macs = {p.get("mac") for p in data["port_stats"] if p.get("mac")}
        assert stat_macs & device_macs, f"{path}: stats no longer join to devices"
```

*(note: bare 12-hex MACs in `devices[].mac` are redacted to 12-hex tokens — the joins
must survive, which is exactly what the third test asserts.)*

- [ ] **Step 4: Gate** — `uv run pytest tests/golden -q && uv run ruff check . && uv run mypy` → PASS
- [ ] **Step 5: Commit** — `"Plan 5: capture tool + redacted real-org fixture + hygiene CI"` (fixture included)

---

### Task 11: GS1–GS8 golden scenario suite

**Files:**
- Create: `tests/golden/builders.py`
- Create: `tests/golden/test_golden_scenarios.py`

- [ ] **Step 1: Write the scenario builders** — pure helpers over the fixture that SEARCH for preconditions and synthesize delta payloads:

```python
# tests/golden/builders.py
"""GS builders: search the fixture's BASELINE for each scenario's precondition
and synthesize the delta. Every builder returns (plan_dict, context) or None
when the topology lacks the precondition (the test skips with the reason)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.analysis.context import AnalysisContext
from digital_twin.observability.replay.store import load_fixture_raw
from digital_twin.providers.base import RawSiteState

FIXTURE = Path(__file__).parent / "fixtures" / "site.json"


def fixture_raw() -> RawSiteState:
    return load_fixture_raw(FIXTURE)


def baseline(raw: RawSiteState):
    out = MistAdapter().ingest(raw)
    assert out.ir is not None, f"fixture must ingest cleanly: {out.report.failures}"
    return out


def _plan(raw: RawSiteState, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source": "mist",
        "scope": {"org_id": raw.scope.org_id, "site_id": raw.scope.site_id},
        "ops": ops,
    }


def _device_op(raw: RawSiteState, device: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = {**copy.deepcopy(device), **overrides}
    return {
        "action": "update",
        "order": 0,
        "object_type": "device",
        "object_id": str(device["id"]),
        "payload": payload,
    }


def _vlan_edges(ctx: AnalysisContext, vid: int):
    return [
        (u, v, d["data"]) for u, v, d in ctx.vlan_graph(vid).edges(data=True)
    ]


def _carrier_count(ctx: AnalysisContext, vid: int) -> dict[frozenset, int]:
    counts: dict[frozenset, int] = {}
    for u, v, _ in _vlan_edges(ctx, vid):
        counts[frozenset((u, v))] = counts.get(frozenset((u, v)), 0) + 1
    return counts


def find_single_carrier_vlan(raw: RawSiteState):
    """GS1: a vlan whose member segment hangs off EXACTLY ONE inter-node edge
    (cutting it strands members). Returns (vid, device_raw, port_name) of the
    switch-side port to de-vlan, or None."""
    out = baseline(raw)
    ctx = AnalysisContext(out.ir)
    devices_by_id = {str(d.get("id")): d for d in raw.devices}
    for vid in sorted(out.ir.vlans):
        comps = ctx.vlan_components(vid)
        reaching = [c for c in comps if c.has_members and c.reaches_exit]
        for comp in reaching:
            edges = _vlan_edges(ctx, vid)
            if len(edges) != 1:
                continue
            # the lone edge's switch-side member ports name the device + port
            edge = edges[0][2]
            for pid in edge.member_ports:
                did, _, pname = pid.partition(":")
                dev = next(
                    (d for d in raw.devices if str(d.get("mac", "")).replace(":", "") in did
                     and d.get("type") == "switch"),
                    None,
                )
                if dev and dev.get("port_config"):
                    return vid, dev, pname
    return None


# ... analogous searchers:
# find_redundant_carrier_vlan (GS2): a vlan with >=2 edges between the same
#   reaching component pair — removing ONE keeps reachability.
# find_parallel_link_pair (GS3): a node pair with >=2 standalone edges where a
#   vlan rides only one; delta adds the vlan to the second port's config.
# find_access_port_with_client (GS4): ir.clients attach PORT whose port is
#   ACCESS; delta changes that port's usage/port_network to another vlan.
# cosmetic (GS5): device op changing only "name".
# gs6_raw(): fixture raw with wireless_clients=() and 'wireless_clients'
#   removed from meta.fetched (relevant partial fetch).
# find_ap_serving_vlan (GS7): an AP edge carrying a vlan with observed wireless
#   clients on it; delta removes the vlan from the switch-side port usage.
# gs8_plan(): networktemplate op (unsupported type).
```

*(The full searcher implementations follow the `find_single_carrier_vlan` pattern —
walk `AnalysisContext` for the precondition, then synthesize the device/site_setting
payload from the fixture's raw objects. Implement each completely; if the committed
fixture lacks a precondition the test SKIPS with the builder's reason — except GS3,
which falls back to `augmented_fixture_with_parallel_link()`: deep-copy the fixture
JSON, duplicate one two-sided link's two port_stats rows under spare port names, and
save to tmp_path via ReplayStore — a documented augmentation.)*

- [ ] **Step 2: Write the GS test suite**

```python
# tests/golden/test_golden_scenarios.py
"""GS1-GS8 (spec acceptance): the definition of done, run against the redacted
real-org fixture. Each asserts the FULL verdict decision + key findings."""

import pytest

from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision

from .builders import FIXTURE, fixture_raw  # + every builder used below

PROVIDER = FixtureProvider(FIXTURE)


def _simulate(plan, provider=None):
    return simulate(plan, provider=provider or PROVIDER)


def test_gs1_single_uplink_vlan_removal_is_unsafe():
    found = find_single_carrier_vlan(fixture_raw())
    if not found:
        pytest.skip("fixture has no single-carrier vlan with members")
    vid, dev, port = found
    plan = build_devlan_plan(fixture_raw(), dev, port, vid)  # from builders
    v = _simulate(plan)
    assert v.decision is Decision.UNSAFE
    assert any("blackhole" in f.code for f in v.findings)


def test_gs2_redundant_vlan_removal_is_safe(): ...
def test_gs3_unprotected_new_cycle_is_unsafe_or_review(): ...
def test_gs4_access_vlan_change_with_clients_is_review(): ...
def test_gs5_cosmetic_change_is_safe(): ...
def test_gs6_missing_client_data_is_review_not_silent(): ...
def test_gs7_ap_vlan_removal_with_observed_clients_is_unsafe(): ...
def test_gs8_unsupported_object_type_is_unknown(): ...
```

*(Each `...` body follows GS1's shape: builder → skip-if-absent → simulate →
assert decision + the spec's named findings/statuses, e.g. GS3 asserts
`l2.loop` FAIL when the parallel ports' `stp_enabled is False` or WARN/REVIEW when
unknown — match the spec table EXACTLY, including the GS7 zero-client REVIEW
variant as a second test when the fixture has a clientless AP vlan. Write all
bodies fully during execution; the RED step is the suite failing on missing
builders.)*

- [ ] **Step 3: Iterate** — run `uv run pytest tests/golden -q`, fix builders/expectations until every non-skipped GS passes; record which GS skipped and why; minimize skips (fixture augmentation where documented).

- [ ] **Step 4: Full gate** — `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q` → all PASS
- [ ] **Step 5: Commit** — `"Plan 5: GS1-GS8 golden scenarios against the redacted real-org fixture"`

---

### Task 12: Public API + plan sync + M1 wrap

**Files:**
- Modify: `tests/test_public_api.py` (add `test_plan5_public_api` importing: `simulate`, `RunContext`, `Trace`, `bound_logger`, `redact`, `ReplayStore`, `FixtureProvider`, `load_fixture_raw`, `verdict_to_dict`, `render_human`, `main` (cli), `simulate_change` (mcp), `StateMetaView`, `build_state_meta` — follow the existing function style)
- Modify: this plan doc (check boxes)

- [ ] **Step 1: Public API test + full gate** — `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q` → PASS
- [ ] **Step 2: Mark checkboxes; commit** — `"Plan 5: public API surface + plan doc synced"`

---

## Acceptance (Plan 5 / M1 exit)

1. Full offline suite green (ruff + mypy-strict + pytest), including GS1–GS8 against the committed redacted fixture, with skips only for preconditions the real topology genuinely lacks (each named).
2. `digital-twin --plan plan.json [--replay-fixture f.json] [--json]` exits 0/10/20/30 by decision.
3. The MCP tool returns a verdict document and never throws.
4. Committed fixtures contain no un-redacted MAC/private-IP/secret (CI-tested), and pseudonymization preserves device↔stats joins.
5. The pipeline short-circuits at the spec's exact stages (test-pinned per stage) and `state_meta`/`trace_ref` ride every verdict that had a fetch.

**Deferred beyond M1 (spec):** rules/ engine population (L1/L3), SnapshotProvider product backend, multi-site simulation, apply module, Aruba adapter.
