# L2 Isolation Over-Severance — Design

**Status:** Approved (design); plan pending
**Date:** 2026-06-26
**Author:** Thomas Munzer (with Claude)

## Problem

On a real delta — disabling four `mge` ports on an EX4000 access switch
(`DNT-NTR-SWB-3`, device `2093390b3580`, site `d6fb4f96-…`) — the twin reports
`wired.l2.isolation.severed` for **the entire surviving network**
(`{DNT-NTR-SWB-2, DNT-NTR-SWB-3, DNT-NTR-APE, DNT-NTR-APT, …}`), which the
approval UI then paints amber across nearly every node.

Live Mist data shows this is physically false. The four disabled ports are all
**leaves**:

| Port | Real neighbor (LLDP) | Kind |
|------|----------------------|------|
| `mge-0/0/0` | `DNT-NTR-APB` (AP, 2 clients) | AP uplink |
| `mge-0/0/1` | none, already **down** | dead port (no-op) |
| `mge-0/0/2` | one wired client | access |
| `mge-0/0/3` | a 2nd AP (`003e7316ff9e`) | AP uplink |

SWB-3's actual uplink to the rest of the network is **`xe-0/1/3`** (10G fiber,
`port_usage: backbone`, STP root port) → `DNT-NTR-SWB-2`, and **that port is not
in the change**. So disabling the four `mge` ports can only strand the leaf AP +
2nd AP + wired client. Everything else keeps its path. The correct result is:
flag the cut-off leaves, leave the survivors quiet.

## Root cause

`wired.l2.isolation` flags a proposed L2 component as "severed" when it is a
**strict subset** of its baseline component and holds occupants
(`l2_isolation.py`):

```python
baseline_home = next((b for b in base_comps if fragment & b), None)
if baseline_home is None or not (fragment < baseline_home):
    continue
```

When a baseline component splits into `[big survivor, small cut-off]`, **both**
sides are strict subsets of the original combined component, so **both** are
flagged — including the surviving majority that merely shed some leaves. The
check is deliberately **exit-agnostic** (its docstring: "the severance itself
needs no exit … a structural fact on the PHYSICAL L2 multigraph"), which is
exactly why it cannot tell the survivor from the cut-off side: with no exit
anchor, "my component shrank" is conflated with "my component got isolated."

This is upstream of the severity/attribution layers — the engine computes the
wrong impacted set, so no amount of presentation work corrects it.

## Design

When a baseline L2 component fragments in the proposed graph, decide which
fragment(s) are **home** and flag only the rest.

### Anchor set

A new helper in `analysis/exits.py`, reusing the exact two exit kinds
`resolve_exit` already recognizes, lifted from per-VLAN to the physical graph:

```python
def exit_anchor_nodes(ir: IR) -> set[str]:
    """Graph nodes that ARE a network exit: gateway-role devices, or devices
    owning a routed IRB/SVI. A fragment containing one of these still reaches an
    L3 exit and is therefore NOT L2-isolated."""
    vc = vc_root_map(ir)
    anchors = {
        node_for(vc, d.id) for d in ir.devices.values() if d.role is DeviceRole.GATEWAY
    }
    anchors |= {
        node_for(vc, i.device_id) for i in ir.l3intfs if i.role in (L3Role.IRB, L3Role.SVI)
    }
    return anchors
```

(`WAN`/`LOOPBACK`/`GATEWAY` `L3Role`s are excluded: a loopback is not an exit,
and gateway-side L3 interfaces already belong to a `DeviceRole.GATEWAY` device
counted above.)

### The home/severed rule

```
anchors = exit_anchor_nodes(ir)
for each baseline component B (with occupants):
    fragments = proposed components overlapping B
    if len(fragments) == 1 and fragments[0] == B:   # B unchanged
        continue
    if anchors & B:                       # B had an exit
        home = { F in fragments : F & anchors }     # every exit-keeping side
    else:                                  # exit-less B — original motivating case
        home = { the single largest fragment }      # majority fallback
    for F in fragments:
        if F in home:           continue
        if not occupants(F):    continue   # an empty segment going dark is not impact
        emit wired.l2.isolation.severed(F)  # only the genuinely cut-off side
```

Two edge cases, both resolving toward the never-false-SAFE direction:
- **The exit itself was severed.** If `anchors & B` is non-empty but **no**
  proposed fragment retains an anchor (the exit device was itself cut off),
  `home` is empty and every occupant fragment is flagged — the correct,
  conservative outcome (everything genuinely lost its exit).
- **"largest fragment"** in the fallback is defined deterministically: most
  occupants, tie-broken by node count, then by sorted node id — so the choice is
  stable across runs.

Occupant counting, the confidence calc (MIN over the severed boundary links),
and the per-fragment subject/message are **unchanged**. The structural change is
solely *which* fragments are eligible: today every strict-subset fragment; now
every fragment that is neither home nor empty.

### Behavior on the two cases

- **The reported topology:** `B` = the whole L2 domain, which contains SWB-2/SWB-3
  IRBs **and** the SSR400C gateway. It fragments into `[survivor (keeps IRBs +
  gateway), {APB}, {2nd AP}, {wired client}]`. `home` = the survivor. Only the
  three leaf fragments with occupants are flagged. **Survivors go quiet.**
- **The original exit-less case** (disable a switch's only uplink, no modeled L3
  exit — the scenario the check was written for): `anchors & B` is empty →
  majority fallback → `home` = the surviving majority, and the cut-off
  switch+downstream is still flagged. **No regression.**

### Never-false-SAFE guard

The CARDINAL RULE is never to hide a real breakage. This change only ever
**drops** a fragment that:
1. **itself contains an exit anchor** — it has a real local L3 exit, so it is by
   definition not L2-isolated (it can still route); or
2. in the **exit-less fallback only**, is the **largest** fragment.

Case 1 is exit-grounded and cannot conceal a genuine cut-off. Case 2 is confined
to the legacy no-exit-modeled path and is strictly *less* aggressive than today's
"flag everything"; its one documented limitation is a degenerate split where the
disabled port cuts off a majority — there the largest (cut-off) fragment would be
mislabeled home. This is accepted: it only occurs when the IR models no exit at
all, and it never makes the result worse than the current behavior.

## Verdict impact

This is an intentional verdict change. For deltas that disable leaf ports on an
exit-anchored domain, `isolation.severed` drops from "the whole domain" to "the
cut-off leaves," so the flood of NETWORK WARNINGs that floored REVIEW collapses
to the genuinely-impacted set. Severity mechanics (`ERROR if HIGH-confidence
else WARNING`) are unchanged; a genuine confident severance of a real cut-off
segment still reaches UNSAFE.

## Files touched

- `src/digital_twin/analysis/exits.py` — add `exit_anchor_nodes(ir) -> set[str]`.
- `src/digital_twin/checks/wired/l2_isolation.py` — replace the per-fragment
  strict-subset loop with the home/anchor rule. No change to occupants,
  confidence, subject, or message construction.
- Tests + a golden fixture (below).

## Testing

1. **Headline golden (the reported topology).** An L3 access switch owning an
   IRB, with a backbone uplink to a core that holds the gateway/IRBs, plus leaf
   AP + wired-client ports. Disable the leaf ports → assert `isolation.severed`
   fires **only** for the leaf fragments, and the survivor nodes (core, peer
   switches, their APs) appear in **no** `isolation.severed` finding.
2. **Regression — exit-less case.** Disable a switch's only uplink with no
   modeled L3 exit → the cut-off switch+downstream is **still** flagged via the
   majority fallback.
3. **Never-false-SAFE.** A genuine severance that strands an occupant segment
   which loses its only exit → still flagged.
4. **Both-sides-keep-an-exit.** A split where each side retains an anchor →
   neither side flagged.
5. **Existing `tests/checks/test_l2_isolation*` stay green** — verify none relied
   on the old survivor-flagging; update only those that asserted the bug (none
   expected).

## Scope and deferred

In scope: the `l2_isolation` over-severance only. Three smaller contributors seen
on the same live delta are **explicitly deferred** to their own analysis:
- `admin_disable` flags an already-down, neighbor-less port (`mge-0/0/1`) as
  "a trunk link goes down" — should be a no-op/INFO.
- `vlan_segmentation.split` fires when a leaf AP leaves a broadcast domain.
- `blackhole.exit_unlocatable` for VLANs whose exit the model cannot locate (an
  exit-location coverage gap, distinct from the survivor bug).
