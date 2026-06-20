# Config-lint tier (GS30–GS33)

**Status:** Implemented — live-verified 2026-06-20
**Date:** 2026-06-20
**Author:** brainstormed with the repo owner

## Problem

Every check today is a **delta** check: it compares baseline vs proposed and flags
connectivity/segmentation changes. None validate the *proposed configuration on its own*
for static misconfigurations a single state can carry — a VLAN id claimed by two networks,
two VLANs with overlapping subnets, the same SSID on overlapping APs, or an open guest WLAN
with no client isolation. These are real config smells the twin currently can't surface.

This adds a **config-lint tier**: four single-state lint checks (GS30–GS33) that *detect* a
violation by inspecting the proposed IR, but contribute to the verdict **delta-conditioned** —
exactly like `native_mismatch`/`mtu_mismatch`: a violation **this change introduced or
worsened** is a `WARNING` (→ REVIEW); a **pre-existing** violation the delta didn't touch is
an `INFO` context finding that **never floors** an unrelated change's verdict.

## Goal

Four checks, all delta-conditioned, all never-false-positive on unknowns:

- **GS30 `wired.l2.vlan_collision`** — one `vlan_id` claimed by 2+ distinct network names.
- **GS31 `wired.l3.subnet_overlap`** — two different VLANs with overlapping subnets.
- **GS32 `wireless.wlan.duplicate_ssid`** — same SSID on 2+ enabled WLANs whose AP scopes
  provably overlap.
- **GS33 `wireless.wlan.open_guest`** — an enabled WLAN with open auth and no client isolation.

Plus the supporting IR + object-pipeline work that makes WLAN changes simulable.

### Out of scope (deferred)
- wxtag AP-membership resolution (the GS20 boundary): GS32/GS33 treat wxtag-scoped overlap
  as **unverifiable** (coverage note), never a finding.
- VLAN-collision detection for collisions Mist itself forbids (this models what the *derived*
  config can express).

## Architecture — "Approach A": model in the IR, condition on the delta

The checks are vendor-neutral and read only the IR. Two facts the IR doesn't carry today
are added at ingest; everything else (subnets) already exists. Delta-conditioning comes free
from `diff_ir`.

## Components

### 1. New IR modeling

**`Wlan` IR entity** (new frozen dataclass in `ir/entities.py`) — minimal and **secret-free**
(no `portal_template_url` or any credential field; only what the lint checks read):
```
id: str            # provider WLAN id (see identity note)
ssid: str
enabled: bool
auth_type: str | None     # auth.type ("open"|"psk"|"eap"|…); None = unparsed
isolation: bool           # isolation OR l2_isolation (either client-isolation form)
apply_to: str | None      # "site" | "aps" | "wxtags" | None
ap_ids: tuple[str, ...]   # sorted+deduped; explicit AP scope
wxtag_ids: tuple[str, ...] # sorted+deduped
inherited: bool           # True = org-template-owned (NOT site-writable); see below
meta: FactMeta = CONFIG_META
```
- **Derived list = effective facts, but not all are writable.** `_wlans()` fetches the
  **derived** WLAN list (`listSiteWlansDerived`) precisely to include org-template WLANs the
  site broadcasts. That is correct for **detection** — GS32/GS33 reason about the full
  effective set. But an org-template WLAN is **not a site-writable object**, so `inherited`
  records ownership. It is computed **fail-closed**: `inherited = False` (site-writable) ONLY
  when the row is *positively* site-owned (raw `for_site == True` AND no foreign
  `template_id`); a row with `for_site == False`, a `template_id`, OR **missing/ambiguous
  ownership metadata** is `inherited = True` (not writable). A derived row whose ownership
  can't be positively confirmed must never be silently mutable. This split is load-bearing:
  detection consumes all `Wlan`s; **simulation** (a `wlan` op) is allowed only against
  site-owned (`inherited == False`) WLANs (see §2).
- **Identity**: `id` = the provider WLAN id. This is a pragmatic deviation from the IR's
  "stable logical key" doctrine (an SSID rename then diffs as a *modification*, not
  remove+add — which is what we want for delta-conditioning). Documented here.
- **Built by extending `WlanIngester`** (`ingest/wlan.py`) to mint **one `Wlan` per fetched
  WLAN row** — from `ctx.raw.wlans` directly, NOT from `ap_required_vlans()` output (which
  filters to enabled/local-bridged/vlan-tagged). GS32/GS33 need open/isolated/**disabled**
  facts even when a WLAN produces no AP-vlan requirement.
- **Scope normalization**: preserve the distinction between "site/all APs" and an *empty
  explicit* scope. `apply_to` is kept verbatim; `ap_ids`/`wxtag_ids` are the explicit lists
  (sorted+deduped). Empty `ap_ids` under `apply_to: site` does NOT mean "applies nowhere".
- Stored as `ir.wlans: tuple[Wlan, ...]`; added to `diff_ir._ENTITY_KINDS` as
  `("wlan", lambda ir: ir.wlans)`. Earns the **existing** `WLAN_CONFIG` capability.
- **`inherited` is NOT a diff-bearing lint fact.** `_changed_fields` compares every
  dataclass field except an ignored set, so an ownership-only flip would otherwise make
  `touches("wlan")` true and spuriously fire GS32/GS33 (which don't reason about ownership).
  Add `inherited` to `_IGNORED_BY_KIND["wlan"]` (the same mechanism that ignores `Device.name`)
  — it stays queryable for the apply screen but is invisible to the diff / `applies_to`.

**`Vlan.collisions` field** (GS30) — the collision today's dedup hides. In
`ingest/switch.py:_vlans`, the `seen` set keeps the first network per `vlan_id` and silently
drops later same-`vlan_id` rows. Sources are `[site_effective, *device_effective.values()]`,
so the **same** logical network name legitimately repeats. Add
`Vlan.collisions: tuple[str, ...] = ()` = the **distinct OTHER** network names (≠ the winner)
that claim this `vlan_id` (sorted+deduped). Empty = no collision (the common case). A name
repeating across effective sources must NOT register as a collision.

**No new modeling for GS31** — `Vlan.subnet` / `DhcpScope.subnet` are already CIDR strings.

### 2. WLAN as a simulable site object

For a GS32/GS33 golden (or a real agent plan) to *introduce* a WLAN violation, a `wlan`
ChangePlan op must be simulable. Today `object_type="wlan"` is rejected pre-fetch.

- **`object_gate`**: add `"wlan"` to `SUPPORTED_OBJECT_TYPES`.
- **`RAW_ALLOWLIST["wlan"]`** — modeled leaves only (an unmodeled WLAN-field edit honestly
  → UNKNOWN): `ssid`, `enabled`, `auth.type`, `isolation`, `l2_isolation`, `apply_to`,
  `ap_ids`, `wxtag_ids`. **`ap_ids`/`wxtag_ids` are whole-list (atomic) leaves, NOT
  `ap_ids.*`** — `changed_leaf_paths` treats lists atomically, so `["ap1"]→["ap2"]` surfaces
  as `ap_ids`; an `ap_ids.*` entry would not match and would wrongly reject the modeled
  scope change.
- **`apply.get_object` / `replace_object`**: extend to target `raw.wlans` by provider WLAN
  id (same shape as the `device` tuple-by-id path). `replace_object()` must branch
  **explicitly** on `device` vs `wlan` — today the non-`site_setting` fallback is effectively
  "device", so the WLAN branch must not fall through. The op payload merges via the existing
  root-level `effective_update`; the rolling update re-ingests and re-mints the `Wlan`.
- **Inherited-WLAN rejection (honest, never misleading)**: a `wlan` op whose target id is an
  **inherited** (org-template-owned) WLAN is rejected → UNKNOWN with a clear reason
  ("WLAN `<id>` is inherited from an org wlantemplate; simulate the change at the org/template
  level, not the site"). Only site-owned (`inherited == False`) WLANs are writable here.
  Ownership is only known **post-fetch**, so this rejection lives at the post-fetch screen
  (the same stage as the existing device-role "must be a switch" check in `screen_op`), NOT
  the pre-fetch `object_gate` — and it is NEVER a silent pretend-update of an inherited row.
  (Org-level WLAN-template simulation is a separate object type, out of scope for this tier.)
- **L0 validation (required — else every `wlan` op fatals before apply).** The pipeline
  L0-validates each effective object *before* `screen_op`/apply, and L0 treats an object type
  with no registered schema as **fatal** (there's even a test pinning `validate_payload("wlan",
  {})` as fatal). So `"wlan"` must get an L0 route: **commit a `site_wlan` OAS schema** and
  register it in `_SCHEMA_FILES["wlan"]`, sourced from the Mist `wlan` object OAS — exactly how
  `gatewaytemplate`/`sitetemplate` were added (fail-closed: a supported type with no schema is a
  registration error). Because L0 is **scoped to changed roots by default** (`l0_full_object=
  False`), a partial WLAN op like `{"isolation": false}` validates only the touched root, so the
  full schema is fine and not unwieldy. If the upstream OAS has no cleanly-usable site-level WLAN
  object, the fallback is a **thin permissive schema** covering just the modeled leaves
  (`ssid`/`enabled`/`auth`/`isolation`/`l2_isolation`/`apply_to`/`ap_ids`/`wxtag_ids`,
  `additionalProperties` permissive) — same precedent as the thin `site_template` schema. The
  existing `test_validate_l0` case pinning `wlan` as fatal flips to "validates".
- **Field gate** rides the new allowlist automatically.

GS30/GS31 ride existing `site_setting` ops: `networks.*.vlan_id` (GS30) and
`networks.*.subnet` (GS31) are both already in `RAW_ALLOWLIST["site_setting"]`
(`scope/allowlist.py`), so their introducing edits are already simulable.

### 3. The delta-conditioned lint shape

All four checks share one shape (one small helper computes `introduced = proposed_keys −
baseline_keys`); each supplies its own **violation key** and finding builder:

- Compute a keyed violation **set** on `ctx.baseline.ir` and `ctx.proposed.ir`.
- `introduced` (key in proposed, not baseline) → **`WARNING`** finding.
- `pre-existing` (key in both) → **`INFO`** context finding (never floors the verdict).
- `Status` = WARN if any introduced else PASS; `confidence` HIGH (config facts).

**Violation keys carry the violation FACTS, not just the subject** (so a changed violation
reads as introduced, not pre-existing):
- **GS30**: `(vlan_id, frozenset({winner_name, *collisions}))` — `corp+guest`→`corp+iot` is
  *altered* (new key), not INFO. `applies_to` touches `vlan`; `requires` WIRED_L2.
- **GS31**: `frozenset({(vid_a, canon_a), (vid_b, canon_b)})` where `canon_x =
  ip_network(subnet, strict=False)` — the **canonical parsed** network, NOT the raw string,
  so a reformatted-but-identical CIDR doesn't read as a new violation (raw text stays in
  evidence). Same IP version only; identical subnet on the *same* vlan is not a collision.
  `applies_to` touches `vlan`; `requires` WIRED_L2.
- **GS32**: the overlapping **WLAN-id pair** (a pre-existing dup on A/B must not mask a new
  dup on C/D). `applies_to` touches `wlan`; `requires` WLAN_CONFIG.
- **GS33**: `wlan_id` (the predicate is binary: enabled + open + not isolated).
  `applies_to` touches `wlan`; `requires` WLAN_CONFIG.

**Per-check detection + never-false-positive guards:**

- **GS30**: violation = a `vlan_id` with non-empty `Vlan.collisions`. Evidence: the colliding
  network names; `caused_by` = the vlan.
- **GS31**: collect `(vlan_id, subnet)` from `ir.vlans` where `subnet` present, **not
  `subnet_unresolved`**, and `ipaddress`-parseable; flag pairwise `.overlaps()` across
  **different** vlans. A `subnet_unresolved`/unparseable subnet is **skipped** (never a false
  positive) and contributes a coverage note **only when relevance-scoped** (see below).
- **GS32**: group **enabled** WLANs by `ssid`; for a group of 2+, flag a pair whose AP scopes
  **provably overlap** — both `apply_to: site`; OR `site`/all + an explicit-AP WLAN; OR a
  shared explicit `ap_id`. wxtag-scoped, mixed-wxtag, **or `None`/unknown `apply_to`** →
  overlap **unverifiable** → coverage note (relevance-scoped), **not** a finding.
- **GS33**: violation = an **enabled** WLAN with `auth_type == "open"` AND
  `not isolation`. **Scope-aware**: explicit *empty* AP scope (`apply_to: aps`, `ap_ids=()`)
  → applies nowhere → silent (no finding/note); `site`/all or explicit non-empty AP → WARN;
  wxtag-only **or `None`/unknown `apply_to`** → "potentially active, unresolved" → coverage
  note (relevance-scoped), not a finding. Unknown/unparsed `auth_type` → skipped (never assume
  open). `None`/unknown scope is explicitly NOT treated as either "site-wide" or "inactive".

**Relevance-scoped coverage (critical).** `PARTIAL` floors the verdict to REVIEW, so a lint
check emits a coverage note **only** when the skipped/unverifiable item is **delta-touched**
*or* a `WARNING` conclusion's correctness depends on the incomplete comparison. An unrelated
pre-existing wxtag WLAN or an unparseable subnet on an untouched vlan must NOT taint an
unrelated change (same lesson as the blackhole/GS25 relevance-scoping). Otherwise `COMPLETE`.

### 4. Capabilities, gating, registration

- Registry order is `applies_to(diff)` **then** `requires()`. So: WLAN checks require
  `WLAN_CONFIG`; when a `wlan` diff IS present but either side lacks the capability →
  `INSUFFICIENT_DATA`. A change that mints no `Wlan` entities yields no `wlan` diff →
  `NOT_APPLICABLE`; an op targeting a non-existent WLAN id → apply rejection (UNKNOWN).
- GS30/GS31 require WIRED_L2 (always earned for switch sites).
- Four new checks in `checks/wired/`, added to `ALL_WIRED_CHECKS`; domains `wired.l2`
  (GS30), `wired.l3` (GS31), `wireless.wlan` (GS32/GS33).
- A `Vlan.collisions`/`Vlan.subnet` change surfaces as a *modified vlan* (the existing `vlan`
  diff kind), so GS30/GS31 `applies_to("vlan")` fire correctly.

## Testing

1. **Per-check units** — for each: introduced → WARNING, pre-existing → INFO; the guards:
   GS30 altered-claimant-set key; GS31 unresolved/unparseable subnet skipped + relevance-
   scoped note + same-version-only + same-vlan-not-a-collision; GS32 the three overlap cases
   vs the wxtag-unverifiable note + disabled-excluded; GS33 the open×isolation matrix incl.
   empty-scope-silent, wxtag-PARTIAL, and unknown-auth skipped.
2. **Ingest units** — `Wlan` minted from ALL rows incl. disabled; `isolation = isolation or
   l2_isolation`; `inherited` derived from `for_site`/`template_id`; scope normalization (site
   vs empty-explicit); tuple determinism. `Vlan.collisions` = distinct OTHER names; repeated
   site+device effective rows don't false-collide.
3. **Object/apply/field-gate units for `wlan`** — object_gate accepts a `wlan` op (pre-fetch);
   a modeled-leaf edit (e.g. `isolation: false`) on a **site-owned** WLAN passes the field
   gate and applies; an unmodeled-leaf edit → UNKNOWN; an op targeting an **inherited**
   (org-template) WLAN → UNKNOWN at the post-fetch screen (NOT a silent update);
   `get_object`/`replace_object` target `raw.wlans` by id (explicit `wlan` branch). **L0**: a
   `wlan` payload now **validates** (not fatal) against the committed `site_wlan` schema —
   replace the existing `validate_payload("wlan", …)`-is-fatal test.
4. **diff** — a modeled `wlan` change (ssid/enabled/auth/isolation/scope) diffs; a
   `Vlan.collisions` change diffs; an **`inherited`-only flip does NOT diff** (it's in
   `_IGNORED_BY_KIND["wlan"]`), so it can't spuriously fire GS32/GS33.
5. **Goldens GS30–GS33** — a delta that *introduces* each violation → REVIEW naming it; the
   same violation **pre-existing** → SAFE with an INFO finding. The pre-existing goldens
   include a **benign modeled in-domain edit that produces a diff but leaves the violation
   key unchanged** (e.g. add an unrelated network, or toggle an unrelated WLAN field) — a
   *true* no-op produces no diff, so `applies_to` would return NOT_APPLICABLE and no INFO
   context would be emitted.
6. **Live read-only verify** — the real org is rich material: `mist-guest` is open **with**
   `isolation:true` (GS33 negative); SSIDs like `Mist_IoT`/`Live_demo_only` appear on multiple
   org templates (candidate per-site GS32 cases). Confirm verdicts are unchanged on plans that
   don't touch vlan/wlan.
7. **Redaction** — assert the `Wlan` entity carries no secret field; and (if a golden fixture
   captures raw `wlans`) that the `portal_template_url` AWS pre-signed URL is tokenized by the
   existing `_URL_CRED` rule.

## Plan phases (preview)

- **P1** — `Wlan` IR entity + `WlanIngester` extension (mint all rows) + `diff_ir` kind +
  builder.
- **P2** — `Vlan.collisions` field + collision recording at the `_vlans` dedup (distinct
  other names).
- **P3** — WLAN as a simulable object: `object_gate` + `RAW_ALLOWLIST["wlan"]` + committed
  `site_wlan` L0 schema (`_SCHEMA_FILES["wlan"]`, flips the wlan-fatal test) + apply
  `get_object`/`replace_object` (explicit `wlan` branch) + post-fetch inherited rejection +
  field-gate tests.
- **P4** — the shared delta-conditioned helper + **GS33** (open guest) + **GS32**
  (duplicate ssid).
- **P5** — **GS31** (subnet overlap) + **GS30** (vlan collision).
- **P6** — goldens GS30–GS33 + live verify + roadmap (incl. fixing the stale GS20
  "← recommended next" tag) + memory.

## Open items (resolve during implementation)

1. Confirm the exact `WLAN_CONFIG`-capability behavior when `wlans` is fetched but empty
   (earned-as-empty vs unearned) so GS32/GS33 degrade honestly.
2. Confirm `apply` re-ingest re-mints `Wlan` from the mutated `raw.wlans` (the rolling-state
   path) so a `wlan` op's effect is visible to the proposed-side check.
