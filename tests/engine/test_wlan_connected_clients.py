"""Current client associations gate WLAN changes on site AND org paths.

Seven-day session history is not proof of absence: a client that is still
connected may have no session record yet. A WLAN change is SAFE only when the
history is clean AND the current association evidence is complete and clean.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from digital_twin.engine.pipeline import simulate, simulate_org_plan
from digital_twin.providers.base import FetchFailure
from digital_twin.verdict.decision import Decision
from tests.engine import test_org_plan as org
from tests.engine.test_pipeline import AP, FakeProvider, _plan, _raw_wlan, _wlan

PSK = {"type": "psk", "psk": "secret"}


def _site_raw(*clients: dict[str, Any], wireless: str = "ok", auth: dict[str, Any] = PSK):
    raw = _raw_wlan(_wlan() | {"auth": dict(auth), "isolation": True}, clients=clients)
    if wireless == "failed":
        meta = replace(
            raw.meta,
            fetched=tuple(f for f in raw.meta.fetched if f != "wireless_clients"),
            failures=(FetchFailure(object="wireless_clients", error="HTTP 500"),),
        )
        raw = replace(raw, meta=meta)
    elif wireless == "missing":
        meta = replace(
            raw.meta, fetched=tuple(f for f in raw.meta.fetched if f != "wireless_clients")
        )
        raw = replace(raw, meta=meta)
    return raw


def _site_update(raw, payload):
    op = {"action": "update", "order": 0, "object_type": "wlan", "object_id": "w1",
          "payload": payload}
    return simulate(_plan([op]), provider=FakeProvider(raw))


def _codes(verdict) -> set[str]:
    return {f.code for f in verdict.findings}


def _connected(**extra: Any) -> dict[str, Any]:
    return {"mac": "11:22:33:44:55:66", "ap_mac": AP["mac"], **extra}


# --- site path ---------------------------------------------------------------


def test_site_psk_to_eap_with_complete_empty_evidence_is_safe():
    v = _site_update(_site_raw(), {"auth": {"type": "eap"}})
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_site_client_on_another_wlan_is_not_usage():
    v = _site_update(_site_raw(_connected(ssid="guest", wlan_id="w9")), {"auth": {"type": "eap"}})
    assert v.decision is Decision.SAFE, v.decision_reasons


@pytest.mark.parametrize("wireless", ["failed", "missing"])
def test_site_psk_to_eap_without_current_client_telemetry_is_review(wireless):
    v = _site_update(_site_raw(wireless=wireless), {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.unverified" in _codes(v)


@pytest.mark.parametrize("wireless", ["failed", "missing"])
def test_site_secure_to_open_without_current_client_telemetry_is_partial_review(wireless):
    v = _site_update(_site_raw(wireless=wireless), {"auth": {"type": "open"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.auth_transition.unverified" in _codes(v)


def test_site_psk_to_eap_with_unidentified_connected_client_is_review():
    v = _site_update(_site_raw(_connected()), {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.unverified" in _codes(v)


def test_site_secure_to_open_with_unidentified_connected_client_is_review():
    v = _site_update(_site_raw(_connected()), {"auth": {"type": "open"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.auth_transition.unverified" in _codes(v)


def test_site_band_removal_ignores_clients_proven_on_another_band():
    raw = _raw_wlan(_wlan() | {"bands": ["24", "5"]}, clients=(_connected(ssid="corp", band="5"),))
    v = _site_update(raw, {"bands": ["5"]})
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_site_band_removal_counts_clients_with_unknown_band():
    raw = _raw_wlan(_wlan() | {"bands": ["24", "5"]}, clients=(_connected(ssid="corp"),))
    v = _site_update(raw, {"bands": ["5"]})
    assert v.decision is Decision.REVIEW, v.decision_reasons


# --- org path ----------------------------------------------------------------


def _org_provider(*clients: dict[str, Any], clients_fetched: bool = True,
                  failed: bool = False, auth: dict[str, Any] = PSK):
    row = {**org._wlan_row(), "isolation": True, "auth": dict(auth)}
    site = org._wlan_site("s1", wlans=(row,), clients=clients, clients_fetched=clients_fetched)
    if failed:
        site = replace(site, meta=replace(
            site.meta, failures=(FetchFailure(object="wireless_clients", error="HTTP 500"),)
        ))
    return org.FakeProvider(
        {"s1": site}, {}, org_wlans={"w1": row}, wlan_membership={"w1": {"s1": row}}
    )


def _org_update(provider, payload):
    return simulate_org_plan(org._plan(org._upd("wlan", "w1", payload)), provider=provider)


def _org_codes(verdict) -> set[str]:
    return {f.code for f in verdict.template_findings}


def test_org_psk_to_eap_with_complete_empty_evidence_is_safe():
    v = _org_update(_org_provider(), {"auth": {"type": "eap"}})
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_org_psk_to_eap_with_connected_client_and_empty_history_is_review():
    v = _org_update(_org_provider(org._client()), {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.recent_usage" in _org_codes(v)


def test_org_secure_to_open_with_connected_client_and_empty_history_is_unsafe():
    v = _org_update(_org_provider(org._client()), {"auth": {"type": "open"}})
    assert v.decision is Decision.UNSAFE, v.decision_reasons
    assert "wireless.wlan.auth_transition.recent_usage" in _org_codes(v)


@pytest.mark.parametrize("kwargs", [{"clients_fetched": False}, {"failed": True}])
def test_org_psk_to_eap_without_current_client_telemetry_is_review(kwargs):
    v = _org_update(_org_provider(**kwargs), {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.unverified" in _org_codes(v)


def test_org_psk_to_eap_with_unidentified_connected_client_is_review():
    unidentified = {"mac": org.WIRELESS_CLIENT, "ap_mac": org.AP}
    v = _org_update(_org_provider(unidentified), {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.unverified" in _org_codes(v)


# --- WLAN identity -------------------------------------------------------------


def _shared_ssid_raw(*clients: dict[str, Any], auth: dict[str, Any] = PSK):
    target = _wlan("w1") | {"auth": dict(auth), "isolation": True}
    return _raw_wlan(target, _wlan("w2"), clients=clients)


def test_client_wlan_id_is_authoritative_over_its_ssid():
    raw = _site_raw(_connected(wlan_id="w1", ssid="renamed"))
    v = _site_update(raw, {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.recent_usage" in _codes(v)


def test_update_with_shared_ssid_client_is_unverified_review():
    v = _site_update(_shared_ssid_raw(_connected(ssid="corp")), {"auth": {"type": "eap"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.change.unverified" in _codes(v)


def test_secure_to_open_with_shared_ssid_client_is_review_not_unsafe():
    v = _site_update(_shared_ssid_raw(_connected(ssid="corp")), {"auth": {"type": "open"}})
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.auth_transition.unverified" in _codes(v)


def test_disabled_same_ssid_wlan_does_not_make_the_client_ambiguous():
    target = _wlan("w1") | {"auth": dict(PSK), "isolation": True}
    raw = _raw_wlan(target, _wlan("w2", enabled=False), clients=(_connected(ssid="corp"),))
    v = _site_update(raw, {"auth": {"type": "open"}})
    assert v.decision is Decision.UNSAFE, v.decision_reasons


def test_delete_leaves_shared_ssid_clients_to_the_coverage_check_visibly():
    op = {"action": "delete", "order": 0, "object_type": "wlan", "object_id": "w1",
          "payload": {}}
    v = simulate(_plan([op]), provider=FakeProvider(_shared_ssid_raw(_connected(ssid="corp"))))
    gate = next(r for r in v.check_results if r.check_id == "wireless.wlan.recent_usage")
    assert gate.status.value == "pass"
    assert v.decision is Decision.SAFE, v.decision_reasons


def test_delete_with_failed_client_telemetry_is_review():
    op = {"action": "delete", "order": 0, "object_type": "wlan", "object_id": "w1",
          "payload": {}}
    v = simulate(_plan([op]), provider=FakeProvider(_site_raw(wireless="failed")))
    assert v.decision is Decision.REVIEW, v.decision_reasons
    assert "wireless.wlan.recent_usage.unverified" in _codes(v)
