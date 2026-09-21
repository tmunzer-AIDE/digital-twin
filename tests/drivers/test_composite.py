from dataclasses import replace
from datetime import UTC, datetime, timedelta

from digital_twin.contracts import ChangeOp
from digital_twin.drivers.composite import (
    needs_composite_evaluation,
    simulate_composite,
)
from digital_twin.providers.base import (
    ObjectReference,
    ObjectRelationshipContext,
    OrgNetworksContext,
    OrgScope,
    OrgTemplateContext,
    OrgWlanTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
)
from digital_twin.providers.proposed import ProposedStateProvider


class _Provider:
    def __init__(
        self, *, wlans=(), clients=(), devices=(), initial_network_reference=False,
        assigned_templates=False,
    ) -> None:
        self.initial_network_reference = initial_network_reference
        self.assigned_templates = assigned_templates
        self.raw = RawSiteState(
            scope=SiteScope("o1", "s1"),
            site={"id": "s1"},
            setting={},
            networktemplate=None,
            devices=tuple(devices),
            device_stats=(),
            port_stats=(),
            wireless_clients=tuple(clients),
            wired_clients=(),
            derived_setting=None,
            meta=StateMeta(
                datetime.now(UTC),
                "test",
                (
                    "site", "setting", "devices", "device_stats", "port_stats",
                    "wireless_clients", "wired_clients", "wlans", "org_networks",
                ),
                (),
            ),
            org_networks=({"id": "n1", "name": "corp", "vlan_id": 10},),
            wlans=tuple(wlans),
        )

    def resolve_org_networks(self, scope):
        return OrgNetworksContext(self.raw.org_networks)

    def resolve_object_relationships(self, scope, object_type, object_id):
        target = next(
            row for row in self.raw.org_networks if str(row.get("id")) == object_id
        )
        references = (
            (ObjectReference(
                "org_gatewaytemplates", "gt1", "$.port_config.ge-0/0/0.port_network"
            ),)
            if self.initial_network_reference else ()
        )
        return ObjectRelationshipContext(target, references, ("org_gatewaytemplates",))

    def resolve_org_template(self, scope, template_id, object_type):
        template = {"id": template_id, "name": "branch"}
        if self.initial_network_reference:
            template["port_config"] = {"ge-0/0/0": {"port_network": "corp"}}
        return OrgTemplateContext(template, ("s1",) if self.assigned_templates else ())

    def fetch_site(self, scope, *, include_derived=False):
        return self.raw

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        return {"s1": self.raw}

    def resolve_org_wlan_template(self, scope, template_id):
        derived = tuple(
            row
            for row in self.raw.wlans
            if str(row.get("template_id") or "") == template_id
        )
        return OrgWlanTemplateContext(
            template={"id": template_id, "name": "branches"},
            derived_rows_by_site={"s1": derived} if derived else {},
            template_wlans=(
                {"id": "tw1", "ssid": "guest", "enabled": True, "apply_to": "site"},
            ),
            template_wlans_complete=True,
        )


def _op(order, object_type, *, scope="org", payload=None, action="update", object_id=None):
    return {
        "action": action,
        "order": order,
        "scope": scope,
        "object_type": object_type,
        "object_id": object_id or f"obj-{order}",
        "payload": payload if payload is not None else {"name": f"name-{order}"},
    }


def test_mixed_policy_and_name_change_produce_one_safe_verdict():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [
            _op(0, "org_networks", action="create", object_id="n2", payload={
                "name": "guest", "vlan_id": 20, "subnet": "10.20.0.0/24",
            }),
            _op(1, "wxtags"),
        ],
    }

    assert needs_composite_evaluation(plan)
    verdict = simulate_composite(plan, provider=_Provider())

    assert verdict["decision"] == "safe"
    assert verdict["composite"] is True
    assert [segment["route"] for segment in verdict["segments"]] == ["policy", "name"]
    assert [change["order"] for change in verdict["changes"]] == [0, 1]
    network_diff = next(
        diff for diff in verdict["config_diffs"]
        if diff["object_type"] == "org_networks"
    )
    assert {change["path"] for change in network_diff["changes"]} == {
        "name", "subnet", "vlan_id",
    }


def test_multiple_operations_on_one_route_still_use_composite_evaluation():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(0, "wlan", scope="site", action="create", object_id="w1", payload={
                "ssid": "one", "enabled": True,
            }),
            _op(1, "wlan", scope="site", action="create", object_id="w2", payload={
                "ssid": "two", "enabled": True,
            }),
        ],
    }

    assert needs_composite_evaluation(plan)
    verdict = simulate_composite(plan, provider=_Provider())

    assert [item["order"] for item in verdict["change_assessments"]] == [0, 1]
    assert [item["decision"] for item in verdict["change_assessments"]] == ["safe", "safe"]


def test_composite_exposes_coverage_and_state_metadata():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(0, "wlan", scope="site", action="create", object_id="w1", payload={
                "ssid": "one", "enabled": True,
            }),
            _op(1, "wlan", scope="site", action="create", object_id="w2", payload={
                "ssid": "two", "enabled": True,
            }),
        ],
    }
    provider = _Provider()
    provider.raw = replace(
        provider.raw,
        meta=StateMeta(
            datetime.now(UTC) - timedelta(minutes=2),
            "test",
            provider.raw.meta.fetched,
            (),
        ),
    )

    verdict = simulate_composite(plan, provider=provider)

    expected_counts = {state: 0 for state in (
        "complete", "partial", "insufficient", "not_applicable"
    )}
    for check in verdict["check_results"]:
        expected_counts[check["coverage"]["state"]] += 1
    actual_counts = {state: 0 for state in expected_counts}
    for domain in verdict["coverage"].values():
        for state in actual_counts:
            actual_counts[state] += domain[state]

    assert actual_counts == expected_counts
    assert verdict["state_meta"]["host"] == "test"
    assert verdict["state_meta"]["age_seconds"] >= 120
    assert verdict["state_meta"]["fetch_failures"] == []


def test_duplicate_ssid_batch_attributes_unsafe_to_affected_wlans():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(0, "wlan", scope="site", action="create", object_id="w1", payload={
                "ssid": "guest", "enabled": True, "apply_to": "site",
            }),
            _op(1, "wlan", scope="site", action="create", object_id="w2", payload={
                "ssid": "guest", "enabled": True, "apply_to": "site",
            }),
        ],
    }

    verdict = simulate_composite(plan, provider=_Provider())

    assert verdict["decision"] == "unsafe"
    assert [item["decision"] for item in verdict["change_assessments"]] == [
        "unsafe", "unsafe",
    ]
    assert verdict["batch_interaction"]["attributed_change_orders"] == [0, 1]


def test_global_site_id_does_not_misroute_org_operation():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(0, "org_networks", action="create", object_id="n2", payload={
                "name": "guest", "vlan_id": 20,
            }),
            _op(1, "wxtags", scope="site"),
        ],
    }

    verdict = simulate_composite(plan, provider=_Provider())

    assert verdict["segments"][0]["verdict"]["decision"] == "safe"
    assert verdict["decision"] == "safe"


def test_worst_segment_decision_drives_batch():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [
            _op(0, "org_settings", payload={"enabled": True}),
            _op(1, "wxtags"),
        ],
    }

    verdict = simulate_composite(plan, provider=_Provider())

    assert verdict["decision"] == "review"
    assert any("config.org_settings.review" in reason for reason in verdict["decision_reasons"])


def test_single_inert_org_template_create_uses_policy_path():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [_op(
            0,
            "networktemplate",
            action="create",
            payload={"name": "new template"},
        )],
    }

    assert needs_composite_evaluation(plan)
    verdict = simulate_composite(plan, provider=_Provider())
    assert verdict["decision"] == "safe"


def test_wlan_template_create_and_metadata_update_use_policy_path():
    created = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [_op(
            0, "wlantemplate", action="create", payload={"name": "branches"}
        )],
    }
    updated = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [_op(0, "wlantemplate", payload={"name": "branches-renamed"})],
    }
    assert needs_composite_evaluation(created)
    assert simulate_composite(created, provider=_Provider())["decision"] == "safe"
    assert simulate_composite(updated, provider=_Provider())["decision"] == "safe"


def test_wlan_template_assignment_without_conflict_is_safe():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [_op(0, "wlantemplate", payload={"site_ids": ["s1"]})],
    }
    verdict = simulate_composite(plan, provider=_Provider())
    assert verdict["decision"] == "safe"


def test_wlan_template_assignment_with_duplicate_ssid_is_unsafe():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [_op(0, "wlantemplate", payload={"site_ids": ["s1"]})],
    }
    provider = _Provider(wlans=({
        "id": "existing", "ssid": "guest", "enabled": True, "apply_to": "site",
    },))
    verdict = simulate_composite(plan, provider=provider)
    assert verdict["decision"] == "unsafe"
    assert any(
        finding["code"] == "config.wlantemplate.assignment.conflict"
        for finding in verdict["findings"]
    )


def test_later_site_change_sees_wlan_template_assignment():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(
                0,
                "wlantemplate",
                scope="org",
                object_id="wt1",
                payload={"site_ids": ["s1"]},
            ),
            _op(
                1,
                "wlan",
                scope="site",
                action="create",
                object_id="w2",
                payload={"ssid": "guest", "enabled": True, "apply_to": "site"},
            ),
        ],
    }

    verdict = simulate_composite(plan, provider=_Provider())

    assert verdict["decision"] == "unsafe", verdict["decision_reasons"]
    assert any(
        finding["code"] == "wireless.wlan.duplicate_ssid.introduced"
        for finding in verdict["findings"]
    )


def test_later_site_change_sees_wlan_template_unassignment():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(
                0,
                "wlantemplate",
                scope="org",
                object_id="wt1",
                payload={"site_ids": []},
            ),
            _op(
                1,
                "wlan",
                scope="site",
                action="create",
                object_id="w2",
                payload={"ssid": "guest", "enabled": True, "apply_to": "site"},
            ),
        ],
    }
    provider = _Provider(wlans=({
        "id": "tw1",
        "template_id": "wt1",
        "ssid": "guest",
        "enabled": True,
        "apply_to": "site",
    },))

    verdict = simulate_composite(plan, provider=provider)

    assert verdict["decision"] == "safe", verdict["decision_reasons"]


def test_wlan_template_unassignment_blocks_active_client_disconnect():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [
            _op(
                0,
                "wlantemplate",
                scope="org",
                object_id="wt1",
                payload={"site_ids": []},
            ),
        ],
    }
    provider = _Provider(
        wlans=({
            "id": "tw1",
            "template_id": "wt1",
            "ssid": "guest",
            "enabled": True,
            "apply_to": "site",
        },),
        clients=({
            "mac": "11:22:33:44:55:66",
            "ap_mac": "cc0000000001",
            "ssid": "guest",
            "vlan_id": 10,
        },),
        devices=({
            "mac": "cc0000000001",
            "id": "ap-a",
            "type": "ap",
            "model": "AP45",
            "name": "ap-a",
        },),
    )

    verdict = simulate_composite(plan, provider=provider)

    assert verdict["decision"] == "unsafe", verdict["decision_reasons"]
    assert any(
        finding["code"] == "config.wlantemplate.assignment.impact"
        for finding in verdict["findings"]
    )


def test_later_delete_sees_reference_added_by_earlier_template_change():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [
            _op(0, "gatewaytemplate", object_id="gt1", payload={
                "port_config": {"ge-0/0/0": {"port_network": "corp"}},
            }),
            _op(1, "org_networks", action="delete", object_id="n1", payload={}),
        ],
    }
    verdict = simulate_composite(plan, provider=_Provider())
    assert verdict["decision"] == "unsafe", verdict["decision_reasons"]
    assert any(
        finding["code"] == "config.networks.referenced"
        for finding in verdict["findings"]
    )


def test_later_delete_sees_reference_removed_by_earlier_template_change():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [
            _op(0, "gatewaytemplate", object_id="gt1", payload={"port_config": {}}),
            _op(1, "org_networks", action="delete", object_id="n1", payload={}),
        ],
    }
    verdict = simulate_composite(
        plan, provider=_Provider(initial_network_reference=True)
    )
    assert verdict["decision"] == "safe", verdict["decision_reasons"]


def test_proposed_org_networks_are_visible_to_later_site_reads():
    provider = ProposedStateProvider(_Provider())
    provider.apply_org_network_ops(
        OrgScope("o1"),
        (ChangeOp(
            action="create",
            order=0,
            object_type="org_networks",
            object_id="n2",
            payload={"name": "guest", "vlan_id": 20},
            scope="org",
        ),),
    )

    state = provider.fetch_site(SiteScope("o1", "s1"))

    assert isinstance(state, RawSiteState)
    assert [row["name"] for row in state.org_networks] == ["corp", "guest"]


def test_network_vlan_gateway_dhcp_batch_runs_final_cross_object_checks():
    switch = {
        "mac": "aa0000000001",
        "id": "sw1",
        "type": "switch",
        "model": "EX4100-48P",
        "port_config": {"ge-0/0/1": {"usage": "ap_uplink"}},
    }
    gateway = {
        "mac": "bb0000000001",
        "id": "gw1",
        "type": "gateway",
        "model": "SRX300",
    }
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(
                0,
                "wlan",
                scope="site",
                action="create",
                object_id="wlan-guest",
                payload={
                    "ssid": "Guest",
                    "enabled": True,
                    "vlan_enabled": True,
                    "vlan_id": 420,
                },
            ),
            _op(
                1,
                "org_networks",
                action="create",
                object_id="net-guest",
                payload={
                    "name": "PRD-Guest",
                    "vlan_id": 420,
                    "subnet": "10.3.42.0/24",
                },
            ),
            _op(
                2,
                "networktemplate",
                object_id="nt1",
                payload={
                    "networks": {"guest": {"vlan_id": 420}},
                    "port_usages": {
                        "ap_uplink": {"mode": "trunk", "networks": ["guest"]}
                    },
                },
            ),
            _op(
                3,
                "gatewaytemplate",
                object_id="gt1",
                payload={
                    "port_config": {"ge-0/0/1": {"networks": ["PRD-Guest"]}},
                    "ip_configs": {
                        "PRD-Guest": {
                            "type": "static",
                            "ip": "10.3.42.9",
                            "netmask": "/24",
                        }
                    },
                    "dhcpd_config": {
                        "PRD-Guest": {
                            "type": "local",
                            "ip_start": "10.3.42.10",
                            "ip_end": "10.3.42.250",
                            "gateway": "10.3.42.9",
                            "dns_servers": ["1.1.1.1", "8.8.8.8"],
                            "lease_time": 86400,
                        }
                    },
                },
            ),
        ],
    }

    verdict = simulate_composite(
        plan,
        provider=_Provider(
            devices=(switch, gateway),
            assigned_templates=True,
        ),
    )

    wanted = {
        "wired.dhcp.path",
        "wired.dhcp.scope_lint",
        "wired.l2.mtu_mismatch",
        "wired.l2.vlan_collision",
        "wired.l2.vlan_segmentation",
        "wired.l3.gateway_gap",
        "wired.l3.subnet_overlap",
    }
    statuses = {
        check["check_id"]: check["status"]
        for check in verdict["check_results"]
        if check["check_id"] in wanted
    }
    assert set(statuses) == wanted
    assert all(status != "not_applicable" for status in statuses.values())
    batch = next(segment for segment in verdict["segments"] if segment["route"] == "batch")
    ir_diff = batch["verdict"]["per_site"]["s1"]["ir_diff"]
    assert {row["kind"] for row in ir_diff["added"]} >= {"vlan", "l3intf", "dhcp_scope"}
    assert any(row["ref"]["kind"] == "port" for row in ir_diff["modified"])
    assessments = {item["order"]: item for item in verdict["change_assessments"]}
    assert assessments[0]["decision"] == "safe"
    assert assessments[1]["decision"] == "safe"
    assert assessments[2]["decision"] == "safe"
    assert assessments[3]["decision"] == "review"
    assert assessments[3]["findings"][0]["code"] == "l0.schema.violation"


def test_open_site_wlan_without_isolation_is_visibly_attributed_to_that_change():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [
            _op(
                0,
                "wlan",
                scope="site",
                action="create",
                object_id="wlan-guest",
                payload={
                    "ssid": "Guest",
                    "enabled": True,
                    "auth": {"type": "open"},
                    "vlan_enabled": True,
                    "vlan_id": 420,
                },
            ),
            _op(
                1,
                "org_networks",
                action="create",
                object_id="net-guest",
                payload={
                    "name": "PRD-Guest",
                    "vlan_id": 420,
                    "subnet": "10.3.42.0/24",
                },
            ),
        ],
    }

    verdict = simulate_composite(plan, provider=_Provider())

    assessments = {item["order"]: item for item in verdict["change_assessments"]}
    assert verdict["decision"] == "review"
    assert assessments[0]["decision"] == "review"
    assert assessments[0]["findings"] == [{
        "code": "wireless.wlan.open_guest.introduced",
        "severity": "warning",
        "message": (
            "open guest WLAN 'Guest' has no client isolation — joined clients "
            "can reach each other (lateral traffic)"
        ),
    }]
    assert assessments[1]["decision"] == "safe"
    assert verdict["batch_interaction"]["attributed_change_orders"] == [0]


def test_proposed_org_wlan_is_visible_to_later_site_segments():
    provider = ProposedStateProvider(_Provider())
    provider.apply_org_ops(
        OrgScope("o1"),
        (ChangeOp(
            action="create",
            order=0,
            object_type="wlan",
            object_id="w2",
            payload={"ssid": "guest", "enabled": True},
            scope="org",
        ),),
    )

    state = provider.fetch_site(SiteScope("o1", "s1"))

    assert isinstance(state, RawSiteState)
    assert [(row["id"], row["ssid"]) for row in state.wlans] == [("w2", "guest")]
