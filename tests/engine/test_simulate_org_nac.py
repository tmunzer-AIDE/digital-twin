from dataclasses import dataclass

from digital_twin.engine.pipeline import simulate_org_nac
from digital_twin.providers.base import FetchError, NacFetch, OrgScope
from digital_twin.verdict.decision import Decision


@dataclass
class FakeProvider:
    fetch: object
    def resolve_org_nac(self, scope: OrgScope):
        return self.fetch


def _rule(id, order, action="allow", **m):
    return {"id": id, "name": id, "order": order, "enabled": True, "action": action,
            "matching": m, "apply_tags": []}


def _plan(*ops, org="o1"):
    return {"source": "mist", "scope": {"org_id": org}, "ops": list(ops)}


def _op(action, oid, payload, order=0):
    return {"action": action, "order": order, "object_type": "nacrule",
            "object_id": oid, "payload": payload}


BASE = (_rule("a", 1), _rule("b", 2, auth_type="cert"))


def test_fetch_error_is_unknown():
    fe = FetchError(scope=OrgScope("o1"), failures=(), acquired_at=None, host="h")  # type: ignore[arg-type]
    v = simulate_org_nac(_plan(_op("update", "b", {"name": "b2"})), provider=FakeProvider(fe))
    assert v.decision is Decision.UNKNOWN


def test_noop_update_same_value_is_safe():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"name": "b"})),
                         provider=FakeProvider(nf))
    # name unchanged from baseline → empty diff → SAFE
    assert v.decision is Decision.SAFE


def test_reorder_delta_is_review():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any(c.kind == "modified" and c.rule_id == "b" for c in v.changes)


def test_create_broad_rule_buries_existing_review_with_shadow():
    nf = NacFetch(rules=BASE, tags=())
    # create a catch-all at order 0 → shadows a and b
    v = simulate_org_nac(_plan(_op("create", "z", _rule("z", 0))), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    codes = [f.code for r in v.check_results for f in r.findings]
    assert any(c.endswith("nac.rule.shadowed.introduced") for c in codes)


def test_create_with_existing_id_rejected_unknown():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("create", "a", _rule("a", 9))), provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN


def test_unallowlisted_field_is_unknown():
    nf = NacFetch(rules=BASE, tags=())
    payload = {"guest_auth_state": "x"}
    v = simulate_org_nac(_plan(_op("update", "b", payload)), provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN


def test_conflict_marker_is_unknown():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"matching": {}, "-matching": ""})),
                         provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN


def test_nactags_failure_is_review_not_unknown():
    from digital_twin.contracts import Severity
    nf = NacFetch(rules=BASE, tags=(), tag_findings=(
        _tag_finding(),))
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any(f.severity is Severity.WARNING for f in v.adapter_findings)


def _tag_finding():
    from digital_twin.providers.mist_api import _nactag_fetch_finding
    return _nactag_fetch_finding("boom")


def test_delete_drops_row_review():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("delete", "b", {})), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any(c.kind == "removed" and c.rule_id == "b" for c in v.changes)


def test_partial_update_no_bogus_required():
    nf = NacFetch(rules=BASE, tags=())
    # payload omits name/action; they're inherited from baseline → no L0 'required' error
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 9})), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert not any("required" in f.message for f in v.adapter_findings)


def test_non_fatal_l0_is_review_with_finding():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"enabled": "yes"})),
                         provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any("enabled" in (f.evidence.get("path", "") + f.message)
               for f in v.adapter_findings)


def test_malformed_in_both_states_still_diffs():
    # auth_type=1 (int) is malformed → ingest raises → opaque_digest set on the rule.
    # Scalar at the raw level so the field_gate sees "matching.auth_type" (allowlisted),
    # not "matching.auth_type.k" (which would be UNKNOWN). Changing 1→2 produces a
    # different opaque_digest → the diff still sees a "modified" even though parsing fails.
    bad_a = {"id": "a", "name": "a", "order": 1, "enabled": True, "action": "allow",
             "matching": {"auth_type": 1}, "apply_tags": []}
    nf = NacFetch(rules=(bad_a, _rule("b", 2)), tags=())
    v = simulate_org_nac(
        _plan(_op("update", "a", {"matching": {"auth_type": 2}})),
        provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any(c.rule_id == "a" and c.kind == "modified" for c in v.changes)


def test_id_less_fetched_row_surfaces_warning():
    # a fetched row with no id is dropped by ingest BUT must still emit an operational
    # warning (regression: building the baseline IR from the id-keyed dict hid it)
    nf = NacFetch(rules=({"name": "ghost", "action": "allow"}, _rule("b", 2)), tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any(f.code == "nac.ingest.dropped" for f in v.adapter_findings)


def test_duplicate_baseline_id_no_phantom_diff():
    # fetch has two rows with id "a" — ingester is first-wins; the orchestrator's
    # baseline_raw must match (NOT last-wins via dict comp) so base_ir == proposed and
    # NO phantom 'a' modify appears. The duplicate WARNING still surfaces → REVIEW.
    nf = NacFetch(rules=(_rule("a", 1), _rule("a", 2), _rule("b", 3)), tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"name": "b"})), provider=FakeProvider(nf))
    assert not any(c.rule_id == "a" for c in v.changes)          # no phantom diff on the dup
    assert any(f.code == "nac.ingest.duplicate" for f in v.adapter_findings)
    assert v.decision is Decision.REVIEW


def test_crashing_nac_check_degrades_to_review_not_crash(monkeypatch):
    # a buggy NAC check must be isolated by CheckRegistry to a CHECK_ERROR result
    # (→ decide() floors REVIEW) — it must NOT escape simulate_org_nac as a traceback.
    from digital_twin.checks.base import Status
    from digital_twin.checks.nac.shadowing import NacShadowingCheck

    def _boom(self, ctx):  # noqa: ANN001, ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(NacShadowingCheck, "run", _boom)
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    assert v.decision is Decision.REVIEW
    assert any(r.status is Status.CHECK_ERROR for r in v.check_results)


def test_config_diff_update_shows_redacted_before_after():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"order": 0})), provider=FakeProvider(nf))
    cds = {d.object_id: d for d in v.config_diffs}
    assert "b" in cds and cds["b"].object_type == "nacrule" and cds["b"].action == "update"
    by = {c.path: c for c in cds["b"].changes}
    assert by["order"].kind == "changed" and by["order"].before == 2 and by["order"].after == 0


def test_config_diff_create_lists_added_leaves():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("create", "z", _rule("z", 0))), provider=FakeProvider(nf))
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["z"].action == "create"
    assert cds["z"].name == "z"  # name comes from `effective`, not the {"id":...} stub
    assert {c.kind for c in cds["z"].changes} == {"added"}
    paths = {c.path for c in cds["z"].changes}
    assert "order" in paths and "action" in paths


def test_config_diff_delete_lists_removed_leaves():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("delete", "b", {})), provider=FakeProvider(nf))
    cds = {d.object_id: d for d in v.config_diffs}
    assert cds["b"].action == "delete"
    assert {c.kind for c in cds["b"].changes} == {"removed"}
    paths = {c.path for c in cds["b"].changes}
    assert "order" in paths and "action" in paths


def test_config_diff_empty_on_unknown():
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(_plan(_op("update", "b", {"guest_auth_state": "x"})),
                         provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    assert v.config_diffs == ()


def test_config_diff_dropped_when_later_op_makes_plan_unknown():
    # op-b accumulates a diff, then op-a (unallowlisted field) → UNKNOWN.
    # The decision-gate must drop the accumulated diff: config_diffs == ().
    nf = NacFetch(rules=BASE, tags=())
    v = simulate_org_nac(
        _plan(_op("update", "b", {"order": 0}, order=0),
              _op("update", "a", {"guest_auth_state": "x"}, order=1)),
        provider=FakeProvider(nf))
    assert v.decision is Decision.UNKNOWN
    assert v.config_diffs == ()
