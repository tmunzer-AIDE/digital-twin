# STP policy attribution (`wired.stp.policy`) — Design

**Status:** PROPOSED
**Date:** 2026-07-03
**Author:** brainstormed with the repo owner

## Problem

Spec-1 (PR #41) classified `stp_required`, `stp_no_root_port`, `stp_p2p`, and
`use_vstp` as recognized-but-unmodeled: they ride `PortMisc` and every change
produces a generic `wired.port.unmodeled_change.recognized` REVIEW. That is
honest but blunt. Two of these knobs cause **outages** with predictable
mechanics:

- `stp_required` ("remain in block state if no BPDU is received"): enabled on a
  port whose peer never sends BPDUs — an AP, a wired client, a peer port with
  `bpdu_filter` — the port sits in blocking and everything behind it is cut.
- `stp_no_root_port` (root-protect): enabled on the device's only path toward
  the elected root bridge, the port blocks when superior BPDUs arrive,
  isolating the device.

The other two are **consistency** knobs: `use_vstp`/`stp_p2p` disagreement
across a link causes protocol/link-type mismatch (degradation, wrong
convergence), detectable when both ends are modeled.

This slice graduates the four knobs from generic REVIEW to precise STP policy
attribution — sharper findings, UNSAFE escalation for concrete predicted harm —
**without** granting SAFE.

## Verdict contract (the load-bearing decision)

**Policy floor: an STP policy change NEVER resolves SAFE in this slice.**
The twin can see LLDP ties, configured knobs, some STP telemetry, and graph
reachability, but it cannot prove the whole bridge domain: unmanaged switches,
invisible BPDU sources, off-fabric root behavior, and transient convergence are
outside the model.

- Concrete predicted harm escalates: `.blocking_risk` and `.root_protect_risk`
  are **ERROR at HIGH confidence** (→ UNSAFE via the NETWORK-ERROR gate),
  WARNING below HIGH.
- A clean/no-risk STP knob change still emits **`.policy_change`
  WARNING/MEDIUM → REVIEW** (the floor).
- A pre-existing violation the delta merely touches demotes to **INFO** context
  (the established pre-existing = not-caused convention).
- **SAFE is deferred** to a future STP tree/convergence engine validated
  against live `stp_state` (see Deferred).

## Design

### IR surface — `StpPolicy` value object

The four knobs leave `PortMisc` and become a frozen `StpPolicy` on `Port`
(the `PortAuth` pattern):

```python
@dataclass(frozen=True)
class StpPolicy:
    """Spec-2 STP policy knobs (graduated from PortMisc). Frozen + comparable;
    Port.stp_policy is None ONLY when all are default. bool | str: a
    templated/unparseable value stays a diff-bearing `unresolved:` token
    (Spec-1 _bool_token), never collapsed to a bool."""

    stp_required: bool | str = False
    stp_no_root_port: bool | str = False
    stp_p2p: bool | str = False
    use_vstp: bool | str = False
```

- `PortMisc` shrinks to `inter_switch_link`, `storm_control`, `poe_priority`,
  `community_vlan_id`, `inter_isolation_network_link`. Its
  `dataclasses.fields` loop in `unmodeled_change` makes the coverage change
  automatic — the four knobs stop waking that check.
- Ingest: a `_stp_policy(usage)` reader in `ingest/switch.py` beside
  `_port_auth`/`_port_misc`, reusing `_bool_token`; returns `None` when
  all-default. The local-capable trio (`use_vstp`/`stp_p2p`/`stp_no_root_port`)
  keeps flowing through the existing local-contribution path (they stay in the
  local-attrs tuple in `ingest/ports.py`; whether that tuple keeps the name
  `_MISC_ATTRS` or splits an `_STP_POLICY_LOCAL_ATTRS` out is an
  implementation choice — the contribution set must not change).
- `stp_policy` is a real config field: NOT diff-ignored.
- **Gates are untouched.** All four leaves are already in raw + effective +
  device-profile (Spec-1); only the ingest destination changes.

### New check `wired.stp.policy`

`requires()` = `{WIRED_L2}`. **Precise `applies_to`:** not bare
`diff.touches("port")` — the check applies iff some port diff entry is an
add/remove **or** has `"stp_policy"` in its `changed_fields` (IRDiff entries
carry `changed_fields`; the plan pins the exact accessor). Unrelated port
changes do not wake it.

Four codes:

**`wired.stp.policy.blocking_risk`** — the delta enables `stp_required`
(default/False/absent → True; port-add with True counts; profile swap that
resolves to True counts) on a port whose peer **won't send BPDUs**, defined
conservatively:

| peer evidence | confidence | severity |
|---|---|---|
| LLDP-tied AP, two-sided tie | HIGH | ERROR |
| observed wired client, no modeled bridge peer | HIGH | ERROR |
| modeled peer port with `bpdu_filter`, two-sided link | HIGH | ERROR |
| any of the above with a one-sided tie | MEDIUM | WARNING |

The rule is uniform: **ERROR iff the peer evidence is HIGH**; a one-sided tie
still names a CANDIDATE non-BPDU peer, so it stays `.blocking_risk` at
WARNING/MEDIUM. **Unknown / no peer evidence does NOT qualify** — the model
cannot claim "peer won't send BPDUs" about a peer it cannot see. That case
falls through to the `.policy_change` floor with a coverage/evidence note
("stp_required enabled on <port>: peer unobserved — blocking outcome not
assessable"), review finding P2. Evidence: `peer`, `peer_kind` (`ap`/`client`/`bpdu_filter`/
`unknown`), tie provenance, `occupants_behind` (clients/APs on the port —
reuses the occupancy helpers), `severity_reason`.

**`wired.stp.policy.root_protect_risk`** — the delta enables
`stp_no_root_port` on a port that is the device's **only graph path toward
the component's elected root**. Mechanics: reuse `stp_root`'s election on the
proposed component; "only path" = remove this port's edge from the component
graph → root node unreachable from the device. **Explicit dependency: ERROR
requires the elected root to be known at HIGH** — all participating priorities
interpretable (`stp_priority_invalid` on any candidate, unknown priorities, or
an external-root signal → the election is not HIGH). If the root is not HIGH,
emit WARNING + a coverage/abstention note ("root election not provable"),
never ERROR, never silence. Redundant path exists → no risk finding (the
`.policy_change` floor still fires). Evidence: `elected_root`,
`only_path` bool, `election_confidence`, `severity_reason`.

**`wired.stp.policy.link_mismatch`** — both ends of a modeled link disagree on
`use_vstp` or `stp_p2p`, and the delta introduced or changed the disagreement.
**Keyed by `(link_id, knob)`** — `use_vstp` and `stp_p2p` mismatches on the
same link are both visible as separate findings. WARNING/MEDIUM (HIGH-tie
links may carry HIGH confidence; severity stays WARNING — degradation, not
outage). Observed `Port.stp_mode` corroborates in evidence when present
(`observed_modes`), never gates. Pre-existing mismatch merely touched → INFO.

**`wired.stp.policy.policy_change`** — the floor: any port whose `stp_policy`
changed and produced no port-level risk finding → one WARNING/MEDIUM finding
("STP policy changed on <port>: <knobs> — bridge-domain outcome not fully
modeled"). **`unresolved:` token values ALWAYS land here** — a templated knob
never produces a precise prediction, only the floor + a coverage note naming
the unresolved knob.

### Precedence

Per **port**: most specific code wins — `blocking_risk`/`root_protect_risk`
(both can fire on one port if both knobs were enabled — they attribute
different knobs) suppress that port's `.policy_change`. Per **link**:
`.link_mismatch` findings are keyed `(link_id, knob)` and coexist with
port-level findings (a mismatch is not a substitute for the floor on OTHER
changed knobs of the same port). Invariant: **every port with a changed
`stp_policy` yields at least one finding** — the floor makes falling through
to SAFE structurally impossible.

## Never-false-SAFE

Strictly stronger than today. Currently all four knobs → `unmodeled_change`
WARNING/REVIEW. After this slice: every change still floors REVIEW
(`.policy_change` at minimum — same severity/confidence as the old path), and
confident harm escalates to ERROR/UNSAFE, which today's model never does. No
demotion anywhere: `unmodeled_change` coverage is *replaced* by an
equal-or-stronger floor. Pre-existing violations (not delta-caused) are INFO
context, exactly like `blackhole.preexisting_unlocatable`. Token values cannot
reach precise codes, so a templated knob can never manufacture (or suppress)
an ERROR. The migration is pinned by tests mirroring Spec-1's enable_qos
migration, in the opposite direction (leaves generic check, gains precise
check).

## Files touched

- `src/digital_twin/ir/entities.py` — `StpPolicy`; `Port.stp_policy`;
  `PortMisc` shrinks.
- `src/digital_twin/adapters/mist/ingest/switch.py` — `_stp_policy(usage)`;
  `_port_misc` drops the trio+`stp_required`... (exact split per plan).
- `src/digital_twin/adapters/mist/ingest/ports.py` — local-contribution tuple
  keeps the trio flowing (naming per plan).
- `src/digital_twin/checks/wired/stp_policy.py` — the new check (+ registry).
- `src/digital_twin/checks/wired/unmodeled_change.py` — docstring only (field
  loop shrinks automatically).
- `src/digital_twin/ir/diff.py` — nothing expected (`stp_policy` diffs like
  any field); verify `changed_fields` exposure for `applies_to`.
- Tests: migration pins, tier fixtures, mismatch matrix, floor/token cases,
  e2e REVIEW/UNSAFE, device-profile pins stay green.
- README check table + inventory test (+1 check, 29 total).

## Testing requirements

- **Migration pins:** each of the four knobs no longer wakes
  `unmodeled_change`; each DOES wake `stp.policy` (floor at minimum). The five
  remaining PortMisc knobs still wake `unmodeled_change` (#38/#40 pins green).
- **blocking_risk tiers:** one fixture per row of the peer-evidence table
  (severity + confidence + evidence pinned); an unknown/no-peer-evidence
  fixture asserts the FLOOR + peer-unobserved note and NO `.blocking_risk`
  (P2); disable-direction (True→False) produces floor only, never risk;
  pre-existing True untouched → INFO/nothing per the pre-existing convention.
- **root_protect_risk:** only-path + HIGH election → ERROR/HIGH; redundant
  path → floor only; election not HIGH (invalid priority in component) →
  WARNING + note; root external → WARNING + note.
- **link_mismatch:** (link, knob) keying — both knobs mismatched on one link →
  2 findings; delta-conditioned (pre-existing mismatch → INFO); observed
  stp_mode in evidence when the fixture provides it.
- **Floor + tokens:** `use_vstp="{{vstp}}"` → `.policy_change` + coverage note,
  no precise finding; any single-knob change → exactly one floor finding.
- **applies_to:** an unrelated port change (e.g. description) does not wake
  the check.
- **e2e:** `stp_required` enable on AP-facing port → UNSAFE with
  `.blocking_risk`; any knob change on quiet topology → REVIEW via
  `.policy_change`; device-profile below-profile pins from Spec-1 stay green.
- **Verdict invariant:** no fixture in the suite may produce SAFE for a
  changed `stp_policy` (parametrized guard over the check's fixtures).

## Live verification (read-only)

Against the authorized org via the Mist MCP: confirm real `stp_state`/
`stp_mode` shapes on switch ports; simulate (no writes) enabling
`stp_required` on an AP-facing port → `.blocking_risk` ERROR/UNSAFE with the
AP named; a `use_vstp` flip on one end of a modeled inter-switch link →
`.link_mismatch`.

## Scope and deferred

In scope: the above. Deferred: **STP tree/convergence engine** (per-VLAN root
election + port roles + blocked-set, validated against live `stp_state` — the
prerequisite for ever granting SAFE here); per-VLAN priority divergence (Mist
config cannot express it — `stp_config` carries only `bridge_priority`);
mixed-protocol (VSTP↔RSTP) interop simulation; LAG/ESI-LAG path costs
(aggregates unmodeled); `PortMisc.inter_switch_link` bool-token harmonization
(already on the roadmap).
