# SP1 — Wireless client impact from lost provable SSID coverage (site)

**Status:** PROPOSED
**Date:** 2026-06-27
**Author:** brainstormed with the repo owner

First of three sub-projects expanding WLAN simulation (SP1 site, SP2 org WLAN,
SP3 wlantemplate). SP1 makes the twin flag, **fail-closed**, when a site-level
WLAN change leaves an active wireless client's SSID **no longer provably served
at its AP**. Delete and disable are the headline cases, but the check is framed
on *coverage loss* so it also catches SSID rename, scope shrink, and AP removal —
mutations that would otherwise resolve falsely SAFE.

## Problem

- The site path (`simulate`) is **update-only** (`object_gate._M1_ACTION`), so a
  WLAN **delete** is rejected outright — the twin cannot assess it at all.
- Worse, several *update* mutations silently pass as SAFE today even though they
  cut service for connected clients, because no check reasons about per-client
  SSID coverage:
  - **SSID rename** (`ssid: "corp" → "corp2"`) — clients on `corp` lose service.
  - **Scope shrink** (`apply_to: "site" →` an explicit `ap_ids` that excludes a
    client's AP; or an AP removed from `ap_ids`).
  - **Disable** (`enabled: true → false`).
  - A same-SSID survivor exists **only via `wxtag` scope**, so its coverage of
    the client's AP is unverifiable.
- The data to detect this is half-present: wireless clients are ingested, but the
  `Client` entity **drops the `ssid`** the raw client carries — so there is no
  join key from a client to the WLAN serving it.

## Goals

- **Site path accepts `delete` for `wlan` only.** `site_setting`/`device` stay
  update-only. The delete op **skips L0 + field-gate** (no proposed object, like
  org deletes); a **post-fetch ownership gate** rejects deleting an *inherited*
  (org/template-owned) WLAN, reusing `wlan_is_inherited` — SP2/SP3 own that.
- **`Client.ssid`** (wireless only) — observational, **non-load-bearing**: added
  to the diff's per-kind ignored fields and pinned by a diff-isolation test
  (`Client` IS diff-bearing today — `ir/diff.py` lists `("client", …)`).
- **New check `wireless.wlan.client_impact`** built on a single invariant
  (below), with the verdict matrix in §3. `requires()` = `WLAN_CONFIG` only;
  `CLIENTS_ACTIVE` is inspected **internally** so missing telemetry degrades to
  `.unverified` (REVIEW-floor), never INSUFFICIENT_DATA and never SAFE.
- **Reuses** the merged `config_diffs` (the delete/rename verdict also shows the
  WLAN before→after) and the existing scope-verifiability notion from
  `wlan.duplicate_ssid`.

## Non-goals (recorded, deferred)

- **Org WLAN / wlantemplate** changes — SP2 and SP3. SP1 is site-owned WLANs
  only; inherited WLANs are bounced (ownership gate / existing field-gate).
- **Roaming/airtime/capacity** modeling — SP1 only answers "is this client's
  SSID still provably reachable at its AP," not signal/load.
- **AP up/down or radio state** — coverage is judged from WLAN config scope, not
  live AP health.
- **Wired clients** — unaffected; `ssid` stays `None` for them.

## §1 The invariant (the check)

> For every active **wireless** client on a **baseline** SSID that a changed WLAN
> affects, the **proposed** state must contain an **enabled** WLAN with that same
> SSID that **provably covers the client's AP**. If not — including when coverage
> can only be established unverifiably — **fail closed**.

- **applies_to**: the diff touches `wlan` (any WLAN added / removed / modified).
- **Affected SSIDs** = the set of `ssid` values carried in **baseline** by each
  delta-touched WLAN **that was `enabled=True` in baseline** (removed, disabled,
  renamed, or scope-reduced *enabled* WLANs, plus any whose `ssid`/`enabled`/
  `apply_to`/`ap_ids`/`wxtag_ids` changed). A WLAN **already disabled** in
  baseline served no client, so it contributes **no** affected SSID — renaming or
  deleting it must not produce a false `coverage_lost` (P1). Added-only WLANs
  create no affected SSID (they remove no coverage) but DO count as potential
  survivors (below).
- **Impact set** = active wireless clients whose `Client.ssid` ∈ affected SSIDs.
- **Unknown-SSID clients** = active **wireless** clients with `Client.ssid is
  None` (telemetry fetched a row but without an SSID). When affected SSIDs is
  non-empty these cannot be confirmed unaffected, so they are **never** allowed to
  fall into "zero impacted → SAFE" — they degrade to `.unverified` (§3, P1).

## §2 Provable coverage — `_covers(wlan, ap_id) -> yes | no | unknown`

Mirrors `apply_to` semantics (and the `wlan.duplicate_ssid` verifiability rule):

| `wlan.apply_to` | result |
|---|---|
| `"site"` | **yes** (covers every AP at the site, incl. an unmappable one) |
| `"aps"` | **yes** if `ap_id ∈ wlan.ap_ids` (and `ap_id` is known/mappable); else **unknown** |
| `"wxtags"` / `None` / unrecognized | **unknown** (wxtag→AP membership not modeled) |

A client on AP *X* is **provably still served** for SSID *S* iff some **enabled**
proposed WLAN `W'` has `W'.ssid == S` **and** `_covers(W', X) == yes`. If every
candidate is `no` or `unknown`, the client is **impacted** (fail-closed).

**Unmappable client AP** (`Client.attach_id` not resolvable to a known AP): only
a `"site"`-scoped survivor proves coverage; `"aps"`/`"wxtags"` survivors are
`unknown` → fail-closed. (Note: today `ClientsIngester` skips clients on unknown
APs, so this is a defensive rule, not a common path — stated so the boundary is
explicit and future-proof.)

## §3 Verdict matrix

Rows are evaluated together; the **most severe** outcome wins (UNSAFE > REVIEW > SAFE).

| Situation | Finding | Verdict |
|---|---|---|
| Wireless-client telemetry **not fetched** (`CLIENTS_ACTIVE` absent either side) | `…client_impact.unverified` (OPERATIONAL, WARNING) | **REVIEW**-floor |
| Telemetry fetched; ≥1 impacted client (no provable survivor, or unverifiable) | `…client_impact.coverage_lost` (NETWORK, ERROR, HIGH) — evidence lists impacted clients (mac, ap, ssid) | **UNSAFE** |
| Telemetry fetched; affected SSIDs non-empty AND ≥1 **unknown-SSID** wireless client, with no `coverage_lost` | `…client_impact.unverified` — "active wireless client(s) with unknown SSID — cannot confirm unaffected" | **REVIEW**-floor |
| Telemetry fetched; **zero** impacted clients, **all** clients' SSID known (all provably roam, or no clients on affected SSIDs) | none | **SAFE**, coverage **COMPLETE** + plain note |

The point-in-time note ("client impact assessed from point-in-time wireless
telemetry") is **informational text on COMPLETE coverage** — NOT a `PARTIAL` note
(PARTIAL would floor REVIEW and contradict SAFE).

### Finding shape (pinned)

`coverage_lost` (the headline UNSAFE finding) — one **per affected SSID** (§4):
- `source = CHECK`, `category = NETWORK` (NETWORK + ERROR is what drives UNSAFE;
  OPERATIONAL never does), `severity = Severity.ERROR`, `confidence = HIGH`
  (Status `FAIL`).
- `subject = ObjectRef("wlan", <changed_wlan_id>, <ssid>)` — the headline WLAN.
- `affected_entities` = the impacted clients' IR ids (deduped) — feeds the
  client-impact/visual-attribution surface.
- `caused_by = ctx.delta_index.causes("wlan", [<changed wlan ids carrying this SSID>])`
  — the WLAN delete/disable/rename/scope edit that produced the loss.
- `evidence` = per-client `{mac, ap, ssid}` list.

`unverified` — `source = CHECK`, `category = OPERATIONAL` (→ REVIEW, never
UNSAFE), `severity = Severity.WARNING`, `confidence = HIGH`.

## §4 Aggregation (no duplicate findings)

Impact is computed per **(affected SSID, client)** and de-duplicated: when two
delta-touched WLANs share one SSID, an impacted client is reported **once**. One
`coverage_lost` finding **per affected SSID**, its evidence carrying the deduped
impacted-client list — never one finding per changed WLAN row.

## §5 Delete plumbing (site path)

- **`object_gate.py`** — the site branch allows `delete` **only when
  `object_type == "wlan"`**; all other types stay update-only. A `wlan` delete
  with a **non-empty payload is rejected** (UNKNOWN), matching the org/NAC rule
  "a delete has no proposed object" — so a stray payload is never silently
  ignored.
- **Delete skips L0 + field-gate** (no proposed object), the same shape as org
  deletes — so phrase this as "delete bypasses the L0/field-gate stages," NOT
  "the field gate rejects it."
- **Post-fetch ownership gate** — after the WLAN is fetched, reject deleting an
  **inherited** WLAN via `wlan_is_inherited(row)` ("inherited from an org
  wlantemplate — simulate at the org/template level"). This replaces the
  field-gate's inherited rejection for the delete case (the field gate keyed off
  changed paths, which a payload-less delete has none of).
- **Proposed state** — a `wlan` delete yields a proposed raw state with that
  WLAN removed from the derived WLAN list, so `diff_ir` shows the `Wlan` present
  in baseline / absent in proposed → the check's `applies_to` fires.
- **`config_diffs`** — reused as-is: the delete verdict shows the removed WLAN's
  before→after (all leaves removed), redacted; disable/rename show their leaf
  deltas.

## §6 `Client.ssid` (observational, non-load-bearing)

- Add `ssid: str | None = None` to `Client` (`ir/entities.py`); populate from the
  raw wireless client's `ssid` in `ClientsIngester` (`None` for wired).
- Add `"client": frozenset({"ssid"})` to `_IGNORED_BY_KIND` in `ir/diff.py` so a
  client's observed SSID never registers as a config diff.
- **Diff-isolation test**: two IRs identical except a wireless client's `ssid`
  differs → `diff_ir` produces **no** `client` change.

## §7 Testing

- **object_gate**: `wlan` delete accepted; `site_setting`/`device` delete still
  rejected (update-only); a non-wlan delete still rejected; **`wlan` delete with a
  non-empty payload → UNKNOWN** (P2).
- **ownership gate**: deleting an inherited WLAN → UNKNOWN/rejected with the
  inherited reason; deleting a site-owned WLAN → proceeds.
- **ingest**: `Client.ssid` set for wireless from raw `ssid`, `None` for wired;
  diff-isolation test (above).
- **check e2e** (each asserts decision + the right finding code):
  1. delete WLAN, active client on its SSID, no survivor → **UNSAFE** + client listed.
  2. disable (`enabled: true→false`), active client → **UNSAFE**.
  3. **rename** (`ssid` changed), active client on old SSID, no survivor → **UNSAFE**.
  4. **scope shrink** (`apply_to: site→aps` excluding the client's AP) → **UNSAFE**.
  5. delete, but a provable **site-scope** survivor WLAN with same SSID → **SAFE** + note.
  6. survivor exists only via **wxtag** scope → **UNSAFE** (fail-closed).
  7. telemetry **not fetched** → `.unverified` → **REVIEW**.
  8. telemetry fetched, **zero** clients on the SSID → **SAFE**, COMPLETE coverage, note present, no PARTIAL.
  9. two changed WLANs share one SSID, one impacted client → **one** finding (aggregation).
  10. **disabled baseline WLAN** (already `enabled=False`) deleted/renamed, active client on that SSID → **NOT** flagged (no affected SSID) (P1).
  11. telemetry fetched, coverage change, active wireless client with **unknown SSID** (`Client.ssid is None`), no provable-loss client → **REVIEW** via `.unverified` (P1).
- **finding shape** (P2): on a `coverage_lost` finding assert `category == NETWORK`,
  `severity == ERROR`, `affected_entities` == the impacted client ids, and
  `caused_by` is non-empty (the changed WLAN); on `.unverified` assert
  `category == OPERATIONAL`, `severity == WARNING`.
- **config_diffs** present on a delete verdict (before→after of the removed WLAN).
- goldens + `docs/ROADMAP.md` entry.

## Files touched (anchor map for the plan)

- `src/digital_twin/ir/entities.py` — `Client.ssid`.
- `src/digital_twin/ir/diff.py` — `_IGNORED_BY_KIND["client"] = {"ssid"}`.
- `src/digital_twin/adapters/mist/ingest/clients.py` — populate `ssid` (wireless).
- `src/digital_twin/scope/object_gate.py` — allow `wlan` delete (site branch).
- `src/digital_twin/engine/pipeline.py` (+ apply/ingest) — site `wlan` delete:
  skip L0/field-gate, post-fetch ownership gate, removed-from-proposed.
- `src/digital_twin/checks/wired/wlan_client_impact.py` (NEW) —
  `WlanClientImpactCheck` (id `wireless.wlan.client_impact`).
- `src/digital_twin/checks/wired/__init__.py` + `tests/test_public_api.py` — register + count.
- Tests: `tests/engine/test_pipeline.py`, `tests/adapters/.../test_ingest_*`,
  `tests/checks/test_wlan_client_impact.py`, `tests/ir/test_diff*`, goldens.
- `docs/ROADMAP.md`.
