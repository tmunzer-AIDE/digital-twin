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
  the WLAN→VLAN story; it makes the *original* reported bug (AP-uplink
  trunk→access) a precise UNSAFE naming the SSIDs. **← recommended next.**
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

- 🔵 **VLAN ID collision** (GS30, MVP: CFG-VLAN) — one vlan_id claimed by
  multiple networks → forwarding ambiguity. NOTE: today's vlan ingest silently
  dedups (`seen` set) — a collision would fold invisibly; the check must read
  the raw networks maps.
- 🔵 **IP subnet overlap** (GS31, MVP: CFG-SUBNET) — pairwise overlap across
  networks/other_ip_configs subnets. Needs subnets in the IR (rides the
  richer-L3 work).
- 🔵 **Duplicate SSID** (GS32, MVP: CFG-SSID) — same SSID on multiple enabled
  WLANs of the site; derived WLAN list is already fetched
  (`RawSiteState.wlans`).
- 🔵 **Open guest SSID without isolation** (GS33, MVP: SEC-GUEST) — open auth
  + no client isolation → lateral traffic; same WLAN data.

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
- 🔵 **OSPF transit changes** (GS27, MVP: ROUTE-OSPF) — passive/metric changes
  on a transit interface → REVIEW; with live telemetry: peer IPs no longer
  reachable within predicted interface subnets → adjacency break.
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
- 🟡 **gatewaytemplate / sitetemplate** as first-class `object_type`s
  (spec'd 2026-06-15, in progress). Generalizes the networktemplate fan-out to a
  **typed** set `{networktemplate, gatewaytemplate, sitetemplate}` over a unified
  layered effective-config compiler (`fold_layers(layers, policy)`). The vendor
  derivation is one uniform stack for every device family:
  `<type>template → sitetemplate → site_setting → device-profile → device`. Adds
  the **sitetemplate** layer (fixes the latent baseline gap for sites already
  assigned one) and a **gateway compile** (gatewaytemplate folded under the device
  via the PUT-root overlay → existing GS22 gateway IR/checks reused, no parallel
  path; unmodeled gateway fields — routing/BGP/tunnels/security — stay field-gated
  → UNKNOWN). Org fan-out pins exactly one edited layer per plan. Device-profile is
  the named out-of-scope layer below (relevance-scoped UNKNOWN, not silent).
  Spec: docs/superpowers/specs/2026-06-15-gateway-site-template-object-types-design.md.
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
- 🔵 **template / org-object changes — DELETE + the wider ripple.** A template
  `delete` (object deletion) and a non-`update` action are still rejected
  pre-fetch → UNKNOWN (`object_gate`; fails safe, test-pinned). Modify-ripple is
  now DONE for networktemplate (above); delete-ripple, gateway/site templates,
  multiple templates per plan, and other org objects (org_networks, WLAN/RF
  templates) remain. **Gateway templates are a wider gap — gateways aren't a
  compile target, so there is no template-merge path on that side at all.**
  Distinct from Mist's attribute-delete (`{"-attr": ""}`) inside an `update`,
  which IS modeled (`effective_update` / `update_conflicts`, field gate
  "deleted vs changed").

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
