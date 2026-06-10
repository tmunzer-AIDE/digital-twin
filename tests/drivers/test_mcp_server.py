from digital_twin.drivers.mcp_server import simulate_change
from digital_twin.observability.replay.store import ReplayStore
from tests.adapters.mist.fixtures import raw_site


def test_tool_returns_verdict_dict_and_never_raises(tmp_path):
    fixture = ReplayStore(tmp_path).save_raw("fx", raw_site())
    out = simulate_change({"source": "mist", "ops": "garbage"}, replay_fixture=str(fixture))
    assert out["decision"] == "unknown"  # bad plan -> verdict, not an exception


def test_tool_isolates_internal_errors(tmp_path):
    out = simulate_change({"source": "mist"}, replay_fixture=str(tmp_path / "missing.json"))
    assert out["decision"] == "unknown"
    assert any("error" in r.lower() or "fixture" in r.lower() for r in out["decision_reasons"])


def test_error_path_returns_the_full_verdict_document_shape(tmp_path):
    # agents need predictable fields ESPECIALLY on errors: the error path must
    # be a real assembled Verdict, not a hand-rolled 3-key dict
    ok = simulate_change(
        {"source": "mist", "ops": "garbage"},
        replay_fixture=str(ReplayStore(tmp_path).save_raw("fx", raw_site())),
    )
    err = simulate_change({"source": "mist"}, replay_fixture=str(tmp_path / "missing.json"))
    assert set(err.keys()) == set(ok.keys())  # identical document shape
