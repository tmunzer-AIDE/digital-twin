# SP2 — Org WLAN coverage-loss fan-out

**Status:** PROPOSED
**Date:** 2026-06-28
**Author:** brainstormed with the repo owner

Second of the WLAN simulation slices. SP1 made a **site-owned** WLAN delete /
disable / rename / scope shrink flow through the single-site engine and report
`wireless.wlan.client_impact` when active wireless clients lose provable SSID
coverage. SP2 lifts the same impact model to **org WLAN** objects
(`/api/v1/orgs/{org_id}/wlans/{wlan_id}`), fanning a single org WLAN change out
to every currently affected site and reusing the existing per-site engine.

SP3 remains separate: WLAN template/container changes (`/orgs/{org_id}/templates`
and related template-level assignment semantics). SP2 is about first-class org
WLAN rows, not the higher-level template object that may produce them.

## Problem

- A site-local edit against an inherited WLAN is correctly rejected by SP1:
  inherited rows are not site-writable. The user is told to simulate the change
  at the org/template level.
- The org path currently supports only layer-like org objects:
  `networktemplate`, `gatewaytemplate`, and `sitetemplate`. It cannot target an
  org WLAN row even though the local Mist SDK exposes concrete org WLAN endpoints:
  `listOrgWlans`, `getOrgWLAN`, `updateOrgWlan`, and `deleteOrgWlan`.
- The IR already ingests derived per-site WLAN rows (`RawSiteState.wlans` from
  `listSiteWlansDerived`) and SP1's `wireless.wlan.client_impact` already knows
  how to decide client impact once the baseline/proposed per-site WLAN lists are
  correct. Missing piece: org fan-out must rewrite `RawSiteState.wlans` for each
  affected site.

## Goals

- Accept org-scope ChangePlans (`scope.site_id` absent) targeting
  `object_type == "wlan"` with `update` or `delete`.
- Resolve the baseline org WLAN snapshot with the org endpoint and compute the
  sites where that WLAN is currently effective.
- For each affected site, build baseline/proposed raw states by pinning the
  site's own derived WLAN row into `RawSiteState.wlans`:
  - baseline: the resolver-captured per-site derived row is present;
  - update: the org payload is applied to that per-site derived row;
  - delete: the row is absent.
- Run the existing `_simulate_site_state` path once per affected site, so
  `WlanIngester`, `diff_ir`, and `wireless.wlan.client_impact` do the actual
  per-site impact work.
- Keep the existing org verdict surface: `OrgVerdict.changes`, `per_site`,
  `driving_sites`, `site_failures`, `config_diffs`, and roll-up decision
  precedence.

## Non-goals

- **WLAN template/container simulation** — SP3. SP2 does not model
  `/orgs/{org_id}/templates` or any template object that indirectly produces
  WLAN rows.
- **Org WLAN assignment mutations** (`site_ids`, `sitegroup_ids`, or equivalent
  future assignment fields) — denied in SP2. Changing which sites receive an org
  WLAN can absolutely remove service, but it requires proposed-assignment fan-out
  (`baseline_assigned ∪ proposed_assigned`) and sitegroup expansion semantics.
  Until that is built deliberately, assignment-field edits must resolve UNKNOWN,
  never SAFE.
- **Create** — adding a new org WLAN is not a coverage-loss event and needs its
  own proposed-assignment semantics. SP2 supports update/delete only.
- **Auth / credential compatibility** — same boundary as SP1. Auth-type changes
  or PSK rotation can disrupt clients without removing SSID coverage; they remain
  a deferred wireless-auth safety check.
- **AP health / RF / roaming capacity** — SP2 still answers only whether the SSID
  remains provably configured on the client's AP.

## Core model

### `OrgWlanContext`

Add a provider result type alongside `OrgTemplateContext`:

```python
@dataclass(frozen=True)
class OrgWlanContext:
    wlan: JsonObj
    derived_rows_by_site: Mapping[str, JsonObj]
```

`wlan` is the resolved baseline org WLAN snapshot from `/orgs/{org}/wlans/{id}`.
`derived_rows_by_site` is the baseline effective WLAN row from each affected
site's `listSiteWlansDerived` result, keyed by site id. The baseline assigned
site set is `sorted(derived_rows_by_site)`.

### Canonical assignment source

For SP2, `assigned_site_ids` is based on **baseline derived WLAN membership**:
a site is assigned if its derived WLAN list contains a row with the org WLAN id.

This is intentionally more conservative than trusting undocumented assignment
fields on the org WLAN object. The derived list is the exact data the IR ingests
today; if the provider cannot determine derived membership, it must return a
`FetchError` and the org verdict is UNKNOWN.

The **derived row is authoritative for per-site baseline coverage**. Do not pin
the org-level snapshot into a site's baseline raw WLAN list. If an org WLAN's
effective leaves ever differ per site (`enabled`, `ssid`, `apply_to`, `ap_ids`,
`wxtag_ids`, etc.), replacing the derived row with the org snapshot could hide a
real coverage loss. Example: if the derived row is enabled and serving clients
but the org snapshot is disabled or stale, pinning the snapshot would make
`wireless.wlan.client_impact` skip the deleted SSID and could resolve SAFE. SP2
therefore carries the per-site derived rows through the overlay.

Consequences:
- deleting or disabling an org WLAN fans out only to sites where that WLAN is
  actually effective today;
- a zero-site org WLAN delete/update is SAFE with an auditable `changes` /
  `config_diffs` entry and a "no affected sites" reason;
- assignment-field edits are not supported by this slice, so SP2 never needs to
  compute proposed assigned sites.

### Org overlay for WLAN rows

Extend `OrgOverlay` so `object_type == "wlan"` means "overlay these per-site
derived rows into `RawSiteState.wlans`", not "pin a template layer".

Add optional WLAN-only payloads to the overlay:

```python
wlan_baseline_by_site: Mapping[str, JsonObj] = field(default_factory=dict)
wlan_proposed_by_site: Mapping[str, JsonObj | None] = field(default_factory=dict)
```

For `object_type == "wlan"`, the existing `OrgOverlay.baseline` / `proposed`
slots hold the org-level baseline/proposed snapshots for audit symmetry only;
`apply_overlays` ignores those slots and uses the per-site WLAN maps. The key
invariant is:

```python
assigned_site_ids == frozenset(wlan_baseline_by_site)
```

and, for updates/deletes, `wlan_proposed_by_site` must carry the same keys.

For a site in `overlay.assigned_site_ids`, `apply_overlays` uses those maps:

| phase | `proposed` value | raw-state effect |
|---|---|---|
| baseline | `wlan_baseline_by_site[site_id]` | upsert that derived row by `id` into `raw.wlans` |
| update | `wlan_proposed_by_site[site_id]` | upsert that proposed derived row by `id` into `raw.wlans` |
| delete | `None` in `wlan_proposed_by_site[site_id]` | remove row by `id` from `raw.wlans` |

"Upsert" is deliberate. The fetched derived list normally contains the row
for an assigned site, but pinning the resolver-captured derived row prevents a
fetch-time race between org-object resolution and per-site fetch from changing
the diff. Other WLAN rows in the site's derived list remain untouched.

For an **update**, the per-site proposed row is:

```python
effective_update(site_derived_baseline_row, op.payload)
```

not `effective_update(org_snapshot, op.payload)`. This preserves the site's
actual effective baseline for untouched roots and applies only the org WLAN
change being simulated. If a higher-precedence override would mask that org
change in reality, this may overstate impact, but it stays safe-side; it cannot
hide an actual coverage loss by erasing site-derived baseline facts.

For sites not in the overlay's baseline `assigned_site_ids`, no WLAN row is
pinned. SP2 does not add the WLAN to newly assigned sites because assignment
mutations are out of scope.

## Scope and gates

### Object gate

`object_gate` learns an org-scope WLAN mode:

- ORG mode is true when every op has no `site_id` and each `object_type` is one
  of the existing org template types or `wlan`.
- `ORG_OBJECT_TYPES` gains `"wlan"` while `SUPPORTED_OBJECT_TYPES` keeps
  `"wlan"`. The disambiguator is `scope.site_id`: with a site id, `wlan` stays
  SP1 site mode; without a site id, `wlan` routes to SP2 org mode.
- Org WLAN actions allowed: `update`, `delete`.
- A delete with a non-empty payload is rejected before fetch, matching org
  template and site WLAN delete semantics.
- Existing site behavior stays intact: with `scope.site_id` present, `wlan`
  remains a site object; site WLAN delete stays SP1; inherited site rows stay
  rejected by the post-fetch ownership gate.

Mixed org plans are allowed when they target distinct objects, e.g. a
`networktemplate` update plus an org `wlan` delete. The per-site simulation must
apply all overlays atomically, as the current org-template path does.

### L0 and raw field gate

Org WLAN updates use the existing `wlan.schema.json` L0 surface and the existing
WLAN modeled leaves:

```text
ssid
enabled
auth.type
isolation
l2_isolation
apply_to
ap_ids
wxtag_ids
```

However, the site WLAN field gate cannot be reused as-is: it intentionally
rejects inherited rows via `wlan_is_inherited(current)`. Org WLAN updates must
reuse the leaf allowlist while **bypassing the site-writable ownership check**.

Implementation shape:
- keep `RAW_ALLOWLIST["wlan"]` as the site WLAN allowlist;
- parameterize `screen_op` as
  `screen_op(object_type, current, payload, *, enforce_wlan_site_ownership=True)`;
  the site path keeps the default, while the org WLAN path calls it with
  `enforce_wlan_site_ownership=False`;
- keep assignment fields (`site_ids`, `sitegroup_ids`, etc.) out of that
  allowlist. If a payload changes them, the org field gate rejects the op as
  UNKNOWN.

This avoids a false-SAFE where a site-scoped WLAN op could mutate assignment
metadata that no check consumes.

### Delete

Org WLAN delete has no proposed object:

- it skips proposed-object L0 and the raw field gate;
- it still requires successful baseline resolution so `config_diffs` and
  `changes` are auditable;
- proposed per-site raw state removes the row from `RawSiteState.wlans`.

## Provider API

Extend `StateProvider` with:

```python
def resolve_org_wlan(
    self, scope: OrgScope, wlan_id: str
) -> OrgWlanContext | FetchError: ...
```

`MistApiProvider.resolve_org_wlan`:

1. Fetches the baseline org WLAN via `mistapi.api.v1.orgs.wlans.getOrgWLAN`.
2. Lists org sites via `_org_sites`.
3. For each site id, fetches that site's derived WLAN list with the same
   `listSiteWlansDerived` endpoint used by `_wlans`.
4. A site counts only if a derived row with `id == wlan_id` is present.
5. If any site membership probe fails, returns `FetchError` instead of assuming
   "not assigned".
6. Returns `OrgWlanContext(wlan=snapshot, derived_rows_by_site={...})`.

A missing org WLAN, a failure to list candidate sites, or a failure that prevents
derived membership from being determined is a `FetchError` and yields org-level
UNKNOWN before fan-out. Individual per-site fetch failures after the affected
set is known remain per-site failures, exactly as in the template org path.

Replay/fixture providers gain the same method. Multi-site fixtures can model org
WLANs with an `org_wlans` map plus per-site derived `wlans`; assignment is still
inferred from the per-site rows so fixtures exercise the same contract as live.

Cost note: the conservative membership rule is an N-site derived-WLAN probe, and
`fetch_sites` then fetches affected sites again. That is acceptable for SP2. An
implementation may cache and reuse the membership-probe rows for affected sites,
but it must not replace the derived-membership rule with raw assignment fields.

## Pipeline

`simulate_org_plan` remains the single org entry point.

For each org op:

1. If `object_type == "wlan"`, call `resolve_org_wlan`; otherwise call the
   existing `resolve_org_template`.
2. Build the hydrated `OrgChange`.
3. For update:
   - compute `proposed_org = effective_update(snapshot, payload)` using the same
     Mist root-level semantics as site WLAN updates;
   - build `object_config_diff(... action="update", before=snapshot, after=proposed_org)`;
   - run L0 scoped to changed roots;
   - run org-WLAN leaf screening without the site ownership check.
   - compute `wlan_proposed_by_site` with
     `effective_update(site_row, payload)` for every `site_row` in
     `derived_rows_by_site`.
4. For delete:
   - `proposed_org = None`;
   - build `object_config_diff(... action="delete", before=snapshot, after=None)`.
5. Append an `OrgOverlay` carrying `object_type="wlan"` and the baseline
   assigned sites, plus `wlan_baseline_by_site` and `wlan_proposed_by_site`.

After all overlays are built:

- affected sites are the union of every overlay's `assigned_site_ids`;
- `apply_overlays` handles both template-layer overlays and WLAN-row overlays;
  the helper must thread `object_id` into `_pin` (or equivalent) so the WLAN
  branch can upsert/remove a row by id;
- `_simulate_site_state` runs unchanged for every affected site;
- `decide_org` rolls up unchanged.

## Verdict behavior

The per-site verdicts are exactly SP1 verdicts, just produced under an
`OrgVerdict`:

| org WLAN change | per-site outcome |
|---|---|
| delete / disable / rename / scope shrink with active clients and no provable same-SSID survivor | `UNSAFE` via `wireless.wlan.client_impact.coverage_lost` |
| client telemetry missing for an affected SSID | `REVIEW` via `wireless.wlan.client_impact.unverified` |
| zero clients or provable same-SSID survivor covering every impacted AP | `SAFE` |
| affected-site fetch fails | that site is `UNKNOWN`; other sites continue |
| no baseline affected sites | org verdict `SAFE` with reason "no affected sites — nothing ripples" |
| assignment-field edit | org verdict `UNKNOWN` at field gate |

The org roll-up precedence is unchanged: UNKNOWN site failures can dominate only
when no site is UNSAFE; UNSAFE sites drive the org verdict UNSAFE and appear in
`driving_sites`.

## Config diffs and rendering

SP2 reuses the merged config-diff surface:

- org WLAN delete: before is the resolved org WLAN, after is `None`;
- org WLAN update: before/after are the baseline/proposed org WLAN snapshots;
- redaction rules apply identically to site WLAN diffs;
- `OrgVerdict.changes` names the WLAN id/name and action.

Human and JSON renderers need no new top-level shape, but help text must stop
describing the org path as only "template" once `wlan` is accepted in org mode.

Non-fatal L0 findings from org WLAN updates use the existing
`OrgVerdict.template_findings` channel and therefore floor REVIEW just like
template-object L0 findings. The field name is historical; a rename can be a
future cosmetic cleanup, not part of SP2.

## Tests

- **Object gate**
  - org-scope `wlan` update/delete accepted;
  - org-scope `wlan` delete with non-empty payload rejected;
  - site-scope `wlan` behavior unchanged;
  - mixed org template + org WLAN plan accepted when objects are distinct.

- **Provider / fixture**
  - `resolve_org_wlan` fetches the org WLAN and returns only sites whose derived
    `wlans` contain that id, carrying each site's actual derived row;
  - missing org WLAN -> `FetchError`;
  - derived membership failure -> `FetchError` (fail closed);
  - fixture provider mirrors the same membership rule.

- **Overlay**
  - WLAN overlay upserts baseline/proposed **per-site derived** rows by id;
  - org snapshot differing from a site derived row does not overwrite the
    baseline row used by the per-site IR;
  - delete overlay removes only that row;
  - sites not in `assigned_site_ids` are untouched;
  - template overlays and WLAN overlays compose in one `apply_overlays` call.

- **Field gate**
  - org WLAN update to a modeled leaf (`enabled`, `ssid`, `apply_to`, `ap_ids`)
    passes even though the row is inherited/not site-owned;
  - site WLAN update against the same inherited row still rejects;
  - org WLAN assignment-field edit (`site_ids` / `sitegroup_ids`) rejects UNKNOWN.

- **Pipeline end to end**
  - org WLAN delete with one active client on an affected site -> per-site
    UNSAFE, org UNSAFE, `coverage_lost`, config diff present;
  - org WLAN disable/rename/scope-shrink variants produce the same SP1 findings;
  - same-SSID site-scope survivor on one site makes that site SAFE while another
    site without a survivor is UNSAFE;
  - missing client telemetry on an affected site -> that site REVIEW;
  - zero affected sites -> org SAFE with `changes` and `config_diffs`;
  - missing org WLAN -> org UNKNOWN without fabricated config diff;
  - later-op failure preserves earlier op config diffs, matching the post-SP4
    "config diffs always surfaced" doctrine.

- **Drivers**
  - CLI/MCP org-plan detection routes no-site `wlan` ops to `simulate_org_plan`;
  - CLI/MCP keeps site-id `wlan` ops on the single-site path;
  - rendered org output names `wlan` changes like other org changes.

## Roadmap update

When implemented:

- mark SP2 "Org WLAN coverage-loss fan-out" complete;
- leave SP3 "wlantemplate / template-container WLAN changes" deferred;
- leave org WLAN assignment mutation and wireless-auth compatibility as explicit
  deferred items.
