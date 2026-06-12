# GS25 — DHCP lint: scope ranges + snooping trust (design)

Date: 2026-06-11
Status: approved (Approach A)
MVP mapping: CFG-DHCP-RNG, CFG-DHCP-CFG, snooping hazard

## Problem

Three DHCP misconfiguration classes are invisible to the twin today:

1. **Scope overlap** — two dhcpd scopes whose `ip_start..ip_end` ranges
   intersect hand out colliding addresses.
2. **Scope/subnet incoherence** — a scope's `gateway` or range edge outside
   the owning network's subnet hands clients an unusable default gateway.
3. **Snooping drops** — `dhcp_snooping` enabled on a switch whose only path
   toward the vlan's DHCP source crosses an UNTRUSTED port silently drops
   offers; clients lose addressing at lease renewal (the GS24 blast-radius
   shape, caused by a filter instead of a removal).

## Decision: Approach A — IR facts + two baseline-aware checks

Adapter-level lint (the `bridge_priority_invalid` pattern) was rejected:
adapter findings cannot see the baseline, so a pre-existing overlap would
taint every future plan. Pre-existing demotion requires checks.

## IR additions (`ir/entities.py`)

- `DhcpScope` (frozen): `provider` (`"site"` | gateway device id),
  `network` (name in the provider's namespace), `vlan: int | None`,
  `ip_start: str | None`, `ip_end: str | None`, `gateway: str | None`,
  `subnet: str | None` — the OWNING network's subnet resolved in the
  provider's namespace (org networks for gateway scopes, site networks for
  site scopes); None when unknown/blind.
  - **Identity vs violation parity**: `DhcpScope.id` = `provider:network` —
    exactly how `dhcpd_config` is keyed, and stable when vlan resolution
    flips unknown→known (`vlan` is a DIFFED FIELD, never part of identity). Pre-existing demotion is
    VIOLATION-SPECIFIC, never the full field tuple: an overlap pair is
    pre-existing iff the same id-pair overlapped in baseline AND both ranges
    are unchanged (editing a still-overlapping range = altered = WARNING,
    the native-mismatch precedent); a gateway-only edit can never flip an
    old overlap to "introduced". An out-of-subnet violation is pre-existing
    iff the same id violated in baseline with the same offending field value
    and same subnet.
  - Stored as `IR.dhcp_scopes: tuple[DhcpScope, ...]`, sorted by id.
- `Port.dhcp_trusted: bool | None` — **tri-state** (per OAS: `allow_dhcpd`
  is itself tri-state; only the UNDEFINED value defers to the mode default):
  - `True`: `allow_dhcpd is True`, OR `allow_dhcpd` absent and resolved
    mode is trunk;
  - `False`: `allow_dhcpd is False` (even on a trunk), OR `allow_dhcpd`
    absent and resolved mode is access;
  - `None`: effective usage unknown — i.e. a DYNAMIC port whose runtime
    usage did NOT resolve from observed LLDP (the existing
    `unresolved_dynamic_findings` distinction), or a vlan-blind/unresolved
    usage. A dynamic port that DID resolve to a concrete runtime usage uses
    that usage's `allow_dhcpd`/mode like any other port. Unknown trust must
    NEVER collapse to untrusted (no false REVIEW from blindness) — but
    resolved dynamics must not collapse to unknown either (no false PARTIAL
    from a resolved uplink).
- `Device.dhcp_snooping: tuple[str, ...] | None` — `None` = disabled,
  `("*",)` = `all_networks`, else the enabled network names (site-network
  namespace).

**Diff/applicability plumbing** (the registry consults `applies_to()`
against `diff_ir`, which walks a fixed entity-kind list):
- `diff.py`: append `("dhcp_scope", lambda ir: ir.dhcp_scopes)`; `DhcpScope`
  exposes the stable `.id` above.
- `IRBuilder` / validation / export carry `dhcp_scopes` like every other
  collection.
- `scope_lint.applies_to`: `("dhcp_scope", "vlan")` (subnet intent lives on
  vlans/networks). `snooping.applies_to`: `("device", "port", "dhcp_scope",
  "vlan", "link")` — snooping toggles are device facts, trust is a port
  fact, and a topology change can remove the last trusted path (doctrine c:
  list every kind run() reads).

Templated values (`{{var}}`) in `ip_start`/`ip_end`/`gateway`: the scope is
still minted with the field as `None`. The adapter semantic finding is
emitted ONLY when the DELTA introduces or changes the unresolved value — a
pre-existing unchanged template must not floor unrelated plans to REVIEW
(adapter findings are baseline-blind and fire every run; the
bridge_priority precedent fires on baseline deliberately because a
malformed priority poisons a GLOBAL election, but a templated range only
poisons conclusions about that one scope). Checks treat a None field as
unevaluable → abstain for that comparison + PARTIAL coverage note, scoped
to runs where that scope is actually relevant.

## Check 1 — `wired.dhcp.scope_lint` (requires WIRED_L2 only)

Pure config lint over `IR.dhcp_scopes`; no topology.

- `.overlap`: normalized IP ranges (`ipaddress.ip_address` ordering) of two
  scopes intersect.
  - PRE-EXISTING (→ INFO, no verdict drag) iff the same unordered id-pair
    overlapped in baseline AND both scopes' ranges are byte-identical to
    baseline. Anything else — new scope, or a range edit that still/newly
    overlaps — is INTRODUCED/ALTERED → WARNING / REVIEW (the
    native-mismatch precedent: touching the hazard forfeits demotion).
  - Either range unparseable/None → that pair abstains + PARTIAL note.
- `.out_of_subnet`: scope `gateway` or a range edge outside `subnet`.
  - PRE-EXISTING (→ INFO) iff the same scope id violated in baseline with
    the SAME offending field value and the SAME subnet. A bad value changed
    to a different bad value, or the subnet changed under it, is ALTERED →
    WARNING.
  - `subnet` None (org namespace blind, templated, or network has no
    subnet configured) → scope skipped + PARTIAL note. A network with no
    subnet intent is NOT a violation.
- **Blind-gateway scoping (user nuance)**: a blind gateway elsewhere does
  NOT taint a concrete finding built from fully parsed scopes. `l3_unmodeled`
  only degrades coverage for scopes whose owning namespace is the blind
  gateway's; site-scope findings stay HIGH/COMPLETE.

## Check 2 — `wired.dhcp.snooping` (requires WIRED_L2)

For each switch S with snooping active in PROPOSED for vlan V (named
network mapping to V, or `all_networks`), where V has modeled
`dhcp_sources`:

- **Path rule**: every modeled DHCP source must be reachable from S through
  at least ONE egress path whose first hop out of S is a trusted port
  carrying V. Multiple paths, one trusted → silent. ALL known egress ports
  toward the source untrusted (`dhcp_trusted is False`) → `.untrusted_path`
  WARNING / REVIEW.
- Source is hosted on S itself (gateway scope on S) → local, silent.
- Source is `"site"`: PLACEMENT IS UNKNOWABLE in the current model — site
  scopes are activated per-device by device-level `dhcpd_config.enabled`
  (visible in the live fixture), and device-level dhcpd_config is
  deliberately unmodeled. GS24 could treat "site" as an abstract provider
  because it only tested path EXISTENCE; snooping needs placement. So a
  snooped vlan whose only source is "site" → abstain + PARTIAL note
  ("site DHCP service placement is unmodeled"), never silent, never a
  dropped-offer finding.
- **Mixed sources** (`("site", <gateway>)`): the gateway path is still
  evaluated and an all-untrusted gateway path still emits `.untrusted_path`
  — but the message must hedge to "offers from <gateway> are dropped; an
  unmodeled site-hosted service may still serve this vlan", never claim a
  full DHCP outage. The unlocatable site source ALWAYS adds the PARTIAL
  note for that vlan, finding or not.
- Any candidate egress port with `dhcp_trusted is None` on an otherwise
  all-untrusted set → UNKNOWN, abstain + PARTIAL note (never invent a
  dropped-offer conclusion from unknown trust).
- Source device absent from the L2 graph / no path found → abstain +
  PARTIAL note (placement unknowable ≠ offers dropped).
- **Pre-existing**: same (switch, vlan, source) already snooped-and-blocked
  in baseline (snooping active AND all baseline egress ports untrusted) →
  INFO. Introduction = ACTIVITY, not pair: newly-enabled snooping OR a
  trusted port going untrusted OR the topology change that removes the last
  trusted path all count as introduced (the native-mismatch lesson).
- **Blind gateways**: when the conclusion depends on a gateway source's
  placement and that gateway is `l3_unmodeled`/`dhcp_unresolved`,
  confidence caps MEDIUM + coverage note (GS24 rail). Config-only facts
  about S's own ports are not affected.

Severity is WARNING/REVIEW for the whole GS25 tier (MVP): external DHCP
servers, source placement, and runtime state still carry enough unknowns
that ERROR/UNSAFE would over-claim. Graduation path (post-MVP): observed
clients + complete source/path certainty → ERROR.

## Scope/allowlist + compile carry-through (GS21 lesson applied up front)

- Allowlist (site_setting): `dhcpd_config.*.{ip_start,ip_end,gateway}`
  (extends the existing `{type,servers}`), `dhcp_snooping.{enabled,
  all_networks,networks}` on the switch section.
- Allowlist (device): `dhcp_snooping.{enabled,all_networks,networks}`.
- Allowlist: `allow_dhcpd` added to `_MODELED_USAGE_ATTRS` → honored on
  `port_usages.*`, inline `port_config.*`, and `local_port_config.*`
  (every surface the OAS exposes it on), with resolver carry-through.
- `compile/switch.py`: add `dhcp_snooping` to the device merge surface
  (`merge.py` already knows its REPLACE policy); verify `allow_dhcpd`
  carries through usage compilation. EVERY newly allowlisted device field
  gets a compile carry-through regression test.

## Ingest (`adapters/mist/ingest/switch.py`)

- Extend the existing `_dhcp_sources` walk to also mint `DhcpScope` rows —
  but ONLY for SERVING entries, decided by a NEW predicate
  `_dhcp_serves_scope(entry)`. Do NOT reuse `_dhcp_active`: it answers "is
  this a DHCP PATH" and deliberately counts relay-with-servers as active
  (correct for GS24 sources, wrong for scope ownership). Truth table:
  | entry | `_dhcp_active` (sources) | `_dhcp_serves_scope` (scopes) |
  |---|---|---|
  | `type` absent / `local` / `server` | True | True |
  | `relay` with `servers` | True | **False** |
  | `relay` without `servers` | False | False |
  | `none` | False | False |
  Serving means `type in {"local", "server"}` or absent: `local` is what
  live Mist emits (fixture-verified), `server` is the OAS-canonical enum
  value — both exist in the wild. This review also surfaced a SHIPPED GS24
  bug (`_dhcp_active` ignored `server` → its removal could false-SAFE);
  fixed with regression tests for both shapes, shared constant
  `_DHCP_SERVING_TYPES` consumed by BOTH predicates so they cannot drift.
  RELAY and `none` entries own no `ip_start..ip_end` and must NOT become
  scope rows —
  a range-less relay row would abstain the range lints and drag PARTIAL
  noise onto every normal relay config. Relay/none participate in
  `dhcp_sources` only. A SERVING entry whose range fields are templated or
  absent is still minted (fields None = intentionally unresolved → scoped
  abstention), because the scope exists even when we cannot read its edges.
  Namespace resolution: site scopes → site-network subnet; gateway scopes →
  org-network subnet via the GS24 namespace contract, present-None shadows.
- **Unfetched org namespace** (refined from GS24): the GS24 rule — no
  `dhcp_sources` CREDIT — stands untouched, because crediting a source is a
  guessed positive that suppresses removal findings. But gateway `DhcpScope`
  rows ARE still minted with `vlan=None`/`subnet=None` when the range
  fields parse: `ip_start/ip_end/gateway` are LITERAL device config fetched
  with the device, no namespace needed. Otherwise a new site scope
  overlapping an unmodeled gateway range would falsely PASS the overlap
  lint. Out-of-subnet abstains for such rows (subnet None).
- Port trust from the EFFECTIVE usage attrs (tri-state rule above).
  `allow_dhcpd` joins `_MODELED_USAGE_ATTRS`, which makes it honored on
  `port_usages.*`, inline `port_config.*`, AND `local_port_config.*` (the
  OAS exposes it on all three) and carried by `resolve_effective_ports` —
  a baseline inline `allow_dhcpd` override must not be invisible to trust.
- `dhcp_snooping` from effective switch config (template→site→device merge
  already handled by `merge.py`).

## Goldens

- **GS25a** (overlap): plan adds a site dhcpd scope overlapping the SRX's
  LD_VLAN2 range → REVIEW. Variant: same overlap pre-staged in baseline →
  not REVIEW (INFO only).
- **GS25b** (snooping): enable snooping for vlan 2 on `ld-cup-idf-b` with a
  staged access/untrusted uplink → REVIEW. Variant: real trunk uplink
  (trusted) → SAFE. Variant: unknown-trust uplink → REVIEW via
  PARTIAL coverage (NO `.untrusted_path` finding minted) — blindness floors
  the verdict honestly without inventing a dropped-offer conclusion.
- Checks go 12 → 14 (`test_public_api` count bumps). All eight live plans
  re-verified after every commit.

## Error handling / honesty rails summary

| Blind spot | Behavior |
|---|---|
| Templated range/gateway value | field None + abstain; adapter finding ONLY if the delta introduced/changed it |
| Snooped vlan served only by "site" | abstain + PARTIAL (placement unmodeled) |
| Org namespace unfetched | no dhcp_sources CREDIT (GS24 rule); gateway scopes still minted with vlan/subnet None (ranges are literal config) |
| Mixed site+gateway sources | gateway findings hedged; PARTIAL always (site placement unmodeled) |
| Unknown port trust | `dhcp_trusted=None` → abstain, never untrusted |
| Source unlocatable in graph | abstain + PARTIAL |
| Blind gateway, conclusion source-dependent | MEDIUM cap + note |
| Blind gateway, conclusion config-only | untouched (HIGH/COMPLETE) |
