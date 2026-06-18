"""Template apply (Mist root-level update semantics).

The baseline-snapshot pinning that used to live here (override_template) was
generalized into engine/org_overlay.apply_overlays for the multi-op delete-ripple
engine; its behavior is covered by tests/engine/test_org_overlay.py.
"""

from digital_twin.contracts import Rejection
from digital_twin.engine.org_template import apply_template


def test_apply_template_edits_one_snapshot():
    snap = {"id": "nt1", "networks": {"corp": {"vlan_id": 10}}}
    out = apply_template(snap, {"networks": {"corp": {"vlan_id": 20}}})
    assert out == {"id": "nt1", "networks": {"corp": {"vlan_id": 20}}}  # root replace, id preserved


def test_apply_template_set_and_delete_conflict_rejects():
    r = apply_template({"id": "nt1"}, {"networks": {}, "-networks": ""})
    assert isinstance(r, Rejection) and r.stage == "apply"
