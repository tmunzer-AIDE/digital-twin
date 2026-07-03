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
