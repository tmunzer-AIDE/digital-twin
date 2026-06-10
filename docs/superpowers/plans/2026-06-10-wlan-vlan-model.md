# Fix B — model WLAN→VLAN so AP VLAN needs are known from config

Context: real-use false-SAFE (see memory `wireless-vlan-observation-gap`). Fix A
(done) floors AP-uplink VLAN severance to REVIEW via a coverage note when there
are no observed clients. Fix B makes the AP's VLAN needs KNOWN FROM CONFIG, so
the verdict is precise (UNSAFE when a needed VLAN with an exit is severed) and
covers idle WLANs and null-`vlan_id` clients.

## Model (grounded in the live `site_wlan` schema)

An enabled WLAN requires its VLAN(s) delivered on the wired uplink of every AP it
applies to — IFF it is locally bridged. Per WLAN:
- skip unless `enabled` and `vlan_enabled` (untagged WLANs ride the native).
- skip unless `interface` ∈ {`all`,`eth0`,`eth1`,`eth2`,`eth3`} (tunnelled =
  `mxtunnel`/`site_mxedge`/`wxtunnel` → local switch VLAN irrelevant).
- required vlans = integer `vlan_id`, or `vlan_ids` (pool), or the static
  candidate pool inside `dynamic_vlan` (RADIUS still trunks every candidate).
- applies-to: `apply_to`==`site` → all site APs; `apply_to`==`aps` → `ap_ids`.

UNRESOLVABLE → coverage note (REVIEW, never a false SAFE / false UNSAFE):
`apply_to`==`wxtags` (group match not modelled), non-integer/var-template vlan,
org-template-sourced WLANs. Recorded per-AP so the check can note it.

## Honest-boundary integration

The AP becomes a **config-based member** of each required VLAN (third basis
alongside config access-ports and observed wireless clients). The existing
blackhole member-strand + attribution logic then does the work:
- severed VLAN with a HIGH exit → `exit_lost` → UNSAFE.
- severed exit-less VLAN → `exit_unlocatable` (delta-touched) → REVIEW.
- pre-existing exit-less strand, delta untouched → INFO context (no flood).

## Tasks (each TDD)

1. `adapters/mist/ingest/wlan_vlans.py` — pure resolver: (wlans, site APs,
   networks) → (resolved: ap_id→frozenset[int], unresolved: ap_id→reasons).
2. provider contract: `RawSiteState.wlans=()`, `IRCapability.WLAN_CONFIG`,
   loader `.get("wlans", ())`, redaction `_RAW_FIELDS`, mist_api `listSiteWlans`.
3. IR carries `ap_wlan_vlans` + `ap_wlan_unresolved`; builder setters.
4. `WlanIngester` wires resolver → IR; registered in `MistAdapter`.
5. vlan graph + reachability: config AP membership (`wlan_members`).
6. blackhole check: `wlan_members` in membership + attribution + unresolved note.
7. golden GS10: AP WLAN on exit-bearing VLAN severed (no clients) → UNSAFE;
   exit-less variant → REVIEW; unresolved (wxtag) → REVIEW note.
8. wrap: README scope line, allowlist note, fixture.
