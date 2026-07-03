# Port-Profile Attribute Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the remaining 12 switch port-profile attributes so benign-only edits resolve SAFE and non-modeled knobs produce a specific REVIEW, never an unhelpful coverage gap — with the no-false-SAFE boundary intact.

**Architecture:** Two new allowlist groups (`_BENIGN_PROFILE_USAGE_ATTRS` = allowed-but-ignored; `_USAGE_ONLY_REVIEWED_ATTRS` = reviewed leaves the OAS puts on `port_usages` only), `enable_qos` moves from the REVIEW path to benign, `bypass_auth_when_server_down_for_voip` joins `PortAuth` (surfaced by the existing `wired.auth.access_change.policy_change`), and the 6 other reviewed knobs join `PortMisc` (surfaced by the existing `wired.port.unmodeled_change.recognized`).

**Tech Stack:** Python 3.14, uv, pytest, ruff (100-col), mypy strict on src. Gate: `uv run pytest tests -q && uv run ruff check . && uv run mypy src` (pytest `-q` prints no summary line — all-dots = pass).

**Spec:** `docs/superpowers/specs/2026-06-29-port-profile-attribute-completion-design.md` (approved after 3 review rounds).

**Baseline:** branch `feat/port-profile-completion` off main@`2bd2ab8` (includes PRs #35–#40; #40 added confidence assertions to `tests/checks/test_unmodeled_change.py` that Task 4's rewrite must preserve, and #38 added `test_inter_switch_link_change_is_review`, `test_storm_control_change_is_review`, `test_misc_object_flip_without_recognized_knob_is_silent` to `tests/checks/test_unmodeled_change.py` — they must keep passing, and Task 4 REPLACES `test_enable_qos_change_is_review_not_unknown`).

---

## Pre-resolved design facts (verified against the repo on 2026-07-03 — do not re-derive)

**OAS state.** `device_switch.schema.json` is already current: its `port_usages` value-schema has all 46 keys including the 12 attrs here. `site_setting.schema.json` and `networktemplate.schema.json` `port_usages` are missing the same 4: `poe_keep_state_when_reboot`, `server_fail_retry_interval`, `bypass_auth_when_server_down_for_voip`, `poe_priority`. Map placement (from `device_switch`, the closed/refreshed schema):

| attribute | `port_config` | `port_config_overwrite` | `local_port_config` | `port_usages` |
|---|---:|---:|---:|---:|
| `ui_evpntopo_id` | no | no | no | yes |
| `enable_qos` | no | no | yes | yes |
| `poe_keep_state_when_reboot` | no | yes | no | yes |
| `server_fail_retry_interval` | no | no | no | yes |
| `bypass_auth_when_server_down_for_voip` | no | no | **no** | yes |
| `poe_priority` | no | no | no | yes |
| `community_vlan_id` | no | no | no | yes |
| `inter_isolation_network_link` | no | no | no | yes |
| `stp_required` | no | no | no | yes |
| `stp_no_root_port` | no | no | yes | yes |
| `stp_p2p` | no | no | yes | yes |
| `use_vstp` | no | no | yes | yes |

This confirms the spec's expected table, including the split the spec anticipated: `bypass_auth_when_server_down_for_voip` is **usage-only** (unlike the other 14 `_AUTH_ATTRS`, which are local-capable). Consequence: on the ALLOWLIST side it must NOT ride `_AUTH_ATTRS` (that would dead-allow a `local_port_config` leaf the OAS says doesn't exist); it goes into `_USAGE_ONLY_REVIEWED_ATTRS`. On the INGEST side it still joins `PortAuth`/`_port_auth` (usage-level attrs flow via `usage_definition`, not `_AUTH_ATTRS`/`_LOCAL_ATTRS`, so no `ingest/ports.py` auth change is needed).

**`ui_evpntopo_id` validation gate (spec task-1 obligation) — RESOLVED cosmetic.** The OAS entry is `{"type": "string", "format": "uuid", "description": "Optional for Campus Fabric Core-Distribution ESI-LAG profile. Helper used by the UI to select this port profile as the ESI-Lag between Distribution and Access switches"}`. It is a UI selection helper id; the actual ESI-LAG membership lives in other leaves (`esilag`/`aggregated` surfaces, out of scope, still gap-floored). It stays SAFE. Task 1 pins the type/format so a future OAS refresh that changes its shape fails the test and forces re-evaluation.

**OAS types for new `PortMisc`/`PortAuth` fields:** `poe_priority`: string enum `low|high`; `community_vlan_id`: integer; `inter_isolation_network_link`, `stp_required`, `stp_no_root_port`, `stp_p2p`, `use_vstp`, `bypass_auth_when_server_down_for_voip`, `poe_keep_state_when_reboot`: boolean (default false); `server_fail_retry_interval`: integer (default 120).

**`wired.auth.access_change`** fires `.policy_change` on ANY `PortAuth` inequality (`checks/wired/auth_change.py`) — adding the field to `PortAuth` + `_port_auth` is sufficient; no check change.

**Global constraints (bind every task):**
- Never false-SAFE: an unclassified leaf stays a coverage gap; a reviewed leaf must reach ALL THREE gates (raw allowlist, effective allowlist, device-profile overridable list — the last via `_USAGE_LEAVES`); benign leaves reach raw+effective but deliberately NOT the device-profile modeled surface.
- Benign leaves are never read by ingest and never enter the IR.
- `_port_misc` returns `None` when every field is default; `Port.auth is None` only when the whole auth surface is default.
- No behavior change to `inter_switch_link` / `storm_control` REVIEW handling.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: OAS refresh + placement pins + `ui_evpntopo_id` ruling

**Files:**
- Modify: `src/digital_twin/adapters/mist/oas/site_setting.schema.json` (port_usages value-schema)
- Modify: `src/digital_twin/adapters/mist/oas/networktemplate.schema.json` (port_usages value-schema)
- Create: `tests/adapters/mist/test_oas_port_usage_placement.py`

- [ ] **Step 1: Write the failing placement test**

```python
"""Placement pins for the 12 Spec-1 port-profile attributes (2026-06-29 spec).

The allowlist gates are hand-derived from these committed OAS extracts; if a
leaf is absent here, the L0 unknown-key walker floors payloads carrying it, so
the extracts MUST carry every classified leaf on exactly the maps Mist
documents. device_switch is the refreshed authority; site_setting and
networktemplate must agree with it for port_usages.
"""

from __future__ import annotations

import json
from pathlib import Path

OAS = Path("src/digital_twin/adapters/mist/oas")

SPEC1_ATTRS = (
    "ui_evpntopo_id", "enable_qos", "poe_keep_state_when_reboot",
    "server_fail_retry_interval", "bypass_auth_when_server_down_for_voip",
    "poe_priority", "community_vlan_id", "inter_isolation_network_link",
    "stp_required", "stp_no_root_port", "stp_p2p", "use_vstp",
)


def _map_props(schema_file: str, map_name: str) -> set[str]:
    schema = json.loads((OAS / f"{schema_file}.schema.json").read_text())
    node = schema.get("properties", {}).get(map_name, {})
    ap = node.get("additionalProperties")
    if isinstance(ap, dict) and "properties" in ap:
        return set(ap["properties"])
    pp = node.get("patternProperties")
    if isinstance(pp, dict):
        return set(next(iter(pp.values()), {}).get("properties", {}))
    return set()


def _every_port_usages_occurrence(schema_file: str) -> list[tuple[str, set[str]]]:
    """(json-path, value-schema property set) for EVERY `port_usages` node in the
    file — site_setting carries a SECOND copy nested at
    properties.switch.allOf[0].properties.port_usages, and updating only the
    top-level one would leave the nested copy stale (review finding P2)."""
    schema = json.loads((OAS / f"{schema_file}.schema.json").read_text())
    found: list[tuple[str, set[str]]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}"
                if k == "port_usages" and isinstance(v, dict):
                    props = (v.get("additionalProperties") or {}).get("properties") or {}
                    found.append((p, set(props)))
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(schema, schema_file)
    return found


def test_every_port_usages_occurrence_carries_all_spec1_attrs():
    # walks ALL occurrences (site_setting has 2: top-level + switch.allOf[0])
    for f in ("site_setting", "networktemplate", "device_switch"):
        occurrences = _every_port_usages_occurrence(f)
        assert occurrences, f"{f}: no port_usages found"
        for path, props in occurrences:
            missing = [a for a in SPEC1_ATTRS if a not in props]
            assert not missing, f"{path} missing {missing}"


def test_inline_map_placement_matches_the_spec_table():
    # device_switch is the only schema carrying the inline device maps
    assert not set(SPEC1_ATTRS) & _map_props("device_switch", "port_config")
    assert set(SPEC1_ATTRS) & _map_props("device_switch", "local_port_config") == {
        "enable_qos", "stp_no_root_port", "stp_p2p", "use_vstp",
    }
    assert set(SPEC1_ATTRS) & _map_props("device_switch", "port_config_overwrite") == {
        "poe_keep_state_when_reboot",
    }


def test_ui_evpntopo_id_is_still_a_cosmetic_selection_id():
    # Spec 1's validation gate, pinned: SAFE classification rests on this being
    # a UI selection helper (uuid string), not a forwarding-config leaf. If a
    # future OAS refresh changes its shape, this fails and the classification
    # must be re-argued before the gate change ships.
    schema = json.loads((OAS / "device_switch.schema.json").read_text())
    node = schema["properties"]["port_usages"]["additionalProperties"]["properties"][
        "ui_evpntopo_id"
    ]
    assert node["type"] == "string" and node.get("format") == "uuid"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/adapters/mist/test_oas_port_usage_placement.py -v`
Expected: `test_every_port_usages_occurrence_carries_all_spec1_attrs` FAILS naming the 4 missing attrs for site_setting (BOTH occurrences) and networktemplate; the other two tests PASS.

- [ ] **Step 3: Refresh the two stale extracts**

Copy these 4 property definitions **verbatim from `device_switch.schema.json`'s `port_usages` value-schema** into EVERY stale `port_usages` value-schema `properties` — that is THREE insertion points: `site_setting.schema.json` at `properties.port_usages` AND at `properties.switch.allOf[0].properties.port_usages` (the nested per-switch defaults copy — review finding P2), plus `networktemplate.schema.json` at `properties.port_usages` (alphabetical position within the existing properties, matching the files' ordering):

```json
"bypass_auth_when_server_down_for_voip": {
  "default": false,
  "description": "Only if `mode`!=`dynamic` and `port_auth`==`dot1x`. Bypass auth for VOIP if set to true when RADIUS server is down",
  "type": "boolean"
},
"poe_keep_state_when_reboot": {
  "default": false,
  "description": "Only if `mode`!=`dynamic`. Whether Perpetual PoE is enabled; keeps PoE state across reboots",
  "type": "boolean"
},
"poe_priority": {
  "description": "PoE priority. enum: `low`, `high`",
  "enum": ["low", "high"],
  "type": "string"
},
"server_fail_retry_interval": {
  "default": 120,
  "description": "Only if `mode`!=`dynamic` and `port_auth`==`dot1x`. Interval, in seconds. Sets the wait time before retrying authentication after RADIUS failure to reduce client flapping. Range 120-65535",
  "type": "integer"
}
```

Before pasting, `grep` the actual definitions out of `device_switch.schema.json` and use THOSE exact bodies (the snippets above were extracted 2026-07-03; the file is the authority).

- [ ] **Step 4: Run the placement test + the full L0/validation suites**

Run: `uv run pytest tests/adapters/mist/test_oas_port_usage_placement.py tests/adapters -q`
Expected: PASS (the unknown-key walker now accepts the 4 leaves on templates/site_setting).

- [ ] **Step 5: Commit**

```bash
git add src/digital_twin/adapters/mist/oas/site_setting.schema.json \
        src/digital_twin/adapters/mist/oas/networktemplate.schema.json \
        tests/adapters/mist/test_oas_port_usage_placement.py
git commit -m "feat(oas): sync port_usages extracts with device_switch (+4 attrs) + placement pins"
```

---

### Task 2: Allowlist — benign group, usage-only reviewed group, modeled additions

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py`
- Test: `tests/scope/test_allowlist.py` (extend), `tests/scope/test_field_gate.py` (extend)

- [ ] **Step 1: Write the failing allowlist tests** (append to `tests/scope/test_allowlist.py`; match its existing import style)

```python
def test_spec1_benign_leaves_are_raw_and_effective_but_not_device_profile():
    from digital_twin.scope.allowlist import (
        DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE,
        EFFECTIVE_ALLOWLIST,
        RAW_ALLOWLIST,
    )

    benign = (
        "port_usages.*.ui_evpntopo_id",
        "port_usages.*.enable_qos",
        "port_usages.*.poe_keep_state_when_reboot",
        "port_usages.*.server_fail_retry_interval",
    )
    for leaf in benign:
        assert leaf in RAW_ALLOWLIST["site_setting"], leaf
        assert leaf in RAW_ALLOWLIST["device"], leaf
        assert leaf in EFFECTIVE_ALLOWLIST, leaf
        # benign = ignored by IR; a device-profile overriding it changes nothing,
        # so it must NOT taint profiled switches to UNKNOWN
        assert leaf not in DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"], leaf
    assert "local_port_config.*.enable_qos" in RAW_ALLOWLIST["device"]
    assert "port_config_overwrite.*.poe_keep_state_when_reboot" in RAW_ALLOWLIST["device"]


def test_spec1_reviewed_leaves_are_in_all_three_gates():
    from digital_twin.scope.allowlist import (
        DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE,
        EFFECTIVE_ALLOWLIST,
        RAW_ALLOWLIST,
    )

    reviewed = (
        "bypass_auth_when_server_down_for_voip", "poe_priority", "community_vlan_id",
        "inter_isolation_network_link", "stp_required", "stp_no_root_port",
        "stp_p2p", "use_vstp",
    )
    for attr in reviewed:
        leaf = f"port_usages.*.{attr}"
        assert leaf in RAW_ALLOWLIST["site_setting"], leaf
        assert leaf in EFFECTIVE_ALLOWLIST, leaf
        # the third gate — absent here, a below-profile edit on a profiled
        # switch would resolve REVIEW/SAFE instead of UNKNOWN (false-SAFE)
        assert leaf in DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"], leaf


def test_spec1_usage_only_leaves_are_not_dead_allowed_on_local():
    # refreshed OAS: these do NOT exist on local_port_config — allowing them
    # there would violate the placement contract (spec: "allowlisted only on
    # the maps documented")
    from digital_twin.scope.allowlist import RAW_ALLOWLIST

    for attr in ("bypass_auth_when_server_down_for_voip", "poe_priority",
                 "community_vlan_id", "inter_isolation_network_link", "stp_required"):
        assert f"local_port_config.*.{attr}" not in RAW_ALLOWLIST["device"], attr
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/scope/test_allowlist.py -q`
Expected: the three new tests FAIL (leaves absent).

- [ ] **Step 3: Implement in `src/digital_twin/scope/allowlist.py`**

(a) Add `bypass...for_voip` is NOT added to `_AUTH_ATTRS` — leave `_AUTH_ATTRS` untouched.

(b) Edit `_MODELED_USAGE_ATTRS`: remove `"enable_qos"`, append the local-capable reviewed STP leaves after `"storm_control"`:

```python
_MODELED_USAGE_ATTRS: tuple[str, ...] = (
    "mode",
    "port_network",
    "networks",
    "all_networks",
    "voip_network",
    "poe_disabled",
    "mtu",
    "mac_limit",
    "allow_dhcpd",
    "speed",
    "duplex",
    "disable_autoneg",
    *_AUTH_ATTRS,
    "inter_switch_link",
    "storm_control",
    # Spec 1 reviewed STP knobs — local-capable per the refreshed OAS, so they
    # ride the same local+usage surface as inter_switch_link/storm_control.
    # (enable_qos moved OUT of this tuple: it is benign — allowed by the gates
    # but deliberately ignored by IR; see _BENIGN_PROFILE_USAGE_ATTRS.)
    "stp_no_root_port",
    "stp_p2p",
    "use_vstp",
)
```

(c) Directly below, add the two new groups:

```python
# Spec 1 reviewed leaves the refreshed OAS documents on port_usages ONLY (never
# local_port_config / inline maps). They enter _USAGE_LEAVES — and therefore the
# raw, effective AND device-profile gates — but must NOT ride
# _MODELED_USAGE_ATTRS, which also feeds the local_port_config leaf set (a
# local leaf the OAS says cannot exist would be a dead allow violating the
# placement contract). bypass_auth_when_server_down_for_voip is auth-surface
# (PortAuth) but usage-only, unlike the 14 local-capable _AUTH_ATTRS.
_USAGE_ONLY_REVIEWED_ATTRS: tuple[str, ...] = (
    "bypass_auth_when_server_down_for_voip",
    "poe_priority",
    "community_vlan_id",
    "inter_isolation_network_link",
    "stp_required",
)

# Spec 1 benign SAFE leaves: allowed by the raw + effective gates, deliberately
# NEVER read by ingest and NEVER in the device-profile modeled surface (a
# profile overriding a leaf the IR ignores changes nothing, so it must not
# taint profiled switches to UNKNOWN). ui_evpntopo_id: UI selection helper for
# ESI-LAG profiles (uuid string — pinned by the OAS placement test).
# enable_qos: scheduling knob, M1 usability ruling. poe_keep_state_when_reboot:
# reboot-time behavior only. server_fail_retry_interval: RADIUS retry timing —
# explicit M1 tradeoff, revisit with a RADIUS-outage model (spec 2026-06-29).
_BENIGN_PROFILE_USAGE_ATTRS: tuple[str, ...] = (
    "ui_evpntopo_id",
    "enable_qos",
    "poe_keep_state_when_reboot",
    "server_fail_retry_interval",
)
_BENIGN_USAGE_LEAVES: tuple[str, ...] = tuple(
    f"port_usages.*.{a}" for a in _BENIGN_PROFILE_USAGE_ATTRS
)
```

(d) Extend `_USAGE_LEAVES` to include the usage-only reviewed attrs:

```python
_USAGE_LEAVES: tuple[str, ...] = tuple(
    f"port_usages.*.{a}"
    for a in (
        *_MODELED_USAGE_ATTRS,
        *_USAGE_ONLY_REVIEWED_ATTRS,
        *_DYNAMIC_PROFILE_ATTRS,
        *_STP_USAGE_ATTRS,
    )
)
```

(e) `_LOCAL_PORT_CONFIG_LEAVES`: `enable_qos` left `_MODELED_USAGE_ATTRS`, but the OAS keeps it on `local_port_config` as benign — re-add it explicitly:

```python
_LOCAL_PORT_CONFIG_LEAVES: tuple[str, ...] = tuple(
    f"local_port_config.*.{a}"
    # "enable_qos" is BENIGN (ignored by ingest) but OAS-present on
    # local_port_config, so it stays field-gate-decidable here.
    for a in ("usage", "stp_edge", "disabled", "description", "enable_qos",
              *_MODELED_USAGE_ATTRS)
)
```

(f) `_OVERWRITE_LEAVES`: append `"port_config_overwrite.*.poe_keep_state_when_reboot"` with a `# benign (Spec 1)` comment.

(g) Add `*_BENIGN_USAGE_LEAVES,` to `RAW_ALLOWLIST["site_setting"]`, `RAW_ALLOWLIST["device"]`, and `EFFECTIVE_ALLOWLIST` (place right after `*_USAGE_LEAVES,` in each). `DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE` is untouched — reviewed leaves arrive there automatically via `_USAGE_LEAVES`; benign leaves stay out by construction.

- [ ] **Step 4: Run scope suites**

Run: `uv run pytest tests/scope -q`
Expected: PASS, including all pre-existing field-gate/device-profile tests.

- [ ] **Step 5: Add field-gate decidability tests** (append to `tests/scope/test_field_gate.py`, mirroring the existing `description` cosmetic test at ~line 101 — reuse its helper/fixture idiom exactly as found in the file):

One test per benign bucket asserting a payload changing ONLY that leaf passes the field gate with no rejection and no coverage gap: `port_usages.*.ui_evpntopo_id`, `port_usages.*.enable_qos`, `local_port_config.*.enable_qos`, `port_usages.*.poe_keep_state_when_reboot`, `port_config_overwrite.*.poe_keep_state_when_reboot`, `port_usages.*.server_fail_retry_interval`. Plus one regression: a fabricated unknown leaf (e.g. `port_usages.*.not_a_real_knob`) still produces a coverage gap, not a pass.

- [ ] **Step 6: Run + commit**

```bash
uv run pytest tests/scope -q && uv run ruff check . && uv run mypy src
git add src/digital_twin/scope/allowlist.py tests/scope/test_allowlist.py tests/scope/test_field_gate.py
git commit -m "feat(scope): Spec-1 benign + usage-only-reviewed port-profile leaves in the gates"
```

---

### Task 3: `bypass_auth_when_server_down_for_voip` → `PortAuth`

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (PortAuth)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_port_auth`)
- Test: `tests/checks/test_auth_access_change.py` (extend), `tests/adapters/mist/test_ingest_switch.py` (extend)

- [ ] **Step 1: Write the failing ingest test** (in `tests/adapters/mist/test_ingest_switch.py`, next to the existing `_port_auth` tests — find them with `grep -n "port_auth" tests/adapters/mist/test_ingest_switch.py`):

```python
def test_port_auth_lone_voip_bypass_flip_is_non_default():
    # PortAuth invariant: Port.auth is None ONLY when the whole surface is
    # default. A lone False->True flip of the voip bypass must produce a
    # non-default PortAuth so wired.auth.access_change wakes (Spec 1).
    from digital_twin.adapters.mist.ingest.switch import _port_auth

    assert _port_auth({}) is None
    assert _port_auth({"bypass_auth_when_server_down_for_voip": False}) is None
    a = _port_auth({"bypass_auth_when_server_down_for_voip": True})
    assert a is not None and a.bypass_auth_when_server_down_for_voip is True
```

- [ ] **Step 2: Run to verify failure** (AttributeError / None).

- [ ] **Step 3: Implement.** In `entities.py` `PortAuth`, after `bypass_auth_when_server_down_for_unknown_client`:

```python
    bypass_auth_when_server_down_for_voip: bool = False
```

In `switch.py` `_port_auth`, add the matching line where the other bypass fields are read:

```python
        bypass_auth_when_server_down_for_voip=bool(
            usage.get("bypass_auth_when_server_down_for_voip")
        ),
```

- [ ] **Step 4: Write the check-level test** (append to `tests/checks/test_auth_access_change.py`, reusing its fixture idiom):

A baseline port with default auth, proposed identical except `bypass_auth_when_server_down_for_voip=True` on the resolved usage → `wired.auth.access_change.policy_change` finding, `Severity.WARNING`, confidence pinned to whatever the existing policy_change tests pin (read them; do not guess), result status floors REVIEW at the decision layer (assert via the file's existing pattern if it has one, else omit the decision assertion — the e2e in Task 7 covers it).

- [ ] **Step 5: Run + commit**

```bash
uv run pytest tests/adapters/mist/test_ingest_switch.py tests/checks/test_auth_access_change.py -q
git add -A && git commit -m "feat(auth): bypass_auth_when_server_down_for_voip joins PortAuth (policy-floor REVIEW)"
```

---

### Task 4: `PortMisc` rework — 6 reviewed knobs in, `enable_qos` out

**Files:**
- Modify: `src/digital_twin/ir/entities.py` (PortMisc)
- Modify: `src/digital_twin/adapters/mist/ingest/switch.py` (`_port_misc`)
- Modify: `src/digital_twin/adapters/mist/ingest/ports.py` (`_MISC_ATTRS`)
- Modify: `src/digital_twin/checks/wired/unmodeled_change.py`
- Test: `tests/checks/test_unmodeled_change.py`, `tests/adapters/mist/test_ingest_switch.py`

- [ ] **Step 1: Write the failing tests.** In `tests/checks/test_unmodeled_change.py` (match the file's existing fixture helpers — #38 extended it):

```python
def test_each_new_reviewed_knob_is_review():
    # poe_priority (str), community_vlan_id (int), and the PVLAN/STP booleans
    # each wake the recognized-but-unmodeled REVIEW carrier (Spec 1)
    cases = [
        ("poe_priority", "high"),
        ("community_vlan_id", 811),
        ("inter_isolation_network_link", True),
        ("stp_required", True),
        ("stp_no_root_port", True),
        ("stp_p2p", True),
        ("use_vstp", True),
    ]
    for knob, value in cases:
        result = _run_with_misc_flip(knob, value)  # per-file helper; build it on
        # the idiom the existing inter_switch_link/storm_control tests use
        f = result.findings[0]
        assert f.code == "wired.port.unmodeled_change.recognized", knob
        assert f.severity is Severity.WARNING, knob
        assert f.confidence.level is ConfidenceLevel.MEDIUM, knob
        assert knob in f.evidence["knobs"], knob


def test_enable_qos_no_longer_wakes_the_check():
    # Spec 1 moved enable_qos to the benign SAFE group: it must not enter
    # PortMisc, so an enable_qos-only delta produces NO unmodeled_change
    # finding. (Replaces test_enable_qos_change_is_review_not_unknown; the
    # SAFE end-to-end lives in the pipeline suite.)
    result = _run_with_misc_flip("enable_qos", True)
    assert result.status is Status.PASS and not result.findings
```

Adapt `_run_with_misc_flip` to however the existing tests construct baseline/proposed IRs with `Port.misc` (they use `dataclasses.replace` on ports or the usage-dict path — reuse, don't invent). For `enable_qos` the flip goes through `_port_misc({"enable_qos": True})`, which after this task returns `None`.

In `tests/adapters/mist/test_ingest_switch.py`:

```python
def test_port_misc_reads_the_spec1_reviewed_knobs():
    from digital_twin.adapters.mist.ingest.switch import _port_misc

    assert _port_misc({}) is None
    assert _port_misc({"enable_qos": True}) is None  # benign: ignored by ingest
    m = _port_misc({"poe_priority": "high", "community_vlan_id": 811,
                    "stp_p2p": True})
    assert m is not None
    assert m.poe_priority == "high"
    assert m.community_vlan_id == 811
    assert m.stp_p2p is True
    # scalar honesty: templated/unparseable values stay diff-bearing tokens,
    # never collapsed to None/default/bool (the GS27 metric false-SAFE scar)
    t = _port_misc({"community_vlan_id": "{{pvlan}}"})
    assert t is not None and t.community_vlan_id == "unresolved:{{pvlan}}"
    b = _port_misc({"use_vstp": "{{vstp}}"})
    assert b is not None and b.use_vstp == "unresolved:{{vstp}}"  # NOT True
    assert _port_misc({"stp_p2p": False}) is None  # explicit default == absent
```

- [ ] **Step 2: Run to verify failures.**

- [ ] **Step 3: Implement `PortMisc`** in `entities.py`:

```python
@dataclass(frozen=True)
class PortMisc:
    """Recognized-but-unmodeled port knobs (SP4 + Spec 1), surfaced as REVIEW by
    wired.port.unmodeled_change. Frozen + comparable; Port.misc is None ONLY
    when all are default, so a lone flip is detectable. enable_qos left this
    surface in Spec 1 (benign SAFE — ignored by ingest entirely). Spec-1 scalar
    honesty: the new boolean knobs are `bool | str` — a templated/unparseable
    value stays a diff-bearing `unresolved:` token, never collapsed to a bool
    (blanket bool() would turn "{{vstp}}" into True and hide the change)."""

    inter_switch_link: bool = False
    storm_control: str | None = None  # canonical digest of the storm_control object
    poe_priority: str | None = None  # "low" | "high" | None
    community_vlan_id: int | str | None = None  # int, or "unresolved:<raw>" token
    inter_isolation_network_link: bool | str = False
    stp_required: bool | str = False
    stp_no_root_port: bool | str = False
    stp_p2p: bool | str = False
    use_vstp: bool | str = False
```

(`inter_switch_link` keeps its legacy `bool()` coercion deliberately — it predates
Spec 1 and its behavior is pinned by #38's tests; harmonizing it to the token
parser is a separate follow-up because it CHANGES pinned behavior. Note it in
the ROADMAP entry in Task 9.)

(`enable_qos` field REMOVED — grep `src/` and `tests/` for `PortMisc(` constructions and `\.enable_qos` on misc objects and update every site; `tests/golden/builders.py` may construct PortMisc.)

- [ ] **Step 4: Implement `_port_misc`** in `switch.py` (reuse the `_mac_limit` honesty pattern for the int scalar — extract the shared shape only if it stays readable):

```python
def _int_token(v: Any) -> int | str | None:
    """Concrete int / None (absent/empty/bool) / a stable `unresolved:` token
    (templated/unparseable — NEVER collapsed, it must stay change-detecting)."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
        return f"unresolved:{s}" if s else None
    return f"unresolved:{v!r}"


def _bool_token(v: Any) -> bool | str:
    """Concrete bool / False (absent/None/empty) / a stable `unresolved:` token
    (templated/unparseable — NEVER collapsed to a bool; bool("{{vstp}}") would be
    True and hide the change from the diff)."""
    if v is None or v == "":
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip()
        return f"unresolved:{s}" if s else False
    return f"unresolved:{v!r}"


def _port_misc(usage: dict[str, Any]) -> PortMisc | None:
    m = PortMisc(
        inter_switch_link=bool(usage.get("inter_switch_link")),
        storm_control=_storm_digest(usage.get("storm_control")),
        poe_priority=usage.get("poe_priority") or None,
        community_vlan_id=_int_token(usage.get("community_vlan_id")),
        inter_isolation_network_link=_bool_token(usage.get("inter_isolation_network_link")),
        stp_required=_bool_token(usage.get("stp_required")),
        stp_no_root_port=_bool_token(usage.get("stp_no_root_port")),
        stp_p2p=_bool_token(usage.get("stp_p2p")),
        use_vstp=_bool_token(usage.get("use_vstp")),
    )
    return m if m != PortMisc() else None
```

- [ ] **Step 5: Update `_MISC_ATTRS`** in `ingest/ports.py` (local-contribution list — usage-only leaves must NOT be here; `enable_qos` is benign and leaves ingest entirely):

```python
# Misc attrs local_port_config can contribute to the effective usage (OAS:
# present on local_port_config + port_usages; mac_limit also on overwrite).
# Spec 1: + the local-capable reviewed STP knobs; enable_qos REMOVED (benign,
# never read by ingest); usage-only knobs (poe_priority, community_vlan_id,
# inter_isolation_network_link, stp_required, bypass_..._for_voip) are NOT here.
_MISC_ATTRS = (
    "voip_network", "mac_limit", "storm_control", "inter_switch_link",
    "use_vstp", "stp_p2p", "stp_no_root_port",
)
```

- [ ] **Step 6: Update `unmodeled_change.py`** — replace the hand-listed `_changed` with a field loop (auto-syncs with PortMisc), update the docstring and the confidence reason:

```python
"""wired.port.unmodeled_change — a recognized port-profile knob changed whose
impact the twin does not model yet (inter_switch_link, storm_control,
poe_priority, community_vlan_id, inter_isolation_network_link, stp_required,
stp_no_root_port, stp_p2p, use_vstp). The twin recognizes the change and floors
REVIEW — never SAFE, never ERROR/UNSAFE. (enable_qos left this surface in
Spec 1: benign SAFE.)
"""
```

```python
_MEDIUM = Confidence(
    level=ConfidenceLevel.MEDIUM,
    reasons=("the changed knob's impact is not modeled",),
)


def _changed(old: PortMisc | None, new: PortMisc | None) -> list[str]:
    o, n = old or PortMisc(), new or PortMisc()
    return [
        f.name for f in dataclasses.fields(PortMisc)
        if getattr(o, f.name) != getattr(n, f.name)
    ]
```

(add `import dataclasses`; message wording `f"port {pid}: {', '.join(knobs)} changed — impact not modeled (review)"` is already generic — keep it.)

- [ ] **Step 7: Full gate + fix fallout.** `#38`'s `test_inter_switch_link_change_is_review`, `test_storm_control_change_is_review`, `test_misc_object_flip_without_recognized_knob_is_silent` must pass UNCHANGED; the old `test_enable_qos_change_is_review_not_unknown` is deleted (replaced in Step 1). Any golden asserting an `enable_qos` unmodeled_change finding must be updated ONLY if one exists (grep `tests/golden` for `enable_qos`).

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "feat(port): PortMisc gains the Spec-1 reviewed knobs; enable_qos goes benign"
```

---

### Task 5: `no_local_overwrite` ripple — comment + reviewed-STP flip behavior

**Files:**
- Modify: `src/digital_twin/scope/allowlist.py` (the `_PORT_CONFIG_ATTRS` comment block)
- Test: `tests/scope/test_field_gate.py` (extend)

- [ ] **Step 1: Write the failing/behavior tests** (find the existing `_local_overwrite_ripple` tests in `tests/scope/test_field_gate.py` and extend beside them):

```python
def test_no_local_overwrite_flip_over_reviewed_stp_leaves_is_decidable():
    # Spec 1: use_vstp/stp_p2p/stp_no_root_port are now REVIEWED local leaves
    # (they reach PortMisc via _MISC_ATTRS), so a no_local_overwrite flip over a
    # local entry containing only them must PASS the field gate (the PortMisc
    # diff carries the REVIEW) — no coverage gap, no rejection.
    ...


def test_no_local_overwrite_flip_over_an_unmodeled_local_leaf_still_gaps():
    # Dormant backstop: the ripple guard survives Spec 1. Use a real OAS
    # local_port_config leaf that remains unmodeled after Spec 1 (derive it in
    # the test from the committed device_switch OAS minus the allowlisted local
    # attrs — do NOT invent a fake leaf; if the set proves empty, assert that
    # emptiness instead and keep the guard-path unit from the existing suite).
    ...
```

Implementation note for the second test: compute `local_props - allowed_local_attrs` from `device_switch.schema.json` at test time (the OAS has 37 local keys; several — e.g. aggregation/esilag-family leaves — remain unmodeled, so the set is very unlikely to be empty; pick `sorted(...)[0]` deterministically). Fill in the `...` bodies using the SAME payload/fixture idiom the existing ripple tests use — read them first.

- [ ] **Step 2: Run; the first test FAILS on main-before-this-branch behavior only if the ripple still gaps on STP leaves — with Tasks 2+4 landed it may already pass. If it already passes, keep it as a regression pin (that is the point).**

- [ ] **Step 3: Update the stale comment** in `allowlist.py` `_PORT_CONFIG_ATTRS` block: replace the parenthetical `(use_vstp, stp_p2p, stp_no_root_port)` example with wording like "including any local leaves the gates cannot otherwise see (the Spec-1 STP knobs use_vstp/stp_p2p/stp_no_root_port are now REVIEWED via PortMisc; the ripple remains as the backstop for the still-unmodeled remainder of the OAS local map)".

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/scope -q
git add -A && git commit -m "test(scope): no_local_overwrite ripple over Spec-1 reviewed STP leaves + dormant backstop"
```

---

### Task 6: Device-profile third-gate regression pin

**Files:**
- Test: `tests/engine/test_pipeline_device_profile.py` (extend; read `test_below_profile_stp_edit_on_profiled_switch_is_unknown` and clone its harness)

- [ ] **Step 1: Write the pin**

```python
def test_below_profile_poe_priority_edit_on_profiled_switch_is_unknown():
    # Spec 1 third-gate regression: poe_priority is a reviewed usage leaf, so
    # it MUST be in DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE["switch"] — a
    # below-profile edit on a device-profiled switch resolves UNKNOWN (the
    # profile may override it invisibly), never REVIEW or SAFE.
    ...


def test_below_profile_benign_edit_on_profiled_switch_is_not_unknown():
    # The benign twin: ui_evpntopo_id is ignored by the IR, so the same
    # below-profile situation must NOT taint to UNKNOWN.
    ...
```

Fill both bodies from the existing below-profile STP test's harness (same fixture, different leaf). Assert `Decision.UNKNOWN` for the first; for the second assert the decision is NOT UNKNOWN.

- [ ] **Step 2: Run: both should PASS given Tasks 2–4 (they are pins, not drivers). If the first FAILS, the third gate is broken — stop and fix `_USAGE_LEAVES` wiring before proceeding.**

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test(engine): Spec-1 device-profile third-gate pins (reviewed UNKNOWN / benign not)"
```

---

### Task 7: SAFE end-to-end suite

**Files:**
- Test: `tests/engine/test_pipeline.py` (extend; clone the `test_cosmetic_noop_is_safe` harness at ~line 265)

- [ ] **Step 1: Write six SAFE e2e tests**, one per bucket: `port_usages.*.ui_evpntopo_id`, `port_usages.*.enable_qos`, `local_port_config.*.enable_qos`, `port_usages.*.poe_keep_state_when_reboot`, `port_config_overwrite.*.poe_keep_state_when_reboot`, `port_usages.*.server_fail_retry_interval`. Each: simulate a site_setting (or device, for the local/overwrite maps) update changing ONLY that leaf on a fixture that exercises checks; assert

```python
    assert verdict.decision is Decision.SAFE
    assert not any("coverage" in f.code for f in verdict.findings)
    assert not any(f.code.startswith("wired.port.unmodeled_change") for f in verdict.findings)
    assert verdict.config_diffs  # the diff still SHOWS the changed leaf
    # and the changed path appears in the diff:
    assert any(leaf in c.path for d in verdict.config_diffs for c in d.changes)
```

(adapt attribute names for `ObjectConfigDiff`/`FieldChange` to the real contract — read `contracts/` first; a parametrized single test over the six buckets is fine if the harness allows).

- [ ] **Step 2: Run; all six must PASS. A failure here means a gate/ingest leak — debug against Tasks 2/4, do not weaken assertions.**

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test(e2e): Spec-1 benign buckets resolve SAFE with diffs surfaced"
```

---

### Task 8: REVIEW end-to-end + mixed-delta precedence

**Files:**
- Test: `tests/engine/test_pipeline.py` (extend)

- [ ] **Step 1: Write the REVIEW e2e tests** (same harness):

- `port_usages.*.bypass_auth_when_server_down_for_voip` True → decision REVIEW, findings contain `wired.auth.access_change.policy_change`.
- `port_usages.*.poe_priority` → REVIEW with `wired.port.unmodeled_change.recognized`, evidence knobs `["poe_priority"]`.
- one PVLAN leaf (`community_vlan_id`) and one STP leaf (`use_vstp`) → same code, REVIEW.

- [ ] **Step 2: Write the precedence pins:**

- SAFE leaf + REVIEW leaf in one op (e.g. `ui_evpntopo_id` + `poe_priority`) → REVIEW.
- SAFE leaf + a modeled UNSAFE change (reuse an existing UNSAFE scenario from the file, e.g. an uplink de-vlan) in one plan → UNSAFE, and the benign leaf still appears in `config_diffs`.

- [ ] **Step 3: Run + commit**

```bash
uv run pytest tests/engine -q
git add -A && git commit -m "test(e2e): Spec-1 REVIEW routes + SAFE/REVIEW/UNSAFE precedence pins"
```

---

### Task 9: Full gate, docs, wrap

**Files:**
- Modify: `docs/superpowers/specs/2026-06-29-port-profile-attribute-completion-design.md` (Status → Implemented)
- Modify: `ROADMAP.md` (if it tracks the spec; follow the repo's existing roadmap-entry style — add the four follow-up specs from the spec's last section as deferred items if not already there)

- [ ] **Step 1: Full gate**

Run: `uv run pytest tests -q && uv run ruff check . && uv run mypy src`
Expected: all green.

- [ ] **Step 2: Grep sweep for stragglers**

Run: `grep -rn "enable_qos" src/ | grep -v oas/` — expected hits ONLY in `scope/allowlist.py` (benign group + local leaf comment). Any hit in `ingest/` or `checks/` is a missed removal.

- [ ] **Step 3: Update spec status + roadmap; commit**

```bash
git add -A && git commit -m "docs: Spec-1 port-profile attribute completion implemented"
```

---

## Self-review checklist (run after writing, before execution)

1. **Spec coverage:** every spec requirement maps to a task — SAFE table (T1/T2/T7), ui_evpntopo_id gate (T1), enable_qos removal list (T2 gates / T4 PortMisc+ingest+check), REVIEW table (T3 auth / T4 misc), OAS refresh-first (T1), three-gates invariant (T2/T6), ingest wiring incl. usage-only vs local-capable split and scalar honesty (T4), ripple coupling (T5), testing requirements incl. below-profile UNKNOWN pin and mixed-delta precedence (T6/T7/T8).
2. **Deviation from spec, intentional:** `bypass_auth_when_server_down_for_voip` enters the gates via `_USAGE_ONLY_REVIEWED_ATTRS`, NOT `_AUTH_ATTRS` — the refreshed OAS proved it usage-only, and the spec's own contingency clause ("if the refreshed OAS forces a split...the invariant is unchanged") governs. Ingest still treats it as auth surface (PortAuth).
3. **Type consistency:** `PortMisc.community_vlan_id: int | str | None` with `unresolved:` tokens matches the `_mac_limit` precedent; `_changed` iterates `dataclasses.fields(PortMisc)` so T4's field list can never drift from the entity.
