# Richer impacted-client reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich each client the `wired.client.impact` check already flags with observational identity (hostname, fingerprint, manufacturer, auth/NAC detail), a derived subnet, and a narrow DHCP-config-change signal — in both JSON and human output — **without ever changing the verdict**.

**Architecture:** A dedicated, observational `ir.client_enrichment: Mapping[str, ClientEnrichment]` collection (MAC-keyed join over `wired_clients` ∪ `wireless_clients` base + `nac_clients` overlay), built by a **self-isolating best-effort** ingester (never raises → never flips `report.ok` → never UNKNOWN). The `client_impact` check reads it *only after* impact detection, purely to annotate. It is **not** in `diff_ir`, earns **no** capability, and never touches severity/coverage.

**Tech Stack:** Python 3.14, uv, pytest/ruff/mypy. Spec: `docs/superpowers/specs/2026-06-19-richer-impacted-client-reporting-design.md`.

**Gate (run after every task):** `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

---

## Reference: the non-load-bearing guarantees (do not violate)

1. **Self-isolating ingester** — `IngesterRegistry.run` records any ingester exception into `IngestReport.failures`; `report.ok = not failures`; `MistAdapter.ingest` sets `ir=None` when not ok; the pipeline maps a `None` ir to UNKNOWN. So the enrichment ingester **must** swallow all its own errors (per-row AND whole-body) and never append to `IngestReport.failures`.
2. **No capability** — `produces()` returns `frozenset()`; nothing `requires()` it.
3. **Not in `diff_ir`** — never add `client_enrichment` to `_ENTITY_KINDS` in `src/digital_twin/ir/diff.py`.
4. **Annotation-only** — the check reads it after impact detection; never in detection conditions, severity, confidence, coverage, or `applies_to`.

---

## Phase 1 — raw state + fetch

### Task 1: `RawSiteState.nac_clients` + provider fetch + fixture passthrough

**Files:**
- Modify: `src/digital_twin/providers/base.py:73` (add trailing field)
- Modify: `src/digital_twin/providers/mist_api.py:255` (wire `attempt`) and `:357` (new `_nac_clients`)
- Modify: `src/digital_twin/observability/replay/store.py:44` (`_RAW_FIELDS`) and `:104` (`load_fixture_doc`)
- Test: `tests/providers/test_nac_clients_field.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/test_nac_clients_field.py
from datetime import UTC, datetime

from digital_twin.observability.replay.store import load_fixture_doc
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _meta() -> StateMeta:
    return StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=())


def test_raw_site_state_defaults_nac_clients_empty():
    raw = RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={}, setting={},
        networktemplate=None, devices=(), device_stats=(), port_stats=(),
        wireless_clients=(), wired_clients=(), derived_setting=None, meta=_meta(),
    )
    assert raw.nac_clients == ()


def test_load_fixture_doc_carries_nac_clients_and_tolerates_absence():
    base = {
        "scope": {"org_id": "o1", "site_id": "s1"}, "site": {}, "setting": {},
        "networktemplate": None, "devices": [], "device_stats": [], "port_stats": [],
        "wireless_clients": [], "wired_clients": [], "derived_setting": None,
        "meta": {"acquired_at": datetime.now(UTC).isoformat(), "host": "t",
                 "fetched": [], "failures": []},
    }
    assert load_fixture_doc(base).nac_clients == ()  # pre-feature fixtures
    withnac = {**base, "nac_clients": [{"mac": "aa", "last_family": "Printer"}]}
    assert load_fixture_doc(withnac).nac_clients == ({"mac": "aa", "last_family": "Printer"},)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/providers/test_nac_clients_field.py -v`
Expected: FAIL (`TypeError: ... unexpected keyword` is not it — rather `AttributeError: 'RawSiteState' object has no attribute 'nac_clients'` / `load_fixture_doc` ignores the key).

- [ ] **Step 3: Add the field to `RawSiteState`**

In `src/digital_twin/providers/base.py`, after the `gatewaytemplate` field (line 73):

```python
    gatewaytemplate: JsonObj | None = None
    # observed NAC clients (GET /orgs/{org}/nac_clients/search, site-filtered) —
    # OBSERVATIONAL enrichment only (fingerprint + auth/NAC identity for the
    # client.impact report). Trailing + defaulted: absence is "not fetched" and
    # is NON-FATAL (best-effort enrichment, never earns/loses a capability).
    nac_clients: tuple[JsonObj, ...] = ()
```

- [ ] **Step 4: Carry it through the fixture store**

In `src/digital_twin/observability/replay/store.py`, add `"nac_clients"` to `_RAW_FIELDS` (after `"gatewaytemplate"`, line 43):

```python
    "gatewaytemplate",
    "nac_clients",
```

And in `load_fixture_doc` (after the `gatewaytemplate=` line, 108):

```python
        gatewaytemplate=data.get("gatewaytemplate"),  # .get: pre-gateway-site-templates fixtures
        nac_clients=tuple(data.get("nac_clients", ())),  # .get: pre-enrichment fixtures
```

- [ ] **Step 5: Add the provider fetch (non-fatal via `attempt`)**

In `src/digital_twin/providers/mist_api.py`, add the fetch into `_fetch_one`'s `RawSiteState(...)` (after the `wlans=` line, 256):

```python
            wlans=tuple(attempt("wlans", lambda: self._wlans(scope), [])),
            nac_clients=tuple(attempt("nac_clients", lambda: self._nac_clients(scope), [])),
```

And add the method near `_wired_clients` (after line 357):

```python
    def _nac_clients(self, s: SiteScope) -> list[_Json]:
        # OBSERVATIONAL enrichment for the client.impact report (fingerprint +
        # auth/NAC identity). Org-scoped search, site-filtered, last 1d. A failure
        # here is NON-FATAL — `attempt` records it in StateMeta.failures and the
        # enrichment ingester degrades to "no enrichment", never UNKNOWN.
        resp = mistapi.api.v1.orgs.nac_clients.searchOrgNacClients(
            self._session, s.org_id, site_id=s.site_id, duration="1d"
        )
        return [dict(d) for d in mistapi.get_all(self._session, resp)]
```

> **Feasibility note (resolve at live-verify, Task 8):** confirm the exact `mistapi` function name/params for NAC client search (mirrors `searchOrgWiredClients`), and whether the site `wired_clients` fetch surfaces `last_hostname`/`manufacture`. Neither blocks offline tests (the ingester reads whatever fields are present).

- [ ] **Step 6: Run tests + gate**

Run: `uv run pytest tests/providers/test_nac_clients_field.py -v && uv run pytest tests -q`
Expected: PASS, full suite green (defaulted trailing field breaks no existing constructor).

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/providers/base.py src/digital_twin/providers/mist_api.py src/digital_twin/observability/replay/store.py tests/providers/test_nac_clients_field.py
git commit -m "feat(enrich): RawSiteState.nac_clients + non-fatal provider fetch + fixture passthrough"
```

---

## Phase 2 — `ClientEnrichment` record + join + ingester + IR

### Task 2: `ClientEnrichment` record + the pure MAC-keyed join

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (add `ClientEnrichment` after `Client`, ~line 293)
- Modify: `src/digital_twin/ir/__init__.py` (export `ClientEnrichment`)
- Create: `src/digital_twin/adapters/mist/ingest/client_enrichment.py` (Mist-row join — pure)
- Test: `tests/adapters/mist/test_client_enrichment_join.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/test_client_enrichment_join.py
from digital_twin.adapters.mist.ingest.client_enrichment import build_client_enrichment


def test_wired_only_oui_manufacturer():
    out = build_client_enrichment(
        wired=({"mac": "AA:BB:CC:00:00:01", "last_hostname": "r2d2",
                "manufacture": "Raspberry Pi Trading Ltd"},),
        wireless=(), nac=(),
    )
    ce = out["aabbcc000001"]                      # client_id-normalized key
    assert ce.hostname == "r2d2"
    assert ce.mfg == "Raspberry Pi Trading Ltd"
    assert ce.family is None


def test_nac_overlay_wins_but_unknown_does_not_clobber_base_mfg():
    # the HP + NAC(family=Printer, mfg=Unknown) case from the spec
    out = build_client_enrichment(
        wired=({"mac": "aabbcc000002", "manufacture": "HP"},),
        wireless=(),
        nac=({"mac": "aabbcc000002", "last_family": "Printer", "last_mfg": "Unknown",
              "auth_type": "mab", "last_nacrule_name": "printer_mab", "last_status": "permitted"},),
    )
    ce = out["aabbcc000002"]
    assert ce.mfg == "HP"          # NAC "Unknown" cleaned to None -> base survives
    assert ce.family == "Printer"  # NAC adds the useful field
    assert ce.auth_type == "mab" and ce.nacrule == "printer_mab" and ce.status == "permitted"


def test_unknown_blank_whitespace_collapse_to_none():
    out = build_client_enrichment(
        wired=(), wireless=(),
        nac=({"mac": "aabbcc000003", "last_family": " Unknown ", "last_model": "",
              "last_os": "unknown", "last_hostname": "LiveDemo-CD51"},),
    )
    ce = out["aabbcc000003"]
    assert ce.family is None and ce.model is None and ce.os is None
    assert ce.hostname == "LiveDemo-CD51"


def test_cross_separator_mac_join_and_empty_record_omitted():
    out = build_client_enrichment(
        wired=({"mac": "AA-BB-CC-00-00-04", "manufacture": "Intel Corporate"},),
        wireless=(),
        nac=({"mac": "aabbcc000004", "auth_type": "eap-tls"},),  # same device, other separators
    )
    assert out["aabbcc000004"].mfg == "Intel Corporate"
    assert out["aabbcc000004"].auth_type == "eap-tls"
    # a row with only "Unknown"/blank useful fields produces NO entry
    out2 = build_client_enrichment(wired=(), wireless=(),
                                   nac=({"mac": "deadbeef", "last_os": "Unknown"},))
    assert "deadbeef" not in out2


def test_malformed_row_is_skipped_not_fatal():
    # a row missing mac, and a row that is not a dict-shaped client, are skipped
    out = build_client_enrichment(
        wired=({"no_mac": True}, {"mac": "aabbcc000005", "last_hostname": "ok"}),
        wireless=(), nac=(),
    )
    assert out["aabbcc000005"].hostname == "ok"
    assert len(out) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_client_enrichment_join.py -v`
Expected: FAIL (`ModuleNotFoundError: ... client_enrichment`).

- [ ] **Step 3: Add the `ClientEnrichment` record**

In `src/digital_twin/ir/entities.py`, after the `Client` class (after line 293):

```python
@dataclass(frozen=True)
class ClientEnrichment:
    """OBSERVATIONAL per-client identity for the client.impact report. Evidence
    ONLY — never read by verdict logic, never in diff_ir. All fields optional;
    an instance is created only when at least one field is non-empty."""

    hostname: str | None = None
    family: str | None = None
    mfg: str | None = None
    model: str | None = None
    os: str | None = None
    auth_type: str | None = None
    auth_method: str | None = None
    auth_state: str | None = None
    nacrule: str | None = None
    status: str | None = None
    assigned_vlan: str | None = None
    vlan_source: str | None = None
    username: str | None = None
    # OBSERVED provenance, mirroring every other IR entity. NOT part of the
    # identity projection — the check allowlists identity fields (Task 5), so meta
    # never leaks into evidence["impacts"][i].identity.
    meta: FactMeta = OBSERVED_META
```

> `FactMeta` and `OBSERVED_META` are already imported in `entities.py` (line 14: `from .provenance import CONFIG_META, OBSERVED_META, FactMeta`) — no new import needed.

- [ ] **Step 4: Export it**

In `src/digital_twin/ir/__init__.py`, add `ClientEnrichment` to the `from .entities import (...)` block (after `Client,`) and to `__all__` (after `"Client",`).

- [ ] **Step 5: Write the pure join**

Create `src/digital_twin/adapters/mist/ingest/client_enrichment.py`:

```python
"""Pure MAC-keyed enrichment join: wired ∪ wireless (base) + nac (overlay).

OBSERVATIONAL only. Per-row try/except so one malformed row never drops the
batch; the ingester (same module) adds a whole-body backstop so it can never be
fatal. NAC overlays the base per-field, but only when the overlay value is
USEFUL — Mist's literal "Unknown"/empty collapses to None so it cannot clobber a
good base value (e.g. a real OUI manufacturer)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from digital_twin.ir import ClientEnrichment, client_id

_Json = dict[str, Any]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" or s.lower() == "unknown" else s


def _first(row: _Json, *keys: str) -> Any:
    """First present value across `keys`; a list value yields its first element."""
    for k in keys:
        v = row.get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v is not None:
            return v
    return None


def _wired_vals(row: _Json) -> dict[str, Any]:
    return {
        "hostname": _first(row, "last_hostname", "hostname"),
        "mfg": row.get("manufacture"),
        "username": _first(row, "last_username", "username"),
        "auth_method": row.get("auth_method"),
        "auth_state": row.get("auth_state"),
    }


def _wireless_vals(row: _Json) -> dict[str, Any]:
    return {
        "hostname": _first(row, "last_hostname", "hostname"),
        "family": _first(row, "last_family", "family"),
        "mfg": row.get("manufacture"),
        "model": _first(row, "last_model", "model"),
        "os": _first(row, "last_os", "os"),
    }


def _nac_vals(row: _Json) -> dict[str, Any]:
    return {
        "hostname": _first(row, "last_hostname", "hostname"),
        "family": _first(row, "last_family", "family"),
        "mfg": _first(row, "last_mfg", "mfg"),
        "model": _first(row, "last_model", "model"),
        "os": _first(row, "last_os", "os"),
        "auth_type": row.get("auth_type"),
        "nacrule": _first(row, "last_nacrule_name", "nacrule_name"),
        "status": row.get("last_status"),
        "assigned_vlan": _first(row, "last_vlan", "vlan"),
        "vlan_source": row.get("vlan_source"),
        "username": _first(row, "last_username", "username"),
    }


def _apply(acc: dict[str, dict[str, str]], row: _Json, extract: Any) -> None:
    try:
        mac = row.get("mac")
        if not mac:
            return
        cur = acc.setdefault(client_id(str(mac)), {})
        for key, raw in extract(row).items():
            cleaned = _clean(raw)
            if cleaned is not None:  # non-None overwrites -> processing order = precedence
                cur[key] = cleaned
    except Exception:  # noqa: BLE001 — one malformed row never drops the batch
        return


def build_client_enrichment(
    *, wired: Iterable[_Json], wireless: Iterable[_Json], nac: Iterable[_Json]
) -> dict[str, ClientEnrichment]:
    acc: dict[str, dict[str, str]] = {}
    for row in wired:
        _apply(acc, row, _wired_vals)
    for row in wireless:
        _apply(acc, row, _wireless_vals)
    for row in nac:  # last -> NAC wins per useful field
        _apply(acc, row, _nac_vals)
    return {mac: ClientEnrichment(**vals) for mac, vals in acc.items() if vals}
```

> Note: the test calls `build_client_enrichment(wired=..., wireless=..., nac=...)` with keyword args — the signature is keyword-only (`*`).

- [ ] **Step 6: Run to verify pass + gate**

Run: `uv run pytest tests/adapters/mist/test_client_enrichment_join.py -v && uv run ruff check . && uv run mypy src`
Expected: PASS, clean.

- [ ] **Step 7: Commit**

```bash
git add src/digital_twin/ir/entities.py src/digital_twin/ir/__init__.py src/digital_twin/adapters/mist/ingest/client_enrichment.py tests/adapters/mist/test_client_enrichment_join.py
git commit -m "feat(enrich): ClientEnrichment record + pure MAC-keyed wired/wireless/nac join"
```

### Task 3: IR `client_enrichment` field + builder + diff isolation

**Files:**
- Modify: `src/digital_twin/ir/model.py` (IR field, builder dict + method, build() wiring)
- Test: `tests/ir/test_client_enrichment_ir.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/ir/test_client_enrichment_ir.py
from digital_twin.ir import ClientEnrichment
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _ir_with(enrich: dict[str, ClientEnrichment]):
    b = IRBuilder()
    b.set_client_enrichment(enrich)
    return b.build()


def test_builder_exposes_client_enrichment():
    ir = _ir_with({"aa": ClientEnrichment(hostname="r2d2")})
    assert ir.client_enrichment["aa"].hostname == "r2d2"


def test_empty_default_is_empty_mapping():
    assert dict(IRBuilder().build().client_enrichment) == {}


def test_diff_ignores_client_enrichment():
    # the key non-load-bearing acceptance test: enrichment-only change -> empty diff
    base = _ir_with({"aa": ClientEnrichment(hostname="old")})
    prop = _ir_with({"aa": ClientEnrichment(hostname="new"), "bb": ClientEnrichment(family="Printer")})
    assert diff_ir(base, prop).is_empty()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/ir/test_client_enrichment_ir.py -v`
Expected: FAIL (`AttributeError: 'IRBuilder' object has no attribute 'set_client_enrichment'`).

- [ ] **Step 3: Implement on the IR + builder**

In `src/digital_twin/ir/model.py`:

(a) import the record — extend the `from .entities import (...)` block with `ClientEnrichment,` (alphabetically near `Client,`).

(b) add the IR field (after `ap_wlan_unresolved`, line 59):

```python
    ospf_intfs: tuple[OspfIntf, ...] = ()
    # OBSERVATIONAL per-client identity for the client.impact report (mac ->
    # ClientEnrichment). Evidence only: NOT walked by diff_ir, earns no
    # capability, never read by verdict logic. Defaulted: absence = no enrichment.
    client_enrichment: Mapping[str, ClientEnrichment] = _EMPTY_MAP  # type: ignore[assignment]
```

(c) in `IRBuilder.__init__` (after `self._ap_wlan_unresolved`, line 88):

```python
        self._client_enrichment: Mapping[str, ClientEnrichment] = {}
```

(d) add the builder method (after `mark_ap_wlan_unresolved`, line 153). It publishes the
WHOLE map in one assignment so a half-built map can never be observed (the ingester computes
the complete map first, then calls this once — atomic publish):

```python
    def set_client_enrichment(self, enrichment: Mapping[str, ClientEnrichment]) -> IRBuilder:
        """Publish the COMPLETE observational enrichment map atomically. NOT validated
        in build() — a bad entry must never fail the IR (non-load-bearing). Replacing
        (not merging) keeps 'broken enrichment == no enrichment': a partial map is never
        observed."""
        self._client_enrichment = dict(enrichment)
        return self
```

(e) wire into `build()`'s `IR(...)` (after the `ap_wlan_unresolved=` block, ~line 334):

```python
            ap_wlan_unresolved=MappingProxyType(
                {ap: tuple(r) for ap, r in self._ap_wlan_unresolved.items()}
            ),
            client_enrichment=MappingProxyType(dict(self._client_enrichment)),
```

> Do **not** add a `_validate_*` for it and do **not** add it to `diff.py` `_ENTITY_KINDS` — both omissions are intentional (non-load-bearing).

- [ ] **Step 4: Run to verify pass + gate**

Run: `uv run pytest tests/ir/test_client_enrichment_ir.py -v && uv run pytest tests -q && uv run mypy src`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/ir/model.py tests/ir/test_client_enrichment_ir.py
git commit -m "feat(enrich): ir.client_enrichment field + builder + diff isolation"
```

### Task 4: the self-isolating best-effort `ClientEnrichmentIngester`

**Files:**
- Modify: `src/digital_twin/adapters/mist/ingest/client_enrichment.py` (add the ingester class)
- Modify: `src/digital_twin/adapters/mist/adapter.py:48` (register it)
- Test: `tests/adapters/mist/test_ingest_client_enrichment.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/adapters/mist/test_ingest_client_enrichment.py
from datetime import UTC, datetime

from digital_twin.adapters.mist.adapter import MistAdapter
from digital_twin.adapters.mist.ingest.base import IngestContext
from digital_twin.adapters.mist.ingest.client_enrichment import ClientEnrichmentIngester
from digital_twin.ir.model import IRBuilder
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta


def _raw(*, nac=(), wired=(), wireless=()) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id="o1", site_id="s1"), site={}, setting={},
        networktemplate=None, devices=(), device_stats=(), port_stats=(),
        wireless_clients=tuple(wireless), wired_clients=tuple(wired), derived_setting=None,
        nac_clients=tuple(nac),
        meta=StateMeta(acquired_at=datetime.now(UTC), host="t", fetched=(), failures=()),
    )


def _ctx(raw: RawSiteState) -> IngestContext:
    return IngestContext(raw=raw, site_effective={}, device_effective={}, builder=IRBuilder())


def test_ingester_populates_enrichment():
    ctx = _ctx(_raw(nac=({"mac": "aabbcc000001", "last_family": "Surveillance Camera",
                          "last_mfg": "Verkada Inc"},)))
    assert ClientEnrichmentIngester().ingest(ctx) == frozenset()  # earns NO capability
    ce = ctx.builder.build().client_enrichment["aabbcc000001"]
    assert ce.family == "Surveillance Camera" and ce.mfg == "Verkada Inc"


def test_ingester_never_raises_on_garbage():
    # rows that would make a naive parser blow up -> swallowed, empty map, no raise
    ctx = _ctx(_raw(nac=("not-a-dict", 42, {"mac": None})))  # type: ignore[arg-type]
    assert ClientEnrichmentIngester().ingest(ctx) == frozenset()
    assert dict(ctx.builder.build().client_enrichment) == {}


def test_broken_nac_does_not_taint_report_ok_through_the_adapter():
    # the verdict-path guarantee: a malformed nac_clients row must NOT add to
    # IngestReport.failures (which would flip report.ok -> ir=None -> UNKNOWN)
    raw = _raw(nac=("garbage", {"oops": 1}))  # type: ignore[arg-type]
    outcome = MistAdapter().ingest(raw)
    assert outcome.report.ok is True
    assert outcome.ir is not None
    assert all(f.ingester != "client_enrichment" for f in outcome.report.failures)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/adapters/mist/test_ingest_client_enrichment.py -v`
Expected: FAIL (`ImportError: cannot import name 'ClientEnrichmentIngester'`).

- [ ] **Step 3: Add the ingester (whole-body backstop)**

Append to `src/digital_twin/adapters/mist/ingest/client_enrichment.py`:

```python
from .base import IngestContext  # noqa: E402 — keep the pure join above the ingester


class ClientEnrichmentIngester:
    """Best-effort observational enrichment. SELF-ISOLATING: it never lets an
    exception reach IngesterRegistry.run (which would record an IngestReport
    failure -> report.ok False -> ir=None -> UNKNOWN). Earns NO capability."""

    name = "client_enrichment"

    def produces(self) -> frozenset[str]:
        return frozenset()  # purely additive; nothing requires() it

    def ingest(self, ctx: IngestContext) -> frozenset[str]:
        try:
            # Build the COMPLETE map first, then publish in ONE atomic call. If
            # anything here raises, nothing is published — so a partial map is never
            # observed and "broken enrichment == no enrichment" holds exactly.
            enrich = build_client_enrichment(
                wired=ctx.raw.wired_clients,
                wireless=ctx.raw.wireless_clients,
                nac=ctx.raw.nac_clients,
            )
            ctx.builder.set_client_enrichment(enrich)
        except Exception:  # noqa: BLE001 — best-effort: degrade to "no enrichment", never fatal
            pass
        return frozenset()
```

> `IRCapability` is not imported because this ingester returns `frozenset()` always — by design it earns nothing.

- [ ] **Step 4: Register it in the adapter**

In `src/digital_twin/adapters/mist/adapter.py`, add the import near the other ingester imports and append to the default list (line 48):

```python
            else [SwitchIngester(), LldpIngester(), ClientsIngester(), WlanIngester(),
                  ClientEnrichmentIngester()]
```

Add the import (with the sibling ingester imports near the top of the file):

```python
from digital_twin.adapters.mist.ingest.client_enrichment import ClientEnrichmentIngester
```

- [ ] **Step 5: Run to verify pass + gate**

Run: `uv run pytest tests/adapters/mist/test_ingest_client_enrichment.py -v && uv run pytest tests -q && uv run mypy src`
Expected: PASS, full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/digital_twin/adapters/mist/ingest/client_enrichment.py src/digital_twin/adapters/mist/adapter.py tests/adapters/mist/test_ingest_client_enrichment.py
git commit -m "feat(enrich): self-isolating best-effort ClientEnrichmentIngester (never UNKNOWN)"
```

---

## Phase 3 — check enrichment

### Task 5: enrich `client_impact` impact entries (identity / subnet / dhcp_vlan_touched)

**Files:**
- Modify: `src/digital_twin/checks/wired/client_impact.py` (`_entry` + 2 helpers + call sites)
- Test: `tests/checks/test_client_impact_enrichment.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/checks/test_client_impact_enrichment.py
"""Enrichment is annotation-only: identity from BASELINE, subnet from baseline
Vlan.subnet, dhcp_vlan_touched from the delta. None of it changes the verdict."""
from digital_twin.analysis.context import AnalysisContext
from digital_twin.checks.base import CheckContext
from digital_twin.checks.wired.client_impact import ClientImpactCheck
from digital_twin.ir import (
    AttachKind, Client, ClientEnrichment, ClientKind, Device, DeviceRole, DhcpScope,
    IRCapability, Port, PortMode, Vlan,
)
from digital_twin.ir.diff import diff_ir
from digital_twin.ir.model import IRBuilder


def _build(*, native: int, subnet=None, enrich=None, dhcp_trusted=None,
           dhcp_sources=(), snooping=None, scope=False) -> object:
    """vlan 10 ('corp'). Optional dhcp dimensions exercise _dhcp_vlan_touched's
    four triggers: dhcp_sources, a serving DhcpScope, device.dhcp_snooping, port trust."""
    b = IRBuilder()
    b.with_capability(IRCapability.WIRED_L2).with_capability(IRCapability.CLIENTS_ACTIVE)
    b.add_device(Device(id="sw1", role=DeviceRole.SWITCH, site="s1", dhcp_snooping=snooping))
    # Port.mode is REQUIRED (entities.py:116) — set it explicitly
    p = Port(id="sw1:ge-0/0/1", device_id="sw1", name="ge-0/0/1", mode=PortMode.ACCESS,
             native_vlan=native, dhcp_trusted=dhcp_trusted)
    b.add_port(p)
    b.add_vlan(Vlan(vlan_id=10, name="corp", subnet=subnet, dhcp_sources=tuple(dhcp_sources)))
    if scope:  # provider="site" needs no gateway device (build validation skips it)
        b.add_dhcp_scope(DhcpScope(provider="site", network="corp", vlan=10))
    b.add_client(Client(mac="aabbcc000001", kind=ClientKind.WIRED,
                        attach_kind=AttachKind.PORT, attach_id="sw1:ge-0/0/1", vlan=10))
    b.set_client_enrichment(enrich or {})  # atomic publish (see Task 3)
    return b.build()


def _ctx(base, prop) -> CheckContext:
    return CheckContext(baseline=AnalysisContext(base), proposed=AnalysisContext(prop),
                        diff=diff_ir(base, prop))


def test_identity_from_baseline_and_subnet():
    enrich = {"aabbcc000001": ClientEnrichment(hostname="r2d2", family="Printer", mfg="HP")}
    base = _build(native=10, subnet="10.0.0.0/24", enrich=enrich)
    prop = _build(native=20, subnet="10.0.0.0/24", enrich={})  # vlan_move; proposed has NO enrich
    res = ClientImpactCheck().run(_ctx(base, prop))
    entry = res.findings[0].evidence["impacts"][0]
    assert entry["impact"] == "vlan_move"
    assert entry["identity"] == {"hostname": "r2d2", "family": "Printer", "mfg": "HP"}
    assert entry["subnet"] == "10.0.0.0/24"            # from BASELINE vlan
    assert entry["dhcp_vlan_touched"] is False


def test_identity_omitted_when_no_enrichment():
    base = _build(native=10)
    prop = _build(native=20)
    entry = ClientImpactCheck().run(_ctx(base, prop)).findings[0].evidence["impacts"][0]
    assert "identity" not in entry and entry["subnet"] is None


def _touched(base, prop) -> bool:
    # every arm moves native 10->20 so client.impact emits a vlan_move entry to annotate
    entry = ClientImpactCheck().run(_ctx(base, prop)).findings[0].evidence["impacts"][0]
    return entry["dhcp_vlan_touched"]


def test_dhcp_vlan_touched_on_port_trust_flip():  # (d)
    assert _touched(_build(native=10, dhcp_trusted=True),
                    _build(native=20, dhcp_trusted=False)) is True


def test_dhcp_vlan_touched_on_dhcp_sources_change():  # (a)
    assert _touched(_build(native=10, dhcp_sources=()),
                    _build(native=20, dhcp_sources=("site",))) is True


def test_dhcp_vlan_touched_on_serving_scope_change():  # (b)
    assert _touched(_build(native=10, scope=False),
                    _build(native=20, scope=True)) is True


def test_dhcp_vlan_touched_on_applicable_snooping_change():  # (c) snooping now covers corp
    # corp has a modeled dhcp source, and snooping flips None -> all ('*') -> corp snooped
    assert _touched(_build(native=10, dhcp_sources=("site",), snooping=None),
                    _build(native=20, dhcp_sources=("site",), snooping=("*",))) is True


def test_dhcp_vlan_NOT_touched_when_snooping_change_misses_client_vlan():  # (c) negative
    # snooping changes, but never includes the client's vlan name 'corp' -> not touched
    assert _touched(_build(native=10, dhcp_sources=("site",), snooping=("other",)),
                    _build(native=20, dhcp_sources=("site",), snooping=("other", "extra"))) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/checks/test_client_impact_enrichment.py -v`
Expected: FAIL (`KeyError: 'identity'` / `'subnet'`).

- [ ] **Step 3: Implement the enrichment in the check**

In `src/digital_twin/checks/wired/client_impact.py`:

(a) two import edits — ruff sorts by module path (`I` is enabled), so place each in its
correct alphabetical slot or the gate fails:
- extend the existing entities import (line 20) to add `ClientEnrichment`:

```python
from digital_twin.ir.entities import AttachKind, Client, ClientEnrichment
```

- add the snooping helper WITH the other `digital_twin.checks` imports — immediately after
  `from digital_twin.checks.base import ...` and BEFORE `from digital_twin.contracts import ...`:

```python
from digital_twin.checks.wired.snooping import _snooped_vlans  # "vlans this device snoops"
```

> No import cycle: `snooping.py` does not import `client_impact`. Reusing `_snooped_vlans`
> keeps the "snooping applies to this vlan" semantics in ONE place (`("*",)` = all
> dhcp-source vlans; named snooping via `Vlan.name`).

(b) thread enrichment facts through `_entry`. Replace `_impact_of`'s three `self._entry(...)` calls so each passes `ctx` (so `_entry` can compute facts). Simplest: change `_entry` to accept `ctx` and the client, and compute identity/subnet/dhcp there. Replace the whole `_entry` method (lines 116-126) with:

```python
    def _entry(
        self, ctx: CheckContext, client: Client, impact: str, detail: str,
        caused_by: tuple[Cause, ...] = (),
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "mac": client.mac,
            "vlan": client.vlan,
            "attachment": client.attach_id,
            "impact": impact,
            "detail": detail,
            "caused_by": caused_by,
            "subnet": self._subnet(ctx, client),
            "dhcp_vlan_touched": self._dhcp_vlan_touched(ctx, client),
        }
        identity = self._identity(ctx, client)
        if identity:
            entry["identity"] = identity
        return entry

    def _identity(self, ctx: CheckContext, client: Client) -> dict[str, str]:
        # BASELINE enrichment: the finding describes clients connected BEFORE the change.
        # Project an EXPLICIT allowlist (_IDENTITY_FIELDS) so the record's `meta` (and any
        # future non-identity field) never leaks into evidence["impacts"][i].identity.
        ce: ClientEnrichment | None = ctx.baseline.ir.client_enrichment.get(client.id)
        if ce is None:
            return {}
        return {
            name: getattr(ce, name)
            for name in _IDENTITY_FIELDS
            if getattr(ce, name) is not None
        }

    def _subnet(self, ctx: CheckContext, client: Client) -> str | None:
        if client.vlan is None:
            return None
        vlan = ctx.baseline.ir.vlans.get(client.vlan)
        return vlan.subnet if vlan is not None else None

    def _dhcp_vlan_touched(self, ctx: CheckContext, client: Client) -> bool:
        vid = client.vlan
        base_ir, prop_ir = ctx.baseline.ir, ctx.proposed.ir
        # (a) the vlan's modeled DHCP providers changed
        if vid is not None:
            bv, pv = base_ir.vlans.get(vid), prop_ir.vlans.get(vid)
            if bv is not None and pv is not None and bv.dhcp_sources != pv.dhcp_sources:
                return True
            # (b) a DHCP scope SERVING this vlan was added/removed/changed
            # (DhcpScope exposes `vlan`, NOT `vlan_id` — entities.py:208)
            def serving(ir: Any) -> dict[str, Any]:
                return {s.id: s for s in ir.dhcp_scopes if s.vlan == vid}
            if serving(base_ir) != serving(prop_ir):
                return True
        # (d) the client's own attach port: dhcp_trusted flip
        if client.attach_kind is AttachKind.PORT:
            bp, pp = base_ir.ports.get(client.attach_id), prop_ir.ports.get(client.attach_id)
            if bp is not None and pp is not None and bp.dhcp_trusted != pp.dhcp_trusted:
                return True
            # (c) snooping on the client's switch — counts ONLY if it flips whether the
            # CLIENT's vlan is snooped (not any snooping change). Reuses _snooped_vlans.
            if bp is not None and vid is not None and (
                (vid in _snooped_vlans(base_ir, bp.device_id))
                != (vid in _snooped_vlans(prop_ir, bp.device_id))
            ):
                return True
        return False
```

(c) add the identity-field allowlist as a module constant (near `_CAVEAT`, after the
imports). `Any` is already imported (`from typing import Any`, line 14) — no `dataclasses`
import is needed:

```python
# the ONLY ClientEnrichment fields projected into evidence — excludes `meta` so
# observational provenance never leaks into the report.
_IDENTITY_FIELDS = (
    "hostname", "family", "mfg", "model", "os", "auth_type", "auth_method",
    "auth_state", "nacrule", "status", "assigned_vlan", "vlan_source", "username",
)
```

(d) update the three call sites in `_impact_of` (lines 81, 86, 100) to pass `ctx` first:

```python
                return self._entry(
                    ctx, client, "disconnect", "attach port removed",
                    caused_by=ctx.delta_index.causes("port", [client.attach_id]),
                )
```
```python
                return self._entry(
                    ctx, client,
                    "vlan_move",
                    f"access vlan {base_port.native_vlan} -> {prop_port.native_vlan}",
                    caused_by=ctx.delta_index.causes("port", [client.attach_id]),
                )
```
```python
                                return self._entry(
                                    ctx, client, "blackhole", f"vlan {vlan} segment loses its exit",
                                    caused_by=causes_for_blackhole(ctx, vlan, comp),
                                )
```

- [ ] **Step 4: Run to verify pass + gate**

Run: `uv run pytest tests/checks/test_client_impact_enrichment.py -v && uv run pytest tests -q && uv run mypy src`
Expected: PASS. The existing `tests/checks/test_client_impact*.py` still pass (entries gain keys, existing asserts on `mac`/`impact`/`caused_by` unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/checks/wired/client_impact.py tests/checks/test_client_impact_enrichment.py
git commit -m "feat(enrich): client.impact annotates identity/subnet/dhcp_vlan_touched (baseline, annotation-only)"
```

---

## Phase 4 — rendering

### Task 6: human-output per-client expansion (capped) + dict carries identity

**Files:**
- Modify: `src/digital_twin/drivers/render.py` (`render_human` loop + a helper)
- Test: `tests/drivers/test_render_client_impact.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/drivers/test_render_client_impact.py
from digital_twin.contracts import Finding, FindingCategory, FindingSource, Severity
from digital_twin.drivers.render import render_human, verdict_to_dict
from digital_twin.ir import Confidence, ConfidenceLevel
from digital_twin.verdict.decision import Decision
from digital_twin.verdict.verdict import Verdict


def _verdict(impacts) -> Verdict:
    f = Finding(
        source=FindingSource.CHECK, category=FindingCategory.NETWORK,
        code="wired.client.impact.active_clients", severity=Severity.WARNING,
        confidence=Confidence(level=ConfidenceLevel.HIGH),
        message=f"{len(impacts)} currently-connected client(s) affected by the delta",
        affected_entities=tuple(i["mac"] for i in impacts), evidence={"impacts": impacts},
    )
    return Verdict(decision=Decision.REVIEW, overall_severity=Severity.WARNING,
                   decision_reasons=(), check_results=(), findings=(f,))


def test_human_expands_each_client_line():
    impacts = [{"mac": "aabbcc000001", "vlan": 30, "attachment": "sw1:mge-0/0/1",
                "impact": "disconnect", "detail": "attach port removed", "caused_by": (),
                "subnet": None, "dhcp_vlan_touched": False,
                "identity": {"hostname": "LiveDemo-CD51", "family": "Surveillance Camera",
                             "mfg": "Verkada Inc", "auth_type": "mab", "status": "permitted",
                             "nacrule": "wired_camera_mab"}}]
    out = render_human(_verdict(impacts))
    assert "LiveDemo-CD51" in out and "Surveillance Camera" in out
    assert "disconnect" in out and "mab" in out


def test_human_caps_at_20_with_more_note():
    impacts = [{"mac": f"aa{i:010x}", "vlan": 10, "attachment": "sw1:ge-0/0/1",
                "impact": "blackhole", "detail": "x", "caused_by": (),
                "subnet": None, "dhcp_vlan_touched": False} for i in range(25)]
    out = render_human(_verdict(impacts))
    assert "and 5 more" in out


def test_dict_carries_full_identity():
    impacts = [{"mac": "aabbcc000001", "vlan": 10, "attachment": "sw1:ge-0/0/1",
                "impact": "vlan_move", "detail": "access vlan 1 -> 20", "caused_by": (),
                "subnet": "10.0.0.0/24", "dhcp_vlan_touched": True,
                "identity": {"hostname": "LD_Kitchen", "mfg": "Mist Systems, Inc."}}]
    d = verdict_to_dict(_verdict(impacts))
    got = d["findings"][0]["evidence"]["impacts"][0]
    assert got["identity"]["hostname"] == "LD_Kitchen" and got["dhcp_vlan_touched"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/drivers/test_render_client_impact.py -v`
Expected: FAIL (`test_human_expands...` and `test_human_caps...` fail — no expansion yet; the dict test already passes via `_plain`).

- [ ] **Step 3: Implement the expansion**

In `src/digital_twin/drivers/render.py`, add a helper after `_finding_line` (line 35):

```python
_MAX_IMPACT_LINES = 20


def _impact_lines(f: Finding) -> list[str]:
    """Expand wired.client.impact per-client entries into indented human lines."""
    impacts = f.evidence.get("impacts")
    if not isinstance(impacts, list):
        return []
    lines: list[str] = []
    for i in impacts[:_MAX_IMPACT_LINES]:
        ident = i.get("identity") or {}
        who = ident.get("hostname") or i.get("mac", "?")
        kind = " · ".join(
            str(ident[k]) for k in ("family", "model", "mfg", "os") if ident.get(k)
        )
        kind = f" ({kind})" if kind else ""
        detail = f": {i['detail']}" if i.get("detail") else ""
        tags = []
        if ident.get("auth_type") or ident.get("status") or ident.get("nacrule"):
            auth = "/".join(str(ident[k]) for k in ("auth_type", "status") if ident.get(k))
            via = f" via {ident['nacrule']}" if ident.get("nacrule") else ""
            tags.append(f"auth {auth}{via}")
        if i.get("subnet"):
            tags.append(f"subnet {i['subnet']}")
        if i.get("dhcp_vlan_touched"):
            tags.append("dhcp config changed")
        tagstr = "".join(f"  [{t}]" for t in tags)
        lines.append(
            f"    - {who}{kind} vlan {i.get('vlan')} on {i.get('attachment')}"
            f" — {i.get('impact')}{detail}{tagstr}"
        )
    extra = len(impacts) - _MAX_IMPACT_LINES
    if extra > 0:
        lines.append(f"    … and {extra} more (see JSON)")
    return lines
```

Then in `render_human`, replace the findings loop (lines 70-71):

```python
    for f in verdict.findings[:20]:
        lines.append(_finding_line(f))
        lines.extend(_impact_lines(f))
```

- [ ] **Step 4: Run to verify pass + gate**

Run: `uv run pytest tests/drivers/test_render_client_impact.py -v && uv run pytest tests -q && uv run mypy src`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/drivers/render.py tests/drivers/test_render_client_impact.py
git commit -m "feat(enrich): human render expands client.impact per-client (capped 20), dict carries identity"
```

---

## Phase 5 — equivalence golden + fixture + docs

### Task 7: enrichment absent/present/broken equivalence golden

Reuse the **existing GS4 scenario** (`tests/golden/test_golden_scenarios.py::test_gs4_access_vlan_change_with_client_is_review`) — it already moves `WIRED_CLIENT_MAC`'s access VLAN and fires `wired.client.impact` (vlan_move). The equivalence golden rebuilds that exact doc + plan three times, varying ONLY `doc["nac_clients"]`. No new `builders.py` function is needed (all helpers — `augmented_doc`, `device_op`, `plan_for`, `write_doc` — already live in `tests/golden/builders.py`).

**Files:**
- Create: `tests/golden/test_client_enrichment_equivalence.py`

- [ ] **Step 1: Write the test**

```python
# tests/golden/test_client_enrichment_equivalence.py
"""Enrichment is non-load-bearing: present / absent / BROKEN nac_clients must all
produce the same decision, severity multiset, and coverage. The broken arm is the
regression for the self-isolating-ingester guarantee (a malformed row never UNKNOWNs)."""
import copy

from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision
from tests.golden.builders import (
    EDGE, EDGE_ACCESS_PORT, GS_VLAN, WIRED_CLIENT_MAC,
    augmented_doc, device_op, plan_for, write_doc,
)

_NAC_PRESENT = [{
    "mac": WIRED_CLIENT_MAC, "last_family": "Surveillance Camera", "last_mfg": "Verkada Inc",
    "auth_type": "mab", "last_status": "permitted", "last_nacrule_name": "wired_camera_mab",
    "last_vlan": str(GS_VLAN), "vlan_source": "nactag",
}]
# rows a naive parser would choke on: a bare string, a mac-less dict, a None mac
_NAC_BROKEN = ["garbage", {"oops": 1}, {"mac": None}]


def _gs4_doc_and_plan():
    """GS4: move the wired client's access port off vlan 999 -> client.impact fires."""
    doc = augmented_doc(parallel_carries_gs=True, with_wireless_client=False)
    doc["setting"]["port_usages"]["gs_access2"] = {
        "mode": "access",
        "port_network": next(
            name for name, net in doc["setting"]["networks"].items()
            if isinstance(net, dict) and net.get("vlan_id") not in (None, 999)
        ),
    }
    plan = plan_for(
        doc, [device_op(doc, EDGE, **{EDGE_ACCESS_PORT.replace("/", "__"): "gs_access2"})]
    )
    return doc, plan


def _signature(v):
    return (
        v.decision,
        tuple(sorted(f.severity.value for f in v.findings)),
        tuple(sorted((r.check_id, r.coverage.state.value) for r in v.check_results)),
    )


def _run(nac, tmp_path, tag):
    doc, plan = _gs4_doc_and_plan()
    doc["nac_clients"] = copy.deepcopy(nac)
    fixture = write_doc(doc, tmp_path / f"{tag}.json")
    return simulate(plan, provider=FixtureProvider(fixture))


def test_present_absent_broken_are_equivalent(tmp_path):
    present = _run(_NAC_PRESENT, tmp_path, "present")
    absent = _run([], tmp_path, "absent")
    broken = _run(_NAC_BROKEN, tmp_path, "broken")
    assert _signature(present) == _signature(absent) == _signature(broken)
    assert broken.decision is not Decision.UNKNOWN  # self-isolating ingester held


def test_present_arm_actually_enriches(tmp_path):
    present = _run(_NAC_PRESENT, tmp_path, "present2")
    impact = next(f for f in present.findings if f.code == "wired.client.impact.active_clients")
    entry = next(i for i in impact.evidence["impacts"] if i["mac"] == WIRED_CLIENT_MAC)
    assert entry["identity"]["family"] == "Surveillance Camera"
    assert entry["identity"]["nacrule"] == "wired_camera_mab"
```

> Why this works with no provider changes: `write_doc` dumps the doc dict as-is and `FixtureProvider` reads it via `load_fixture_doc`, which (after Task 1) carries `nac_clients`. The broken rows are skipped per-row inside the join (`"garbage".get` → caught), so the broken arm yields empty enrichment === the absent arm, and the ingester never adds an `IngestReport.failures` entry.

- [ ] **Step 2: Run to verify it passes (it should pass once Tasks 1–5 are in)**

Run: `uv run pytest tests/golden/test_client_enrichment_equivalence.py -v`
Expected: PASS — three equal signatures, broken arm not UNKNOWN, present arm enriched.

- [ ] **Step 3: Gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add tests/golden/test_client_enrichment_equivalence.py
git commit -m "test(enrich): absent/present/broken equivalence golden (non-load-bearing + never-UNKNOWN)"
```

### Task 8: redacted fixture `nac_clients` + live verify + docs/roadmap/memory

**Files:**
- Modify: `tests/golden/fixtures/site.json` (add redacted `nac_clients`) — only if the committed real fixture is used by a client.impact golden; otherwise skip and note it.
- Modify: redaction config if NAC fields (username/mac/hostname) need new redaction rules — check `src/digital_twin/observability/redaction*`.
- Modify: `docs/ROADMAP.md` (flip the bullet to ✅), `docs/superpowers/specs/2026-06-19-richer-impacted-client-reporting-design.md` (Status → Implemented).
- Memory: `~/.claude/projects/-Users-tmunzer-4-dev-digital-twin/memory/digital-twin-project.md`.

- [ ] **Step 1: Resolve the two feasibility items live (read-only)**

Confirm the `mistapi` NAC search function name/params and that `searchOrgNacClients(..., site_id=, duration=)` returns the fields used (`last_family`/`last_mfg`/`last_nacrule_name`/...). Confirm whether the site `wired_clients` fetch surfaces `last_hostname`/`manufacture`; if not, document that wired-only (non-NAC, non-wireless) clients get reduced enrichment, and (optionally) follow up by switching `_wired_clients` to the rollup view. Run:

```bash
set -a; source .env; set +a
uv run digital-twin --plan <a plan that touches a port with active wired clients> 2>&1 | tail -40
```

Expected: a `wired.client.impact` finding whose human output now shows hostnames/fingerprints, and the **decision is unchanged** versus the pre-feature run on the same plan.

- [ ] **Step 2: Redaction for NAC fields**

If a committed fixture gains `nac_clients`, ensure `username`, `mac`, and `hostname` in those rows are redacted by the existing redaction pass (check `src/digital_twin/observability/redaction*`). Add rules only if a class of secret is not already covered. Re-capture/redact the fixture and run the hygiene CI test.

- [ ] **Step 3: Flip docs**

- Spec status `design — pending user review` → `Implemented — live-verified 2026-06-19`.
- `docs/ROADMAP.md`: change the `🔵 Richer impacted-client reporting` bullet to `✅ … done 2026-06-19`, noting traffic-significance stays deferred (no data source) and the wired-fetch field-surfacing follow-up if it was confirmed missing.

- [ ] **Step 4: Update project memory**

Add a bullet to `digital-twin-project.md` summarizing: evidence-only client enrichment (Approach B), `ir.client_enrichment` MAC-keyed wired∪wireless+nac join, self-isolating best-effort ingester (the verdict-path guarantee + the broken-arm golden), baseline-only identity, `dhcp_vlan_touched` definition, capped human expansion, and deferred traffic significance.

- [ ] **Step 5: Final gate + commit**

```bash
uv run pytest tests -q && uv run ruff check . && uv run mypy src
git add -A
git commit -m "docs(enrich): spec Implemented + roadmap done + redacted nac_clients fixture + live verify"
```

---

## Self-review (against the spec)

- **Fetch + raw state** → Task 1 (non-fatal via `attempt`, fixture passthrough). ✓
- **`ClientEnrichment` record + join (`"Unknown"`/empty→None, NAC overlay, HP case, cross-separator MAC, omission)** → Task 2. ✓
- **IR field + diff isolation** → Task 3 (and the key acceptance test). ✓
- **Self-isolating best-effort ingester (verdict-path guarantee, no capability, never `report.failures`)** → Task 4 (incl. the through-the-adapter `report.ok` test). ✓
- **Check enrichment: baseline identity, subnet, `dhcp_vlan_touched` (4 triggers), MAC-only degradation** → Task 5. ✓
- **Rendering: human expansion capped at 20 + dict full** → Task 6. ✓
- **Absent/present/broken equivalence golden** → Task 7. ✓
- **Redacted fixture + live verify + docs/roadmap/memory; deferred traffic significance** → Task 8. ✓

**Type consistency:** `build_client_enrichment(*, wired, wireless, nac)` keyword-only across Tasks 2/4; `ClientEnrichment` field names identical in entities (Task 2), `_identity` projection (Task 5), and the join (Task 2); `set_client_enrichment(mapping)` (atomic, whole-map) identical in builder (Task 3), ingester (Task 4), and check tests (Task 5). `DhcpScope.vlan` (not `vlan_id`) and `Port.mode` (required) verified against entities.py. `dhcp_vlan_touched` (bool) and `client_enrichment` (IR field) named consistently throughout.

**Known follow-up (flagged, not a gap):** if live verify shows the site `wired_clients` fetch omits `last_hostname`/`manufacture`, non-NAC wired clients get reduced enrichment until `_wired_clients` is switched to the rollup view — documented in Task 8, out of scope for this plan.
