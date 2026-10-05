"""Placement pins for the benign gateway DHCP domain leaves.

The allowlist treats `dhcpd_config.*.dns_suffix` and DHCP option 15 (domain
name) as benign on gateway scope rows: they set the DNS domain handed to DHCP
clients, which changes how clients expand short names but never leases,
addressing or forwarding. These pin the OAS shapes that classification rests
on; if a refresh changes them, the classification must be re-argued before the
gate change ships.
"""

from __future__ import annotations

import json
from pathlib import Path

OAS = Path("src/digital_twin/adapters/mist/oas")


def _gateway_dhcp_row() -> dict:
    schema = json.loads((OAS / "gatewaytemplate.schema.json").read_text())
    return schema["properties"]["dhcpd_config"]["additionalProperties"]["properties"]


def test_gateway_dhcp_dns_suffix_is_still_a_list_of_domain_strings():
    node = _gateway_dhcp_row()["dns_suffix"]
    assert node["type"] == "array" and node["items"]["type"] == "string"


def test_gateway_dhcp_options_are_keyed_by_number_with_only_type_and_value():
    # options.15.type / options.15.value must stay the WHOLE of an option entry:
    # a new field on the entry would be a new, un-allowlisted leaf
    entry = _gateway_dhcp_row()["options"]["additionalProperties"]
    assert set(entry["properties"]) == {"type", "value"}
    assert "string" in entry["properties"]["type"]["enum"]


def test_switch_dhcp_rows_have_the_same_naming_shapes():
    # device_switch is the switch DHCP row authority: the committed site_setting /
    # networktemplate extracts do not carry dhcpd_config at all
    schema = json.loads((OAS / "device_switch.schema.json").read_text())
    row = schema["properties"]["dhcpd_config"]["additionalProperties"]["properties"]
    assert row["dns_suffix"]["type"] == "array"
    assert row["dns_suffix"]["items"]["type"] == "string"
    assert set(row["options"]["additionalProperties"]["properties"]) == {"type", "value"}
