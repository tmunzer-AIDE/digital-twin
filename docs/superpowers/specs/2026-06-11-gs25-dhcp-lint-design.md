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
  - **Canonical identity** for deterministic pre-existing demotion:
    `DhcpScope.key` = `(provider, network, vlan, ip_start, ip_end, gateway)`.
    Baseline parity compares keys (and for overlap, key PAIRS), never object
    identity or list position.
  - Stored as `IR.dhcp_scopes: tuple[DhcpScope, ...]`, sorted by key.
- `Port.dhcp_trusted: bool | None` — **tri-state**:
  - `True`: usage has `allow_dhcpd=true` OR resolved mode is trunk;
  - `False`: resolved mode is access AND `allow_dhcpd` is not true;
  - `None`: effective usage unresolved / dynamic / vlan-blind — unknown
    trust must NEVER collapse to untrusted (no false REVIEW from blindness).
- `Device.dhcp_snooping: tuple[str, ...] | None` — `None` = disabled,
  `("*",)` = `all_networks`, else the enabled network names (site-network
  namespace).

Templated values (`{{var}}`) in `ip_start`/`ip_end`/`gateway`: the scope is
still minted with the field as `None` plus an adapter semantic finding
(doctrine f: uninterpretable in-scope values are surfaced, never parsed or
guessed). Checks treat a None field as unevaluable → abstain for that
comparison + PARTIAL coverage note.

## Check 1 — `wired.dhcp.scope_lint` (requires WIRED_L2 only)

Pure config lint over `IR.dhcp_scopes`; no topology.

- `.overlap`: normalized IP ranges (`ipaddress.ip_address` ordering) of two
  scopes intersect.
  - Pair INTRODUCED (the unordered key-pair did not overlap in baseline,
    or either key is new) → WARNING / REVIEW.
  - Pair already overlapping in baseline → INFO (visible context, no
    verdict drag).
  - Either range unparseable/None → that pair abstains + PARTIAL note.
- `.out_of_subnet`: scope `gateway` or a range edge outside `subnet`.
  - Introduced (this scope key was not violating in baseline) → WARNING.
  - Pre-existing → INFO.
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
- Source is `"site"` (switch-hosted) or hosted on S itself → local, no path
  needed, silent.
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
- Allowlist: `port_usages.*.allow_dhcpd` (both objects carrying usages).
- `compile/switch.py`: add `dhcp_snooping` to the device merge surface
  (`merge.py` already knows its REPLACE policy); verify `allow_dhcpd`
  carries through usage compilation. EVERY newly allowlisted device field
  gets a compile carry-through regression test.

## Ingest (`adapters/mist/ingest/switch.py`)

- Extend the existing `_dhcp_sources` walk to also mint `DhcpScope` rows
  (site dhcpd entries → provider `"site"` + site-network subnet; gateway
  dhcpd entries → provider device-id + org-network subnet via the
  GS24 namespace contract — unfetched org namespace mints NO gateway
  scopes, present-None shadows).
- Port trust from the effective usage at port-compile time (tri-state rule
  above).
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
| Templated range/gateway value | adapter finding + field None + abstain that comparison |
| Org namespace unfetched | no gateway scopes minted (GS24 rule) |
| Unknown port trust | `dhcp_trusted=None` → abstain, never untrusted |
| Source unlocatable in graph | abstain + PARTIAL |
| Blind gateway, conclusion source-dependent | MEDIUM cap + note |
| Blind gateway, conclusion config-only | untouched (HIGH/COMPLETE) |
