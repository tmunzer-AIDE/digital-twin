import json
from dataclasses import dataclass
from datetime import UTC, datetime

from digital_twin.drivers.cli import _is_org_plan, main
from digital_twin.observability.replay.store import ReplayStore, load_fixture_raw
from digital_twin.providers.base import (
    OrgTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
)
from tests.adapters.mist.fixtures import raw_site

GS8_PLAN = {
    "source": "mist",
    "scope": {"org_id": "o1", "site_id": "s1"},
    "ops": [
        {
            "action": "update",
            "order": 0,
            "object_type": "networktemplate",
            "object_id": "nt1",
            "payload": {},
        }
    ],
}

# ---------------------------------------------------------------------------
# helpers for org-path tests
# ---------------------------------------------------------------------------

def _meta():
    return StateMeta(
        acquired_at=datetime.now(UTC), host="h",
        fetched=("site", "setting", "devices", "wireless_clients", "wired_clients"),
        failures=(),
    )


def _org_site(sid, *, setting, devices, nt):
    return RawSiteState(
        scope=SiteScope("o1", sid),
        site={"id": sid, "networktemplate_id": "nt1"},
        setting=setting,
        networktemplate=nt,
        devices=tuple(devices),
        device_stats=(),
        port_stats=(),
        wireless_clients=(),
        wired_clients=(),
        derived_setting=None,
        meta=_meta(),
    )


@dataclass
class _OrgFakeProvider:
    """Minimal fake provider for org-template CLI tests."""
    sites: dict
    template: dict

    def resolve_org_template(self, scope, template_id, object_type="networktemplate"):
        return OrgTemplateContext(template=self.template, assigned_site_ids=tuple(self.sites))

    def fetch_sites(self, scope, site_ids=None, *, include_derived=False):
        ids = list(site_ids) if site_ids is not None else list(self.sites)
        return {sid: self.sites[sid] for sid in ids}

    def fetch_site(self, scope, *, include_derived=False):
        raise NotImplementedError


def _fixture(tmp_path):
    return ReplayStore(tmp_path).save_raw("fx", raw_site())


def test_unknown_exits_30_and_prints_json(tmp_path, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(GS8_PLAN))
    code = main(["--plan", str(plan), "--replay-fixture", str(_fixture(tmp_path)), "--json"])
    assert code == 30
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "unknown"


def test_replay_store_captures_the_exact_state_the_run_used(tmp_path, capsys, monkeypatch):
    # the saved replay must be the FIRST fetch (the one the verdict judged),
    # not a second fetch that could differ on a live provider — exactly ONE
    # provider fetch happens even with --replay-store
    from digital_twin.observability.replay.store import FixtureProvider

    calls = {"n": 0}
    original = FixtureProvider.fetch_site

    def counting(self, scope, *, include_derived=False):
        calls["n"] += 1
        return original(self, scope, include_derived=include_derived)

    monkeypatch.setattr(FixtureProvider, "fetch_site", counting)
    fixture = _fixture(tmp_path)
    fx_raw = load_fixture_raw(fixture)
    plan = {
        "source": "mist",
        "scope": {"org_id": fx_raw.scope.org_id, "site_id": fx_raw.scope.site_id},
        "ops": [
            {
                "action": "update",
                "order": 0,
                "object_type": "site_setting",
                "object_id": fx_raw.scope.site_id,
                "payload": dict(fx_raw.setting),
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    store_dir = tmp_path / "runs"
    code = main(
        [
            "--plan",
            str(plan_path),
            "--replay-fixture",
            str(fixture),
            "--replay-store",
            str(store_dir),
        ]
    )
    assert code == 0
    (saved,) = sorted(store_dir.glob("*.json"))
    doc = json.loads(saved.read_text())
    assert doc["verdict"]["decision"] == "safe"
    assert doc["trace"]["stages"]  # the run's own trace rode along
    # the captured raw is the state the run used (fixture scope matches)
    assert doc["scope"]["site_id"] == fx_raw.scope.site_id
    assert calls["n"] == 1  # ONE fetch: the recorded state IS what the run judged


def test_cli_l0_full_object_flag_threads_through(tmp_path, monkeypatch):
    # --l0-full-object flips the engine's L0 scope from changed-roots (default)
    # to whole-object; the CLI must pass the flag through on the SITE path
    import digital_twin.drivers.cli as cli

    seen = []
    real = cli.simulate

    def spy(plan_data, **kwargs):
        seen.append(kwargs.get("l0_full_object"))
        return real(plan_data, **kwargs)

    monkeypatch.setattr(cli, "simulate", spy)

    fixture = _fixture(tmp_path)
    fx_raw = load_fixture_raw(fixture)
    plan = {
        "source": "mist",
        "scope": {"org_id": fx_raw.scope.org_id, "site_id": fx_raw.scope.site_id},
        "ops": [{"action": "update", "order": 0, "object_type": "site_setting",
                 "object_id": fx_raw.scope.site_id, "payload": dict(fx_raw.setting)}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))

    assert main(["--plan", str(plan_path), "--replay-fixture", str(fixture)]) == 0
    assert main(
        ["--plan", str(plan_path), "--replay-fixture", str(fixture), "--l0-full-object"]
    ) == 0
    assert seen == [False, True]


def test_safe_noop_exits_0_human(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    fx_raw = load_fixture_raw(fixture)  # scope/site are REDACTED in the fixture
    plan = {
        "source": "mist",
        "scope": {"org_id": fx_raw.scope.org_id, "site_id": fx_raw.scope.site_id},
        "ops": [
            {
                "action": "update",
                "order": 0,
                "object_type": "site_setting",
                "object_id": fx_raw.scope.site_id,
                "payload": dict(fx_raw.setting),
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    code = main(["--plan", str(plan_path), "--replay-fixture", str(fixture)])
    assert code == 0
    assert "decision: SAFE" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _is_org_plan unit tests
# ---------------------------------------------------------------------------

def test_is_org_plan_recognizes_all_org_types():
    for t in ("networktemplate", "gatewaytemplate", "sitetemplate"):
        plan = {
            "source": "mist",
            "scope": {"org_id": "o1"},
            "ops": [{"action": "update", "order": 0, "object_type": t,
                     "object_id": "nt1", "payload": {}}],
        }
        assert _is_org_plan(plan) is True, f"{t!r} should be recognized as ORG type"
    assert _is_org_plan({
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0, "object_type": "device",
                 "object_id": "d1", "payload": {}}],
    }) is False


def test_is_org_plan_true():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                 "object_id": "nt1", "payload": {}}],
    }
    assert _is_org_plan(plan) is True


def test_is_org_plan_false_with_site_id():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                 "object_id": "nt1", "payload": {}}],
    }
    assert _is_org_plan(plan) is False


def test_is_org_plan_false_non_template_op():
    plan = {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0, "object_type": "site_setting",
                 "object_id": "s1", "payload": {}}],
    }
    assert _is_org_plan(plan) is False


def test_is_org_plan_false_malformed_ops_not_list():
    assert _is_org_plan({"source": "mist", "ops": "not-a-list", "scope": {"org_id": "o1"}}) is False


def test_is_org_plan_false_op_missing_object_type():
    assert _is_org_plan({
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0}],
    }) is False


# ---------------------------------------------------------------------------
# CLI org dispatch tests
# ---------------------------------------------------------------------------

def _org_plan(payload=None):
    return {
        "source": "mist",
        "scope": {"org_id": "o1"},
        "ops": [{"action": "update", "order": 0, "object_type": "networktemplate",
                 "object_id": "nt1", "payload": payload or {}}],
    }


def test_org_plan_routed_to_org_path_json(tmp_path, capsys, monkeypatch):
    """An org-template plan is dispatched to simulate_org_template; exit code
    matches the org decision (SAFE=0) and JSON output contains 'changes'."""
    tmpl = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    s1 = _org_site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    fake = _OrgFakeProvider(sites={"s1": s1}, template=tmpl)

    import digital_twin.providers.mist_api as mist_mod
    monkeypatch.setattr(mist_mod, "MistApiProvider", lambda: fake)

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_org_plan({"networks": {"corp": {"vlan_id": 10}}})))
    code = main(["--plan", str(plan_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0  # SAFE
    assert out["decision"] == "safe"
    assert out["changes"][0]["object_id"] == "nt1"
    assert "s1" in out["per_site"]


def test_org_plan_routed_to_org_path_human(tmp_path, capsys, monkeypatch):
    """Human-readable org output starts with 'org decision:' and includes a per-site line."""
    tmpl = {"id": "nt1", "networks": {}}
    s1 = _org_site("s1", setting={"id": "s1"}, devices=(), nt=tmpl)
    fake = _OrgFakeProvider(sites={"s1": s1}, template=tmpl)

    import digital_twin.providers.mist_api as mist_mod
    monkeypatch.setattr(mist_mod, "MistApiProvider", lambda: fake)

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_org_plan({"networks": {}})))
    code = main(["--plan", str(plan_path)])
    output = capsys.readouterr().out
    assert code == 0
    assert "org decision:" in output
    assert "s1" in output  # per-site line rendered


# ---------------------------------------------------------------------------
# Malformed-plan regression: CLI must NOT crash, must exit 30 (UNKNOWN)
# ---------------------------------------------------------------------------

def test_malformed_plan_ops_not_list_exits_30(tmp_path, capsys, monkeypatch):
    """A plan with ops='not-a-list' falls through to the SITE path and is
    envelope-rejected; the driver must exit 30 (UNKNOWN), not crash."""
    from digital_twin.observability.replay.store import ReplayStore
    fixture = ReplayStore(tmp_path).save_raw("fx", raw_site())
    bad_plan = {"source": "mist", "scope": {"org_id": "o1", "site_id": "s1"},
                "ops": "not-a-list"}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(bad_plan))
    code = main(["--plan", str(plan_path), "--replay-fixture", str(fixture)])
    assert code == 30


def test_malformed_plan_op_missing_object_type_exits_30(tmp_path, capsys, monkeypatch):
    """A plan whose op dict lacks 'object_type' is rejected as UNKNOWN (exit 30)."""
    from digital_twin.observability.replay.store import ReplayStore
    fixture = ReplayStore(tmp_path).save_raw("fx", raw_site())
    bad_plan = {
        "source": "mist",
        "scope": {"org_id": "o1", "site_id": "s1"},
        "ops": [{"action": "update", "order": 0, "object_id": "x", "payload": {}}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(bad_plan))
    code = main(["--plan", str(plan_path), "--replay-fixture", str(fixture)])
    assert code == 30
