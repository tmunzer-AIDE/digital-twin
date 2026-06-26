# Switch wired-auth config (802.1X / MAC-auth, whole surface)

**Status:** PROPOSED
**Date:** 2026-06-25
**Author:** brainstormed with the repo owner

A device/template PUT that changes switch port wired-authentication config
(`port_auth`, `enable_mac_auth`, the `mac_auth_*` knobs, dynamic-VLAN / RADIUS
fallback networks, `bypass_auth_*`, `persist_mac`, `reauth_interval`) currently
resolves to **UNKNOWN** — none of these 14 leaves are modeled. This spec brings
the **whole wired-auth surface** in-scope with a **policy-floor** model: any
auth-config change floors **REVIEW** ("admission/access impact depends on
RADIUS/NAC and is not fully modeled"), and **observed** connected clients
escalate the detail/confidence — without ever pretending to simulate a RADIUS or
NAC decision. The cardinal value over today's UNKNOWN: a specific reason + the
affected observed clients, and other modeled changes in the same PUT no longer
get dragged to UNKNOWN by an auth leaf.

## Program context

Sub-project 3 of 4 in the switch port-config attribute-modeling program. SP1
(admin-disable) and SP2 (L1 speed/duplex/autoneg) are merged. The owner chose to
do the **whole auth surface in this one cycle** (folding the originally-deferred
SP3b network-name fields and SP3c knobs into SP3), made tractable by the
policy-floor framing. SP4 (mac_limit / storm_control / qos / voip_network /
inter_switch_link) remains.

## Problem

Wired-auth is an **admission-policy** layer: enabling `port_auth=dot1x` or
`enable_mac_auth` on a port forces every client — current and future — to
authenticate; the landed VLAN and pass/fail depend on the RADIUS server and the
org NAC rules, neither of which the twin can observe or simulate. So a faithful
"will this client get on / which VLAN" simulation is impossible. But leaving the
14 leaves UNKNOWN is coarse: it gives no reason, no affected-client detail, and
drags any co-changed modeled leaf in the same PUT to UNKNOWN.

OAS facts (refreshed `device_switch`, confirmed):
- All 14 auth attrs are on **`local_port_config`** and **`port_usages`** only —
  **absent from `port_config` and `port_config_overwrite`** (like `stp_edge`).
- `port_auth`: enum `["dot1x"]`, nullable (null/absent = no 802.1X).
  `enable_mac_auth`/`mac_auth_only`/`mac_auth_preferred`/
  `allow_multiple_supplicants`/`bypass_auth_when_server_down`/
  `…_for_unknown_client`/`persist_mac`: boolean (default false where defaulted).
  `mac_auth_protocol`: enum `["eap-md5","eap-peap","pap"]` (default eap-md5).
  `dynamic_vlan_networks`: array of network-name strings.
  `server_fail_network`/`server_reject_network`/`guest_network`: nullable
  network-name string. `reauth_interval`: **typeless in the OAS**; real values
  seen are `int`, numeric string, `""`, `null`, and even an object `{…}`.
- The IR `Port` models **zero** auth today; org-NAC (`NacRule`/`NacTag`,
  `simulate_org_nac`) is **disjoint** and never references ports — SP3 does not
  touch it.
- `ClientEnrichment` carries observed `auth_method`/`auth_state`/`auth_type`/
  `assigned_vlan`/`vlan_source` per wired client, but is today **evidence-only**
  ("never read by verdict logic").

## Goals

- All 14 auth leaves are **simulated** (policy-floor), not UNKNOWN.
- A `wired.auth.access_change` check floors **REVIEW** on any auth-config change
  and escalates detail/confidence from **observed** connected clients — capped at
  REVIEW (never asserts a deterministic UNSAFE outage; RADIUS/NAC are unknowable).
- **No false-SAFE** (an auth change never resolves SAFE) and **no normalization
  that drops an allowlisted auth change** (every effective auth-leaf change wakes
  the check).
- Resolver/allowlist precision: auth flows from `local_port_config` (device) +
  `port_usages` only — never `port_config`/`port_config_overwrite`.

## Non-goals

- Simulating RADIUS/NAC decisions, dynamic-VLAN landing, or pass/fail outcomes.
- Resolving `dynamic_vlan_networks`/`*_network` names to VLANs (the policy-floor
  makes this unnecessary — the change is surfaced for review, not simulated).
- SP4 attrs (mac_limit/storm_control/qos/voip_network/inter_switch_link).

## Design

### 1. IR modeling (`ir/entities.py`)

New frozen, comparable dataclass `PortAuth` holding the **effective, normalized**
auth config (typed where the check reasons, captured wholesale for
change-detection):

```
port_auth: str | None            # "dot1x" | None
mac_auth: bool                   # enable_mac_auth
mac_auth_only: bool
mac_auth_preferred: bool
mac_auth_protocol: str | None
allow_multiple_supplicants: bool
dynamic_vlan_networks: tuple[str, ...]
server_fail_network: str | None
server_reject_network: str | None
guest_network: str | None
bypass_auth_when_server_down: bool
bypass_auth_when_server_down_for_unknown_client: bool
persist_mac: bool
reauth_interval: str | None      # canonical (see §2)
```

`Port.auth: PortAuth | None = None`. **`None` means the ENTIRE auth surface is
default/absent** — not merely `port_auth is None`. Change-detection is
`base.auth != prop.auth` (frozen-dataclass equality). A derived predicate
`tightens(old: PortAuth | None, new: PortAuth | None) -> bool` (admission got
stricter: gained `port_auth`/`mac_auth`/`mac_auth_only`, lost a `guest_network`
fallback, etc.) drives escalation.

### 2. Ingest (`ingest/ports.py` + `ingest/switch.py`)

- New `_AUTH_ATTRS` tuple (the 14 names) in `ports.py`. Add it to **`_LOCAL_ATTRS`
  only** (`_LOCAL_ATTRS = (*_USAGE_OVERRIDE_ATTRS, "disabled", *_AUTH_ATTRS)`) — so
  auth applies from `local_port_config`; usage-level auth flows via
  `usage_definition` already. **Do NOT add to `_USAGE_OVERRIDE_ATTRS`** (that is
  the `port_config` inline layer, which the OAS shows never carries auth).
- `switch.py` `_port_auth(usage) -> PortAuth | None`: normalize the effective
  auth attrs; return `None` iff the result equals the all-default `PortAuth()`
  (so a change to ANY auth leaf — e.g. `persist_mac` while `port_auth` absent —
  wakes the check; the §Goals "don't normalize away" invariant).
  - **`reauth_interval` canonicalization** (real values are int / numeric-string
    / `""` / null / object): `None` for null/`""`/absent; the decimal string for
    an int or numeric string (so `36000` and `"36000"` compare equal); otherwise
    a stable raw token (e.g. `str(value)`) — **never silently collapse an
    unparseable non-empty value to `None`**, so a to/from change still wakes the
    check. Stored as `str | None`, always hashable.

### 3. New check `wired.auth.access_change` (template: `poe_disconnect` tiering + `NacDelta` honesty)

`requires() = {WIRED_L2}`; `applies_to = diff.touches("port") or diff.touches("client")`.

- **Detection:** per port in the proposed IR, compare `base.auth` vs `prop.auth`
  (matched by id, both-None/equal → silent). Any difference (gained / lost /
  modified) → a finding.
- **Floor — `wired.auth.access_change.policy_change`, WARNING → REVIEW:**
  "wired-auth config changed on port X — admission/access impact depends on
  RADIUS/NAC and is not fully modeled." This is the no-false-SAFE guarantee for
  the whole surface (incl. the network-name/RADIUS knobs we deliberately don't
  resolve).
- **Observed escalation — `wired.auth.access_change.clients_at_risk`,
  WARNING → REVIEW (capped):** when the change `tightens()` admission AND
  `clients_by_port(base_ir)` has currently-connected wired clients on the port,
  consult `ClientEnrichment.auth_state`/`auth_method` and name the clients
  observed in a state the change would block (unauthenticated, or authenticated
  by a method being removed). Raise confidence and attach the affected clients as
  evidence. **Cap at REVIEW** — RADIUS could still admit them, so never ERROR/
  UNSAFE. Degrade gracefully: no client/enrichment data → floor only.
- **Aggregation:** WARNING → WARN (REVIEW); the check never returns FAIL.
- Complementary to `wired.client.impact` (topology-driven disconnect/vlan_move/
  blackhole) — this is the admission dimension; distinct codes, both may appear.

### 4. Field gate (`scope/allowlist.py`)

Add the 14 auth attrs to **`_MODELED_USAGE_ATTRS`** (splice in a named
`_AUTH_ATTRS` for clarity). This — and only this — propagates correctly:
- `port_usages.*.<auth>` → `_USAGE_LEAVES` → `site_setting` + `device` +
  `networktemplate` (template carries port_usages). ✓
- `local_port_config.*.<auth>` → `_LOCAL_PORT_CONFIG_LEAVES` → `_DEVICE_PORT_LEAVES`
  → **device only** (site_setting/networktemplate have no local map). ✓
- **NOT** added to `_PORT_CONFIG_ATTRS` or `_OVERWRITE_LEAVES` (auth is absent
  from those maps). In scope now that the check models them.

### 5. ClientEnrichment contract (`ir/entities.py` + `ir/model.py`)

Update the `ClientEnrichment` docstring and the `IR.client_enrichment` comment:
from "Evidence ONLY — never read by verdict logic" to **"observational,
non-diff-bearing, best-effort; MAY enrich or cap a finding (e.g.
`wired.auth.access_change` escalation), but its absence must degrade gracefully;
still never part of `diff_ir`."** (It remains out of `diff_ir` — only the new
check reads it, and only to enrich, never to originate or floor a verdict.)

### 6. Registration + L0

Register `AuthAccessChangeCheck` in `ALL_WIRED_CHECKS` + `__all__`; bump
`tests/test_public_api.py` `len(ALL_WIRED_CHECKS) == 22 → 23`. **L0: no change** —
all 14 attrs are documented in the OAS (the typeless `reauth_interval` is still a
documented property the walker accepts).

## Testing

- **IR/normalization unit:** `_port_auth` returns `None` only for the all-default
  surface; a lone `persist_mac`/`dynamic_vlan_networks`/`reauth_interval` change
  (port_auth absent) produces a non-None `PortAuth` and a different value;
  `reauth_interval` `36000`=="36000", `""`/null→None, object→stable token (no
  silent collapse); `tightens()` truth table.
- **Resolver unit:** auth applies from `local_port_config` and `port_usages`;
  a `port_config`/`port_config_overwrite` auth key is **ignored** (not honored);
  SP1/SP2 precedence intact.
- **Check unit** (`tests/checks/test_auth_access_change.py`): gained dot1x →
  policy_change REVIEW; lost auth → REVIEW; modified knob (e.g. mac_auth_protocol,
  persist_mac) → REVIEW; both-default → silent; tightening + connected
  unauthenticated client → clients_at_risk REVIEW naming the client, higher
  confidence; tightening + no client data → floor only; escalation **caps at
  REVIEW** (never UNSAFE) even with observed evidence; absent ClientEnrichment →
  graceful floor.
- **Field-gate unit:** `port_usages.*.<auth>` in site_setting + device +
  networktemplate + EFFECTIVE; `local_port_config.*.<auth>` in device only;
  `port_config.*.port_auth` and `port_config_overwrite.*.port_auth` NOT in scope.
- **Public API:** `len(ALL_WIRED_CHECKS) == 22 → 23`.
- **e2e pipeline:** a device PUT enabling `port_auth=dot1x` (via local_port_config
  or a referenced port_usage) on a port with a connected wired client →
  not UNKNOWN; `wired.auth.access_change.*` present; decision REVIEW.
- **Goldens:** run full golden suite; reconcile any test that used an auth attr
  as an "unmodeled/out-of-scope" example (likely `tests/scope/`), retargeting to
  a still-unmodeled attr (e.g. `mac_limit`). Re-pin goldens only with
  justification.
- **Full gate** each task: `uv run pytest -q && uv run ruff check . && uv run mypy src`.

## Risks / open points

- Escalation makes `ClientEnrichment` a verdict-logic *consumer* (enrich/cap
  only). Mitigated by: never originating or flooring from it, graceful absence,
  and the contract-comment update (§5).
- The policy-floor means **frequent REVIEWs** on auth edits — accepted (honest
  over quiet; strictly better than today's UNKNOWN). Mass auth rollouts will
  REVIEW per affected port.
- `reauth_interval` object-valued `{…}` in the wild is likely templated/malformed;
  the stable-token canonicalization keeps it change-detecting without crashing.
