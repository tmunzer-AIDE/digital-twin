# `admin_disable` over-warning + the Mist `uplink` bit — Design

**Status:** Approved (design); plan pending
**Date:** 2026-06-26
**Author:** Thomas Munzer (with Claude)

## Problem

`wired.port.admin_disable` reports `WARNING` ("a trunk link goes down") for **any**
trunk port being disabled — even a trunk with **no modeled link and nothing
downstream**. On the live delta that motivated the `l2_isolation` fix, disabling
`mge-0/0/1` (a trunk that was already down, with no LLDP neighbour and no clients)
produced a spurious `WARNING`. Disabling an unconnected port has no impact and
should be `INFO`.

Mist exposes an authoritative `uplink` boolean on each switch **port stat** (seen
live: the backbone `xe-0/1/3` had `uplink: true`; every disabled leaf `mge` port
had `uplink: false`). It is already fetched — `RawSiteState.port_stats` carries it
— but not modeled. It is exactly the signal needed to tell a real uplink apart
from an edge/leaf or an unconnected trunk, so a disabled trunk can be demoted to
`INFO` **without** ever under-warning a genuine uplink.

## Design

### Part 1 — `Port.is_uplink` fact + ingest (independent of STP)

Add `Port.is_uplink: bool | None = None`, an **observational** fact (OBSERVED
provenance) mirroring `Port.stp_state`. `None` = not observed (no port-stat row);
`True`/`False` = Mist's classification.

In `adapters/mist/ingest/lldp.py`, the existing stats-annotation pass
(`_apply_stp`) **skips rows that have no `stp_state`** (`lldp.py:98`). The `uplink`
bit must NOT hide behind that guard — a port can carry `uplink` with no STP row.
So apply it independently: add `_apply_port_uplink(ctx)` (a sibling pass that, for
each `port_stats` row with a `port_id` and a non-None `uplink`, sets
`Port.is_uplink = bool(row["uplink"])`), or generalize the annotation pass so each
row annotates whatever facts it carries. **Capability earning is unchanged:** only
`stp_state` earns `IRCapability.STP_STATE`; `is_uplink` earns nothing (it is a
weighting hint, not a capability gate).

### Part 2 — Diff isolation (explicit)

`Port` fields are diffed by default; only `meta`/`stp_meta` are in
`_IGNORED_FIELDS`, and `stp_state` IS diff-bearing today. `is_uplink` is
evidence-only — a change in the observed uplink bit is **not** a config change and
must never wake a check. Add it to `_IGNORED_BY_KIND` in `ir/diff.py`:

```python
_IGNORED_BY_KIND: dict[str, frozenset[str]] = {
    "device": frozenset({"name"}),
    "wlan": frozenset({"inherited"}),
    "bgp_peer": frozenset({"session_name"}),
    "port": frozenset({"is_uplink"}),
}
```

### Part 3 — `admin_disable` reordered classification

Today the trunk branch (`admin_disable.py:164`) is a catch-all that fires before
the modeled-link (`nonap_peers`) check, so every trunk gets `WARNING` at a blanket
`_HIGH`, and a linked trunk never gets its link's confidence. Reorder so the
modeled link is evaluated first and the uplink bit gates the trunk fallback. After
the existing AP-port and wired-client checks (unchanged), classify in this order:

1. `peer_lk = nonap_peers.get(pid)` is not None → **WARNING**, confidence =
   `peer_lk.meta.confidence` (a one-sided LLDP peer is weaker evidence than a
   two-sided one — honest about the link).
2. `base_port.mode is TRUNK` and `base_port.is_uplink is True` → **WARNING**
   (Mist says it faces the core), confidence `_HIGH`.
3. `base_port.mode is TRUNK` and `base_port.is_uplink is None` → **WARNING**,
   confidence `_HIGH` — conservative: the bit is unknown, don't quiet it.
4. `base_port.mode is TRUNK` and `base_port.is_uplink is False` (no peer/AP/client
   — all already excluded above) → **INFO**, code `wired.port.admin_disable.edge`,
   message "trunk port with no modeled uplink or downstream — no impact".
5. Edge access fallback → **INFO** (unchanged).

Only case 4 is new behaviour: a demotion that requires **explicit** `uplink is
False` **and** no modeled peer link (and, already known at this point, not an AP
port and no wired clients).

## Never-false-SAFE

`admin_disable` is a NETWORK check, so demoting `WARNING`→`INFO` changes the
verdict (`REVIEW`→`SAFE` for that finding). The demotion is gated on **positive**
evidence of harmlessness: `is_uplink == False` AND no modeled peer link AND no AP
AND no wired clients. When the bit is **absent** (`None`) — the port wasn't in the
stats — the check stays at today's conservative `WARNING` (case 3). So a real
uplink the LLDP link-modeling missed is still caught by the bit, and an unknown
port is never silently quieted.

## Files touched

- `src/digital_twin/ir/entities.py` — add `Port.is_uplink: bool | None = None`.
- `src/digital_twin/ir/diff.py` — add `"port": frozenset({"is_uplink"})` to
  `_IGNORED_BY_KIND`.
- `src/digital_twin/adapters/mist/ingest/lldp.py` — apply the `uplink` bit
  independently of the STP guard.
- `src/digital_twin/checks/wired/admin_disable.py` — reordered classification
  (Part 3); update `_nonap_peer_links` use only if needed; update the module
  docstring's per-case summary.

## Testing

- **Ingest:** a `port_stats` row with `uplink: true` (and no `stp_state`) sets
  `Port.is_uplink is True` and earns **no** `STP_STATE` capability; a row with
  `stp_state` but no `uplink` still sets `stp_state` and leaves `is_uplink` None.
- **Diff isolation:** two IRs differing only in `Port.is_uplink` produce an empty
  `diff_ir` (no `port` modification) — so the change does not wake any check.
- **admin_disable demotion (the headline):** a disabled trunk with `is_uplink is
  False`, no peer link, no AP, no clients → `INFO` `admin_disable.edge`.
- **admin_disable conservative paths (never-false-SAFE):** a disabled trunk with
  (a) a modeled peer link → `WARNING` at the **link's** confidence; (b) `is_uplink
  is True` → `WARNING`; (c) `is_uplink is None` → `WARNING`. None demoted.
- **Existing `tests/checks/test_admin_disable.py` stay green** — verify none
  asserted the old blanket-`_HIGH` for a *linked* trunk; if one did, that
  expectation legitimately changes to the link's confidence (update it and note
  why). The AP-port and wired-client cases are untouched.

## Scope and deferred

In scope: `Port.is_uplink` modeling + the `admin_disable` reorder/demotion. The
other two co-conspirators from the same live delta remain their own follow-ups:
`vlan_segmentation.split` firing when a leaf AP leaves a broadcast domain, and
`blackhole.exit_unlocatable`'s exit-location coverage gap (where the uplink bit
may later help locate the exit by following uplinks toward the gateway).
