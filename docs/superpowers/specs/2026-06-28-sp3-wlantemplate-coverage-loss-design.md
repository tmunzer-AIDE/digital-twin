# SP3 - WLAN template coverage-loss fan-out

**Status:** PROPOSED
**Date:** 2026-06-28
**Author:** brainstormed with the repo owner

Third WLAN simulation slice. SP1 made site-owned WLAN delete / disable / rename
/ scope shrink flow through the single-site engine. SP2 lifted first-class org
WLAN rows (`/api/v1/orgs/{org_id}/wlans/{wlan_id}`) to org fan-out. SP3 covers
Mist WLAN template/container deletes: the generic org `Template` endpoint at
`/api/v1/orgs/{org_id}/templates/{template_id}` when its derived site WLAN rows
would disappear.

This spec uses the twin object type `wlantemplate` for that Mist API object. The
name is deliberate: the Juniper endpoint and model are generic `Template`, while
the twin already has `networktemplate`, `gatewaytemplate`, and `sitetemplate`.
`wlantemplate` avoids overloading those existing layer templates.

## Grounded facts

- Juniper's Create/Update/Delete Org Template docs use
  `POST|PUT|DELETE /api/v1/orgs/{org_id}/templates/{template_id}` and the
  response/body model is `Template`.
- The `Template` model has fixed assignment/filtering fields (`applies`,
  `exceptions`, `deviceprofile_ids`, `filter_by_deviceprofile`, `name`, id/audit
  metadata) and explicitly accepts arbitrary `additionalProperties`.
- The Mist SDK exposes these endpoints under `mistapi.api.v1.orgs.templates`
  (`listOrgTemplates`, `getOrgTemplate`, `updateOrgTemplate`,
  `deleteOrgTemplate`, etc.).
- Site WLAN derivation is already fetched from `listSiteWlansDerived` into
  `RawSiteState.wlans`. Existing inherited rows carry `template_id`, and the IR
  already treats those rows as the source of truth for WLAN coverage.

References:
- https://www.juniper.net/documentation/us/en/software/mist/api/llms-pages/http/api/orgs/wlan-templates/create-org-template.md
- https://www.juniper.net/documentation/us/en/software/mist/api/llms-pages/http/api/orgs/wlan-templates/update-org-template.md
- https://www.juniper.net/documentation/us/en/software/mist/api/llms-pages/http/models/structures/template.md
- https://www.juniper.net/documentation/us/en/software/mist/api/llms-pages/http/api/orgs/wlan-templates/delete-org-template.md

## Problem

Deleting a Mist WLAN template can remove one or more derived WLAN rows from every
site where that template currently contributes SSIDs. SP1 can detect the client
impact once a site raw state loses those rows, and SP2 already knows how to fan
out an org WLAN row change to affected sites. The missing piece is resolving a
template delete into the concrete per-site WLAN rows that disappear.

The dangerous false-SAFE shape is the same doctrine SP2 closed: do not infer
coverage from the org object snapshot when the per-site derived rows are what
actually serve clients. For templates this is even more important because the
model is open-ended. The twin must use derived WLAN rows, not template body
interpretation, for baseline coverage and proposed removal.

## Goals

- Accept org-scope ChangePlans (`scope.site_id` absent) with
  `object_type == "wlantemplate"` and `action == "delete"`.
- Resolve the baseline org template snapshot for audit/config-diff purposes.
- Determine affected sites from baseline derived WLAN membership:
  every site whose `listSiteWlansDerived` result contains one or more rows with
  `template_id == template_id`.
- For each affected site, remove all baseline derived rows produced by that
  template in the proposed raw state and run the existing `_simulate_site_state`
  path.
- Reuse `wireless.wlan.client_impact` unchanged. The check decides whether
  active clients lose SSID coverage, whether a same-SSID survivor covers the
  client's AP, and whether missing wireless-client telemetry floors REVIEW.
- Preserve the org verdict surface: `OrgVerdict.changes`, `per_site`,
  `driving_sites`, `site_failures`, `config_diffs`, and the post-#27 roll-up
  precedence where UNSAFE outranks coverage-gap UNKNOWN.

## Non-goals

- **Template update semantics.** The Mist `Template` model is open-ended. An
  update may mutate assignment/filtering fields or arbitrary additional
  properties whose effect on derived WLAN rows is not modeled. SP3 v1 rejects
  `wlantemplate` updates as UNKNOWN, not SAFE. A future SP3b may add a grounded
  update slice once real template payloads and derivation semantics are known.
- **Create.** Creating a template does not remove coverage, and proposed
  assignment/sitegroup semantics are out of scope.
- **Assignment mutations.** Changes to `applies`, `exceptions`,
  `deviceprofile_ids`, or `filter_by_deviceprofile` are not simulated. They can
  remove or add sites and require baseline union proposed assignment expansion.
  SP3 v1 handles only delete of the currently effective baseline rows.
- **Interpreting `additionalProperties`.** Do not treat arbitrary keys in the
  template body as WLAN definitions. Unknown template payload structure must
  remain UNKNOWN until grounded.
- **Auth / credential compatibility.** Same boundary as SP1/SP2: auth-type
  changes or PSK rotation can disrupt clients while preserving SSID coverage;
  that remains a deferred wireless-auth safety check.
- **Sitegroup/device-profile expansion.** The template docs expose sitegroup and
  device-profile filters, but v1 derives membership from site WLAN rows rather
  than trying to simulate those filters.

## Core model

### Object type

Add `wlantemplate` to the org fan-out object types. It is not a site object and
does not enter `SUPPORTED_OBJECT_TYPES`.

Allowed actions for `wlantemplate` in v1:

| action | result |
|---|---|
| `delete` with empty payload | supported |
| `delete` with non-empty payload | object-gate UNKNOWN |
| `update` | UNKNOWN, unsupported for SP3 v1 |
| `create` | UNKNOWN, unsupported |

This may be expressed either in `object_gate` or in the early
`simulate_org_plan` branch, but it must happen before any path can return SAFE.
One detail is load-bearing for the current codebase: the generic org object gate
allows `update` for every `ORG_OBJECT_TYPES` member. If `wlantemplate` joins
that tuple, `update` can pass `object_gate`. The `wlantemplate` branch in
`simulate_org_plan` must therefore reject every non-`delete` action explicitly
before resolving or falling through to the layer-template path. A
`wlantemplate` update must return a deliberate UNKNOWN, not accidentally call
`resolve_org_template` with an object type it does not own.

### `OrgWlanTemplateContext`

Add a provider result alongside `OrgTemplateContext` and `OrgWlanContext`:

```python
@dataclass(frozen=True)
class OrgWlanTemplateContext:
    template: JsonObj
    derived_rows_by_site: Mapping[str, tuple[JsonObj, ...]]
```

`template` is the baseline Mist `Template` snapshot from
`GET /orgs/{org_id}/templates/{template_id}`.

`derived_rows_by_site` is keyed by site id. Each value is the tuple of baseline
derived WLAN rows from that site's `listSiteWlansDerived` response where
`str(row.get("template_id") or "") == template_id`.

The baseline affected site set is exactly `sorted(derived_rows_by_site)`.
Sites with no matching rows are not affected. A zero-site template delete is
SAFE with an auditable `changes` / `config_diffs` entry and a "no affected
sites" decision reason.

### Canonical assignment source

SP3 uses **baseline derived WLAN membership** as the only assignment source. It
does not use `Template.applies`, `Template.exceptions`, `deviceprofile_ids`,
`filter_by_deviceprofile`, or sitegroup expansion to decide affected sites.

Reason: `listSiteWlansDerived` is the exact data ingested into the IR and the
exact fact that answers "which SSIDs are configured on this site today?" If the
provider cannot determine derived membership for all org sites, it returns a
`FetchError` and the org verdict is UNKNOWN. Never assume a site is unaffected
because its membership probe failed.

### Multi-row overlay

A single WLAN template can produce multiple WLAN rows per site. SP3 must not
pretend the template delete is a single org WLAN delete.

Extend the overlay model with template-WLAN row maps:

```python
wlan_template_rows_by_site: Mapping[str, tuple[JsonObj, ...]] = field(default_factory=dict)
```

For `object_type == "wlantemplate"`:

- `baseline` / `proposed` hold the template snapshot / `None` for audit and
  config-diff symmetry.
- `assigned_site_ids == frozenset(wlan_template_rows_by_site)`.
- `apply_overlays` ignores the layer-template `_pin` path and the SP2 single
  WLAN `_pin_wlan` path. It uses a new template-row pinning path.

### Template-row pinning

For an affected site, baseline and proposed raw states are normalized for the
template id before row insertion/removal:

- Baseline: remove any fetched `raw.wlans` row whose `template_id` equals the
  template id or whose `id` is one of the resolver-captured row ids, then append
  the resolver-captured rows.
- Proposed delete: remove any fetched `raw.wlans` row whose `template_id` equals
  the template id or whose `id` is one of the resolver-captured row ids.

This is stricter than "remove by captured id only" on purpose. It prevents a
fetch-time race from leaving a newly fetched same-template row as a phantom
survivor after the template is deleted. Other WLAN rows remain untouched.

For sites not in `assigned_site_ids`, no template-row pinning occurs.

### Mixed org plans

`wlantemplate` deletes may be combined with unrelated org template deletes in
one plan. The existing overlay union and per-site roll-up still apply.

For v1, reject a plan that mixes `object_type == "wlantemplate"` with
`object_type == "wlan"`. A first-class org WLAN overlay and a template overlay
can target the same derived row id; allowing both without conflict resolution
would make proposed row presence depend on overlay order. The safe MVP boundary
is UNKNOWN until an overlap-aware conflict rule is designed.

This is new cross-op logic. The current object gate checks org-vs-site shape and
per-op actions; it does not reject particular org object type combinations. The
implementation may put this guard in `object_gate` or in `simulate_org_plan`,
but it must run before overlays are applied.

Multiple distinct `wlantemplate` deletes are allowed. A derived WLAN row has one
`template_id`, so two template deletes cannot both own the same row unless the
provider returns inconsistent data; if that happens, the row-removal operation is
idempotent and safe-side.

## Provider contract

Add `resolve_org_wlan_template(scope, template_id)` to `StateProvider`.

### Mist provider

Implementation shape:

1. Fetch the template with `mistapi.api.v1.orgs.templates.getOrgTemplate`.
   Missing / error -> `FetchError(object="org_wlantemplate")`.
2. Fetch org sites with the existing `_org_sites(scope)`.
3. For each org site id, call the existing `_wlans(SiteScope(scope.org_id,
   site_id))`.
4. Collect every derived row whose `template_id` equals the requested template
   id. If at least one row matches, add the site to `derived_rows_by_site`.
5. Any exception while probing membership -> `FetchError(
   object="org_wlantemplate_membership")`.

Membership-probe failure is whole-org UNKNOWN, not a per-site warning. Without
derived membership the simulator cannot know which sites lose coverage.

### Fixture provider

Extend multi-site fixtures to carry org WLAN templates. Prefer the existing typed
template container:

```json
{
  "templates": {
    "wlantemplate": {
      "tmpl1": {"id": "tmpl1", "name": "Guest template"}
    }
  }
}
```

Membership is still derived from each site's `wlans` rows, not from the template
body. A fixture without multi-site data, with a mismatched org id, or missing the
requested template returns `FetchError`.

## Pipeline

`simulate_org_plan` adds a branch for `op.object_type == "wlantemplate"`.

The branch is mandatory. `engine/org_overlay.py` currently has a layer-template
helper whose default branch pins unknown template-like objects as
`networktemplate`. A `wlantemplate` overlay must never reach that fallback.
`apply_overlays` must dispatch `wlantemplate` to the new template-row WLAN
pinning path explicitly.

Delete flow:

1. Resolve `OrgWlanTemplateContext`.
2. Hydrate `OrgChange(ref=ObjectRef("wlantemplate", id, name=template.name),
   action="delete")`.
3. Append an `ObjectConfigDiff` with `before=template`, `after=None`.
4. Create an `OrgOverlay` with `object_type="wlantemplate"`,
   `assigned_site_ids=frozenset(derived_rows_by_site)`,
   `baseline=template`, `proposed=None`, and the per-site derived rows.
5. Continue through existing affected-site fetch, `apply_overlays`, and
   `_simulate_site_state`.

There is no L0 or raw field gate for delete because there is no proposed object.
This matches the existing org-template delete doctrine.

Unsupported `wlantemplate` actions return UNKNOWN and still name the attempted
change in `OrgVerdict.changes`. Config diffs exist only after a successful
baseline template resolution.

## Check behavior

No new check.

SP3 depends on existing behavior:

- Removing derived WLAN rows yields `diff_ir` removed `wlan` entities.
- `wireless.wlan.client_impact` computes affected SSIDs and active wireless
  clients.
- A same-SSID survivor must provably cover the client's AP (`site`/all or
  explicit AP membership). Wxtag-only survivor coverage remains unverifiable and
  fail-closed, as in SP1.
- Missing wireless-client telemetry floors REVIEW only when coverage was removed
  for some SSID. If no coverage is removed, telemetry absence is irrelevant and
  the per-site verdict can be SAFE.

## Decision matrix

| situation | expected org result |
|---|---|
| template delete removes SSID with active clients and no provable survivor | UNSAFE |
| one site SAFE and another site UNSAFE for the same template delete | org UNSAFE, driving sites name the UNSAFE site |
| template delete removes rows but all affected clients have provable same-SSID survivors | SAFE |
| template delete removes rows but wireless-client telemetry is absent where removed SSIDs exist | REVIEW / coverage-gap UNKNOWN per existing decision precedence |
| template exists but no site has derived rows with that `template_id` | SAFE with no-affected-sites reason |
| template lookup fails | UNKNOWN |
| membership probe fails for any org site | UNKNOWN |
| `wlantemplate` update/create or non-empty delete payload | UNKNOWN |
| plan mixes `wlantemplate` and first-class org `wlan` ops | UNKNOWN |

## Tests

### Object gate / routing

- Org-scope `wlantemplate` delete with empty payload is accepted.
- Non-empty `wlantemplate` delete is rejected.
- `wlantemplate` update/create is rejected UNKNOWN.
- `wlantemplate` is org-only; a site-scoped `wlantemplate` op is rejected.
- A plan mixing `wlantemplate` and `wlan` is rejected UNKNOWN.
- `wlan` with site id still routes to SP1; `wlan` without site id still routes
  to SP2.

### Provider

- `resolve_org_wlan_template` returns the template snapshot and every matching
  derived WLAN row grouped by site.
- One site with two rows from the same template returns both rows.
- Rows from other templates and site-owned rows are excluded.
- Missing template -> `FetchError(object="org_wlantemplate")`.
- Membership probe failure -> `FetchError(object="org_wlantemplate_membership")`.
- Fixture provider mirrors the Mist-provider membership rule from site docs.

### Overlay

- Baseline pinning uses resolver-captured rows, not the later fetched rows.
- Proposed delete removes every captured row id.
- Proposed delete also removes fetched rows whose `template_id` matches the
  deleted template, closing the phantom-survivor race.
- Other WLAN rows remain untouched.
- Multiple rows per site are removed together.
- `assigned_site_ids` must equal the row-map keys.
- A `wlantemplate` overlay leaves `raw.networktemplate` untouched and mutates
  only `raw.wlans`. This pins the explicit-dispatch requirement and prevents the
  layer-template `_pin` fallback from silently mis-pinning the overlay.

### End-to-end org plan

- Delete template with active wireless client on a removed SSID -> org UNSAFE and
  finding code `wireless.wlan.client_impact.coverage_lost`.
- Delete template with two derived WLAN rows on one site -> both removed; clients
  on either SSID are evaluated.
- Delete template on two sites where site A has a provable same-SSID survivor and
  site B does not -> org UNSAFE, `driving_sites == ("siteB",)`.
- Delete template with zero affected sites -> org SAFE, `config_diffs` still
  names the deleted template.
- Delete template when affected site fetch later fails -> per-site failure and
  org UNKNOWN/UNSAFE roll-up according to existing precedence.
- Missing wireless-client telemetry on an affected site with removed SSID ->
  REVIEW / coverage-gap UNKNOWN, never SAFE.
- `config_diffs` are present on delete and on UNKNOWN after successful template
  lookup.
- Config diff rendering/redaction covers open-ended template bodies. A template
  snapshot with a secret-like value under an arbitrary additional property must
  redact that value in the displayed diff.

## Implementation notes

- `OrgOverlay.__post_init__` should validate the new `wlantemplate` row-map
  invariant independently from the SP2 single-WLAN invariant.
- Keep all tuple outputs sorted/deterministic: site ids, rows by row id, and
  config diff ordering.
- Do not add a public check or bump check counts.
- Keep `template_findings` behavior unchanged; `wlantemplate` delete contributes
  none because there is no proposed object.
- Update roadmap after implementation: SP3 done, arbitrary template update /
  assignment mutation remains deferred.
