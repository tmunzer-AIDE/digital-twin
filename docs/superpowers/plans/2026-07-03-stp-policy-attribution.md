# STP Policy Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Graduate `stp_required`, `stp_no_root_port`, `stp_p2p`, `use_vstp` from generic `unmodeled_change` REVIEW to a precise `wired.stp.policy` check — UNSAFE escalation for concrete predicted harm, a `policy_change` REVIEW floor for everything else, SAFE structurally impossible.

**Architecture:** New frozen `StpPolicy` value object on `Port` (PortAuth pattern, `bool | str` token honesty); `PortMisc` shrinks by four; new check `wired.stp.policy` with four codes (`blocking_risk`, `root_protect_risk`, `link_mismatch`, `policy_change`) and a precise `changed_fields`-based `applies_to`; gates untouched (leaves already placed by Spec-1).

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy strict on src. Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src` (pytest `-q` prints no summary line — all-dots = pass).

**Spec:** `docs/superpowers/specs/2026-07-03-stp-policy-attribution-design.md` (approved; P2 folded: unknown/no-peer → floor, never `blocking_risk`).

**Baseline:** branch `feat/stp-policy` off main@`56f9ee5` (includes Spec-1 PR #41).

---

## Pre-resolved code facts (verified 2026-07-03 — reuse, don't reinvent)

- **Election:** `checks/wired/stp_root.py:_root_of(ir, component)` returns `(root_device_id, any_default_assumed: bool)` on success, or a str/None sentinel for unelectable — read its docstring/callers before use. `any_default_assumed=True` or a non-tuple return ⇒ the election is NOT HIGH (root-protect ERROR gate).
- **Peer classification template:** `checks/wired/admin_disable.py` — `_ap_ports`-style AP ties and `_nonap_peer_links(base_ir)` returning `dict[port_id, Link]` where the LINK carries the tie confidence. Clone the idiom, don't import private helpers across checks unless they already live in a shared module — if a helper is private to admin_disable, COPY the small idiom into the new check with a comment, or lift it to `analysis/` only if the diff stays small (implementer's call, reviewer checks).
- **Occupants:** `checks/wired/l2_isolation.py:_occupants(ir)` → per-node occupant counts (same cross-check rule as above applies).
- **Peer entity kinds:** `ir.devices[node].role is DeviceRole.AP`; observed wired clients live on ports (see how `client_impact`/`admin_disable` find wired clients per port).
- **`Port.bpdu_filter: bool`** at `ir/entities.py:241` (drops BPDUs — its comment explains vs stp_edge).
- **Diff:** `ir/diff.py` — diff entries carry `changed_fields: tuple[str, ...]` (line ~54) plus kind/key/action; `IRDiff.touches(kind)` is the coarse gate. The check's `applies_to` must use the ENTRY-level API: read `ir/diff.py` for the exact entry container name (entries list + action enum) before writing it.
- **Token parser:** `_bool_token` in `adapters/mist/ingest/switch.py` (Spec-1). `_port_misc`, `_port_auth` are the reader templates.
- **`_MISC_ATTRS`** in `adapters/mist/ingest/ports.py` currently `("voip_network", "mac_limit", "storm_control", "inter_switch_link", "use_vstp", "stp_p2p", "stp_no_root_port")` — the STP trio must KEEP flowing from local_port_config after this change (only the destination object changes). `stp_required` is usage-only (never local).
- **Registry:** `checks/wired/__init__.py:ALL_WIRED_CHECKS` (26 entries). README carries a 28-row inventory table pinned by `tests/checks/test_registry_inventory.py` — adding a check requires README row + count updates or that test fails (by design).
- **Check test idioms:** `tests/checks/test_unmodeled_change.py` (misc-flip helper), `tests/checks/test_admin_disable.py` (peer-tie fixtures), `tests/checks/test_stp_root.py` (election fixtures), `tests/factories` (sw/access_port/trunk_port/link/irb; `link(..., prov=Provenance.LLDP_ONE_SIDED)` for one-sided ties — see `test_l2_blackhole.py:156`).

**Global constraints (bind every task):**
- Never false-SAFE: every port whose `stp_policy` changed yields ≥1 finding (`.policy_change` floor at minimum). ERROR only at HIGH evidence (`blocking_risk`: HIGH peer tie; `root_protect_risk`: HIGH election + only-path). Unknown/no-peer and `unresolved:` tokens NEVER produce precise codes — floor + note only (spec P2).
- Pre-existing (not delta-caused) violations → INFO context.
- Gates untouched: no allowlist changes (Spec-1 placed all four leaves); device-profile pins must stay green.
- `Port.stp_policy is None` ⇔ whole surface default.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: `StpPolicy` entity + ingest migration

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (StpPolicy, Port.stp_policy, PortMisc shrink)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_stp_policy`, `_port_misc` shrink)
- Modify: `src/digital_twin/adapters/mist/ingest/ports.py` (`_MISC_ATTRS` comment only — set unchanged)
- Test: `tests/adapters/mist/test_ingest_switch.py`, `tests/ir/test_port_misc.py` (extend/adjust)

- [ ] **Step 1: Write the failing ingest tests** (beside the Spec-1 `_port_misc`/`_port_auth` tests):

```python
def test_stp_policy_reads_the_four_knobs_with_token_honesty():
    from digital_twin.adapters.mist.ingest.switch import _stp_policy

    assert _stp_policy({}) is None
    assert _stp_policy({"stp_p2p": False}) is None  # explicit default == absent
    p = _stp_policy({"stp_required": True, "use_vstp": True})
    assert p is not None and p.stp_required is True and p.use_vstp is True
    assert p.stp_no_root_port is False and p.stp_p2p is False
    t = _stp_policy({"use_vstp": "{{vstp}}"})
    assert t is not None and t.use_vstp == "unresolved:{{vstp}}"  # NOT True


def test_port_misc_no_longer_carries_the_stp_policy_knobs():
    from digital_twin.adapters.mist.ingest.switch import _port_misc

    # the four knobs now land in StpPolicy; a knobs-only usage yields no PortMisc
    assert _port_misc({"stp_required": True, "use_vstp": True,
                       "stp_p2p": True, "stp_no_root_port": True}) is None
```

- [ ] **Step 2: Run to verify failure** (`uv run pytest tests/adapters/mist/test_ingest_switch.py -q` → import/attribute errors).

- [ ] **Step 3: Implement `StpPolicy`** in `entities.py` (place after `PortMisc`):

```python
@dataclass(frozen=True)
class StpPolicy:
    """Spec-2 STP policy knobs (graduated from PortMisc; wired.stp.policy is
    the consumer). Frozen + comparable; Port.stp_policy is None ONLY when all
    are default. bool | str: a templated/unparseable value stays a
    diff-bearing `unresolved:` token (_bool_token), never collapsed to a bool
    — tokens can never produce (or suppress) a precise prediction."""

    stp_required: bool | str = False
    stp_no_root_port: bool | str = False
    stp_p2p: bool | str = False
    use_vstp: bool | str = False
```

Add `stp_policy: StpPolicy | None = None` to `Port` (next to `misc`); export `StpPolicy` from `ir/__init__.py`. Shrink `PortMisc` to five fields (remove the four; update its docstring: "…the four STP policy knobs graduated to StpPolicy in Spec-2"). Grep `PortMisc(` across src/ and tests/ for construction fallout (Spec-1's tests construct with the STP knobs — those move to StpPolicy constructions).

- [ ] **Step 4: Implement `_stp_policy`** in `switch.py` beside `_port_misc`:

```python
def _stp_policy(usage: dict[str, Any]) -> StpPolicy | None:
    p = StpPolicy(
        stp_required=_bool_token(usage.get("stp_required")),
        stp_no_root_port=_bool_token(usage.get("stp_no_root_port")),
        stp_p2p=_bool_token(usage.get("stp_p2p")),
        use_vstp=_bool_token(usage.get("use_vstp")),
    )
    return p if p != StpPolicy() else None
```

Remove the four reads from `_port_misc`; wire `stp_policy=_stp_policy(usage)` at the `Port(...)` construction site where `misc=_port_misc(usage)` is passed (find it; there may be more than one — gateway ports likely don't carry usages, verify). Update the `_MISC_ATTRS` comment in `ports.py`: the STP trio still flows from local (destination is now StpPolicy).

- [ ] **Step 5: Full gate; fix constructor fallout only** (Spec-1 ingest/check tests that constructed `PortMisc` with STP knobs move to `StpPolicy`; do NOT touch `unmodeled_change` check behavior yet — if its tests fail because the knobs left `PortMisc`, that IS this migration: update those tests to construct the remaining-five knobs, and leave the four-knob coverage to Task 2's new pins). Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(ir): StpPolicy value object — the four STP knobs graduate from PortMisc"
```

---

### Task 2: migration pins on `unmodeled_change`

**Files:**
- Modify: `src/digital_twin/checks/wired/unmodeled_change.py` (docstring only)
- Test: `tests/checks/test_unmodeled_change.py`

- [ ] **Step 1: Write the pins:**

```python
def test_stp_policy_knobs_no_longer_wake_unmodeled_change():
    # Spec-2: the four knobs moved to Port.stp_policy / wired.stp.policy —
    # a knobs-only flip produces NO unmodeled_change finding (the new check's
    # floor carries the REVIEW; pinned in tests/checks/test_stp_policy.py)
    for knob in ("stp_required", "stp_no_root_port", "stp_p2p", "use_vstp"):
        result = _run_with_stp_policy_flip(knob, True)  # helper: baseline
        # default policy, proposed replace(port, stp_policy=StpPolicy(**{knob: True}))
        assert result.status is Status.PASS and not result.findings, knob


def test_remaining_misc_knobs_still_wake_unmodeled_change():
    for knob, value in [("inter_switch_link", True), ("storm_control", "no_broadcast=True"),
                        ("poe_priority", "high"), ("community_vlan_id", 811),
                        ("inter_isolation_network_link", True)]:
        result = _run_with_misc_flip(knob, value)
        assert result.findings and result.findings[0].severity is Severity.WARNING, knob
```

(Reuse/adapt the file's existing `_run_with_misc_flip` helper; add the sibling `_run_with_stp_policy_flip`. The #38/#40 named tests must remain green.)

- [ ] **Step 2: Run; the first pin should already PASS after Task 1 (regression pin), the second must PASS unchanged.**

- [ ] **Step 3: Update the module docstring** (drop the four knobs from the enumerated list, note the graduation).

- [ ] **Step 4: Gate + commit**

```bash
git add -A && git commit -m "test(checks): pin the StpPolicy graduation out of unmodeled_change"
```

---

### Task 3: check skeleton — `applies_to` + `.policy_change` floor + registry

**Files:**
- Create: `src/digital_twin/checks/wired/stp_policy.py`
- Modify: `src/digital_twin/checks/wired/__init__.py` (register)
- Modify: `README.md` (+1 row, counts 28→29, 26→27 wired/wireless), `tests/checks/test_registry_inventory.py` expectations if hardcoded
- Test: `tests/checks/test_stp_policy.py` (new)

- [ ] **Step 1: Write the failing tests** (new file; clone fixture idioms from `test_unmodeled_change.py`/`test_admin_disable.py`):

```python
def test_any_stp_policy_change_floors_review_via_policy_change():
    result = _run_flip("stp_p2p", True)  # helper: quiet 2-switch topology
    f = result.findings[0]
    assert f.code == "wired.stp.policy.policy_change"
    assert f.severity is Severity.WARNING
    assert f.confidence.level is ConfidenceLevel.MEDIUM
    assert result.status is Status.WARN


def test_unresolved_token_lands_on_the_floor_with_a_note():
    result = _run_flip("use_vstp", "{{vstp}}")
    assert result.findings[0].code == "wired.stp.policy.policy_change"
    assert any("unresolved" in n for n in result.coverage.notes)
    assert result.coverage.state is CoverageState.PARTIAL


def test_unrelated_port_change_does_not_wake_the_check():
    # description-only delta: applies_to must be False (changed_fields-precise)
    check = StpPolicyCheck()
    assert check.applies_to(_diff_with_description_only_change()) is False


def test_port_add_and_remove_wake_the_check():
    check = StpPolicyCheck()
    assert check.applies_to(_diff_with_port_added_carrying_policy()) is True


def test_no_stp_policy_fixture_can_resolve_safe():
    # structural guard: every fixture in this module that changes stp_policy
    # must yield >=1 finding from this check (the floor makes SAFE impossible)
    for knob in ("stp_required", "stp_no_root_port", "stp_p2p", "use_vstp"):
        assert _run_flip(knob, True).findings, knob
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement the skeleton:**

```python
"""wired.stp.policy — precise STP policy attribution under a REVIEW floor.

The four StpPolicy knobs are modeled but the bridge domain is not provable
(unmanaged switches, invisible BPDU sources, off-fabric roots, convergence),
so a policy change NEVER resolves SAFE in this slice: concrete predicted harm
escalates (.blocking_risk / .root_protect_risk, ERROR only at HIGH evidence);
everything else floors REVIEW via .policy_change. SAFE is deferred to a
future STP tree engine validated against live stp_state (see the 2026-07-03
spec)."""


class StpPolicyCheck:
    id = "wired.stp.policy"
    title = "STP policy change — blocking/root-protect/mismatch attribution"
    domain = "wired.stp"
    default_severity = Severity.WARNING

    def requires(self) -> frozenset[Capability]:
        return frozenset({IRCapability.WIRED_L2})

    def applies_to(self, diff: IRDiff) -> bool:
        # precise: a port entry added/removed, or stp_policy among its
        # changed fields — an unrelated port edit must not wake this check
        return any(
            e.kind == "port"
            and (e.action is not DiffAction.CHANGED or "stp_policy" in e.changed_fields)
            for e in diff.entries
        )
```

(Adapt entry/action names to the REAL `ir/diff.py` API — read it first; the plan's names are intent, the file is authority.) `run()` v1: collect ports whose `stp_policy` changed between baseline/proposed (plus adds/removes with non-None policy); emit one `.policy_change` WARNING/MEDIUM per port naming the changed knobs (diff the two StpPolicy objects field-wise — same `dataclasses.fields` idiom as `unmodeled_change._changed`); any `unresolved:` token among changed values → coverage note + PARTIAL. Register in `ALL_WIRED_CHECKS` (alphabetical/near stp_edge/stp_root). Update README row + counts; run the inventory test.

- [ ] **Step 4: Gate + commit**

```bash
git add -A && git commit -m "feat(checks): wired.stp.policy skeleton — precise applies_to + policy_change floor"
```

---

### Task 4: `.blocking_risk`

**Files:**
- Modify: `src/digital_twin/checks/wired/stp_policy.py`
- Test: `tests/checks/test_stp_policy.py`

- [ ] **Step 1: Write the failing tier tests** — one per spec-table row + P2 + direction/pre-existing:

```python
def test_blocking_risk_ap_peer_two_sided_is_error_high():
    result = _run_enable_stp_required(peer="ap", tie="two_sided")
    f = _find(result, "wired.stp.policy.blocking_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["peer_kind"] == "ap"
    # ERROR -> UNSAFE at the decision layer (assert via decide() like
    # test_l2_blackhole's decision-level asserts)


def test_blocking_risk_wired_client_no_bridge_peer_is_error_high(): ...
def test_blocking_risk_bpdu_filter_peer_two_sided_is_error_high(): ...
def test_blocking_risk_one_sided_ap_tie_is_warning_medium(): ...


def test_unknown_peer_is_floor_plus_note_not_blocking_risk():
    # spec P2: no peer evidence -> the model cannot claim "peer won't send
    # BPDUs"; floor + coverage note, NO blocking_risk
    result = _run_enable_stp_required(peer=None)
    assert not _findall(result, "wired.stp.policy.blocking_risk")
    assert _find(result, "wired.stp.policy.policy_change")
    assert any("peer unobserved" in n for n in result.coverage.notes)


def test_disabling_stp_required_is_floor_only(): ...
def test_preexisting_stp_required_on_ap_port_untouched_is_info_context(): ...
```

(`...` bodies: same fixture family, different peer/direction — write them fully in the implementation, each pinning code+severity+confidence+evidence. `_run_enable_stp_required` builds: 2 switches linked, target access port on A with the peer variant — AP device + link with chosen provenance / wired client record / peer switch port with `bpdu_filter=True` — baseline default policy, proposed `stp_required=True`.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Classification order per port with newly-True `stp_required` (False/absent→True only; token never reaches here): (1) LLDP-tied AP peer (link confidence HIGH→ERROR/HIGH, one-sided→WARNING/MEDIUM); (2) observed wired client on the port with no modeled bridge peer → ERROR/HIGH; (3) modeled peer port `bpdu_filter is True` (two-sided→ERROR/HIGH, else WARNING/MEDIUM); (4) otherwise → NOT this code — fall to floor + note "stp_required enabled on <port>: peer unobserved — blocking outcome not assessable". Evidence: `peer`, `peer_kind` ("ap"|"client"|"bpdu_filter"), tie provenance, `occupants_behind` (occupant counts for the nodes behind the port — reuse/clone `_occupants`), `severity_reason`. Suppress the port's `.policy_change` when this fires (most-specific precedence). Pre-existing True (baseline already True, delta touches something else on the port) → INFO context finding, existing convention wording.

- [ ] **Step 4: Gate + commit**

```bash
git add -A && git commit -m "feat(checks): stp.policy.blocking_risk — no-BPDU peer tiers (ERROR at HIGH tie)"
```

---

### Task 5: `.root_protect_risk`

**Files:**
- Modify: `src/digital_twin/checks/wired/stp_policy.py`
- Test: `tests/checks/test_stp_policy.py`

- [ ] **Step 1: Failing tests:**

```python
def test_root_protect_on_only_path_to_high_root_is_error_high():
    # A(prio 32768) - B(prio 4096, root); the only A->B edge gets
    # stp_no_root_port=True -> A can never accept its root port -> blocks
    result = _run_enable_no_root_port(topology="chain", priorities={"A": 32768, "B": 4096})
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.ERROR and f.confidence.level is ConfidenceLevel.HIGH
    assert f.evidence["elected_root"] == "B" and f.evidence["only_path"] is True


def test_root_protect_with_redundant_path_is_floor_only():
    result = _run_enable_no_root_port(topology="triangle", priorities={"A": 32768, "B": 4096, "C": 32768})
    assert not _findall(result, "wired.stp.policy.root_protect_risk")
    assert _find(result, "wired.stp.policy.policy_change")


def test_root_protect_with_unprovable_election_is_warning_plus_note():
    # any stp_priority_invalid / default-assumed priority in the component:
    # ERROR requires the elected root known at HIGH — degrade, never guess
    result = _run_enable_no_root_port(topology="chain", priorities={"A": None, "B": 4096})
    f = _find(result, "wired.stp.policy.root_protect_risk")
    assert f.severity is Severity.WARNING
    assert any("root election" in n for n in result.coverage.notes)
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** For each port with newly-True `stp_no_root_port`: elect the proposed component's root via `_root_of` (reuse/clone per the cross-check rule). Election HIGH ⇔ `_root_of` returned a tuple with `any_default_assumed is False` (and no `stp_priority_invalid` in the component — check what `_root_of` already folds in; read it). Only-path test: on the component graph, drop this port's edge; if the elected root is unreachable from the port's device → only path. HIGH election + only-path → ERROR/HIGH; only-path but election not HIGH → WARNING + note "root election not provable — root-protect risk assessed at reduced confidence"; redundant path → no risk code (floor covers the change); device IS the root → no risk (note in evidence). Evidence: `elected_root`, `only_path`, `election_confidence`, `severity_reason`. Suppresses the port's floor like Task 4.

- [ ] **Step 4: Gate + commit**

```bash
git add -A && git commit -m "feat(checks): stp.policy.root_protect_risk — only-path-to-root, ERROR gated on HIGH election"
```

---

### Task 6: `.link_mismatch`

**Files:**
- Modify: `src/digital_twin/checks/wired/stp_policy.py`
- Test: `tests/checks/test_stp_policy.py`

- [ ] **Step 1: Failing tests:**

```python
def test_use_vstp_mismatch_on_modeled_link_is_warning():
    result = _run_one_end_flip("use_vstp")  # A:up use_vstp=True, B:down default
    f = _find(result, "wired.stp.policy.link_mismatch")
    assert f.severity is Severity.WARNING
    assert f.evidence["knob"] == "use_vstp"


def test_both_knobs_mismatched_yield_two_findings_keyed_by_link_and_knob():
    result = _run_one_end_flip("use_vstp", "stp_p2p")
    mm = _findall(result, "wired.stp.policy.link_mismatch")
    assert {f.evidence["knob"] for f in mm} == {"use_vstp", "stp_p2p"}
    assert len({(f.evidence["link"], f.evidence["knob"]) for f in mm}) == 2


def test_preexisting_mismatch_touched_is_info():
    # both states mismatch identically; delta touches another stp_policy knob
    ...


def test_observed_stp_mode_lands_in_evidence_when_present():
    ...
```

(Fill `...` bodies fully; the stp_mode fixture sets `Port.stp_mode` via `dataclasses.replace` — it's an OBSERVED field, see how lldp-ingest tests build it.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** For each modeled link where the delta changed `use_vstp` or `stp_p2p` on either end: compare the two ends' effective values per knob (tokens excluded — token → floor path). Disagreement introduced/changed by the delta → one WARNING finding per `(link, knob)`; confidence = link tie confidence (HIGH two-sided, MEDIUM below). Pre-existing disagreement merely touched → INFO. Evidence: `link`, `knob`, `values` (both ends), `observed_modes` (both ends' `Port.stp_mode` when present). Link findings COEXIST with port-level findings and do NOT suppress the floor for other changed knobs on the same port (spec precedence).

- [ ] **Step 4: Gate + commit**

```bash
git add -A && git commit -m "feat(checks): stp.policy.link_mismatch — (link, knob)-keyed vstp/p2p disagreement"
```

---

### Task 7: e2e + precedence + device-profile pins

**Files:**
- Test: `tests/engine/test_pipeline.py` (extend, Spec-1 harness idioms)

- [ ] **Step 1: Write the e2e tests:**

- `port_usages.*.stp_required` enabled on an AP-facing port (fixture world has the LLDP tie) → decision **UNSAFE**, findings contain `wired.stp.policy.blocking_risk` ERROR.
- `port_usages.*.stp_p2p` flip on a quiet topology → decision **REVIEW** via `.policy_change`; NO `unmodeled_change` finding for it.
- Benign leaf (`ui_evpntopo_id`) + `stp_p2p` in one op → REVIEW (floor wins over benign-SAFE).
- Parametrized guard: each of the four knobs changed alone → decision is NEVER SAFE.
- Spec-1's below-profile device-profile pins still green (no new test — just named in the gate run).

- [ ] **Step 2: Run; full gate.**

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test(e2e): stp.policy UNSAFE/REVIEW routes + never-SAFE guard"
```

---

### Task 8: docs wrap + live-verify prep

**Files:**
- Modify: `docs/superpowers/specs/2026-07-03-stp-policy-attribution-design.md` (Status → Implemented)
- Modify: `docs/ROADMAP.md` (Spec-2 done; STP tree engine stays deferred)

- [ ] **Step 1: Full gate** (STOP if red).
- [ ] **Step 2:** grep sweep: no `stp_required|stp_no_root_port|stp_p2p|use_vstp` reads left in `_port_misc`/`unmodeled_change` (comments fine); README row present; inventory test green.
- [ ] **Step 3:** spec Status → `Implemented (2026-07-03; plan docs/superpowers/plans/2026-07-03-stp-policy-attribution.md)`; ROADMAP entry.
- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: Spec-2 STP policy attribution implemented"
```

(Live verification against the org via the Mist MCP runs from the controller session after the branch review — read-only: real `stp_state`/`stp_mode` shapes; simulate `stp_required` on an AP-facing port → `.blocking_risk`.)

---

## Self-review checklist

1. **Spec coverage:** StpPolicy migration (T1), unmodeled_change graduation pins (T2), precise applies_to + floor + tokens (T3), blocking_risk tiers incl. P2 (T4), root_protect HIGH gate (T5), (link,knob) mismatch (T6), e2e + never-SAFE guard (T7), README/registry/docs (T3/T8). Pre-existing→INFO covered in T4/T6.
2. **Placeholders:** T4/T6 contain `...` sibling-test stubs by design — each names its exact fixture variant and the assertions to pin; implementers write them fully (same convention as Spec-1's plan, which executed cleanly).
3. **Type consistency:** `StpPolicy` fields `bool | str` everywhere; `_stp_policy` returns `StpPolicy | None`; check reads `Port.stp_policy` only.
