from __future__ import annotations

from datetime import UTC, datetime

from digital_twin.engine.pipeline import simulate
from digital_twin.providers.base import RawSiteState, SiteScope, StateMeta
from digital_twin.verdict.decision import Decision

SITE = "s1"
ORG = "o1"


def _switch(mac: str, *, override_edge: bool | None) -> dict:
    device = {
        "mac": mac,
        "id": f"dev-{mac[-1]}",
        "type": "switch",
        "model": "EX4100-48P",
        "port_config": {"ge-0/0/0": {"usage": "office"}},
    }
    if override_edge is not None:
        device["port_usages"] = {
            "office": {
                "mode": "access",
                "port_network": "corp",
                "stp_edge": override_edge,
            }
        }
    return device


def _raw(devices: tuple[dict, ...]) -> RawSiteState:
    return RawSiteState(
        scope=SiteScope(ORG, SITE),
        site={"id": SITE},
        setting={
            "networks": {"corp": {"vlan_id": 10}},
            "port_usages": {
                "office": {
                    "mode": "access",
                    "port_network": "corp",
                    "stp_edge": False,
                }
            },
        },
        networktemplate=None,
        devices=devices,
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=StateMeta(
            acquired_at=datetime.now(UTC),
            host="test",
            fetched=("site", "setting", "devices"),
            failures=(),
        ),
    )


class Provider:
    def __init__(self, raw: RawSiteState):
        self.raw = raw

    def fetch_site(self, scope, *, include_derived=False):
        return self.raw


def _plan() -> dict:
    return {
        "source": "mist",
        "scope": {"org_id": ORG, "site_id": SITE},
        "ops": [{
            "action": "update",
            "order": 0,
            "object_type": "site_setting",
            "object_id": SITE,
            "payload": {
                "port_usages": {
                    "office": {
                        "mode": "access",
                        "port_network": "corp",
                        "stp_edge": True,
                    }
                }
            },
        }],
    }


def test_fully_overridden_lower_layer_change_is_reported():
    raw = _raw((
        _switch("aa0000000001", override_edge=False),
        _switch("aa0000000002", override_edge=False),
    ))
    verdict = simulate(_plan(), provider=Provider(raw))

    assert verdict.decision is Decision.REVIEW
    finding = next(
        finding for finding in verdict.findings
        if finding.code == "scope.effective_noop.fully_overridden"
    )
    assert finding.evidence["paths"] == ["port_usages.office.stp_edge"]
    assert finding.affected_entities == ("aa0000000001", "aa0000000002")


def test_partially_overridden_lower_layer_change_is_reported():
    raw = _raw((
        _switch("aa0000000001", override_edge=False),
        _switch("aa0000000002", override_edge=None),
    ))
    verdict = simulate(_plan(), provider=Provider(raw))

    assert verdict.decision is Decision.REVIEW
    finding = next(
        finding for finding in verdict.findings
        if finding.code == "scope.effective_noop.partially_overridden"
    )
    assert finding.evidence["overridden_devices_by_path"] == {
        "port_usages.office.stp_edge": ["aa0000000001"]
    }
    assert finding.evidence["applied_devices_by_path"] == {
        "port_usages.office.stp_edge": ["aa0000000002"]
    }
