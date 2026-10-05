"""Every driver entry point holds device renames to the post-fetch matcher proof.

The CLI and MCP drivers run the name-only rule BEFORE choosing a provider, and
the composite driver routes renames through its own segments; none of them may
hand a device rename the pre-fetch SAFE (the pre-fetch rule itself is pinned in
tests/engine/test_name_change.py, the proof in tests/engine/test_device_rename.py).
"""

import json

from digital_twin.drivers.composite import simulate_composite
from digital_twin.providers.base import OrgNetworksContext
from tests.engine.test_device_rename import SITE, _raw, _rename


class _Provider:
    def __init__(self, state):
        self.state = state
        self.fetches = 0

    def fetch_site(self, scope, *, include_derived=False):
        self.fetches += 1
        return self.state

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        self.fetches += 1
        return {SITE: self.state}

    def resolve_org_networks(self, scope):
        return OrgNetworksContext(())


def test_mcp_driver_fetches_and_rejects_an_uncompiled_rule_flip(monkeypatch):
    import digital_twin.drivers.mcp_server as srv

    provider = _Provider(_raw())
    monkeypatch.setattr(srv, "_provider", lambda replay_fixture: provider)

    out = srv.simulate_change(_rename("sw-1", "core-01"))

    assert out["decision"] == "unknown"
    assert provider.fetches >= 1


def test_cli_driver_fetches_and_rejects_an_uncompiled_rule_flip(
    monkeypatch, tmp_path, capsys
):
    import digital_twin.drivers.cli as cli

    provider = _Provider(_raw())
    monkeypatch.setattr(cli, "FixtureProvider", lambda path: provider)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_rename("sw-1", "core-01")))

    code = cli.main(["--plan", str(plan), "--json", "--replay-fixture", "unused"])

    assert code == cli.EXIT_CODES[cli.Decision.UNKNOWN]
    assert json.loads(capsys.readouterr().out)["decision"] == "unknown"
    assert provider.fetches >= 1


def test_composite_site_segment_rejects_an_uncompiled_rule_flip():
    plan = _rename("sw-1", "core-01")
    plan["ops"][0]["scope"] = "site"
    plan["ops"].append({
        "action": "update", "order": 1, "scope": "org", "object_type": "wxtags",
        "object_id": "t1", "payload": {"name": "lobby"},
    })

    out = simulate_composite(plan, provider=_Provider(_raw()))

    assert out["decision"] == "unknown"
    device = next(a for a in out["change_assessments"] if a["object_id"] == "sw-1")
    assert device["decision"] == "unknown"
    # the original -> composed batch pass reaches the same floor on its own
    batch = next(s for s in out["segments"] if s["route"] == "batch")
    assert batch["verdict"]["decision"] == "unknown"


def test_composite_org_scoped_device_rename_is_review():
    # no site_id: the device rename lands on the composite "name" route
    plan = _rename("sw-1", "sw-ex4100-02", site_id=None)
    plan["ops"].append({
        "action": "update", "order": 1, "object_type": "wxtags",
        "object_id": "t1", "payload": {"name": "lobby"},
    })

    out = simulate_composite(plan, provider=_Provider(_raw()))

    assert out["decision"] == "review"
    assert [s["route"] for s in out["segments"]][0] == "name"


def test_composite_safe_switch_rename_stays_safe():
    plan = _rename("sw-1", "sw-ex4100-02")
    plan["ops"][0]["scope"] = "site"
    plan["ops"].append({
        "action": "update", "order": 1, "scope": "org", "object_type": "wxtags",
        "object_id": "t1", "payload": {"name": "lobby"},
    })

    out = simulate_composite(plan, provider=_Provider(_raw()))

    assert out["decision"] == "safe", out["decision_reasons"]
