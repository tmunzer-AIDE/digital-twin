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
