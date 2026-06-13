# GS22-SUB — Templated-subnet false-SAFE: routed-but-unreadable intent (design)

Date: 2026-06-13
Status: approved (generalize the winning-literal core; mirror gateway_unresolved)
Debt mapping: closes ROADMAP §5 "templated-subnet false-SAFE in gateway_gap"
(the twin of GS22-GW's `gateway_unresolved`).

## Problem

`Vlan.subnet` is the routed-intent signal: `wired.l3.gateway_gap` treats a
vlan with a subnet as ROUTED (something must own its L3 interface) and a
vlan with `subnet=None` as NOT routed (silent). The subnet is minted at
`ingest/switch.py:404` as `net.get("subnet") or org_subnets.get(vid)`:

- `org_subnets` is pre-filtered through `_literal_subnet`, so a TEMPLATED
  org subnet (`{{vlan_subnet}}`) never lands — it reads as absent.
- the winning effective row's `net.get("subnet")` is raw and un-flagged: a
  site row rendered to `""` falls through the `or` to the (templated →
  absent) org overlay.

Either way a vlan whose only subnet declaration is templated ends up
`Vlan.subnet=None` and gateway_gap reads it as NOT routed — silencing
`.removed`/`.unserved`/`.unowned` for that vlan. Routed-but-unreadable is
indistinguishable from not-routed: a false-SAFE. This is exactly the gap
GS22-GW closed for `networks.*.gateway` with `Vlan.gateway_unresolved`;
the design review that round deferred the subnet twin to this debt entry.

There is a second, same-root shape (the GS22-GW singleton-Vlan limitation):
`networks.*.subnet` is allowlisted on device ops too, so a device-effective
row for an ALREADY-SEEN vlan id can declare a subnet the singleton `Vlan`
silently drops — the op passes the gate, the IR never changes. GS22-GW
pinned the conflict→unresolved rule for gateway; subnet gets the same.

## IR addition

- `Vlan.subnet_unresolved: bool` — declared-but-unreadable subnet intent,
  mirroring `Vlan.gateway_unresolved` and `DhcpScope.subnet_unresolved`.
  **Absent/empty subnet = no intent = NOT a blind spot** (the
  `DhcpScope.subnet_unresolved` contract: a blanket note would PARTIAL-floor
  every ordinary subnet-less network). Only an unreadable (templated) subnet
  on the precedence winner — or an unresolvable non-winning-row conflict —
  sets the flag.

## Shared winning-literal core (the approved generalization)

`_vlan_gateway` and the new subnet mint share an identical five-leg
precedence. Factor the skeleton into one helper parameterized by the parser
and the comparator; both fields become thin call sites. This removes the
hand-sync drift that already bit GS22-GW (the mint loop used raw `int(vid)`
while the row collection used `_vlan_int`).

```
_winning_literal(vid, rows_by_vid, org_raw, *, parse, same) -> (str|None, bool)
  rows    = rows_by_vid.get(vid, [])          # raw value per row, source order
  declare = [r for r in rows if r is not None] # declared = value is not None
  if not declare:
      if vid not in org_raw: return None, False         # no intent
      lit = parse(org_raw[vid]); return lit, lit is None # org overlay; templated→unresolved
  if rows[0] is None:  return None, True   # silent winner, sibling declares → ambiguous
  win = parse(rows[0])
  if win is None:      return None, True   # unreadable winner shadows org
  for other in declare[1:]:
      if same(parse(other), win) is not True: return None, True  # conflict → unresolvable
  return win, False
```

- `_vlan_gateway` ⇒ `_winning_literal(vid, gw_rows, org_gw_raw, parse=_literal_ip, same=same_ip)`.
  Its existing tests re-run against the shared core — the regression net.
- subnet ⇒ `_winning_literal(vid, subnet_rows, org_subnet_raw, parse=_literal_subnet, same=same_subnet)`.

The declared-predicate stays uniform (`r is not None`); subnet's
empty-string-is-absent rule is applied **at collection**, not in the core:
- `subnet_rows_by_vid[vid].append(net.get("subnet") or None)` — `""` → None
  (absent), a templated `"{{var}}"` stays (truthy) → declared-unreadable.
- `org_subnet_raw.setdefault(vid, net.get("subnet"))` only when that value
  is truthy (raw, so templated stays distinguishable from absent — FIRST-wins
  via `setdefault`, byte-identical to how `org_gw_raw` and `org_subnets` are
  built today; a duplicate org row for the same vlan id never overwrites).

Line 404's `subnet=net.get("subnet") or org_subnets.get(vid)` is replaced by
the helper call; `org_subnets` (the `_literal_subnet`-filtered map) is
retired in favour of `org_subnet_raw` fed to the core.

## Comparator — `same_subnet` (new, beside `same_ip`)

`same_subnet(a: str | None, b: str | None) -> bool | None` in
`ir/ip_match.py`:
- CIDR-normalize each side with `ipaddress.ip_network(str(x), strict=False)`
  (tolerates host bits set: `10.0.10.5/24` == `10.0.10.0/24`; a bare host
  address becomes /32 or /128).
- FAMILY-AWARE: mismatched versions are simply not equal (the GS25 lesson —
  never int-compare across families).
- None when either side is None or unparseable: comparison UNKNOWN, never a
  guessed equality. Used only by the non-winning-row conflict rule.

## Consumer — `wired.l3.gateway_gap`

The existence loop's `if vlan.subnet is None or prop_l3.get(vid): continue`
splits so unresolved routed intent ABSTAINS instead of silencing:

```
if prop_l3.get(vid): continue                  # served — positive fact
if vlan.subnet is None:
    if vlan.subnet_unresolved and relevant(vid):
        subnet_abstain_notes.append(
          f"vlan {vid}: declared subnet is unreadable or ambiguous — "
          "routed intent cannot be verified")
    continue                                    # not routed, or abstained
```

- `relevant(vid)` = vid in the delta's changed vlan ids OR an l3intf for that
  vid was touched — the SAME relevance machinery the `.gateway_unowned`
  abstain uses (hoisted above both loops). An unchanged unresolved vlan
  elsewhere never taints (GS25 relevance discipline).
- The abstain note attaches to `Coverage.notes` → PARTIAL → floors REVIEW.
  Never a `.removed`/`.unserved` finding (subnet is None — routed-ness is
  unproven, a violation would be fact creation over a blind spot).
- `.gateway_unowned` is unaffected (it reads `gateway`/`gateway_unresolved`,
  not `subnet`); a vlan with unresolved subnet but present L3 interfaces
  still runs ownership normally.
- `requires()`/`applies_to()` unchanged ({WIRED_L2, L3_EXITS}; subnet is a
  vlan field, already watched).

`wired.dhcp.scope_lint.out_of_subnet` reads `DhcpScope.subnet` /
`subnet_unresolved` (already flagged in GS25) — no change.

## Goldens (filed under GS22)

- **GS22-SUB-a** — routed vlan whose ONLY subnet declaration is templated
  (`{{...}}`), op removes its modeled L3 interface → today false-SAFE (SAFE),
  now **REVIEW**: subnet unresolved → no `.removed` ERROR (routed-ness
  unproven) → abstain note PARTIAL-floors.
- **GS22-SUB-b** — device op introduces a subnet on an already-seen vlan id
  that DISAGREES with the winner's literal (`same_subnet is not True`) →
  `Vlan.subnet` flips to `None` + unresolved → diff fires → abstain →
  **REVIEW**. Resolving the conflict restores the literal.

Two five-leg legs collapse a non-winning row into unresolved; both get a
pinned INGEST unit test (mirroring GS22-GW's `_vlan_gateway` suite):
- **literal-disagreement** — winner declares subnet X, a later row declares
  Y, `same_subnet(Y, X) is not True` → unresolved (the GS22-SUB-b shape).
- **silent-winner-shadowed** — the WINNER row declares NO subnet (`rows[0] is
  None`) while a later device row DOES declare one (`rows[0] is None` leg) →
  unresolved, never a silent adoption of the non-winner's value. This is the
  twin of `test_silent_winner_with_declaring_nonwinning_row_is_unresolved`
  and pins the distinct false-SAFE the prose at the helper's leg-2 describes.
- **GS22-SUB-c** (control) — templated subnet pre-existing in baseline,
  delta touches an UNRELATED vlan → the templated vlan is not in the delta →
  no note → **SAFE** (relevance discipline holds; no global taint).
- Live verification: all eight plans hold their verdicts (the live org's
  routed vlans carry literal subnets; none templated).

## Honesty rails summary

| Blind spot | Behavior |
|---|---|
| Declared subnet templated on the winner | `subnet_unresolved` → abstain + note only when vlan touched/conclusion-relevant |
| Non-winning same-vlan row declares a disagreeing subnet | conflict → `subnet=None` + unresolved (singleton cannot represent it) |
| Subnet absent or empty `""` | no intent — NOT a blind spot, no note (would PARTIAL-floor ordinary subnet-less vlans) |
| Mixed families / host-bits-set subnet shapes | `same_subnet` normalizes via `ip_network(strict=False)`, family-aware |
| Org namespace unfetched | org overlay absent → `subnet=None`, no flag (no intent observed — existing org-overlay discipline) |

## Out of scope (recorded, not built)

- Redaction network-name joins (ROADMAP §5, the next sequential round).
- Subnet-containment / overlap lint across vlans (GS25 already lints the
  DHCP-handed gateway against its scope subnet; vlan-vs-vlan subnet hygiene
  is a separate config-lint tier item).
