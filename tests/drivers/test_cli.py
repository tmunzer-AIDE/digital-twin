import json

from digital_twin.drivers.cli import main
from digital_twin.observability.replay.store import ReplayStore, load_fixture_raw
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
