"""Policy rule for configuration-object display-name updates.

Changing only the top-level ``name`` attribute is network-safe for Mist
configuration objects, except for the security profile/policy families whose
names may participate in policy references.  This rule is intentionally
pre-fetch: it can cover configuration object types that the topology simulator
does not otherwise model, while every non-name field remains default-denied.

Devices are the exception to "pre-fetch": Mist selects device configuration by
device NAME — `switch_matching`/`gateway_matching` rules (`match_name`,
`match_name[A:B]`) and switch dynamic port profiles matching a neighbor's
`lldp_system_name` — so a rename can swap a device's whole matched rule
(port_config, ip_config, stp_config, ...) or a neighbor port's profile. An AP's
name can also reach an external DHCP server through WLAN option 82. A device
rename is SAFE only after `device_rename_risks` proves, against the fetched site
state, that no such matcher can change outcome; everything else is REVIEW.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from digital_twin.adapters.mist.compile.switch_matching import rename_sensitive_criterion
from digital_twin.adapters.mist.ingest.dynamic_usage import rename_flips_dynamic_rule
from digital_twin.contracts import ChangeOp, ChangePlan
from digital_twin.providers.base import RawSiteState
from digital_twin.scope.allowlist import CONFIG_POLICY_OBJECT_TYPES

NAME_CHANGE_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "secintelprofiles",
        "aamwprofiles",
        "avprofiles",
        "idpprofiles",
        "services",
        "servicepolicies",
        "vpns",
    }
)
DEVICE_OBJECT_TYPES: frozenset[str] = frozenset({"device", "devices"})

# The name-matching block Mist evaluates for each device type. `ap_matching`
# documents only `match_model`; scanning it keeps an undocumented name criterion
# from being trusted. A type outside this table has no proof -> REVIEW.
_MATCHING_BLOCKS: Mapping[str, str] = {
    "switch": "switch_matching",
    "gateway": "gateway_matching",
    "ap": "ap_matching",
}
_TEMPLATE_LAYERS: tuple[str, ...] = ("networktemplate", "sitetemplate", "gatewaytemplate")
# Layers whose ABSENCE (assigned but not in the fetched state) blinds the proof:
# switch templates carry switch_matching AND the dynamic LLDP rules any device
# can trip as a neighbor; a gateway template carries only gateway_matching.
_SWITCH_LAYERS: tuple[str, ...] = ("networktemplate", "sitetemplate")


@dataclass(frozen=True)
class NameChangeAssessment:
    safe: bool
    object_types: tuple[str, ...]
    reason: str
    # device renames whose SAFE still needs the post-fetch matcher proof
    device_ops: tuple[ChangeOp, ...] = ()


@dataclass(frozen=True)
class RenameRisks:
    """Why one device rename is not provably SAFE (both empty = proven)."""

    matchers: tuple[str, ...] = ()  # name-matched rules whose outcome can change
    unverified: tuple[str, ...] = ()  # evidence the proof needs but could not read


def _canonical_object_type(object_type: str) -> str:
    """Accept both twin names and Mist bridge names (``org_*``/``site_*``)."""
    normalized = object_type.strip().lower()
    for prefix in ("org_", "site_"):
        if normalized.startswith(prefix):
            return normalized[len(prefix):]
    return normalized


def is_device_object_type(object_type: str) -> bool:
    return _canonical_object_type(object_type) in DEVICE_OBJECT_TYPES


def assess_name_only_change(plan: ChangePlan) -> NameChangeAssessment | None:
    """Classify a pure, non-empty top-level ``name`` update.

    ``None`` means the plan is not exclusively a name change and must continue
    through the normal simulation pipeline.  An excluded family is returned as
    an explicit non-safe assessment so it can never fall through to a future
    model and accidentally receive this policy grant.  Device renames come back
    non-safe with ``device_ops`` set: they are SAFE only after the post-fetch
    matcher proof.
    """
    if plan.source != "mist" or not plan.ops:
        return None

    object_types: list[str] = []
    for op in plan.ops:
        if op.object_type in CONFIG_POLICY_OBJECT_TYPES:
            # Explicit per-object policy (for example webhooks always REVIEW and
            # PSKs require usage telemetry) takes precedence over this generic rule.
            return None
        name = op.payload.get("name")
        if (
            op.action != "update"
            or set(op.payload) != {"name"}
            or not isinstance(name, str)
            or not name.strip()
        ):
            return None
        object_types.append(_canonical_object_type(op.object_type))

    normalized_types = tuple(dict.fromkeys(object_types))
    excluded = tuple(t for t in normalized_types if t in NAME_CHANGE_EXCEPTIONS)
    if excluded:
        joined = ", ".join(excluded)
        return NameChangeAssessment(
            safe=False,
            object_types=normalized_types,
            reason=(
                f"name changes for {joined} are excluded from the safe-name rule "
                "and require impact-aware validation"
            ),
        )

    joined = ", ".join(normalized_types)
    device_ops = tuple(op for op in plan.ops if is_device_object_type(op.object_type))
    if device_ops:
        return NameChangeAssessment(
            safe=False,
            object_types=normalized_types,
            reason=(
                "device renames can change name-matched template rules; the fetched "
                f"site configuration must prove they do not ({joined})"
            ),
            device_ops=device_ops,
        )
    return NameChangeAssessment(
        safe=True,
        object_types=normalized_types,
        reason=f"name-only configuration update is safe for: {joined}",
    )


def _find(doc: Any, key: str, path: str) -> Iterator[tuple[str, Any]]:
    """Every non-null value stored under `key` at any depth of `doc` (so nested
    placements such as `setting.switch.switch_matching` are never missed)."""
    if isinstance(doc, Mapping):
        for k, value in doc.items():
            here = f"{path}.{k}"
            if k == key:
                if value is not None:
                    yield here, value
            else:
                yield from _find(value, key, here)
    elif isinstance(doc, list):
        for index, value in enumerate(doc):
            yield from _find(value, key, f"{path}[{index}]")


def _config_layers(state: RawSiteState) -> tuple[tuple[str, Any], ...]:
    return (
        *((layer, getattr(state, layer)) for layer in _TEMPLATE_LAYERS),
        ("site_setting", state.setting),
    )


def _rules(value: Any, path: str, unverified: list[str]) -> list[tuple[int, Mapping[str, Any]]]:
    rules = value.get("rules") if isinstance(value, Mapping) else None
    if rules is None and isinstance(value, Mapping):
        return []
    if not isinstance(rules, list):
        unverified.append(f"{path}: rules are unreadable")
        return []
    out: list[tuple[int, Mapping[str, Any]]] = []
    for index, rule in enumerate(rules):
        if isinstance(rule, Mapping):
            out.append((index, rule))
        else:
            unverified.append(f"{path}.rules[{index}] is unreadable")
    return out


def _ap_name_in_dhcp(
    state: RawSiteState, rename: str, matchers: list[str], unverified: list[str]
) -> None:
    """WLAN DHCP option 82 can carry `{{AP_NAME}}` to a DHCP server the twin
    cannot see; whether that server keys addressing or policy on it is unknowable.
    `state.wlans` is the site's DERIVED list (org-template WLANs included)."""
    if "wlans" not in state.meta.fetched:  # a failed fetch is never recorded as fetched
        unverified.append(
            "site WLANs were not fetched: DHCP option 82 use of {{AP_NAME}} is unknown"
        )
        return
    for wlan in state.wlans:
        option = wlan.get("inject_dhcp_option_82")
        label = wlan.get("ssid") or wlan.get("id")
        if option is not None and not isinstance(option, Mapping):
            unverified.append(f"WLAN {label!r} inject_dhcp_option_82 is unreadable")
        elif (
            isinstance(option, Mapping)
            and option.get("enabled")
            and "ap_name" in str(option.get("circuit_id") or "").lower()
        ):
            matchers.append(
                f"WLAN {label!r} injects {{{{AP_NAME}}}} into the DHCP option 82 circuit_id: "
                f"a DHCP server keyed on it sees a new value after the rename {rename}"
            )


def device_rename_risks(
    state: RawSiteState, device: Mapping[str, Any], new_name: str
) -> RenameRisks:
    """Prove (or not) that renaming `device` to `new_name` changes no outcome of
    a name-based matcher in the fetched site configuration.

    Scope: the device's own matching block (`switch_matching` for switches,
    `gateway_matching` for gateways, `ap_matching` for APs) in every fetched
    template layer and the site setting, plus every switch dynamic port profile
    rule (templates, site setting, device configs) — the renamed device may be
    any switch's LLDP neighbor. `enable` flags are ignored: how layers merge
    them is not modeled, so a disabled block's rules still count. AP renames
    also check the site WLANs for `{{AP_NAME}}` in DHCP option 82.
    """
    old_name = str(device.get("name") or "")
    if old_name == new_name:
        return RenameRisks()
    rename = f"{old_name!r} -> {new_name!r}"
    matchers: list[str] = []
    unverified: list[str] = []

    governing = (*_SWITCH_LAYERS, "gatewaytemplate") if device.get("type") == "gateway" else (
        _SWITCH_LAYERS
    )
    for layer in governing:
        assigned = state.site.get(f"{layer}_id")
        if assigned and getattr(state, layer) is None:
            unverified.append(f"assigned {layer} {assigned} is absent from the fetched state")

    block = _MATCHING_BLOCKS.get(str(device.get("type")))
    if block is None:
        unverified.append(
            f"device type {device.get('type')!r} has no modeled name-matching semantics"
        )
    else:
        for layer, doc in _config_layers(state):
            for path, value in _find(doc, block, layer):
                for index, rule in _rules(value, path, unverified):
                    key = rename_sensitive_criterion(dict(rule), old_name, new_name)
                    if key is not None:
                        label = rule.get("name") or f"#{index}"
                        matchers.append(
                            f"{block} rule {label!r} ({path}): {key!r} can match "
                            f"differently after the rename {rename}"
                        )

    usage_docs = (
        *_config_layers(state),
        *((f"device {d.get('id')}", d) for d in state.devices),
    )
    for layer, doc in usage_docs:
        for path, usages in _find(doc, "port_usages", layer):
            if not isinstance(usages, Mapping):
                unverified.append(f"{path} is unreadable")
                continue
            for usage, spec in usages.items():
                for index, rule in _rules(spec, f"{path}.{usage}", unverified):
                    if rename_flips_dynamic_rule(rule, old_name, new_name):
                        matchers.append(
                            f"dynamic port profile {usage!r} ({path}) rule #{index} on "
                            f"{rule.get('src')!r} can match this device as an LLDP "
                            f"neighbor differently after the rename {rename}"
                        )
    if device.get("type") == "ap":
        _ap_name_in_dhcp(state, rename, matchers, unverified)
    return RenameRisks(tuple(dict.fromkeys(matchers)), tuple(dict.fromkeys(unverified)))
