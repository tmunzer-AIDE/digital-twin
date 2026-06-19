# Richer impacted-client reporting

**Status:** design — pending user review
**Date:** 2026-06-19
**Author:** brainstormed with the repo owner

## Problem

The `wired.client.impact` check already names the *currently-connected* clients a
proposed change would disturb (vlan_move / disconnect / blackhole), and — since the
cause-attribution work — nests a per-client entry under `evidence["impacts"]` with its
own `caused_by`. But each entry only carries `mac`, `vlan`, `attachment`, `impact`,
`detail`, `caused_by`. A human or AI agent reading "3 clients affected" cannot tell
*which* devices: is it three idle laptops or the building's surveillance cameras and a
NAC-authed printer? The MAC alone is not actionable.

Mist already knows far more about each client — hostname, fingerprint (family / model /
OS), manufacturer, and the full NAC/auth story. This feature surfaces that as
**evidence only**: it enriches each impact entry so the report says *who* is at risk,
**without ever changing the verdict** (decision, severity, confidence, coverage, or
applicability).

## Goal

For every client the `client.impact` check already flags, attach observational identity
(hostname, fingerprint, manufacturer, auth/NAC detail), the client's derived subnet, and
a narrow DHCP-config-change signal — in both the JSON/dict output (for agents) and an
expanded human CLI rendering. **Strictly additive and non-load-bearing.**

### In scope
- A MAC-keyed enrichment join over `wired_clients` ∪ `wireless_clients` (base) and
  `nac_clients` (overlay), exposed as a new observational IR collection.
- Per-impact evidence fields: `identity{…}`, `subnet`, `dhcp_vlan_touched`.
- Human-output expansion of the per-client impacts (capped), full list in JSON.

### Out of scope (deferred)
- **Traffic significance** — no data source exists (neither `wired_clients` nor
  `port_stats` carry tx/rx bytes); the roadmap already marked this optional. Deferred.
- Any influence of enrichment on decision / severity / confidence / coverage / `applies_to`.
- Full DHCP *path* tracing from client to server (see `dhcp_vlan_touched` — v1 is
  VLAN-scoped, not a path trace).

## Domain facts (grounded against the live org, read-only)

The roadmap bullet over-stated what the client object carries. Verified field reality:

**`wired_clients`** (base): `mac`, `device_mac`, `port_id`, `vlan`, `ip`/`last_ip`,
`hostname`/`last_hostname` (observed, often empty), `manufacture` (MAC-OUI vendor, e.g.
"Raspberry Pi Trading Ltd"), `auth_method`, `auth_state`, `username`. **No** os / family /
model and **no** DHCP lease/subnet on the object.

**`wireless_clients`** (base, *already fetched*): carries `family`, `manufacture`,
`model`, `os`, `hostname` — richer than wired. Reused so wireless impacts enrich too.

**`nac_clients`** (overlay, NEW fetch): the richest source for NAC-authed clients —
`last_family` ("Surveillance Camera", "Access Point"), `last_mfg` ("Verkada Inc"),
`last_model` ("MIST BT11-WW"), `last_os`, `last_hostname`, `auth_type` ("mab"/"eap-tls"),
`last_nacrule_name`, `last_status` ("permitted"/"session_started"), `last_vlan` +
`vlan_source` ("nactag"), `username`. Many fingerprint fields are the literal string
`"Unknown"`.

**Subnet**: not on any client object; derivable only from the twin's modeled
`Vlan.subnet` (GS22).

## Architecture — "Approach B": a separate observational collection

Client identity is valuable evidence but must never become verdict-bearing. So it lives
**outside** the load-bearing `Client` entity and **outside** `diff_ir`'s entity kinds, in
a dedicated `ir.client_enrichment` map. The `client_impact` check reads it *only after*
it has already decided an impact, purely to annotate. Four independent properties make
the non-load-bearing guarantee hard to violate by accident:

1. **Self-isolating best-effort ingester (the verdict-path guarantee)** — `IngesterRegistry.run`
   records ANY ingester exception into `IngestReport.failures`; `report.ok = not failures`;
   `MistAdapter.ingest` sets `ir = None` when `report.ok` is False; the pipeline maps a `None`
   baseline/proposed `ir` to UNKNOWN. So a registered ingester that raises IS verdict-bearing
   through the error path. The enrichment ingester therefore MUST swallow all its own errors
   and never append to `IngestReport.failures` — see Component 2.
2. **No capability / `requires()`** — absent data yields an empty map; never UNKNOWN/REVIEW.
3. **Not in `diff_ir`** — an enrichment-only change produces an empty `IRDiff`, so it can
   never wake a check or move the verdict.
4. **Annotation-only in the check** — enrichment is read after impact detection; it never
   participates in detection conditions, severity, confidence, coverage, or `applies_to`.

## Components

### 1. Fetch + raw state

- `RawSiteState.nac_clients: tuple[JsonObj, ...] = ()` — defaulted trailing field, mirroring
  `wlans`. Absence means "not fetched / unavailable."
- Provider `_nac_clients()` method fetching the org `nac_clients` search scoped to the site.
- **Non-fatal but visible failure**: a failed `nac_clients` fetch leaves the field empty,
  earns **no** capability loss, and records a `StateMeta.failures` entry (the existing
  pattern) so the gap is observable without affecting the verdict.
- **Feasibility item (resolve in implementation):** confirm the existing site
  `wired_clients` fetch actually surfaces `last_hostname` / `manufacture` / `auth_method`
  (the probe cache showed leaner flat rows); adjust the call if needed. If unavailable,
  the base layer degrades to what is present — `nac_clients` still supplies fingerprint
  for NAC-authed clients.

### 2. `ClientEnrichment` record + join

New frozen IR record (own file), all fields `Optional`, all observational, carrying
OBSERVED `FactMeta`:

```
hostname, family, mfg, model, os,
auth_type, auth_method, auth_state, nacrule, status,
assigned_vlan, vlan_source, username
```

It deliberately does **not** carry vlan / attachment / subnet / dhcp facts — those are
computed in the check.

A dedicated `ClientEnrichmentIngester` builds `ir.client_enrichment: Mapping[str,
ClientEnrichment]`:

- **Key:** the existing client-MAC normalizer `client_id(mac)`, so wired / wireless / NAC
  rows for one device join on one key regardless of case/separators.
- **Base layer** from `wired_clients` (`hostname←last_hostname`, `mfg←manufacture`,
  `username`/`auth_method`/`auth_state`) and `wireless_clients` (`family`, `mfg←manufacture`,
  `model`, `os`, `hostname`).
- **Overlay layer** from `nac_clients`, winning per-field **only when the overlay value is
  useful** (`family←last_family`, `mfg←last_mfg`, `model←last_model`, `os←last_os`,
  `auth_type`, `nacrule←last_nacrule_name`, `status←last_status`,
  `assigned_vlan←last_vlan`, `vlan_source`, `username`/`hostname`).
- **`"Unknown"` / empty normalization:** any field whose value `.strip().lower()` is
  `"unknown"` or empty collapses to `None` (so a NAC `mfg="Unknown"` never overwrites a
  base `mfg="HP"`).

**Best-effort isolation (mandatory — the verdict-path guarantee from the architecture).**
The ingester MUST be non-fatal: it never lets an exception reach `IngesterRegistry.run`
(which would record an `IngestReport.failures` entry → `report.ok` False → `ir=None` →
UNKNOWN). Two layers:
- **Per-row:** each `wired_clients` / `wireless_clients` / `nac_clients` row is parsed in
  its own `try/except`; a malformed row is skipped (omitted from the map), the rest survive.
- **Whole-ingester:** the entire `ingest()` body is additionally wrapped so any unexpected
  error degrades to "no enrichment" (return `frozenset()`, empty map) rather than raising.
- It MUST NOT append to `IngestReport.failures` and earns **no** capability.
- Transparency is best-effort and lives **outside** the verdict path — e.g. a `bound_logger`
  line (the existing observability seam); never `IngestReport.failures`, never
  `CheckResult.coverage`.

**Join invariants (pinned by tests):**
- Every map key is a normalized `client_id(mac)` key.
- An entry with no non-empty fields is **omitted** (no blank `identity:{}` noise downstream).
- The map is exposed as an immutable mapping at the IR boundary, same style as
  `ap_wlan_vlans`.

### 3. Diff isolation

`client_enrichment` is a new IR field that `diff_ir` does **not** enumerate among its
entity kinds. An enrichment-only change therefore yields an empty `IRDiff`. This is the
**key acceptance test**: mutate only `client_enrichment` → empty diff, identical decision.

### 4. `client_impact` enrichment

`_entry()` gains three computed, evidence-only fields, populated **after** an impact is
decided (detection logic untouched):

- **`identity`** — projected from the **baseline** `ir.client_enrichment.get(client_id(mac))`,
  **non-`None` fields only**: `{hostname, family, mfg, model, os, auth_type, auth_method,
  auth_state, nacrule, status, assigned_vlan, vlan_source, username}`. The `identity` key is
  **omitted entirely** when there is nothing to say. Baseline (not proposed) because the
  finding describes the population *currently connected* before the change.
- **`subnet`** — from the **baseline** `ir.vlans[vid].subnet` (GS22); `None` when unresolved
  (never guessed).
- **`dhcp_vlan_touched`** — `bool`, narrow and mechanical: `True` iff the delta contains a
  DHCP-relevant change for this client's VLAN, defined as **any** of:
  - `Vlan.dhcp_sources` changed for `vid` (GS24),
  - a `DhcpScope` serving `vid` changed (GS25),
  - `device.dhcp_snooping` that applies to `vid` changed (GS25),
  - `port.dhcp_trusted` changed on the client's **own attachment port** (cheap — the port
    is already known; no full-path trace).

  This is a "DHCP config for this VLAN changed" signal, **not** a path trace; the name
  reflects that. Annotation only — it never feeds detection or severity.

Resulting per-impact entry: `{mac, vlan, attachment, impact, detail, caused_by, subnet,
dhcp_vlan_touched, identity:{…}}`.

### 5. Rendering

- **Dict/JSON:** the full per-client list (entire blast radius) serializes for free via
  the existing `_plain` walker — agents get everything.
- **Human CLI:** expand each impact under the `client.impact` finding, one indented line,
  e.g.:

  ```
  finding [WARNING] wired.client.impact.active_clients: 3 client(s) affected …
      - "LiveDemo-CD51" (Surveillance Camera · Verkada Inc) vlan 30 on HUB:mge-0/0/1 — disconnect  [auth mab/session_started via wired_camera_mab]
      - "LD_Kitchen" (Mist Systems) vlan 1 on EDGE:ge-0/0/46 — vlan_move: access vlan 1→20  [subnet 10.100.0.0/24] [dhcp config changed]
      - e8c8:…:40cb (Intel Corporate) vlan 1 on EDGE:ge-0/0/44 — blackhole
  ```

  Capped at **20** clients per finding with a "… and N more (see JSON)" note for large
  blast radii; the JSON retains the full list.

## The enrichment absent / present / broken equivalence invariant

The headline acceptance test (named so future readers know what it pins): runs of the same
plan that differ ONLY in enrichment input — **present**, **absent**, and **broken**
(malformed `nac_clients`/`wired_clients` rows, or an enrichment parser that would otherwise
raise) — must all produce an **identical decision, severity multiset, and coverage**. Only
the `evidence["impacts"][i].identity` / `subnet` / `dhcp_vlan_touched` annotations may
differ (richer when present, absent/MAC-only otherwise). The **broken** arm is what proves
the self-isolating-ingester guarantee: a cosmetic-data defect can never flip the verdict to
UNKNOWN. This extends the cause-attribution non-load-bearing pattern.

## Testing

1. **Diff-isolation test** — mutate only `client_enrichment` → empty `IRDiff`, identical
   decision. (Key acceptance test.)
2. **Join unit tests** — wired-only; wireless base; NAC overlay; the explicit
   HP-mfg + NAC `family=Printer`/`mfg=Unknown` case (result keeps `mfg=HP`, adds
   `family=Printer`); `"Unknown"` / `" Unknown "` / `""` → `None`; cross-separator MAC join;
   empty-record omission; key-normalization invariant.
3. **Check enrichment tests** — `identity` sourced from **baseline**; evidence shape;
   `subnet` present/absent; `dhcp_vlan_touched` true (each of the four triggers) / false;
   graceful MAC-only degradation when enrichment absent.
4. **Enrichment absent / present / broken equivalence golden** — identical decision /
   severity-multiset / coverage across all three arms, including a **broken-input** arm
   (a malformed `nac_clients` row / a row that would make a naive parser raise) that must
   behave exactly like the absent arm — never UNKNOWN, no `IngestReport.failures`. This is
   the direct regression for the self-isolating-ingester guarantee.
5. **Render tests** — human per-client expansion lines; the 20-cap truncation note; dict
   carries the full identity.
6. **Real fixture + live verify** — add redacted `nac_clients` to the committed fixture
   (redaction must cover NAC usernames / MACs / hostnames); then a read-only live run to
   confirm real identity surfaces and the verdict is unchanged vs the pre-feature run.

## Plan phases (preview)

- **P1** — `RawSiteState.nac_clients` + provider `_nac_clients()` fetch + non-fatal
  `StateMeta.failures` handling; resolve the `wired_clients` field-surfacing feasibility item.
- **P2** — `ClientEnrichment` record + `ClientEnrichmentIngester` join (base ∪ overlay,
  `"Unknown"`/empty normalization, key + omission invariants, **best-effort per-row +
  whole-ingester isolation**) + IR field + diff isolation.
- **P3** — `client_impact` enrichment (`identity` from baseline, `subnet`,
  `dhcp_vlan_touched`) + evidence shape.
- **P4** — render: human per-client expansion (capped) + dict.
- **P5** — enrichment absent/present equivalence golden + redacted-`nac_clients` fixture +
  live verify + roadmap/memory.

## Open feasibility items (resolve during implementation)

1. Whether the site `wired_clients` fetch surfaces `last_hostname`/`manufacture`/
   `auth_method`, or needs a call adjustment (Component 1).
2. The exact `nac_clients` provider endpoint/params (org search scoped to site) and its
   redaction profile for the committed fixture.
