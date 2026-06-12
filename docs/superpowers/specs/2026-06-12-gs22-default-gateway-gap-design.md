# GS22-GW — Default gateway gap: ownership + DHCP coherence (design)

Date: 2026-06-12
Status: approved (Approach A — extend the two existing checks)
MVP mapping: ROUTE-GW (the explicit-check form; closes the roadmap's
"Default gateway gap" entry)

## Problem

The GS22 `wired.l3.gateway_gap` check tests only the EXISTENCE of a modeled
L3 interface on a routed vlan. The declared default-gateway IP
(`networks.*.gateway` — allowlisted since GS22, consumed nowhere) is
ignored, so two hazards pass SAFE today:

1. **Ownership break** — the delta moves a network's declared gateway IP to
   an address no modeled L3 interface owns (or changes/removes the owning
   interface's IP) while SOME interface still exists on the vlan: clients'
   configured next-hop becomes a black hole, the existence check stays
   green.
2. **DHCP/network incoherence** — a DHCP scope hands out a `gateway`
   different from its owning network's declared gateway: new leases route
   through the wrong (possibly dead) next-hop.

## Ownership split (the user's boundary rule)

- `wired.l3.gateway_gap` owns "does any modeled L3 interface OWN the
  declared gateway?" — routed-intent semantics.
- `wired.dhcp.scope_lint` owns "does the DHCP scope hand out a gateway
  COHERENT with its owning network?" — scope hygiene, baseline-demotable
  lint.
- A new check file was rejected: it would duplicate `_l3_by_vlan`, the
  blind-gateway capping, and the DhcpScope parity rails. Adapter-finding
  placement was rejected: baseline-blind (GS25 r1 lesson).

## IR additions

- `Vlan.gateway: str | None` — declared default-gateway IP, minted exactly
  like `Vlan.subnet`: site-networks value with org-networks overlay
  fallback by vlan id, `_literal_ip` parsing (templated `{{var}}` → None).
- `Vlan.gateway_unresolved: bool` — declared-but-unreadable (templated),
  mirroring `DhcpScope.subnet_unresolved`: absent gateway = no intent =
  NOT a blind spot; only unreadable intent sets the flag. (The org overlay
  contributes: org value templated while site declares none → flag set.)
- `DhcpScope.network_gateway: str | None` + `network_gateway_unresolved:
  bool` — the OWNING network's declared gateway resolved in the PROVIDER's
  namespace (org networks for gateway scopes, site networks for site
  scopes — the same namespace discipline as `DhcpScope.subnet`; unfetched
  org namespace → None + unresolved True, exactly like subnet).

## Shared IP-equality helper

One small utility in a new module `checks/wired/ip_match.py` (NOT a new
check; `link_boundary.py` stays L2-boundary-only):
`same_ip(a: str | None, b: str | None) -> bool | None`.
- Tolerates `/prefix` suffixes on either side (`10.0.0.1` == `10.0.0.1/24`).
- FAMILY-AWARE (the GS25 lesson: never compare bare ints across v4/v6 —
  mismatched families are simply not equal).
- Returns None when either side is None or unparseable: comparison UNKNOWN,
  never a guessed equality or inequality.
Both checks use it; the L3Intf `ip` field arrives in mixed shapes
(`ip_configs.ip` bare, IRB `other_ip_configs.ip` bare, inferred gateway
intfs carry the org network's `gateway` value verbatim).

## Check change 1 — `wired.l3.gateway_gap` gains `.gateway_unowned`

STRICT code precedence (no double-fire): if the routed vlan has NO modeled
L3 interface at all, the existing `.removed`/`.unserved`/`.preexisting`
codes fire exactly as today and `.gateway_unowned` is not evaluated. Only
when interfaces EXIST on the vlan and a declared gateway G is present does
ownership run:

- **Owned** — some proposed L3Intf on the vlan has `same_ip(intf.ip, G) is
  True` → silent (positive fact; blind gateways cannot taint it).
- **Ownership broken** — baseline had a KNOWN owner (some baseline L3Intf
  owned the baseline's declared G) and the proposed state has none —
  whether the delta moved G or changed/removed the owner → ERROR at the
  baseline owning fact's confidence (UNSAFE at HIGH). The `_BLIND_GATEWAY`
  cap applies exactly as in `.removed` (an unmodeled gateway may own the
  new G); ERROR demotes to WARNING below HIGH (existing rule).
- **Never owned / newly declared** — interfaces exist but none owns G, and
  the baseline had NO known owner (vlan new, G newly declared, or baseline
  equally unowned-but-G-changed... see parity below) → WARNING/MEDIUM
  (`_UNMODELED` reasoning: the owner may live on an unmodeled box). The
  doctrine the user pinned: known-owner-removed is strong; unknown-owner
  absence is honest REVIEW — even though interfaces exist.
- **Pre-existing** — same declared G in baseline AND baseline equally
  unowned → INFO (context).
- Parity is value-based (GS25 rule): demotion requires the declared G
  byte-identical in baseline; a G that changed from one unowned value to
  another is introduced → WARNING (never ERROR — there was no known owner).

Abstention rails (GS25 relevance discipline — no global taint):
- `Vlan.gateway_unresolved` → ownership skipped for that vlan + PARTIAL
  note ONLY when that vlan is in the delta or a non-INFO conclusion for
  that vlan depends on it.
- An L3Intf on the vlan with `ip=None`/unparseable while no other intf
  owns G → ownership UNKNOWN (the nameless intf may own it) → abstain +
  note, NEVER `.gateway_unowned` (unknown never collapses to violation).
- `requires()`/`applies_to()` unchanged ({WIRED_L2, L3_EXITS};
  vlan/l3intf — `Vlan.gateway` is a vlan field, already watched).

## Check change 2 — `wired.dhcp.scope_lint` gains `.gateway_mismatch`

For each proposed scope where BOTH `scope.gateway` (handed to clients) and
`scope.network_gateway` (network's declared) are literal and
`same_ip(...) is False`:
- Introduced/altered → WARNING/REVIEW (config coherence, not proven
  outage — lint tier).
- Pre-existing → INFO, demotion requiring BOTH values byte-identical to
  baseline (handed gateway AND network gateway — either changing forfeits).
- Either side None → silent (no intent / nothing handed).
- `network_gateway_unresolved` on a scope whose id is in the delta's
  dhcp_scope refs → per-scope PARTIAL note (dimension-specific relevance,
  the GS25 r2 rule; an unchanged unresolved scope elsewhere never taints).
- `same_ip` returning None on two PRESENT values (unparseable literal) →
  abstain + per-scope note under the same relevance rule.

## Out of scope (recorded, not built)

- Network-gateway-outside-subnet lint (ownership is the operational
  hazard; GS25 already lints the DHCP-handed gateway against the subnet).
- The pre-existing templated-SUBNET false-SAFE in gateway_gap
  (`Vlan.subnet=None` for a templated subnet reads as "not routed") —
  real debt, separate ownership gap → add to ROADMAP §5 as part of this
  round's docs commit, do not fix here.

## Goldens (filed under GS22)

- **GS22-GW-a**: org/site staged so vlan 2's declared gateway is OWNED by
  the SRX's `ip_configs` interface (HIGH); op moves the declared gateway to
  an unowned IP → UNSAFE (`.gateway_unowned` ERROR/HIGH).
- **GS22-GW-b**: pre-existing unowned declared gateway (staged in
  baseline), unrelated routed-vlan delta → SAFE with `.gateway_unowned`
  INFO.
- **GS22-GW-c**: site scope hands a gateway different from the network's
  declared one (introduced by the op) → REVIEW
  (`.gateway_mismatch` WARNING).
- **GS22-GW-d**: same mismatch pre-staged in baseline, op touches an
  unrelated scope → SAFE with `.gateway_mismatch` INFO.
- Live verification: all eight plans must hold their verdicts (the live
  org's vlan-2 gateway is genuinely owned by the SRX).

## Honesty rails summary

| Blind spot | Behavior |
|---|---|
| Declared gateway templated | `gateway_unresolved` → skip + note only when vlan touched/conclusion-relevant |
| L3Intf ip None/unparseable on an otherwise-unowned vlan | ownership UNKNOWN → abstain + note, never a violation |
| Unfetched org namespace | `network_gateway=None` + unresolved flag (existing DhcpScope discipline) |
| Mixed IP families / `/prefix` shapes | `same_ip` family-aware, never int-compare across families |
| Blind gateway elsewhere | caps ownership-broken ERROR (it may own the new G); never taints OWNED positives |
