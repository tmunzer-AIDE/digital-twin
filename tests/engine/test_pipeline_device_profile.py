"""Task 14: device-profile gate wired into the pipeline (post-ingest).

Three correctness points:

(i)   A site_setting (or below-profile) edit that changes an OVERRIDABLE leaf on
      a profiled switch's effective -> verdict UNKNOWN via device_profile_gate.

(iii) A device-only plan on a profiled device -> NOT tainted (its below-profile
      effective == baseline -> no overridable diff; verdict per the real checks).

(iv)  A mixed device + site_setting plan where the device op changes the
      profiled device's port_config and the site_setting op does NOT affect
      that device -> NOT tainted either (below-profile == baseline for that
      device, same outcome as (iii)).

All three call simulate() end-to-end via a FakeProvider so they exercise the
full pipeline including the below-profile scoping logic in simulate().
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.verdict.decision import Decision

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SITE = "s1"
ORG = "o1"

# A site setting with port_usages that carry vlan 10 (corp).
# The profile layer overrides the switch's port_config -> the gate must check
# whether an edit to port_usages changes an overridable leaf on the device eff.
BASE_SETTING = {
    "networks": {"corp": {"vlan_id": 10}},
    "port_usages": {
        "office": {"mode": "access", "port_network": "corp"},
        "uplink": {"mode": "trunk", "networks": ["corp"]},
    },
}

# A profiled switch whose port_config references the site_setting port_usages.
# `deviceprofile_id` is present -> the gate treats it as profiled.
PROFILED_SWITCH_MAC = "aa0000000001"
PROFILED_SWITCH = {
    "mac": PROFILED_SWITCH_MAC,
    "id": "dev-p1",
    "type": "switch",
    "model": "EX4100-48P",
    "name": "sw-profiled",
    "deviceprofile_id": "profile-x",
    "port_config": {
        "ge-0/0/0": {"usage": "office"},
        "ge-0/0/1": {"usage": "uplink"},
    },
}

# A second, non-profiled switch in the same site (no deviceprofile_id).
PLAIN_SWITCH_MAC = "aa0000000002"
PLAIN_SWITCH = {
    "mac": PLAIN_SWITCH_MAC,
    "id": "dev-p2",
    "type": "switch",
    "model": "EX2300-48P",
    "name": "sw-plain",
    # no deviceprofile_id
    "port_config": {"ge-0/0/0": {"usage": "office"}},
}


def _meta() -> StateMeta:
    return StateMeta(
        acquired_at=datetime.now(UTC),
        host="test",
        fetched=("site", "setting", "devices"),
        failures=(),
    )


def _raw(setting: dict | None = None, devices: tuple | None = None) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(org_id=ORG, site_id=SITE),
        site={"id": SITE},
        setting=setting if setting is not None else BASE_SETTING,
        networktemplate=None,
        devices=devices if devices is not None else (PROFILED_SWITCH, PLAIN_SWITCH),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=_meta(),
    )


class FakeProvider:
    """Returns the configured RawSiteState for any fetch_site call."""

    def __init__(self, raw: RawSiteState):
        self._raw = raw

    def fetch_site(self, scope, *, include_derived=False):
        return self._raw

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        return {SITE: self._raw}


def _plan(ops: list[dict]) -> dict:
    return {
        "source": "mist",
        "scope": {"org_id": ORG, "site_id": SITE},
        "ops": ops,
    }


def _site_op(payload: dict, order: int = 0) -> dict:
    return {
        "action": "update",
        "order": order,
        "object_type": "site_setting",
        "object_id": SITE,
        "payload": payload,
    }


def _device_op(device_id: str, payload: dict, order: int = 0) -> dict:
    return {
        "action": "update",
        "order": order,
        "object_type": "device",
        "object_id": device_id,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# (i) site_setting edit that changes an overridable leaf on a profiled switch
# ---------------------------------------------------------------------------


def test_site_setting_edit_taints_profiled_switch_unknown():
    """A site_setting edit that changes port_usages.*.mode for a usage referenced
    by the profiled switch -> UNKNOWN via device_profile_gate.

    The profiled switch's port_config references 'office' (mode: access). The
    edit flips office to mode: trunk. That changes port_usages.*.mode on the
    switch's effective config — port_usages.*.mode is in
    DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE['switch'], so the gate must taint.
    """
    # Flip "office" from access -> trunk: changes the profiled switch's effective
    new_setting = {
        "networks": {"corp": {"vlan_id": 10}},
        "port_usages": {
            "office": {"mode": "trunk", "networks": ["corp"]},   # changed: access -> trunk
            "uplink": {"mode": "trunk", "networks": ["corp"]},
        },
    }
    op = _site_op(new_setting)
    provider = FakeProvider(_raw())

    verdict = simulate(_plan([op]), provider=provider)

    assert verdict.decision is Decision.UNKNOWN, verdict.decision_reasons
    assert any("device_profile_gate" in r for r in verdict.decision_reasons), (
        f"expected device_profile_gate in reasons, got: {verdict.decision_reasons}"
    )


# ---------------------------------------------------------------------------
# (iii) device-only plan on a profiled device -> NOT tainted
# ---------------------------------------------------------------------------


def test_device_only_plan_on_profiled_device_not_tainted():
    """A plan that ONLY changes the profiled switch's device object (e.g. renames
    a port mapping in port_config) must NOT trigger the device_profile_gate.

    Rationale: device ops are ABOVE the profile in the precedence stack; a
    device-level port_config change wins over the profile. The below-profile
    effective (= baseline, because no below-profile ops) is IDENTICAL to the
    baseline effective, so no overridable diff -> gate passes.
    """
    # Op: change the profiled switch's port_config for ge-0/0/0 to use "uplink"
    # instead of "office". This is a device-level (above-profile) change.
    dev = copy.deepcopy(PROFILED_SWITCH)
    dev["port_config"]["ge-0/0/0"] = {"usage": "uplink"}
    op = _device_op("dev-p1", dev)
    provider = FakeProvider(_raw())

    verdict = simulate(_plan([op]), provider=provider)

    assert verdict.decision is not Decision.UNKNOWN or not any(
        "device_profile_gate" in r for r in verdict.decision_reasons
    ), (
        f"device-only plan must not be tainted by device_profile_gate; "
        f"got: {verdict.decision_reasons}"
    )


# ---------------------------------------------------------------------------
# (iv) mixed device + site_setting plan where site_setting does NOT affect the
#      profiled device -> NOT tainted
# ---------------------------------------------------------------------------


def test_mixed_plan_site_setting_changes_non_overridable_leaf():
    """A mixed plan: device op changes the profiled switch's port_config (above-
    profile), site_setting op changes stp_config.bridge_priority — a leaf that
    IS in the switch effective but NOT in DEVICE_PROFILE_OVERRIDABLE_LEAVES_BY_ROLE
    for 'switch' (stp_config is not in _NETWORK_LEAVES/_USAGE_LEAVES/_DEVICE_PORT_LEAVES
    /_DHCP_LEAVES).

    The below-profile effective (site_setting applied, device op excluded): the
    profiled switch gets the stp_config change, but no overridable leaf differs
    from baseline -> gate passes.

    This validates that the below-profile scoping correctly ignores non-overridable
    leaf changes and does not false-taint mixed plans.
    """
    # Site_setting op: change stp_config.bridge_priority (NOT in switch overridable
    # leaves — stp_config is site-level STP, NOT in DEVICE_PROFILE_OVERRIDABLE_LEAVES
    # for switches which only covers _NETWORK_LEAVES, _USAGE_LEAVES, _DEVICE_PORT_LEAVES,
    # _DHCP_LEAVES). Port_usages are UNCHANGED -> no overridable diff for profiled sw.
    new_setting = {
        **BASE_SETTING,
        "stp_config": {"bridge_priority": 8192},
    }
    site_op = _site_op(new_setting, order=0)

    # Device op: profiled switch renames ge-0/0/0 to use "uplink" (above-profile)
    dev = copy.deepcopy(PROFILED_SWITCH)
    dev["port_config"]["ge-0/0/0"] = {"usage": "uplink"}
    dev_op = _device_op("dev-p1", dev, order=1)

    provider = FakeProvider(_raw())

    verdict = simulate(_plan([site_op, dev_op]), provider=provider)

    assert verdict.decision is not Decision.UNKNOWN or not any(
        "device_profile_gate" in r for r in verdict.decision_reasons
    ), (
        f"mixed plan where site_setting changes only non-overridable leaf must not "
        f"be tainted by device_profile_gate; got: {verdict.decision_reasons}"
    )
