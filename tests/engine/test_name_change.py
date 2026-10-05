import pytest

from digital_twin.engine.pipeline import simulate_name_change
from digital_twin.verdict.decision import Decision


def _plan(object_type: str, payload: dict | None = None, *, action: str = "update") -> dict:
    return {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [
            {
                "action": action,
                "order": 0,
                "object_type": object_type,
                "object_id": "obj-1",
                "payload": {"name": "renamed"} if payload is None else payload,
            }
        ],
    }


def test_unmodeled_configuration_object_name_change_is_safe_without_fetch():
    verdict = simulate_name_change(_plan("networks"))

    assert verdict is not None
    assert verdict.decision is Decision.SAFE
    assert verdict.check_results[0].check_id == "config.name_change"
    assert verdict.check_results[0].reasoning == (
        "name-only configuration update is safe for: networks"
    )


@pytest.mark.parametrize("object_type", ["device", "devices", "site_devices"])
def test_device_rename_is_never_granted_before_fetch(object_type):
    # switch_matching/gateway_matching `match_name` rules and neighbor dynamic
    # port profiles select config by device name: SAFE needs the fetched site
    assert simulate_name_change(_plan(object_type)) is None


def test_plan_mixing_device_and_object_renames_is_withheld_before_fetch():
    plan = _plan("device")
    plan["ops"].append(
        {
            "action": "update",
            "order": 1,
            "object_type": "networks",
            "object_id": "obj-2",
            "payload": {"name": "corp"},
        }
    )
    assert simulate_name_change(plan) is None


@pytest.mark.parametrize(
    "object_type",
    [
        "secintelprofiles",
        "aamwprofiles",
        "avprofiles",
        "idpprofiles",
        "servicepolicies",
    ],
)
def test_security_profile_and_policy_name_changes_are_not_safe(object_type):
    verdict = simulate_name_change(_plan(object_type))

    assert verdict is not None
    assert verdict.decision is Decision.UNKNOWN
    assert verdict.decision_reasons
    assert "excluded from the safe-name rule" in verdict.decision_reasons[0]


@pytest.mark.parametrize(
    ("payload", "action"),
    [
        ({"name": "renamed", "vlan_id": 20}, "update"),
        ({"name": ""}, "update"),
        ({"name": "renamed"}, "create"),
    ],
)
def test_non_name_only_updates_continue_through_normal_pipeline(payload, action):
    assert simulate_name_change(_plan("networks", payload, action=action)) is None


def test_mixed_name_only_plan_with_an_exception_is_not_safe():
    plan = _plan("networks")
    plan["ops"].append(
        {
            "action": "update",
            "order": 1,
            "object_type": "servicepolicies",
            "object_id": "obj-2",
            "payload": {"name": "renamed policy"},
        }
    )

    verdict = simulate_name_change(plan)
    assert verdict is not None
    assert verdict.decision is Decision.UNKNOWN
