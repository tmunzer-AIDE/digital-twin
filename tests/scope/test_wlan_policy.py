import json
from pathlib import Path

from digital_twin.scope.wlan_policy import WLAN_POLICY_ALLOWLIST

_SERVER_OR_OUTPUT_ONLY = {
    "created_time",
    "for_site",
    "id",
    "modified_time",
    "msp_id",
    "org_id",
    "portal_api_secret",
    "portal_sso_url",
    "portal_template_url",
    "site_id",
    "template_id",
    "thumbnail",
}


def test_every_wlan_schema_root_is_policy_owned_or_explicitly_output_only():
    schema_path = (
        Path(__file__).parents[2]
        / "src/digital_twin/adapters/mist/oas/wlan.schema.json"
    )
    properties = set(json.loads(schema_path.read_text())["properties"])
    policy_roots = {pattern.split(".", 1)[0] for pattern in WLAN_POLICY_ALLOWLIST}

    assert properties == policy_roots | _SERVER_OR_OUTPUT_ONLY
    assert not policy_roots & _SERVER_OR_OUTPUT_ONLY


def test_wlan_schema_inventory_remains_the_reviewed_105_attributes():
    schema_path = (
        Path(__file__).parents[2]
        / "src/digital_twin/adapters/mist/oas/wlan.schema.json"
    )
    properties = json.loads(schema_path.read_text())["properties"]
    assert len(properties) == 105
