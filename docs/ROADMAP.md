# Roadmap / backlog

The single place for "what's next." Status: ✅ done · 🔵 in scope, not started ·
🟡 needs a decision · 🔴 open debt. M1 (one site, switch L2, Wi-Fi-aware client
impact) is **done**; everything below is post-M1. Ordered by leverage, grouped
by kind.

GS numbers are golden-scenario slots: **GS1–GS19 implemented** (GS12 unused —
numbering gap), GS20+ assigned below to planned work so slides/docs and the
test suite share one numbering.

## 1. Precision — turn honest REVIEWs into precise verdicts (real-use driven)

These are the gaps that made real changes resolve to REVIEW/MEDIUM instead of a
sharp UNSAFE during live testing on the Live-Demo site.

- 🔵 **wxtag WLAN scoping** (GS20) — resolve which APs a `apply_to: wxtags` WLAN
  applies to (evaluate wxtag membership against AP model/name/etc.). Today these
  WLANs are recorded `unresolved` → REVIEW. This is the last unmodeled piece of
  the WLAN→VLAN story; it would make the *original* reported bug (AP-uplink
  trunk→access) a precise UNSAFE naming the SSIDs. _Investigated + deprioritized
  2026-06-20: the Live-Demo org uses no `apply_to: wxtags` WLANs, and current
  behavior is already never-false-SAFE (unresolved → REVIEW/coverage note). Pick
  it up only if a real org surfaces wxtag-scoped WLANs._
- ✅ **PoE impact** — `poe_disabled` is now modeled (`Port.poe` config intent +
  `Port.poe_draw` observed from stats `poe_on`); `wired.poe.disconnect` fires
  UNSAFE when a port that powers an LLDP-confirmed AP or an observed-drawing
  device loses PoE. Verified live: `plan.json` now → UNSAFE naming the exact
  APs and their client counts (was UNKNOWN). [done 2026-06-10]
- ✅ **Richer L3 exit modeling** (GS22) — done 2026-06-11. The gateway/SRX is
  modeled from its OWN config: LAN-port carriage (names resolved via the new
  `org_networks` fetch — the GATEWAY namespace, different names than the
  switch-side site networks; unresolvable names → vlan-BLIND port, never
  config-empty) and L3 interfaces (ip_configs → CONFIG/HIGH; routed org
  network attached to a LAN port → INFERRED/MEDIUM per the Mist gateway
  model). `Vlan.subnet` carries routed intent (site + org overlay; `{{var}}`
  values stay unresolved, never guessed — live org crash caught this). New
  check `wired.l3.gateway_gap` (MVP: ROUTE-GW): removing the only modeled L3
  interface of a routed network → UNSAFE; newly-routed-unserved → REVIEW.
  Live: vlan 2 + vlan 250 lost their "unlocatable exit" blind spots (the SRX
  terminating LD_VLAN2 is now a modeled exit); only vlan 22 remains, honestly.
  NOTE remaining: fixture re-captures need redaction to hash network-NAME
  references consistently (org network `name` vs port_config lists) or
  gateway joins break in fixtures — tracked in section 5.
- 🔵 **Dynamic profiles on neighbor switches** (GS23) — the core's downlink to an IDF
  isn't in its `port_config` (system/dynamic), so inter-switch links are
  blind-peer (MEDIUM). Resolving the *neighbor's* dynamic/system ports would
  lift those to HIGH.
- ✅ **Finding cause attribution** (`Finding.caused_by`) — done 2026-06-17.
  Every delta-attributed finding now names the changed entity (port/link/device/
  l3intf/dhcp_scope) that produced it, with the IR fields that changed; pre-existing
  context rows carry none. Spec + plan
  (`docs/superpowers/specs/2026-06-16-finding-cause-attribution-design.md`,
  `docs/superpowers/plans/2026-06-16-finding-cause-attribution.md`). Strictly
  evidence-only (non-load-bearing, golden-pinned). `analysis/delta_cause.py` =
  cached `DeltaIndex` on `CheckContext` + per-finding graph mappings (boundary/cut/
  split/merge/loop/root). Live: `isolation.severed` on a real severed-uplink plan
  now reads `(caused by port "ge-0/0/46" […])`. **Deferred follow-ons:** cause-first
  rendering (group output by changed port, not by vlan); per-leaf differential
  `--explain` mode (re-simulate each leaf in isolation); raw-config-path `fields`
  (needs a field-gate change-path index, which the differential mode also wants);
  articulation-node-removal split attribution (currently honest-empty).
- ✅ **Richer impacted-client reporting** — done 2026-06-20, live-verified (284
  enriched clients on the Live-Demo site). `wired.client.impact` now annotates each
  affected client (evidence-only; **never** changes decision/severity/coverage). A
  separate observational `ir.client_enrichment` map (Approach B) joins
  `wired_clients ∪ wireless_clients` (base) + `nac_clients` (overlay) by normalized MAC,
  built by a **self-isolating best-effort** ingester (no capability, not in `diff_ir`,
  swallows all its own errors so a malformed row can never flip the verdict to UNKNOWN —
  pinned by the absent/present/**broken** equivalence golden). Each `evidence["impacts"]`
  entry gains: **`identity`** (hostname / family / mfg / model / os / auth_type / nacrule /
  status / …, from BASELINE enrichment, allowlisted so observational `meta` never leaks);
  **`subnet`** (derived from `Vlan.subnet`); **`dhcp_vlan_touched`** (a narrow 4-trigger
  signal: the vlan's `dhcp_sources` / a serving `DhcpScope` / the client's port
  `dhcp_trusted` / vlan-scoped `dhcp_snooping` via `snooped_vlans`). Human CLI expands each
  client (capped 20 + "… and N more"); JSON carries the full blast radius. Spec
  `docs/superpowers/specs/2026-06-19-richer-impacted-client-reporting-design.md`; plan
  `docs/superpowers/plans/2026-06-19-richer-impacted-client-reporting.md`. Redaction v7
  closes a `last_hostname`/`username` PII gap surfaced by serializing `nac_clients`.
  - **Deferred:** **traffic significance** — no data source exists (neither `wired_clients`
    nor `port_stats` carry tx/rx bytes), so the optional "weight impact by real traffic"
    sub-item is dropped until a stats fetch is added. DHCP **lease** detail is likewise
    absent from the client object (only IP + derived subnet are modeled).

## 2. New coverage — more checks over the existing IR

- ✅ **native-VLAN mismatch** — `wired.l2.native_mismatch`: both-ends-known
  mismatch introduced/altered by the delta → UNSAFE (the leak is invisible to
  reachability analysis — the graph just drops the native); pre-existing →
  INFO context; native changed against a vlan-blind peer → REVIEW
  (unverifiable, never silent); AP uplinks vlan-transparent. GS18.
  [done 2026-06-10]
- ✅ **MTU mismatch** — `wired.l2.mtu_mismatch`: explicit-vs-explicit
  introduced/altered → UNSAFE; explicit vs platform-default (value unmodeled)
  or vs a vlan-blind peer → REVIEW, with the same baseline-parity/uncertainty
  symmetry as native_mismatch (shared `link_boundary.BoundaryView`). `mtu`
  modeled from port_usages + inline port_config/local_port_config (NOT
  port_config_overwrite — not in schema). GS19. [done 2026-06-10]
- ✅ **STP topology** (GS21) — two checks: `wired.stp.edge_on_uplink`
  (`stp_disable` = BPDU drop on a switch-to-switch link → UNSAFE, MVP:
  STP-BPDU; `stp_edge` there → REVIEW; AP uplinks skipped — edge toward an AP
  is correct practice) and `wired.stp.root_change` (predicted root election —
  lowest (bridge_priority, mac) per L2 component, default 32768 ASSUMED →
  MEDIUM — moves → REVIEW). Modeled: `port_usages.{stp_edge,stp_disable}`,
  `local_port_config.stp_edge`, `stp_config.bridge_priority` (device override
  required a compile fix — `_DEVICE_OWN_FIELDS` was silently dropping it).
  GS21 + variant; live test plan 07 → REVIEW naming the real root move.
  [done 2026-06-11]
- 🔵 loop check FAIL path — currently maxes at WARN because Mist live data never
  asserts STP *disabled*; revisit if a config source for that appears.

### Config-lint tier (single-state checks over the PROPOSED IR — MVP carryover)

The delta checks compare baseline vs proposed; these validate the proposed
state on its own. Cheap: the data is already fetched and in the IR.

- ✅ **VLAN ID collision** (GS30) — `wired.l2.vlan_collision`. Reads
  `Vlan.collisions` (distinct other names minted at the ingest dedup, no longer
  folded invisibly). [done 2026-06-20]
- ✅ **IP subnet overlap** (GS31) — `wired.l3.subnet_overlap`. Pairwise overlap
  across VLAN subnets, keyed on the canonical parsed network; unresolved/
  unparseable subnets skipped with a relevance-scoped coverage note. [done
  2026-06-20]
- ✅ **Duplicate SSID** (GS32) — `wireless.wlan.duplicate_ssid`. Same SSID on
  2+ enabled WLANs with PROVABLE AP-scope overlap; wxtag/unknown scope →
  coverage note, never a finding. [done 2026-06-20]
- ✅ **Open guest SSID without isolation** (GS33) —
  `wireless.wlan.open_guest`. Enabled open-auth WLAN with no client isolation;
  scope-aware (empty scope silent, unresolved → note). [done 2026-06-20]

All four are **delta-conditioned** via the shared `run_delta_lint` core
(introduced → WARNING/REVIEW; pre-existing → INFO context, never floors an
unrelated change) and rest on the new `Wlan` IR entity (secret-free) + WLAN as
a simulable site object. Live-verified 2026-06-20: `mist-guest` (open WITH
isolation) correctly NOT flagged; ingest clean.

- 🔵 **WLAN auth-type transition (psk/eap → open) → sharp GS33** — the twin
  models only `auth.type`, but Mist replaces the whole `auth` ROOT, so a
  psk/eap→open edit drops companion `auth.{psk,…}` leaves. The field gate
  rejects those deletions as out-of-scope → the op floors to UNKNOWN before
  `wireless.wlan.open_guest` (GS33) can fire — exactly the transition it targets.
  Never false-SAFE (UNKNOWN is conservative), just blunt. Pinned by
  `tests/scope/test_wlan_object.py::test_auth_root_replace_currently_out_of_scope`.
  Fix = a deliberate auth-leaf policy in the field gate: when `auth.type` is
  among the changed leaves on a `wlan` op, ignore companion `auth.*` secret/
  unmodeled churn so GS33 runs; a pure auth change with no type change stays
  UNKNOWN. (Decide the psk→eap-reads-SAFE edge before building.)

### Routing & services tier (needs the L3/routing IR extension)

Today every plan touching these resolves to UNKNOWN by default-deny (test
plan 02 pins it) — never false-SAFE, but not yet useful. Each item = model
the config (allowlist + IR) + a check + a GS. Builds on "richer L3 exit
modeling" below.

- ✅ **DHCP path removal** (GS24) — done 2026-06-11. `Vlan.dhcp_sources`
  models the providers: site-level `dhcpd_config` (type local, or relay WITH
  servers; 'none' = explicit no-path) + gateways' own `dhcpd_config` (names
  via org networks — the live SRX serves LD_VLAN2 this way). Check
  `wired.dhcp.path` (12th): removal with observed clients → UNSAFE; without →
  REVIEW; never-served vlans silent (external servers invisible). Review-series
  rails built in: blind gateway caps at MEDIUM/REVIEW; clients-unfetched
  degrades coverage (GS6), never silently downgrades. Allowlist:
  `dhcpd_config.*.{type,servers}` on site_setting ONLY — device-level switch
  dhcpd_config stays unmodeled (compile_device does not carry it; allowlisting
  it would be a GS21-class false-SAFE shape — model it together with the
  compile carry-through if needed). GS24 + clientless variant (first goldens
  exercising a site_setting op).
- ✅ **DHCP lint** (GS25, MVP: CFG-DHCP-RNG / CFG-DHCP-CFG + snooping) —
  DONE 2026-06-12: `wired.dhcp.scope_lint` (`.overlap` pairwise ranges,
  `.out_of_subnet` — WARNING introduced/altered, INFO pre-existing-unchanged,
  violation-specific parity) + `wired.dhcp.snooping` (`.untrusted_path` —
  any-trusted-path-is-enough over the vlan graph; trust = allow_dhcpd or
  trunk-default, tri-state with unknown-never-untrusted; "site" sources
  unlocatable → PARTIAL by design). New IR: `DhcpScope` (provider:network
  identity, subnet_unresolved blind flag), `Port.dhcp_trusted`,
  `Device.dhcp_snooping`. Delta-gated adapter finding
  `scope.dhcp.range_unresolved` for templated ranges. En route: fixed a
  shipped GS24 false-SAFE (`_dhcp_active` ignored OAS-canonical type
  `server`). 14 wired checks; GS25a/GS25b goldens + 3 variants; live plan 02
  graduated UNKNOWN→SAFE (dhcp_snooping now modeled; file renamed).
  Spec/plan: docs/superpowers/{specs,plans}/2026-06-11-gs25-dhcp-lint*.md.
- ✅ **Default gateway gap** (part of GS22, MVP: ROUTE-GW) — DONE 2026-06-12:
  `gateway_gap.gateway_unowned` (interfaces exist but none owns the declared
  `networks.*.gateway` — known-owner-broken → ERROR/UNSAFE at the owner's
  confidence; never-owned → WARNING/MEDIUM; pre-existing → INFO; strict
  precedence vs the existence codes) + `scope_lint.gateway_mismatch`
  (DHCP-handed gateway incoherent with its owning network — WARNING/INFO
  with both-values-byte-identical parity). New IR: `Vlan.gateway(+_unresolved)`
  minted from the WINNING effective network row (non-winning-row conflict =
  unresolved intent, never a silent winner; null==absent canon),
  `DhcpScope.network_gateway(+_unresolved)` in the provider's namespace;
  `ir/ip_match.py same_ip` (family-aware, /prefix-tolerant, None=unknown —
  IR-layer so ingest can use it). Goldens GS22-GW a-d (owner-broken UNSAFE,
  preexisting INFO/SAFE, mismatch REVIEW, preexisting-mismatch SAFE; the
  b/d staging resolves two fixture recording artifacts in the dynamic-port
  profile). En route: mint loop hardened against templated vlan_id (the
  _vlan_int contract). Spec/plan: docs/superpowers/{specs,plans}/
  2026-06-12-gs22-default-gateway-gap*.md.
- ✅ **OSPF exit withdrawal** (GS26) — done 2026-06-13. Structural withdrawal of
  a SWITCH's OSPF participation for a routed segment (no RIB → we detect modeled
  participation leaving OSPF, never real reachability). New IR entity `OspfIntf`
  (switch-only; role-validated; minted by a `_ospf` ingest pass gated on
  `ospf_config.enabled`, joined by network name, `unresolved` row when the name
  has no vlan). Check `wired.l3.ospf_withdrawal` (15th wired check), three codes:
  `.egress_lost` (a device's last ACTIVE adjacency collapses — removal, disable,
  or a collapsing active→passive flip — ERROR/UNSAFE iff an affected segment has
  observed clients, else REVIEW), `.advertised_removed` (a routed segment fully
  withdrawn while the device keeps adjacency → REVIEW), `.transit_mutation` (the
  deferred-mutation REVIEW floor for a retained `(device,vlan)` whose
  active-status/area changed — GS27 supersedes it; a pure rename stays silent).
  Comparison is by the semantic `(device,vlan[,area,active])` tuple, never
  `OspfIntf.id`; egress-collapse suppresses the weaker codes per-`(device,vlan)`,
  so an independent mutation on a second device sharing the vlan still surfaces.
  Leaf-tightened allowlist: ONLY `ospf_config.enabled` +
  `ospf_areas.*.networks.*.passive` (metric/area-type/auth/timers denied →
  UNKNOWN, so GS27 adopts `metric` without a false-SAFE hole). Compile
  carry-through fix (`ospf_*` → `_DEVICE_OWN_FIELDS`, the GS21 gotcha). Bare-`{}`
  active withdrawal is in-scope (detection rides the IR diff, not raw leaves).
  Goldens GS26 a–e (advertised_removed REVIEW; bare-{} collapse + client UNSAFE;
  disable + client UNSAFE; addition SAFE; non-collapsing flip transit_mutation
  REVIEW). Gateway OSPF deferred (device-level gateway ops are out of M1 scope at
  the field gate). All eight live plans unchanged (live `ospf_areas` empty).
  Spec/plan: docs/superpowers/{specs,plans}/2026-06-13-gs26-ospf-exit-withdrawal*.md.
- ✅ **OSPF transit changes** (GS27) — done 2026-06-22. Five structural codes on
  `wired.l3.ospf_withdrawal` replacing the GS26 `.transit_mutation` placeholder:
  `.metric_changed` / `.passive_flip` / `.area_changed` / `.participation_added`
  (additions are NOT SAFE — a bare-`{}` add yields an `ospf_intf` diff) /
  `.advertised_prefix_changed` (a retained OSPF vlan's connected prefix shifted).
  All WARNING/REVIEW; `metric` now modeled + allowlisted. Plus an **escalate-only
  live-telemetry** layer (`site_ospf` neighbors → `OspfNeighbor`, secret-free,
  non-diff-bearing, self-isolating ingester, `OSPF_TELEMETRY` capability; pure
  `analysis/ospf_reachability.py` predicts each active interface's connected subnet
  and confirms a break only for an **established** peer): a confirmed break escalates
  the owning structural finding to UNSAFE naming the peer; `.peer_unreachable` is the
  defensive backstop; unevaluable (subnet→unresolved) and blind cases stay REVIEW
  notes, never UNSAFE. **Built blind** (the reachable org has zero OSPF) — proven by
  synthetic goldens; live regression-only. **Deferred follow-ups:** (a) ground/verify
  the telemetry model on a real OSPF-bearing org — PARTIAL: the `ospf_peers/search` record
  shape was grounded 2026-06-23 (live records are lowercase `state`, carry `vrf_name`, omit
  `area` → subnet-only matching; pinned by `test_real_ospf_peers_payload_shape_parses`); a
  full live simulate against an OSPF org is still pending; (b) the `site_ospf` endpoint **404s**
  on the live org and is currently read as "fetched empty" (earns `OSPF_TELEMETRY`,
  zero peers) — never-false-SAFE under escalate-only, but a 404/endpoint-unavailable
  should read as telemetry-**blind** (no capability) for coverage honesty; (c) extract
  the telemetry layer (`run()` sections 7–8) into a `_apply_telemetry` helper.
  Spec/plan: `docs/superpowers/{specs,plans}/2026-06-22-gs27-ospf-transit-changes*.md`.
- 🔵 **BGP adjacency break** (GS28, MVP: ROUTE-BGP) — `bgp_config` on SWITCHES
  too, and NOT only in EVPN/campus-fabric deployments: a standalone L3 switch
  can run plain BGP (peering to a router/firewall/upstream) with no fabric at
  all. Cases: fabric underlay/overlay peers, standalone switch BGP, gateway
  WAN peers. Removing a neighbor that carries the peering or the default
  route → UNSAFE; with live telemetry: peer IPs vs predicted subnets. NOTE:
  the committed `device_switch.schema.json` snapshot has
  ospf_areas/ospf_config but NO `bgp_config` — refresh the OAS snapshot when
  this lands, or switch-BGP plans will fail/act unvalidated at L0.
- 🔵 **WAN failover impact** (GS29, MVP: ROUTE-WAN) — WAN port removed from a
  gateway → redundancy/bandwidth reduction → REVIEW; the last one → UNSAFE.
- 🔵 **Security policy / NAC rule deltas** (GS34, MVP: SEC-POLICY, SEC-NAC) —
  new object types (out-of-scope → UNKNOWN today); first step is honest diff
  REPORTING of additions/removals/changes, before any impact modeling.

## 3. New scope — more fields / objects / sites

- 🟡 widen the field allowlist case-by-case (each needs an IR model + check, or
  an explicit "modeled" decision): `dhcp_snooping`, `dhcpd_config`,
  `port_mirroring`, `vrf_config`, … Default-deny stays the rule.
- ✅ **multi-site / org-template (networktemplate) simulation** — done 2026-06-14.
  A `networktemplate` (switch template) edit is simulated across ALL sites
  assigned to it: new `simulate_org_template(plan) -> OrgVerdict` (separate entry;
  single-site `simulate()` unchanged). The per-site pipeline core was extracted
  (`_simulate_site_state`, stages 5–10) and is reused per assigned site. Flow:
  classify SITE vs ORG plan mode (object_gate — ORG = all-`networktemplate` +
  no `site_id`, exactly one template id; both `simulate`/`simulate_org_template`
  guard the wrong mode → UNKNOWN); `resolve_org_template` (listOrgSites filter by
  `networktemplate_id` + fetch the template) → apply the edit to ONE snapshot →
  **override each fetched site's `networktemplate` with the baseline/proposed
  snapshot** so the per-site diff is exactly the edit (the fetch-race guardrail) →
  org-level L0 + field gate ONCE (networktemplate allowlist = the site_setting
  leaf tuple; `switch_matching` denied → UNKNOWN) → per-site dynamic/derived
  gates + the existing checks → `decide_org` rollup (worst-of
  `UNKNOWN>UNSAFE>REVIEW>SAFE` + a `template_findings` REVIEW floor + 0-sites
  SAFE). `OrgVerdict` carries per-site `Verdict`s + driving sites + `site_failures`
  + structured `org_rejections` (fatal-L0/conflict/field-gate/lookup) + non-fatal
  `template_findings`. CLI/MCP dispatch by mode (defensive — malformed → SITE
  path → UNKNOWN, never a crash). Goldens MS-a..d (network-removal breaks one
  site → org UNSAFE naming it; fetch-fail site → UNKNOWN; cosmetic → SAFE;
  0-assigned → SAFE). Live: 8 single-site plans unchanged; a no-op `{}` edit on a
  real template assigned to 2 sites ran the full real-provider fan-out → SAFE,
  rollup consistent. 770 tests. Spec/plan:
  docs/superpowers/{specs,plans}/2026-06-14-multisite-org-template-simulation*.md.
- ✅ **gatewaytemplate / sitetemplate** as first-class `object_type`s —
  done 2026-06-15 (21 TDD tasks, branch `feat/gateway-site-templates`). Generalizes
  the networktemplate fan-out to a **typed** set `{networktemplate, gatewaytemplate,
  sitetemplate}` over a unified layered effective-config compiler
  (`fold_layers(layers, policy)`; the vendor stack is uniform per family:
  `<type>template → sitetemplate → site_setting → device-profile → device`). Adds
  the **sitetemplate** layer (fixes the latent baseline gap) and a **gateway
  compile** (`compile_gateway_device` = fold gateway layers → **per-key device
  overlay** → `_resolve` vars last; materialized back into `RawSiteState.devices`
  so the existing GS22 gateway IR/checks run unchanged; unmodeled gateway fields —
  routing/BGP/tunnels/security — stay field-gated → UNKNOWN). Role-keyed
  `check_derived` also screens gateway effective (source-aware projection) + a
  shared row-level DHCP-relevance helper (the 3×3 S/R/I participation matrix).
  Device-profile is a **coarse fail-safe gate** (a profiled modeled device whose
  own effective, restricted to the role's overridable leaves, differs vs the
  below-profile proposed → site UNKNOWN; can't diverge from the IR). Domain
  findings during build: gateway `dhcpd_config`/`networks` are gateway-namespace
  only (NOT inherited from site-level layers — confirmed with the owner); the OAS
  `site_template` component is thin (auto_upgrade/name/vars) but a real sitetemplate
  carries more, so L0 stays permissive. **Live-verified** read-only on the real org:
  8 single-site plans unchanged (a dhcpd_config `enabled`-flag crash was caught live
  + fixed), and a no-op gatewaytemplate org fan-out → SAFE with a consistent rollup.
  Two Tier-2 items observed consistent (gateway DICT_MERGE; gateway-device vars
  resolve). Spec/plan:
  docs/superpowers/{specs,plans}/2026-06-15-gateway-site-template-object-types*.md.
- 🔵 **device-profile as a modeled compile layer.** The derivation stack is
  `<type>template → sitetemplate → site_setting → device-profile → device`, and
  the twin does not model the **device-profile** layer (a pre-existing gap, true
  since M1). Because the profile *wins* over the template/site/sitetemplate
  layers, ignoring it can make an upper-layer template edit look more or less
  impactful than reality. Interim honesty (shipped with the
  gatewaytemplate/sitetemplate slice): a modeled switch/gateway device carrying a
  `deviceprofile_id` whose unknown content could override a leaf the edit changes
  forces that **site** → UNKNOWN (relevance-scoped — unrelated AP profiles /
  unaffected devices do not taint the org verdict). This item is to model the
  device-profile layer for real (fetch + fold it into `fold_layers` as the
  highest-precedence non-device layer) so those edits can resolve
  SAFE/REVIEW/UNSAFE instead of UNKNOWN.
- ✅ **template / org-object changes — DELETE-ripple + multiple templates per
  plan** — done 2026-06-17, live-verified. An org template `delete`
  (networktemplate / gatewaytemplate / sitetemplate) now fans out to its
  baseline-assigned sites: each loses that inherited layer (config collapse,
  `proposed is None` ⇔ layer absent) and is simulated through the existing
  per-site engine. A plan may carry MULTIPLE distinct org ops; a site assigned
  to more than one is simulated atomically with ALL applicable overlays pinned
  (Approach A "org overlays" → `OrgOverlay` / `apply_overlays`; `OrgVerdict`
  names the SET of changed objects via `OrgChange`). The gate relaxation +
  `simulate_org_plan` landed in ONE commit (no empty-payload-delete → false-SAFE
  window). A per-site ingest crash (e.g. an unresolvable gateway `{{var}}`) is
  contained as that site's UNKNOWN, never a hard crash. Distinct from Mist's
  attribute-delete (`{"-attr": ""}`) inside an `update`, which IS modeled
  (`effective_update` / `update_conflicts`, field gate "deleted vs changed").
  **Deferred:** other org objects (`org_networks`, WLAN/RF templates);
  site-reassignment (changing which template a site points at); the apply path;
  per-overlay source-aware gateway screening (a combined plan touching a
  gatewaytemplate currently fail-safes the whole site to UNKNOWN via
  `gw_full`).

## 4. Product / infrastructure (spec-deferred behind seams)

- 🔵 **apply module** — the write path (simulate→apply gate). The whole point;
  currently simulate-only.
- 🔵 SnapshotProvider backend — point-in-time state vs on-demand fetch.
- 🔵 declarative L1/L3 rule engine (`rules/` dir, spec-deferred).
- 🔵 additional vendor adapters (Aruba) via the `VendorAdapter` seam.
- 🔵 MCP server hardening for headless/cron use.

## 5. Open debt / hygiene

- ✅ **leaked fixtures in git history** — resolved 2026-06-11. Inventory: 4
  pre-signed S3 URL signatures + 2 STS key ids + 2 STS session tokens (1h TTL,
  expired 2026-06-10) and 4 Mist JWTs scoped to one device thumbnail (expired
  2026-06-10T16:32Z); NO password hashes or long-lived credentials found, so
  rotation was moot. History rewritten with `git filter-repo --replace-text`
  (all 12 values → `REDACTED-EXPIRED-HISTORY` across 102 commits; SHAs
  changed; pre-rewrite mirror kept at `../digital-twin-pre-rewrite-backup.git`
  — delete it after a sanity period, it still holds the expired values).
- ✅ templated-subnet false-SAFE in gateway_gap — resolved 2026-06-13
  (GS22-SUB). Added `Vlan.subnet_unresolved` (the twin of `gateway_unresolved`):
  a templated subnet now reads as unresolved-routed, not "not routed", and
  gateway_gap ABSTAINS (PARTIAL coverage note → REVIEW) instead of silencing.
  The five-leg precedence was GENERALIZED into one `_winning_literal(parse,
  same)` core (`_vlan_gateway` is now a thin wrapper, its tests the
  regression net); subnet mints through it with `_literal_subnet`/the new
  family-aware `same_subnet` comparator. The NON-WINNING same-vlan-id device
  row drop is closed too (the conflict→unresolved rule now covers subnet:
  a disagreeing sibling row, OR a silent winner shadowed by a declaring
  sibling, → unresolved). empty-string subnet `""` stays absent (no intent,
  no flag — never PARTIAL-floors ordinary subnet-less vlans). Goldens
  GS22-SUB a/b/c (templated removed-L3 REVIEW vs literal UNSAFE; nonwinning
  conflict REVIEW; unrelated-delta SAFE); all eight live plans unchanged.
- 🟡 redaction network-name joins — `name` VALUES are hashed (NAME_KEYS) but
  references to networks inside lists (gateway `port_config.networks`,
  `ip_configs` keys) are not, so the gateway↔org-network join breaks in
  redacted fixtures (carriage falls back to blind — honest but imprecise).
  Extend redaction to hash network-name references consistently before the
  next fixture capture.
- ✅ redaction entropy catch-all — backstop added (REDACTION_VERSION 6,
  2026-06-11): contiguous hex ≥36 chars (longer than any pseudonym the module
  mints) redacted unconditionally; base64-ish tokens ≥24 chars redacted when
  Shannon entropy ≥4.0 bits/char AND mixed case+digits. Applied LAST in
  `_sub_embedded`; own tokens (`redacted-*`/`uuid-*`/`name-*`) never re-trip
  (backstop idempotent); prose, port ranges, model names spared.

## 6. Visualization — topology charts of the findings

Spec: `docs/superpowers/specs/2026-06-17-topology-visualization-design.md`. v1
emits **mermaid** charts (L2, per-VLAN, Routed VLAN exits) on every verdict with a
proposed IR, with the blast radius severity-highlighted and `caused_by` in
captions; exposed as
`Verdict.diagrams` + a markdown helper for the elicitation UI.

- 🔵 **v1 — single annotated proposed-state charts** (the spec above). New
  `viz/` package; `Diagram` DTO in `contracts/`; `Device.name` added to the IR
  (also re-enables device-subject finding names). Node-only highlighting.
- 🔵 **Device display name in the IR** — `Device.name` from raw `name` at ingest
  (prerequisite of v1; standalone value: device-subject findings render the real
  name, not the MAC).
- 🟡 **Before/after & diff-overlay charts** — baseline-vs-proposed pairs, and a
  single diff chart (added=green / removed=red-dashed / affected=highlighted).
  Needs the baseline IR alongside the proposed.
- 🟡 **Visual cut-link rendering of `caused_by`** + edge/`linkStyle` coloring —
  draw the removed/changed link that caused a severance (needs baseline overlay;
  v1 only captions the cause and colors endpoint nodes).
- 🔴 **Image rendering (SVG/PNG)** — only if an elicitation surface can't render
  mermaid markdown; adds a headless-render dependency (breaks pure-Python).
- 🔵 **Org-level aggregate topology** — one cross-site view for org/template runs
  (v1 produces per-site diagrams only).
- 🟡 **Payload guard for very large sites** — configurable cap on per-VLAN charts
  (or lazy/on-demand generation) with a `notes` line for omitted charts; v1 emits
  the full set, affected-first.
- 🟡 **OSPF adjacency / routing-area view** — once the IR models that topology
  (today the L3 view is routed-VLAN ↔ serving-interface only).
