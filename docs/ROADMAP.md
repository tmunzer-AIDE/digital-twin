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
- 🔵 **Default gateway gap** (part of GS22, MVP: ROUTE-GW) — IN PROGRESS
  (spec approved: docs/superpowers/specs/2026-06-12-gs22-default-gateway-gap-design.md).
  The NO-L3-interface form already shipped with GS22 (`wired.l3.gateway_gap`
  `.removed`/`.unserved`); what remains is OWNERSHIP of the declared gateway
  IP: `gateway_gap.gateway_unowned` (interfaces exist but none owns
  `networks.*.gateway` — known-owner-broken → ERROR, never-owned →
  WARNING/MEDIUM) + `scope_lint.gateway_mismatch` (DHCP hands out a gateway
  incoherent with its owning network). New IR: `Vlan.gateway(+_unresolved)`,
  `DhcpScope.network_gateway(+_unresolved)`; shared family-aware `same_ip`
  helper.
- 🔵 **OSPF exit withdrawal** (GS26) — `ospf_areas` / interface ospf config:
  withdrawing the area or interface that is a segment's L3 exit → UNSAFE.
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
- 🔵 multi-site / org-template simulation (the `fetch_sites` org-batch path and
  template inheritance exist; the pipeline is single-site).
- 🔵 networktemplate / sitetemplate as first-class `object_type`s (today only
  `site_setting` + `device`).

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
- 🟡 templated-subnet false-SAFE in gateway_gap — `Vlan.subnet` is minted
  without `_literal_subnet` discipline in the site path and a TEMPLATED
  subnet reads as None = "not routed", silencing every gateway_gap code for
  that vlan (and the `or`-fallback (ingest/switch.py ~350) lets a falsy site value fall to
  the org overlay). Needs a `subnet_unresolved`-style flag on Vlan
  (declared-but-unreadable ≠ no intent) — surfaced during the GS22-GW
  design review 2026-06-12; deliberately NOT bundled into that round.
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
