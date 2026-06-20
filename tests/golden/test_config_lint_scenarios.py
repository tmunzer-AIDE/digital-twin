"""GS30-GS33 end-to-end goldens: config-lint tier driven through simulate().

Five scenarios cover the two delta-conditioning outcomes:
  INTRODUCED -> Decision.REVIEW  (lint violation new in the proposed state)
  PRE-EXISTING -> Decision.SAFE  (violation existed in baseline; INFO context only)

The builder creates MINIMAL single-site docs (no real-fixture noise) so the
field gate sees only the test-specific network/wlan diff — not the many
unmodelled leaves from the real captured fixture.
"""

from digital_twin.engine.pipeline import simulate
from digital_twin.observability.replay.store import FixtureProvider
from digital_twin.verdict.decision import Decision
from tests.golden.builders import config_lint_base_doc, write_doc


def _run(doc, plan, tmp_path, tag):
    return simulate(plan, provider=FixtureProvider(write_doc(doc, tmp_path / f"{tag}.json")))


def test_gs30_introduced_vlan_collision_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="vlan_collision_introduce")
    v = _run(doc, plan, tmp_path, "gs30")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wired.l2.vlan_collision.introduced" for f in v.findings)


def test_gs31_introduced_subnet_overlap_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="subnet_overlap_introduce")
    v = _run(doc, plan, tmp_path, "gs31")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wired.l3.subnet_overlap.introduced" for f in v.findings)


def test_gs33_open_guest_remove_isolation_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="open_guest_introduce")
    v = _run(doc, plan, tmp_path, "gs33")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wireless.wlan.open_guest.introduced" for f in v.findings)


def test_gs32_duplicate_ssid_introduced_is_review(tmp_path):
    doc, plan = config_lint_base_doc(kind="duplicate_ssid_introduce")
    v = _run(doc, plan, tmp_path, "gs32")
    assert v.decision is Decision.REVIEW
    assert any(f.code == "wireless.wlan.duplicate_ssid.introduced" for f in v.findings)


def test_preexisting_collision_with_benign_edit_is_safe_info(tmp_path):
    # Violation already in baseline; a benign in-domain edit produces a diff
    # (applies_to fires) but leaves the collision key unchanged -> INFO, SAFE.
    doc, plan = config_lint_base_doc(kind="vlan_collision_preexisting")
    v = _run(doc, plan, tmp_path, "pre")
    assert v.decision is Decision.SAFE
    assert any(f.code == "wired.l2.vlan_collision.preexisting" for f in v.findings)
