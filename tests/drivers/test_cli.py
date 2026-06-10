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
