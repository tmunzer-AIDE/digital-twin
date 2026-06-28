# SP2 — Org WLAN coverage-loss fan-out implementation plan

**Status:** PROPOSED
**Date:** 2026-06-28
**Spec:** `docs/superpowers/specs/2026-06-28-sp2-org-wlan-coverage-loss-design.md`

## Architecture

Add org-scope `object_type == "wlan"` to the existing org fan-out engine. The
provider resolves a baseline org WLAN snapshot and, more importantly, each
affected site's **derived** WLAN row. The org pipeline builds a WLAN overlay that
pins those per-site derived rows into `RawSiteState.wlans`; update proposals are
computed as `effective_update(site_derived_row, op.payload)`, never from the org
snapshot. The existing per-site `_simulate_site_state` path and SP1
`wireless.wlan.client_impact` check then decide impact.

The no-false-SAFE spine:

- assignment mutations (`site_ids`, `sitegroup_ids`) stay denied by the WLAN
  field gate -> UNKNOWN;
- baseline coverage comes from derived per-site rows, not potentially divergent
  org snapshots;
- membership probe failures are `FetchError` -> UNKNOWN;
- per-site fetch failures stay isolated in `site_failures`;
- per-site UNSAFE still beats per-site UNKNOWN in `decide_org`.

## Task 1 — Org routing and org-WLAN field gate

**Files**

- Modify `src/digital_twin/scope/allowlist.py`
- Modify `src/digital_twin/scope/object_gate.py`
- Modify `src/digital_twin/scope/field_gate.py`
- Modify tests:
  - `tests/scope/test_allowlist.py`
  - `tests/scope/test_object_gate.py`
  - `tests/scope/test_wlan_object.py`
  - `tests/drivers/test_cli.py`
  - `tests/drivers/test_mcp_server.py`

**RED**

Add tests:

- `ORG_OBJECT_TYPES` includes `"wlan"` while `SUPPORTED_OBJECT_TYPES` still
  includes `"wlan"`.
- no-`site_id` `wlan` update/delete passes `check_objects`.
- no-`site_id` `wlan` delete with non-empty payload rejects before fetch.
- site-id `wlan` update/delete remains valid for the SP1 site path.
- `_is_org_plan` returns true for no-site `wlan`, false for site-id `wlan`.
- `screen_op("wlan", inherited, proposed)` still rejects by default.
- `screen_op("wlan", inherited, proposed, enforce_wlan_site_ownership=False)`
  passes for modeled leaves such as `enabled`.
- org-scope assignment leaves such as `site_ids` / `sitegroup_ids` reject because
  they are changed paths outside `RAW_ALLOWLIST["wlan"]`.

**GREEN**

- Add `"wlan"` to `ORG_OBJECT_TYPES`.
- Update comments/docstrings that describe org mode as template-only.
- Parameterize `screen_op`:

```python
def screen_op(
    object_type: str,
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    enforce_wlan_site_ownership: bool = True,
) -> Rejection | None:
```

- Guard `wlan_is_inherited(current)` only when
  `enforce_wlan_site_ownership is True`.

**Verify**

```bash
uv run pytest tests/scope/test_allowlist.py tests/scope/test_object_gate.py tests/scope/test_wlan_object.py tests/drivers/test_cli.py tests/drivers/test_mcp_server.py -q
uv run mypy src
```

**Commit**

`feat(org-wlan): route org-scope wlan plans and gate modeled leaves`

## Task 2 — Provider contract and live org-WLAN resolver

**Files**

- Modify `src/digital_twin/providers/base.py`
- Modify `src/digital_twin/providers/mist_api.py`
- Modify `tests/providers/test_base.py`
- Modify `tests/providers/test_mist_api.py`

**RED**

Add tests:

- `OrgWlanContext` carries:

```python
wlan: JsonObj
derived_rows_by_site: Mapping[str, JsonObj]
```

- `MistApiProvider.resolve_org_wlan(OrgScope("o1"), "w1")`:
  - fetches the org WLAN snapshot;
  - lists org sites;
  - probes each site's derived WLAN list;
  - returns only sites whose derived rows contain id `"w1"`;
  - preserves each site's actual derived row, even if it differs from the org
    snapshot.
- missing org WLAN -> `FetchError`.
- any membership probe failure -> `FetchError`, never "not assigned".

Use a small subclass/fake of `MistApiProvider` that overrides `_org_sites`,
`_wlans`, and a new `_org_wlan` helper. Avoid network.

**GREEN**

- Add `OrgWlanContext` after `OrgTemplateContext` in `providers/base.py`.
- Add `StateProvider.resolve_org_wlan(...)`.
- In `MistApiProvider`, add:

```python
def _org_wlan(self, scope: OrgScope, wlan_id: str) -> _Json:
    resp = mistapi.api.v1.orgs.wlans.getOrgWLAN(self._session, scope.org_id, wlan_id)
    return dict(resp.data)
```

- Implement `resolve_org_wlan`:
  - call `_org_wlan`;
  - call `_org_sites`;
  - for every site with an id, call `_wlans(SiteScope(scope.org_id, sid))`;
  - collect the first row where `str(row.get("id")) == wlan_id`;
  - return `OrgWlanContext(wlan=dict(snapshot), derived_rows_by_site=...)`;
  - catch exceptions and return `FetchError` with object `"org_wlan"` or
    `"org_wlan_membership"`.

**Verify**

```bash
uv run pytest tests/providers/test_base.py tests/providers/test_mist_api.py -q
uv run mypy src
```

**Commit**

`feat(org-wlan): resolve org wlan affected sites from derived rows`

## Task 3 — WLAN-aware org overlays

**Files**

- Modify `src/digital_twin/engine/org_overlay.py`
- Modify `tests/engine/test_org_overlay.py`

**RED**

Add tests:

- A WLAN overlay upserts `wlan_baseline_by_site[site_id]` into baseline raw by id.
- A WLAN update overlay upserts `wlan_proposed_by_site[site_id]` into proposed raw
  by id.
- A WLAN delete overlay removes only that row from proposed raw.
- A site not in `assigned_site_ids` is untouched.
- A template overlay and WLAN overlay compose in one `apply_overlays` call.
- `assigned_site_ids != frozenset(wlan_baseline_by_site)` raises at construction
  or otherwise fails loudly.
- `wlan_proposed_by_site` keys must match baseline keys for WLAN overlays.
- The P1 guard: when `OrgOverlay.baseline` has `enabled=False` but the per-site
  derived baseline row has `enabled=True`, the baseline raw uses the derived
  `enabled=True` row. Pin this with an assertion on the returned raw state, e.g.
  `assert next(w for w in base_raw.wlans if w["id"] == "w1")["enabled"] is True`.

**GREEN**

- Add defaulted fields to `OrgOverlay`:

```python
wlan_baseline_by_site: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
wlan_proposed_by_site: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)
```

- Add `__post_init__` validation for `object_type == "wlan"`:
  - `assigned_site_ids == frozenset(wlan_baseline_by_site)`;
  - `frozenset(wlan_proposed_by_site) == assigned_site_ids`.
- Thread `object_id` into `_pin`, or split into `_pin_template` and `_pin_wlan`.
- Implement WLAN helpers:
  - upsert row by `id == object_id`;
  - remove row by `id == object_id`.
- In `apply_overlays`, branch on `o.object_type == "wlan"` and use per-site maps;
  template overlays keep the existing `baseline` / `proposed` slot behavior.

**Verify**

```bash
uv run pytest tests/engine/test_org_overlay.py -q
uv run mypy src
```

**Commit**

`feat(org-wlan): overlay per-site derived wlan rows`

## Task 4 — Replay fixture provider support

**Files**

- Modify `src/digital_twin/observability/replay/store.py`
- Modify `tests/observability/test_replay_store.py`

**RED**

Add multi-site fixture tests:

- fixture shape:

```json
{
  "org_wlans": {
    "w1": {"id": "w1", "ssid": "corp", "enabled": false}
  },
  "sites": {
    "s1": { "...": "...", "wlans": [{"id": "w1", "ssid": "corp", "enabled": true}] },
    "s2": { "...": "...", "wlans": [] }
  }
}
```

- `resolve_org_wlan(..., "w1")` returns the org snapshot plus only `s1`'s derived
  row.
- Missing `w1` -> `FetchError`, not zero sites.
- Wrong org -> `FetchError`.
- A site marked as a later `fetch_sites` failure can still be included in
  `derived_rows_by_site` if its fixture doc contains the row; this lets the org
  path later report a per-site fetch failure instead of losing membership.

**GREEN**

- In `FixtureProvider.__init__`, parse top-level `org_wlans` into
  `self._org_wlans: dict[str, dict[str, Any]]`.
- Implement `resolve_org_wlan`:
  - reject single-site fixtures;
  - enforce `_wrong_org` like `resolve_org_template`;
  - missing org WLAN -> `FetchError`;
  - scan `self._site_docs` / `self._sites` for derived `wlans` rows with matching id;
  - return `OrgWlanContext(wlan=dict(org_wlan), derived_rows_by_site=...)`.

**Verify**

```bash
uv run pytest tests/observability/test_replay_store.py -q
uv run mypy src
```

**Commit**

`feat(replay): support org wlan resolution in fixtures`

## Task 5 — Wire `simulate_org_plan` for org WLANs

**Files**

- Modify `src/digital_twin/engine/pipeline.py`
- Modify `tests/engine/test_org_plan.py`

**RED**

Extend the `FakeProvider` in `tests/engine/test_org_plan.py` with
`resolve_org_wlan`.

Add end-to-end org tests:

1. Org WLAN delete with one affected site, active wireless client, no survivor:
   per-site UNSAFE, org UNSAFE, finding code
   `wireless.wlan.client_impact.coverage_lost`, config diff present.
2. P1 regression: org snapshot has `enabled=False`, affected site's derived row
   has `enabled=True`; delete still produces `coverage_lost`.
3. Org WLAN update `{"enabled": False}` produces the same impact as delete.
4. Org WLAN rename/scope-shrink variants produce the same SP1 finding.
5. Site `s1` has same-SSID survivor and is SAFE; site `s2` has no survivor and
   is UNSAFE; org is UNSAFE with `driving_sites == ("s2",)`.
6. Missing client telemetry on affected site -> per-site REVIEW via
   `.unverified`.
7. Zero affected sites -> org SAFE, `changes` and `config_diffs` present.
8. Missing org WLAN -> org UNKNOWN, no fabricated config diff for the missing op.
9. Org WLAN assignment-field edit, e.g. `{"site_ids": ["s2"]}`, rejects at
   field gate UNKNOWN and carries the computable config diff.
10. Earlier org WLAN op diff survives a later lookup failure, preserving the
    "config diffs always surfaced" doctrine.

Use `registry=CheckRegistry([WlanClientImpactCheck()])` for the focused impact
tests.

Pin the three no-false-SAFE assertions explicitly:

```python
# T5 #2: derived row, not org snapshot, controls baseline coverage
assert ov.decision is Decision.UNSAFE, ov.decision_reasons
v = ov.per_site["s1"]
assert any(
    f.code == "wireless.wlan.client_impact.coverage_lost"
    for f in v.findings
)

# T5 #9: assignment edit is UNKNOWN at field gate, never modeled/SAFE
assert ov.decision is Decision.UNKNOWN
assert any("field_gate" in r for r in ov.decision_reasons)
assert any("site_ids" in r for r in ov.decision_reasons)
cds = {d.object_id: d for d in ov.config_diffs}
assert "w1" in cds
```

**GREEN**

In `simulate_org_plan`:

- Import `OrgWlanContext` and `effective_update`.
- In the per-op loop:
  - if `op.object_type == "wlan"`, call `provider.resolve_org_wlan`;
  - otherwise keep `provider.resolve_org_template`.
- For WLAN update:
  - `snapshot = dict(resolved.wlan)`;
  - `proposed_org = effective_update(snapshot, op.payload)`;
  - build org-level config diff from `snapshot -> proposed_org`;
  - run `adapter.validate(replace(op, payload=proposed_org), ...)`;
  - `_stamp` non-fatal L0 findings into `template_findings`;
  - call `screen_op("wlan", snapshot, proposed_org, enforce_wlan_site_ownership=False)`;
  - compute `wlan_proposed_by_site = {sid: effective_update(row, op.payload) ...}`.
- For WLAN delete:
  - `proposed_org = None`;
  - config diff from `snapshot -> None`;
  - `wlan_proposed_by_site = {sid: None for sid in derived_rows_by_site}`.
- Append `OrgOverlay` with:
  - `baseline=snapshot`, `proposed=proposed_org` for audit symmetry;
  - `assigned_site_ids=frozenset(derived_rows_by_site)`;
  - `wlan_baseline_by_site=...`;
  - `wlan_proposed_by_site=...`.
- Keep template path behavior unchanged.
- Update fetch-failure messages to say "org object lookup failed" or include
  `op.object_type`, rather than hard-coding "org-template" for WLAN failures.

**Verify**

```bash
uv run pytest tests/engine/test_org_plan.py tests/engine/test_pipeline.py tests/checks/test_wlan_client_impact.py -q
uv run mypy src
```

**Commit**

`feat(org-wlan): simulate org wlan coverage loss across affected sites`

## Task 6 — Drivers, docs, roadmap, full verification

**Files**

- Modify `src/digital_twin/drivers/cli.py`
- Modify `src/digital_twin/drivers/mcp_server.py`
- Modify `docs/ROADMAP.md`
- Keep / update:
  - `docs/superpowers/specs/2026-06-28-sp2-org-wlan-coverage-loss-design.md`
  - this plan file
- Modify tests as needed:
  - `tests/drivers/test_cli.py`
  - `tests/drivers/test_mcp_server.py`
  - `tests/drivers/test_render.py`

**RED**

Add/adjust tests:

- CLI no-site `wlan` plans route to `simulate_org_plan` / org verdict.
- CLI site-id `wlan` plans still route to `simulate`.
- MCP doc/error text no longer says org plans are only
  `networktemplate/gatewaytemplate/sitetemplate`.
- Rendered org output names a `wlan` change cleanly.

**GREEN**

- Update comments/docstrings/help text from "org/template" where it would now be
  misleading; do not change the JSON shape.
- The drivers currently call the back-compat alias `simulate_org_template`, not
  `simulate_org_plan` directly. That is fine; no functional code rename is
  required. The required change is routing/docstring wording so no-site `wlan`
  plans are recognized as org plans and are not described as template-only.
- Roadmap:
  - mark SP2 org WLAN coverage-loss fan-out done;
  - leave SP3 WLAN template/container changes deferred;
  - keep org WLAN assignment mutations and wireless-auth compatibility as
    explicit deferred items.
- If live verification is available, run a read-only org WLAN dry run against a
  harmless/zero-site or known-safe WLAN; otherwise record that synthetic fixtures
  prove the feature and live verification is deferred until a safe org WLAN target
  is identified.

**Verify**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
```

**Commit**

`docs(org-wlan): record SP2 completion and driver wording`

## Self-review checklist

- Spec coverage:
  - org `wlan` route ✓
  - `OrgWlanContext.derived_rows_by_site` ✓
  - derived-row baseline / per-site proposed update ✓
  - assignment-field UNKNOWN ✓
  - `OrgOverlay` invariant ✓
  - `_pin` object-id threading ✓
  - `template_findings` for org WLAN L0 ✓
  - driver wording ✓
- No false-SAFE audit:
  - no org snapshot pinned into baseline raw WLAN rows;
  - membership probe failure does not silently narrow affected sites;
  - no assignment edit can pass as modeled;
  - missing client telemetry remains REVIEW via SP1 check;
  - per-site UNSAFE still dominates per-site UNKNOWN in org roll-up.
- Compatibility:
  - site SP1 WLAN path unchanged;
  - existing template org path unchanged;
  - NAC org path unchanged;
  - no public check count changes.
