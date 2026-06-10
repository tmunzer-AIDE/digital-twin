from digital_twin.observability.logging import bound_logger
from digital_twin.observability.trace import Trace


def test_trace_records_stages_in_order_with_timing():
    t = Trace(run_id="r1")
    with t.stage("fetch"):
        pass
    with t.stage("ingest_baseline", note="19 devices"):
        pass
    d = t.to_dict()
    assert d["run_id"] == "r1"
    assert [s["stage"] for s in d["stages"]] == ["fetch", "ingest_baseline"]
    assert all(s["duration_ms"] >= 0 for s in d["stages"])
    assert d["stages"][1]["note"] == "19 devices"


def test_stage_records_even_when_body_raises():
    t = Trace(run_id="r1")
    try:
        with t.stage("boom"):
            raise ValueError("x")
    except ValueError:
        pass
    assert t.to_dict()["stages"][0]["error"] == "x"


def test_bound_logger_smoke():
    log = bound_logger(run_id="r1", check_id="wired.l2.loop")
    log.info("hello")  # must not raise; binding is in the extra
