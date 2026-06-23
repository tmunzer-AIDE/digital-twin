from digital_twin.adapters.mist.validate.schema import validate_payload
from digital_twin.scope.allowlist import (
    _BGP_GATEWAY_LEAVES,
    _BGP_LEAVES,
    EFFECTIVE_ALLOWLIST,
    GATEWAY_EFFECTIVE_ALLOWLIST,
    RAW_ALLOWLIST,
)

_MODELED = {
    "bgp_config.*.local_as",
    "bgp_config.*.type",
    "bgp_config.*.neighbors.*.neighbor_as",
    "bgp_config.*.neighbors.*.disabled",
}
_DENIED = {
    "bgp_config.*.auth_key",
    "bgp_config.*.networks",
    "bgp_config.*.hold_time",
    "bgp_config.*.neighbors.*.import_policy",
}


def test_switch_surfaces_carry_the_four_modeled_leaves():
    assert _MODELED == set(_BGP_LEAVES)
    for obj in ("site_setting", "device", "networktemplate", "sitetemplate"):
        assert _MODELED <= set(RAW_ALLOWLIST[obj])
    assert _MODELED <= set(EFFECTIVE_ALLOWLIST)


def test_gateway_surfaces_add_via():
    assert set(_BGP_GATEWAY_LEAVES) == _MODELED | {"bgp_config.*.via"}
    assert set(_BGP_GATEWAY_LEAVES) <= set(RAW_ALLOWLIST["gatewaytemplate"])
    assert set(_BGP_GATEWAY_LEAVES) <= set(GATEWAY_EFFECTIVE_ALLOWLIST)


def test_secrets_and_unmodeled_leaves_are_denied_everywhere():
    for obj in ("site_setting", "device", "gatewaytemplate"):
        assert not (_DENIED & set(RAW_ALLOWLIST[obj]))
    assert not (_DENIED & set(EFFECTIVE_ALLOWLIST))
    assert not (_DENIED & set(GATEWAY_EFFECTIVE_ALLOWLIST))


def test_bgp_config_edit_does_not_fatal_at_l0():
    # bgp_config is permissive on device/site_setting (additionalProperties unset) and
    # DEFINED on gatewaytemplate -> a bgp_config edit must never be structurally fatal.
    payload = {"bgp_config": {"underlay": {"type": "external", "local_as": 65000,
               "neighbors": {"10.0.0.2": {"neighbor_as": 65001}}}}}
    for obj in ("device", "site_setting", "gatewaytemplate"):
        res = validate_payload(obj, payload, scope_roots={"bgp_config"})
        assert not res.fatal, (obj, res.findings)
