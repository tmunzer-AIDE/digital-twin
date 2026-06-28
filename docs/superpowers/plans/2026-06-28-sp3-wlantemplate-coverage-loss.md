# SP3 - WLAN template coverage-loss fan-out implementation plan

**Status:** PROPOSED
**Date:** 2026-06-28
**Spec:** `docs/superpowers/specs/2026-06-28-sp3-wlantemplate-coverage-loss-design.md`

## Goal

Implement SP3: simulate org-scope Mist WLAN template deletes
(`object_type == "wlantemplate"`) by resolving the concrete per-site derived
WLAN rows with that `template_id`, removing those rows from proposed
`RawSiteState.wlans`, and reusing the existing SP1
`wireless.wlan.client_impact` check.

SP3 v1 is deliberately delete-only. Mist's `/orgs/{org_id}/templates/{id}`
`Template` model is open-ended (`additionalProperties`), and update semantics
for derived WLAN rows are not modeled. Updates/create must resolve UNKNOWN, not
SAFE.

## Implementation baseline

Start from post-SP2 `main`:

- Required baseline: `c7c624b` or newer (`feat(org-wlan): simulate org WLAN coverage loss (#29)`).
- Reason: SP3 reuses SP2's org-WLAN fan-out machinery, `OrgWlanContext`,
  `wlan_baseline_by_site`, `wlan_proposed_by_site`, and the SP1
  `WlanClientImpactCheck`.

Preflight:

```bash
git fetch origin
git merge-base --is-ancestor c7c624b HEAD
```

Do not start from a pre-SP2 branch.

## Architecture

Add a new org object type, `wlantemplate`, mapped to Mist's generic org
`Template` endpoint (`/api/v1/orgs/{org_id}/templates/{template_id}`). The
provider resolves:

- the template snapshot, for `OrgChange` / config diff audit;
- the affected derived WLAN rows, grouped by site, by scanning each site's
  `listSiteWlansDerived` rows for `row.template_id == template_id`.

The pipeline creates a `wlantemplate` overlay. For each affected site, baseline
pins the resolver-captured rows into `raw.wlans`; proposed delete removes all
rows from the deleted template. Then `_simulate_site_state` and the existing
WLAN client-impact check decide the per-site verdict.

The no-false-SAFE spine:

- membership comes from derived site WLAN rows, not from the open-ended template
  body;
- membership-probe failure is `FetchError` -> org UNKNOWN;
- proposed delete removes rows by captured id and by matching `template_id`, so
  a later fetched same-template row cannot survive as a phantom WLAN;
- `wlantemplate` updates/create are UNKNOWN;
- plans mixing `wlantemplate` and first-class org `wlan` are UNKNOWN until
  overlap semantics are designed.

## Task 1 - Org routing and `wlantemplate` boundary

### Files

- Modify `src/digital_twin/scope/allowlist.py`
- Modify `src/digital_twin/scope/object_gate.py`
- Modify tests:
  - `tests/scope/test_allowlist.py`
  - `tests/scope/test_object_gate.py`
  - `tests/drivers/test_cli.py`
  - `tests/drivers/test_mcp_server.py` if it has an org-plan routing predicate test

### RED

Add tests:

- `ORG_OBJECT_TYPES` includes `"wlantemplate"` and `SUPPORTED_OBJECT_TYPES` does
  not.
- Org-scope `wlantemplate` delete with empty payload passes `check_objects`.
- Org-scope `wlantemplate` delete with non-empty payload is rejected by
  `object_gate`.
- Site-scoped `wlantemplate` is rejected as unsupported single-site fan-out.
- A plan mixing `wlantemplate` and first-class org `wlan` rejects before overlay
  application. Assert the rejection mentions both object types or otherwise
  clearly names the unsupported mix.
- A plan mixing `wlantemplate` with layer org templates such as `networktemplate`
  still passes object gate.
- `_is_org_plan` returns true for no-site `wlantemplate`; site-id
  `wlantemplate` stays false.

Do not add a test that expects `object_gate` to reject `wlantemplate` update.
The current org action gate allows `update` for every org object type, and the
spec requires the `simulate_org_plan` branch to reject non-delete actions
deliberately. That assertion belongs in Task 5.

### GREEN

- Add `"wlantemplate"` to `ORG_OBJECT_TYPES`.
- Add a cross-op guard, preferably in `object_gate`, that rejects plans
  containing both `"wlantemplate"` and `"wlan"`.
- Refresh comments/docstrings in `allowlist.py`, `object_gate.py`, CLI, and MCP
  that describe org fan-out as template/WLAN-template aware.

### Verify

```bash
uv run pytest tests/scope/test_allowlist.py tests/scope/test_object_gate.py tests/drivers/test_cli.py tests/drivers/test_mcp_server.py -q
uv run mypy src
```

### Commit

`feat(wlantemplate): route delete-only template fan-out`

## Task 2 - Provider contract and resolvers

### Files

- Modify `src/digital_twin/providers/base.py`
- Modify `src/digital_twin/providers/__init__.py`
- Modify `src/digital_twin/providers/mist_api.py`
- Modify `src/digital_twin/drivers/cli.py`
- Modify tests:
  - `tests/providers/test_base.py`
  - `tests/providers/test_mist_api.py`
  - `tests/test_public_api.py`

### RED

Add tests:

- `OrgWlanTemplateContext` carries:

```python
template: JsonObj
derived_rows_by_site: Mapping[str, tuple[JsonObj, ...]]
```

- `StateProvider` protocol requires `resolve_org_wlan_template(...)`.
- `_RecordingProvider` delegates `resolve_org_wlan_template(...)`.
- `MistApiProvider.resolve_org_wlan_template(OrgScope("o1"), "tmpl1")`:
  - fetches the org template snapshot;
  - lists org sites;
  - probes each site's derived WLAN rows;
  - returns every row whose `template_id == "tmpl1"`, grouped by site;
  - returns multiple rows for the same site when the template produced multiple
    SSIDs;
  - excludes rows with another `template_id` and site-owned rows with no
    `template_id`.
- Missing template -> `FetchError` with object `"org_wlantemplate"`.
- Any membership probe failure -> `FetchError` with object
  `"org_wlantemplate_membership"`.

Use the existing offline `FakeProvider` subclass in `tests/providers/test_mist_api.py`.
Extend it with:

```python
org_wlantemplates: dict[str, dict[str, Any]]

def _org_wlan_template(self, scope: OrgScope, template_id: str) -> dict[str, Any]:
    return self._org_wlantemplates[template_id]
```

### GREEN

- Add `OrgWlanTemplateContext` in `providers/base.py` after `OrgWlanContext`.
- Add `resolve_org_wlan_template` to `StateProvider`.
- Export `OrgWlanTemplateContext` in `providers/__init__.py` and
  `tests/test_public_api.py`.
- Add `_RecordingProvider.resolve_org_wlan_template` in the CLI driver.
- In `MistApiProvider`, add:

```python
def _org_wlan_template(self, scope: OrgScope, template_id: str) -> _Json:
    resp = mistapi.api.v1.orgs.templates.getOrgTemplate(
        self._session, scope.org_id, template_id
    )
    return dict(resp.data)
```

- Implement `resolve_org_wlan_template`:
  - fetch `_org_wlan_template`;
  - fetch `_org_sites`;
  - call `_wlans(SiteScope(scope.org_id, site_id))` for every org site;
  - collect matching rows as sorted tuples per site;
  - catch template lookup errors separately from membership-probe errors.

### Verify

```bash
uv run pytest tests/providers/test_base.py tests/providers/test_mist_api.py tests/test_public_api.py tests/drivers/test_cli.py -q
uv run mypy src
```

### Commit

`feat(wlantemplate): resolve affected derived wlan rows`

## Task 3 - Replay fixture provider support

### Files

- Modify `src/digital_twin/observability/replay/store.py`
- Modify `tests/observability/test_replay_store.py`

### RED

Add tests using the typed multi-site fixture shape:

```json
{
  "templates": {
    "wlantemplate": {
      "tmpl1": {"id": "tmpl1", "name": "Guest template"}
    }
  },
  "sites": {
    "siteA": {"wlans": [{"id": "w1", "template_id": "tmpl1"}]},
    "siteB": {"wlans": [{"id": "w2", "template_id": "tmpl1"}]}
  }
}
```

Tests:

- `resolve_org_wlan_template` returns `OrgWlanTemplateContext` with the template
  body and matching rows grouped by site.
- One site with two matching rows returns both rows, deterministically ordered by
  row id.
- Rows from a different `template_id`, site-owned rows with no `template_id`, and
  rows missing `id` are excluded from the membership set.
- Missing `wlantemplate` -> `FetchError`.
- Single-site fixtures do not support `resolve_org_wlan_template` -> `FetchError`.
- Wrong org -> `FetchError`.
- A site marked in `fetch_failures` still appears in
  `derived_rows_by_site` if its fixture doc has a matching WLAN row; the later
  `fetch_sites` result remains a per-site `FetchError`. This preserves the SP2
  "membership before later fetch failure" behavior.

### GREEN

- Reuse `self._templates["wlantemplate"]` from the existing typed template map.
- Add `FixtureProvider.resolve_org_wlan_template(...)`:
  - require multi-site fixture;
  - enforce strict org matching;
  - require the template exists;
  - scan `self._site_docs[sid]["wlans"]`;
  - collect rows whose `template_id` equals the requested template id;
  - return sorted row tuples.

### Verify

```bash
uv run pytest tests/observability/test_replay_store.py -q
uv run mypy src
```

### Commit

`feat(wlantemplate): replay template-derived wlan membership`

## Task 4 - Template-row overlays

### Files

- Modify `src/digital_twin/engine/org_overlay.py`
- Modify `tests/engine/test_org_overlay.py`

### RED

Add tests:

- `OrgOverlay(object_type="wlantemplate", ...)` requires
  `assigned_site_ids == frozenset(wlan_template_rows_by_site)`.
- Baseline pinning uses resolver-captured rows even when the later fetched raw
  rows differ.
- Proposed delete removes every captured row id.
- Proposed delete also removes any fetched row whose `template_id` equals the
  deleted template id, even when that row id was not in the captured row set.
  This is the phantom-survivor regression.
- Other WLAN rows remain untouched.
- Multiple captured rows for one site are removed together.
- A `wlantemplate` overlay leaves `raw.networktemplate` unchanged and mutates
  only `raw.wlans`. This pins the explicit dispatch requirement and catches the
  existing `_pin` fallback trap.
- A `wlantemplate` overlay composes with a normal `networktemplate` overlay on
  the same site.

### GREEN

- Add a defaulted field to `OrgOverlay`:

```python
wlan_template_rows_by_site: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
```

- Extend `__post_init__`:
  - existing `object_type == "wlan"` validation stays unchanged;
  - new `object_type == "wlantemplate"` validation requires row-map keys to
    equal `assigned_site_ids`.
- Add a helper such as:

```python
def _pin_wlan_template(
    raw: RawSiteState,
    template_id: str,
    rows: tuple[Mapping[str, Any], ...] | None,
) -> RawSiteState:
```

Behavior:

- compute captured row ids from `rows`;
- filter `raw.wlans` where `row.template_id == template_id` or `row.id` is in
  captured ids;
- if `rows is not None`, append copied rows sorted by `id`;
- if `rows is None`, return the filtered rows only.

- In `apply_overlays`, add an explicit `elif o.object_type == "wlantemplate"`
  branch before the layer-template fallback:
  - baseline calls `_pin_wlan_template(..., rows=template rows)`;
  - proposed delete calls `_pin_wlan_template(..., rows=None)`.

### Verify

```bash
uv run pytest tests/engine/test_org_overlay.py -q
uv run mypy src
```

### Commit

`feat(wlantemplate): overlay template-derived wlan rows`

## Task 5 - Pipeline branch and org e2e behavior

### Files

- Modify `src/digital_twin/engine/pipeline.py`
- Modify `tests/engine/test_org_plan.py`
- Modify `tests/drivers/test_cli.py` if `_is_org_plan` needs a direct routing pin

### RED

Extend `tests/engine/test_org_plan.py`'s `FakeProvider` with:

```python
wlan_templates: dict[str, dict[str, Any]]
wlan_template_membership: dict[str, dict[str, tuple[dict[str, Any], ...]]]

def resolve_org_wlan_template(...) -> OrgWlanTemplateContext | FetchError:
    ...
```

Add e2e tests:

- Delete template with one derived WLAN row, active wireless client on that SSID,
  and no survivor -> org UNSAFE; per-site finding code
  `wireless.wlan.client_impact.coverage_lost`; config diff object type
  `"wlantemplate"`, action `"delete"`.
- Delete template with two derived rows on one site -> both rows are removed and
  clients on either SSID are evaluated. Assert both removed SSIDs are reflected
  in findings or impact evidence.
- Multi-site mixed result: site A has a provable same-SSID survivor not from the
  deleted template; site B has no survivor. Assert org UNSAFE,
  `driving_sites == ("s2",)`, site A SAFE, site B UNSAFE.
- Zero affected sites -> org SAFE, no per-site entries, config diff still names
  the deleted template, and decision reasons include "no assigned sites" or the
  existing equivalent no-ripple reason.
- Missing template -> org UNKNOWN and no config diff.
- Membership-probe `FetchError` from provider -> org UNKNOWN and no config diff.
  The provider returns one `FetchError` value for the whole resolution, so the
  pipeline has no usable `OrgWlanTemplateContext` and must not synthesize a diff
  from partial internal state.
- Unsupported `wlantemplate` update -> org UNKNOWN with a deliberate rejection
  reason from the pipeline branch. Assert it does not call
  `resolve_org_template`; the fake can raise if that path is touched.
- Mixed `wlantemplate` + org `wlan` plan -> org UNKNOWN before overlays.
- Missing wireless-client telemetry on a site with removed SSID -> REVIEW (or
  coverage-gap UNKNOWN if another gate also fires), never SAFE.

### GREEN

- Import `OrgWlanTemplateContext`.
- In `simulate_org_plan`, add an `elif op.object_type == "wlantemplate"` branch
  before the generic layer-template branch.
- First line of the branch:
  - if `op.action != "delete"`, return `org_unknown` with a clear rejection such
    as stage `"scope.pre"` and reason `"wlantemplate supports delete only in SP3"`.
  - This is required because `object_gate` allows org updates generically.
- Resolve with `provider.resolve_org_wlan_template(org_scope, op.object_id)`.
- On fetch error, return UNKNOWN with a `"org-wlantemplate lookup failed"` reason.
- Build:
  - `snapshot = dict(ctx.template)`;
  - `name = snapshot.get("name")`;
  - `changes[i] = OrgChange(ObjectRef("wlantemplate", op.object_id, name), "delete")`;
  - `object_config_diff(... before=snapshot, after=None)`;
  - `OrgOverlay(object_type="wlantemplate", object_id=op.object_id,
     baseline=snapshot, proposed=None, assigned_site_ids=frozenset(rows_by_site),
     wlan_template_rows_by_site=rows_by_site)`.
- Let existing affected-site fetch, overlay application, `_simulate_site_state`,
  and `decide_org` run unchanged.

### Verify

```bash
uv run pytest tests/engine/test_org_plan.py tests/drivers/test_cli.py -q
uv run mypy src
```

### Commit

`feat(wlantemplate): simulate template delete coverage loss`

## Task 6 - Config-diff redaction, roadmap, and full gate

### Files

- Modify tests:
  - `tests/test_config_diff.py` or `tests/drivers/test_render_config_diff.py`
- Modify `docs/ROADMAP.md`

### RED

Add a template-body config diff redaction test:

```python
d = object_config_diff(
    object_type="wlantemplate",
    object_id="tmpl1",
    name="Guest template",
    action="delete",
    before={
        "id": "tmpl1",
        "name": "Guest template",
        "additional": {"portal_psk": "SUPERSECRET"},
        "vendorBlob": {"api_token": "eyJhbGciOiJI.eyJzdWIiOiIxMjMifQ.signature"},
    },
    after=None,
)
```

Assert:

- no raw secret value appears in `repr(d)` or the rendered/dict output;
- the sensitive leaf is `REDACTED` or tokenized by the existing entropy/JWT
  backstop.

This pins the open-ended `Template.additionalProperties` security bar without
adding special-case redaction logic.

### GREEN

- Use the existing `object_config_diff` / redaction machinery. No product code
  should be needed unless the test exposes a real leak.
- Update `docs/ROADMAP.md`:
  - mark SP3 WLAN template delete coverage-loss fan-out done;
  - keep `wlantemplate` update / assignment / additionalProperties semantics
    deferred.

### Full verification

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src
```

Then inspect:

```bash
git status --short
```

### Commit

`test(wlantemplate): pin redaction and roadmap`

## Execution notes

- Keep this slice delete-only. Do not add `RAW_ALLOWLIST["wlantemplate"]` for
  update fields in this plan.
- Do not add a new check or change public check counts.
- Keep row/order determinism: site ids sorted, row tuples sorted by row id,
  config diffs in op order.
- Every UNKNOWN path after a successful template lookup should still carry the
  computable config diff; pre-lookup UNKNOWN paths cannot.
- If a live org has no WLAN templates, live verification is regression-only:
  the synthetic fixtures prove the feature behavior, and live can only prove
  route/provider shape.
