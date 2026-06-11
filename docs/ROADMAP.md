# Roadmap / backlog

The single place for "what's next." Status: ✅ done · 🔵 in scope, not started ·
🟡 needs a decision · 🔴 open debt. M1 (one site, switch L2, Wi-Fi-aware client
impact) is **done**; everything below is post-M1. Ordered by leverage, grouped
by kind.

## 1. Precision — turn honest REVIEWs into precise verdicts (real-use driven)

These are the gaps that made real changes resolve to REVIEW/MEDIUM instead of a
sharp UNSAFE during live testing on the Live-Demo site.

- 🔵 **wxtag WLAN scoping** — resolve which APs a `apply_to: wxtags` WLAN
  applies to (evaluate wxtag membership against AP model/name/etc.). Today these
  WLANs are recorded `unresolved` → REVIEW. This is the last unmodeled piece of
  the WLAN→VLAN story; it makes the *original* reported bug (AP-uplink
  trunk→access) a precise UNSAFE naming the SSIDs. **← recommended next.**
- ✅ **PoE impact** — `poe_disabled` is now modeled (`Port.poe` config intent +
  `Port.poe_draw` observed from stats `poe_on`); `wired.poe.disconnect` fires
  UNSAFE when a port that powers an LLDP-confirmed AP or an observed-drawing
  device loses PoE. Verified live: `plan.json` now → UNSAFE naming the exact
  APs and their client counts (was UNKNOWN). [done 2026-06-10]
- 🔵 **Richer L3 exit modeling** — many verdicts cap at MEDIUM because the only
  exit is a `boundary_uplink` over an assumed-carriage edge (no IRB on the
  switches; L3 lives on the SRX). Model the gateway/SRX side and the neighbor
  switch's downlink config so VLAN-2-class exits resolve at HIGH.
- 🔵 **Dynamic profiles on neighbor switches** — the core's downlink to an IDF
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
- 🔵 MTU mismatch across a link.
- 🔵 STP topology: root-bridge change, a configured `stp_edge` on an uplink.
- 🔵 loop check FAIL path — currently maxes at WARN because Mist live data never
  asserts STP *disabled*; revisit if a config source for that appears.

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

- 🔴 **leaked fixtures in git history** — early redaction rounds committed
  password hashes / a pre-signed URL / JWTs before the redactor caught them.
  Repo has no remote (local-only exposure). Decide `git filter-repo` history
  rewrite vs credential rotation **before any push**.
- 🟡 redaction entropy catch-all — current redactor is key-name + known-pattern
  based; a high-entropy-value backstop would catch unknown secret shapes.
